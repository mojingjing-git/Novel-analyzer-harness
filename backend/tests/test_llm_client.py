"""LLMClient 非流式/流式调用、重试退避链、Anthropic 协议、思考探测与流式进度回调测试。"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.config.settings import APIConfig
from backend.core.error_types import LLMResult, UsageStatus
from backend.core.llm_client import LLMClient, detect_provider
from backend.core.llm_probe import _get_reasoning_tokens, probe_thinking_params
from backend.core.llm_stream import (
    StreamChunk,
    estimate_token_count,
    parse_anthropic_response,
    parse_openai_stream_event,
)
from backend.core.stream_progress import make_stream_progress_callback
from backend.progress_hub import ProgressHub


def make_config(**kwargs) -> APIConfig:
    defaults = dict(
        base_url="https://api.test.com",
        api_key="test-key",
        model="test-model",
        max_tokens=1000,
        timeout=10,
        max_retries=3,
        json_mode="default",
        temperature=0.7,
        temperature_step=0.1,
        temperature_max_retries=3,
        backoff_max_retries=2,
        thinking_mode={},
        provider="openai",
        streaming_enabled=True,
    )
    defaults.update(kwargs)
    return APIConfig(**defaults)


def ok_result(content="", tokens=(10, 20)) -> LLMResult:
    return LLMResult(True, content, "", tokens, UsageStatus.KNOWN)


def fail_result(error: str, tokens=None) -> LLMResult:
    usage = UsageStatus.KNOWN if tokens is not None else UsageStatus.UNKNOWN
    return LLMResult(False, "", error, tokens, usage)


# ---------------------------------------------------------------------------
# 非流式重试链与取消
# ---------------------------------------------------------------------------

async def test_nonstream_retry_annealing_and_validation():
    client = LLMClient(make_config(temperature_max_retries=3, backoff_max_retries=0))
    client._sleep = AsyncMock()
    temps = []

    async def mock_chat(messages, temperature=0.1, max_tokens=None):
        temps.append(temperature)
        if len(temps) < 3:
            return ok_result("invalid json", (5, 10))
        return ok_result('{"valid":true}', (5, 10))

    client.chat = mock_chat
    res = await client.chat_with_retry(
        [{"role": "user", "content": "hi"}],
        validate_response=lambda c: (True, "") if c.startswith("{") else (False, "bad json"),
        retry_messages_builder=lambda err, msgs: msgs + [{"role": "assistant", "content": "retry"}],
    )
    assert res.success
    assert len(temps) == 3
    assert temps[0] == pytest.approx(0.7) and temps[1] == pytest.approx(0.6)
    assert res.call_stats["attempts"] == 3
    assert res.call_stats["failed_tokens"] == 30


async def test_429_extended_backoff_respects_retry_after():
    client = LLMClient(make_config(temperature=0.5, temperature_step=0.25, temperature_max_retries=2, backoff_max_retries=1))
    seq = [
        fail_result("API错误 (HTTP 429): rate limited [Retry-After:5]"),
        fail_result("API错误 (HTTP 429): rate limited [Retry-After:5]"),
        fail_result("API错误 (HTTP 429): rate limited [Retry-After:5]"),
        fail_result("API错误 (HTTP 429): rate limited [Retry-After:5]"),
        ok_result("ok", (1, 1)),
    ]
    client.chat = AsyncMock(side_effect=seq)
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    client._sleep = fake_sleep
    with patch("asyncio.sleep", new=fake_sleep):
        res = await client.chat_with_retry([{"role": "user", "content": "hi"}])
    assert res.success
    assert res.call_stats["attempts"] == 5
    assert 5 in sleeps


async def test_chat_cancelled_by_stop_and_external_cancel():
    client = LLMClient(make_config(timeout=10))
    started = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    client.client = MagicMock()
    client.client.chat.completions.create = hang

    task = asyncio.create_task(client.chat([{"role": "user", "content": "hi"}]))
    await asyncio.wait_for(started.wait(), timeout=2)
    client.request_stop()
    res = await asyncio.wait_for(task, timeout=2)
    assert not res.success and "停止" in res.error
    assert res.tokens is None and res.usage_status is UsageStatus.UNKNOWN


# ---------------------------------------------------------------------------
# 流式事件解析与流式重试链
# ---------------------------------------------------------------------------

def test_stream_event_parsing_and_token_estimation():
    def _ev(content="", reasoning="", reasoning_details=None, usage=None):
        ev = MagicMock()
        if usage is not None:
            ev.choices = []
            ev.usage = usage
            return ev
        d = MagicMock()
        d.content = content
        d.reasoning_content = reasoning
        d.reasoning_details = reasoning_details
        ev.choices = [MagicMock(delta=d)]
        ev.usage = None
        return ev

    c1 = parse_openai_stream_event(_ev(content="hello"))
    assert c1 is not None and c1.type == "content" and c1.text == "hello"

    c2 = parse_openai_stream_event(_ev(reasoning_details=[{"text": "step 1 "}, {"text": "step 2"}]))
    assert c2 is not None and c2.type == "reasoning" and c2.reasoning_text == "step 1 step 2"

    u = MagicMock(prompt_tokens=100, completion_tokens=50, prompt_cache_hit_tokens=0)
    u.prompt_tokens_details = MagicMock(cached_tokens=10)
    c3 = parse_openai_stream_event(_ev(usage=u))
    assert c3 is not None and c3.type == "usage" and c3.usage_cached_tokens == 10 and c3.is_final

    assert estimate_token_count("") == 0
    assert estimate_token_count("hello world") == 2
    assert estimate_token_count("这是小说") == 4


async def test_chat_stream_with_retry_and_partial_preservation():
    client = LLMClient(make_config(temperature_max_retries=2, backoff_max_retries=0))
    client._sleep = AsyncMock()
    calls = [0]

    async def mock_chat_stream(messages, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            yield StreamChunk(type="content", text='{"bad": "json"}')
        else:
            yield StreamChunk(type="content", text='{"good": "json"}')
            yield StreamChunk(type="usage", usage_prompt_tokens=5, usage_completion_tokens=3, is_final=True)

    client.chat_stream = mock_chat_stream
    res = await client.chat_stream_with_retry(
        [{"role": "user", "content": "hi"}],
        validate_response=lambda c: (True, "") if "good" in c else (False, "missing good"),
    )
    assert res.success and res.content == '{"good": "json"}' and calls[0] == 2

    # 失败时保留 partial content
    client2 = LLMClient(make_config(temperature_max_retries=1, backoff_max_retries=0))
    client2._sleep = AsyncMock()

    async def partial_stream(*args, **kwargs):
        yield StreamChunk(type="content", text='{"foreshadows":[{"clue":"残缺')
        yield StreamChunk(type="error", error="stream truncated")

    client2.chat_stream = partial_stream
    res2 = await client2.chat_stream_with_retry([{"role": "user", "content": "hi"}])
    assert not res2.success and res2.content == '{"foreshadows":[{"clue":"残缺'


# ---------------------------------------------------------------------------
# Anthropic 协议与思考参数探测
# ---------------------------------------------------------------------------

def test_provider_detection_and_anthropic_payload():
    assert detect_provider(make_config(provider="auto", base_url="https://api.anthropic.com")) == "anthropic"
    assert detect_provider(make_config(provider="auto", api_key="sk-ant-abc")) == "anthropic"
    assert detect_provider(make_config(provider="auto", model="claude-sonnet-4-5")) == "anthropic"
    assert detect_provider(make_config(provider="openai", api_key="sk-ant-abc")) == "openai"

    client = LLMClient(make_config(
        provider="anthropic",
        model="claude-3-5-sonnet",
        thinking_mode={"thinking": {"type": "enabled", "budget_tokens": 1024}},
    ))
    payload = client._build_anthropic_payload(
        [
            {"role": "system", "content": "你是分析器"},
            {"role": "user", "content": "问题1"},
            {"role": "user", "content": "问题2"},
        ],
        temperature=0.5,
        max_tokens=800,
    )
    assert payload["system"] == "你是分析器"
    assert payload["messages"] == [{"role": "user", "content": "问题1\n\n问题2"}]

    resp = MagicMock()
    resp.content = [MagicMock(type="text", text='{"ok": true}'), MagicMock(type="thinking", thinking="...")]
    resp.usage = MagicMock(input_tokens=100, output_tokens=50, cache_read_input_tokens=30)
    content, pt, ct, cached = parse_anthropic_response(resp)
    assert content == '{"ok": true}' and (pt, ct, cached) == (100, 50, 30)


async def test_probe_thinking_params_handles_pydantic_usage():
    class _Details:
        def __init__(self, t):
            self.reasoning_tokens = t

    class _Usage:
        def __init__(self, d):
            self.prompt_tokens = 10
            self.completion_tokens = 20
            self.total_tokens = 30
            self.completion_tokens_details = d

    assert _get_reasoning_tokens(None) == 0
    assert _get_reasoning_tokens({"completion_tokens_details": {"reasoning_tokens": 250}}) == 250
    assert _get_reasoning_tokens(_Usage(_Details(250))) == 250

    def _resp(rt):
        r = MagicMock()
        r.choices = [MagicMock(message=MagicMock(content="ok", reasoning_content=None, reasoning_details=None))]
        r.usage = _Usage(_Details(rt))
        return r

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(side_effect=[
        _resp(250), _resp(250), _resp(0), _resp(250), _resp(250), _resp(250), _resp(0),
    ])
    with patch("backend.core.llm_probe.AsyncOpenAI", return_value=fake_client):
        result = await probe_thinking_params("https://api.test.com", "k", "m3", "auto")
    assert result["default_thinks"] is True and result["best"] is not None


# ---------------------------------------------------------------------------
# 流式进度回调工厂
# ---------------------------------------------------------------------------

async def test_stream_progress_callback_contract(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    class RecordingHub(ProgressHub):
        def __init__(self):
            super().__init__()
            self.events = []

        async def broadcast_token_delta(self, context, session_id, unit_idx, delta):
            self.events.append((context, session_id, unit_idx, delta))

    hub = RecordingHub()
    cb = make_stream_progress_callback(hub, "analysis", "book1", 7)
    now[0] += 0.5
    await cb(StreamChunk(type="content", text="x", estimated_total_tokens=40))
    await cb(StreamChunk(type="reasoning", text="r", estimated_total_tokens=99))  # 过滤不广播

    assert len(hub.events) == 1
    ctx, sid, uid, delta = hub.events[0]
    assert (ctx, sid, uid) == ("analysis", "book1", 7)
    assert delta == {"output_tokens": 40, "rate_tokens_per_sec": 80.0, "elapsed_sec": 0.5}
