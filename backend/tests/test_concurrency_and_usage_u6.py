"""U6 验收：T03 并发不串判定 / T04 未知消耗不得宣称为零 / _decide_retry 类型守卫。

三部分：

1. T04（usage_status 贯通）：analyzer 的 retry_info 带上 usage_status，
   pipeline 的块级载荷（block_done / chapter token_stats）原样携带。
   记账口径保持 "拿不到 usage → tokens=(0, 0)"，但 "UNKNOWN" 让
   "没拿到用量" 与 "用量确实为零" 在数据上可区分，下游不得把 0 读成精确消耗。
2. T03（并发不串判定）：6 路 chat_with_retry 真并发，共用同一 LLMClient，
   SDK 层按消息里的 tag 分派各自的错误；断言每路只携带自己的
   cls / confidence / action / attempts，成功路 verdict 为 None。
   同时验证两块并发时 usage_status 与致命块登记互不串味。
3. _decide_retry 的入口类型守卫：ClassVerdict（策略层三元组）必须被拒收，
   不能被静默当成"不 stop"。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest

from backend.config.settings import APIConfig, AppConfig
from backend.core.error_taxonomy import ClassVerdict
from backend.core.error_types import (
    Confidence,
    ErrorClass,
    ErrorVerdict,
    SourceChannel,
    UsageStatus,
)
from backend.core.llm_client import LLMClient
from backend.models.analysis_result import AnalysisResult


# ===========================================================================
# 公共夹具
# ===========================================================================

JSON_TEXT = '{"core_events": ["事件"], "cross_block": {"summary": "概要"}, "long_context_insights": {}}'


def _norm(value) -> str:
    """把枚举 / 裸字符串归一成小写字符串。

    error_types.ErrorClass 与 error_taxonomy.ErrorClass 是两份**独立**的 str 枚举
    （取值逐一相同、`==` 为真、`is` 为假），断言用值比较才不会因身份不同误报。
    Confidence 同理：error_types 侧是大写成员值，策略层三元组侧是小写字面量。
    """
    return str(getattr(value, "value", value)).lower()


def _billing_verdict() -> ErrorVerdict:
    """结构化 billing 结论（SDK 402 通道），永久失败。"""
    return ErrorVerdict(
        cls=ErrorClass.BILLING,
        confidence=Confidence.STRUCTURED,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        provider_code="insufficient_quota",
        http_status=402,
        action="stop",
        message="余额不足",
    )


def _make_cfg(**kw) -> APIConfig:
    defaults = dict(
        base_url="https://api.test.com", api_key="k", model="m",
        max_tokens=100, timeout=5,
        temperature_max_retries=2, backoff_max_retries=1,
    )
    defaults.update(kw)
    return APIConfig(**defaults)


def _sdk_error(msg, status, body, cls=None):
    """构造带真实 HTTP 状态的 SDK 异常（结构化通道的输入端）。"""
    url = "https://api.test.com/v1/chat/completions"
    resp = httpx.Response(status, request=httpx.Request("POST", url), json=body)
    return (cls or openai.APIStatusError)(msg, response=resp, body=body)


def _ok_response(content=JSON_TEXT, prompt_tokens=11, completion_tokens=5):
    """成功响应：choices[0].message.content + usage（真实 chat() 的读取面）。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=content, reasoning_content=None))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


def _make_pipeline(tmp_path, progress=None, token_stats=None):
    from backend.core.memory_state import MemoryState
    from backend.core.pipeline import AnalysisPipeline

    pipeline = AnalysisPipeline(
        config=AppConfig(),
        directory=tmp_path,
        knowledge_file=tmp_path / "knowledge.json",
    )
    pipeline.state = MemoryState()
    pipeline.file_processor = MagicMock()
    pipeline.file_processor.read_block = MagicMock(return_value="正文")
    pipeline._on_progress = progress
    pipeline._on_token_stats = token_stats
    return pipeline


# ===========================================================================
# 一、T04：analyzer → retry_info 的 usage_status
# ===========================================================================

