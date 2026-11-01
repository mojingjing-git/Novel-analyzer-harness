"""网关以 OpenAI 协议暴露 claude-* 时，预检应探测出正确协议并就地纠正"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import AppConfig
from backend.services.queue_manager import QueueManager


def _cfg(provider="auto"):
    c = AppConfig()
    c.api.model = "claude-3-5-sonnet"
    c.api.base_url = "https://gw.example.com/v1"
    c.api.provider = provider
    return c


def _svc():
    # check_api 定义在 QueueManager 上（AnalysisService 经 self.queue 调用），
    # 且不依赖实例状态——用 __new__ 绕过构造器
    return QueueManager.__new__(QueueManager)


def test_cross_protocol_probe_heals_and_returns_true(monkeypatch):
    calls = []

    async def fake_list_models(base_url, api_key, provider):
        calls.append(provider)
        if provider == "openai":
            return ["claude-3-5-sonnet"]
        raise RuntimeError("404 not found")

    monkeypatch.setattr(
        "backend.services.queue_manager.LLMClient.list_models", fake_list_models)
    # 原生 chat 探针封掉（避免真实网络调用）：无论何种 provider 都失败
    async def fake_chat_fail(self, messages, *a, **k):
        return False, "", "down", (0, 0)
    monkeypatch.setattr(
        "backend.services.queue_manager.LLMClient.chat", fake_chat_fail)

    cfg = _cfg()
    ok = asyncio.run(_svc().check_api(cfg))
    assert ok is True
    assert cfg.api.provider == "openai", "预检成功后应就地纠正 provider"


def test_both_protocols_fail_returns_false(monkeypatch):
    async def always_fail(base_url, api_key, provider):
        raise RuntimeError("down")

    monkeypatch.setattr(
        "backend.services.queue_manager.LLMClient.list_models", always_fail)
    # 同时封掉 chat 探针与其翻转复试
    class FakeClient:
        def __init__(self, config): pass
        async def chat(self, messages):
            return False, "", "down", (0, 0)
    monkeypatch.setattr(
        "backend.services.queue_manager.LLMClient", FakeClient)

    cfg = _cfg()
    ok = asyncio.run(_svc().check_api(cfg))
    assert ok is False
    assert cfg.api.provider in ("auto", "anthropic"), "全败时不得盲目改写 provider"


def test_models_ok_with_claude_prefix_heals_to_openai(monkeypatch):
    """/models 可用的聚合网关（OpenAI 面）+ claude-* 模型
    ——models 早退绕过纠正时，整书仍会 404"""
    async def fake_list_models(base_url, api_key, provider):
        # auto 解析走 OpenAI /models 分支：成功
        return ["claude-3-5-sonnet"]

    monkeypatch.setattr(
        "backend.services.queue_manager.LLMClient.list_models", fake_list_models)

    cfg = _cfg()   # 文件内既有 helper：model=claude-*、provider=auto
    ok = asyncio.run(QueueManager.__new__(QueueManager).check_api(cfg))
    assert ok is True
    assert cfg.api.provider == "openai"


def test_non_claude_model_not_touched(monkeypatch):
    async def fake_list_models(base_url, api_key, provider):
        return ["gpt-4o"]
    monkeypatch.setattr(
        "backend.services.queue_manager.LLMClient.list_models", fake_list_models)

    cfg = _cfg()
    cfg.api.model = "gpt-4o"
    ok = asyncio.run(QueueManager.__new__(QueueManager).check_api(cfg))
    assert ok is True
    assert cfg.api.provider == "auto", "非 claude 模型不得改写 provider"


def test_official_anthropic_direct_not_healed(monkeypatch):
    """官方 Anthropic 直连（auto + anthropic URL）不得被改写为 openai"""
    async def fake_list_models(base_url, api_key, provider):
        return ["claude-3-5-sonnet"]   # Anthropic 面 /models 成功
    monkeypatch.setattr(
        "backend.services.queue_manager.LLMClient.list_models", fake_list_models)

    cfg = _cfg()
    cfg.api.base_url = "https://api.anthropic.com"
    ok = asyncio.run(QueueManager.__new__(QueueManager).check_api(cfg))
    assert ok is True
    assert cfg.api.provider == "auto", "官方直连不得被纠正"


def test_heal_persists_via_config_manager(monkeypatch, tmp_path):
    """纠正值必须写入 config_manager 并落盘，
    否则总结链路 config_manager.load() 重读磁盘后丢失"""
    saved = {}
    class FakeCM:
        def __init__(self):
            from backend.config.settings import AppConfig
            self.config = AppConfig()
            self.config.api.model = "claude-3-5-sonnet"
        def save(self):
            saved["provider"] = self.config.api.provider
            return True
    svc = QueueManager.__new__(QueueManager)
    svc.config_manager = FakeCM()  # type: ignore[assignment]

    async def fake_list_models(base_url, api_key, provider):
        return ["claude-3-5-sonnet"]
    monkeypatch.setattr(
        "backend.services.queue_manager.LLMClient.list_models", fake_list_models)

    cfg = svc.config_manager.config  # type: ignore[union-attr]
    ok = asyncio.run(svc.check_api(cfg))
    assert ok and cfg.api.provider == "openai"
    assert saved.get("provider") == "openai", "纠正值必须经 config_manager.save() 落盘"
