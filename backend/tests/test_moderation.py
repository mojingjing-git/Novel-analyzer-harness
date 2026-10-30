"""
内容审核拦截识别测试：分类器 + chat 标记注入 + 重试短路 + pipeline 跳过
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core.error_types import LLMResult, UsageStatus
from backend.core.moderation import (
    is_moderation_code, is_moderation_message,
    mark_moderation, is_moderation_error,
)


def test_structured_codes():
    # MiniMax 1026/1027（输入/输出涉敏）
    assert is_moderation_code("1026")
    assert is_moderation_code("1027")
    # OpenAI 系错误码
    assert is_moderation_code("content_policy_violation")
    assert is_moderation_code("content_filter")
    assert is_moderation_code("safety")
    # 非审核错误码
    assert not is_moderation_code("1008")       # 余额不足
    assert not is_moderation_code("rate_limit_exceeded")
    assert not is_moderation_code(None)


def test_chinese_keywords():
    # 智谱风格中文消息
    assert is_moderation_message("内容不符合安全规范")
    assert is_moderation_message("请求内容包含敏感信息，已拒绝生成")
    assert is_moderation_message("高风险内容，无法生成")
    assert is_moderation_message("当前内容涉及违规")
    assert not is_moderation_message("服务器繁忙，请稍后再试")


def test_english_keywords():
    # 小米/OpenAI 风格英文消息
    assert is_moderation_message("high risk content detected")
    assert is_moderation_message("This request violates our safety policy")
    assert is_moderation_message("content filtered by moderation")
    assert not is_moderation_message("The model is overloaded, retry later")


def test_marker_roundtrip():
    err = "API错误 (HTTP 400): bad"
    marked = mark_moderation(err)
    assert marked.startswith("[MODERATION]")
    assert is_moderation_error(marked)
    assert not is_moderation_error(err)
    # 幂等
    assert mark_moderation(marked) == marked


def _api_error(code=None, status=400, msg="bad"):
    """构造真实 openai.APIError（side_effect 必须抛真异常才能命中 except APIError）"""
    from openai import APIError
    e = APIError(msg, request=None, body={"error": {"code": code, "message": msg}} if code else {"error": {"message": msg}})
    e.status_code = status
    if code:
        e.code = code
    e.body = {"error": {"code": code, "message": msg}} if code else {"error": {"message": msg}}
    e.response = MagicMock()
    e.response.headers = {}
    return e


def _make_cfg(**kw):
    from backend.config.settings import APIConfig
    defaults = dict(
        base_url="https://api.test.com", api_key="k", model="m",
        max_tokens=100, timeout=5,
        temperature_max_retries=1, backoff_max_retries=0,
    )
    defaults.update(kw)
    return APIConfig(**defaults)


async def test_chat_api_error_moderation_marked():
    """OpenAI APIError 带 content_policy_violation → 错误串含 [MODERATION]"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=_api_error(code="content_policy_violation"))
    client.client = fake
    res = await client.chat([{"role": "user", "content": "hi"}])
    assert not res.success
    assert "[MODERATION]" in res.error


async def test_chat_error_text_moderation_marked():
    """通用异常消息含中文安全词 → 标记"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg())
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=RuntimeError("内容不符合安全规范"))
    client.client = fake
    res = await client.chat([{"role": "user", "content": "hi"}])
    assert not res.success
    assert "[MODERATION]" in res.error


async def test_retry_once_then_shortcircuit():
    """审核拦截：第1次失败→重试1次→仍拦截→短路返回，不进入退避链"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg(temperature_max_retries=2, backoff_max_retries=1))
    err = mark_moderation("API错误 (HTTP 400): content policy")
    client.chat = AsyncMock(side_effect=[
        LLMResult(False, "", err),
        LLMResult(False, "", err),
    ])
    with patch("asyncio.sleep", new=AsyncMock()):
        res = await client.chat_with_retry(
            [{"role": "user", "content": "hi"}])
    assert not res.success
    assert "[MODERATION]" in res.error
    assert res.call_stats["attempts"] == 2          # 只重试1次，不进退避（否则会是 3+）
    assert client.chat.call_count == 2


async def test_retry_once_success():
    """审核拦截后第 2 次成功 → 正常返回（偶发误判可自愈）"""
    from backend.core.llm_client import LLMClient
    client = LLMClient(_make_cfg(temperature_max_retries=2, backoff_max_retries=1))
    err = mark_moderation("API错误 (HTTP 400): content policy")
    client.chat = AsyncMock(side_effect=[
        LLMResult(False, "", err),
        LLMResult(True, '{"ok":1}', "", (1, 1), UsageStatus.KNOWN),
    ])
    with patch("asyncio.sleep", new=AsyncMock()):
        res = await client.chat_with_retry(
            [{"role": "user", "content": "hi"}])
    assert res.success
    assert res.call_stats["attempts"] == 2


