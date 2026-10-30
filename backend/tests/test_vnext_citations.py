"""引用校验：严松分级 + Prompt 引用规则（S3-6）。

守两件事：真引用不能被误杀（模型改标点是常态），编造引用不能被放过（那是污染源）。
用例按"模型实际会犯的错"组织，而不是按函数分支组织。
"""

from __future__ import annotations

import pytest

from backend.vnext.citations import (
    CITATION_INSTRUCTION,
    summarize_checks,
    verify_citation,
    verify_fact,
)
from backend.vnext.knowledge import FactRecord, SourceRef

CHAPTER = (
    "　　方醒从棺中坐起，四下寂静。\n"
    "　　「我这是……活了？」他低声自语。\n"
    "　　棺外的雨声渐密，像是有人在敲木头。\n"
)


def _fact(text: str, *, start: int = 1, end: int | None = None, state: str = "UNVERIFIED") -> FactRecord:
    return FactRecord(
        fact_id="f1",
        text=text,
        source=SourceRef("ch_1", 1, start, end if end is not None else start + len(text), "h"),
        verification_state=state,
    )


# ---------------------------------------------------------------------------
# Prompt 规则
# ---------------------------------------------------------------------------


def test_instruction_forbids_paraphrase_and_ellipsis() -> None:
    """指令里必须明确"不省略、不改标点"，否则模型给的就是拼接句。"""
    assert "逐字照抄" in CITATION_INSTRUCTION
    assert "省略号" in CITATION_INSTRUCTION
    assert "null" in CITATION_INSTRUCTION  # 允许"没有可照抄片段"时留空


# ---------------------------------------------------------------------------
# 1. 逐字
# ---------------------------------------------------------------------------


def test_exact_quote_is_located() -> None:
    quote = "方醒从棺中坐起"
    start = CHAPTER.index(quote)
    check = verify_citation(quote_text=quote, chapter_text=CHAPTER, start=start, end=start + len(quote))
    assert check.located is True
    assert check.match_kind == "EXACT"
    assert check.verification_state == "EVIDENCE_LOCATED"


# ---------------------------------------------------------------------------
# 2. 标点/全半角差异——最常见的"真引用被判假"
# ---------------------------------------------------------------------------


def test_punctuation_difference_still_located() -> None:
    """模型把全角引号改成半角、去掉空格，不该判失败。"""
    quote = '「我这是……活了?」他低声自语'   # 半角问号 + 半角引号
    start = CHAPTER.index("「我这是")
    check = verify_citation(quote_text=quote, chapter_text=CHAPTER, start=start, end=start + 20)
    assert check.located is True
    # 具体走"同窗口规范化"还是"整章重定位"取决于窗口边界；两者都算通过
    assert check.match_kind in {"NORMALIZED", "RELOCATED"}


def test_whitespace_difference_still_located() -> None:
    quote = "方醒 从棺中坐起，四下 寂静"
    start = CHAPTER.index("方醒")
    check = verify_citation(quote_text=quote, chapter_text=CHAPTER, start=start, end=start + 14)
    assert check.located is True


# ---------------------------------------------------------------------------
# 3. 偏移算错但引文真实
# ---------------------------------------------------------------------------


def test_wrong_offset_but_real_quote_is_relocated() -> None:
    """模型给的位置偏了，但整章确实有这句 → 算通过，并给出修正位置。"""
    quote = "棺外的雨声渐密"
    check = verify_citation(quote_text=quote, chapter_text=CHAPTER, start=0, end=3)
    assert check.located is True
    assert check.match_kind == "RELOCATED"
    assert check.corrected_start is not None and check.corrected_start > 0


def test_offset_beyond_text_is_still_relocated_if_quote_exists() -> None:
    quote = "他低声自语"
    check = verify_citation(quote_text=quote, chapter_text=CHAPTER, start=9999, end=10050)
    assert check.located is True
    assert check.match_kind == "RELOCATED"


# ---------------------------------------------------------------------------
# 4. 省略号拼接
# ---------------------------------------------------------------------------


def test_ellipsis_joined_segments_are_accepted_in_order() -> None:
    """模型用"……"把两段原文接起来：不算逐字，但各段都在，按顺序核对即通过。"""
    quote = "方醒从棺中坐起……棺外的雨声渐密"
    check = verify_citation(quote_text=quote, chapter_text=CHAPTER, start=1, end=40)
    assert check.located is True
    assert check.match_kind == "SEGMENTED"


def test_ellipsis_segments_out_of_order_is_rejected() -> None:
    """顺序颠倒就不是"原文的片段拼接"，必须判否。"""
    quote = "棺外的雨声渐密……方醒从棺中坐起"
    check = verify_citation(quote_text=quote, chapter_text=CHAPTER, start=1, end=40)
    assert check.located is False


# ---------------------------------------------------------------------------
# 5. 模糊
# ---------------------------------------------------------------------------


