"""
Prompt 预览路由
POST /api/prompt/preview — 构建指定章节的 LLM prompt 并返回内容与统计

业务编排在 backend/services/prompt_preview.py；本路由只做 HTTP 形态转换
（LookupError → 404，ValueError → 422）。
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.prompt_preview import build_prompt_preview

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/prompt", tags=["prompt"])


class PromptPreviewRequest(BaseModel):
    book_id: str
    chapter: int = 1  # 第几块（1-based）
    max_chars: int = 0  # 章节原文最大返回字符数（0=不限制，返回全文与 LLM 看到的一致）


@router.post("/preview")
async def prompt_preview(req: PromptPreviewRequest):
    """构建指定书目的 prompt 并返回"""
    try:
        return build_prompt_preview(req.book_id, req.chapter, req.max_chars)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
