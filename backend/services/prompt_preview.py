"""Prompt 预览服务：构建指定书指定块的 LLM prompt 并返回内容与统计。

业务编排从 routes_prompt 下沉而来（路由层只做 HTTP 形态转换），行为与原实现一致：
- 章节号校验（LookupError / ValueError 由路由层翻译为 404 / 422）
- 块映射与 pipeline 一致：断号目录不能连号 range，否则中间缺失章节被静默截断
- KB 构建失败降级为空 KB（预览是只读诊断功能，不阻塞）
"""

import logging

from backend.core.file_processor import FileProcessor
from backend.core.knowledge_base import KnowledgeBaseManager
from backend.core.prompt_builder import PromptBuilder
from backend.models.knowledge import KnowledgeBase
from backend.services.book_service import get_book_path, get_output_dir
from backend.config.settings import ConfigManager
from backend.services.queue_manager import CONFIG_FILE

logger = logging.getLogger(__name__)


def build_prompt_preview(book_id: str, chapter: int, max_chars: int = 0) -> dict:
    """构建指定书目的 prompt 预览。

    raises LookupError: 书目或章节目录不存在（路由层转 404）
    raises ValueError: 章节号非法（路由层转 422）
    """
    book_path = get_book_path(book_id)
    if not book_path:
        raise LookupError(f"书目不存在: {book_id}")

    blocks_path = book_path / "blocks"
    if not blocks_path.exists():
        raise LookupError(f"章节目录不存在: {blocks_path}")

    output_dir = get_output_dir(book_id)
    if not output_dir:
        output_dir = book_path / "output"

    # 必须 load() 才会读取 config.json 的真实配置（模型/Key/base_url），
    # 否则 ConfigManager().config 永远是默认配置
    config = ConfigManager(CONFIG_FILE).load()

    # 读章节内容
    chapter_content = "[正文内容] (占位符 — 未找到章节文件)"
    fp = FileProcessor(blocks_path, config.analysis.encoding_priority)
    chapters = fp.scan_chapters()
    real_name = ""
    chapter_total_chars = 0  # 必须在 if 块外初始化，否则无章节文件时下方引用会 UnboundLocalError
    # 必须在 if 块外初始化，否则 blocks 为空时下方 build_temp_knowledge 引用 target_ch 会 UnboundLocalError
    target_ch = chapter
    ch_idx = 0  # 无章节文件时 KB 用空上限（=不加载任何章）
    if chapters:
        bs = config.analysis.block_size
        if target_ch < 1:
            raise ValueError(f"章节号必须为正整数（收到 {target_ch}）")
        if target_ch > len(chapters):
            raise ValueError(f"章节号超出范围 (共 {len(chapters)} 块)")
        ch_idx = chapters[target_ch - 1]
        real_name = f"块 #{ch_idx}"
        # 取本块真实章号列表（与 pipeline 块映射一致：断号目录不能连号 range，
        # 否则中间缺失章节被静默截断，预览内容残缺）
        block_chs = chapters[target_ch - 1: target_ch - 1 + bs]
        content = fp.read_block(block_chs)
        if content:
            # 默认返回全文，让用户看到 LLM 实际收到的内容；
            # 若 max_chars > 0 则仅返回前 N 字符（调试大文本时可选）
            chapter_content = content if max_chars <= 0 else content[:max_chars]
            chapter_total_chars = len(content)
        else:
            chapter_content = "[正文内容] (文件读取为空)"
            chapter_total_chars = 0

    # 构建 KB：chapter_limit 用"本块之前的真实章号"（不能用块序号，断号/块化>1 时 KB 范围错位）
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
        "book_id": book_id,
        "chapter": chapter,
        "chapter_label": real_name,
        "chapter_total_chars": chapter_total_chars,
        "chapter_truncated": max_chars > 0 and chapter_total_chars > max_chars,
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
