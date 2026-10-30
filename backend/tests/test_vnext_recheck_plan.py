"""S7 复检规划与覆盖汇总（PLAN v2 §4 S7-3/7/9）。"""

from __future__ import annotations

import pytest

from backend.vnext.budget import RECHECK, Budget, BudgetCap
from backend.vnext.recheck_plan import (
    RecheckSummary,
    RecheckUnit,
    plan_recheck,
    record_plan_outcome,
    summarize_from_ledger,
)
from backend.vnext.tasks import (
    BUDGET_SKIPPED,
    NO_CANDIDATE,
    SUCCEEDED,
    TaskLedger,
    TaskRecord,
    make_task_key,
)


def _unit(entity: str, candidates: tuple[str, ...] = ("cd_1",), model: str = "m1") -> RecheckUnit:
    return RecheckUnit(
        entity_id=entity,
        candidate_ids=candidates,
        inputs={"model": model, "volume": 1},
    )


def _key(unit: RecheckUnit) -> str:
    return make_task_key(
        kind="recheck", stage="resolve",
        inputs={**dict(unit.inputs), "entity": unit.entity_id, "candidates": list(unit.candidate_ids)},
        strategy_versions={},
    )


# ---------------------------------------------------------------------------
# 缓存优先
# ---------------------------------------------------------------------------


def test_successful_task_is_reused_without_new_call() -> None:
    unit = _unit("fs_1")
    ledger = TaskLedger()
    ledger.record(TaskRecord(task_key=_key(unit), kind="recheck", stage="resolve", status=SUCCEEDED))
    plan = plan_recheck([unit], ledger=ledger)
    assert [u.entity_id for u in plan.reuse] == ["fs_1"]
    assert plan.new_calls == 0


def test_insufficient_result_is_reused() -> None:
    """证据不足是可复用结论：同输入重跑只会再花一次钱。"""
    unit = _unit("fs_1")
    ledger = TaskLedger()
    ledger.record(TaskRecord(task_key=_key(unit), kind="recheck", stage="resolve", status="INSUFFICIENT"))
    assert len(plan_recheck([unit], ledger=ledger).reuse) == 1


def test_changed_input_is_not_reused() -> None:
    unit = _unit("fs_1", model="m1")
    ledger = TaskLedger()
    ledger.record(TaskRecord(task_key=_key(unit), kind="recheck", stage="resolve", status=SUCCEEDED))
    plan = plan_recheck([_unit("fs_1", model="m2")], ledger=ledger)
    assert plan.reuse == []
    assert plan.new_calls == 1


# ---------------------------------------------------------------------------
# 没有候选
# ---------------------------------------------------------------------------


def test_entity_without_candidates_records_no_candidate_not_a_call() -> None:
    plan = plan_recheck([_unit("fs_9", candidates=())], ledger=TaskLedger())
    assert plan.no_candidate == ["fs_9"]
    assert plan.new_calls == 0


def test_no_candidate_is_persisted_as_a_conclusion() -> None:
    unit = _unit("fs_9", candidates=())
    ledger = TaskLedger()
    plan = plan_recheck([unit], ledger=ledger)
    record_plan_outcome(plan, ledger=ledger)
    record = ledger.latest(plan.task_keys["fs_9"])
    assert record is not None and record.status == NO_CANDIDATE
    # 下一轮同输入直接复用，不再重复判定
    again = plan_recheck([unit], ledger=ledger)
    assert [u.entity_id for u in again.reuse] == ["fs_9"]
    assert again.no_candidate == []


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------


def test_budget_exhaustion_marks_skipped_not_failed() -> None:
    budget = Budget(caps={RECHECK: BudgetCap(max_calls=1, max_tokens=1000)})
    ledger = TaskLedger()
    units = [_unit("fs_1"), _unit("fs_2")]
    # 占满额度
    budget.settle(budget.reserve(RECHECK, calls=1, tokens=10))
    plan = plan_recheck(units, ledger=ledger, budget=budget)
    assert [u.entity_id for u in plan.budget_skipped] == ["fs_1", "fs_2"]
    assert plan.new_calls == 0


def test_skipped_is_persisted_as_unfinished() -> None:
    budget = Budget(caps={RECHECK: BudgetCap(max_calls=0, max_tokens=0)})
    ledger = TaskLedger()
    plan = plan_recheck([_unit("fs_1")], ledger=ledger, budget=budget)
    record_plan_outcome(plan, ledger=ledger)
    record = ledger.latest(plan.task_keys["fs_1"])
    assert record is not None and record.status == BUDGET_SKIPPED
    assert record.reusable is False          # 未完成，拿到预算后可继续
    assert ledger.reusable(plan.task_keys["fs_1"]) is None


def test_budget_increase_lets_skipped_units_continue() -> None:
    ledger = TaskLedger()
    tight = Budget(caps={RECHECK: BudgetCap(max_calls=0, max_tokens=0)})
    plan = plan_recheck([_unit("fs_1")], ledger=ledger, budget=tight)
    record_plan_outcome(plan, ledger=ledger)

    roomy = Budget(caps={RECHECK: BudgetCap(max_calls=5, max_tokens=1000)})
    again = plan_recheck([_unit("fs_1")], ledger=ledger, budget=roomy)
    assert [u.entity_id for u in again.pending] == ["fs_1"]


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def test_summary_separates_skipped_from_completed() -> None:
    summary = RecheckSummary(candidates_total=10, new_calls=2, cache_reused=3,
                             no_candidate=4, budget_skipped=1)
    assert summary.executed == 5
    assert summary.not_completed_reason() is not None       # 有跳过 ⇒ 不算跑完
    payload = summary.as_dict()
    assert payload["budget_skipped"] == 1
    assert payload["not_completed_reason"].startswith("1 个单元")


def test_summary_complete_when_nothing_left() -> None:
    summary = RecheckSummary(candidates_total=2, new_calls=1, cache_reused=1)
    assert summary.not_completed_reason() is None


def test_summary_from_ledger_after_restart() -> None:
    """重启后只靠记录重建汇总：memory 里的计数器不可信。"""
    ledger = TaskLedger.from_dicts([
        {"task_key": "k1", "kind": "recheck", "stage": "resolve", "status": SUCCEEDED,
         "calls": 1, "prompt_tokens": 10, "completion_tokens": 4},
        {"task_key": "k2", "kind": "recheck", "stage": "resolve", "status": NO_CANDIDATE},
        {"task_key": "k3", "kind": "recheck", "stage": "resolve", "status": "FAILED",
         "calls": 1, "prompt_tokens": 6},
        {"task_key": "k4", "kind": "recheck", "stage": "resolve", "status": BUDGET_SKIPPED},
        {"task_key": "k5", "kind": "other", "stage": "resolve", "status": SUCCEEDED},
    ])
    summary = summarize_from_ledger(ledger, kind="recheck", stage="resolve", candidates_total=4)
    assert summary.new_calls == 1
    assert summary.no_candidate == 1
    assert summary.failed == 1
    assert summary.budget_skipped == 1
    assert summary.prompt_tokens == 16
    assert summary.not_completed_reason() is not None
