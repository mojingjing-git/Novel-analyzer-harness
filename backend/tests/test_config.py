"""配置管理（ConfigManager / AppConfig）与模型费用估算测试。"""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

from backend.api.routes_analysis import _estimate_session_cost
from backend.config.constants import MAX_OUTPUT_TOKENS
from backend.config.settings import AppConfig, ConfigManager, validate_settings_payload
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


def test_config_manager_patch_shallow_dict_and_env_key_protection(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "api": {
            "base_url": "https://api.test.com/v1",
            "api_key": "",
            "model": "main-m",
            "summary_thinking_mode": {"reasoning_effort": "low"},
        },
        "analysis": {"concurrency": 4, "summary_batch_size": 30},
    }, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("LLM_API_KEY", "env-secret-key")
    mgr = ConfigManager(cfg_path)
    mgr.load()

    # 局部 patch 不破坏 env key（磁盘仍保持空串，内存保留 env key），同时允许清空 dict 字段
    patched = mgr.patch({
        "api": {"summary_thinking_mode": {}},
        "analysis": {"summary_batch_size": 45},
    })
    assert patched is not None
    assert patched.analysis.summary_batch_size == 45
    assert patched.analysis.concurrency == 4
    assert patched.api.summary_thinking_mode == {}
    assert patched.api.api_key == "env-secret-key"

    disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert disk["api"]["api_key"] == ""
    assert disk["api"]["summary_thinking_mode"] == {}
    assert disk["analysis"]["summary_batch_size"] == 45

    # config.json 损坏时 patch 返回 None 且不覆盖原文件
    broken_path = tmp_path / "broken.json"
    broken = '{"api": '
    broken_path.write_text(broken, encoding="utf-8")
    broken_mgr = ConfigManager(broken_path)
    broken_mgr.load()
    assert broken_mgr.patch({"analysis": {"summary_batch_size": 10}}) is None
    assert broken_path.read_text(encoding="utf-8") == broken


def test_summary_and_recheck_concurrency_follow_defaults_and_validation():
    """默认 summary_concurrency=0/recheck_concurrency=0（跟随主分析），允许 0 拒绝负数。"""
    cfg = AppConfig()
    assert cfg.analysis.summary_concurrency == 0
    assert cfg.analysis.recheck_concurrency == 0

    assert validate_settings_payload({"analysis": {"summary_concurrency": 0, "recheck_concurrency": 0}}) == []
    assert validate_settings_payload({"analysis": {"summary_concurrency": 4, "recheck_concurrency": 6}}) == []

    errs_neg = validate_settings_payload({"analysis": {"summary_concurrency": -1, "recheck_concurrency": -1}})
    locs = [tuple(e["loc"]) for e in errs_neg]
    assert ("body", "analysis", "summary_concurrency") in locs
    assert ("body", "analysis", "recheck_concurrency") in locs


def test_recheck_v3_option_flags_defaults_and_validation():
    """V3 灰区终审与支线单跳开关默认关闭，支持布尔强转与非法值校验拒绝。"""
    cfg = AppConfig()
    assert cfg.analysis.recheck_v3_gray_u4 is False
    assert cfg.analysis.recheck_v3_mid_single_hop is False

    coerced = AppConfig.from_dict({
        "analysis": {"recheck_v3_gray_u4": "true", "recheck_v3_mid_single_hop": 1},
    })
    assert coerced.analysis.recheck_v3_gray_u4 is True
    assert coerced.analysis.recheck_v3_mid_single_hop is True

    assert validate_settings_payload({"analysis": {"recheck_v3_gray_u4": True, "recheck_v3_mid_single_hop": False}}) == []
    errs = validate_settings_payload({"analysis": {"recheck_v3_gray_u4": "maybe"}})
    assert [("body", "analysis", "recheck_v3_gray_u4")] == [tuple(e["loc"]) for e in errs]
