"""任务账本：把"这一步跑过了吗"变成可判定、可重放、可复用的记录。

为什么必须有它（PLAN v2 §4 S7-1/2/4/9）：

旧实现用"业务状态"当缓存——例如"这条伏笔已回收"就等于"复检做过了"。这有两个致命后果：
一是**状态会被别的路径改写**（调和、人工、休眠判定都能改），缓存命中与事实真假混在一起；
二是**无法区分"没跑"与"跑了但没结论"**，于是"证据不足"每次都要重跑，或者更糟——被当成完成。

本模块只做一件事：按 ``task_key``（输入指纹）记录任务**跑没跑、结论是什么、花了多少**。
业务语义不进这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

from .fingerprint import canonical_json, task_key as _task_key

# 任务状态（PLAN v2 §2.4）
PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
BUDGET_SKIPPED = "BUDGET_SKIPPED"
STALE_INPUT = "STALE_INPUT"
#: 没有可回收候选：这是一个**有效结论**（"查过了，没有东西可判"），不是跳过
NO_CANDIDATE = "NO_CANDIDATE"

#: 可复用的终态。注意包含 INSUFFICIENT/CONFLICT 的语义：
#: "证据不足"是结论，重跑同输入只会得到同一结论，还会再花一次钱。
REUSABLE = (SUCCEEDED, NO_CANDIDATE, "INSUFFICIENT", "CONFLICT", "INVALID_TERMINAL")

#: 未完成任务：只保存，不写成功标记；拿到预算或重跑后可以继续
UNFINISHED = (PENDING, RUNNING, FAILED, CANCELLED, BUDGET_SKIPPED, STALE_INPUT)


@dataclass(frozen=True)
class TaskRecord:
    """一次任务执行的记录。字段刻意少：够判定"能不能复用"就行。"""

    task_key: str
    kind: str
    stage: str
    status: str
    #: 输入摘要（只存"哪些东西进了这个任务"，不存内容本身）
    inputs: Mapping[str, object] = field(default_factory=dict)
    attempt: int = 1
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 应用键：同一份结论只能被应用一次（重复应用会让历史被追加两次）
    apply_key: str | None = None
    #: 结论指向（result_id / candidate_id / 产物路径），本模块不解释它
    result_ref: str | None = None
    reason: str = ""

    @property
    def reusable(self) -> bool:
        return self.status in REUSABLE

    @property
    def finished(self) -> bool:
        return self.status in REUSABLE

    def to_dict(self) -> dict:
        return {
            "task_key": self.task_key,
            "kind": self.kind,
            "stage": self.stage,
            "status": self.status,
            "inputs": dict(self.inputs),
            "attempt": self.attempt,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "apply_key": self.apply_key,
            "result_ref": self.result_ref,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "TaskRecord":
        return cls(
            task_key=str(data["task_key"]),
            kind=str(data["kind"]),
            stage=str(data["stage"]),
            status=str(data["status"]),
            inputs=dict(data.get("inputs") or {}),
            attempt=int(data.get("attempt") or 1),
            calls=int(data.get("calls") or 0),
            prompt_tokens=int(data.get("prompt_tokens") or 0),
            completion_tokens=int(data.get("completion_tokens") or 0),
            apply_key=data.get("apply_key") if isinstance(data.get("apply_key"), str) else None,
            result_ref=data.get("result_ref") if isinstance(data.get("result_ref"), str) else None,
            reason=str(data.get("reason") or ""),
        )


def make_task_key(*, kind: str, stage: str, inputs: Mapping[str, object], strategy_versions: Mapping[str, int]) -> str:
    """任务身份 = 阶段 + 输入摘要 + 策略版本。

    ``inputs`` 必须包含**所有会影响结论的东西**：用到的上游结果 ID、摘要/原文范围与哈希、
    定位/窗口/Prompt/模型版本。只哈希"某一处引用"是不够的——一条核对任务的输入是一整份
    材料，不是其中一行字。
    """
    canonical_json(inputs)  # 提前校验可序列化：算到一半才炸更难查
    return _task_key(kind=kind, stage=stage, inputs=inputs, strategy_versions=strategy_versions)


class TaskLedger:
    """任务记录集合。只追加、按 ``task_key`` 索引、应用键去重。"""

    def __init__(self, records: Iterable[TaskRecord] = ()) -> None:
        self._records: list[TaskRecord] = list(records)
        self._by_key: dict[str, list[TaskRecord]] = {}
        self._applied: set[str] = set()
        for record in self._records:
            self._index(record)

    def _index(self, record: TaskRecord) -> None:
        self._by_key.setdefault(record.task_key, []).append(record)
        if record.apply_key and record.status in REUSABLE:
            self._applied.add(record.apply_key)

    def __len__(self) -> int:
        return len(self._records)

    def records(self) -> tuple[TaskRecord, ...]:
        return tuple(self._records)

    def history(self, task_key: str) -> tuple[TaskRecord, ...]:
        return tuple(self._by_key.get(task_key, ()))

    def latest(self, task_key: str) -> TaskRecord | None:
        history = self._by_key.get(task_key)
        return history[-1] if history else None

    def reusable(self, task_key: str) -> TaskRecord | None:
        """可复用的记录：**优先取最近一次**，但只有终态才算。

        一次失败的尝试不能掩盖之前成功的那次——重跑失败不该让缓存失效。
        反过来，输入变了会得到新的 ``task_key``，所以这里不需要再比输入。
        """
        for record in reversed(self._by_key.get(task_key, ())):
            if record.reusable:
                return record
        return None

    def record(self, record: TaskRecord) -> bool:
        """追加一条记录。``apply_key`` 已应用的结论拒绝重复应用，返回 False。"""
        if record.apply_key and record.status in REUSABLE and record.apply_key in self._applied:
            return False
        self._records.append(record)
        self._index(record)
        return True

    def mark_stale(self, task_key: str, *, reason: str = "SOURCE_CHANGED") -> int:
        """把某任务键下的未完成记录标成 ``STALE_INPUT``，返回改了条数。

        只动未完成的：已经成功的结论要留在历史里（"当时是这么判的"不能被事后改写）。
        """
        changed = 0
        for index, record in enumerate(self._records):
            if record.task_key != task_key:
                continue
            if record.status in UNFINISHED and record.status != STALE_INPUT:
                self._records[index] = replace(record, status=STALE_INPUT, reason=reason)
                changed += 1
        if changed:
            self._rebuild_index()
        return changed

    def _rebuild_index(self) -> None:
        self._by_key.clear()
        self._applied.clear()
        for record in self._records:
            self._index(record)

    def to_dicts(self) -> list[dict]:
        return [record.to_dict() for record in self._records]

    @classmethod
    def from_dicts(cls, rows: Iterable[Mapping[str, object]]) -> "TaskLedger":
        return cls(TaskRecord.from_dict(row) for row in rows)

    # -- 统计 ---------------------------------------------------------------

    def totals(self) -> dict:
        """按状态计数 + 累计用量。用量是"花掉的钱"，不区分成败。"""
        by_status: dict[str, int] = {}
        prompt = completion = calls = 0
        for record in self._records:
            by_status[record.status] = by_status.get(record.status, 0) + 1
            prompt += record.prompt_tokens
            completion += record.completion_tokens
            calls += record.calls
        return {
            "records": len(self._records),
            "by_status": dict(sorted(by_status.items())),
            "calls": calls,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
        }
