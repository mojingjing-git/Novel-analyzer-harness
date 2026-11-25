"""Prompt 预览服务：构建指定书指定块的 LLM prompt 并返回内容与统计。

业务编排从 routes_prompt 下沉而来（路由层只做 HTTP 形态转换），行为与原实现一致：
- 章节号校验（LookupError / ValueError 由路由层翻译为 404 / 422）
- 块映射与 pipeline 一致：断号目录不能连号 range，否则中间缺失章节被静默截断
- KB 构建失败降级为空 KB（预览是只读诊断功能，不阻塞）
"""

import logging

from backend.core.batch_packer import is_thinking_enabled, pack_chapter_batches, resolve_thinking_effort
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

    # 读章节内容与按当前预算动态装包
    chapter_content = "[正文内容] (占位符 — 未找到章节文件)"
    fp = FileProcessor(blocks_path, config.analysis.encoding_priority)
    chapters = fp.scan_chapters()
    real_name = ""
    chapter_total_chars = 0
    target_ch = chapter
    ch_idx = 0
    batch_chs: list[int] = []
    preview_items: list[tuple[int, str]] = [(0, chapter_content)]
    if chapters:
        bs = max(1, int(config.analysis.block_size or 1))
        if target_ch < 1:
            raise ValueError(f"章节号必须为正整数（收到 {target_ch}）")
        if target_ch > len(chapters):
            raise ValueError(f"章节号超出范围 (共 {len(chapters)} 章)")
        ch_idx = chapters[target_ch - 1]
        cand_chapters = chapters[target_ch - 1 : target_ch - 1 + bs]
        cand_texts: dict[int, str] = {}
        cand_lengths: dict[int, int] = {}
        for cid in cand_chapters:
            txt = fp.read_chapter(cid)
            if txt is not None:
                cand_texts[cid] = txt
                cand_lengths[cid] = len(txt)
            else:
                cand_lengths[cid] = 0
        packed = pack_chapter_batches(
            cand_chapters,
            cand_lengths,
            block_size=bs,
            max_content_chars=max(1, int(config.analysis.max_content_chars or 12000)),
            max_tokens=config.api.max_tokens,
            thinking_enabled=is_thinking_enabled(config.api),
            reasoning_effort=resolve_thinking_effort(config.api),
            break_on_gap=True,
        )
        batch_chs = packed[0] if packed else [ch_idx]
        real_name = (
            f"第 {batch_chs[0]} 章"
            if len(batch_chs) == 1
            else f"第 {batch_chs[0]}-{batch_chs[-1]} 章（共 {len(batch_chs)} 章）"
        )
        chapter_items = [(cid, cand_texts[cid]) for cid in batch_chs if cid in cand_texts]
        chapter_total_chars = sum(len(text) for _, text in chapter_items)
        if chapter_items:
            if max_chars > 0:
                remaining = max_chars
                preview_items = []
                for cid, text in chapter_items:
                    if remaining <= 0:
                        break
                    if len(text) <= remaining:
                        preview_items.append((cid, text))
                        remaining -= len(text)
                    else:
                        preview_items.append((cid, text[:remaining]))
                        remaining = 0
            else:
                preview_items = list(chapter_items)
        else:
            preview_items = [(ch_idx, "[正文内容] (文件读取为空)")]

    # 构建 KB：chapter_limit 用本批起始章之前的真实章号，且仅收录单章协议产物
    kb = KnowledgeBase()
    try:
        kb = KnowledgeBaseManager.build_temp_knowledge(
            output_dir,
            chapter_limit=max(0, ch_idx - 1),
            single_chapter_only=True,
        )
    except Exception as e:
        logger.warning("KB 构建失败（使用空KB继续）: %s", e)

    # 构建 prompt（与 NovelAnalyzer 保持全部上限一致）
    pb = PromptBuilder(
        max_arc_length=config.analysis.max_arc_length,
        max_arcs=config.analysis.max_arcs_in_prompt,
        max_summaries=config.analysis.max_summaries_in_prompt,
        timeline_truncate=config.analysis.timeline_truncate,
        max_character_states=config.analysis.max_character_states,
        max_world_items=config.analysis.max_world_items,
        max_foreshadow_entries=config.analysis.max_foreshadow_entries,
        max_relationships=config.analysis.max_relationships,
        max_verified_facts=config.analysis.max_verified_facts_in_prompt,
        max_themes=config.analysis.max_themes,
        max_content_chars=config.analysis.max_content_chars,
        rolling_max_momentum=config.analysis.rolling_max_momentum,
    )
    messages = pb.build_batch_messages(preview_items, kb)

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
            "batch_chapters": batch_chs,
            "kb_arcs": len(kb.compressed_arcs),
            "kb_summaries": len(kb.recent_summaries),
        },
    }
