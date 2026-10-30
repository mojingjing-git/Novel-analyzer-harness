"""引用校验：模型说"这句出自第 N 章第 a–b 字"，程序核对它是否真的出自那里。

设计取两条：

1. **Prompt 里写清引用格式**（:data:`CITATION_INSTRUCTION`）。让模型给可逐字比对的引文，
   比事后用模糊匹配猜它想说什么有效得多。指令要具体到"不改标点、不加省略、不确定就留空"。
2. **比对放宽，但要分级**。松比对的目的不是"尽量判过"，而是**区分过错的类型**：
   位置偏了、标点改了、中间省略了、还是根本没这句。四种情况对应不同的处理，
   全都判"过"等于放弃校验，全都判"不过"会把真引用误杀。

只暴露两个状态给下游（``EVIDENCE_LOCATED`` / ``UNVERIFIED``），额外的粒度放在
:class:`CitationCheck` 里供审计与统计，不污染事实本身的语义。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping, Sequence

from .knowledge import FactRecord, SourceRef

#: 写进 Prompt 的引用规则。要点：逐字照抄、保留标点、不省略、偏移用码点、拿不准就留空。
CITATION_INSTRUCTION = """引用规则（必须遵守）：
1. 每条结论若来自原文，给出 quote 对象：{"start": 起始码点, "end": 结束码点, "text": "原文"}。
2. text 必须是原文的**连续片段**，逐字照抄：不改标点、不改全半角、不加省略号、不拼接。
3. start/end 为该片段在本章正文中的码点位置（左闭右开），不是字数、不是行号。
4. 若结论是概括或推断、找不到可照抄的连续片段，则 quote 留空（null），不要自己造句充数。
5. 一次引用只对应一处连续片段；跨段的证据请分开给多条。"""

#: 句内省略标记。模型常用它们"接"两段原文，这不算逐字照抄，但可以按段核对。
ELLIPSIS_MARKERS = ("……", "…", "...", "⋯⋯", "⋯", "。。。")

#: 模糊匹配阈值。只在"分段也匹配不上"时启用，且要求引文足够长，否则短句极易撞车。
FUZZY_RATIO = 0.90
FUZZY_MIN_CHARS = 10

_MATCH_KIND = {
    "exact": "EXACT",
    "normalized": "NORMALIZED",
    "relocated": "RELOCATED",
    "segmented": "SEGMENTED",
    "fuzzy": "FUZZY",
    "mismatch": "MISMATCH",
}


@dataclass(frozen=True)
class CitationCheck:
    """一次引用的核对结果。``located`` 是唯一给下游看的那一位。"""

    located: bool
    match_kind: str        # EXACT / NORMALIZED / RELOCATED / SEGMENTED / FUZZY / MISMATCH
    reason: str = ""       # 失败原因：OUT_OF_RANGE / CHAPTER_MISSING / TEXT_MISMATCH
    similarity: float = 0.0
    corrected_start: int | None = None
    corrected_end: int | None = None

    @property
    def verification_state(self) -> str:
        return "EVIDENCE_LOCATED" if self.located else "UNVERIFIED"


# ---------------------------------------------------------------------------
# 规范化（仅用于比对，绝不用于偏移）
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """比对用的归一：全半角统一、去空白、去掉标点。

    刻意**只**用于比对：这些步骤会改变字符数，一旦拿去做偏移，引用就错位了。
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", "")
    text = re.sub(r"\s+", "", text)
    # 去掉所有标点与符号（保留文字、数字、下划线）
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith(("P", "Z")))


def _split_segments(quote: str) -> list[str]:
    """按省略标记把引文切成若干段（空段丢弃）。"""
    pattern = "|".join(re.escape(marker) for marker in ELLIPSIS_MARKERS)
    parts = re.split(pattern, quote)
    return [p for p in (_normalize(part) for part in parts) if p]


def _find_ordered(haystack: str, needles: Sequence[str]) -> int | None:
    """按顺序在 haystack 里定位各段，返回首段起点；任一段找不到返回 None。"""
    cursor = 0
    first = None
    for needle in needles:
        found = haystack.find(needle, cursor)
        if found < 0:
            return None
        if first is None:
            first = found
        cursor = found + len(needle)
    return first


# ---------------------------------------------------------------------------
# 主校验入口
# ---------------------------------------------------------------------------


