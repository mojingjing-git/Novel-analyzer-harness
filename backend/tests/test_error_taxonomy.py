"""error_taxonomy gold fixtures 与接入集成测试
（docs/PLAN-provider-error-taxonomy-2026-09-30.md §4 表全量 + §8 P2-4/§9 P2-1 歧义样本 + T2.1/T2.2/T2.4 验收）

fixtures 来源：api_failures.log 真实错误串（脱敏）+ 各厂商官方文档错误码样例。
既是单测也是防回归契约——改动码表/优先级前先看这里。
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core.error_taxonomy import (
    ErrorClass, RETRY_POLICY, classify_exception, classify_message,
)
from backend.core.moderation import mark_moderation, MODERATION_MARKER


def _ns(**kw):
    """异常对象模拟（对齐 test_llm_moderation_sniff 的 SimpleNamespace 风格）"""
    kw.setdefault("code", None)
    kw.setdefault("body", None)
    kw.setdefault("status_code", None)
    return SimpleNamespace(**kw)


# ===========================================================================
# 一、classify_message：日志真实串 gold fixtures
# ===========================================================================

def test_msg_moderation_1027_structured():
    """MiniMax 输出涉敏（§0.1 表 558 条）：码在消息文本中，经码串扫描以 structured 穿越"""
    v = classify_message(f"{MODERATION_MARKER} 流式调用异常: APIError: output new_sensitive (1027)")
    assert v.cls is ErrorClass.MODERATION_OUTPUT
    assert v.confidence == "structured"


def test_msg_moderation_422_input_structured():
    """MiniMax Anthropic 皮肤输入拦截（§0.1 表 ~5525 条，P2-1）"""
    v = classify_message("流式调用异常: UnprocessableEntityError: 422 "
                         "{'error': {'type': 'unprocessable_entity_error', 'message': 'input new_sensitive detected'}}")
    assert v.cls is ErrorClass.MODERATION_INPUT
    assert v.confidence == "structured"


def test_msg_billing_402_structured():
    v = classify_message("流式调用异常: APIStatusError: 402 - Error code: 402 - "
                         "{'error': {'type': 'insufficient_balance_error', 'message': 'insufficient balance'}}")
    assert v.cls is ErrorClass.BILLING
    assert v.confidence == "structured"


def test_msg_billing_429_fakeouts():
    """429 伪装成限流的账单错误（OpenAI insufficient_quota / 智谱 1113）"""
    assert classify_message("API错误 (HTTP 429): {'error': {'code': 'insufficient_quota'}}").cls is ErrorClass.BILLING
    assert classify_message("API错误 (HTTP 429): {'error': {'code': '1113', 'message': '账户已欠费'}}").cls is ErrorClass.BILLING


def test_msg_rate_limit():
    v = classify_message("API错误 (HTTP 429): {'error': {'type': 'rate_limit_error', 'message': '已达到 Token Plan 速率限制'}}")
    assert v.cls is ErrorClass.RATE_LIMIT
    assert classify_message("Weekly usage limit reached").cls is ErrorClass.RATE_LIMIT


def test_msg_throttling_prefix_outranks_quota():
    """百炼 Throttling.* 前插规则：即使消息同时含 insufficient_quota 也判限流（P2-4）"""
    v = classify_message("{'code': 'Throttling.RequestsThrottled', 'message': 'insufficient_quota, slow down'}")
    assert v.cls is ErrorClass.RATE_LIMIT


def test_msg_moderation_text_legacy():
    """现有文本兜底行为不回归"""
    assert classify_message("API错误 (HTTP 400): content policy").cls is ErrorClass.MODERATION_OUTPUT
    assert classify_message("未知错误: RuntimeError: 内容不符合安全规范").cls is ErrorClass.MODERATION_OUTPUT


def test_msg_moderation_text_threshold_guard():
    """text 置信守卫（§9 P3-8）：WAF 文本拦截必须是 text 而非 structured，维持 3 次短路"""
    v = classify_message(mark_moderation("API错误: Request blocked by WAF"))
    assert v.cls is ErrorClass.MODERATION_OUTPUT
    assert v.confidence == "text"


def test_msg_moderation_input_upgrade():
    v = classify_message("API错误: input new_sensitive detected")
    assert v.cls is ErrorClass.MODERATION_INPUT
    assert v.confidence == "text"


def test_msg_bad_request_model():
    assert classify_message("流式调用异常: BadRequestError: 400 'you must provide a model parameter'").cls is ErrorClass.BAD_REQUEST
    assert classify_message("API错误 (HTTP 400): {'error': {'code': 20012, 'message': 'Model does not exist or not available'}}").cls is ErrorClass.BAD_REQUEST


def test_msg_context_length_text():
    assert classify_message("API错误 (HTTP 400): Range of input length should be [1, 131072]").cls is ErrorClass.CONTEXT_LENGTH


def test_msg_auth():
    assert classify_message("认证失败，请检查API Key: sk-***").cls is ErrorClass.AUTH


def test_msg_timeout_and_unknown():
    assert classify_message("请求硬超时 (timeout=230s): API 无响应").cls is ErrorClass.TIMEOUT
    assert classify_message("transient error").cls is ErrorClass.UNKNOWN
    assert classify_message("stream truncated mid-response").cls is ErrorClass.UNKNOWN


def test_msg_server_network():
    assert classify_message("服务器繁忙，请稍后再试").cls is ErrorClass.SERVER
    assert classify_message("The model is overloaded, retry later").cls is ErrorClass.SERVER
    assert classify_message("连接失败: Connection error.").cls is ErrorClass.NETWORK


def test_msg_guards_no_false_positive():
    """P3-6/数字码守卫：refusal 仅 body 通道；数字 1000/131072 不得误报"""
    assert classify_message("Request failed: refusal").cls is ErrorClass.UNKNOWN
    assert classify_message("重试 1000 次后仍失败").cls is ErrorClass.UNKNOWN
    assert classify_message("input 131072 tokens exceeds nothing").cls is ErrorClass.UNKNOWN


# ===========================================================================
# 二、classify_exception：结构化通道 fixtures
# ===========================================================================

def test_exc_moderation_1026_attr_code():
    assert classify_exception(_ns(code="1026")).cls is ErrorClass.MODERATION_INPUT


def test_exc_moderation_sensitive_prefix():
    v = classify_exception(_ns(status_code=400, body={"error": {"code": "SensitiveContentDetected.SevereViolation"}}))
    assert v.cls is ErrorClass.MODERATION_INPUT
    assert v.confidence == "structured"


def test_exc_moderation_minimax_base_resp():
    """MiniMax 原生 200 响应错误：base_resp.status_code"""
    assert classify_exception(_ns(body={"base_resp": {"status_code": 1026}})).cls is ErrorClass.MODERATION_INPUT
    assert classify_exception(_ns(body={"base_resp": {"status_code": 1008}})).cls is ErrorClass.BILLING


def test_exc_billing_status_fallback():
    """body 无可识别码时 402 状态兜底"""
    assert classify_exception(_ns(status_code=402, body={"error": {"message": "no credits left"}})).cls is ErrorClass.BILLING


def test_exc_billing_429_fakeout_priority():
    """body 码表优先于 HTTP 状态（P2-4）：429 + insufficient_quota → BILLING 非 RATE_LIMIT"""
    v = classify_exception(_ns(status_code=429, body={"error": {"code": "insufficient_quota"}}))
    assert v.cls is ErrorClass.BILLING
    assert classify_exception(_ns(status_code=429, body={"error": {"type": "rate_limit_error"}})).cls is ErrorClass.RATE_LIMIT


def test_exc_ark_403_overdue_is_billing():
    """P2-4 歧义样本：方舟 403 AccountOverdueError → BILLING（非 AUTH）"""
    v = classify_exception(_ns(status_code=403, body={"error": {"code": "AccountOverdueError"}}))
    assert v.cls is ErrorClass.BILLING


def test_exc_bailian_context_over_bad_request():
    """P2-4 歧义样本：百炼 InvalidParameter + input-length message → CONTEXT_LENGTH。
    body 码 InternalError.Algo.InvalidParameter 不在码表（classify_exception 返回 None），
    判定经 classify_message 文本通道完成——即 llm_client 实际使用的组合链"""
    e = _ns(status_code=400, body={"error": {"code": "InternalError.Algo.InvalidParameter",
                                              "message": "Range of input length should be [1, 131072]"}})
    assert classify_exception(e) is None
    verdict = classify_exception(e) or classify_message(str(e))
    assert verdict.cls is ErrorClass.CONTEXT_LENGTH


def test_exc_unprocessable_422_conditional():
    """P2-1 条件规则：422 + unprocessable_entity_error + 敏感 message → INPUT；无敏感 message → BAD_REQUEST"""
    v = classify_exception(_ns(status_code=422, body={
        "error": {"type": "unprocessable_entity_error", "message": "input new_sensitive detected"}}))
    assert v.cls is ErrorClass.MODERATION_INPUT
    v2 = classify_exception(_ns(status_code=422, body={
        "error": {"type": "unprocessable_entity_error", "message": "max_tokens must be positive"}}))
    assert v2.cls is ErrorClass.BAD_REQUEST


def test_exc_status_fallbacks():
    assert classify_exception(_ns(status_code=401)).cls is ErrorClass.AUTH
    assert classify_exception(_ns(status_code=429)).cls is ErrorClass.RATE_LIMIT
    assert classify_exception(_ns(status_code=503)).cls is ErrorClass.SERVER
    assert classify_exception(_ns(status_code=529)).cls is ErrorClass.SERVER
    # 400/404/422 不做状态兜底：未识别 → None（保守退避，避免误停补跑）
    assert classify_exception(_ns(status_code=400, body={"error": {"message": "plain"}})) is None


def test_exc_defensive_bodies():
    """非 dict body 防御（对齐 moderation_hit_from_exception 的 P1 语义）"""
    assert classify_exception(_ns(body="Bad Gateway HTML")) is None
    assert classify_exception(_ns(body=[{"detail": "err"}])) is None
    assert classify_exception(_ns()) is None


# ===========================================================================
# 三、RETRY_POLICY 决策矩阵（§1.4）
# ===========================================================================

def test_retry_policy_matrix():
    stop = {ErrorClass.AUTH, ErrorClass.BILLING, ErrorClass.CONTEXT_LENGTH,
            ErrorClass.BAD_REQUEST, ErrorClass.MODERATION_INPUT, ErrorClass.MODERATION_OUTPUT}
    for cls, policy in RETRY_POLICY.items():
        if cls in stop:
            assert policy == "stop", cls
        elif cls is ErrorClass.RATE_LIMIT:
            assert policy == "retry_after"
        else:
            assert policy == "backoff", cls


# ===========================================================================
# 四、T2.1/T2.2 集成：流式生成器与重试循环
# ===========================================================================

def _make_cfg(**kw):
    from backend.config.settings import APIConfig
    defaults = dict(
        base_url="https://api.test.com", api_key="k", model="m",
        max_tokens=100, timeout=5,
        temperature_max_retries=3, backoff_max_retries=1,
    )
    defaults.update(kw)
    return APIConfig(**defaults)


def _api_error(msg, status=None, body=None, code=None, headers=None):
    from openai import APIError
    e = APIError(msg, request=None, body=body or {})
    if status is not None:
        e.status_code = status
    if code is not None:
        e.code = code
    if body is not None:
        e.body = body
    if headers is not None:
        e.response = MagicMock()
        e.response.headers = headers
    return e


async def test_stream_moderation_1027_marked():
    """T2.1 验收：MiniMax 1027 真实异常 → error chunk 带 [MODERATION]（本 plan 直接动机）"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(
        side_effect=_api_error("output new_sensitive (1027)",
                               body={"error": {"message": "output new_sensitive (1027)"}}))
    client.client = fake
    chunks = [c async for c in client.chat_stream([{"role": "user", "content": "x"}])]
    errs = [c for c in chunks if c.type == "error"]
    assert len(errs) == 1
    assert errs[0].error.startswith(MODERATION_MARKER)


