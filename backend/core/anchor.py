"""伏笔锚点：把"模型说它看到的那句话"钉回原文坐标。

为什么要单独一个模块：锚点校验必须发生在**落盘之前**（`analyzer` 手上有正文），
否则等复检时才发现锚点是编的，整本书的钱已经花掉了。

三条规矩：
1. 只认**逐字**命中；规范化只容忍空白差异（模型偶尔把换行删了）。
2. 定位不到就置空并计数，**不抛错**——一条伏笔缺锚点不该让整章失败。
3. 锚点所属章节由**块内标题反查**得出；块内没有标题时，退回"整块按首章算"并标记
   `chapter_attribution="block"`，供后续判断可信度。
"""

from __future__ import annotations

import re
from typing import Iterable

#: 块内章节标题：第一/十二/一百零三章（回）。只用来切边界，不校验标题格式。
CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章回节]|Chapter\s+\d+)",
    re.MULTILINE,
)

#: 锚点最短长度。太短的句子在长段里必然命中，等于把闸门交给巧合。
MIN_ANCHOR_CHARS = 6

#: 允许的匹配级别（本模块只产 EXACT / NORMALIZED 两级）
KIND_EXACT = "EXACT"
KIND_NORMALIZED = "NORMALIZED"


def build_chapter_spans(block_text: str, first_chapter: int) -> list[tuple[int, int, int]]:
    """返回块内每章的 ``(chapter_number, start, end)``。

    块由"按故事序拼接的若干章"组成，标题在正文里；这里用标题位置切边界。
    找不到标题时返回单段（整块算一章，章号取 ``first_chapter``）。
    """
    starts = [m.start() for m in CHAPTER_HEADING_RE.finditer(block_text)]
    if len(starts) < 2:
        return [(first_chapter, 0, len(block_text))]
    spans: list[tuple[int, int, int]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(block_text)
        spans.append((first_chapter + idx, start, end))
    return spans


def _squeeze_index(text: str) -> tuple[str, list[int]]:
    """去掉所有空白，返回 (压缩串, 压缩串下标 → 原文下标)。"""
    kept: list[str] = []
    mapping: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        kept.append(ch)
        mapping.append(i)
    return "".join(kept), mapping


def locate_anchor(block_text: str, quote: str) -> tuple[int, int, str] | None:
    """在块文本里定位引文，返回 ``(start, end, kind)``；定位不到返回 ``None``。

    先逐字找（最快也最可信），再退到"忽略空白"的规范化比较。
    """
    q = (quote or "").strip()
    if len(q) < MIN_ANCHOR_CHARS or not block_text:
        return None

    pos = block_text.find(q)
    if pos >= 0:
        return pos, pos + len(q), KIND_EXACT

    squeezed_text, mapping = _squeeze_index(block_text)
    squeezed_quote = "".join(ch for ch in q if not ch.isspace())
    hit = squeezed_text.find(squeezed_quote) if squeezed_quote else -1
    if hit < 0:
        return None
    start = mapping[hit]
    end = mapping[hit + len(squeezed_quote) - 1] + 1
    # 回验：规范化后的切片必须等于规范化后的引文
    if "".join(ch for ch in block_text[start:end] if not ch.isspace()) != squeezed_quote:
        return None
    return start, end, KIND_NORMALIZED


def _chapter_of(spans: Iterable[tuple[int, int, int]], pos: int) -> tuple[int, str]:
    for chapter, start, end in spans:
        if start <= pos < end:
            return chapter, "heading"
    return 0, "unknown"


def verify_result_anchors(result, block_text: str, first_chapter: int) -> dict:
    """就地校验一条 ``AnalysisResult`` 的伏笔锚点，返回统计。

    - 命中：把 ``anchor_quote`` 规范化为**原文切片**（不信模型的大小写/空白），
      并写入 ``anchor_chapter``
    - 未命中：清空 ``anchor_quote``，计 ``missing``
    - 跨章边界的引文：判未命中（说明模型在拼接）

    返回 ``{"total", "located", "missing", "by_kind"}``。
    """
    items = list(getattr(result, "foreshadowing", []) or [])
    stats = {"total": len(items), "located": 0, "missing": 0, "by_kind": {}}
    if not items:
        return stats

    spans = build_chapter_spans(block_text, first_chapter)
    for item in items:
        quote = (getattr(item, "anchor_quote", "") or "").strip()
        if not quote:
            stats["missing"] += 1
            continue
        hit = locate_anchor(block_text, quote)
        if hit is None:
            item.anchor_quote = ""
            item.anchor_chapter = 0
            stats["missing"] += 1
            continue
        start, end, kind = hit
        # 跨章边界 = 拼接，不接受（引文必须在同一章内）
        if any(start < edge < end for _c, edge, _e in spans[1:]):
            item.anchor_quote = ""
            item.anchor_chapter = 0
            stats["missing"] += 1
            continue
        chapter, _how = _chapter_of(spans, start)
        item.anchor_quote = block_text[start:end]
        item.anchor_chapter = int(chapter or first_chapter)
        stats["located"] += 1
        stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + 1
    return stats
