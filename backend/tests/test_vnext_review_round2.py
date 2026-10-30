"""第二轮外部复审七条的回归（2026-10-30）。

这七条的共同点：**都能通过原来的用例，因为原用例把错误行为当成了正确行为**。
所以这里不写"函数返回什么"，只写"什么情况下结论必须不算数"。
"""

from __future__ import annotations

import pytest

from backend.vnext import context, rolling
from backend.vnext.blocks import BlockPlan
from backend.vnext.foreshadow import Entity, Occurrence, cutoff_view, make_entity_id, make_occurrence_id
from backend.vnext.knowledge import SourceRef
from backend.vnext.resolution import (
    AUTO_RESOLVED,
    INSUFFICIENT,
    INVALID,
    OPEN,
    REASON_NO_COUNTER_SEARCH,
    REASON_NO_EVIDENCE,
    REASON_NO_QUOTE,
    REASON_NO_VERIFIER,
    REASON_QUOTE_OUT_OF_RANGE,
    SUPPORTED,
    CandidateVerdict,
    count_verdicts,
    parse_recheck,
    recheck_status,
    reduce_entity,
)


def _src(chapter: int) -> SourceRef:
    return SourceRef(f"ch_{chapter:04d}", chapter, 0, 6, f"h{chapter}")


def _payload(**over):
    base = {
        "candidate_id": "cd_1",
        "verdict": "SUPPORTED",
        "answer": "答案",
        "evidence_id": "ev_1",
        "supporting_span": [0, 5],
        "supporting_quote": "原文片段",
        "counter_search": {"performed": True, "counter_quotes": []},
    }
    base.update(over)
    return base


def _parse(**over):
    return parse_recheck(
        _payload(**over),
        allowed_candidate_ids={"cd_1"},
        allowed_evidence_ids={"ev_1"},
        quote_within_range=lambda q, s, e: True,
    )


# ---------------------------------------------------------------------------
# 1. 没有原文证据不能自动回收
# ---------------------------------------------------------------------------


def test_supported_without_evidence_id_is_invalid() -> None:
    verdict = _parse(evidence_id=None)
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_NO_EVIDENCE


def test_supported_without_span_is_invalid() -> None:
    verdict = _parse(supporting_span=None)
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_NO_QUOTE


def test_supported_without_quote_is_invalid() -> None:
    verdict = _parse(supporting_quote="   ")
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_NO_QUOTE


def test_supported_without_verifier_is_invalid() -> None:
    """没有校验器就无法确认引文真在原文里；此时放行等于让模型自己给自己打分。"""
    verdict = parse_recheck(
        _payload(),
        allowed_candidate_ids={"cd_1"},
        allowed_evidence_ids={"ev_1"},
        quote_within_range=None,
    )
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_NO_VERIFIER


def test_supported_with_quote_mismatch_is_invalid() -> None:
    verdict = parse_recheck(
        _payload(),
        allowed_candidate_ids={"cd_1"},
        allowed_evidence_ids={"ev_1"},
        quote_within_range=lambda q, s, e: False,
    )
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_QUOTE_OUT_OF_RANGE


def test_evidence_less_supported_cannot_auto_resolve() -> None:
    """完整链路：缺证据的 SUPPORTED 不能把实体推到 AUTO_RESOLVED。"""
    verdict = _parse(evidence_id=None)
    state, _ = reduce_entity([verdict])
    assert state == OPEN  # 一条 INVALID 什么都不算


# ---------------------------------------------------------------------------
# 2. 前缀不变性：追加未来证据不改变过去的视图
# ---------------------------------------------------------------------------


def _occ(entity_id: str, chapter: int) -> Occurrence:
    src = _src(chapter)
    return Occurrence(
        occurrence_id=make_occurrence_id(book_id="bk", source=src, kind="mention"),
        entity_id=entity_id,
        source=src,
        quote="原文",
    )


