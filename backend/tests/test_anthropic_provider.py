"""
测试 Anthropic 格式支持：provider 检测 + 载荷构造 + 响应解析（mock）
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import APIConfig
from backend.core.llm_client import LLMClient, detect_provider


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
        provider="auto",
    )
    defaults.update(kwargs)
    return APIConfig(**defaults)


def test_detect_provider():
    """auto 检测：URL/Key/模型名特征 + 显式覆盖"""
    # URL 含 anthropic
    assert detect_provider(make_config(base_url="https://api.anthropic.com")) == "anthropic"
    # sk-ant- 前缀
    assert detect_provider(make_config(api_key="sk-ant-abc123")) == "anthropic"
    # claude- 模型名
    assert detect_provider(make_config(model="claude-sonnet-4-5")) == "anthropic"
    # 显式覆盖
    assert detect_provider(make_config(provider="anthropic")) == "anthropic"
    assert detect_provider(make_config(provider="openai", api_key="sk-ant-abc")) == "openai"
    # 其余默认 openai
    assert detect_provider(make_config()) == "openai"
    print("✅ test_detect_provider passed")


def test_anthropic_payload_building():
    """system 提取到顶层参数、同角色消息合并、thinking 透传"""
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
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "问题3"},
        ],
        temperature=0.5,
        max_tokens=800,
    )
    assert payload["model"] == "claude-3-5-sonnet"
    assert payload["max_tokens"] == 800
    assert payload["temperature"] == 0.5
    assert payload["system"] == "你是分析器"
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    # 相邻同角色合并
    assert payload["messages"] == [
        {"role": "user", "content": "问题1\n\n问题2"},
        {"role": "assistant", "content": "回答"},
        {"role": "user", "content": "问题3"},
    ]
    print("✅ test_anthropic_payload_building passed")


def test_anthropic_response_parsing():
    """text block 拼接、thinking block 丢弃、usage 映射"""
    client = LLMClient(make_config(provider="anthropic"))
    resp = MagicMock()
    t1 = MagicMock()
    t1.type = "text"
    t1.text = '{"core_events": []}'
    t2 = MagicMock()
    t2.type = "thinking"
    t2.thinking = "思考过程"
    t3 = MagicMock()
    t3.type = "text"
    t3.text = " 补充"
    resp.content = [t1, t2, t3]
    resp.usage.input_tokens = 100
    resp.usage.output_tokens = 50
    resp.usage.cache_read_input_tokens = 30
    from backend.core.llm_stream import parse_anthropic_response
    content, pt, ct, cached = parse_anthropic_response(resp)
    assert content == '{"core_events": []} 补充'
    assert (pt, ct, cached) == (100, 50, 30)
    print("✅ test_anthropic_response_parsing passed")


async def test_anthropic_chat_success():
    """anthropic 分支：messages.create 被调用且返回解析后的内容"""
    client = LLMClient(make_config(provider="anthropic", model="claude-sonnet-4-5"))
    fake_anthropic = MagicMock()
    resp = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = '{"ok": true}'
    resp.content = [block]
    resp.usage.input_tokens = 10
    resp.usage.output_tokens = 20
    resp.usage.cache_read_input_tokens = 0
    fake_anthropic.messages.create = AsyncMock(return_value=resp)
    client.anthropic = fake_anthropic

    res = await client.chat(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        temperature=0.3,
    )
    assert res.success
    assert res.content == '{"ok": true}'
    assert res.tokens == (10, 20)
    # 断言 create 收到的载荷
    call_kwargs = fake_anthropic.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-sonnet-4-5"
    assert call_kwargs["system"] == "sys"
    assert call_kwargs["messages"] == [{"role": "user", "content": "hi"}]
    print("✅ test_anthropic_chat_success passed")


async def test_anthropic_empty_content():
    """anthropic 空内容（无 text block）→ 失败"""
    client = LLMClient(make_config(provider="anthropic"))
    fake_anthropic = MagicMock()
    resp = MagicMock()
    resp.content = []
    resp.usage.input_tokens = 5
    resp.usage.output_tokens = 0
    resp.usage.cache_read_input_tokens = 0
    fake_anthropic.messages.create = AsyncMock(return_value=resp)
    client.anthropic = fake_anthropic
    res = await client.chat([{"role": "user", "content": "hi"}])
    assert not res.success
    assert "空内容" in res.error
    print("✅ test_anthropic_empty_content passed")


async def test_openai_path_unchanged():
    """provider=openai 时仍走 OpenAI 客户端"""
    client = LLMClient(make_config())
    assert client.client is not None
    assert client.anthropic is None
    assert client.provider == "openai"
    fake_openai = MagicMock()
    resp = MagicMock()
    msg = MagicMock()
    msg.content = '{"ok": true}'
    resp.choices = [MagicMock(message=msg)]
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 20
    resp.usage.total_tokens = 30
    resp.usage.prompt_tokens_details = None
    fake_openai.chat.completions.create = AsyncMock(return_value=resp)
    client.client = fake_openai
    res = await client.chat([{"role": "user", "content": "hi"}])
    assert res.success
    assert res.content == '{"ok": true}'
    assert res.tokens == (10, 20)
    print("✅ test_openai_path_unchanged passed")


async def run_all():
    test_detect_provider()
    test_anthropic_payload_building()
    test_anthropic_response_parsing()
    await test_anthropic_chat_success()
    await test_anthropic_empty_content()
    await test_openai_path_unchanged()


if __name__ == "__main__":
    asyncio.run(run_all())

async def test_probe_thinking_finds_working_param():
    """探测：找到有效的禁用思考参数"""
    from backend.core.llm_probe import probe_thinking_params
    fake_client = MagicMock()

    def make_resp(content_len, reasoning_len):
        resp = MagicMock()
        msg = MagicMock()
        msg.content = "x" * content_len
        msg.reasoning_content = "r" * reasoning_len
        resp.choices = [MagicMock(message=msg)]
        return resp

    # 基线(思考) → thinking:disabled(有效) → reasoning_effort(无效) → enable_thinking(无效)
    side_effects = [
        make_resp(3, 200),   # 基线：有思考
        make_resp(3, 0),     # thinking disabled：content 有、reasoning 空 → 有效
        make_resp(3, 150),   # reasoning_effort：思考还在 → 无效
        make_resp(3, 180),   # enable_thinking：思考还在 → 无效
    ]
    fake_client.chat.completions.create = AsyncMock(side_effect=side_effects)

    with patch("backend.core.llm_probe.AsyncOpenAI", return_value=fake_client):
        result = await probe_thinking_params("https://api.test.com", "k", "m", "auto")

    assert result["default_thinks"] is True
    assert result["best"]["thinking_mode"] == {"thinking": {"type": "disabled"}}
    worked = [r for r in result["results"] if r["worked"]]
    assert len(worked) == 1
    print("✅ test_probe_thinking_finds_working_param passed")


async def test_probe_thinking_none_works():
    """探测：无参数有效 → best=None + note"""
    from backend.core.llm_probe import probe_thinking_params
    fake_client = MagicMock()

    def make_resp(reasoning_len):
        resp = MagicMock()
        msg = MagicMock()
        msg.content = "x" * 3
        msg.reasoning_content = "r" * reasoning_len
        resp.choices = [MagicMock(message=msg)]
        return resp

    fake_client.chat.completions.create = AsyncMock(side_effect=[
        make_resp(200), make_resp(180), make_resp(190), make_resp(170),
    ])
    with patch("backend.core.llm_probe.AsyncOpenAI", return_value=fake_client):
        result = await probe_thinking_params("https://api.test.com", "k", "m", "auto")

    assert result["best"] is None
    assert "未探测到" in result["note"]
    print("✅ test_probe_thinking_none_works passed")


async def test_probe_thinking_model_no_think():
    """探测：模型默认不思考 → 无需禁用"""
    from backend.core.llm_probe import probe_thinking_params
    fake_client = MagicMock()
    resp = MagicMock()
    msg = MagicMock()
    msg.content = "x" * 5
    msg.reasoning_content = ""
    resp.choices = [MagicMock(message=msg)]
    fake_client.chat.completions.create = AsyncMock(return_value=resp)

    with patch("backend.core.llm_probe.AsyncOpenAI", return_value=fake_client):
        result = await probe_thinking_params("https://api.test.com", "k", "m", "auto")

    assert result["default_thinks"] is False
    assert result["best"] is None
    assert "无需禁用" in result["note"]
    print("✅ test_probe_thinking_model_no_think passed")


async def test_probe_thinking_anthropic_skip():
    """anthropic 协议直接返回官方参数，不发请求"""
    from backend.core.llm_probe import probe_thinking_params
    with patch("backend.core.llm_probe.AsyncOpenAI") as fake_cls:
        result = await probe_thinking_params("https://api.anthropic.com", "k", "claude-x", "auto")
        fake_cls.assert_not_called()
    assert result["best"]["thinking_mode"] == {"thinking": {"type": "disabled"}}
    print("✅ test_probe_thinking_anthropic_skip passed")


# =============================================================================
# v2 多模式检测测试（2026-08-26）— 覆盖 M3 / QwQ / DeepSeek R1 / OpenAI o-series / Anthropic
# =============================================================================

def _make_probe_resp(content, reasoning_content="", reasoning_details=None, usage_dict=None):
    """构造 probe_thinking 用的 mock response 对象"""
    resp = MagicMock()
    msg = MagicMock()
    msg.content = content
    msg.reasoning_content = reasoning_content
    msg.reasoning_details = reasoning_details
    resp.choices = [MagicMock(message=msg)]
    usage = MagicMock()
    usage.completion_tokens_details = (usage_dict or {}).get("completion_tokens_details")
    resp.usage = usage
    return resp


def _patch_all_probe_candidates(fake_client, resp):
    """让所有候选都返回同一 resp（用于基线/参数统一场景）"""
    fake_client.chat.completions.create = AsyncMock(return_value=resp)


async def test_probe_v2_detects_think_tags_in_content():
    """v2 #1: M3 / DeepSeek R1 / QwQ — thinking 嵌 content 的 <think> 形式必须被检测"""
    from backend.core.llm_probe import probe_thinking_params

    fake_client = MagicMock()
    # 基线：think 嵌 content
    baseline = _make_probe_resp(
        "<think>用户在问 123456789 * 987654321，我算一下。</think>\n121932631112635269"
    )
    # 候选参数：thinking:disabled 后 think 标签消失
    no_think = _make_probe_resp("121932631112635269")
    # side_effect 按调用顺序返回不同 resp
    fake_client.chat.completions.create = AsyncMock(side_effect=[
        baseline,  # 基线
        no_think,  # thinking:disabled
        no_think,  # reasoning_effort:none
        no_think,  # enable_thinking:false
        no_think,  # thinking:disabled + reasoning_split
        baseline,  # chat_template_kwargs:enable_thinking=false → Qwen3 不认此格式
        no_think,  # reasoning:effort=none
    ])

    with patch("backend.core.llm_probe.AsyncOpenAI", return_value=fake_client):
        result = await probe_thinking_params("https://api.test.com", "k", "m3", "auto")

    # 基线必须识别为 thinking
    assert result["default_thinks"] is True, "M3 嵌 <think> 应该被识别为 thinking"
    base_breakdown = result["default_detection_breakdown"]
    assert base_breakdown["think_tags"] is True, "think_tags 维度必须命中"
    assert base_breakdown["reasoning_content"] is False, "M3 默认不暴露 reasoning_content 字段"
    assert base_breakdown["usage_reasoning_tokens"] is False, "无 usage 数据时不命中"

    # 最佳参数应该是 thinking:disabled
    assert result["best"] is not None
    assert result["best"]["thinking_mode"] == {"thinking": {"type": "disabled"}}
    print("✅ test_probe_v2_detects_think_tags_in_content passed")