async def test_analyzer_propagates_moderation_error():
    """analyzer 失败时 retry_info.error 携带标记（pipeline 据此判 skipped）

    H16 Phase 3 (2026-08-26)：analyzer 改用 chat_auto()，返回 5-tuple（与 chat_with_retry 同）
    U4（2026-10-30）：chat_auto() 改为返回 LLMResult 对象（此处只关心失败态与 call_stats）
    """
    from backend.core.analyzer import NovelAnalyzer
    from backend.config.settings import AppConfig
    from backend.core.moderation import mark_moderation
    analyzer = NovelAnalyzer(AppConfig())
    analyzer.llm_client = MagicMock()
    # chat_auto 返回 LLMResult(success, content, error, tokens, usage_status, call_stats, ...)
    analyzer.llm_client.chat_auto = AsyncMock(return_value=LLMResult(
        False, "", mark_moderation("API错误 (HTTP 400): content policy"),
        call_stats={"attempts": 2, "failed_tokens": 0}))
    analyzer.prompt_builder = MagicMock()
    analyzer.prompt_builder.build_messages = MagicMock(return_value=[{"role": "user", "content": "x"}])
    result, tokens, retry_info = await analyzer.analyze_chapter(1, "内容", MagicMock())
    assert result is None
    assert "[MODERATION]" in (retry_info.get("error") or "")


def test_memory_state_add_skipped():
    """skipped 与 failed 分离：不进失败集，可独立统计"""
    from backend.core.memory_state import MemoryState
    state = MemoryState()
    state.add_failed(1, "普通失败")
    state.add_skipped(3, "内容审核拦截")
    assert 1 in state._failed_chapters
    assert 3 in state._skipped_chapters
    assert 3 not in state._failed_chapters


def test_config_skip_moderation_default():
    """配置默认开启 skip_moderation_blocked，且 to_dict/from_dict 往返保留"""
    from backend.config.settings import AppConfig
    cfg = AppConfig()
    assert cfg.analysis.skip_moderation_blocked is True
    d = cfg.to_dict()
    d["analysis"]["skip_moderation_blocked"] = False
    cfg2 = AppConfig.from_dict(d)
    assert cfg2.analysis.skip_moderation_blocked is False


async def test_pipeline_analyze_block_skips_moderation(tmp_path):
    """_analyze_one_block：analyzer 返回带 [MODERATION] 错误 → add_skipped 而非 add_failed"""
    from backend.core.pipeline import AnalysisPipeline
    from backend.config.settings import AppConfig
    from backend.core.moderation import mark_moderation

    cfg = AppConfig()
    cfg.analysis.skip_moderation_blocked = True

    pipeline = AnalysisPipeline(config=cfg, directory=tmp_path, knowledge_file=tmp_path / "knowledge.json")
    from backend.core.memory_state import MemoryState
    pipeline.state = MemoryState()
    pipeline._block_map = {3: [3, 4]}
    pipeline.file_processor = MagicMock()
    pipeline.file_processor.read_block = AsyncMock(return_value="正文")

    analyzer = MagicMock()
    async def fake_analyze(ch, content, kb, block_size=1, book_id=""):
        return None, (10, 10), {"error": mark_moderation("API错误 (HTTP 400): content policy"), "retries": 2}
    analyzer.analyze_chapter = fake_analyze

    state = pipeline.state
    ok, _elapsed, _tokens, result, retry_info = await pipeline._analyze_one_block(
        analyzer, pipeline.file_processor, state, 3, [3, 4], block_size=2)
    assert not ok
    assert 3 in state._skipped_chapters
    assert 3 not in state._failed_chapters
    assert retry_info.get("moderation_skip") is True


async def test_pipeline_skips_when_config_off(tmp_path):
    """skip_moderation_blocked=False 时：拦截错误按普通失败处理（进 failed、可补跑）"""
    from backend.core.pipeline import AnalysisPipeline
    from backend.config.settings import AppConfig
    from backend.core.moderation import mark_moderation

    cfg = AppConfig()
    cfg.analysis.skip_moderation_blocked = False
    pipeline = AnalysisPipeline(config=cfg, directory=tmp_path, knowledge_file=tmp_path / "knowledge.json")
    from backend.core.memory_state import MemoryState
    pipeline.state = MemoryState()
    pipeline._block_map = {3: [3, 4]}
    pipeline.file_processor = MagicMock()
    pipeline.file_processor.read_block = AsyncMock(return_value="正文")
    analyzer = MagicMock()
    async def fake_analyze(ch, content, kb, block_size=1, book_id=""):
        return None, (10, 10), {"error": mark_moderation("API错误 (HTTP 400): content policy"), "retries": 2}
    analyzer.analyze_chapter = fake_analyze
    state = pipeline.state
    ok, _e, _t, _r, _ri = await pipeline._analyze_one_block(
        analyzer, pipeline.file_processor, state, 3, [3, 4], block_size=2)
    assert not ok
    assert 3 in state._failed_chapters
    assert 3 not in state._skipped_chapters
