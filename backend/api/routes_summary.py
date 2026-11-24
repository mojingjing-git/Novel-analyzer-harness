"""
最终总结路由
POST /api/summary/start — 启动指定书的最终总结
POST /api/summary/stop  — 停止当前总结任务
GET  /api/summary/status — 查询总结任务状态
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.summary_service import get_summary_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/summary", tags=["summary"])


class SummaryStartRequest(BaseModel):
    book_id: str
    start_chapter: int = 1
    end_chapter: int = 0
    batch_size: int = 0
    concurrency: int = 0


@router.get("/status")
async def summary_status() -> dict:
    return get_summary_service().status()


@router.post("/start")
async def start_summary(req: SummaryStartRequest) -> dict:
    service = get_summary_service()
    try:
        service.start(
            book_id=req.book_id,
            start_chapter=req.start_chapter,
            end_chapter=req.end_chapter if req.end_chapter > 0 else 999999,
            batch_size=req.batch_size if req.batch_size > 0 else None,
            concurrency=req.concurrency if req.concurrency > 0 else None,
        )
        return {"ok": True}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/stop")
async def stop_summary() -> dict:
    if not get_summary_service().stop():
        raise HTTPException(status_code=400, detail="没有正在运行的总结任务")
    return {"ok": True}
