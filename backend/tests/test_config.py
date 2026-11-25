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


def test_schema_bijection_and_example_config_parity():
    """schema.SPEC_BY_PATH 与 AppConfig 严格 1:1 双射，且 desktop/config.example.json 键集合完全对齐。"""
    from pathlib import Path
    from backend.config.schema import CONFIG_GROUPS, SPEC_BY_PATH, get_config_schema

    def _flatten(d: dict) -> set[str]:
        out: set[str] = set()
        for k, v in d.items():
            if isinstance(v, dict) and k in ("api", "analysis", "gui"):
                for sk in v:
                    out.add(f"{k}.{sk}")
            else:
                out.add(k)
        return out

    app_paths = _flatten(AppConfig().to_dict())
    spec_paths = set(SPEC_BY_PATH.keys())
    assert len(spec_paths) == 131
    assert spec_paths == app_paths

    schema_doc = get_config_schema()
    assert schema_doc["total_fields"] == 131
    assert len(schema_doc["groups"]) == len(CONFIG_GROUPS) == 11

    ex_path = Path(__file__).resolve().parents[2] / "desktop" / "config.example.json"
    example_cfg = json.loads(ex_path.read_text(encoding="utf-8-sig"))
    assert _flatten(example_cfg) == app_paths
    assert validate_settings_payload(example_cfg) == []


