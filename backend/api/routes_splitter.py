"""
切分工具路由
POST /api/splitter/preview     — 预览切分结果
POST /api/splitter/save        — 保存 blocks 目录（支持工作区模式）
POST /api/splitter/infer_name  — 从文件名推断书名
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services import splitter_service
from backend.services.queue_service import get_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/splitter", tags=["splitter"])


class SplitterPreviewRequest(BaseModel):
    file_path: str
    pattern: str = ""
    mode: str = "auto"          # auto | custom
    use_volume: bool = True
    min_words: int = 0
    merge_tiny: bool = False
    max_words: int = 0
    remove_ads: bool = False
    strip_whitespace: bool = True
    preview_count: int = 50


class SplitterSaveRequest(BaseModel):
    file_path: str
    output_dir: str = ""  # 可选，留空则走工作区模式
    book_name: str = ""   # 工作区模式下的书名
    pattern: str = ""
    mode: str = "auto"
    use_volume: bool = True
    min_words: int = 0
    merge_tiny: bool = False
    max_words: int = 0
    remove_ads: bool = False
    strip_whitespace: bool = True


class InferNameRequest(BaseModel):
    file_path: str


# 注意：文件选择已改为前端原生 <input type="file">（见 SplitterPage.vue），
# 不再提供后端 tkinter 路由——tkinter 在无 DISPLAY/Linux 服务端/PyInstaller 打包环境会崩，
# 且会阻塞 FastAPI 事件循环。保留下方业务路由即可。


@router.post("/infer_name")
async def infer_name(req: InferNameRequest) -> dict:
    """从文件名推断书名"""
    path = Path(req.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {req.file_path}")
    book_name = splitter_service.infer_book_name(path)
    return {"book_name": book_name}


@router.post("/preview")
async def preview_split(req: SplitterPreviewRequest) -> dict:
    path = Path(req.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {req.file_path}")
    try:
        options = splitter_service.SplitOptions(
            pattern=req.pattern,
            mode=req.mode,
            use_volume=req.use_volume,
            min_words=req.min_words,
            merge_tiny=req.merge_tiny,
            max_words=req.max_words,
            remove_ads=req.remove_ads,
            strip_whitespace=req.strip_whitespace,
        )
        return await asyncio.to_thread(
            splitter_service.preview_split,
            path,
            options=options,
            preview_count=req.preview_count,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"预览切分失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"预览切分失败: {e}")


@router.post("/save")
async def save_split(req: SplitterSaveRequest) -> dict:
    # P1（2026-08-24）：切分会先清理目标 blocks 目录的旧章文件；分析运行中对同一本书
    # 重切分会摧毁正在分析的输入（pipeline 惰性逐章读文件）。对照 routes_workspace 的护栏。
    if get_service().is_running:
        raise HTTPException(status_code=409, detail="分析正在进行中，请先停止后再切分保存")
    path = Path(req.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {req.file_path}")
    try:
        options = splitter_service.SplitOptions(
            pattern=req.pattern,
            mode=req.mode,
            use_volume=req.use_volume,
            min_words=req.min_words,
            merge_tiny=req.merge_tiny,
            max_words=req.max_words,
            remove_ads=req.remove_ads,
            strip_whitespace=req.strip_whitespace,
        )
        if req.output_dir:
            # 手动指定输出目录模式
            return await asyncio.to_thread(
                splitter_service.save_split,
                path,
                Path(req.output_dir),
                options=options,
            )
        else:
            # 工作区模式：自动保存到 workspace/{书名}/blocks/
            if not req.book_name:
                req.book_name = splitter_service.infer_book_name(path)
            service = get_service()
            workspace = service.workspace_path
            return await asyncio.to_thread(
                splitter_service.save_to_workspace,
                path,
                workspace,
                req.book_name,
                options=options,
            )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"保存切分失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"保存切分失败: {e}")


class SplitterBatchRequest(BaseModel):
    file_paths: list[str]
    book_names: list[str] = []
    pattern: str = ""
    mode: str = "auto"
    use_volume: bool = True
    min_words: int = 0
    merge_tiny: bool = False
    max_words: int = 0
    remove_ads: bool = False
    strip_whitespace: bool = True


@router.post("/batch")
async def batch_split(req: SplitterBatchRequest) -> dict:
    """批量切分多本小说到工作区（逐本复用 save_to_workspace）"""
    if not req.file_paths:
        raise HTTPException(status_code=422, detail="file_paths 不能为空")
    # P1（2026-08-24）：切分会先清理目标 blocks 目录的旧章文件；分析运行中对同一本书
    # 批量切分会摧毁正在分析的输入（pipeline 惰性逐章读文件）。对照 routes_workspace 的护栏。
    if get_service().is_running:
        raise HTTPException(status_code=409, detail="分析正在进行中，请先停止后再批量切分保存")
    options = splitter_service.SplitOptions(
        pattern=req.pattern,
        mode=req.mode,
        use_volume=req.use_volume,
        min_words=req.min_words,
        merge_tiny=req.merge_tiny,
        max_words=req.max_words,
        remove_ads=req.remove_ads,
        strip_whitespace=req.strip_whitespace,
    )
    service = get_service()
    workspace = service.workspace_path
    results = []
    success = 0
    fail = 0
    for i, fp in enumerate(req.file_paths):
        path = Path(fp)
        if not path.exists():
            results.append({"file": fp, "book": "", "total_chapters": 0, "ok": False, "error": "文件不存在"})
            fail += 1
            continue
        book_name = req.book_names[i].strip() if i < len(req.book_names) else ""
        if not book_name:
            book_name = splitter_service.infer_book_name(path)
        try:
            # 批量切分是 CPU/IO 重活：走线程池，避免阻塞事件循环
            # （WS 进度/健康检查/其它 API 全程冻结，N 本书 = N 倍阻塞）
            r = await asyncio.to_thread(
                splitter_service.save_to_workspace, path, workspace, book_name, options=options)
            results.append({
                "file": fp,
                "book": r.get("book_name", book_name),
                "total_chapters": r.get("total_chapters", 0),
                "ok": True,
                "error": "",
            })
            success += 1
        except Exception as e:
            logger.error(f"批量切分失败 {fp}: {e}", exc_info=True)
            results.append({"file": fp, "book": book_name, "total_chapters": 0, "ok": False, "error": str(e)})
            fail += 1
    return {"total": len(req.file_paths), "success": success, "fail": fail, "results": results}
