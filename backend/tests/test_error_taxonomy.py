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

from backend.core.error_types import LLMResult
from backend.core.error_taxonomy import (
    ErrorClass, RETRY_POLICY, classify_exception, classify_message, resolve_action,
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

def test_msg_moderation_1027_text_only():
    """MiniMax 输出涉敏（§0.1 表 558 条）。
    >>> 旧契约改写（C-01）：断言 confidence == "structured" 依赖"消息码串扫描"把文本里的
    裸数字 1027 当厂商码——那正是正文数字被判审核的通道；现改为 text 置信 + 非 stop。"""
    v = classify_message(f"{MODERATION_MARKER} 流式调用异常: APIError: output new_sensitive (1027)")
    assert v.cls is ErrorClass.MODERATION_OUTPUT
    assert v.confidence == "text"
    assert v.action != "stop"


def test_msg_moderation_422_input_text():
    """MiniMax Anthropic 皮肤输入拦截（§0.1 表 ~5525 条，P2-1）。
    >>> 旧契约改写（C-01）：文本通道不再给 structured 置信，也不靠裸数字 "422" 触发。"""
    v = classify_message("流式调用异常: UnprocessableEntityError: 422 "
                         "{'error': {'type': 'unprocessable_entity_error', 'message': 'input new_sensitive detected'}}")
    assert v.cls is ErrorClass.MODERATION_INPUT
    assert v.confidence == "text"
    assert v.action != "stop"


def test_msg_billing_402_text():
    """>>> 旧契约改写（C-01）：类判定不变（BILLING），但文本通道置信为 text、
    action 降级为 limited_retry——是否停止由调用方按预算决定。"""
    v = classify_message("流式调用异常: APIStatusError: 402 - Error code: 402 - "
                         "{'error': {'type': 'insufficient_balance_error', 'message': 'insufficient balance'}}")
    assert v.cls is ErrorClass.BILLING
    assert v.confidence == "text"
    assert v.action == "limited_retry"


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


async def test_stream_retry_billing_structured_stops_first_attempt():
    """T2.2 验收（结构化输入）：真实 402 billing 首次尝试即停（不再烧温度退火链）+ 可读提示

    U2（C-01）改写：原写法把纯文本错误串 mock 进 chat_stream，靠"文本通道也能 stop"才通过。
    文本通道被禁止 stop 后必须走真实结构化通道：mock 底层 SDK 抛带 402 状态的异常，
    chat_stream() 在异常对象还在场时 classify_exception 得到 confidence=structured，
    经结构化结论槽传给重试循环 → 业务意图（真实欠费首次即停）不变。"""
    from backend.core.llm_client import LLMClient
    exc = _api_error("Error code: 402 - insufficient balance", status=402,
                     body={"error": {"type": "insufficient_balance_error",
                                     "message": "insufficient balance"}})
    assert classify_exception(exc).is_permanent() is True  # 先确认走的是结构化停止
    client = LLMClient(_make_cfg())
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=exc)
    client.client = fake
    client._sleep = AsyncMock()
    result = await client.chat_stream_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 1
    assert "余额不足" in result.error