def verify_citation(
    *,
    quote_text: str,
    chapter_text: str,
    start: int | None = None,
    end: int | None = None,
) -> CitationCheck:
    """核对一条引用。

    判定顺序（先严后松，任一成立即 ``located``）：

    1. 区间合法且与正文**逐字**相同 → ``EXACT``
    2. 去掉标点/空白/全半角差异后相同 → ``NORMALIZED``（模型改了标点是常态）
    3. 区间越界或对不上，但整章里能找到这段连续文字 → ``RELOCATED``（偏移算错了）
    4. 引文含省略标记时，各段在正文中**按顺序**都能找到 → ``SEGMENTED``
    5. 引文足够长且相似度 ≥ ``FUZZY_RATIO`` → ``FUZZY``
    6. 否则 → ``MISMATCH``
    """
    if not quote_text or not quote_text.strip():
        return CitationCheck(False, _MATCH_KIND["mismatch"], reason="EMPTY_QUOTE")

    length = len(chapter_text)
    in_range = (
        isinstance(start, int)
        and isinstance(end, int)
        and 0 <= start < end <= length
    )

    # 1. 逐字
    if in_range and chapter_text[start:end] == quote_text:
        return CitationCheck(True, _MATCH_KIND["exact"], corrected_start=start, corrected_end=end)

    # 2. 规范化后相等（仍要求区间本身合法）
    if in_range:
        window = _normalize(chapter_text[start:end])
        if window and window == _normalize(quote_text):
            return CitationCheck(
                True, _MATCH_KIND["normalized"], corrected_start=start, corrected_end=end
            )

    normalized_chapter = _normalize(chapter_text)
    normalized_quote = _normalize(quote_text)

    # 3. 位置错了但引文是真的：整章里搜得到
    if normalized_quote and normalized_quote in normalized_chapter:
        pos = normalized_chapter.find(normalized_quote)
        return CitationCheck(
            True,
            _MATCH_KIND["relocated"],
            corrected_start=_to_raw_offset(chapter_text, pos),
            corrected_end=_to_raw_offset(chapter_text, pos + len(normalized_quote)),
        )

    # 4. 分段（模型用省略号把两段接起来）
    segments = _split_segments(quote_text)
    if len(segments) > 1:
        pos = _find_ordered(normalized_chapter, segments)
        if pos is not None:
            return CitationCheck(
                True,
                _MATCH_KIND["segmented"],
                corrected_start=_to_raw_offset(chapter_text, pos),
            )

    # 5. 模糊：引文要够长（短句在长文里几乎必然"像"某处）
    if len(normalized_quote) >= FUZZY_MIN_CHARS:
        matcher = difflib.SequenceMatcher(None, normalized_quote, normalized_chapter)
        block = matcher.find_longest_match(0, len(normalized_quote), 0, len(normalized_chapter))
        coverage = block.size / len(normalized_quote) if normalized_quote else 0.0
        if coverage >= FUZZY_RATIO:
            return CitationCheck(
                True,
                _MATCH_KIND["fuzzy"],
                similarity=round(coverage, 4),
                corrected_start=_to_raw_offset(chapter_text, block.b),
                corrected_end=_to_raw_offset(chapter_text, block.b + block.size),
            )
        return CitationCheck(
            False,
            _MATCH_KIND["mismatch"],
            reason="OUT_OF_RANGE" if not in_range else "TEXT_MISMATCH",
            similarity=round(coverage, 4),
        )

    return CitationCheck(
        False,
        _MATCH_KIND["mismatch"],
        reason="OUT_OF_RANGE" if not in_range else "TEXT_MISMATCH",
    )


def _to_raw_offset(chapter_text: str, normalized_offset: int) -> int:
    """把"去标点去空白后的位置"映射回原文位置。

    映射不精准（一个规范化字符可能对应多个原文字符），所以只用于**修正提示**，
    不作为权威偏移写回事实——写回偏移必须走 ``SOURCE_REF_UNRESOLVED`` 的严格路径。
    """
    if normalized_offset <= 0:
        return 0
    seen = 0
    for index, ch in enumerate(chapter_text):
        if unicodedata.category(ch).startswith(("P", "Z")):
            continue
        if seen >= normalized_offset:
            return index
        seen += 1
    return len(chapter_text)


# ---------------------------------------------------------------------------
# 与 FactRecord 的衔接
# ---------------------------------------------------------------------------


def verify_fact(
    fact: FactRecord, *, chapter_text: str | None
) -> tuple[FactRecord, CitationCheck]:
    """核对一条事实并给出**升级后**的记录（失败不降级已有的已定位状态）。"""
    if fact.source is None:
        return fact, CitationCheck(False, _MATCH_KIND["mismatch"], reason="CHAPTER_MISSING")
    if chapter_text is None:
        return fact, CitationCheck(False, _MATCH_KIND["mismatch"], reason="CHAPTER_MISSING")

    check = verify_citation(
        quote_text=fact.text,
        chapter_text=chapter_text,
        start=fact.source.start,
        end=fact.source.end,
    )
    if not check.located:
        return fact, check
    if fact.verification_state == "EVIDENCE_LOCATED":
        return fact, check

    updated = FactRecord(
        fact_id=fact.fact_id,
        text=fact.text,
        source=fact.source,
        verification_state="EVIDENCE_LOCATED",
    )
    return updated, check


def summarize_checks(checks: Sequence[CitationCheck]) -> dict:
    """统计核对结果分布——这个分布就是"模型的编造率"，决定能不能换小模型。"""
    total = len(checks)
    located = sum(1 for c in checks if c.located)
    by_kind: dict[str, int] = {}
    for check in checks:
        by_kind[check.match_kind] = by_kind.get(check.match_kind, 0) + 1
    return {
        "total": total,
        "located": located,
        "located_ratio": round(located / total, 4) if total else 0.0,
        "by_kind": by_kind,
    }


def verify_facts_against_book(
    facts: Sequence[FactRecord], chapter_texts: Mapping[str, str]
) -> list[tuple[FactRecord, CitationCheck]]:
    """对一批事实逐条核对，返回（升级后的记录，核对结果）。

    ``chapter_texts`` 由调用方一次性读好传入（按 ``chapter_id`` 索引）：这里刻意
    不做 IO——否则"校验"会顺手触发磁盘读，批量场景变成 N 次文件读取，边界也很难在
    测试里构造。缺章文本按 ``CHAPTER_MISSING`` 处理，不猜。
    """
    results: list[tuple[FactRecord, CitationCheck]] = []
    for fact in facts:
        text = None
        if fact.source is not None:
            text = chapter_texts.get(fact.source.chapter_id)
        results.append(verify_fact(fact, chapter_text=text))
    return results