async def test_analyzer_success_keeps_numbers_and_reports_known():
    """成功且拿到 usage：usage_status=KNOWN，token 数字与改造前完全一致。"""
    from backend.core.analyzer import NovelAnalyzer
    from backend.core.llm_client import LLMResult

    analyzer = NovelAnalyzer(AppConfig())
    analyzer.llm_client = MagicMock()

    async def fake_chat_auto(messages, max_tokens=None, validate_response=None,
                             retry_messages_builder=None, on_progress=None):
        # 真实客户端会先跑 validator 再返回；这里同步跑一遍以填充 parsed_cache
        if validate_response is not None:
            ok, err = validate_response(JSON_TEXT)
            assert ok, f"validator 应通过：{err}"
        return LLMResult(
            True, JSON_TEXT, "",
            tokens=(11, 5), usage_status=UsageStatus.KNOWN,
            call_stats={"attempts": 2, "failed_tokens": 0},
        )

    analyzer.llm_client.chat_auto = fake_chat_auto
    analyzer.prompt_builder = MagicMock()
    analyzer.prompt_builder.build_messages = MagicMock(
        return_value=[{"role": "user", "content": "x"}])

    result, tokens, retry_info = await analyzer.analyze_chapter(1, "内容", MagicMock())

    assert result is not None
    assert tokens == (11, 5), f"成功路径的数字不得改变，实际 {tokens!r}"
    assert retry_info["usage_status"] == "KNOWN"
    assert retry_info["retries"] == 1
    assert "verdict" not in retry_info


async def test_analyzer_failure_without_usage_flags_unknown_sentinel():
    """没拿到 usage：tokens 仍是 (0, 0) 哨兵（记账口径不变），但必须标 UNKNOWN。"""
    from backend.core.analyzer import NovelAnalyzer
    from backend.core.llm_client import LLMResult

    analyzer = NovelAnalyzer(AppConfig())
    analyzer.llm_client = MagicMock()
    analyzer.llm_client.chat_auto = AsyncMock(return_value=LLMResult(
        False, "", "API错误 (HTTP 402): insufficient_quota",
        tokens=None, usage_status=UsageStatus.UNKNOWN,
        call_stats={"attempts": 3, "failed_tokens": 0},
        verdict=_billing_verdict(),
    ))
    analyzer.prompt_builder = MagicMock()
    analyzer.prompt_builder.build_messages = MagicMock(
        return_value=[{"role": "user", "content": "x"}])

    result, tokens, retry_info = await analyzer.analyze_chapter(1, "内容", MagicMock())

    assert result is None
    # 旧口径保留：不改成 None，否则十余处解包全炸
    assert tokens == (0, 0)
    # 新增的可区分位：0 是哨兵，不是"确实零消耗"
    assert retry_info["usage_status"] == "UNKNOWN"
    assert retry_info["verdict"]["cls"] == "billing"


async def test_analyzer_exception_path_flags_unknown():
    """异常兜底同样不得把"未知"报成零消耗：usage_status=UNKNOWN。"""
    from backend.core.analyzer import NovelAnalyzer

    analyzer = NovelAnalyzer(AppConfig())
    analyzer.llm_client = MagicMock()
    analyzer.llm_client.chat_auto = AsyncMock(side_effect=RuntimeError("boom"))
    analyzer.prompt_builder = MagicMock()
    analyzer.prompt_builder.build_messages = MagicMock(
        return_value=[{"role": "user", "content": "x"}])

    result, tokens, retry_info = await analyzer.analyze_chapter(1, "内容", MagicMock())

    assert result is None
    assert tokens == (0, 0)
    assert retry_info["usage_status"] == "UNKNOWN"


# ===========================================================================
# 二、T04：pipeline 块级载荷携带 usage_status
# ===========================================================================

