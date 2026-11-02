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
from backend.services.final_summary import recheck_channel, recheck_conflict_reason


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


def test_no_conflict_when_recheck_model_not_configured():
    """没配复检模型 = 跟随主通道，任何 base_url 都不该被拦。"""
    cfg = _cfg(recheck_model="", recheck_base_url="https://api.minimaxi.com/v1")
    assert recheck_conflict_reason(cfg) == ""