async def test_probe_v2_detects_usage_reasoning_tokens():
    """v2 #2: OpenAI o-series 风格 — 完全隐藏 thinking 但 usage.reasoning_tokens > 0"""
    from backend.core.llm_probe import probe_thinking_params

    fake_client = MagicMock()
    # 基线：content 只有数字但 usage 报 reasoning_tokens
    baseline = _make_probe_resp(
        "121932631112635269",
        usage_dict={"completion_tokens_details": {"reasoning_tokens": 250}},
    )
    # 参数有效：usage 报 0
    no_thinking = _make_probe_resp(
        "121932631112635269",
        usage_dict={"completion_tokens_details": {"reasoning_tokens": 0}},
    )
    fake_client.chat.completions.create = AsyncMock(side_effect=[
        baseline,
        baseline,  # thinking:disabled 对 o-series 无效
        no_thinking,  # reasoning_effort:none 有效
        baseline,  # enable_thinking:false 无效
        baseline,  # thinking:disabled + reasoning_split
        baseline,  # chat_template_kwargs
        no_thinking,  # reasoning:effort=none 有效
    ])

    with patch("backend.core.llm_probe.AsyncOpenAI", return_value=fake_client):
        result = await probe_thinking_params("https://api.test.com", "k", "o1", "auto")

    assert result["default_thinks"] is True
    assert result["default_detection_breakdown"]["usage_reasoning_tokens"] is True
    assert result["default_detection_breakdown"]["think_tags"] is False
    # 第一个有效参数应该是 reasoning_effort:none
    assert result["best"]["thinking_mode"] == {"reasoning_effort": "none"}
    print("✅ test_probe_v2_detects_usage_reasoning_tokens passed")


