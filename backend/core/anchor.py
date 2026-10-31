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


#: 章号解析用的中文数字表
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _cn_to_int(text: str) -> int | None:
    """把"一百零三"这类中文数字转成整数；解析不了返回 None。

    只处理章节号会出现的形态（万以内 + 万位），别当通用数字库用。
    """
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                if number == 0:
                    number = 1  # "十三"里的十
                section += number * unit
            number = 0
        else:
            return None
    return total + section + number


def chapter_number_from_heading(heading: str) -> int | None:
    """从标题文本里取章号。

    为什么不用"块内第几个标题"推算：块和章不是一回事。有些源文件的"块"本身
    包含两章（例如"第13部分 至 第14部分"），按序号推算会把第二段的章号算成
    块序号+1，于是所有落在第二段的锚点都记错章——实测 60 条里错了 39 条。
    标题里写着真章号，就该用真的。
    """
    m = re.search(r"第\s*([0-9零〇一二三四五六七八九十百千万两]+)\s*[章回节]", heading)
    if m:
        return _cn_to_int(m.group(1))
    m = re.search(r"Chapter\s+(\d+)", heading, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def build_chapter_spans(block_text: str, first_chapter: int) -> list[tuple[int, int, int]]:
    """返回块内每章的 ``(chapter_number, start, end)``。

    块由"按故事序拼接的若干章"组成，标题在正文里；这里用标题位置切边界。
    章号优先取**标题里写的**章号（`chapter_number_from_heading`），解析不出才退回
    ``first_chapter + 序号``。找不到标题时返回单段（整块算一章）。
    """
    matches = list(CHAPTER_HEADING_RE.finditer(block_text))
    if len(matches) < 2:
        return [(first_chapter, 0, len(block_text))]
    spans: list[tuple[int, int, int]] = []
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(block_text)
        parsed = chapter_number_from_heading(m.group(0))
        spans.append((parsed if parsed is not None else first_chapter + idx, m.start(), end))
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


def locate_anchor(block_text: str, quote: str, min_chars: int = MIN_ANCHOR_CHARS) -> tuple[int, int, str] | None:
    """在块文本里定位引文，返回 ``(start, end, kind)``；定位不到返回 ``None``。

    先逐字找（最快也最可信），再退到"忽略空白"的规范化比较。
    """
    q = (quote or "").strip()
    if len(q) < min_chars or not block_text:
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


def verify_result_anchors(
    result, block_text: str, first_chapter: int, min_chars: int = MIN_ANCHOR_CHARS
) -> dict:
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
        hit = locate_anchor(block_text, quote, min_chars)
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


def entity_key_for(motif_key: str, fallback_id: str) -> str:
    """稳定实体键：优先用母题词，否则退回 ``clue:<代表条目 id>``。

    这个键**一旦写进账本就不可变**。用指纹重算它会带来一个隐性后果：
    后续章节补进来一条更早的线索，指纹一变、键跟着变，历史就被拆成了两条。
    """
    mk = (motif_key or "").strip()
    return mk if mk else f"clue:{fallback_id}"


def mark_leads(foreshadowing) -> int:
    """把"没给出可关闭事项"的条目标成线索，返回被标数量。

    判据只有一条：`question` 与 `answer_criteria` **都为空**。
    刻意不做"疑问词/专名命中"这类词面筛选——那筛的是表达形式，
    而"能不能被后文回答"是语义资格，词面判不准（会把"称贤侄"这种
    确实埋了婚事的条目错杀，也会放过"未来将如何"这种根本不是悬念的推演）。
    """
    count = 0
    for fs in foreshadowing or []:
        if not (getattr(fs, "question", "") or "").strip() and not (
            getattr(fs, "answer_criteria", "") or ""
        ).strip():
            fs.lead = True
            count += 1
    return count


def split_block_into_chapters(block_text: str, first_chapter: int) -> dict[int, str]:
    """把一个块按标题切成 {真实章号: 章正文}。

    复检要按章号取原文，索引与锚点必须用**同一套章号**；这套章号来自标题，
    不是块序号——块里可能是第 13、14 两章，按序号算会把 14 记成 3。
    """
    return {
        chapter: block_text[start:end]
        for chapter, start, end in build_chapter_spans(block_text, first_chapter)
    }
