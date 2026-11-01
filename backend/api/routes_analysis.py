"""
分析控制与队列管理路由
POST /api/analysis/start|stop、GET /api/analysis/status
GET/PUT /api/queue、POST /api/queue/scan、队列项操作
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.api.auth import require_session_token
from backend.services.queue_manager import QueueItem
from backend.services.queue_service import get_service
from backend.services import workspace_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["analysis"])


# ==================== 分析控制 ====================

@router.post("/analysis/start")
async def start_analysis() -> dict:
    service = get_service()
    if service.is_running:
        raise HTTPException(status_code=409, detail="分析已在运行中")
    if not service.start():
        raise HTTPException(status_code=400, detail="队列为空或已全部完成")
    return {"ok": True}


@router.post("/analysis/stop")
async def stop_analysis() -> dict:
    service = get_service()
    if not service.stop():
        raise HTTPException(status_code=400, detail="没有正在运行的分析")
    return {"ok": True}


@router.get("/analysis/status")
async def analysis_status() -> dict:
    return get_service().status()


@router.get("/analysis/token_stats")
async def analysis_token_stats() -> dict:
    """返回分类 token 统计 + 每章记录"""
    return get_service().token_stats()


@router.get("/analysis/token_stats/session")
async def analysis_token_stats_session() -> dict:
    """本次窗口会话的 token 累计（内存态，重启清零）：
    分析侧 = AnalysisService 单例跨书累计；总结侧 = SummaryService 会话累计。"""
    from backend.services.summary_service import get_summary_service
    return {
        "analysis": get_service().token_stats(),
        "summary": get_summary_service().session_token_stats(),
    }


# ==================== 队列管理 ====================

class ScanRequest(BaseModel):
    base_dir: str


class QueuePutRequest(BaseModel):
    items: list[dict]


class IndexRequest(BaseModel):
    index: int


class DeleteBookRequest(BaseModel):
    index: int


def _ensure_idle():
    service = get_service()
    if service.is_running:
        raise HTTPException(status_code=409, detail="分析运行中，不能修改队列")
    return service


@router.get("/queue")
async def get_queue() -> dict:
    return get_service().status()


@router.put("/queue")
async def put_queue(req: QueuePutRequest) -> dict:
    """全量替换队列（仅空闲时允许）"""
    service = _ensure_idle()
    service.queue.clear()
    for d in req.items:
        # null/空串 blocks_dir 必须拦截：Path(None) 让接口 500，
        # 空串还原成 PROJECT_ROOT 当书入队。用原始 dict 预检 + 构造兜底。
        if not d.get("name") or not d.get("blocks_dir") or not d.get("workspace_dir"):
            continue
        try:
            item = QueueItem.from_dict(d)
        except Exception:
            continue
        service.queue.add_item(item)
    service.save_queue()
    return service.status()


@router.post("/queue/scan")
async def scan_queue(req: ScanRequest) -> dict:
    """扫描 base_dir 下 {书名}/blocks/ 结构，去重加入队列"""
    service = _ensure_idle()
    base = Path(req.base_dir)
    if not base.exists() or not base.is_dir():
        raise HTTPException(status_code=422, detail=f"目录不存在: {req.base_dir}")
    found = await asyncio.to_thread(service.queue.scan_directory, base)
    if not found:
        raise HTTPException(status_code=404, detail="未扫描到任何 {书名}/blocks/*.txt 结构")
    for item in found:
        # 恢复进度推断（沿用配置的 block_size）
        item.block_size = max(1, service.config_manager.config.analysis.block_size)
        done_blocks = service.queue.infer_progress(item)
        item.completed_chapters = done_blocks
        service.queue.add_item(item)
    service.save_queue()
    return service.status()


@router.post("/queue/scan_workspace")
async def scan_workspace() -> dict:
    """刷新工作区目录扫描，自动发现新小说"""
    service = _ensure_idle()
    added = await asyncio.to_thread(service.scan_workspace)
    return {"added": added, **service.status()}


@router.post("/queue/remove")
async def remove_queue_item(req: IndexRequest) -> dict:
    service = _ensure_idle()
    if service.queue.remove_item(req.index) is None:
        raise HTTPException(status_code=422, detail="索引无效")
    service.save_queue()
    return service.status()


@router.post("/queue/move_up")
async def move_up_queue_item(req: IndexRequest) -> dict:
    service = _ensure_idle()
    if not service.queue.move_up(req.index):
        raise HTTPException(status_code=422, detail="无法上移")
    service.save_queue()
    return service.status()


@router.post("/queue/move_down")
async def move_down_queue_item(req: IndexRequest) -> dict:
    service = _ensure_idle()
    if not service.queue.move_down(req.index):
        raise HTTPException(status_code=422, detail="无法下移")
    service.save_queue()
    return service.status()


@router.post("/queue/delete_book")
async def delete_book(req: DeleteBookRequest) -> dict:
    """删除队列项对应的小说：先把文件移入回收站，成功后再从队列移除并持久化。

    顺序要求：先移文件、成功后再动队列，保证任一环节失败都不会出现
    “队列已删但磁盘未删”。若先 remove_item + save_queue 再移回收站，移回收站失败会抛 500，
    但队列项已删、前端刷新后“书凭空消失而文件还在”，状态不一致。"""
    service = _ensure_idle()
    if not (0 <= req.index < service.queue.count):
        raise HTTPException(status_code=422, detail="索引无效")
    item = service.queue.items[req.index]
    workspace_dir = item.workspace_dir

    # 先把书目目录移入系统回收站；用 item.workspace_dir 显式路径——
    # 队列项可能来自任意 base_dir 扫描，不在工作区内，按名字解析会删错/删不到
    if workspace_dir.exists():
        result = workspace_service.delete_novel_to_trash(item.name, novel_path=workspace_dir)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("error", "删除失败"))

    # 文件回收成功（或目录本就不存在）后再从队列移除并持久化
    service.queue.remove_item(req.index)
    service.save_queue()
    return service.status()


@router.post("/queue/reset_item")
async def reset_queue_item(req: IndexRequest) -> dict:
    """把 done/failed/skipped 的项重置为 pending（重跑）"""
    service = _ensure_idle()
    if not (0 <= req.index < service.queue.count):
        raise HTTPException(status_code=422, detail="索引无效")
    item = service.queue.items[req.index]
    item.status = "pending"
    item.error_message = ""
    service.save_queue()
    return service.status()


@router.post("/queue/clear")
async def clear_queue() -> dict:
    service = _ensure_idle()
    service.queue.clear()
    service.save_queue()
    return service.status()


# ==================== 风格分析 ====================

class StyleStartRequest(BaseModel):
    book_id: str
    use_llm: bool = True
    limit: int = 50


@router.post("/style/start")
async def start_style(req: StyleStartRequest) -> dict:
    """启动风格分析"""
    from backend.services import style_service
    try:
        ok = await style_service.start_style_analysis(req.book_id, req.use_llm, req.limit)
        if not ok:
            raise HTTPException(status_code=409, detail="风格分析已在运行中")
        return {"ok": True}
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/style/status")
async def style_status() -> dict:
    """获取风格分析状态"""
    from backend.services import style_service
    return style_service.status()


@router.post("/style/stop")
async def stop_style() -> dict:
    """停止风格分析"""
    from backend.services import style_service
    if not style_service.stop_style_analysis():
        raise HTTPException(status_code=400, detail="没有正在运行的风格分析")
    return {"ok": True}


@router.get("/style/result/{book_id}")
async def get_style_result(book_id: str) -> dict:
    """获取风格分析 style.md 内容"""
    from backend.services import book_service
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")
    style_path = output_dir.parent / "style.md"
    if not style_path.exists():
        style_path = output_dir / "style.md"
    if not style_path.exists():
        raise HTTPException(status_code=404, detail="风格分析结果不存在")
    try:
        content = style_path.read_text(encoding="utf-8")
        return {"book_id": book_id, "content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")


# ==================== 日志下载 ====================

@router.get("/analysis/logs", dependencies=[Depends(require_session_token)])
async def download_logs():
    """下载 analyzer.log 日志文件（需 session token 鉴权）"""
    from backend.app import LOG_FILE
    if not LOG_FILE.exists():
        raise HTTPException(status_code=404, detail="日志文件不存在")
    return FileResponse(
        path=str(LOG_FILE),
        filename="analyzer.log",
        media_type="text/plain",
    )