async def test_probe_v2_detects_anthropic_thinking_blocks():
    """v2 #3: Anthropic 协议 — content 是 list 且含 type=thinking block"""
    from backend.core.llm_probe import probe_thinking_params

    fake_client = MagicMock()
    # Anthropic 协议 mock：content 是 list
    baseline = MagicMock()
    msg = MagicMock()
    msg.content = [
        {"type": "thinking", "thinking": "让我算一下..."},
        {"type": "text", "text": "121932631112635269"},
    ]
    msg.reasoning_content = None
    msg.reasoning_details = None
    baseline.choices = [MagicMock(message=msg)]
    baseline.usage = MagicMock()
    baseline.usage.completion_tokens_details = None

    # OpenAI 协议探测路径通常不命中 anthropic_thinking_blocks（因为 list 形态被当作异常）
    # 这里只验证 detection_breakdown 的 anthropic_thinking_blocks 维度计算逻辑
    fake_client.chat.completions.create = AsyncMock(return_value=baseline)

    with patch("backend.core.llm_probe.AsyncOpenAI", return_value=fake_client):
        result = await probe_thinking_params("https://api.test.com", "k", "m", "auto")

    # OpenAI 协议探测会把 content 列表里的 thinking block 识别为 anthropic_thinking_blocks
    # 同时 content_str 提取空 → content_chars=0
    # 但 baseline['reasoning_chars'] 是 0 → default_thinks 应基于 anthropic_thinking_blocks 命中
    assert result["default_detection_breakdown"]["anthropic_thinking_blocks"] is True
    assert result["default_thinks"] is True
    print("✅ test_probe_v2_detects_anthropic_thinking_blocks passed")


