"""复检通道的配置解析与"同源"校验。

2026-11-02 事故：`--recheck-model codex/gpt-6-luna` 配上官方端点，280 次调用全被
`400 unknown model` 拒绝，而失败被吞成空回包，整轮只剩 `checked=0`。
生产侧 `_run_recheck_v2` 原先只 replace(model)，**不换 base_url**，会 100% 重演。

现在两件事各有唯一实现，便于单测钉住：
- `recheck_channel(config)`：复检实际用的 (model, base_url)，空则跟随主通道；
- `recheck_conflict_reason(config)`：同源校验，冲突时返回原因（调用方据此整轮阻断）。
"""

from __future__ import annotations

from backend.config.settings import AppConfig
from backend.services.final_summary import (
    recheck_channel,
    recheck_conflict_reason,
    recheck_overrides,
)


def _cfg(**recheck) -> AppConfig:
    cfg = AppConfig()
    for key, value in recheck.items():
        setattr(cfg.analysis, key, value)
    return cfg


def test_channel_falls_back_to_main_channel():
    """没单独配复检通道时，与主通道完全一致（行为不变）。"""
    cfg = AppConfig()
    assert recheck_channel(cfg) == (cfg.api.model, cfg.api.base_url)


def test_channel_uses_recheck_overrides():
    cfg = _cfg(
        recheck_model="codex/gpt-6-luna",
        recheck_base_url="http://127.0.0.1:3425/v1",
    )
    assert recheck_channel(cfg) == ("codex/gpt-6-luna", "http://127.0.0.1:3425/v1")


def test_conflict_blocks_gateway_model_on_official_endpoint():
    """网关专用模型名 + 官方端点 = 必然 400，必须在跑之前就阻断。"""
    cfg = _cfg(
        recheck_model="codex/gpt-6-luna",
        recheck_base_url="https://api.minimaxi.com/v1",
    )
    reason = recheck_conflict_reason(cfg)
    assert "配置冲突" in reason and "codex/gpt-6-luna" in reason


def test_no_conflict_when_channel_and_model_match():
    """同一网关下的专用模型名是合法组合，不许误判。"""
    cfg = _cfg(
        recheck_model="codex/gpt-6-luna",
        recheck_base_url="http://127.0.0.1:3425/v1",
    )
    assert recheck_conflict_reason(cfg) == ""


def test_model_follows_main_gateway_name_conflicts_with_official_endpoint():
    """判定模型留空 = 跟随主模型：主模型是网关专用名而复检指向官方端点 = 冲突。

    回归锁定：同源校验在 recheck_channel **解析后**的 (model, base_url) 上做，
    不因"复检模型字段为空"就跳过——否则跟随主网关模型 + 官方端点会绕过阻断
    （400 unknown model 刷屏的老事故换个入口重演）。
    """
    cfg = _cfg(recheck_model="", recheck_base_url="https://api.minimaxi.com/v1")
    cfg.api.model = "codex/gpt-6-luna"
    reason = recheck_conflict_reason(cfg)
    assert "配置冲突" in reason and "codex/gpt-6-luna" in reason


def test_model_follows_main_official_name_no_conflict():
    """判定模型留空且主模型是官方名：跟随意味着同源，不拦。"""
    cfg = _cfg(recheck_model="", recheck_base_url="https://api.minimaxi.com/v1")
    assert recheck_conflict_reason(cfg) == ""


# ---------------------------------------------------------------------------
# recheck_overrides：通道字段 → replace(config.api, **overrides) 的组装
# ---------------------------------------------------------------------------


def test_overrides_empty_when_nothing_configured():
    """全部留空 = 没有任何复检通道配置，复检直接复用总结客户端。"""
    assert recheck_overrides(AppConfig()) == {}


def test_overrides_include_every_set_channel_field():
    cfg = _cfg(
        recheck_model="judge-x",
        recheck_base_url="http://127.0.0.1:3425/v1",
        recheck_api_key="sk-recheck",
        recheck_provider="openai",
        recheck_timeout=90,
        recheck_json_mode="qwen",
        recheck_reasoning_effort="high",
    )
    assert recheck_overrides(cfg) == {
        "model": "judge-x",
        "base_url": "http://127.0.0.1:3425/v1",
        "api_key": "sk-recheck",
        "provider": "openai",
        "timeout": 90,
        "json_mode": "qwen",
        "reasoning_effort": "high",
    }


def test_base_url_override_applies_even_when_model_follows_main():
    """回归：只换通道不换模型时 base_url 必须生效。

    旧实现的条件是 `recheck_model and recheck_model != api.model`——同模型名
    换端点时整个 override 被跳过，复检静默打回主通道。
    """
    cfg = _cfg(recheck_base_url="http://127.0.0.1:3425/v1")
    ov = recheck_overrides(cfg)
    assert ov.get("base_url") == "http://127.0.0.1:3425/v1"
    assert "model" not in ov


def test_blank_strings_and_zero_timeout_mean_follow():
    """空白串/0 都按"跟随主配置"处理，不产生空键。"""
    cfg = _cfg(recheck_api_key="  ", recheck_timeout=0, recheck_json_mode="")
    assert recheck_overrides(cfg) == {}
