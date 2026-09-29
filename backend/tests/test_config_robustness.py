"""
测试 ConfigManager BOM 容错（#1）与 anthropic list_models 空 base_url 守卫（#5）
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import ConfigManager, AppConfig
from backend.core.llm_client import LLMClient


def test_config_load_with_bom(tmp_path):
    """#1: 带 UTF-8 BOM 的 config.json 应正常加载（utf-8-sig）"""
    cfg_path = tmp_path / "config.json"
    data = {"api": {"model": "test-model", "base_url": "https://api.test.com",
                    "api_key": "k", "provider": "auto"}}
    with open(cfg_path, "wb") as f:
        f.write(b"\xef\xbb\xbf" + json.dumps(data, ensure_ascii=False).encode("utf-8"))
    mgr = ConfigManager(cfg_path)
    cfg = mgr.load()
    assert cfg.api.model == "test-model"
    assert cfg.api.base_url == "https://api.test.com"
    print("✅ test_config_load_with_bom passed")


def test_config_load_corrupt_logs_error(tmp_path, caplog):
    """#1: 损坏的 config.json 必须记 ERROR 日志而非静默 print"""
    import logging
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"api": {"model": "x",', encoding="utf-8")  # 截断的 JSON
    mgr = ConfigManager(cfg_path)
    with caplog.at_level(logging.ERROR, logger="backend.config.settings"):
        cfg = mgr.load()
    assert cfg.api.model == ""  # 回退默认
    assert any("配置加载失败" in r.message for r in caplog.records)
    print("✅ test_config_load_corrupt_logs_error passed")


async def test_list_models_anthropic_empty_base_url():
    """#5: anthropic 格式且 base_url 为空 → 直接返回空列表，不发 GET /models"""
    from backend.core.llm_client import LLMClient
    with patch("backend.core.llm_client.AsyncOpenAI") as fake_cls:
        result = await LLMClient.list_models("", "k", "anthropic")
    assert result == []
    fake_cls.assert_not_called()
    print("✅ test_list_models_anthropic_empty_base_url passed")


def test_from_dict_coerces_string_numbers():
    """P1（2026-08-24）：PUT /api/settings 的字符串数值必须被强转，
    否则入库后 load() 的 max_tokens > 65537 比较抛 TypeError（try 外）全 API 瘫痪"""
    cfg = AppConfig.from_dict({"api": {"max_tokens": "130000", "timeout": "30"},
                               "analysis": {"concurrency": "4"}})
    assert isinstance(cfg.api.max_tokens, int) and cfg.api.max_tokens == 130000
    assert isinstance(cfg.api.timeout, int) and cfg.api.timeout == 30
    assert isinstance(cfg.analysis.concurrency, int) and cfg.analysis.concurrency == 4


def test_from_dict_coerces_bool_strings():
    cfg = AppConfig.from_dict({"analysis": {"auto_archive": "true", "skip_moderation_blocked": 0}})
    assert cfg.analysis.auto_archive is True
    assert cfg.analysis.skip_moderation_blocked is False


def test_from_dict_drops_garbage_numeric():
    cfg = AppConfig.from_dict({"api": {"max_tokens": "abc"}, "analysis": {"checkpoint_interval": "宽"}})
    from backend.config.constants import MAX_OUTPUT_TOKENS
    assert cfg.api.max_tokens == MAX_OUTPUT_TOKENS      # 回落默认
    assert cfg.analysis.checkpoint_interval == 5        # 回落默认


def test_load_survives_string_max_tokens_on_disk(tmp_path):
    import json
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"api": {"max_tokens": "130000"}}), encoding="utf-8")
    from backend.config.settings import ConfigManager
    cfg = ConfigManager(p).load()   # 此前在这里直接 TypeError
    assert cfg.api.max_tokens == 130000


def test_corrupt_config_blocks_save(tmp_path):
    """P1（2026-08-24）：config.json 解析失败未修复前，save() 必须拒绝写入，
    防止默认配置（空 api_key）原子覆盖掉仍可手工修复的原文件"""
    p = tmp_path / "config.json"
    broken = '{"api": BROKEN'
    p.write_text(broken, encoding="utf-8")

    from backend.config.settings import AppConfig, ConfigManager
    cm = ConfigManager(p)
    cm.load()
    assert cm._load_failed is True

    assert cm.save(AppConfig()) is False
    assert p.read_text(encoding="utf-8") == broken, "磁盘上的坏文件必须原样保留（等待手工修复）"


def test_healthy_config_still_saves(tmp_path):
    import json
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"api": {"model": "gpt-x"}}), encoding="utf-8")

    from backend.config.settings import AppConfig, ConfigManager
    cm = ConfigManager(p)
    cm.load()
    assert cm._load_failed is False
    assert cm.save(AppConfig()) is True
    # 正常覆盖：磁盘内容已变为序列化后的默认配置（DEFAULT_MODEL 为空串，
    # 故不能断言 model 为真值，改为整体比对确认覆盖确实发生）
    assert json.loads(p.read_text(encoding="utf-8")) == AppConfig().to_dict()


def test_gui_language_default_and_coercion():
    """L2 i18n 接口预留（2026-09-02）：GUIConfig.language 字段默认值 + _coerce_fields 强转"""
    # 缺省回落：旧 config.json 没有 gui.language → 默认 "zh-CN"
    cfg = AppConfig.from_dict({"gui": {}})
    assert cfg.gui.language == "zh-CN"

    # 强转：int → str（_coerce_fields 走 str 分支，line 112-113）
    cfg2 = AppConfig.from_dict({"gui": {"language": 123}})
    assert cfg2.gui.language == "123"

    # 正常字符串透传
    cfg3 = AppConfig.from_dict({"gui": {"language": "en"}})
    assert cfg3.gui.language == "en"