def test_appending_future_occurrence_keeps_earlier_view_identical() -> None:
    entity = Entity(entity_id="fs_a", book_id="bk", motif="棺中人", seed_occurrence_id="occ_x")
    before = cutoff_view(entities=[entity], occurrences=[_occ("fs_a", 3)], cutoff_ordinal=11)
    after = cutoff_view(
        entities=[entity], occurrences=[_occ("fs_a", 3), _occ("fs_a", 12)], cutoff_ordinal=11
    )
    assert before == after


def test_entity_attribute_uses_only_visible_occurrences() -> None:
    entity = Entity(entity_id="fs_a", book_id="bk", motif="棺中人", seed_occurrence_id="occ_x")
    rows = cutoff_view(
        entities=[entity], occurrences=[_occ("fs_a", 3), _occ("fs_a", 12)], cutoff_ordinal=11
    )
    assert rows[0]["mention_count"] == 1
    assert rows[0]["last_seen_ordinal"] == 3


# ---------------------------------------------------------------------------
# 3. 签名不符不得继承完成状态
# ---------------------------------------------------------------------------


def test_signature_mismatch_downgrades_to_pending() -> None:
    plans = [BlockPlan("new_sig", ("c1",), 1)]
    states = {0: rolling.BlockSlot(0, "old_sig", rolling.DONE, "old_result")}
    coverage = rolling.build_coverage(plans, batch_size=1, states=states)
    assert coverage.commit_cursor == 0
    assert coverage.prefix_complete is False
    assert coverage.slots[0].result_id is None
    assert coverage.slots[0].reason == rolling.REASON_SIGNATURE_CHANGED


def test_matching_signature_still_inherits() -> None:
    """不能因为要修 #3 就把正常的"签名一致→继承"也废掉。"""
    plans = [BlockPlan("sig", ("c1",), 1)]
    states = {0: rolling.BlockSlot(0, "sig", rolling.DONE, "r1")}
    coverage = rolling.build_coverage(plans, batch_size=1, states=states)
    assert coverage.commit_cursor == 1
    assert coverage.slots[0].result_id == "r1"


# ---------------------------------------------------------------------------
# 4. 候选覆盖必须按集合核对
# ---------------------------------------------------------------------------


def test_duplicate_candidate_rows_do_not_inflate_supported() -> None:
    rows = [CandidateVerdict("c1", SUPPORTED, answer="a"), CandidateVerdict("c1", SUPPORTED, answer="a")]
    counts = count_verdicts(rows, expected_candidate_ids=["c1", "c2"])
    assert counts.supported == 0        # 同一个候选答了两次，一次都不能算
    assert counts.duplicates == 1
    assert counts.not_checked == 1      # c2 没答
    assert counts.integrity_error() is not None


def test_missing_candidate_is_reported_as_not_checked() -> None:
    rows = [CandidateVerdict("c1", SUPPORTED, answer="a")]
    counts = count_verdicts(rows, expected_candidate_ids=["c1", "c2", "c3"])
    assert counts.not_checked == 2
    assert counts.conservation_error() is None


def test_foreign_candidate_is_counted_separately() -> None:
    rows = [CandidateVerdict("c1", SUPPORTED, answer="a"), CandidateVerdict("zz", SUPPORTED, answer="a")]
    counts = count_verdicts(rows, expected_candidate_ids=["c1"])
    assert counts.foreign == 1
    assert counts.supported == 1
    assert counts.conservation_error() is None


def test_duplicates_prevent_completed_status() -> None:
    rows = [CandidateVerdict("c1", SUPPORTED, answer="a"), CandidateVerdict("c1", SUPPORTED, answer="a")]
    counts = count_verdicts(rows, expected_candidate_ids=["c1"])
    assert recheck_status(started=True, finished=True, stopped=False, counts=counts) == "PARTIAL"


# ---------------------------------------------------------------------------
# 5. 同锚点的不同悬念不能撞成同一个实体
# ---------------------------------------------------------------------------