async def test_stream_rate_limit_retry_after_extracted():
    """P3-9 验收：流式 429 提取 Retry-After 注入错误串（此前流式从未做过）"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=_api_error(
        "Error code: 429 - rate limit exceeded", status=429,
        body={"error": {"type": "rate_limit_error"}},
        headers={"retry-after": "42"}))
    client.client = fake
    chunks = [c async for c in client.chat_stream([{"role": "user", "content": "x"}])]
    errs = [c for c in chunks if c.type == "error"]
    assert len(errs) == 1
    assert "[Retry-After:42]" in errs[0].error


def _err_stream_gen(err):
    async def gen(*a, **k):
        from backend.core.llm_stream import StreamChunk
        yield StreamChunk(type="error", error=err)
    return gen


async def test_stream_retry_billing_stops_first_attempt():
    """T2.2 验收：402 billing 首次尝试即停（不再烧温度退火链）+ 可读提示"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    client.chat_stream = _err_stream_gen(
        "流式调用异常: APIStatusError: 402 - Error code: 402 - "
        "{'error': {'type': 'insufficient_balance_error'}}")
    client._sleep = AsyncMock()
    result = await client.chat_stream_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 1
    assert "余额不足" in result.error


async def test_stream_retry_moderation_structured_first_hit():
    """T2.2 验收：structured 审核（1027）单次调用即短路（原 13-16 次 → 1 次）"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    client.chat_stream = _err_stream_gen(
        f"{MODERATION_MARKER} 流式调用异常: APIError: output new_sensitive (1027)")
    client._sleep = AsyncMock()
    result = await client.chat_stream_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 1
    assert result.error.startswith(MODERATION_MARKER)


async def test_stream_retry_moderation_text_keeps_3_strike():
    """text 置信维持连续 3 次短路（test_moderation_threshold 的流式对齐，P3-8）"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    client.chat_stream = _err_stream_gen(
        mark_moderation("API错误: Request blocked by WAF"))
    client._sleep = AsyncMock()
    result = await client.chat_stream_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 3


