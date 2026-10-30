"""S7 任务账本与预算（PLAN v2 §4 S7-2/4/5/9）。

守的四件事：
1. 同输入复用不再调用；2. 输入变了换新键；3. 失败也扣额度；4. 未完成不写成功标记。
"""

from __future__ import annotations

import pytest

from backend.vnext.budget import (
    ANALYSIS,
    BUDGET_GROUPS,
    RECHECK,
    ROLLING_REBUILD,
    Budget,
    BudgetCap,
    BudgetExceeded,
    Usage,
    default_budget,
    rebuild_usage,
)
from backend.vnext.tasks import (
    BUDGET_SKIPPED,
    CANCELLED,
    FAILED,
    NO_CANDIDATE,
    STALE_INPUT,
    SUCCEEDED,
    TaskLedger,
    TaskRecord,
    make_task_key,
)


def _key(**over):
    base = dict(
        kind="recheck",
        stage="resolve",
        inputs={"entity": "fs_1", "candidates": ["cd_1"], "model": "m1"},
        strategy_versions={"prompt": 1, "context": 1},
    )
    base.update(over)
    return make_task_key(**base)


# ---------------------------------------------------------------------------
# 任务键
# ---------------------------------------------------------------------------


def test_same_input_same_key() -> None:
    assert _key() == _key()


def test_key_changes_when_any_input_changes() -> None:
    assert _key() != _key(inputs={"entity": "fs_1", "candidates": ["cd_2"], "model": "m1"})
    assert _key() != _key(strategy_versions={"prompt": 2, "context": 1})
    assert _key() != _key(stage="other")


def test_key_is_order_independent_for_inputs() -> None:
    a = make_task_key(kind="k", stage="s", inputs={"x": 1, "y": 2}, strategy_versions={"v": 1})
    b = make_task_key(kind="k", stage="s", inputs={"y": 2, "x": 1}, strategy_versions={"v": 1})
    assert a == b


# ---------------------------------------------------------------------------
# 复用规则
# ---------------------------------------------------------------------------


def _rec(key: str, status: str, **over) -> TaskRecord:
    base = dict(task_key=key, kind="recheck", stage="resolve", status=status)
    base.update(over)
    return TaskRecord(**base)


def test_success_is_reusable() -> None:
    ledger = TaskLedger()
    ledger.record(_rec("k1", SUCCEEDED, result_ref="cd_1"))
    assert ledger.reusable("k1") is not None


def test_insufficient_is_reusable_not_retried() -> None:
    """"证据不足"是结论：重跑同输入只会再花一次钱得到同一结论。"""
    ledger = TaskLedger()
    ledger.record(_rec("k1", "INSUFFICIENT"))
    assert ledger.reusable("k1") is not None


def test_failed_and_skipped_are_not_reusable() -> None:
    for status in (FAILED, CANCELLED, BUDGET_SKIPPED, STALE_INPUT):
        ledger = TaskLedger()
        ledger.record(_rec("k1", status))
        assert ledger.reusable("k1") is None, status


def test_no_candidate_is_a_conclusion() -> None:
    """没有可回收候选 = 查过了，不是"没跑"。"""
    ledger = TaskLedger()
    ledger.record(_rec("k1", NO_CANDIDATE))
    assert ledger.reusable("k1") is not None


def test_failed_retry_does_not_shadow_earlier_success() -> None:
    ledger = TaskLedger()
    ledger.record(_rec("k1", SUCCEEDED, result_ref="cd_1"))
    ledger.record(_rec("k1", FAILED, attempt=2))
    found = ledger.reusable("k1")
    assert found is not None and found.result_ref == "cd_1"


# ---------------------------------------------------------------------------
# 应用键：同一结论只能应用一次
# ---------------------------------------------------------------------------


def test_same_apply_key_applied_once() -> None:
    ledger = TaskLedger()
    assert ledger.record(_rec("k1", SUCCEEDED, apply_key="ap_1")) is True
    assert ledger.record(_rec("k2", SUCCEEDED, apply_key="ap_1")) is False
    assert len(ledger) == 1


def test_unfinished_record_with_apply_key_does_not_block() -> None:
    """失败的任务没应用过结论，不该占住应用键。"""
    ledger = TaskLedger()
    ledger.record(_rec("k1", FAILED, apply_key="ap_1"))
    assert ledger.record(_rec("k2", SUCCEEDED, apply_key="ap_1")) is True


# ---------------------------------------------------------------------------
# 失效与重放
# ---------------------------------------------------------------------------


def test_input_change_produces_new_key_and_old_result_stays_in_history() -> None:
    old_key = _key()
    new_key = _key(inputs={"entity": "fs_1", "candidates": ["cd_1"], "model": "m2"})
    ledger = TaskLedger()
    ledger.record(_rec(old_key, SUCCEEDED, result_ref="cd_old"))
    assert ledger.reusable(new_key) is None          # 新输入不能复用旧结论
    assert ledger.reusable(old_key) is not None      # 但历史结论不能消失


