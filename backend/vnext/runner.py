"""vnext 复检执行入口：把规划、预算、裁决、账本、落盘串成一条真实链路。

为什么要它（复审 2026-10-30 第 1 条）：``tasks``/``budget``/``recheck_plan``/``resolution``
单独都测过，但**没有任何生产入口消费它们**，于是"27/27 通过"只能证明零件能拼在一起，
不能证明"跑一次 → 落盘 → 重启 → 复用"这条链成立。本模块就是那条链。

三条纪律：

1. **调用前预留、调用后结算**：规划阶段拿到的预留必须由这里 settle（或确认未发出时
   release），否则额度永远挂在途，后续轮次全被挡。
2. **账本先落盘再前进**：每完成一个单元就持久化一次。中途崩溃时"已经花了钱的那几次"
   必须留在账上，否则重启后预算会被重复花掉。
3. **模型只提供素材，不提供结论**：交给 ``call_model`` 的是单元；返回的 payload 一律
   过 :func:`resolution.parse_recheck` 的门禁。跳过门禁的"支持"等于没有校验。

本模块不碰网络、不读小说正文：取材由调用方注入（``chapter_text`` / ``verify``），
这样离线也能完整跑一遍链路。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from . import artifacts
from .budget import Budget, Usage
from .recheck_plan import (
    RecheckPlan,
    RecheckSummary,
    RecheckUnit,
    plan_recheck,
    record_plan_outcome,
    summarize_from_ledger,
)
from .resolution import (
    CONTRADICTED,
    DIFFERENT_ANSWER,
    INSUFFICIENT,
    SUPPORTED,
    CandidateVerdict,
    count_verdicts,
    parse_recheck,
)
from .tasks import (
    CANCELLED,
    FAILED,
    STALE_INPUT,
    SUCCEEDED,
    TaskLedger,
    TaskRecord,
)

LEDGER_FILE_NAME = "recheck_ledger.json"

#: 裁决结论 → 任务状态。SUPPORTED/不足/冲突都是**终态**（可复用），
#: 只有"响应不可用"才落到 FAILED 等可重跑的状态。
_STATUS_BY_VERDICT = {
    SUPPORTED: SUCCEEDED,
    INSUFFICIENT: "INSUFFICIENT",
    DIFFERENT_ANSWER: "CONFLICT",
    CONTRADICTED: "CONFLICT",
}


@dataclass
class RunOutcome:
    """一次复检运行的账。字段够回答"跑了吗 / 花了多少 / 为什么没跑完"。"""

    executed: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    no_candidate: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    verdicts: dict[str, CandidateVerdict] = field(default_factory=dict)
    usage: Usage = Usage()
    stopped_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "executed": list(self.executed),
            "reused": list(self.reused),
            "no_candidate": list(self.no_candidate),
            "skipped": list(self.skipped),
            "failed": list(self.failed),
            "calls": self.usage.calls,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "stopped_reason": self.stopped_reason,
        }


def ledger_path(book_dir: Path) -> Path:
    return Path(book_dir) / LEDGER_FILE_NAME


def load_ledger(path: Path) -> TaskLedger:
    """读账本。文件不存在 = 空账本（首次运行）；格式不对 = 报错，不当空处理。"""
    path = Path(path)
    if not path.is_file():
        return TaskLedger()
    payload = artifacts.read_json_artifact(path)
    rows = payload.get("records")
    if not isinstance(rows, list):
        raise ValueError(f"账本缺少 records 列表：{path}")
    return TaskLedger.from_dicts(rows)


def save_ledger(path: Path, ledger: TaskLedger) -> None:
    artifacts.write_json_atomic(
        path, artifacts.stamp_payload({"records": ledger.to_dicts()})
    )


def run_recheck(
    units: Sequence[RecheckUnit],
    *,
    ledger: TaskLedger,
    call_model: Callable[[RecheckUnit], Mapping[str, object]],
    budget: Budget | None = None,
    strategy_versions: Mapping[str, int] | None = None,
    verify: Callable[[str, int, int], bool] | None = None,
    allowed_evidence_ids: Callable[[RecheckUnit], Iterable[str]] | None = None,
    persist: Callable[[TaskLedger], None] | None = None,
    kind: str = "recheck",
    stage: str = "resolve",
    tokens_per_call: int = 0,
) -> RunOutcome:
    """跑一轮复检。

    ``call_model`` 返回的是**原始 payload**（模拟模型输出），一切判定都走门禁；
    ``persist`` 在每个单元结束后被调用一次，用于把账本落盘。
    """
    outcome = RunOutcome()
    plan = plan_recheck(
        units,
        ledger=ledger,
        budget=budget,
        stage=stage,
        strategy_versions=strategy_versions,
        tokens_per_call=tokens_per_call,
    )

    outcome.reused = [u.entity_id for u in plan.reuse]
    outcome.no_candidate = list(plan.no_candidate)
    outcome.skipped = [u.entity_id for u in plan.budget_skipped]
    record_plan_outcome(plan, ledger=ledger, kind=kind, stage=stage)
    _flush(persist, ledger)

    for unit in plan.pending:
        key = plan.task_keys[unit.entity_id]
        reservation = plan.reservations.get(unit.entity_id)
        try:
            payload = call_model(unit)
        except Exception as exc:  # noqa: BLE001 - 模型/网络失败都按"这次失败"记账
            if reservation is not None and budget is not None:
                spent = budget.settle(reservation, actual=Usage(calls=1))
                outcome.usage = outcome.usage.plus(spent)
            ledger.record(
                TaskRecord(task_key=key, kind=kind, stage=stage, status=FAILED,
                           group="recheck", calls=1, reason=f"{type(exc).__name__}: {exc}")
            )
            outcome.failed.append(unit.entity_id)
            _flush(persist, ledger)
            continue

        evidence_ids = set(allowed_evidence_ids(unit)) if allowed_evidence_ids else set()
        verdict = parse_recheck(
            payload,
            allowed_candidate_ids=set(unit.candidate_ids),
            allowed_evidence_ids=evidence_ids,
            quote_within_range=verify,
        )
        outcome.verdicts[unit.entity_id] = verdict
        status = _STATUS_BY_VERDICT.get(verdict.verdict, FAILED)
        if reservation is not None and budget is not None:
            spent = budget.settle(
                reservation,
                actual=Usage(calls=1, prompt_tokens=tokens_per_call),
            )
            outcome.usage = outcome.usage.plus(spent)
        ledger.record(
            TaskRecord(
                task_key=key, kind=kind, stage=stage, status=status, group="recheck",
                calls=1, prompt_tokens=tokens_per_call,
                apply_key=_apply_key(unit, verdict) if status != FAILED else None,
                result_ref=verdict.evidence_id or verdict.candidate_id,
                reason=verdict.reason,
            )
        )
        if status == FAILED:
            outcome.failed.append(unit.entity_id)
        else:
            outcome.executed.append(unit.entity_id)
        _flush(persist, ledger)

    if outcome.skipped:
        outcome.stopped_reason = f"{len(outcome.skipped)} 个单元因预算未执行"
    elif outcome.failed:
        outcome.stopped_reason = f"{len(outcome.failed)} 个单元失败"
    return outcome


def _apply_key(unit: RecheckUnit, verdict: CandidateVerdict) -> str:
    """应用键：同一实体 + 同一候选 + 同一结论只应用一次。

    用结论而不是时间戳：重跑得到同一结论时不该重复应用（历史会被追加两次），
    得到不同结论时才是一个新的应用。
    """
    return f"{unit.entity_id}|{verdict.candidate_id}|{verdict.verdict}"


def _flush(persist: Callable[[TaskLedger], None] | None, ledger: TaskLedger) -> None:
    if persist is not None:
        persist(ledger)


def summarize_run(outcome: RunOutcome, *, candidates_total: int = 0) -> RecheckSummary:
    summary = RecheckSummary(
        candidates_total=candidates_total,
        new_calls=len(outcome.executed),
        cache_reused=len(outcome.reused),
        no_candidate=len(outcome.no_candidate),
        failed=len(outcome.failed),
        budget_skipped=len(outcome.skipped),
        prompt_tokens=outcome.usage.prompt_tokens,
        completion_tokens=outcome.usage.completion_tokens,
    )
    return summary


def mark_stale_by_source(ledger: TaskLedger, *, source_changed: Iterable[str]) -> int:
    """来源变化时，把受影响任务标 ``STALE_INPUT``（未完成的才动）。"""
    changed = 0
    for key in set(source_changed):
        changed += ledger.mark_stale(key)
    return changed


def coverage_counts(
    outcome: RunOutcome,
    *,
    expected_candidate_ids: Sequence[str],
) -> "count_verdicts_result":
    verdicts = [v for v in outcome.verdicts.values()]
    return count_verdicts(verdicts, expected_candidate_ids=list(expected_candidate_ids))
