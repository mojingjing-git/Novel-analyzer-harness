"""引用校验（转发层）。

实现已收敛到 `backend/core.citations`——两套定位语义会漂移，
而漂移的代价是"同一句引文在一个地方算对、在另一个地方算错"。
这里只保留依赖 vnext 数据类型（`FactRecord`/`SourceRef`）的两层包装。
"""

from __future__ import annotations

from typing import Mapping, Sequence

from backend.core.citations import (  # noqa: F401 - 公开 API 原样转发
    CITATION_INSTRUCTION,
    ELLIPSIS_MARKERS,
    FUZZY_MIN_CHARS,
    FUZZY_RATIO,
    CitationCheck,
    _MATCH_KIND,
    _find_ordered,
    _normalize,
    _split_segments,
    _to_raw_offset,
    summarize_checks,
    verify_citation,
)

from .knowledge import FactRecord, SourceRef


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
