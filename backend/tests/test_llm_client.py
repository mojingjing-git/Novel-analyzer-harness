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
    parse_anthropic_stream_event,
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


async def test_t4_retry_boundary_lowest_transport_sdk_retries_strictly_disabled():
    """F3 验收 5：拦截最底层传输计数，验证 SDK 与包装层绝不发生隐式重试。"""
    import httpx2
    from backend.config.settings import APIConfig

    api = APIConfig(
        base_url="http://127.0.0.1:3425/v1",
        api_key="test",
        model="codex/gpt-6-luna",
        sdk_max_retries=0,
        max_retries=0,
        temperature_max_retries=1,
        backoff_max_retries=0,
        streaming_enabled=False,
    )
    client = LLMClient(api)

    send_count_500 = 0

    async def mock_send_500(request, *args, **kwargs):
        nonlocal send_count_500
        send_count_500 += 1
        return httpx2.Response(status_code=500, request=request, content=b"server error")

    client.client._client.send = mock_send_500
    res500 = await client.chat_single([{"role": "user", "content": "hi"}])
    assert res500.success is False
    assert send_count_500 == 1, f"500 状态码触发了 SDK 隐式重试，计数={send_count_500}"

    send_count_429 = 0

    async def mock_send_429(request, *args, **kwargs):
        nonlocal send_count_429
        send_count_429 += 1
        return httpx2.Response(status_code=429, request=request, content=b"rate limit")

    client.client._client.send = mock_send_429
    res429 = await client.chat_single([{"role": "user", "content": "hi"}])
    assert res429.success is False
    assert send_count_429 == 1, f"429 限流触发了包装层或 SDK 隐式重试，计数={send_count_429}"


async def test_finish_reason_stop_on_truncation_and_usage_isolation():
    """验证双协议 finish_reason/stop_reason 提取、截断短路、空截断不误判审核与跨尝试用量隔离。"""
    # 1. MagicMock 守卫：未显式赋字符串的 finish_reason/stop_reason 不得污染结果
    mock_ev = MagicMock()
    mock_ev.choices = [MagicMock(delta=MagicMock(content="你好", reasoning_content=""))]
    chunk = parse_openai_stream_event(mock_ev)
    assert chunk is not None and chunk.finish_reason == ""

    ev_fr = MagicMock()
    ev_fr.choices = [MagicMock(delta=None, finish_reason="length")]
    fin_chunk = parse_openai_stream_event(ev_fr)
    assert fin_chunk is not None and fin_chunk.type == "finish" and fin_chunk.finish_reason == "length"

    ant_ev = MagicMock(
        type="message_delta",
        delta=MagicMock(stop_reason="max_tokens"),
        usage=MagicMock(output_tokens=120),
    )
    ant_chunk = parse_anthropic_stream_event(ant_ev)
    assert ant_chunk is not None and ant_chunk.finish_reason == "max_tokens"

    # 2. 非流式 OpenAI：空内容 + finish_reason="length" 不得误标 [内容审核拦截]，且保留 token 与 KNOWN
    oa_client = LLMClient(make_config(streaming_enabled=False, temperature_max_retries=2, backoff_max_retries=1))
    oa_client._sleep = AsyncMock()
    oa_resp = MagicMock()
    oa_resp.usage = MagicMock(prompt_tokens=80, completion_tokens=500, total_tokens=580, prompt_cache_hit_tokens=0)
    oa_resp.choices = [MagicMock(finish_reason="length", message=MagicMock(content="", reasoning_content="思考耗尽"))]
    oa_client.client.chat.completions.create = AsyncMock(return_value=oa_resp)

    res_oa = await oa_client.chat_with_retry(
        [{"role": "user", "content": "hi"}],
        stop_on_truncation=True,
    )
    assert res_oa.success is False
    assert res_oa.finish_reason == "length"
    assert res_oa.tokens == (80, 500)
    assert res_oa.usage_status == UsageStatus.KNOWN
    assert "[内容审核拦截]" not in res_oa.error
    assert res_oa.call_stats["attempts"] == 1  # stop_on_truncation=True 立即短路不重试

    # 3. 非流式 Anthropic：stop_reason="max_tokens" 同样短路且不误判审核
    ant_client = LLMClient(make_config(provider="anthropic", streaming_enabled=False, temperature_max_retries=2))
    ant_client._sleep = AsyncMock()
    ant_resp = MagicMock()
    ant_resp.content = [MagicMock(type="thinking", thinking="思考超长")]
    ant_resp.usage = MagicMock(input_tokens=60, output_tokens=300, cache_read_input_tokens=0)
    ant_resp.stop_reason = "max_tokens"
    ant_client.anthropic.messages.create = AsyncMock(return_value=ant_resp)

    res_ant = await ant_client.chat_with_retry(
        [{"role": "user", "content": "hi"}],
        stop_on_truncation=True,
    )
    assert res_ant.success is False
    assert res_ant.finish_reason == "max_tokens"
    assert res_ant.tokens == (60, 300)
    assert "[内容审核拦截]" not in res_ant.error
    assert res_ant.call_stats["attempts"] == 1

    # 4. 流式校验截断短路 + 跨尝试用量不复用（P1-6）
    st_client = LLMClient(make_config(streaming_enabled=True, temperature_max_retries=2, backoff_max_retries=0))
    st_client._sleep = AsyncMock()

    async def trunc_stream(*_args, **_kwargs):
        yield StreamChunk(type="content", text='{"chapters":[]')
        yield StreamChunk(type="finish", finish_reason="length", is_final=True)
        yield StreamChunk(type="usage", usage_prompt_tokens=40, usage_completion_tokens=90, is_final=True)

    st_client.chat_stream = trunc_stream
    res_st = await st_client.chat_stream_with_retry(
        [{"role": "user", "content": "hi"}],
        validate_response=lambda _c: (False, "未闭合"),
        stop_on_truncation=True,
    )
    assert res_st.success is True  # 流本身返回内容（finish_reason="length"）并经短路直接交付给上层抢救
    assert res_st.finish_reason == "length"
    assert res_st.call_stats["attempts"] == 1

    # P1-6: 第1次返回不合规内容(40+20=60 tok)，第2次流式异常失败（0 tok）：
    # total failed_tokens 只记一次 60，不能把第1次的 60 复用给第2次算成 120
    attempt_idx = [0]

    async def two_attempt_stream(*_args, **_kwargs):
        attempt_idx[0] += 1
        if attempt_idx[0] == 1:
            yield StreamChunk(type="content", text="bad-format")
            yield StreamChunk(type="usage", usage_prompt_tokens=40, usage_completion_tokens=20, is_final=True)
        else:
            yield StreamChunk(type="error", error="network drop")

    st2 = LLMClient(make_config(streaming_enabled=True, temperature_max_retries=2, backoff_max_retries=0))
    st2._sleep = AsyncMock()
    st2.chat_stream = two_attempt_stream
    res_p16 = await st2.chat_stream_with_retry(
        [{"role": "user", "content": "hi"}],
        validate_response=lambda _c: (False, "格式错"),
    )
    assert res_p16.success is False
    assert res_p16.call_stats["failed_tokens"] == 60


