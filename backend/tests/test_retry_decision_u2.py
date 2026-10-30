"""U2 验收：重试决策单点化（``_decide_retry``）与"文本通道永不判停"契约。

本文件锁住三件事：

1. 文本通道（``classify_message``）给出的 AUTH/BILLING 只是 ``limited_retry``——
   不得成为永久熔断依据；只有结构化通道（``classify_exception``，异常对象还在场）
   才允许 ``stop``。这是 ``ClassVerdict.is_permanent()`` 的语义（C-01）。
2. ``chat()`` / ``chat_stream()`` 在异常现场算出的结构化结论会传回重试循环，
   所以"结构化 401/402/400 首次即停"在端到端仍然成立，而不是被字符串化降级掉的死路径。
3. 四处重复决断收敛成 ``LLMClient._decide_retry`` 之后，退避公式与失败登记口径逐相位不变
   （温度层 ``RetryNeeded``/``wait_time=0``，退避层 ``ExponentialBackoff``/等待原样回传）。
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import APIConfig
from backend.core.error_types import Confidence, LLMResult
from backend.core.error_taxonomy import (
    ClassVerdict, ErrorClass, classify_exception, classify_message,
)
from backend.core.llm_client import LLMClient, _error_action, _to_error_verdict


def _make_cfg(**kw):
    defaults = dict(
        base_url="https://api.test.com", api_key="k", model="m",
        max_tokens=100, timeout=5,
        temperature_max_retries=3, backoff_max_retries=2,
    )
    defaults.update(kw)
    return APIConfig(**defaults)


def _sdk_error(msg, status, body, cls=None):
    """构造带真实 HTTP 状态的 SDK 异常（结构化通道的输入端）。"""
    url = "https://api.test.com/v1/chat/completions"
    resp = httpx.Response(status, request=httpx.Request("POST", url), json=body)
    return (cls or openai.APIStatusError)(msg, response=resp, body=body)


def _client_with_mock_sdk(exc, **cfg_kw):
    """把底层 SDK 调用打成抛 ``exc``，让真实 chat()/chat_stream() 处理异常。"""
    client = LLMClient(_make_cfg(**cfg_kw))
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=exc)
    client.client = fake
    client._sleep = AsyncMock()
    return client


# ===========================================================================
# 一、文本通道：AUTH/BILLING 不得 stop
# ===========================================================================

@pytest.mark.parametrize("msg", [
    "invalid_api_key",
    "认证失败，请检查API Key: sk-xxx",
    "Error code: 401 - Unauthorized",
    "API错误: insufficient_quota",
    "API错误 (HTTP 402): 余额不足",
])
def test_text_channel_never_permanent(msg):
    """纯文本的 AUTH/BILLING：置信永远不是 structured，因此永不 is_permanent。"""
    verdict = classify_message(msg)
    assert verdict.confidence != "structured"
    assert verdict.is_permanent() is False
    assert verdict.action != "stop"
    # 旧实现（RETRY_POLICY[cls] == "stop"）在这里会返回 "stop"——C-01 的漏判点
    action, back = _error_action(msg, 0)
    assert action == "continue"
    assert back.is_permanent() is False


# ===========================================================================
# 二、结构化通道：同类错误仍然 stop
# ===========================================================================

@pytest.mark.parametrize("status,body,expected_cls", [
    (401, {"error": {"code": "invalid_api_key"}}, ErrorClass.AUTH),
    (402, {"error": {"type": "insufficient_balance_error"}}, ErrorClass.BILLING),
    (400, {"error": {"code": "invalid_parameter"}}, ErrorClass.BAD_REQUEST),
])
def test_structured_stop_classes(status, body, expected_cls):
    """真实结构化错误：confidence=structured + action=stop → 仍判永久停止。"""
    verdict = classify_exception(_sdk_error("boom", status, body))
    assert verdict.cls is expected_cls
    assert verdict.confidence == "structured"
    assert verdict.is_permanent() is True
    assert _error_action("", 0, verdict)[0] == "stop"


def test_structured_rate_limit_not_permanent():
    """429 是暂时性错误：结构化也不 stop。"""
    verdict = classify_exception(_sdk_error("rate limit", 429, {"error": {"type": "rate_limit_error"}}))
    assert verdict.is_permanent() is False
    assert _error_action("", 0, verdict)[0] == "continue"


def test_moderation_real_contract_kept():
    """审核真实契约不变：输入侧/结构化首次即停；输出侧文本仍连续 3 次。"""
    assert _error_action("", 0, ClassVerdict(ErrorClass.MODERATION_INPUT, "structured"))[0] == "moderation_stop"
    assert _error_action("", 0, ClassVerdict(ErrorClass.MODERATION_INPUT, "text"))[0] == "moderation_stop"
    text_out = ClassVerdict(ErrorClass.MODERATION_OUTPUT, "text")
    assert _error_action("", 0, text_out)[0] == "moderation_count"
    assert _error_action("", 2, text_out)[0] == "moderation_stop"


# ===========================================================================
# 三、_decide_retry：退避公式与相位口径（四份拷贝的行为等价性）
# ===========================================================================

def test_decide_retry_temperature_wait_formula():
    """温度层：RATE_LIMIT 优先 Retry-After（上限 120），其余 3*(attempt+1)。"""
    client = LLMClient(_make_cfg())
    common = dict(moderation_hits=0, attempt=2, total_attempts=3, temperature=0.5,
                  messages_length=10, phase="temperature", moderation_log="x")

    d = client._decide_retry("transient boom", **common)
    assert (d.action, d.wait_time) == ("continue", 9)

    d429 = client._decide_retry("API错误 (HTTP 429): 触发限流 [Retry-After:42]", **common)
    assert (d429.action, d429.wait_time) == ("continue", 42)

    # 无 Retry-After 时 30*(attempt+1)，attempt=4 → 150 被 120 夹住
    capped = client._decide_retry("API错误 (HTTP 429): 触发限流", **{**common, "attempt": 4})
    assert capped.wait_time == 120


def test_decide_retry_backoff_passes_wait_time_through():
    """退避层：等待时间已由调用点算好，原样回传（不重算）。"""
    client = LLMClient(_make_cfg())
    d = client._decide_retry("transient boom", moderation_hits=0, attempt=0, total_attempts=4,
                             temperature=1.0, messages_length=10, phase="backoff",
                             moderation_log="x", wait_time=7)
    assert (d.action, d.wait_time) == ("continue", 7)


def test_decide_retry_stop_appends_hint_only_for_non_auth():
    """stop 分支：非 AUTH 追加 _STOP_HINTS，AUTH 不追加（日志措辞不变）。

    U5 起 ``_decide_retry`` 的 verdict 形参是"结果对象上的形态"（ErrorVerdict），
    与 chat()/chat_stream() 写进 LLMResult / StreamChunk 的完全一致；故这里用
    _to_error_verdict 把异常现场的结构化结论转成同一形态再传入，而不是直接塞
    策略层的 ClassVerdict（那会在真实调用路径上出现类型错位）。
    """
    client = LLMClient(_make_cfg())
    billing = _to_error_verdict(
        classify_exception(
            _sdk_error("402", 402, {"error": {"type": "insufficient_balance_error"}})
        ),
        "API错误 (HTTP 402)",
    )
    d = client._decide_retry("API错误 (HTTP 402)", moderation_hits=0, attempt=0, total_attempts=1,
                             temperature=1.0, messages_length=10, phase="temperature",
                             moderation_log="x", verdict=billing)
    assert d.action == "stop"
    assert "余额不足" in d.error

    auth = _to_error_verdict(
        classify_exception(_sdk_error("401", 401, {"error": {"code": "invalid_api_key"}})),
        "认证失败",
    )
    d_auth = client._decide_retry("认证失败", moderation_hits=0, attempt=0, total_attempts=1,
                                  temperature=1.0, messages_length=10, phase="temperature",
                                  moderation_log="x", verdict=auth)
    assert d_auth.action == "stop"
    assert d_auth.error == "认证失败"


async def test_failure_log_error_types_unchanged():
    """登记口径不变：温度层 RetryNeeded(wait_time=0)，退避层 ExponentialBackoff(等待值)。"""
    client = LLMClient(_make_cfg(temperature_max_retries=2, backoff_max_retries=1))
    client.chat = AsyncMock(return_value=LLMResult(False, "", "transient boom"))
    client._sleep = AsyncMock()
    with patch("backend.core.llm_client._get_failure_logger") as gl:
        result = await client.chat_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    kinds = [(c.kwargs["error_type"], c.kwargs.get("wait_time"))
             for c in gl.return_value.record_failure.call_args_list]
    assert kinds == [("RetryNeeded", 0), ("RetryNeeded", 0), ("ExponentialBackoff", 2)]


# ===========================================================================
# 四、端到端：文本走满重试链，结构化首次即停
# ===========================================================================

async def test_e2e_text_auth_retries_full_chain():
    """纯文本 AUTH：不再首次即停，走完温度退火 + 指数退避（3+2）。"""
    client = LLMClient(_make_cfg(temperature_max_retries=3, backoff_max_retries=2))
    client.chat = AsyncMock(return_value=LLMResult(False, "", "认证失败，请检查API Key: sk-xxx"))
    client._sleep = AsyncMock()
    result = await client.chat_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 5
    assert client.chat.call_count == 5
    # 文本结论落在结果对象里：置信 text、动作非 stop（ErrorVerdict 无 is_permanent()）
    assert result.verdict is not None
    assert result.verdict.confidence is not Confidence.STRUCTURED
    assert result.verdict.action != "stop"


async def test_e2e_stream_text_billing_retries_instead_of_stopping():
    """纯文本 BILLING：流式同样不再首次即停。"""
    from backend.core.llm_stream import StreamChunk

    async def _err_stream(*a, **k):
        yield StreamChunk(type="error", error="流式调用异常: APIStatusError: 402 - insufficient_balance_error")

    client = LLMClient(_make_cfg(temperature_max_retries=2, backoff_max_retries=1))
    client.chat_stream = _err_stream
    client._sleep = AsyncMock()
    result = await client.chat_stream_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 3


async def test_e2e_structured_billing_stops_first_attempt():
    """结构化 402：非流式首次即停（结构化结论经槽传回重试循环）。"""
    exc = _sdk_error("insufficient balance", 402, {"error": {"type": "insufficient_balance_error"}})
    client = _client_with_mock_sdk(exc)
    result = await client.chat_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 1
    assert "余额不足" in result.error
    # U4：结论随结果对象返回，不再只有字符串
    assert result.verdict is not None
    assert result.verdict.confidence is Confidence.STRUCTURED
    assert result.verdict.action == "stop"
    assert result.tokens is None  # 失败且无 usage：不是 (0, 0)


async def test_e2e_stream_structured_auth_stops_first_attempt():
    """结构化 401：流式首次即停（结论由错误块携带，经 StreamResult 外传）。"""
    exc = _sdk_error("Unauthorized", 401, {"error": {"code": "invalid_api_key"}},
                     cls=openai.AuthenticationError)
    client = _client_with_mock_sdk(exc)
    result = await client.chat_stream_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 1
    # U5：流式失败结果同样带回完整判定（此前恒为 None，消费方拿不到）
    assert result.verdict is not None
    assert result.verdict.confidence is Confidence.STRUCTURED
    assert result.verdict.action == "stop"


async def test_stream_error_chunk_carries_full_verdict():
    """§2.5：流式错误块自己携带完整契约（含 scope），不是只在终结结果上补齐。"""
    exc = _sdk_error("insufficient balance", 402,
                     {"error": {"type": "insufficient_balance_error"}})
    client = _client_with_mock_sdk(exc)
    chunks = [c async for c in client.chat_stream([{"role": "user", "content": "x"}])]
    err_chunks = [c for c in chunks if c.type == "error"]
    assert len(err_chunks) == 1
    v = err_chunks[0].verdict
    assert v is not None
    assert v.cls.value == "billing"
    assert v.confidence is Confidence.STRUCTURED
    assert v.action == "stop"
    assert v.scope.value != "unknown"  # 结构化通道才敢给的具体作用域
    assert v.message == err_chunks[0].error


async def test_stream_error_chunk_carries_retry_after():
    """429：retry_after 从错误串的 [Retry-After:N] 标记读回——转换必须排在标记拼接之后。"""
    url = "https://api.test.com/v1/chat/completions"
    body = {"error": {"type": "rate_limit_error"}}
    resp = httpx.Response(429, request=httpx.Request("POST", url), json=body,
                          headers={"retry-after": "30"})
    exc = openai.APIStatusError("rate limited", response=resp, body=body)
    client = _client_with_mock_sdk(exc, temperature_max_retries=1, backoff_max_retries=1)
    chunks = [c async for c in client.chat_stream([{"role": "user", "content": "x"}])]
    err_chunks = [c for c in chunks if c.type == "error"]
    assert len(err_chunks) == 1
    assert "[Retry-After:30]" in err_chunks[0].error
    v = err_chunks[0].verdict
    assert v is not None
    assert v.cls.value == "rate_limit"
    assert v.retry_after == 30.0
