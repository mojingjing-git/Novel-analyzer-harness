"""熔断：只有"整个账号都不行"的错误才停派发（PLAN v2 §4 S8 C-07）。

三条边界必须清楚，否则熔断会变成另一个误判源：

1. **只有账户级 + 永久性错误才跳闸**。单次请求参数错（``REQUEST_LOCAL``）不该让整本书停工；
   限流（``TRANSIENT``）更不该——它本来就该重试。
2. **判据只认结构化判定**（``ErrorVerdict``：置信 ``STRUCTURED`` 且动作为 ``stop``）。
   显示字符串一律不参与：C-01 的教训就是"文本里有个 401 就永久停机"。
3. **跳闸是粘性的**：恢复需要显式 ``reset()``（通常是用户换了 key/模型）。自动半开探测
   在这里不划算——账户级错误的每一次探测都可能又是一次计费失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field

CLOSED = "CLOSED"
OPEN = "OPEN"


@dataclass
class Breaker:
    """账户级熔断器。

    ``open_after`` 是"连续多少次账户级永久失败后跳闸"，默认 1：账户级永久错误（欠费、
    认证失败）没有"再试几次就好了"的可能，第一次就停，比失败十次更省。
    """

    open_after: int = 1
    state: str = CLOSED
    consecutive_account_failures: int = 0
    reason: str = ""
    #: 累计观测（不参与判定，供诊断）
    observed_failures: int = 0
    observed_successes: int = 0
    _history: list[str] = field(default_factory=list, repr=False)

    # -- 判定 ---------------------------------------------------------------

    def allows(self) -> bool:
        return self.state == CLOSED

    def record_success(self) -> None:
        self.observed_successes += 1
        self.consecutive_account_failures = 0

    def record_failure(self, verdict: object, *, detail: str = "") -> bool:
        """记录一次失败，返回**本次是否导致跳闸**。

        只接受能回答"是否永久 + 是否账户级"的判定对象；裸字符串、``None``、异常对象
        一律按"不可判定"处理——不跳闸，只计数。
        """
        self.observed_failures += 1
        if not _is_account_permanent(verdict):
            # 请求级/瞬时错误：计入观测，但不推进跳闸计数，也不清零（它不是成功）
            self._history.append(f"ignored:{detail or type(verdict).__name__}")
            return False

        self.consecutive_account_failures += 1
        self._history.append(f"account:{detail or type(verdict).__name__}")
        if self.state == CLOSED and self.consecutive_account_failures >= self.open_after:
            self.state = OPEN
            self.reason = detail or "账户级永久错误"
            return True
        return False

    def reset(self, *, reason: str = "manual") -> None:
        self.state = CLOSED
        self.consecutive_account_failures = 0
        self.reason = ""
        self._history.append(f"reset:{reason}")

    def history(self) -> tuple[str, ...]:
        return tuple(self._history)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "consecutive_account_failures": self.consecutive_account_failures,
            "observed_failures": self.observed_failures,
            "observed_successes": self.observed_successes,
            "open_after": self.open_after,
        }


def _is_account_permanent(verdict: object) -> bool:
    """结构化判定：``is_permanent()`` 为真且作用域是账户级。

    刻意不做字符串嗅探，也不因为对象"看起来像有 status_code 属性"就升级判定。
    """
    if verdict is None:
        return False
    # ErrorVerdict.is_permanent 是 **property**（不是方法）；这里两种形态都接受，
    # 免得"属性一变方法就静默不跳闸"这种最难查的退化。
    permanent = getattr(verdict, "is_permanent", None)
    if callable(permanent):
        permanent = permanent()
    if not permanent:
        return False
    scope = getattr(verdict, "scope", None)
    if scope is None:
        return False
    value = getattr(scope, "value", scope)
    return str(value).upper() == "ACCOUNT_GLOBAL"
