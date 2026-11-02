"""熔断接进 llm_client 的接线测试。

跳闸语义锁定：结构化账户级永久错误第一次命中即跳闸；跳闸后同实例所有
调用入口（chat / chat_with_retry / chat_stream_with_retry）零传输短路，
回包携带跳闸时的原 verdict（pipeline 靠 is_permanent 登记致命块供补跑跳过）；
请求级/瞬时错误不推进跳闸计数（breaker 本体用例见 test_breaker.py）。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from backend.config.settings import APIConfig, AppConfig
from backend.core.analyzer import NovelAnalyzer
from backend.core.breaker import OPEN
from backend.core.error_types import (
    Confidence,
    ErrorClass,
    ErrorScope,
    ErrorVerdict,
    LLMResult,
    SourceChannel,
    UsageStatus,
)
from backend.core.llm_client import LLMClient


def _account_verdict() -> ErrorVerdict:
    return ErrorVerdict(
        cls=ErrorClass.AUTH,
        confidence=Confidence.STRUCTURED,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        action="stop",
        scope=ErrorScope.ACCOUNT_GLOBAL,
    )


def _make_client() -> LLMClient:
    config = APIConfig(
        base_url="https://api.test.com",
        api_key="test-key",
        model="test-model",
        max_tokens=1000,
        timeout=10,
        json_mode="default",
        temperature=0.7,
        temperature_step=0.1,
        temperature_max_retries=3,
        backoff_max_retries=2,
        thinking_mode={},
    )
    return LLMClient(config)


def _fail_account(error="401 欠费") -> LLMResult:
    """契约：失败拿不到 usage → tokens=None + usage_status=UNKNOWN。"""
    return LLMResult(False, "", error, None, UsageStatus.UNKNOWN, verdict=_account_verdict())


def test_account_failure_trips_and_subsequent_funnel_calls_short_circuit() -> None:
    """第一次账户级失败即跳闸；之后 chat_with_retry 不再触达传输。"""
    import asyncio

    client = _make_client()
    transport = AsyncMock(return_value=_fail_account())
    client._chat_once = transport

    async def _run():
        first = await client.chat_with_retry([{"role": "user", "content": "hi"}])
        second = await client.chat_with_retry([{"role": "user", "content": "hi"}])
        return first, second

    first, second = asyncio.run(_run())
    # 两次漏斗调用合计只触达传输一次（第二次在漏斗闸门即被短路）
    assert transport.call_count == 1
    assert client._breaker.state == OPEN
    assert first.success is False
    assert "熔断" not in first.error            # 第一次是真实失败（401）
    assert second.success is False
    assert "熔断" in second.error               # 第二次是短路回包
    assert second.verdict is not None and second.verdict.is_permanent


def test_blocked_result_keeps_the_llmresult_invariants() -> None:
    """短路回包：tokens=None ↔ usage_status=UNKNOWN、attempts=0、verdict 原样。"""
    import asyncio

    client = _make_client()
    verdict = _account_verdict()
    transport = AsyncMock(return_value=_fail_account())
    client._chat_once = transport

    async def _run():
        await client.chat([{"role": "user", "content": "hi"}])   # 触发跳闸
        return await client.chat([{"role": "user", "content": "hi"}])  # 短路

    blocked = asyncio.run(_run())
    assert transport.call_count == 1
    assert blocked.tokens is None
    assert blocked.usage_status is UsageStatus.UNKNOWN
    assert blocked.call_stats["attempts"] == 0
    assert blocked.attempts == 0
    # __post_init__ 会重建 verdict 对象：相等即语义一致，不要求同一引用
    assert blocked.verdict == verdict


def test_request_local_failure_does_not_trip() -> None:
    """请求级错误（参数错）不该让后续调用短路。"""
    import asyncio

    client = _make_client()
    verdict = ErrorVerdict(
        cls=ErrorClass.BAD_REQUEST,
        confidence=Confidence.STRUCTURED,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        action="stop",
        scope=ErrorScope.REQUEST_LOCAL,
    )
    transport = AsyncMock(return_value=LLMResult(False, "", "400 bad request", None, UsageStatus.UNKNOWN, verdict=verdict))
    client._chat_once = transport

    async def _run():
        await client.chat([{"role": "user", "content": "hi"}])
        return await client.chat([{"role": "user", "content": "hi"}])

    asyncio.run(_run())
    assert client._breaker.state != OPEN
    assert transport.call_count == 2   # 未熔断：两次都真实触达


def test_stream_funnel_blocked_result_carries_verdict() -> None:
    """流式漏斗的短路回包同样带 verdict 与零成本 call_stats。"""
    import asyncio

    client = _make_client()
    verdict = _account_verdict()
    transport = AsyncMock(return_value=_fail_account())
    client._chat_once = transport

    async def _run():
        await client.chat([{"role": "user", "content": "hi"}])
        return await client.chat_stream_with_retry([{"role": "user", "content": "hi"}])

    blocked = asyncio.run(_run())
    assert blocked.success is False
    assert "熔断" in blocked.error
    assert blocked.verdict == verdict
    assert blocked.call_stats["attempts"] == 0


def test_prompt_limits_flow_from_config_to_builder() -> None:
    """4 个新配置字段必须真实生效（此前 4 项硬编码在 PromptBuilder 构造默认）。"""
    analysis = replace(
        AppConfig().analysis,
        max_relationships=7,
        max_themes=3,
        max_content_chars=12345,
        max_verified_facts_in_prompt=9,
    )
    config = replace(AppConfig(), analysis=analysis)
    analyzer = NovelAnalyzer(config)

    assert analyzer.prompt_builder.max_relationships == 7
    assert analyzer.prompt_builder.max_themes == 3
    assert analyzer.prompt_builder.max_content_chars == 12345
    assert analyzer.prompt_builder.max_verified_facts == 9
    # 未改动的字段仍走原配置链
    assert analyzer.prompt_builder.max_arcs == analysis.max_arcs_in_prompt


def test_prompt_limits_default_config_matches_old_hardcodes() -> None:
    """默认配置下行为不变：4 个新字段的默认值 = 原 prompt_builder 构造默认。"""
    analyzer = NovelAnalyzer(AppConfig())
    assert analyzer.prompt_builder.max_relationships == 15
    assert analyzer.prompt_builder.max_themes == 8
    assert analyzer.prompt_builder.max_content_chars == 30000
    assert analyzer.prompt_builder.max_verified_facts == 15
