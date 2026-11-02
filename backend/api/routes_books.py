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
from typing import Dict, Any

from fastapi import APIRouter, HTTPException

from pydantic import BaseModel

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
        added = book_service.register_scan_dir(base)
        return {"added": added}
    except Exception as e:
        logger.error(f"注册书目失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"注册书目失败: {e}")


@router.get("")
async def list_books() -> list:
    return book_service.list_books()


@router.get("/{book_id}/results")
async def get_book_results(book_id: str) -> dict:
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    aggregated_file = output_dir / "novel_analysis_aggregated.json"
    if aggregated_file.exists():
        try:
            data = json.loads(aggregated_file.read_text(encoding="utf-8"))
            return {"book_id": book_id, "data": data}
        except Exception as e:
            logger.error(f"读取聚合文件失败: {e}")
            raise HTTPException(status_code=500, detail="聚合文件读取失败")

    # 不存在则实时聚合（重同步操作，放线程池避免阻塞事件循环）
    from backend.utils.aggregate_utils import JSONAggregator
    try:
        aggregator = JSONAggregator(output_dir)
        path = await asyncio.to_thread(aggregator.aggregate_to_single_json)
        data = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        return {"book_id": book_id, "data": data}
    except Exception as e:
        logger.error(f"聚合失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"聚合失败: {e}")


@router.get("/{book_id}/latest_chapter")
async def get_latest_chapter(book_id: str) -> dict:
    """返回该书已分析完成的最大章节号（用于章节详情默认展示最新章节）"""
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")
    return {"book_id": book_id, "latest": book_service.latest_chapter(book_id)}


@router.get("/{book_id}/report")
async def get_book_report(book_id: str) -> dict:
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    report_path = output_dir / "final_summary_report.md"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="报告尚未生成")
    try:
        text = report_path.read_text(encoding="utf-8")
        return {"book_id": book_id, "report": text}
    except Exception as e:
        logger.error(f"读取报告失败: {e}")
        raise HTTPException(status_code=500, detail="报告读取失败")


@router.get("/{book_id}/ledger")
async def get_book_ledger(book_id: str) -> dict:
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    ledger_path = output_dir / "foreshadow_ledger.json"
    if not ledger_path.exists():
        raise HTTPException(status_code=404, detail="伏笔账本不存在")
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        return {"book_id": book_id, "ledger": data}
    except Exception as e:
        logger.error(f"读取账本失败: {e}")
        raise HTTPException(status_code=500, detail="账本读取失败")


@router.get("/{book_id}/foreshadow_ranking")
async def get_foreshadow_ranking(book_id: str) -> dict:
    """伏笔排行：按组合因子分数从大到小排列"""
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    ledger_path = output_dir / "foreshadow_ledger.json"
    if not ledger_path.exists():
        raise HTTPException(status_code=404, detail="伏笔账本不存在，请先运行最终总结")

    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        items = data.get("items", [])

        # 计算排行数据
        ranking = []
        for item in items:
            evidence_count = len(item.get("evidence_chapters", []))
            first_seen = item.get("first_seen_chapter", 0)
            last_seen = item.get("last_seen_chapter", 0)
            span = last_seen - first_seen
            score = item.get("composite_score", 0)

            # 旧数据（schema v2）没有 composite_score，实时计算
            if score == 0 and item.get("importance"):
                imp_order = {"高": 3, "中": 2, "低": 1}
                conf_val = item.get("confidence", 0.8)
                # float confidence → string
                if conf_val >= 0.8:
                    conf_str = "高"
                elif conf_val >= 0.5:
                    conf_str = "中"
                else:
                    conf_str = "低"
                score = (
                    imp_order.get(item.get("importance", "中"), 0) * 100
                    + imp_order.get(conf_str, 0) * 10
                    + min(evidence_count, 50)
                    + min(span, 49)
                )

            ranking.append({
                "id": item.get("id", ""),
                "description": item.get("description", ""),
                "composite_score": score,
                "importance": item.get("importance", "中"),
                "confidence": item.get("confidence", 0.8),
                "status": item.get("status", "active"),
                "first_seen_chapter": first_seen,
                "last_seen_chapter": last_seen,
                "span": span,
                "evidence_count": evidence_count,
                "evidence_chapters": item.get("evidence_chapters", []),
                "resolution": item.get("resolution"),
                "resolved_chapter": item.get("resolved_chapter"),
                "needs_review": bool(item.get("needs_review", False)),
                "resolution_quality": item.get("resolution_quality", ""),
            })

        # 按分数降序
        ranking.sort(key=lambda x: x["composite_score"], reverse=True)

        return {
            "book_id": book_id,
            "total": len(ranking),
            "ranking": ranking,
        }
    except Exception as e:
        logger.error(f"读取伏笔排行失败: {e}")
        raise HTTPException(status_code=500, detail="排行读取失败")


@router.get("/{book_id}/token_stats")
async def get_book_token_stats(book_id: str) -> dict:
    """读取该书落盘的 token 统计（分析 token_stats.json + 总结 summary_token_stats.json）"""
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")
    result: Dict[str, Any] = {"book_id": book_id}
    for key, name in (("analysis", "token_stats.json"), ("summary", "summary_token_stats.json")):
        path = output_dir / name
        if path.exists():
            try:
                result[key] = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"读取 {name} 失败: {e}")
    return result


@router.get("/{book_id}/characters")
async def get_book_characters(book_id: str) -> dict:
    """角色卡索引：返回角色列表与基本信息"""
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    from backend.utils.aggregate_utils import JSONAggregator
    from backend.utils.character_card_generator import CharacterCardGenerator
    try:
        aggregator = JSONAggregator(output_dir)
        await asyncio.to_thread(aggregator.aggregate_character_tracking, output_dir / "character_tracking_aggregated.json")
        generator = CharacterCardGenerator(output_dir)
        summaries = await asyncio.to_thread(generator.get_character_summaries)
        return {"book_id": book_id, "characters": summaries}
    except Exception as e:
        logger.error(f"生成角色卡索引失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"角色卡索引失败: {e}")


@router.get("/{book_id}/characters/{character_name}")
async def get_character_card(book_id: str, character_name: str) -> dict:
    """单个角色卡详情"""
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    from backend.utils.aggregate_utils import JSONAggregator
    from backend.utils.character_card_generator import CharacterCardGenerator
    try:
        aggregator = JSONAggregator(output_dir)
        await asyncio.to_thread(aggregator.aggregate_character_tracking, output_dir / "character_tracking_aggregated.json")
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
    data = book_service.load_chapter_result(book_id, chapter)
    if data is None:
        raise HTTPException(status_code=404, detail=f"章节结果不存在: {book_id} 第{chapter}章")
    return {"book_id": book_id, "chapter": chapter, "data": data}