def test_env_api_key_preserves_disk_key_on_save_and_patch(tmp_path, monkeypatch):
    """环境变量注入 LLM_API_KEY 时，load 返回环境密钥但 save/patch 不得覆写或抹除磁盘 api_key。"""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"api": {"model": "m1", "api_key": "disk-secret-key"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_API_KEY", "env-override-key")

    mgr = ConfigManager(cfg_path)
    loaded = mgr.load()
    assert loaded.api.api_key == "env-override-key"

    assert mgr.save(loaded) is True
    on_disk_after_save = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk_after_save["api"]["api_key"] == "disk-secret-key"

    patched = mgr.patch({"analysis": {"block_size": 3}})
    assert patched is not None
    assert patched.api.api_key == "env-override-key"
    assert patched.analysis.block_size == 3
    on_disk_after_patch = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk_after_patch["api"]["api_key"] == "disk-secret-key"
    assert on_disk_after_patch["analysis"]["block_size"] == 3


def test_schema_resolve_spec_parse_cli_value_and_promoted_validation():
    """resolve_spec/parse_cli_value 与新增调优参数范围校验（含 block_size=200、enum 等）应准确生效。"""
    import pytest
    from backend.config.schema import parse_cli_value, resolve_spec

    assert resolve_spec("api.max_tokens").path == "api.max_tokens"
    assert resolve_spec("max_tokens").path == "api.max_tokens"
    assert resolve_spec("recheck_v3_mode").path == "analysis.recheck_v3_mode"
    with pytest.raises(KeyError):
        resolve_spec("nonexistent_key_xyz")

    assert parse_cli_value(resolve_spec("analysis.block_size"), "42") == 42
    assert parse_cli_value(resolve_spec("analysis.volume_compress_ratio"), "0.35") == 0.35
    assert parse_cli_value(resolve_spec("analysis.auto_archive"), "true") is True
    assert parse_cli_value(resolve_spec("analysis.auto_archive"), "off") is False
    assert parse_cli_value(resolve_spec("working_directory"), "") is None
    assert parse_cli_value(resolve_spec("analysis.encoding_priority"), "utf-8, gbk , utf-8-sig") == [
        "utf-8", "gbk", "utf-8-sig",
    ]
    assert parse_cli_value(resolve_spec("api.thinking_mode"), '{"reasoning_effort": "low"}') == {
        "reasoning_effort": "low",
    }

    assert validate_settings_payload({
        "analysis": {
            "block_size": 200,
            "recheck_v3_mode": "wide",
            "recheck_v3_max_hops": 5,
            "volume_compress_ratio": 0.5,
        },
        "api": {"breaker_open_after": 3, "temp_retry_wait_base": 5},
    }) == []

    bad = validate_settings_payload({
        "analysis": {
            "block_size": 201,
            "recheck_v3_mode": "invalid_mode",
            "recheck_v3_max_hops": 9,
            "volume_compress_ratio": 1.05,
        }
    })
    bad_locs = {tuple(e["loc"]) for e in bad}
    assert ("body", "analysis", "block_size") in bad_locs
    assert ("body", "analysis", "recheck_v3_mode") in bad_locs
    assert ("body", "analysis", "recheck_v3_max_hops") in bad_locs
    assert ("body", "analysis", "volume_compress_ratio") in bad_locs


def test_builtin_profiles_valid_and_preserve_endpoints_and_paths(tmp_path):
    """3 个内置通用预设须通过全量校验、归位同域残留并绝不覆盖连接/模型/思考/分类/路径。"""
    from backend.config.profiles import BUILTIN_PROFILES, BUILTIN_SLOT_IDS, ProfileStore

    for slot_id in BUILTIN_SLOT_IDS:
        assert validate_settings_payload(BUILTIN_PROFILES[slot_id]["patch"]) == []

    cfg_path = tmp_path / "config.json"
    mgr = ConfigManager(cfg_path)
    seed = AppConfig.from_dict({
        "working_directory": "D:\\books\\novel_a",
        "workspace_dir": "custom_ws",
        "knowledge_file": "custom_kb.json",
        "gui": {"language": "en-US"},
        "api": {
            "base_url": "https://gateway.local:3425/v1",
            "api_key": "sk-keep-main",
            "model": "MiniMax-M3.1",
            "summary_model": "gpt-6-luna",
            "provider": "anthropic",
            "json_mode": "deepseek",
            "thinking_mode": {"reasoning_effort": "low"},
            "summary_thinking_mode": {"thinking": {"type": "disabled"}},
            "reasoning_effort": "low",
            "streaming_enabled": False,
        },
        "analysis": {
            "foreshadow_kept_categories": ["身份", "其他"],
            "auto_archive": True,
            "auto_summary": True,
            "recheck_model": "codex/gpt-6-luna",
            "recheck_base_url": "https://recheck.local/v1",
            "recheck_api_key": "sk-keep-recheck",
            "recheck_provider": "openai",
            "recheck_json_mode": "qwen",
            "recheck_reasoning_effort": "medium",
            # 故意写入将被内置槽归位的策略/复检旧值
            "recheck_max_tokens": 2048,
            "recheck_v3_max_audit_windows": 9,
            "volume_compress_group": 12,
            "rolling_max_momentum": 3,
            "recheck_v3_hop3_new_cap": 7,
        },
    })
    assert mgr.save(seed) is True

    store = ProfileStore.for_config_manager(mgr)
    res = store.apply_slot(mgr, "256K")
    assert res["applied_slot_id"] == "builtin_256k"
    assert res["profiles"]["active_slot_id"] == "builtin_256k"
    assert res["profiles"]["is_modified_from_last"] is False

    after = AppConfig.from_dict(res["config"])
    # 策略与上下文参数已切换到 256K 档并归位残留
    assert after.analysis.block_size == 2
    assert after.analysis.recheck_v3_pool_max_chars == 180000
    assert after.analysis.recheck_max_tokens == 32768
    assert after.analysis.recheck_v3_max_audit_windows == 0
    assert after.analysis.volume_compress_group == 5
    assert after.analysis.rolling_max_momentum == 15
    assert after.analysis.recheck_v3_hop3_new_cap == 0

    # 连接/密钥/模型/思考/分类/路径原样保留
    assert after.api.base_url == "https://gateway.local:3425/v1"
    assert after.api.api_key == "sk-keep-main"
    assert after.api.model == "MiniMax-M3.1"
    assert after.api.summary_model == "gpt-6-luna"
    assert after.api.provider == "anthropic"
    assert after.api.thinking_mode == {"reasoning_effort": "low"}
    assert after.api.summary_thinking_mode == {"thinking": {"type": "disabled"}}
    assert after.api.streaming_enabled is False
    assert after.analysis.recheck_model == "codex/gpt-6-luna"
    assert after.analysis.recheck_base_url == "https://recheck.local/v1"
    assert after.analysis.recheck_api_key == "sk-keep-recheck"
    assert after.analysis.foreshadow_kept_categories == ["身份", "其他"]
    assert after.analysis.auto_archive is True
    assert after.analysis.auto_summary is True
    assert after.working_directory == "D:\\books\\novel_a"
    assert after.workspace_dir == "custom_ws"
    assert after.knowledge_file == "custom_kb.json"
    assert after.gui.language == "en-US"

    # 微调参数后 is_modified_from_last=True 且 active_slot_id=None
    mgr.patch({"analysis": {"concurrency": 12}})
    profiles_after_tweak = store.list_profiles(mgr)
    assert profiles_after_tweak["active_slot_id"] is None
    assert profiles_after_tweak["last_applied_slot_id"] == "builtin_256k"
    assert profiles_after_tweak["is_modified_from_last"] is True


def test_user_slots_security_cross_origin_env_key_and_corrupt_store(tmp_path, monkeypatch):
    """覆盖用户槽凭据脱敏、跨端点孤儿复检 Key 清空、环境变量保护、bool/int 匹配与损坏文件护栏。"""
    import pytest
    from backend.config.profiles import ProfileError, ProfileStore

    cfg_path = tmp_path / "config.json"
    mgr = ConfigManager(cfg_path)
    seed = AppConfig.from_dict({
        "api": {
            "base_url": "https://user:pass@slot1.example.com/v1?api_key=query_secret",
            "api_key": "sk-slot1-main",
            "model": "model-slot1",
            "thinking_mode": {"enable_thinking": False},
        },
        "analysis": {
            "concurrency": 6,
            "recheck_base_url": "https://recheck-a.example.com/v1",
            "recheck_api_key": "sk-slot1-recheck",
        },
    })
    mgr.save(seed)
    store = ProfileStore.for_config_manager(mgr)

    # 1. include_credentials=False：剥离密钥 + URL 脱敏 + GET 不暴露凭据
    saved = store.save_user_slot(mgr, "user_1", name="日常槽", description="双通道", include_credentials=False)
    slot1 = saved["slot"]
    assert slot1["has_api_key"] is False
    assert slot1["has_recheck_api_key"] is False
    assert slot1["summary"]["base_url"] == "https://slot1.example.com/v1"
    assert "pass" not in json.dumps(saved, ensure_ascii=False)
    assert "sk-slot1" not in json.dumps(saved, ensure_ascii=False)

    # 2. 切到另一复检通道后应用 user_1（include_credentials=False）-> 清空孤儿 recheck_api_key 并提示主端点变化
    mgr.patch({
        "api": {"base_url": "https://other.example.com/v1", "api_key": "sk-other"},
        "analysis": {
            "recheck_base_url": "https://recheck-b.example.com/v1",
            "recheck_api_key": "sk-orphan-b",
        },
    })
    applied_u1 = store.apply_slot(mgr, "user_1")
    assert applied_u1["config"]["analysis"]["recheck_api_key"] == ""
    assert applied_u1["config"]["api"]["api_key"] == "sk-other"
    assert len(applied_u1["warnings"]) == 2
    assert applied_u1["profiles"]["active_slot_id"] == "user_1"

    # bool vs int 严格区分：thinking_mode={enable_thinking: 0} 不能误匹配 False
    mgr.patch({"api": {"thinking_mode": {"enable_thinking": 0}}})
    assert store.list_profiles(mgr)["active_slot_id"] is None

    # 3. 环境变量保护：LLM_API_KEY 生效时，include_credentials=True 不将其写入磁盘槽位
    monkeypatch.setenv("LLM_API_KEY", "sk-env-secret")
    saved_env = store.save_user_slot(mgr, "user_2", name="环境槽", include_credentials=True)
    assert saved_env["slot"]["has_api_key"] is False
    assert saved_env["warnings"]
    disk_raw = json.loads(store.profiles_path.read_text(encoding="utf-8"))
    assert "api_key" not in disk_raw["slots"]["user_2"]["patch"]["api"]

    # 明确传入非环境变量密钥落盘，应用后仍以 _disk_api_key 维持 active_slot_id 稳定
    custom_raw = AppConfig.from_dict({
        "api": {"base_url": "https://slot3.example.com/v1", "api_key": "sk-explicit-disk", "model": "m3"},
        "analysis": {"concurrency": 4},
    }).to_dict()
    store.save_user_slot(mgr, "user_3", name="独立密钥槽", include_credentials=True, raw_config=custom_raw)
    applied_u3 = store.apply_slot(mgr, "user_3")
    assert applied_u3["config"]["api"]["api_key"] == "sk-env-secret"
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["api"]["api_key"] == "sk-explicit-disk"
    assert applied_u3["profiles"]["active_slot_id"] == "user_3"
    assert store.list_profiles(mgr)["active_slot_id"] == "user_3"
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    # 4. 清空槽位后清除 last_applied_slot_id
    cleared = store.clear_user_slot(mgr, "user_3")
    assert cleared["cleared"] == "user_3"
    assert cleared["profiles"]["last_applied_slot_id"] is None

    # 5. config_profiles.json 损坏保护：拒绝写入用户槽且不覆盖原文件，仍可应用内置槽
    broken = '{"slots": '
    store.profiles_path.write_text(broken, encoding="utf-8")
    listed_broken = store.list_profiles(mgr)
    assert listed_broken["store_corrupted"] is True
    with pytest.raises(ProfileError) as exc_save:
        store.save_user_slot(mgr, "user_1", name="覆写失败")
    assert exc_save.value.status_code == 409
    assert store.profiles_path.read_text(encoding="utf-8") == broken

    res_builtin = store.apply_slot(mgr, "builtin_128k")
    assert res_builtin["applied_slot_id"] == "builtin_128k"
    assert res_builtin["profiles"]["store_corrupted"] is True
    assert store.profiles_path.read_text(encoding="utf-8") == broken
