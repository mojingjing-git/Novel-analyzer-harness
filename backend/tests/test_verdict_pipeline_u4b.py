"""
U4b：结构化 verdict 贯通 analyzer → pipeline 的回归测试。

背景：pipeline 判定"永久失败"块（登记进 _fatal_blocks，补跑轮据此跳过）时，
原先只能拿到错误**字符串**，文本通道判定恒不 permanent，分支永远不成立。
U4 把 llm_result.verdict（结构化结论）从 analyzer 透传到 pipeline，
于是"欠费 / 上下文超长"这类错误不再在补跑轮重复烧钱。

覆盖三件事：
1. ErrorVerdict.is_permanent——判断永久停止的唯一谓词（structured + action=stop）
2. analyzer 失败时 retry_info 透传 verdict（dict）与 usage_status
3. pipeline 致命块登记：结构化 billing verdict → 登记；仅文本错误串 → 不登记
"""
from unittest.mock import AsyncMock, MagicMock

from backend.core.error_types import (
    Confidence,
    ErrorClass,
    ErrorVerdict,
    SourceChannel,
    UsageStatus,
)


# ---------------------------------------------------------------------------
# 1. ErrorVerdict.is_permanent
# ---------------------------------------------------------------------------

def test_is_permanent_true_only_for_structured_stop():
    """structured + action=stop → True；TEXT 通道恒 False；structured 但非 stop → False"""
    stop = ErrorVerdict(
        cls=ErrorClass.BILLING,
        confidence=Confidence.STRUCTURED,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        provider_code="insufficient_quota",
        http_status=402,
        action="stop",
        message="余额不足",
    )
    assert stop.is_permanent is True

    # 文本通道即便 action 写成 stop 也恒不永久（唯一谓词要求 confidence=structured）
    text = ErrorVerdict(
        cls=ErrorClass.BILLING,
        confidence=Confidence.TEXT,
        source_channel=SourceChannel.TEXT_FALLBACK,
        action="stop",
        message="余额不足",
    )
    assert text.is_permanent is False

    # structured 但不是 stop（backoff / limited_retry）→ 不是永久失败
    backoff = ErrorVerdict(
        cls=ErrorClass.BILLING,
        confidence=Confidence.STRUCTURED,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        action="backoff",
    )
    assert backoff.is_permanent is False


# ---------------------------------------------------------------------------
# 2. analyzer → retry_info 的 verdict / usage_status 透传
# ---------------------------------------------------------------------------

async def test_analyzer_retry_info_carries_verdict_and_usage_status():
    """analyze_chapter 失败时 retry_info["verdict"] 是 dict，retry_info["usage_status"] 存在"""
    from backend.config.settings import AppConfig
    from backend.core.analyzer import NovelAnalyzer
    from backend.core.llm_client import LLMResult

    analyzer = NovelAnalyzer(AppConfig())
    analyzer.llm_client = MagicMock()
    verdict = ErrorVerdict(
        cls=ErrorClass.BILLING,
        confidence=Confidence.STRUCTURED,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        provider_code="insufficient_quota",
        http_status=402,
        action="stop",
        message="余额不足",
    )
    analyzer.llm_client.chat_auto = AsyncMock(return_value=LLMResult(
        False, "", "API错误 (HTTP 402): insufficient_quota",
        tokens=None, usage_status=UsageStatus.UNKNOWN,
        call_stats={"attempts": 2, "failed_tokens": 0},
        verdict=verdict,
    ))
    analyzer.prompt_builder = MagicMock()
    analyzer.prompt_builder.build_messages = MagicMock(
        return_value=[{"role": "user", "content": "x"}])

    result, _tokens, retry_info = await analyzer.analyze_chapter(1, "内容", MagicMock())

    assert result is None
    verdict_dict = retry_info.get("verdict")
    assert isinstance(verdict_dict, dict), f"verdict 应为 dict，实际 {verdict_dict!r}"
    assert verdict_dict["cls"] == "billing"
    assert verdict_dict["confidence"] == "STRUCTURED"
    assert verdict_dict["action"] == "stop"
    assert "usage_status" in retry_info
    assert retry_info["usage_status"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# 3. pipeline 的致命块登记（结构化优先 / 文本兜底）
# ---------------------------------------------------------------------------

def _make_pipeline(tmp_path):
    from backend.config.settings import AppConfig
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
    return pipeline


async def test_pipeline_registers_structured_billing_as_fatal(tmp_path):
    """带结构化 billing verdict 的失败结果 → _fatal_blocks 出现该 block_id"""
    pipeline = _make_pipeline(tmp_path)

    verdict = ErrorVerdict(
        cls=ErrorClass.BILLING,
        confidence=Confidence.STRUCTURED,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        provider_code="insufficient_quota",
        http_status=402,
        action="stop",
        message="余额不足",
    )

    analyzer = MagicMock()

    async def fake_analyze(ch, content, kb, block_size=1, book_id=""):
        return None, (10, 10), {
            "error": "API错误 (HTTP 402): insufficient_quota",
            "retries": 2,
            "failed_tokens": 0,
            "verdict": verdict.to_dict(),
            "usage_status": "UNKNOWN",
        }

    analyzer.analyze_chapter = fake_analyze

    ok, _elapsed, _tokens, _result, _retry_info = await pipeline._analyze_one_block(
        analyzer, pipeline.file_processor, pipeline.state, 7, [7, 8], block_size=2)

    assert ok is False
    assert 7 in pipeline._fatal_blocks, f"结构化 billing verdict 应登记致命块：{pipeline._fatal_blocks}"
    assert pipeline._fatal_blocks[7] == "billing"


async def test_pipeline_text_only_error_not_registered_as_fatal(tmp_path):
    """只有文本错误串的结果 → 文本通道恒不永久 → 不登记致命块（补跑轮照常可重试）"""
    pipeline = _make_pipeline(tmp_path)

    analyzer = MagicMock()

    async def fake_analyze(ch, content, kb, block_size=1, book_id=""):
        return None, (10, 10), {
            "error": "API错误 (HTTP 402): insufficient_quota",
            "retries": 2,
            "failed_tokens": 0,
        }

    analyzer.analyze_chapter = fake_analyze

    ok, _elapsed, _tokens, _result, _retry_info = await pipeline._analyze_one_block(
        analyzer, pipeline.file_processor, pipeline.state, 7, [7, 8], block_size=2)

    assert ok is False
    assert 7 not in pipeline._fatal_blocks, (
        f"纯文本判定恒不永久，不应登记致命块：{pipeline._fatal_blocks}")