def test_fabricated_quote_is_rejected() -> None:
    """整章没有这句 → 必须判否。这是校验存在的理由。"""
    quote = "方醒推开门走进雨里，回头看了一眼那口棺材"
    check = verify_citation(quote_text=quote, chapter_text=CHAPTER, start=1, end=30)
    assert check.located is False
    assert check.match_kind == "MISMATCH"


def test_short_fabricated_fragment_is_rejected() -> None:
    """短句不能靠"像"蒙过：阈值只对足够长的引文开放。"""
    check = verify_citation(quote_text="他笑了", chapter_text=CHAPTER, start=1, end=5)
    assert check.located is False


def test_long_quote_with_one_typo_passes_as_fuzzy() -> None:
    """长句里错一个字，应算"基本逐字"；否则真引用会被大量误杀。"""
    quote = "方醒从棺中坐起，四下寂挣。"   # "静"→"挣"
    check = verify_citation(quote_text=quote, chapter_text=CHAPTER, start=1, end=20)
    assert check.located is True


# ---------------------------------------------------------------------------
# 6. 边界与失败原因
# ---------------------------------------------------------------------------


def test_empty_quote_is_rejected() -> None:
    check = verify_citation(quote_text="   ", chapter_text=CHAPTER, start=1, end=3)
    assert check.located is False


def test_missing_offset_falls_back_to_search() -> None:
    """模型没给偏移也要能用：整章里搜得到就算定位。"""
    check = verify_citation(quote_text="四下寂静", chapter_text=CHAPTER, start=None, end=None)
    assert check.located is True
    assert check.match_kind == "RELOCATED"


def test_out_of_range_reason_is_reported() -> None:
    check = verify_citation(quote_text="完全不存在的一句话", chapter_text=CHAPTER, start=1, end=9999)
    assert check.located is False
    assert check.reason == "OUT_OF_RANGE"


# ---------------------------------------------------------------------------
# 与 FactRecord 衔接
# ---------------------------------------------------------------------------


def test_verify_fact_upgrades_state_and_keeps_text() -> None:
    quote = "方醒从棺中坐起"
    start = CHAPTER.index(quote)
    fact = _fact(quote, start=start, end=start + len(quote))
    updated, check = verify_fact(fact, chapter_text=CHAPTER)
    assert check.located is True
    assert updated.verification_state == "EVIDENCE_LOCATED"
    assert updated.text == fact.text          # 不因为校验而改写结论文字
    assert updated.source == fact.source      # 偏移不被"修正"偷偷改掉


def test_verify_fact_failure_keeps_record_intact() -> None:
    fact = _fact("编造的一句话", start=1, end=10)
    updated, check = verify_fact(fact, chapter_text=CHAPTER)
    assert updated is fact
    assert check.located is False


def test_verify_fact_without_chapter_text_reports_missing() -> None:
    fact = _fact("随便一句", start=1, end=5)
    updated, check = verify_fact(fact, chapter_text=None)
    assert updated is fact
    assert check.reason == "CHAPTER_MISSING"


def test_already_located_fact_is_not_downgraded() -> None:
    fact = _fact("方醒从棺中坐起", start=1, end=9, state="EVIDENCE_LOCATED")
    updated, _ = verify_fact(fact, chapter_text=CHAPTER)
    assert updated.verification_state == "EVIDENCE_LOCATED"


# ---------------------------------------------------------------------------
# 统计：模型的编造率
# ---------------------------------------------------------------------------


def test_summary_reports_kind_distribution() -> None:
    checks = [
        verify_citation(quote_text="方醒从棺中坐起", chapter_text=CHAPTER, start=1, end=9),
        verify_citation(quote_text="编造的句子啊啊啊啊", chapter_text=CHAPTER, start=1, end=9),
    ]
    summary = summarize_checks(checks)
    assert summary["total"] == 2
    assert summary["located"] >= 1
    assert summary["located_ratio"] <= 1.0
    assert "MISMATCH" in summary["by_kind"]

# ---------------------------------------------------------------------------
# 批量核对（结果落盘前的接线入口）
# ---------------------------------------------------------------------------


def test_batch_verify_upgrades_only_located_ones() -> None:
    from backend.vnext.citations import verify_facts_against_book

    quote = "方醒从棺中坐起"
    start = CHAPTER.index(quote)
    good = _fact(quote, start=start, end=start + len(quote))
    bad = _fact("编造的一句完全不存在的话", start=1, end=20)

    results = verify_facts_against_book([good, bad], {"ch_1": CHAPTER})
    assert [r[0].verification_state for r in results] == [
        "EVIDENCE_LOCATED",
        "UNVERIFIED",
    ]
    assert [r[1].located for r in results] == [True, False]


def test_batch_verify_missing_chapter_is_unverified() -> None:
    from backend.vnext.citations import verify_facts_against_book

    fact = _fact("随便一句", start=1, end=5)
    results = verify_facts_against_book([fact], {})   # 章文本没给
    assert results[0][0] is fact
    assert results[0][1].reason == "CHAPTER_MISSING"