async def test_probe_v2_all_patterns_clean_means_works():
    """v2 #4: 当所有 7 个检测模式都未命中时 → 参数有效"""
    from backend.core.llm_probe import probe_thinking_params

    fake_client = MagicMock()
    # 基线和所有候选：纯 content，无任何 thinking 标记
    clean = _make_probe_resp("121932631112635269")
    # 但基线应该"无 thinking"（让 default_thinks=False，这样会跳过 best 寻找，
    # 但仍要验证 worked 逻辑对 clean resp 的判断）
    fake_client.chat.completions.create = AsyncMock(return_value=clean)

    with patch("backend.core.llm_probe.AsyncOpenAI", return_value=fake_client):
        result = await probe_thinking_params("https://api.test.com", "k", "m", "auto")

    assert result["default_thinks"] is False
    # 所有 7 个 detection_breakdown 维度都应 False
    for k, v in result["default_detection_breakdown"].items():
        assert v is False, f"基线无 thinking 时 {k} 应为 False"
    # 没有 best（因为 default 不思考）
    assert result["best"] is None
    assert "无需禁用" in result["note"]
    print("✅ test_probe_v2_all_patterns_clean_means_works passed")


async def test_probe_v2_returns_breakdown_for_each_candidate():
    """v2 #5: 每个候选的 detection_breakdown 都要返回（前端展示用）"""
    from backend.core.llm_probe import probe_thinking_params

    fake_client = MagicMock()
    # 基线有 thinking；thinking:disabled 候选也有 thinking（说明该参数对该模型无效）
    baseline = _make_probe_resp("<think>...think...</think>\nanswer")
    still_thinking = _make_probe_resp("<think>...still thinking...</think>\nanswer2")
    fake_client.chat.completions.create = AsyncMock(side_effect=[
        baseline,  # 基线
        still_thinking,  # thinking:disabled 无效
        still_thinking,  # reasoning_effort:none 无效
        still_thinking,  # enable_thinking:false 无效
        still_thinking,  # thinking+reasoning_split 无效
        still_thinking,  # chat_template_kwargs 无效
        still_thinking,  # reasoning:effort=none 无效
    ])

    with patch("backend.core.llm_probe.AsyncOpenAI", return_value=fake_client):
        result = await probe_thinking_params("https://api.test.com", "k", "m2.7", "auto")

    assert result["default_thinks"] is True
    assert result["best"] is None, "所有参数都无效时 best 应为 None"
    # 每个 result 都必须有 detection_breakdown
    for r in result["results"]:
        assert "detection_breakdown" in r
        assert len(r["detection_breakdown"]) == 7, "应有 7 个检测维度"
        assert "think_tag_chars" in r
        assert "reasoning_token_count" in r
    # note 应提示 M2.x 系类 known limitation
    assert "M2.x" in result["note"] or "不支持" in result["note"]
    print("✅ test_probe_v2_returns_breakdown_for_each_candidate passed")

