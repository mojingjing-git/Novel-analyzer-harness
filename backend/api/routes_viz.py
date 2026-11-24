"""
可视化数据路由
GET /api/viz/timeline/{book_id} — 时间线数据
GET /api/viz/atlas/{book_id}    — 全书语义图谱（人物关系谱 + 换地图阶段流 + 多世界树）
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from backend.services import book_service
from backend.services import viz_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/viz", tags=["viz"])


def _get_output_dir(book_id: str):
    output_dir = book_service.get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")
    return output_dir


@router.get("/timeline/{book_id}")
async def get_timeline(book_id: str) -> dict:
    try:
        # 同步全量重解析章节 JSON 较重，放到线程池避免阻塞事件循环（大书会冻结 WS 进度）
        return {"book_id": book_id, **await asyncio.to_thread(viz_service.timeline_data, _get_output_dir(book_id))}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"时间线数据生成失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"时间线数据生成失败: {e}")


@router.get("/atlas/{book_id}")
async def get_atlas(book_id: str) -> dict:
    try:
        data = await asyncio.to_thread(viz_service.atlas_data, _get_output_dir(book_id))
        return {"book_id": book_id, **data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"全书语义图谱读取失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"全书语义图谱读取失败: {e}")
