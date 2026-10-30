"""S8 熔断（PLAN v2 §4 S8 C-07）：只有账户级永久错误才停派发。"""

from __future__ import annotations

from dataclasses import dataclass

from backend.vnext.breaker import CLOSED, OPEN, Breaker
from backend.core.error_types import (
    Confidence,
    ErrorClass,
    ErrorScope,
    ErrorVerdict,
    SourceChannel,
)


def _verdict(cls=ErrorClass.AUTH, scope=ErrorScope.ACCOUNT_GLOBAL,
             confidence=Confidence.STRUCTURED, action="stop") -> ErrorVerdict:
    return ErrorVerdict(
        cls=cls,
        confidence=confidence,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        action=action,
        scope=scope,
    )


def test_account_permanent_error_opens_immediately() -> None:
    breaker = Breaker()
    assert breaker.record_failure(_verdict(), detail="欠费") is True
    assert breaker.state == OPEN
    assert breaker.allows() is False


def test_request_local_error_does_not_open() -> None:
    """单次请求参数错不该让整本书停工。"""
    breaker = Breaker()
    assert breaker.record_failure(_verdict(scope=ErrorScope.REQUEST_LOCAL)) is False
    assert breaker.state == CLOSED
    assert breaker.allows() is True


def test_transient_rate_limit_does_not_open() -> None:
    breaker = Breaker()
    for _ in range(10):
        assert breaker.record_failure(
            _verdict(cls=ErrorClass.RATE_LIMIT, scope=ErrorScope.TRANSIENT)
        ) is False
    assert breaker.state == CLOSED


def test_text_confidence_never_opens() -> None:
    """C-01 的教训：文本通道判出来的"永久错误"不能停派发。"""
    breaker = Breaker()
    for _ in range(5):
        assert breaker.record_failure(
            _verdict(confidence=Confidence.TEXT, scope=ErrorScope.ACCOUNT_GLOBAL)
        ) is False
    assert breaker.state == CLOSED
    assert breaker.observed_failures == 5


def test_plain_string_is_not_a_verdict() -> None:
    breaker = Breaker()
    assert breaker.record_failure("401 Unauthorized") is False
    assert breaker.state == CLOSED


def test_open_is_sticky_until_reset() -> None:
    breaker = Breaker()
    breaker.record_failure(_verdict())
    breaker.record_success()          # 一次成功不应自动半开
    assert breaker.state == OPEN
    breaker.reset(reason="换 key")
    assert breaker.state == CLOSED
    assert breaker.allows() is True


def test_threshold_greater_than_one_counts_consecutively() -> None:
    breaker = Breaker(open_after=2)
    assert breaker.record_failure(_verdict()) is False
    assert breaker.state == CLOSED
    assert breaker.record_failure(_verdict()) is True
    assert breaker.state == OPEN


def test_success_resets_the_consecutive_counter() -> None:
    breaker = Breaker(open_after=2)
    breaker.record_failure(_verdict())
    breaker.record_success()
    assert breaker.record_failure(_verdict()) is False   # 重新计数
    assert breaker.state == CLOSED


def test_none_scope_is_not_account_global() -> None:
    breaker = Breaker()
    verdict = _verdict()
    object.__setattr__(verdict, "scope", None)
    assert breaker.record_failure(verdict) is False


def test_history_and_snapshot_are_inspectable() -> None:
    breaker = Breaker()
    breaker.record_failure(_verdict(), detail="欠费")
    snapshot = breaker.as_dict()
    assert snapshot["state"] == OPEN
    assert snapshot["reason"] == "欠费"
    assert any(item.startswith("account:") for item in breaker.history())
