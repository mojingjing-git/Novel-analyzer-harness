"""调用预算：在**发起请求之前**预留额度，失败也按实际扣，未开始可退回。

为什么要"预留"而不是"事后记账"（PLAN v2 §4 S7-5）：

并发跑十批时，若每批都是"先调用、后扣额度"，十批会同时通过检查——等到额度用尽，
钱已经花掉了。预算的作用是**阻止派发**，而不是事后告知你超了。所以：

1. 派发前 ``reserve()``，额度不足则抛 :class:`BudgetExceeded`，调用方据此记
   ``BUDGET_SKIPPED``（未完成态，不写成功标记，拿到预算可续跑）；
2. 失败的重试也**扣掉实际消耗**——失败也是花了钱的，"没成功所以不算"会让预算失真；
3. 没真正开始的预留可以 ``release()``；已经开始的一律 ``settle()``；
4. 重启后用量从任务记录重建，不靠内存里的计数器。

预算分组固定（顺序即执行顺序），默认值为 0 的组表示"本版不允许这类调用"——
这一点比"默认给个很大值"重要：像"全量重建 rolling"这种动作，本版是**明确不做**的，
用 0 表达比用注释表达可靠。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

#: 预算组（顺序即默认执行顺序）
ANALYSIS = "analysis"
ROLLING_NORMAL = "rolling_normal"
ROLLING_REBUILD = "rolling_rebuild"
VOLUME_SUMMARY = "volume_summary"
RECONCILIATION = "reconciliation"
RECHECK = "recheck"
SEMANTIC_REVIEW = "semantic_review"
STYLE = "style"
FINAL_REPORT = "final_report"

BUDGET_GROUPS: tuple[str, ...] = (
    ANALYSIS,
    ROLLING_NORMAL,
    ROLLING_REBUILD,
    VOLUME_SUMMARY,
    RECONCILIATION,
    RECHECK,
    SEMANTIC_REVIEW,
    STYLE,
    FINAL_REPORT,
)

#: 本版默认上限。0 = 明确不做（例如全量重建 rolling、额外的语义复核调用）
DEFAULT_CAPS: dict[str, "BudgetCap"] = {}


class BudgetExceeded(RuntimeError):
    """预留失败：该组额度不足。调用方应记 ``BUDGET_SKIPPED`` 并保留可续跑状态。"""


@dataclass(frozen=True)
class BudgetCap:
    max_calls: int = 0
    max_tokens: int = 0

    def allows(self, used_calls: int, used_tokens: int) -> bool:
        return used_calls < self.max_calls and used_tokens < self.max_tokens


@dataclass(frozen=True)
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def plus(self, other: "Usage") -> "Usage":
        return Usage(
            calls=self.calls + other.calls,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )


@dataclass(frozen=True)
class Reservation:
    """一次已预留的额度。``reserved`` 是上限，``settle`` 时按实际结算。"""

    group: str
    calls: int
    tokens: int
    reserved_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "reserved_tokens", self.tokens)


@dataclass
class Budget:
    """分组预算账本。线程安全靠调用方串行化（本模块不做锁，避免持有锁时 await）。"""

    caps: Mapping[str, BudgetCap] = field(default_factory=dict)
    used: Mapping[str, Usage] = field(default_factory=dict)
    _open: list[Reservation] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.caps = {group: self.caps.get(group, BudgetCap()) for group in BUDGET_GROUPS} | dict(self.caps)
        self.used = {group: self.used.get(group, Usage()) for group in BUDGET_GROUPS} | dict(self.used)

    # -- 预留 / 结算 --------------------------------------------------------

    def reserve(self, group: str, *, calls: int = 1, tokens: int = 0) -> Reservation:
        if group not in BUDGET_GROUPS:
            # 未知组不能静默放行：那样一个拼错的组名就成了"无限额度"
            raise KeyError(f"未知预算组：{group}")
        if calls < 0 or tokens < 0:
            raise ValueError("预留额度不能为负")
        cap = self.caps[group]
        current = self.used[group]
        pending = self._pending(group)
        projected_calls = current.calls + pending.calls + calls
        projected_tokens = current.tokens + pending.tokens + tokens
        if projected_calls > cap.max_calls or projected_tokens > cap.max_tokens:
            raise BudgetExceeded(
                f"{group} 预算不足：已用 {current.calls} 次/{current.tokens} token，"
                f"在途 {pending.calls} 次/{pending.tokens} token，"
                f"本次请求 {calls} 次/{tokens} token，上限 {cap.max_calls}/{cap.max_tokens}"
            )
        reservation = Reservation(group=group, calls=calls, tokens=tokens)
        self._open.append(reservation)
        return reservation

    def release(self, reservation: Reservation) -> None:
        """退回**未开始**的预留。已经开始的一律用 :meth:`settle`，否则会掩盖真实花费。"""
        try:
            self._open.remove(reservation)
        except ValueError:
            return

    def settle(self, reservation: Reservation, *, actual: Usage | None = None) -> Usage:
        """按实际用量结算（失败也要结算：失败同样花钱）。"""
        try:
            self._open.remove(reservation)
        except ValueError:
            pass
        spent = actual if actual is not None else Usage(
            calls=reservation.calls,
            prompt_tokens=reservation.tokens,
            completion_tokens=0,
        )
        self.used[reservation.group] = self.used[reservation.group].plus(spent)
        return spent

    def _pending(self, group: str) -> Usage:
        total = Usage()
        for reservation in self._open:
            if reservation.group == group:
                total = total.plus(
                    Usage(calls=reservation.calls, prompt_tokens=reservation.tokens, completion_tokens=0)
                )
        return total

    # -- 查询 ---------------------------------------------------------------

    def remaining(self, group: str) -> Usage:
        cap = self.caps[group]
        used = self.used[group].plus(self._pending(group))
        return Usage(
            calls=max(0, cap.max_calls - used.calls),
            prompt_tokens=max(0, cap.max_tokens - used.tokens),
            completion_tokens=0,
        )

    def exhausted(self, group: str) -> bool:
        cap = self.caps[group]
        used = self.used[group].plus(self._pending(group))
        return used.calls >= cap.max_calls or used.tokens >= cap.max_tokens

    def outstanding(self) -> tuple[Reservation, ...]:
        return tuple(self._open)

    def snapshot(self) -> dict:
        return {
            "caps": {g: {"max_calls": c.max_calls, "max_tokens": c.max_tokens} for g, c in self.caps.items()},
            "used": {
                g: {
                    "calls": u.calls,
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                }
                for g, u in self.used.items()
            },
        }


def default_budget(overrides: Mapping[str, BudgetCap] | None = None) -> Budget:
    """本版默认预算。

    ``rolling_rebuild`` 与 ``semantic_review`` 默认 0 次：这两类动作在本版**明确不做**，
    用 0 表达比用注释表达可靠——真被调用时会当场抛 ``BudgetExceeded``，而不是悄悄多花钱。
    """
    caps: dict[str, BudgetCap] = {
        ANALYSIS: BudgetCap(max_calls=100_000, max_tokens=10**9),
        ROLLING_NORMAL: BudgetCap(max_calls=100_000, max_tokens=10**9),
        ROLLING_REBUILD: BudgetCap(max_calls=0, max_tokens=0),
        VOLUME_SUMMARY: BudgetCap(max_calls=10_000, max_tokens=10**8),
        RECONCILIATION: BudgetCap(max_calls=10_000, max_tokens=10**8),
        RECHECK: BudgetCap(max_calls=10_000, max_tokens=10**8),
        SEMANTIC_REVIEW: BudgetCap(max_calls=0, max_tokens=0),
        STYLE: BudgetCap(max_calls=100, max_tokens=10**7),
        FINAL_REPORT: BudgetCap(max_calls=100, max_tokens=10**7),
    }
    if overrides:
        caps.update(overrides)
    return Budget(caps=caps)


def rebuild_usage(usages: Iterable[Mapping[str, object]]) -> dict[str, Usage]:
    """从任务记录重建用量（重启后不依赖内存计数器）。"""
    totals: dict[str, Usage] = {}
    for row in usages:
        group = str(row.get("group") or "")
        if group not in BUDGET_GROUPS:
            continue
        totals[group] = totals.get(group, Usage()).plus(
            Usage(
                calls=int(row.get("calls") or 0),
                prompt_tokens=int(row.get("prompt_tokens") or 0),
                completion_tokens=int(row.get("completion_tokens") or 0),
            )
        )
    return totals
