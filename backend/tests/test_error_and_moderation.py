"""错误类型、错误分类表（error_taxonomy）、内容审核（moderation）与熔断器（Breaker）测试。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from backend.config.settings import APIConfig
from backend.core.breaker import CLOSED, OPEN, Breaker
from backend.core.error_taxonomy import ErrorClass, classify_exception, classify_message
from backend.core.error_types import (
    Confidence,
    ErrorScope,
    ErrorVerdict,
    LLMResult,
    SourceChannel,
    UsageStatus,
)
from backend.core.llm_client import LLMClient
from backend.core.moderation import (
    MODERATION_MARKER,
    is_moderation_code,
    is_moderation_error,
    is_moderation_message,
    mark_moderation,
)


def test_error_types_roundtrip_and_enums():
    assert len(ErrorClass) == 11
    verdict = ErrorVerdict(
        cls=ErrorClass.RATE_LIMIT,
        confidence=Confidence.STRUCTURED,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        provider_code="1027",
        http_status=429,
        action="retry_after",
        scope=ErrorScope.ACCOUNT_GLOBAL,
        retry_after=1.5,
        message="限流",
    )
    res = LLMResult(
        success=False, content="", error="boom", tokens=(11, 22),
        usage_status=UsageStatus.KNOWN, call_stats={"attempts": 2, "failed_tokens": 3},
        verdict=verdict, attempts=2,
    )
    assert LLMResult.from_dict(res.to_dict()) == res
    unknown_res = LLMResult(success=False, content="", error="err", tokens=None)
    assert unknown_res.usage_status is UsageStatus.UNKNOWN


def test_error_taxonomy_classification():
    assert classify_message(f"{MODERATION_MARKER} output new_sensitive (1027)").cls is ErrorClass.MODERATION_OUTPUT
    assert classify_message("UnprocessableEntityError: 422 input new_sensitive detected").cls is ErrorClass.MODERATION_INPUT
    assert classify_message("API错误 (HTTP 429): {'error': {'code': 'insufficient_quota'}}").cls is ErrorClass.BILLING
    assert classify_message("API错误 (HTTP 429): {'error': {'type': 'rate_limit_error'}}").cls is ErrorClass.RATE_LIMIT
    assert classify_message("{'code': 'Throttling.RequestsThrottled', 'message': 'insufficient_quota'}").cls is ErrorClass.RATE_LIMIT
    assert classify_message("BadRequestError: 400 'you must provide a model parameter'").cls is ErrorClass.BAD_REQUEST
    assert classify_message("Range of input length should be [1, 131072]").cls is ErrorClass.CONTEXT_LENGTH
    assert classify_message("认证失败，请检查API Key").cls is ErrorClass.AUTH
    assert classify_message("请求硬超时 (timeout=230s)").cls is ErrorClass.TIMEOUT
    assert classify_message("服务器繁忙，请稍后再试").cls is ErrorClass.SERVER

    exc = SimpleNamespace(code="insufficient_quota", status_code=429, body={"error": {"code": "insufficient_quota"}})
    v = classify_exception(exc)
    assert v.cls is ErrorClass.BILLING and v.confidence == "structured"


def test_moderation_helpers():
    assert is_moderation_code("1026") and is_moderation_code("content_policy_violation")
    assert not is_moderation_code("1008") and not is_moderation_code(None)
    assert is_moderation_message("内容不符合安全规范")
    assert is_moderation_message("This request violates our safety policy")
    assert not is_moderation_message("服务器繁忙，请稍后再试")

    marked = mark_moderation("API错误: bad")
    assert is_moderation_error(marked) and mark_moderation(marked) == marked


async def test_moderation_short_circuit_in_llm_client():
    cfg = APIConfig(
        base_url="https://api.test.com", api_key="k", model="m",
        temperature_max_retries=2, backoff_max_retries=1,
    )
    client = LLMClient(cfg)
    client._sleep = AsyncMock()
    err = mark_moderation("API错误 (HTTP 400): content policy")
    client.chat = AsyncMock(side_effect=[LLMResult(False, "", err), LLMResult(False, "", err)])

    res = await client.chat_with_retry([{"role": "user", "content": "hi"}])
    assert not res.success
    assert "[MODERATION]" in res.error
    assert res.call_stats["attempts"] == 2


def test_breaker_state_machine():
    def _v(cls=ErrorClass.AUTH, scope=ErrorScope.ACCOUNT_GLOBAL, confidence=Confidence.STRUCTURED, action="stop"):
        return ErrorVerdict(cls=cls, confidence=confidence, source_channel=SourceChannel.SDK_ATTRIBUTE, action=action, scope=scope)

    breaker = Breaker()
    assert breaker.record_failure(_v(scope=ErrorScope.REQUEST_LOCAL)) is False
    assert breaker.record_failure(_v(cls=ErrorClass.RATE_LIMIT, scope=ErrorScope.TRANSIENT)) is False
    assert breaker.record_failure(_v(confidence=Confidence.TEXT)) is False
    assert breaker.state == CLOSED

    assert breaker.record_failure(_v(), detail="欠费") is True
    assert breaker.state == OPEN and not breaker.allows()
    breaker.record_success()
    assert breaker.state == OPEN
    breaker.reset(reason="换 key")
    assert breaker.state == CLOSED and breaker.allows()
