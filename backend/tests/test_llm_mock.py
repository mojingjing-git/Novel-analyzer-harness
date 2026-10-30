"""
测试 LLMClient 温度退火 + 指数退避重试链（使用 mock）
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import APIConfig
from backend.core.error_types import Confidence, LLMResult, UsageStatus
from backend.core.llm_client import LLMClient


def ok_result(content="", tokens=(10, 20)):
    """构造成功结果（U4：非流式调用返回 LLMResult 对象，不再是元组）"""
    return LLMResult(True, content, "", tokens, UsageStatus.KNOWN)


def fail_result(error, tokens=None):
    """构造失败结果。

    U4 契约：拿不到 usage 时 tokens 必须是 None + usage_status=UNKNOWN，
    不允许再写 (0, 0)——(0,0) 是"真的用了 0 token"，会把成本统计搅浑。
    """
    usage = UsageStatus.KNOWN if tokens is not None else UsageStatus.UNKNOWN
    return LLMResult(False, "", error, tokens, usage)


def make_config(**kwargs):
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
    )
    defaults.update(kwargs)
    return APIConfig(**defaults)


async def test_success_on_first_try():
    """首次调用成功"""
    config = make_config()
    client = LLMClient(config)
    # Mock chat method
    client.chat = AsyncMock(return_value=ok_result('{"result":"ok"}', (10, 20)))
    result = await client.chat_with_retry([{"role": "user", "content": "hi"}])
    assert result.success
    assert result.content == '{"result":"ok"}'
    assert result.tokens == (10, 20)
    assert result.usage_status is UsageStatus.KNOWN
    assert result.attempts == 1
    assert result.call_stats["attempts"] == 1
    assert result.call_stats["failed_tokens"] == 0
    assert client.chat.call_count == 1
    print("✅ test_success_on_first_try passed")


async def test_temperature_annealing():
    """温度退火：第一次失败，第二次成功"""
    config = make_config(temperature=0.7, temperature_step=0.1, temperature_max_retries=3, backoff_max_retries=0)
    client = LLMClient(config)
    call_temps = []

    async def mock_chat(messages, temperature=0.1, max_tokens=None):
        call_temps.append(temperature)
        if len(call_temps) == 1:
            return fail_result('API error')
        return ok_result('{"result":"ok"}', (10, 20))

    client.chat = mock_chat
    result = await client.chat_with_retry([{"role": "user", "content": "hi"}])
    assert result.success
    assert len(call_temps) == 2
    # First temp: 0.7, second: 0.6
    assert call_temps[0] == 0.7
    assert call_temps[1] == pytest_approx(0.6)
    print("✅ test_temperature_annealing passed")


async def test_text_auth_error_no_longer_stops():
    """旧契约改写（U2 / C-01）：纯文本形态的 AUTH 不再永久熔断

    旧断言 client.chat.call_count == 1 依赖"文本通道也能 stop"——那正是 C-01 要止血的漏判：
    真实 401 一旦被 chat() 字符串化成"认证失败，请检查API Key"，重试循环就只剩文本通道可用，
    而 classify_message 的置信永远是 text、action 上限 limited_retry。现在文本 AUTH 走完
    温度退火 + 指数退避全链（3+2=5 次），只登记 RetryNeeded，不判 fatal。
    结构化 401 仍首次即停，见 test_error_taxonomy.py::test_nonstream_retry_auth_structured_stops。
    """
    config = make_config(temperature_max_retries=3, backoff_max_retries=2)
    client = LLMClient(config)
    client.chat = AsyncMock(return_value=fail_result('认证失败，请检查API Key'))
    client._sleep = AsyncMock()  # 不真睡（全链等待合计约 70s）
    result = await client.chat_with_retry([{"role": "user", "content": "hi"}])
    assert not result.success
    assert "认证" in result.error
    # 文本通道不能作为永久熔断依据：3 次温度退火 + 2 次指数退避
    assert result.call_stats["attempts"] == 5
    assert client.chat.call_count == 5
    # 文本 AUTH 的结论带在结果对象上：verdict 非空，但置信是 text、动作不是 stop
    # （ErrorVerdict 没有 is_permanent()——那是 ClassVerdict 的方法；判据见下表）
    assert result.verdict is not None
    assert result.verdict.confidence is not Confidence.STRUCTURED
    assert result.verdict.action != "stop"
    # 失败且拿不到 usage：tokens 必须缺席（不得是 (0, 0)）
    assert result.tokens is None
    assert result.usage_status is UsageStatus.UNKNOWN
    print("✅ test_text_auth_error_no_longer_stops passed")


async def test_stop_requested():
    """停止请求终止重试链"""
    config = make_config(temperature_max_retries=5)
    client = LLMClient(config)
    client.chat = AsyncMock(return_value=fail_result('API error'))
    client.request_stop()
    result = await client.chat_with_retry([{"role": "user", "content": "hi"}])
    assert not result.success
    assert "停止" in result.error
    print("✅ test_stop_requested passed")


async def test_chat_cancelled_by_stop():
    """停止后立即取消进行中的 API 请求（不等超时）"""
    config = make_config(timeout=10)
    client = LLMClient(config)
    # mock 底层 SDK：永不返回的挂起请求
    async def hang(*args, **kwargs):
        await asyncio.Event().wait()
    client.client = MagicMock()
    client.client.chat.completions.create = hang

    started = asyncio.Event()
    async def wrapped(*args, **kwargs):
        started.set()
        return await hang(*args, **kwargs)
    client.client.chat.completions.create = wrapped

    chat_task = asyncio.create_task(client.chat([{"role": "user", "content": "hi"}]))
    await asyncio.wait_for(started.wait(), timeout=2)
    client.request_stop()
    result = await asyncio.wait_for(chat_task, timeout=2)
    assert not result.success
    assert "停止" in result.error
    # 旧的 assert tokens == (0, 0) 已改写：没拿到 usage 就是 None + UNKNOWN
    assert result.tokens is None
    assert result.usage_status is UsageStatus.UNKNOWN
    print("✅ test_chat_cancelled_by_stop passed")


async def test_chat_cancelled_externally():
    """外部 Task.cancel（如 style_service 停止风格分析）也要取消底层 API 请求"""
    config = make_config(timeout=10)
    client = LLMClient(config)
    api_cancelled = False

    async def hang(*args, **kwargs):
        nonlocal api_cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            api_cancelled = True
            raise
    client.client = MagicMock()
    client.client.chat.completions.create = hang

    chat_task = asyncio.create_task(client.chat([{"role": "user", "content": "hi"}]))
    await asyncio.sleep(0.1)
    chat_task.cancel()
    try:
        await chat_task
        assert False, "chat 应随外部取消而取消"
    except asyncio.CancelledError:
        pass
    assert api_cancelled, "底层 API 请求应被取消，不能泄漏为孤儿请求"
    print("✅ test_chat_cancelled_externally passed")


async def test_chat_hard_timeout_preserved():
    """硬超时语义保留：请求无响应且未停止时返回硬超时"""
    config = make_config(timeout=0.1)
    client = LLMClient(config)

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()
    client.client = MagicMock()
    client.client.chat.completions.create = hang

    # 硬超时 = config.timeout + 30s，mock 掉 sleep 立即触发该分支
    with patch("asyncio.sleep", new=AsyncMock()):
        result = await asyncio.wait_for(
            client.chat([{"role": "user", "content": "hi"}]), timeout=5)
    assert not result.success
    assert "硬超时" in result.error
    assert result.tokens is None
    assert result.usage_status is UsageStatus.UNKNOWN
    print("✅ test_chat_hard_timeout_preserved passed")


async def test_validation_failure_triggers_retry():
    """验证失败触发重试"""
    config = make_config(temperature_max_retries=3, backoff_max_retries=0)
    client = LLMClient(config)
    call_count = 0

    async def mock_chat(messages, temperature=0.1, max_tokens=None):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return ok_result('invalid json', (5, 10))
        return ok_result('{"valid":true}', (5, 10))

    client.chat = mock_chat

    def validate(content):
        import json
        try:
            json.loads(content)
            return True, ''
        except:
            return False, f'JSON parse error: {content}'

    def build_retry(error, messages):
        return messages + [{"role": "assistant", "content": "retry"}]

    result = await client.chat_with_retry(
        [{"role": "user", "content": "hi"}],
        validate_response=validate,
        retry_messages_builder=build_retry,
    )
    assert result.success
    assert call_count == 3
    # 前两次验证失败消耗的 tokens 计入 call_stats.failed_tokens
    assert result.call_stats["failed_tokens"] == 30
    print("✅ test_validation_failure_triggers_retry passed")


async def test_attempts_counter():
    """total_attempts 随重试递增（供重试成本统计）"""
    config = make_config(temperature=0.7, temperature_step=0.1, temperature_max_retries=3, backoff_max_retries=0)
    client = LLMClient(config)
    call_temps = []

    async def mock_chat(messages, temperature=0.1, max_tokens=None):
        call_temps.append(temperature)
        if len(call_temps) < 3:
            return fail_result('API error')
        return ok_result('{"result":"ok"}', (10, 20))

    client.chat = mock_chat
    result = await client.chat_with_retry([{"role": "user", "content": "hi"}])
    assert result.success
    assert client.get_stats()["total_attempts"] == 3
    # call_stats 只统计本次调用（与客户端全局计数一致：单线程场景）
    assert result.call_stats["attempts"] == 3
    assert result.attempts == 3
    assert result.call_stats["failed_tokens"] == 0
    print("✅ test_attempts_counter passed")


async def test_attempts_counter_backoff_path():
    """指数退避路径的调用统计：温度退火 + 退避两循环的 total_attempts 全部计入"""
    config = make_config(temperature=0.7, temperature_step=0.1,
                         temperature_max_retries=3, backoff_max_retries=2)
    client = LLMClient(config)
    # 所有响应均验证失败（消耗 tokens 但无效）→ 温度退火3次 + 指数退避2次
    client.chat = AsyncMock(return_value=ok_result('invalid json', (5, 10)))

    def validate(content):
        return False, 'JSON parse error'

    # 指数退避等待 min(2^(n+1),60) 秒会拖慢测试，mock 掉 sleep
    with patch("asyncio.sleep", new=AsyncMock()):
        result = await client.chat_with_retry(
            [{"role": "user", "content": "hi"}],
            validate_response=validate,
        )

    assert not result.success
    assert "所有重试均失败" in result.error
    # 温度退火3次 + 指数退避2次
    assert result.call_stats["attempts"] == 5
    # 每次验证失败都消耗 (5,10) tokens 并计入 failed_tokens
    assert result.call_stats["failed_tokens"] == 5 * 15
    # 客户端全局计数与 call_stats 一致
    assert client.get_stats()["total_attempts"] == 5
    assert client.get_stats()["failed_tokens"] == 75
    print("✅ test_attempts_counter_backoff_path passed")


def pytest_approx(expected, rel=1e-6):
    """Simple approximation for floating point comparison"""
    class Approx:
        def __init__(self, expected, rel):
            self.expected = expected
            self.rel = rel
        def __eq__(self, other):
            return abs(other - self.expected) < self.rel
        def __repr__(self):
            return f"approx({self.expected})"
    return Approx(expected, rel)


async def main():
    await test_success_on_first_try()
    await test_temperature_annealing()
    await test_text_auth_error_no_longer_stops()
    await test_stop_requested()
    await test_validation_failure_triggers_retry()
    await test_attempts_counter()
    await test_attempts_counter_backoff_path()
    await test_chat_cancelled_by_stop()
    await test_chat_cancelled_externally()
    await test_chat_hard_timeout_preserved()
    print("\n🎉 All LLM mock tests passed!")


if __name__ == "__main__":
    asyncio.run(main())


async def test_429_extended_backoff():
    """429 限流：退避轮数从 backoff_max_retries(1) 扩到 3，且尊重 Retry-After"""
    from backend.core.llm_client import LLMClient
    config = make_config(temperature=0.5, temperature_step=0.25,
                         temperature_max_retries=2, backoff_max_retries=1)
    client = LLMClient(config)
    # 温度退火2次全 429 → 退避阶段 429 扩到 3 轮（每轮带 Retry-After）→ 第 6 次成功
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
    with patch("asyncio.sleep", new=fake_sleep):
        result = await client.chat_with_retry(
            [{"role": "user", "content": "hi"}])

    assert result.success
    assert result.call_stats["attempts"] == 5    # 2 温度 + 3 退避(429扩展)，第 5 次成功
    # 429 等待应尊重 Retry-After=5（而非 2s/短退避）
    assert 5 in sleeps
    assert all(s <= 120 for s in sleeps)
    print("✅ test_429_extended_backoff passed")
