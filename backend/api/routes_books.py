"""
书目与结果查询路由
GET /api/books                — 列出所有书目
GET /api/books/{id}/results   — 按书聚合结果
GET /api/books/{id}/report    — 读取最终总结报告（Markdown）
GET /api/books/{id}/ledger     — 读取伏笔账本
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException

from pydantic import BaseModel

from backend.api.auth import require_session_token
from backend.common.file_lock import BookLockError
from backend.services import book_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/books", tags=["books"])


class RegisterRequest(BaseModel):
    base_dir: str


@router.post("/register")
async def register_books(req: RegisterRequest) -> dict:
    """手动注册 base_dir 下的书目录（不加入队列）"""
    try:
        base = Path(req.base_dir)
        added = await asyncio.to_thread(book_service.register_scan_dir, base)
        return {"added": added}
    except Exception as e:
        logger.error(f"注册书目失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"注册书目失败: {e}")


@router.get("")
async def list_books() -> list:
    return await asyncio.to_thread(book_service.list_books)


@router.get("/{book_id}/results")
async def get_book_results(book_id: str) -> dict:
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    from backend.utils.aggregate_utils import ensure_aggregated_cache
    try:
        agg_dir = await asyncio.to_thread(ensure_aggregated_cache, output_dir)
        path = agg_dir / "novel_analysis_aggregated.json"
        data = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        return {"book_id": book_id, "data": data}
    except Exception as e:
        logger.error(f"聚合失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"聚合失败: {e}")


@router.get("/{book_id}/style_profile")
async def get_book_style_profile(book_id: str) -> dict:
    """读取 Stage 3 落盘的 style_profile.json（书存在但无画像时返回 200 exists=False）"""
    res = await asyncio.to_thread(book_service.load_style_profile, book_id)
    if res is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")
    return res


@router.get("/{book_id}/recheck_verdicts")
async def get_book_recheck_verdicts(book_id: str) -> dict:
    """读取落盘的 recheck_verdicts.json（书存在但无复检结果时返回 200 exists=False）"""
    res = await asyncio.to_thread(book_service.load_recheck_verdicts, book_id)
    if res is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")
    return res


@router.get("/{book_id}/latest_chapter")
async def get_latest_chapter(book_id: str) -> dict:
    """返回该书已分析完成的最大章节号（用于章节详情默认展示最新章节）"""
    output_dir = await asyncio.to_thread(book_service.get_output_dir, book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")
    latest = await asyncio.to_thread(book_service.latest_chapter, book_id)
    return {"book_id": book_id, "latest": latest}


@router.get("/{book_id}/report")
async def get_book_report(book_id: str) -> dict:
    output_dir = await asyncio.to_thread(book_service.get_output_dir, book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    report_path = output_dir / "final_summary_report.md"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="报告尚未生成")
    try:
        text = await asyncio.to_thread(report_path.read_text, encoding="utf-8")
        return {"book_id": book_id, "report": text}
    except Exception as e:
        logger.error(f"读取报告失败: {e}")
        raise HTTPException(status_code=500, detail="报告读取失败")


@router.get("/{book_id}/ledger")
async def get_book_ledger(book_id: str) -> dict:
    output_dir = await asyncio.to_thread(book_service.get_output_dir, book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    ledger_path = output_dir / "foreshadow_ledger.json"
    if not ledger_path.exists():
        raise HTTPException(status_code=404, detail="伏笔账本不存在")
    try:
        data = json.loads(await asyncio.to_thread(ledger_path.read_text, encoding="utf-8"))
        return {"book_id": book_id, "ledger": data}
    except Exception as e:
        logger.error(f"读取账本失败: {e}")
        raise HTTPException(status_code=500, detail="账本读取失败")


@router.get("/{book_id}/foreshadow_ranking")
async def get_foreshadow_ranking(book_id: str) -> dict:
    """伏笔排行：按组合因子分数从大到小排列"""
    try:
        return await asyncio.to_thread(book_service.load_foreshadow_ranking, book_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="伏笔账本不存在，请先运行最终总结")
    except Exception as e:
        logger.error(f"读取伏笔排行失败: {e}")
        raise HTTPException(status_code=500, detail="排行读取失败")


class ForeshadowReviewRequest(BaseModel):
    action: Literal["confirm_resolved", "reject_to_active", "mark_dormant"]
    resolution: Optional[str] = None
    resolved_chapter: Optional[int] = None
    note: Optional[str] = None


class ForeshadowBatchReviewItem(BaseModel):
    item_id: str
    action: Literal["confirm_resolved", "reject_to_active", "mark_dormant"]
    resolution: Optional[str] = None
    resolved_chapter: Optional[int] = None
    note: Optional[str] = None


class ForeshadowBatchReviewRequest(BaseModel):
    items: List[ForeshadowBatchReviewItem]


def _translate_review_error(exc: Exception, book_id: str, item_id: str = "") -> HTTPException:
    if isinstance(exc, BookLockError):
        return HTTPException(status_code=409, detail="书目正在被其他进程操作，请稍后重试")
    if isinstance(exc, LookupError):
        msg = "伏笔账本不存在，请先运行最终总结" if str(exc) == "ledger_not_found" else f"书目不存在: {book_id}"
        return HTTPException(status_code=404, detail=msg)
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=f"伏笔条目不存在: {exc.args[0] if exc.args else item_id}")
    if isinstance(exc, RuntimeError):
        if str(exc) == "summary_running":
            return HTTPException(status_code=409, detail="最终总结运行中，禁止并发改写伏笔账本")
        logger.error(f"写入伏笔复核结果失败: {exc}")
        return HTTPException(status_code=500, detail="保存伏笔账本失败")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.error(f"伏笔复核覆写异常 ({book_id}/{item_id}): {exc}", exc_info=True)
    return HTTPException(status_code=500, detail="伏笔复核覆写失败")


@router.post("/{book_id}/ledger/batch_review", dependencies=[Depends(require_session_token)])
async def batch_review_book_ledger_items(book_id: str, req: ForeshadowBatchReviewRequest) -> dict:
    """原子批量人工复核覆写伏笔账本条目（受 session token 保护）。"""
    try:
        entries = [it.model_dump() for it in req.items]
        return await asyncio.to_thread(book_service.batch_review_foreshadow_items, book_id, entries)
    except Exception as e:
        raise _translate_review_error(e, book_id)


@router.post("/{book_id}/ledger/{item_id}/review", dependencies=[Depends(require_session_token)])
async def review_book_ledger_item(book_id: str, item_id: str, req: ForeshadowReviewRequest) -> dict:
    """人工复核覆写伏笔账本条目状态（受 session token 保护）。"""
    try:
        return await asyncio.to_thread(
            book_service.review_foreshadow_item,
            book_id,
            item_id,
            req.action,
            resolution=req.resolution,
            resolved_chapter=req.resolved_chapter,
            note=req.note,
        )
    except Exception as e:
        raise _translate_review_error(e, book_id, item_id)


@router.get("/{book_id}/token_stats")
async def get_book_token_stats(book_id: str) -> dict:
    """读取该书落盘的 token 统计（分析 token_stats.json + 总结 summary_token_stats.json）。

    categories 里带 model 的新数据会附带逐类费用（models.dev 挂牌价；
    无 model 的老数据不算费——历史 JSON 无模型名，按现牌价推定会失真）。
    """
    output_dir = await asyncio.to_thread(book_service.get_output_dir, book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")
    result: Dict[str, Any] = {"book_id": book_id}
    for key, name in (("analysis", "token_stats.json"), ("summary", "summary_token_stats.json")):
        path = output_dir / name
        if path.exists():
            try:
                data = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
                _attach_category_fees(data)
                result[key] = data
            except Exception as e:
                logger.warning(f"读取 {name} 失败: {e}")
    return result


def _attach_category_fees(data: dict) -> None:
    """就地给 categories 里有 model 的条目附费用；返回是否全部有价。"""
    from backend.core.model_catalog import calc_model_cost

    total = 0.0
    priced_any = False
    for entry in (data.get("categories") or {}).values():
        model = entry.get("model") if isinstance(entry, dict) else None
        if not model:
            entry["usd"] = None if isinstance(entry, dict) else entry
            continue
        fee = calc_model_cost(model, entry.get("input_tokens", 0),
                              entry.get("output_tokens", 0),
                              entry.get("cached_tokens", 0))
        entry["usd"] = round(fee["usd"], 4) if fee else None
        if fee:
            total += fee["usd"]
            priced_any = True
    data["total_usd"] = (round(total, 4) if priced_any
                         else (None if (data.get("categories") or {}) else 0.0))


@router.get("/{book_id}/characters")
async def get_book_characters(book_id: str) -> dict:
    """角色卡索引：按指纹透明更新聚合缓存并返回角色列表（含别名归并）"""
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    from backend.utils.aggregate_utils import ensure_aggregated_cache
    from backend.utils.character_card_generator import CharacterCardGenerator
    try:
        await asyncio.to_thread(ensure_aggregated_cache, output_dir)
        generator = CharacterCardGenerator(output_dir)
        summaries = await asyncio.to_thread(generator.get_character_summaries)
        return {"book_id": book_id, "characters": summaries}
    except Exception as e:
        logger.error(f"生成角色卡索引失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"角色卡索引失败: {e}")


@router.get("/{book_id}/characters/{character_name}")
async def get_character_card(book_id: str, character_name: str) -> dict:
    """单个角色卡详情（支持通过别名解析并直达主规范角色卡）"""
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    from backend.utils.aggregate_utils import ensure_aggregated_cache
    from backend.utils.character_card_generator import CharacterCardGenerator
    try:
        await asyncio.to_thread(ensure_aggregated_cache, output_dir)
        generator = CharacterCardGenerator(output_dir)
        card = await asyncio.to_thread(generator.get_card_data, character_name)
        return {"book_id": book_id, "character": card}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"生成角色卡失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"角色卡生成失败: {e}")


@router.get("/{book_id}/chapter/{chapter}")
async def get_chapter_result(book_id: str, chapter: int) -> dict:
    """单章分析结果 JSON"""
    data = await asyncio.to_thread(book_service.load_chapter_result, book_id, chapter)
    if data is None:
        raise HTTPException(status_code=404, detail=f"章节结果不存在: {book_id} 第{chapter}章")
    return {"book_id": book_id, "chapter": chapter, "data": data}