async def test_pipeline_payloads_carry_usage_status(tmp_path):
    """块成功 → 载荷带 KNOWN 与原数字；块失败且未知 → 数字 0 但标 UNKNOWN。"""
    from backend.core.pipeline import PipelineStatus

    progress, token_stats = [], []

    async def on_progress(payload):
        progress.append(dict(payload))

    async def on_tokens(payload):
        token_stats.append(dict(payload))

    pipeline = _make_pipeline(tmp_path, on_progress, on_tokens)
    analyzer = MagicMock()

    async def fake_analyze(ch, content, kb, block_size=1, book_id=""):
        if ch == 1:
            return AnalysisResult(chapter_number=1), (10, 20), {
                "retries": 0, "failed_tokens": 0, "usage_status": "KNOWN"}
        return None, (0, 0), {
            "error": "API错误 (HTTP 402): insufficient_quota",
            "retries": 2, "failed_tokens": 0,
            "verdict": _billing_verdict().to_dict(),
            "usage_status": "UNKNOWN",
        }

    analyzer.analyze_chapter = fake_analyze

    for bid in (1, 2):
        ok, elapsed, ch_tokens, result, retry_info = await pipeline._analyze_one_block(
            analyzer, pipeline.file_processor, pipeline.state, bid, [bid], block_size=1)
        await pipeline._handle_block_outcome(
            success=ok, block_id=bid, block_chs=[bid], block_size=1, total=2,
            label="", completed_count=0, elapsed=elapsed,
            ch_tokens=ch_tokens, result=result, retry_info=retry_info)

    done = [p for p in progress if p["status"] == PipelineStatus.DONE]
    failed = [p for p in progress if p["status"] == PipelineStatus.FAILED]
    assert len(done) == 1 and len(failed) == 1

    # 成功块：数字必须与改造前一致，并显式标 KNOWN
    assert done[0]["usage_status"] == "KNOWN"
    assert (done[0]["input_tokens"], done[0]["output_tokens"]) == (10, 20)
    assert done[0]["tokens"] == 30

    # 失败块：既没有 token 数字，也不会宣称"消耗 0"
    assert "tokens" not in failed[0]
    assert "input_tokens" not in failed[0]

    # token 统计：两块各一条，未知块带 UNKNOWN 标记（数字仍是 0，但语义不同）
    chapters = [t for t in token_stats if t["category"] == "chapter"]
    assert len(chapters) == 2
    known, unknown = chapters[0], chapters[1]
    assert known["usage_status"] == "KNOWN"
    assert (known["input_tokens"], known["output_tokens"]) == (10, 20)
    assert unknown["usage_status"] == "UNKNOWN"
    assert (unknown["input_tokens"], unknown["output_tokens"]) == (0, 0)
    assert unknown["current_tokens"] == 0


async def test_concurrent_blocks_do_not_cross_usage_or_verdict(tmp_path):
    """两块真并发：一块成功 KNOWN、一块失败 UNKNOWN，结果不互相串味。"""
    from backend.core.pipeline import PipelineStatus

    progress, token_stats = [], []

    async def on_progress(payload):
        progress.append(dict(payload))

    async def on_tokens(payload):
        token_stats.append(dict(payload))

    pipeline = _make_pipeline(tmp_path, on_progress, on_tokens)
    analyzer = MagicMock()

    async def fake_analyze(ch, content, kb, block_size=1, book_id=""):
        await asyncio.sleep(0)  # 强制交错，暴露共享状态
        if ch == 11:
            return AnalysisResult(chapter_number=11), (3, 4), {
                "retries": 0, "failed_tokens": 0, "usage_status": "KNOWN"}
        return None, (0, 0), {
            "error": "API错误 (HTTP 402): insufficient_quota",
            "retries": 2, "failed_tokens": 0,
            "verdict": _billing_verdict().to_dict(),
            "usage_status": "UNKNOWN",
        }

    analyzer.analyze_chapter = fake_analyze

    async def run_block(bid):
        ok, elapsed, ch_tokens, result, retry_info = await pipeline._analyze_one_block(
            analyzer, pipeline.file_processor, pipeline.state, bid, [bid], block_size=1)
        await asyncio.sleep(0)
        await pipeline._handle_block_outcome(
            success=ok, block_id=bid, block_chs=[bid], block_size=1, total=2,
            label="", completed_count=0, elapsed=elapsed,
            ch_tokens=ch_tokens, result=result, retry_info=retry_info)

    await asyncio.gather(run_block(11), run_block(12))

    by_chapter = {p["chapter"]: p for p in progress if p["chapter"] in (11, 12)
                  and p["status"] in (PipelineStatus.DONE, PipelineStatus.FAILED)}
    assert by_chapter[11]["status"] == PipelineStatus.DONE
    assert by_chapter[11]["usage_status"] == "KNOWN"
    assert by_chapter[11]["tokens"] == 7
    assert by_chapter[12]["status"] == PipelineStatus.FAILED
    assert "tokens" not in by_chapter[12]

    tok_by_usage = sorted(t["usage_status"] for t in token_stats
                          if t["category"] == "chapter")
    assert tok_by_usage == ["KNOWN", "UNKNOWN"]
    known = [t for t in token_stats if t.get("usage_status") == "KNOWN"][0]
    unknown = [t for t in token_stats if t.get("usage_status") == "UNKNOWN"][0]
    assert (known["input_tokens"], known["output_tokens"]) == (3, 4)
    assert (unknown["input_tokens"], unknown["output_tokens"]) == (0, 0)

    # 致命块登记只落在失败块上（billing 结构化 stop）
    assert pipeline._fatal_blocks == {12: "billing"}, pipeline._fatal_blocks


# ===========================================================================
# 三、T03：6 路并发 chat_with_retry 不串判定
# ===========================================================================

TAG_PREFIX = "u6-case:"