def test_anthropic_payload_whitelist():
    """#4: thinking_mode 只放行 thinking 键，enable_thinking 残留不得透传"""
    client = LLMClient(make_config(
        provider="anthropic",
        thinking_mode={"enable_thinking": False, "thinking": {"type": "disabled"}},
    ))
    payload = client._build_anthropic_payload(
        [{"role": "user", "content": "hi"}], temperature=0.5, max_tokens=100)
    assert payload["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in payload
    # 只有 enable_thinking 时：不注入任何额外字段
    client2 = LLMClient(make_config(provider="anthropic", thinking_mode={"enable_thinking": False}))
    payload2 = client2._build_anthropic_payload(
        [{"role": "user", "content": "hi"}], temperature=0.5, max_tokens=100)
    assert "thinking" not in payload2
    assert "enable_thinking" not in payload2
    print("✅ test_anthropic_payload_whitelist passed")


def test_anthropic_total_tokens_include_cache():
    """#3: anthropic total = input + output + cache（对齐 openai total_tokens 口径）"""
    client = LLMClient(make_config(provider="anthropic"))
    resp = MagicMock()
    resp.content = []
    resp.usage.input_tokens = 100
    resp.usage.output_tokens = 50
    resp.usage.cache_read_input_tokens = 30
    from backend.core.llm_stream import parse_anthropic_response
    _c, pt, ct, cached = parse_anthropic_response(resp)
    assert (pt, ct, cached) == (100, 50, 30)
    print("✅ test_anthropic_total_tokens_include_cache passed")