async def test_anthropic_cache_control_dict_ptd_and_stop_on_timeout():
    """验证长系统提示词带 Anthropic cache_control、dict prompt_tokens_details 提取与 stop_on_timeout。"""
    from types import SimpleNamespace
    from backend.common.error_taxonomy import ErrorClass, classify_exception
    from backend.core.llm_stream import parse_openai_stream_event

    # 1. classify_exception 识别 TimeoutError / APITimeoutError，且非永久性
    verdict = classify_exception(TimeoutError("read timed out"))
    assert verdict is not None and verdict.cls is ErrorClass.TIMEOUT

    # 2. Anthropic system cache_control（<1024 纯字符串，>=1024 ephemeral block）
    c_anthro = LLMClient(make_config(api_key="sk-ant-test", base_url="https://api.anthropic.com/v1", model="claude-sonnet-4-5"))
    short_payload = c_anthro._build_anthropic_payload(
        [{"role": "system", "content": "短前缀"}, {"role": "user", "content": "hi"}],
        temperature=0.1,
        max_tokens=1024,
    )
    assert isinstance(short_payload["system"], str)
    long_sys = "提示词" * 500
    long_payload = c_anthro._build_anthropic_payload(
        [{"role": "system", "content": long_sys}, {"role": "user", "content": "hi"}],
        temperature=0.1,
        max_tokens=1024,
    )
    assert isinstance(long_payload["system"], list)
    assert long_payload["system"][0]["cache_control"] == {"type": "ephemeral"}

    # 3. dict 形态 prompt_tokens_details 提取（非流式 & 流式）
    ev = SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_tokens_details={"cached_tokens": 64}),
    )
    chunk = parse_openai_stream_event(ev)
    assert chunk is not None and chunk.usage_cached_tokens == 64

    # 4. stop_on_timeout=True 首轮超时即返回且 verdict.cls == TIMEOUT（不触发重试睡）
    c_to = LLMClient(make_config(temperature_max_retries=3, backoff_max_retries=2))
    c_to._sleep = AsyncMock()
    calls = 0

    async def fake_chat(*_a, **_kw):
        nonlocal calls
        calls += 1
        return LLMResult(False, "", "请求超时 (timeout=300s): TimeoutError", None, verdict=verdict)

    c_to.chat = fake_chat
    out_to = await c_to.chat_with_retry([{"role": "user", "content": "x"}], stop_on_timeout=True)
    assert calls == 1
    assert out_to.success is False
    assert out_to.verdict is not None and out_to.verdict.cls.value == "timeout"
    assert out_to.verdict.is_permanent is False