# 期望值（与下面 dispatcher 的构造一一对应）：
#   success        : 一次成功                                    → attempts=1
#   structured_402 : 温度层第 0 轮即 stop（billing 结构化）        → attempts=1
#   structured_401 : 温度层第 0 轮即 stop（auth 结构化）           → attempts=1
#   text_billing   : 温度层 2 轮 + 退避 1 轮（文本通道恒不 stop）   → attempts=3
#   rate_limit_429 : 温度层 2 轮 + 退避 max(1,3)=3 轮              → attempts=5
#   server_500     : 温度层 2 轮 + 退避 1 轮                       → attempts=3
CASES = ("success", "structured_402", "structured_401",
         "text_billing", "rate_limit_429", "server_500")


def _messages_for(case: str):
    return [
        {"role": "system", "content": "你是小说分析器"},
        {"role": "user", "content": f"{TAG_PREFIX}{case}"},
    ]


def _make_dispatcher():
    """按消息 tag 分派 6 种结果；记录并发峰值以证明 6 路真的在交错执行。"""
    state = {"active": 0, "peak": 0}

    async def _create(*args, **kwargs):
        messages = kwargs.get("messages") or (args[1] if len(args) > 1 else [])
        # tag 在 user 消息里（system 在本用例前置），故拼接全部消息再取
        joined = " ".join(str(m.get("content", "")) for m in messages
                          if isinstance(m, dict))
        tag = joined.split(TAG_PREFIX, 1)[-1].strip()
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(0)  # 让 6 路真正交错，任何共享判定都会被这里放大
            if tag == "structured_402":
                raise _sdk_error("余额不足", 402, {"error": {
                    "code": "insufficient_quota", "type": "insufficient_quota"}})
            if tag == "structured_401":
                raise _sdk_error("Unauthorized", 401, {"error": {
                    "code": "invalid_api_key", "type": "invalid_request_error"}},
                    cls=openai.AuthenticationError)
            if tag == "text_billing":
                # 非 SDK 异常（无 code/status/body 属性）→ 只能走文本通道
                raise RuntimeError("API错误 (HTTP 402): insufficient_quota 余额不足")
            if tag == "rate_limit_429":
                raise _sdk_error("Too Many Requests", 429, {"error": {
                    "code": "rate_limit_exceeded", "type": "rate_limit_error"}})
            if tag == "server_500":
                raise _sdk_error("Internal Server Error", 500, {"error": {
                    "code": "server_error", "type": "server_error"}})
            return _ok_response()
        finally:
            state["active"] -= 1

    return _create, state


