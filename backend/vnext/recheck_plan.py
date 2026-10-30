"""复检规划与覆盖汇总（PLAN v2 §4 S7-3/7/9）。

规划只做三件事，顺序固定：

1. **先查缓存**：同输入的成功任务直接复用，一次模型调用都不发；
2. **再认"没有候选"**：实体没有任何可回收候选时记 ``NO_CANDIDATE``（这是结论，不是跳过），
   绝不虚构一个回收窗口去"核对"；
3. **最后按预算分批**：额度不足的进 ``budget_skipped``（未完成态），拿到预算可续跑。

汇总必须把"新调用 / 缓存复用 / 无候选 / 失败 / 取消 / 预算跳过"分开报。
把"未执行"算进"已完成"，是这类系统最容易做、也最难发现的一种谎。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .budget import Budget, BudgetExceeded, RECHECK
from .tasks import (
    BUDGET_SKIPPED,
    NO_CANDIDATE,
    SUCCEEDED,
    TaskLedger,
    TaskRecord,
    make_task_key,
)


@dataclass(frozen=True)
class RecheckUnit:
    """一个待核对单元：一个实体 + 它的候选 + 输入摘要。"""

    entity_id: str
    candidate_ids: tuple[str, ...]
    inputs: Mapping[str, object]

    @property
    def has_candidate(self) -> bool:
        return bool(self.candidate_ids)


@dataclass
class RecheckPlan:
    """规划结果：谁能复用、谁没候选、谁要新调、谁被预算挡下。"""

    reuse: list[RecheckUnit] = field(default_factory=list)
    no_candidate: list[str] = field(default_factory=list)
    pending: list[RecheckUnit] = field(default_factory=list)
    budget_skipped: list[RecheckUnit] = field(default_factory=list)
    task_keys: dict[str, str] = field(default_factory=dict)

    @property
    def new_calls(self) -> int:
        return len(self.pending)

    def as_dict(self) -> dict:
        return {
            "reuse": [unit.entity_id for unit in self.reuse],
            "no_candidate": list(self.no_candidate),
            "pending": [unit.entity_id for unit in self.pending],
            "budget_skipped": [unit.entity_id for unit in self.budget_skipped],
        }


def plan_recheck(
    units: Iterable[RecheckUnit],
    *,
    ledger: TaskLedger,
    budget: Budget | None = None,
    stage: str = "resolve",
    strategy_versions: Mapping[str, int] | None = None,
    tokens_per_call: int = 0,
) -> RecheckPlan:
    """按"缓存 → 无候选 → 预算"的顺序规划一轮复检。"""
    plan = RecheckPlan()
    versions = dict(strategy_versions or {})

    for unit in units:
        key = make_task_key(
            kind="recheck",
            stage=stage,
            inputs={**dict(unit.inputs), "entity": unit.entity_id,
                    "candidates": list(unit.candidate_ids)},
            strategy_versions=versions,
        )
        plan.task_keys[unit.entity_id] = key

        # 1. 同输入的成功任务：直接复用（包括"证据不足"这种结论）
        if ledger.reusable(key) is not None:
            plan.reuse.append(unit)
            continue

        # 2. 没有候选：这是结论，不是待办
        if not unit.has_candidate:
            plan.no_candidate.append(unit.entity_id)
            continue

        # 3. 预算：派发前预留，失败即记"预算跳过"
        if budget is None:
            plan.pending.append(unit)
            continue
        try:
            reservation = budget.reserve(RECHECK, calls=1, tokens=tokens_per_call)
        except BudgetExceeded:
            plan.budget_skipped.append(unit)
            continue
        # 规划阶段只确认"能发"，真正发请求时由执行方 settle/release
        budget.release(reservation)
        plan.pending.append(unit)

    return plan


def record_plan_outcome(
    plan: RecheckPlan,
    *,
    ledger: TaskLedger,
    kind: str = "recheck",
    stage: str = "resolve",
) -> None:
    """把"没候选"与"预算跳过"落成任务记录——它们必须留下痕迹。

    ``NO_CANDIDATE`` 记成功终态（下次同输入直接复用，不再重复判定）；
    ``BUDGET_SKIPPED`` 记未完成态（拿到预算后可以继续）。
    """
    for entity_id in plan.no_candidate:
        key = plan.task_keys[entity_id]
        ledger.record(
            TaskRecord(task_key=key, kind=kind, stage=stage, status=NO_CANDIDATE,
                       reason="没有可回收候选")
        )
    for unit in plan.budget_skipped:
        key = plan.task_keys[unit.entity_id]
        ledger.record(
            TaskRecord(task_key=key, kind=kind, stage=stage, status=BUDGET_SKIPPED,
                       reason="预算不足，本轮未执行")
        )


@dataclass
class RecheckSummary:
    """一轮复检的覆盖汇总。字段与 PLAN v2 §4 S7-7 一一对应。"""

    candidates_total: int = 0
    new_calls: int = 0
    cache_reused: int = 0
    no_candidate: int = 0
    failed: int = 0
    cancelled: int = 0
    budget_skipped: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def executed(self) -> int:
        """真正执行过的单元数（不含被跳过与无候选）。"""
        return self.new_calls + self.cache_reused

    def not_completed_reason(self) -> str | None:
        """为什么这一轮不算跑完。``None`` 表示跑完了。"""
        if self.cancelled:
            return f"{self.cancelled} 个单元被取消"
        if self.failed:
            return f"{self.failed} 个单元失败"
        if self.budget_skipped:
            return f"{self.budget_skipped} 个单元因预算不足未执行"
        return None

    def as_dict(self) -> dict:
        return {
            "candidates_total": self.candidates_total,
            "new_calls": self.new_calls,
            "cache_reused": self.cache_reused,
            "no_candidate": self.no_candidate,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "budget_skipped": self.budget_skipped,
            "executed": self.executed,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "not_completed_reason": self.not_completed_reason(),
        }


def summarize_from_ledger(
    ledger: TaskLedger,
    *,
    kind: str,
    stage: str,
    candidates_total: int = 0,
) -> RecheckSummary:
    """从任务记录汇总本轮情况（重启后同样可用，不依赖内存状态）。"""
    summary = RecheckSummary(candidates_total=candidates_total)
    for record in ledger.records():
        if record.kind != kind or record.stage != stage:
            continue
        summary.prompt_tokens += record.prompt_tokens
        summary.completion_tokens += record.completion_tokens
        if record.status == SUCCEEDED:
            summary.new_calls += 1
        elif record.status == NO_CANDIDATE:
            summary.no_candidate += 1
        elif record.status == BUDGET_SKIPPED:
            summary.budget_skipped += 1
        elif record.status == "CANCELLED":
            summary.cancelled += 1
        elif record.status == "FAILED":
            summary.failed += 1
    return summary