async def test_stream_retry_moderation_1027_text_keeps_3_strike():
    """>>> 旧契约改写（C-01）：旧断言 attempts == 1 依赖"文本里的 1027 数字 → structured 置信
    → 审核首次即短路"。数字退出文本通道后，这条路径回到 text 三分支（连续 3 次才算
    文本审核拦截），与 test_stream_retry_moderation_text_keeps_3_strike 同档。"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    client.chat_stream = _err_stream_gen(
        f"{MODERATION_MARKER} 流式调用异常: APIError: output new_sensitive (1027)")
    client._sleep = AsyncMock()
    result = await client.chat_stream_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 3
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


async def test_nonstream_retry_bad_request_structured_no_retry():
    """T2.2 验收（非流式，结构化输入）：模型参数错误不重试（原烧 13-16 次）

    U2（C-01）改写：原写法用纯文本"API错误 (HTTP 400): you must provide a model parameter"，
    靠"文本通道也能判 BAD_REQUEST=stop"才通过。400 不在状态兜底表里，所以这里 mock 底层
    SDK 抛 400 + body 码 invalid_parameter，走结构化通道 → 仍然首次即停（不重试）。"""
    from backend.core.llm_client import LLMClient
    exc = _api_error("Error code: 400 - you must provide a model parameter", status=400,
                     body={"error": {"code": "invalid_parameter",
                                     "message": "you must provide a model parameter"}})
    assert classify_exception(exc).is_permanent() is True
    client = LLMClient(_make_cfg())
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=exc)
    client.client = fake
    client._sleep = AsyncMock()
    result = await client.chat_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 1  # 未重试
    assert fake.chat.completions.create.call_count == 1


async def test_nonstream_retry_auth_structured_stops():
    """AUTH 即停行为不回归（结构化通道：真实 401）

    U2（C-01）改写：原写法用纯文本"认证失败，请检查API Key: sk-***"，靠文本通道也能 stop。
    文本通道现在只能有限重试，因此这里 mock 底层 SDK 抛真实 AuthenticationError(401)，
    由 chat() 的 AuthenticationError 分支在异常对象还在场时拿到 structured 结论 → 首次即停。"""
    import httpx
    from openai import AuthenticationError
    from backend.core.llm_client import LLMClient
    body = {"error": {"message": "Invalid API key", "code": "invalid_api_key"}}
    resp = httpx.Response(401,
                          request=httpx.Request("POST", "https://api.test.com/v1/chat/completions"),
                          json=body)
    exc = AuthenticationError("Invalid API key", response=resp, body=body)
    assert classify_exception(exc).is_permanent() is True
    client = LLMClient(_make_cfg())
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=exc)
    client.client = fake
    client._sleep = AsyncMock()
    result = await client.chat_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 1
    assert fake.chat.completions.create.call_count == 1
    assert "认证失败" in result.error


async def test_nonstream_retry_transient_still_backs_off():
    """UNKNOWN/transient 维持退避不回归（烧满温度链）"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    client.chat = AsyncMock(return_value=LLMResult(False, "", "transient error"))
    client._sleep = AsyncMock()
    result = await client.chat_with_retry([{"role": "user", "content": "x"}])
    assert not result.success
    assert result.call_stats["attempts"] == 4  # 3 温度 + 1 退避


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


async def test_pipeline_text_billing_not_registered_fatal(tmp_path):
    """U4（C-01 上游收口）：文本通道的 billing 失败不再登记 fatal

    原 T2.4 断言"billing 错误串 → _fatal_blocks 登记"，那正是 C-01 漏判在上游的残迹：
    pipeline 只拿到错误串，走 classify_message 的结论恒为 text 置信，而文本通道永不 stop。
    现在判据是 ClassVerdict.is_permanent()（structured + stop），所以纯文本欠费块只是普通
    失败，补跑轮仍会重试——真正的结构化 402 已经在 llm_client 里首次即停，不会烧满重试链。
    """
    pipeline = _make_pipeline(tmp_path)
    billing_err = ("所有重试均失败。最后错误: 流式调用异常: APIStatusError: 402 - "
                   "{'error': {'type': 'insufficient_balance_error'}}（API 余额不足，请充值后重试）")
    analyzer = _fake_analyzer({3: billing_err})
    ok, *_ = await pipeline._analyze_one_block(
        analyzer, pipeline.file_processor, pipeline.state, 3, [3, 4], block_size=2)
    assert not ok
    assert 3 in pipeline.state._failed_chapters
    assert pipeline._fatal_blocks == {}
    assert pipeline._retryable_failed_blocks([3]) == [3]


async def test_retryable_failed_blocks_excludes_fatal(tmp_path):
    """补跑剔除逻辑本身保持覆盖：fatal 块不出现在补跑列表里"""
    pipeline = _make_pipeline(tmp_path)
    pipeline._fatal_blocks[3] = ErrorClass.BILLING.value
    assert pipeline._retryable_failed_blocks([3, 4]) == [4]


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


# ===========================================================================
# 六、C-01 来源分级契约（2026-10-30）：文本通道永远不得 stop
# ===========================================================================

# 真实正文串：曾被判 AUTH / BILLING / MODERATION_INPUT，导致整块永久失败（C-01）
_C01_NOVEL_TEXTS = [
    "第 401 章 我叫王二",
    "这批货赚了 1008 块",
    "总字数 1026 万，还在连载",
]

# 只能由可信通道产出的"永久停止"类分类
_PERMANENT_CLASSES = {
    ErrorClass.AUTH, ErrorClass.BILLING, ErrorClass.CONTEXT_LENGTH,
    ErrorClass.BAD_REQUEST, ErrorClass.MODERATION_INPUT, ErrorClass.MODERATION_OUTPUT,
}


def test_c01_novel_text_never_permanent():
    """负样本回归：正文里的数字不得被判成永久错误类别，更不得 stop"""
    for text in _C01_NOVEL_TEXTS:
        v = classify_message(text)
        assert v.cls not in _PERMANENT_CLASSES, (text, v)
        assert v.cls is ErrorClass.UNKNOWN, (text, v)
        assert v.confidence == "none", (text, v)
        assert v.action != "stop", (text, v)