async def test_concurrent_chat_with_retry_verdicts_do_not_cross():
    """6 路并发：每路只携带自己的 cls/confidence/action/attempts，成功路 verdict 为 None。"""
    dispatcher, state = _make_dispatcher()
    client = LLMClient(_make_cfg())
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=dispatcher)
    client.client = fake
    client._sleep = AsyncMock()

    results = await asyncio.gather(
        *[client.chat_with_retry(_messages_for(c)) for c in CASES])
    by = dict(zip(CASES, results))

    # 真的并发过（不是被串行化后恰好也正确）
    assert state["peak"] >= 2, f"并发峰值过低，用例未真正交错：{state}"

    # --- 成功路：verdict 为 None，usage 已知 ---
    ok_result = by["success"]
    assert ok_result.success is True
    assert ok_result.verdict is None
    assert ok_result.tokens == (11, 5)
    assert ok_result.usage_status is UsageStatus.KNOWN
    assert ok_result.attempts == 1

    # --- 结构化 402：billing + structured + stop，一次即停 ---
    s402 = by["structured_402"]
    assert s402.success is False
    assert s402.attempts == 1
    assert _norm(s402.verdict.cls) == "billing"
    assert _norm(s402.verdict.confidence) == "structured"
    assert s402.verdict.action == "stop"
    assert s402.verdict.is_permanent is True
    # 没拿到 usage：tokens 保持 None + UNKNOWN，不伪造成 0
    assert s402.tokens is None
    assert s402.usage_status is UsageStatus.UNKNOWN

    # --- 结构化 401：auth + structured + stop ---
    s401 = by["structured_401"]
    assert s401.success is False
    assert s401.attempts == 1
    assert _norm(s401.verdict.cls) == "auth"
    assert _norm(s401.verdict.confidence) == "structured"
    assert s401.verdict.action == "stop"
    assert s401.verdict.is_permanent is True

    # --- 文本 BILLING：同一个 cls，但文本通道 → 重试且永不永久 ---
    tb = by["text_billing"]
    assert tb.success is False
    assert tb.attempts == 3, f"温度 2 轮 + 退避 1 轮，实际 attempts={tb.attempts}"
    assert _norm(tb.verdict.cls) == "billing"
    assert _norm(tb.verdict.confidence) == "text"
    assert tb.verdict.action != "stop"
    assert tb.verdict.is_permanent is False
    # 关键的不串判定断言：两路 cls 相同，但 confidence/action/attempts 各自属于自己
    assert (s402.verdict.confidence, s402.attempts) != (tb.verdict.confidence, tb.attempts)

    # --- 429：限流，非永久，退避轮 max(1,3)=3 ---
    rl = by["rate_limit_429"]
    assert rl.success is False
    assert rl.attempts == 5, f"温度 2 轮 + 退避 3 轮，实际 attempts={rl.attempts}"
    assert _norm(rl.verdict.cls) == "rate_limit"
    assert rl.verdict.is_permanent is False
    assert rl.verdict.action != "stop"

    # --- 500：服务端错误，非永久 ---
    srv = by["server_500"]
    assert srv.success is False
    assert srv.attempts == 3, f"温度 2 轮 + 退避 1 轮，实际 attempts={srv.attempts}"
    assert _norm(srv.verdict.cls) == "server"
    assert srv.verdict.is_permanent is False
    assert srv.verdict.action != "stop"

    # --- 全表逐项核对：没有一路拿到别路的 billing verdict ---
    # 注：402/401 在温度层首轮就 stop，携带的是**结构化**结论；429/500 是"重试耗尽"收尾，
    # 末端用 classify_message(最后一次错误串) 兜底，故 confidence=text（仍不永久、不 stop）。
    expected = {
        "success": (None, None, 1),
        "structured_402": ("billing", "structured", 1),
        "structured_401": ("auth", "structured", 1),
        "text_billing": ("billing", "text", 3),
        "rate_limit_429": ("rate_limit", "text", 5),
        "server_500": ("server", "text", 3),
    }
    for case, (cls, conf, attempts) in expected.items():
        r = by[case]
        assert r.attempts == attempts, f"{case}: attempts 期望 {attempts}，实际 {r.attempts}"
        if cls is None:
            assert r.verdict is None, f"{case}: 不应带 verdict，实际 {r.verdict!r}"
        else:
            assert r.verdict is not None, f"{case}: 缺少 verdict"
            assert _norm(r.verdict.cls) == cls, (
                f"{case}: cls 期望 {cls}，实际 {_norm(r.verdict.cls)}")
            if conf is not None:
                assert _norm(r.verdict.confidence) == conf, (
                    f"{case}: confidence 期望 {conf}，实际 {_norm(r.verdict.confidence)}")


# ===========================================================================
# 四、_decide_retry 的类型守卫
# ===========================================================================

def _decide_kwargs(**kw):
    base = dict(
        moderation_hits=0, attempt=0, total_attempts=1, temperature=0.7,
        messages_length=100, phase="temperature", moderation_log="测试",
    )
    base.update(kw)
    return base


def test_decide_retry_rejects_class_verdict():
    """传策略层 ClassVerdict → TypeError（不得被静默降级成"不 stop"）。"""
    client = LLMClient(_make_cfg())
    client._sleep = AsyncMock()

    with pytest.raises(TypeError) as ei:
        client._decide_retry(
            "API错误 (HTTP 402): insufficient_quota",
            verdict=ClassVerdict(ErrorClass.BILLING, "structured"),
            **_decide_kwargs(),
        )
    msg = str(ei.value)
    assert "ErrorVerdict" in msg
    assert "ClassVerdict" in msg
    assert "_to_error_verdict" in msg


def test_decide_retry_accepts_error_verdict_and_none():
    """守卫放行 None（文本通道自行分类）与 ErrorVerdict（原样消费）。"""
    client = LLMClient(_make_cfg())
    client._sleep = AsyncMock()

    text_decision = client._decide_retry(
        "API错误 (HTTP 402): insufficient_quota",
        verdict=None,
        **_decide_kwargs(),
    )
    assert text_decision.action == "continue"
    assert _norm(text_decision.verdict.confidence) == "text"

    stop_decision = client._decide_retry(
        "API错误 (HTTP 402): insufficient_quota",
        verdict=_billing_verdict(),
        **_decide_kwargs(),
    )
    assert stop_decision.action == "stop"
    assert _norm(stop_decision.verdict.cls) == "billing"
    assert _norm(stop_decision.verdict.confidence) == "structured"