def test_same_anchor_different_motif_yields_different_ids() -> None:
    a = make_entity_id(book_id="bk", seed_occurrence_id="occ_1", motif_key="棺中人身份")
    b = make_entity_id(book_id="bk", seed_occurrence_id="occ_1", motif_key="雨夜敲门者")
    assert a != b


def test_same_motif_and_seed_is_stable() -> None:
    assert make_entity_id(book_id="bk", seed_occurrence_id="occ_1", motif_key="x") == make_entity_id(
        book_id="bk", seed_occurrence_id="occ_1", motif_key="x"
    )


# ---------------------------------------------------------------------------
# 6. 窗口边界落在句中必须算截断
# ---------------------------------------------------------------------------


def test_budget_capped_window_reports_truncation_and_still_covers_quote() -> None:
    """预算装不下完整句时：如实标截断，但引用必须完整落在窗口里。"""
    text = "第一句话。" + "甲" * 200 + "。第二句话。" + "乙" * 200 + "。"
    start = text.index("第二句话")
    window = context.build_context(
        text, span_start=start, span_end=start + 4, pre_target=150, post_target=150, hard_max=40
    )
    assert window.truncated is True
    assert window.reason.startswith("HARD_BUDGET")
    assert context.context_covers(window, span_start=start, span_end=start + 4)


def test_uncapped_window_never_ends_mid_sentence() -> None:
    """没有预算压力时，窗口边界必须落在句界上——半句话会被模型当成原文全部。"""
    text = "甲" * 300 + "。目标句。" + "乙" * 300 + "。"
    start = text.index("目标句")
    window = context.build_context(text, span_start=start, span_end=start + 3)
    assert window.truncated is False
    assert window.text.endswith("。")
    assert text[window.start - 1] in context.SENTENCE_DELIMITERS


def test_sentence_aligned_window_is_not_truncated() -> None:
    text = "第一句话。第二句话。第三句话。"
    start = text.index("第二句")
    window = context.build_context(text, span_start=start, span_end=start + 3)
    assert window.truncated is False
    assert window.text.startswith("第二句") or "第二句" in window.text
    assert window.text.endswith("。")


def test_hard_cap_is_respected_even_when_sentences_are_long() -> None:
    text = "甲" * 5000 + "目标句。" + "乙" * 5000
    start = text.index("目标句")
    window = context.build_context(
        text, span_start=start, span_end=start + 3, pre_target=3000, post_target=3000, hard_max=2000
    )
    assert window.chars_used <= 2000
    assert context.context_covers(window, span_start=start, span_end=start + 3)


# ---------------------------------------------------------------------------
# 7. 没查反证是"材料不足"，不是"响应无效"
# ---------------------------------------------------------------------------


def test_no_counter_search_counts_as_insufficient_not_invalid() -> None:
    verdict = _parse(counter_search={"performed": False, "counter_quotes": []})
    assert verdict.verdict == INSUFFICIENT
    assert verdict.reason == REASON_NO_COUNTER_SEARCH

    counts = count_verdicts([verdict], expected_candidate_ids=["cd_1"])
    assert counts.insufficient == 1
    assert counts.invalid == 0


def test_insufficient_candidate_still_reaches_proposed_state() -> None:
    """"证据不足"是有信息量的结论：实体应停在"候选已提出"，而不是回到 OPEN。"""
    verdict = _parse(counter_search={"performed": False, "counter_quotes": []})
    state, _ = reduce_entity([verdict])
    assert state == "PROPOSED_RESOLVED"


def test_parse_failure_is_retryable_and_not_counted_as_checked() -> None:
    """枚举写错是"这次没答好"，不该变成"这个候选被判无效"；它必须能被重跑。"""
    bad = parse_recheck(
        _payload(verdict="supported"),
        allowed_candidate_ids={"cd_1"},
        allowed_evidence_ids={"ev_1"},
    )
    assert bad.verdict == INVALID
    assert bad.retryable is True
    counts = count_verdicts([bad], expected_candidate_ids=["cd_1"])
    assert counts.not_checked == 1
    assert counts.invalid == 0