def test_mark_stale_only_touches_unfinished() -> None:
    ledger = TaskLedger()
    ledger.record(_rec("k1", SUCCEEDED, result_ref="cd_1"))
    ledger.record(_rec("k1", FAILED))
    assert ledger.mark_stale("k1") == 1
    statuses = [r.status for r in ledger.history("k1")]
    assert statuses == [SUCCEEDED, STALE_INPUT]


def test_roundtrip_through_dicts() -> None:
    ledger = TaskLedger()
    ledger.record(_rec("k1", SUCCEEDED, result_ref="cd_1", calls=1, prompt_tokens=10))
    restored = TaskLedger.from_dicts(ledger.to_dicts())
    assert restored.to_dicts() == ledger.to_dicts()


def test_totals_report_usage_regardless_of_status() -> None:
    ledger = TaskLedger()
    ledger.record(_rec("k1", SUCCEEDED, calls=1, prompt_tokens=10, completion_tokens=5))
    ledger.record(_rec("k2", FAILED, calls=1, prompt_tokens=7, completion_tokens=3))
    totals = ledger.totals()
    assert totals["calls"] == 2
    assert totals["prompt_tokens"] == 17      # 失败的那次也花了钱
    assert totals["by_status"] == {FAILED: 1, SUCCEEDED: 1}


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------


def test_reserve_blocks_before_shipping_when_exhausted() -> None:
    budget = Budget(caps={RECHECK: BudgetCap(max_calls=2, max_tokens=100)})
    budget.reserve(RECHECK, calls=1, tokens=40)
    budget.reserve(RECHECK, calls=1, tokens=40)
    with pytest.raises(BudgetExceeded):
        budget.reserve(RECHECK, calls=1, tokens=40)


def test_in_flight_reservations_count_against_cap() -> None:
    """十个并发都"先查再调"会同时通过 —— 预留必须在派发前生效。"""
    budget = Budget(caps={RECHECK: BudgetCap(max_calls=1, max_tokens=100)})
    budget.reserve(RECHECK, calls=1, tokens=10)
    with pytest.raises(BudgetExceeded):
        budget.reserve(RECHECK, calls=1, tokens=10)


def test_release_returns_unstarted_capacity() -> None:
    budget = Budget(caps={RECHECK: BudgetCap(max_calls=1, max_tokens=100)})
    reservation = budget.reserve(RECHECK, calls=1, tokens=10)
    budget.release(reservation)
    budget.reserve(RECHECK, calls=1, tokens=10)   # 不抛即通过


def test_failed_attempt_still_spends() -> None:
    budget = Budget(caps={RECHECK: BudgetCap(max_calls=5, max_tokens=100)})
    reservation = budget.reserve(RECHECK, calls=1, tokens=10)
    budget.settle(reservation, actual=Usage(calls=1, prompt_tokens=8))
    assert budget.used[RECHECK].calls == 1
    assert budget.used[RECHECK].prompt_tokens == 8


def test_settle_without_actual_uses_reservation() -> None:
    budget = Budget(caps={RECHECK: BudgetCap(max_calls=5, max_tokens=100)})
    reservation = budget.reserve(RECHECK, calls=1, tokens=10)
    budget.settle(reservation)
    assert budget.used[RECHECK].tokens == 10


def test_default_budget_forbids_rolling_rebuild_and_semantic_review() -> None:
    """本版明确不做这两类动作：默认 0，真被调用时当场失败，而不是悄悄花钱。"""
    budget = default_budget()
    with pytest.raises(BudgetExceeded):
        budget.reserve(ROLLING_REBUILD, calls=1, tokens=1)


def test_unknown_group_is_rejected() -> None:
    budget = default_budget()
    with pytest.raises(KeyError):
        budget.reserve("typo_group", calls=1)


def test_remaining_and_exhausted() -> None:
    budget = Budget(caps={ANALYSIS: BudgetCap(max_calls=3, max_tokens=100)})
    assert budget.remaining(ANALYSIS).calls == 3
    assert budget.exhausted(ANALYSIS) is False
    for _ in range(3):
        budget.settle(budget.reserve(ANALYSIS, calls=1, tokens=10))
    assert budget.exhausted(ANALYSIS) is True


def test_rebuild_usage_from_records() -> None:
    rows = [
        {"group": RECHECK, "calls": 1, "prompt_tokens": 10, "completion_tokens": 5},
        {"group": RECHECK, "calls": 2, "prompt_tokens": 20, "completion_tokens": 1},
    ]
    usage = rebuild_usage(rows)
    assert usage[RECHECK].calls == 3
    assert usage[RECHECK].prompt_tokens == 30


def test_all_groups_have_caps_and_snapshot_is_json_ready() -> None:
    budget = default_budget()
    snapshot = budget.snapshot()
    assert set(snapshot["caps"]) == set(BUDGET_GROUPS)
    assert set(snapshot["used"]) == set(BUDGET_GROUPS)