def test_c01_number_bearing_text_never_stop():
    """数字出现在正文语境时，无论落在哪个类别都不得给出 stop"""
    for text in ("第 401 章 我叫王二", "第 1039 章 突破", "第 503 章 归来",
                 "售价 1008 元", "总字数 1026 万", "重试 1000 次后仍失败"):
        v = classify_message(text)
        assert v.action != "stop", (text, v)
        assert v.cls not in _PERMANENT_CLASSES, (text, v)


def test_c01_text_channel_never_stop():
    """文本通道即便命中命名错误码与真实措辞，action 也不得是 stop"""
    probes = [
        "API错误: insufficient_quota",
        "API错误: invalid_api_key",
        "API错误: context_length_exceeded",
        "API错误: invalid_parameter",
        "认证失败，请检查API Key: sk-***",
        "API错误 (HTTP 400): content policy",
        "流式调用异常: output new_sensitive (1027)",
        "transient error",
        "",
    ]
    for text in probes:
        v = classify_message(text)
        assert v.action != "stop", (text, v)
        assert v.confidence in ("text", "none"), (text, v)


def test_c01_structured_channel_keeps_stop():
    """可信通道保留永久判定：结构化数字码 / 真实 HTTP 状态码仍可 stop"""
    assert classify_exception(_ns(code="1026")).action == "stop"            # MODERATION_INPUT
    assert classify_exception(_ns(body={"base_resp": {"status_code": 1008}})).action == "stop"
    v = classify_exception(_ns(status_code=401))
    assert (v.cls, v.confidence, v.action) == (ErrorClass.AUTH, "structured", "stop")
    v = classify_exception(_ns(status_code=429))
    assert (v.cls, v.confidence, v.action) == (ErrorClass.RATE_LIMIT, "structured", "retry_after")


def test_c01_action_cap_matrix():
    """resolve_action：structured 取 RETRY_POLICY，text 降级，none 走有限重试"""
    assert resolve_action(ErrorClass.BILLING, "structured") == "stop"
    assert resolve_action(ErrorClass.MODERATION_INPUT, "structured") == "stop"
    assert resolve_action(ErrorClass.BILLING, "text") == "limited_retry"
    assert resolve_action(ErrorClass.MODERATION_INPUT, "text") == "limited_retry"
    assert resolve_action(ErrorClass.RATE_LIMIT, "text") == "retry_after"
    assert resolve_action(ErrorClass.SERVER, "text") == "backoff"
    assert resolve_action(ErrorClass.UNKNOWN, "text") == "limited_retry"
    assert resolve_action(ErrorClass.UNKNOWN, "none") == "limited_retry"


def test_c01_is_permanent_requires_structured():
    """永久判定只认结构化通道：文本通道的 AUTH/BILLING 一律 not permanent"""
    assert classify_exception(_ns(status_code=401)).is_permanent() is True
    assert classify_exception(_ns(code="1026")).is_permanent() is True
    assert classify_message("认证失败，请检查API Key: sk-***").is_permanent() is False
    assert classify_message("API错误: insufficient_quota").is_permanent() is False
    assert classify_message("transient error").is_permanent() is False


def test_c01_message_scan_table_has_no_bare_digits():
    """守护：消息扫描表只收命名码；纯数字码只留在结构化码表（BODY_CODE_MAP）"""
    from backend.core.error_taxonomy import BODY_CODE_MAP, _MESSAGE_SCAN_CODES
    assert not [code for code in _MESSAGE_SCAN_CODES if code.isdigit()]
    for code in ("1026", "1027", "1008", "1113", "1039", "1261", "20012"):
        assert code in BODY_CODE_MAP          # structured 通道的识别能力不受影响
        assert code not in _MESSAGE_SCAN_CODES


def test_c01_positive_error_wording_still_recognized():
    """正样本回归：命名错误码与明确措辞的识别能力不砍掉（只是不再 stop）"""
    assert classify_message(
        "API错误 (HTTP 429): {'error': {'code': 'insufficient_quota'}}").cls is ErrorClass.BILLING
    assert classify_message("API错误: rate_limit_exceeded").cls is ErrorClass.RATE_LIMIT
    assert classify_message("API错误: invalid_api_key").cls is ErrorClass.AUTH
    assert classify_message("API错误 (HTTP 401): Unauthorized").cls is ErrorClass.AUTH
    assert classify_message("API错误: input new_sensitive detected").cls is ErrorClass.MODERATION_INPUT
    assert classify_message("未知错误: 内容不符合安全规范").cls is ErrorClass.MODERATION_OUTPUT
    assert classify_message("API错误 (HTTP 503): 服务不可用").cls is ErrorClass.SERVER
    assert classify_message("请求硬超时 (timeout=230s): API 无响应").cls is ErrorClass.TIMEOUT
