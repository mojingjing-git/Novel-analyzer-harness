"""配置管理（ConfigManager / AppConfig）与模型费用估算测试。"""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

from backend.api.routes_analysis import _estimate_session_cost
from backend.config.constants import MAX_OUTPUT_TOKENS
from backend.config.settings import AppConfig, ConfigManager
from backend.core.llm_client import LLMClient
from backend.core.model_catalog import calc_model_cost

MINIMAX = "MiniMax-M3.1-Flash"
LUNA = "codex/gpt-6-luna"


def test_config_load_with_bom_and_healthy_save(tmp_path):
    cfg_path = tmp_path / "config.json"
    data = {"api": {"model": "test-model", "base_url": "https://api.test.com", "api_key": "k", "provider": "auto"}}
    cfg_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(data, ensure_ascii=False).encode("utf-8"))

    mgr = ConfigManager(cfg_path)
    cfg = mgr.load()
    assert cfg.api.model == "test-model"
    assert mgr._load_failed is False
    assert mgr.save(AppConfig()) is True


def test_corrupt_config_logs_error_and_blocks_save(tmp_path, caplog):
    cfg_path = tmp_path / "config.json"
    broken = '{"api": {"model": "x",'
    cfg_path.write_text(broken, encoding="utf-8")

    mgr = ConfigManager(cfg_path)
    with caplog.at_level(logging.ERROR, logger="backend.config.settings"):
        cfg = mgr.load()
    assert cfg.api.model == ""
    assert mgr._load_failed is True
    assert any("配置加载失败" in r.message for r in caplog.records)
    assert mgr.save(AppConfig()) is False
    assert cfg_path.read_text(encoding="utf-8") == broken


def test_app_config_field_coercion():
    cfg = AppConfig.from_dict({
        "api": {"max_tokens": "130000", "timeout": "30"},
        "analysis": {"concurrency": "4", "auto_archive": "true", "skip_moderation_blocked": 0},
        "gui": {"language": 123},
    })
    assert cfg.api.max_tokens == 130000
    assert cfg.api.timeout == 30
    assert cfg.analysis.concurrency == 4
    assert cfg.analysis.auto_archive is True
    assert cfg.analysis.skip_moderation_blocked is False
    assert cfg.gui.language == "123"

    fallback = AppConfig.from_dict({"api": {"max_tokens": "abc"}, "analysis": {"checkpoint_interval": "宽"}})
    assert fallback.api.max_tokens == MAX_OUTPUT_TOKENS
    assert fallback.analysis.checkpoint_interval == 5


async def test_list_models_anthropic_empty_base_url():
    with patch("backend.core.llm_client.AsyncOpenAI") as fake_cls:
        result = await LLMClient.list_models("", "k", "anthropic")
    assert result == []
    fake_cls.assert_not_called()


def test_model_catalog_cost_and_session_estimate():
    r = calc_model_cost(MINIMAX, input_tokens=1_000_000, output_tokens=100_000, cached_tokens=200_000)
    assert r is not None and abs(r["usd"] - 0.183) < 1e-9

    clamped = calc_model_cost(MINIMAX, input_tokens=100, output_tokens=10, cached_tokens=500)
    assert clamped is not None and abs(clamped["usd"] - (100 * 0.015 + 10 * 0.6) / 1_000_000) < 1e-12
    assert calc_model_cost("完全未知的私有模型") is None

    analysis = {"categories": {"chapter": {"input_tokens": 1_000_000, "output_tokens": 100_000}}, "cached_tokens": 200_000}
    summary = {"categories": {
        "recheck": {"input_tokens": 500_000, "output_tokens": 50_000},
        "summary": {"input_tokens": 300_000, "output_tokens": 30_000},
    }}
    cost = _estimate_session_cost(analysis, summary, api_model=MINIMAX, summary_model="", recheck_model=LUNA)
    assert abs(cost["usd"] - 0.321) < 1e-9
    assert cost["unpriced"] == []
    assert cost["total_tokens"] == 2_180_000

    empty_cost = _estimate_session_cost(
        {"categories": {}, "cached_tokens": 0}, {"categories": {}},
        api_model=MINIMAX, summary_model="", recheck_model=LUNA,
    )
    assert empty_cost["usd"] == 0.0 and empty_cost["total_tokens"] == 0
