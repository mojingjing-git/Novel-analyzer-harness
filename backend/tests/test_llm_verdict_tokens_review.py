"""外部复审（2026-10-30）第 1、2 条的回归用例。

第 1 条：无 usage 的失败不得继承上一次尝试的 tokens（T45：已知与未知必须分列）。
第 2 条：完整判定不得被压成三元组再重建（provider_code / http_status / 来源通道会丢），
        typed ``retry_after`` 必须被消费，而不是从显示串里正则捞。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import APIConfig
from backend.core.error_taxonomy import ErrorClass
from backend.core.error_types import (
    Confidence,
    ErrorScope,
    ErrorVerdict,
    LLMResult,
    SourceChannel,
    UsageStatus,
)
from backend.core.llm_client import LLMClient, _verdict_from_exception


def _make_cfg(**kw):
    defaults = dict(
        base_url="https://api.test.com", api_key="k", model="m",
        max_tokens=100, timeout=5,
        temperature_max_retries=2, backoff_max_retries=0,
    )
    defaults.update(kw)
    return APIConfig(**defaults)


def _sdk_error(msg, status, body, cls=None):
    url = "https://api.test.com/v1/chat/completions"
    resp = httpx.Response(status, request=httpx.Request("POST", url), json=body)
    return (cls or openai.APIStatusError)(msg, response=resp, body=body)


# ---------------------------------------------------------------------------
# 第 1 条：用量口径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_usage_is_not_filled_by_previous_attempt():
    """复现：第一次校验失败（报过 (11,3)），第二次断连（无 usage）→ 最终不得报 (11,3)/KNOWN。"""
    client = LLMClient(_make_cfg(temperature_max_retries=2, backoff_max_retries=0))
    client._sleep = AsyncMock()
    client.chat = AsyncMock(side_effect=[
        LLMResult(success=True, content="不是JSON", error="", tokens=(11, 3)),
        LLMResult(success=False, content="", error="连接中断", tokens=None),
    ])

    result = await client.chat_with_retry(
        [{"role": "user", "content": "x"}],
        validate_response=lambda text: (False, "格式不对"),
    )

    assert result.success is False
    # 本次尝试没有 usage → 必须是"未知"，不能拿上一次的数值冒充
    assert result.tokens is None
    assert result.usage_status is UsageStatus.UNKNOWN
    # 但已经花掉的钱要单独记账，不能凭空消失
    assert result.prior_known_tokens == (11, 3)


@pytest.mark.asyncio
async def test_known_usage_of_final_attempt_is_reported_as_known():
    """反向：最后一次尝试确实报了 usage，就该如实标 KNOWN。"""
    client = LLMClient(_make_cfg(temperature_max_retries=2, backoff_max_retries=0))
    client._sleep = AsyncMock()
    client.chat = AsyncMock(side_effect=[
        LLMResult(success=True, content="不是JSON", error="", tokens=(5, 1)),
        LLMResult(success=True, content='{"ok": 1}', error="", tokens=(7, 2)),
    ])

    result = await client.chat_with_retry(
        [{"role": "user", "content": "x"}],
        validate_response=lambda text: ('"ok"' in text, "格式不对"),
    )
    assert result.success is True
    assert result.tokens == (7, 2)
    assert result.usage_status is UsageStatus.KNOWN
    assert result.prior_known_tokens == (5, 1)  # 上一次的消耗仍然记着


# ---------------------------------------------------------------------------
# 第 2 条：判定保真
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_verdict_survives_retry_layer():
    """结构化 401：provider_code / http_status / 来源通道都必须活到结果对象上。"""
    exc = _sdk_error("Unauthorized", 401, {"error": {"code": "invalid_api_key"}},
                     cls=openai.AuthenticationError)
    client = LLMClient(_make_cfg(temperature_max_retries=3, backoff_max_retries=2))
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=exc)
    client.client = fake
    client._sleep = AsyncMock()

    result = await client.chat_with_retry([{"role": "user", "content": "x"}])

    verdict = result.verdict
    assert verdict is not None
    assert verdict.confidence is Confidence.STRUCTURED     # 曾被降级成 NONE
    # 来源通道必须与"异常现场分类"的结论一致：只要不是被降成 TEXT_FALLBACK 就说明
    # 判定没有经过"压成三元组再重建"的往返
    assert verdict.source_channel is _verdict_from_exception(exc, "").source_channel
    assert verdict.source_channel is SourceChannel.ERROR_ENVELOPE
    assert verdict.http_status == 401
    assert verdict.provider_code == "invalid_api_key"
    assert result.attempts == 1  # 结构化永久错误首次即停


def test_typed_retry_after_is_consumed_not_parsed_from_text():
    """typed retry_after=7 且显示串不含标记时，等待必须是 7 秒，不是兜底的 30 秒。"""
    client = LLMClient(_make_cfg())
    client._sleep = AsyncMock()
    verdict = ErrorVerdict(
        cls=ErrorClass.RATE_LIMIT,
        confidence=Confidence.STRUCTURED,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        action="retry_after",
        scope=ErrorScope.ACCOUNT_GLOBAL,
        retry_after=7.0,
        message="限流了",  # 刻意不含 "Retry-After:"
    )

    decision = client._decide_retry(
        "限流了",
        moderation_hits=0,
        verdict=verdict,
        attempt=0,
        total_attempts=1,
        temperature=0.1,
        messages_length=10,
        phase="temperature",
        moderation_log="",
    )

    assert decision.action == "continue"
    assert decision.wait_time == 7.0


def test_text_retry_after_still_works_as_fallback():
    """文本通道没有 typed 值时才回落到字符串解析——能力不能被删掉。"""
    client = LLMClient(_make_cfg())
    client._sleep = AsyncMock()
    verdict = ErrorVerdict(
        cls=ErrorClass.RATE_LIMIT,
        confidence=Confidence.TEXT,
        source_channel=SourceChannel.TEXT_FALLBACK,
        action="backoff",
        retry_after=None,
        message="HTTP 429, Retry-After:12",
    )

    decision = client._decide_retry(
        "HTTP 429, Retry-After:12",
        moderation_hits=0,
        verdict=verdict,
        attempt=0,
        total_attempts=1,
        temperature=0.1,
        messages_length=10,
        phase="temperature",
        moderation_log="",
    )
    assert decision.wait_time == 12.0
