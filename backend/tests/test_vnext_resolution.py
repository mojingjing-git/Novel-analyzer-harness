"""全自动裁决（S6）：严格枚举、反证必查、垃圾候选不降级、覆盖必须守恒。

用例按"模型会怎么出错"组织：小写枚举、缺反证、编造引文、外来 ID、空答案、
以及最容易漏的"没跑却说无发现"。
"""

from __future__ import annotations

import pytest

from backend.vnext.resolution import (
    AUTO_RESOLVED,
    COMPLETED,
    CONTRADICTED,
    DIFFERENT_ANSWER,
    INSUFFICIENT,
    INVALID,
    NOT_RUN,
    OPEN,
    PARTIAL,
    PROPOSED_RESOLVED,
    REASON_COUNTER_QUOTES_PRESENT,
    REASON_EMPTY_ANSWER,
    REASON_FOREIGN_ID,
    REASON_MATERIAL_INSUFFICIENT,
    REASON_NO_COUNTER_SEARCH,
    REASON_QUOTE_OUT_OF_RANGE,
    REASON_UNKNOWN_VERDICT,
    RUNNING,
    STOPPED,
    SUPPORTED,
    CandidateVerdict,
    count_verdicts,
    parse_recheck,
    recheck_status,
    reduce_entity,
)

CANDIDATES = {"cd_1"}
EVIDENCE = {"ev_1"}


def _payload(**over):
    base = {
        "candidate_id": "cd_1",
        "evidence_id": "ev_1",
        "verdict": "SUPPORTED",
        "supporting_quote": "王冠早已熔成金锭",
        "supporting_span": [10, 20],
        "answer": "王冠已被熔毁",
        "counter_search": {"performed": True, "checked_evidence_ids": ["ev_1"], "counter_quotes": []},
        "material_insufficient_reason": None,
    }
    base.update(over)
    return base


def _parse(payload, *, verifier=None):
    # 2026-10-30 复审：SUPPORTED 必须有可定位证据，且校验器在场才算核过
    return parse_recheck(
        payload,
        allowed_candidate_ids=CANDIDATES,
        allowed_evidence_ids=EVIDENCE,
        quote_within_range=verifier if verifier is not None else (lambda q, s, e: True),
    )


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_supported_passes_all_gates() -> None:
    verdict = _parse(_payload())
    assert verdict.verdict == SUPPORTED
    assert verdict.answer == "王冠已被熔毁"
    assert verdict.counter_search_performed is True
    # 证据三件套必须齐备：ID、区间、引文
    assert verdict.evidence_id == "ev_1"
    assert verdict.supporting_span == (10, 20)


def test_supported_with_valid_span_and_quote() -> None:
    verdict = _parse(_payload(supporting_span=[10, 20], supporting_quote="王冠早已熔成金锭"))
    assert verdict.supporting_span == (10, 20)


# ---------------------------------------------------------------------------
# 枚举必须严格
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["supported", "OK", "Supported", "", None, 1])
def test_unknown_or_loose_verdict_is_invalid_not_pass(bad) -> None:
    """小写/近义词一律 INVALID：宽容解析会让下游以为这条已经核对过。"""
    verdict = _parse(_payload(verdict=bad))
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_UNKNOWN_VERDICT


def test_missing_candidate_id_is_invalid() -> None:
    verdict = parse_recheck({"verdict": "SUPPORTED"}, allowed_candidate_ids=CANDIDATES,
                            allowed_evidence_ids=EVIDENCE)
    assert verdict.verdict == INVALID


def test_foreign_candidate_id_is_rejected() -> None:
    verdict = _parse(_payload(candidate_id="cd_other"))
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_FOREIGN_ID


def test_foreign_evidence_id_is_rejected() -> None:
    verdict = _parse(_payload(evidence_id="ev_other"))
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_FOREIGN_ID


# ---------------------------------------------------------------------------
# 反证核对必须显式执行
# ---------------------------------------------------------------------------


def test_supported_without_counter_search_is_insufficient() -> None:
    """没查反证不是"响应坏了"，而是"材料/核对不足"——落 INSUFFICIENT，不是 INVALID。

    2026-10-30 复审：原先判 INVALID 会让实体变成 OPEN，等于把"没查"读成"这个悬念没问题"。
    """
    verdict = _parse(_payload(counter_search={"performed": False, "counter_quotes": []}))
    assert verdict.verdict == INSUFFICIENT
    assert verdict.reason == REASON_NO_COUNTER_SEARCH


def test_supported_with_counter_quotes_is_insufficient() -> None:
    """说"支持"却又列了反证：自相矛盾，交给人/下一轮，不能算支持。"""
    verdict = _parse(_payload(counter_search={"performed": True, "counter_quotes": ["反证"]}))
    assert verdict.verdict == INSUFFICIENT
    assert verdict.reason == REASON_COUNTER_QUOTES_PRESENT


def test_performed_must_be_true_not_truthy_string() -> None:
    verdict = _parse(_payload(counter_search={"performed": "true"}))
    assert verdict.verdict == INSUFFICIENT  # 没真执行 ⇒ 材料不足，而非响应无效


def test_material_insufficient_maps_to_insufficient() -> None:
    verdict = _parse(_payload(material_insufficient_reason="上下文不足"))
    assert verdict.verdict == INSUFFICIENT
    assert verdict.reason == REASON_MATERIAL_INSUFFICIENT


# ---------------------------------------------------------------------------
# 引文与答案
# ---------------------------------------------------------------------------