async def test_nonstream_retry_bad_request_no_retry():
    """T2.2 验收（非流式）：模型参数错误不重试（原烧 13-16 次）"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    client.chat = AsyncMock(return_value=(
        False, "", "API错误 (HTTP 400): you must provide a model parameter", (0, 0)))
    client._sleep = AsyncMock()
    ok, _, error, _, stats = await client.chat_with_retry([{"role": "user", "content": "x"}])
    assert not ok
    assert stats["attempts"] == 1
    assert client.chat.call_count == 1


async def test_nonstream_retry_auth_stops():
    """AUTH 即停行为不回归"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    client.chat = AsyncMock(return_value=(
        False, "", "认证失败，请检查API Key: sk-***", (0, 0)))
    client._sleep = AsyncMock()
    ok, _, _, _, stats = await client.chat_with_retry([{"role": "user", "content": "x"}])
    assert not ok
    assert stats["attempts"] == 1


async def test_nonstream_retry_transient_still_backs_off():
    """UNKNOWN/transient 维持退避不回归（烧满温度链）"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    client.chat = AsyncMock(return_value=(False, "", "transient error", (0, 0)))
    client._sleep = AsyncMock()
    ok, _, _, _, stats = await client.chat_with_retry([{"role": "user", "content": "x"}])
    assert not ok
    assert stats["attempts"] == 4  # 3 温度 + 1 退避


# ===========================================================================
# 五、T2.4 集成：pipeline 补跑 fatal 过滤
# ===========================================================================

def _make_pipeline(tmp_path, skip_moderation=True):
    from backend.core.pipeline import AnalysisPipeline
    from backend.config.settings import AppConfig
    from backend.core.memory_state import MemoryState
    cfg = AppConfig()
    cfg.analysis.skip_moderation_blocked = skip_moderation
    pipeline = AnalysisPipeline(config=cfg, directory=tmp_path, knowledge_file=tmp_path / "k.json")
    pipeline.state = MemoryState()
    pipeline._block_map = {3: [3, 4]}
    pipeline.file_processor = MagicMock()
    pipeline.file_processor.read_block = AsyncMock(return_value="正文")
    return pipeline


def _fake_analyzer(err_by_block):
    analyzer = MagicMock()

    async def fake_analyze(block_id, content, kb, block_size=1, book_id=""):
        err = err_by_block.get(block_id, "transient error")
        return None, (10, 10), {"error": err, "retries": 1}
    analyzer.analyze_chapter = fake_analyze
    return analyzer


async def test_pipeline_billing_failure_registered_fatal(tmp_path):
    """T2.4 验收：billing 失败块登记 fatal，补跑列表剔除"""
    pipeline = _make_pipeline(tmp_path)
    billing_err = ("所有重试均失败。最后错误: 流式调用异常: APIStatusError: 402 - "
                   "{'error': {'type': 'insufficient_balance_error'}}（API 余额不足，请充值后重试）")
    analyzer = _fake_analyzer({3: billing_err})
    ok, *_ = await pipeline._analyze_one_block(
        analyzer, pipeline.file_processor, pipeline.state, 3, [3, 4], block_size=2)
    assert not ok
    assert 3 in pipeline.state._failed_chapters
    assert pipeline._fatal_blocks.get(3) == ErrorClass.BILLING.value
    assert pipeline._retryable_failed_blocks([3]) == []


async def test_pipeline_transient_failure_remains_retryable(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    analyzer = _fake_analyzer({3: "请求硬超时 (timeout=230s): API 无响应"})
    await pipeline._analyze_one_block(
        analyzer, pipeline.file_processor, pipeline.state, 3, [3, 4], block_size=2)
    assert 3 not in pipeline._fatal_blocks
    assert pipeline._retryable_failed_blocks([3]) == [3]


async def test_pipeline_moderation_not_fatal_when_config_off(tmp_path):
    """skip_moderation_blocked=False：审核块按用户要求补跑，不登记 fatal"""
    pipeline = _make_pipeline(tmp_path, skip_moderation=False)
    analyzer = _fake_analyzer({3: mark_moderation("API错误 (HTTP 400): content policy")})
    await pipeline._analyze_one_block(
        analyzer, pipeline.file_processor, pipeline.state, 3, [3, 4], block_size=2)
    assert 3 in pipeline.state._failed_chapters
    assert 3 not in pipeline._fatal_blocks
    assert pipeline._retryable_failed_blocks([3]) == [3]
