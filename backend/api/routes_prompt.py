"""
Prompt 预览路由
POST /api/prompt/preview — 构建指定章节的 LLM prompt 并返回内容与统计
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.file_processor import FileProcessor
from backend.core.knowledge_base import KnowledgeBaseManager
from backend.core.prompt_builder import PromptBuilder
from backend.models.knowledge import KnowledgeBase
from backend.services.book_service import get_book_path, get_output_dir
from backend.config.settings import ConfigManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/prompt", tags=["prompt"])


class PromptPreviewRequest(BaseModel):
    book_id: str
    chapter: int = 1  # 第几块（1-based）
    max_chars: int = 0  # 章节原文最大返回字符数（0=不限制，返回全文与 LLM 看到的一致）


@router.post("/preview")
async def prompt_preview(req: PromptPreviewRequest):
    """构建指定书目的 prompt 并返回"""
    # 查找书目路径
    book_path = get_book_path(req.book_id)
    if not book_path:
        raise HTTPException(status_code=404, detail=f"书目不存在: {req.book_id}")

    blocks_path = book_path / "blocks"
    if not blocks_path.exists():
        raise HTTPException(status_code=404, detail=f"章节目录不存在: {blocks_path}")

    output_dir = get_output_dir(req.book_id)
    if not output_dir:
        output_dir = book_path / "output"

    # 必须 load() 才会读取 config.json 的真实配置（模型/Key/base_url），
    # 否则 ConfigManager().config 永远是默认配置
    config = ConfigManager().load()

    # 读章节内容
    chapter_content = "[正文内容] (占位符 — 未找到章节文件)"
    fp = FileProcessor(blocks_path, config.analysis.encoding_priority)
    chapters = fp.scan_chapters()
    real_name = ""
    chapter_total_chars = 0  # 必须在 if 块外初始化，否则无章节文件时下方引用会 UnboundLocalError
    # 必须在 if 块外初始化，否则 blocks 为空时下方 build_temp_knowledge 引用 target_ch 会 UnboundLocalError
    target_ch = req.chapter
    ch_idx = 0  # 无章节文件时 KB 用空上限（=不加载任何章）
    if chapters:
        bs = config.analysis.block_size
        if target_ch < 1:
            raise HTTPException(status_code=422, detail=f"章节号必须为正整数（收到 {target_ch}）")
        if target_ch > len(chapters):
            raise HTTPException(status_code=422, detail=f"章节号超出范围 (共 {len(chapters)} 块)")
        ch_idx = chapters[target_ch - 1]
        real_name = f"块 #{ch_idx}"
        # 取本块真实章号列表（与 pipeline 块映射一致：断号目录不能连号 range，
        # 否则中间缺失章节被静默截断，预览内容残缺）
        block_chs = chapters[target_ch - 1: target_ch - 1 + bs]
        content = fp.read_block(block_chs)
        if content:
            # 默认返回全文，让用户看到 LLM 实际收到的内容；
            # 若 max_chars > 0 则仅返回前 N 字符（调试大文本时可选）
            chapter_content = content if req.max_chars <= 0 else content[:req.max_chars]
            chapter_total_chars = len(content)
        else:
            chapter_content = "[正文内容] (文件读取为空)"
            chapter_total_chars = 0

    # 构建 KB：chapter_limit 用"本块之前的真实章号"（旧实现误用块序号，断号/块化>1 时 KB 范围错位）
    kb = KnowledgeBase()
    try:
        kb = KnowledgeBaseManager.build_temp_knowledge(output_dir, chapter_limit=max(0, ch_idx - 1))
    except Exception as e:
        logger.warning("KB 构建失败（使用空KB继续）: %s", e)

    # 构建 prompt
    pb = PromptBuilder(
        max_arc_length=config.analysis.max_arc_length,
        max_arcs=config.analysis.max_arcs_in_prompt,
        max_summaries=config.analysis.max_summaries_in_prompt,
        timeline_truncate=config.analysis.timeline_truncate,
    )
    messages = pb.build_messages(chapter_content, 0, kb)

    system_content = messages[0]["content"]
    user_content = messages[1]["content"]
    system_len = len(system_content)
    user_len = len(user_content)

    return {
        "book_id": req.book_id,
        "chapter": req.chapter,
        "chapter_label": real_name,
        "chapter_total_chars": chapter_total_chars,
        "chapter_truncated": req.max_chars > 0 and chapter_total_chars > req.max_chars,
        "system_prompt": system_content,
        "user_prompt": user_content,
        "system_len": system_len,
        "user_len": user_len,
        "total_len": system_len + user_len,
        "params": {
            "json_mode": config.api.json_mode,
            "max_arcs": config.analysis.max_arcs_in_prompt,
            "max_summaries": config.analysis.max_summaries_in_prompt,
            "timeline_truncate": config.analysis.timeline_truncate,
            "max_arc_length": config.analysis.max_arc_length,
            "block_size": config.analysis.block_size,
            "kb_arcs": len(kb.compressed_arcs),
            "kb_summaries": len(kb.recent_summaries),
        },
    }