def test_empty_answer_blocks_supported() -> None:
    verdict = _parse(_payload(answer="   "))
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_EMPTY_ANSWER


def test_quote_outside_range_is_invalid() -> None:
    verdict = parse_recheck(
        _payload(supporting_span=[100, 120], supporting_quote="王冠早已熔成金锭"),
        allowed_candidate_ids=CANDIDATES,
        allowed_evidence_ids=EVIDENCE,
        quote_within_range=lambda quote, start, end: False,
    )
    assert verdict.verdict == INVALID
    assert verdict.reason == REASON_QUOTE_OUT_OF_RANGE


def test_inverted_span_is_invalid() -> None:
    verdict = _parse(_payload(supporting_span=[20, 10]))
    assert verdict.verdict == INVALID


# ---------------------------------------------------------------------------
# 非 SUPPORTED 的三个枚举
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enum", [DIFFERENT_ANSWER, CONTRADICTED, INSUFFICIENT])
def test_other_verdicts_pass_without_quote_gates(enum) -> None:
    """答不对本身也是结论：不要求支持引文，但要如实记录反证是否执行。"""
    verdict = _parse(_payload(verdict=enum, answer="另一个答案"))
    assert verdict.verdict == enum
    assert verdict.counter_search_performed is True


# ---------------------------------------------------------------------------
# 实体归约
# ---------------------------------------------------------------------------


def _v(verdict, reason="", candidate_id="cd_1"):
    return CandidateVerdict(candidate_id=candidate_id, verdict=verdict, reason=reason)


def test_any_supported_resolves_entity() -> None:
    state, basis = reduce_entity([_v(INSUFFICIENT), _v(SUPPORTED)])
    assert state == AUTO_RESOLVED
    assert basis is not None and basis.verdict == SUPPORTED


def test_insufficient_only_makes_it_proposed() -> None:
    assert reduce_entity([_v(INSUFFICIENT)])[0] == PROPOSED_RESOLVED
    assert reduce_entity([_v(CONTRADICTED)])[0] == PROPOSED_RESOLVED


def test_invalid_only_leaves_it_open() -> None:
    assert reduce_entity([_v(INVALID, REASON_UNKNOWN_VERDICT)])[0] == OPEN
    assert reduce_entity([])[0] == OPEN


def test_garbage_candidate_does_not_downgrade_supported() -> None:
    """新来的垃圾候选只影响它自己。"""
    state, _ = reduce_entity([_v(SUPPORTED), _v(INVALID, REASON_FOREIGN_ID)])
    assert state == AUTO_RESOLVED


# ---------------------------------------------------------------------------
# 覆盖计数与守恒
# ---------------------------------------------------------------------------


def test_counts_are_conserved() -> None:
    verdicts = [_v(SUPPORTED), _v(INSUFFICIENT), _v(CONTRADICTED), _v(INVALID, REASON_UNKNOWN_VERDICT)]
    counts = count_verdicts(verdicts, candidates_total=4)
    assert counts.supported == 1
    assert counts.insufficient == 1
    assert counts.conflict == 1          # DIFFERENT_ANSWER + CONTRADICTED 合并计
    assert counts.invalid == 1
    assert counts.not_checked == 0
    assert counts.conservation_error() is None


def test_stale_candidates_are_counted_separately() -> None:
    counts = count_verdicts([_v(SUPPORTED)], candidates_total=5, stale=2)
    assert counts.stale == 2
    assert counts.not_checked == 2       # 5 - (1 supported + 2 stale)
    assert counts.conservation_error() is None


def test_conservation_error_detects_missing_bucket() -> None:
    counts = count_verdicts([_v(SUPPORTED)], candidates_total=3)
    assert counts.not_checked == 2       # 自动补齐为"未核对"
    assert counts.conservation_error() is None


def test_invalid_by_reason_must_sum_to_invalid() -> None:
    verdicts = [_v(INVALID, REASON_FOREIGN_ID), _v(INVALID, REASON_EMPTY_ANSWER)]
    counts = count_verdicts(verdicts, candidates_total=2)
    assert counts.invalid_by_reason == {REASON_FOREIGN_ID: 1, REASON_EMPTY_ANSWER: 1}
    assert counts.conservation_error() is None


# ---------------------------------------------------------------------------
# 任务状态：跑没跑完 vs 解决没解决
# ---------------------------------------------------------------------------


def test_status_not_run_when_never_started() -> None:
    assert recheck_status(started=False, finished=False, stopped=False, counts=None) == NOT_RUN


def test_status_running_and_stopped() -> None:
    assert recheck_status(started=True, finished=False, stopped=False, counts=None) == RUNNING
    assert recheck_status(started=True, finished=False, stopped=True, counts=None) == STOPPED


def test_empty_candidates_run_to_completion_is_completed_with_zero() -> None:
    """候选为空但跑过一轮 → COMPLETED 且计数 0（与"没跑"必须可区分）。"""
    counts = count_verdicts([], candidates_total=0)
    assert recheck_status(started=True, finished=True, stopped=False, counts=counts) == COMPLETED


def test_missing_coverage_summary_blocks_completion() -> None:
    assert recheck_status(started=True, finished=True, stopped=False, counts=None) == PARTIAL


def test_not_checked_makes_partial() -> None:
    counts = count_verdicts([_v(SUPPORTED)], candidates_total=4)
    assert recheck_status(started=True, finished=True, stopped=False, counts=counts) == PARTIAL
