"""probe-recheck 端点：复检判定通道的三级回退与异常兜底。

回退链：请求字段 > analysis.recheck_*（复检专用通道）> 主 api.*。
探测是微请求（probe_thinking_params），不写配置、不产生判定调用。
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes_settings import router
from backend.config.settings import AppConfig


app = FastAPI()
app.include_router(router)
client = TestClient(app)


class _StubConfigManager:
    def __init__(self, config):
        self._config = config

    def load(self):
        return self._config


class _StubService:
    def __init__(self, config):
        self.config_manager = _StubConfigManager(config)

    @property
    def is_running(self):
        return False


def _probe_payload():
    """probe_thinking_params 的最小返回形状（端点原样透传）。"""
    return {"endpoint_thinking": "enable_thinking", "attempts": [{"param": "enable_thinking"}]}


def _post(client, body):
    return client.post("/api/settings/probe-recheck", json=body)


def test_fallback_chain_request_beats_recheck_beats_main():
    """三级回退：请求字段 > 复检专用通道 > 主配置。"""
    cfg = AppConfig()
    cfg.analysis.recheck_base_url = "http://recheck:3425/v1"
    cfg.analysis.recheck_api_key = "sk-recheck"
    cfg.api.base_url = "https://api.minimaxi.com/v1"
    cfg.api.api_key = "sk-main"
    cfg.api.model = "main-model"

    seen = {}

    async def fake_probe(base_url, api_key, model, provider):
        seen.update(base_url=base_url, api_key=api_key, model=model, provider=provider)
        return _probe_payload()

    stub = _StubService(cfg)
    with patch("backend.api.routes_settings.get_service", return_value=stub), \
         patch("backend.core.llm_probe.probe_thinking_params", new=fake_probe):
        resp = _post(client, {"model": "judge-x"})

    assert resp.status_code == 200
    assert seen == {
        "base_url": "http://recheck:3425/v1",   # 复检通道优先于主配置
        "api_key": "sk-recheck",                # 复检 Key 优先于主 Key
        "model": "judge-x",                     # 请求字段最优先
        "provider": "auto",                     # 全空 → 主配置的 provider（默认 auto）
    }
    assert resp.json() == _probe_payload()


def test_all_empty_falls_back_to_main_channel():
    """复检通道全空 = 探测主通道（与复检"跟随主配置"的语义一致）。"""
    cfg = AppConfig()
    seen = {}

    async def fake_probe(base_url, api_key, model, provider):
        seen.update(base_url=base_url, api_key=api_key, model=model)
        return _probe_payload()

    stub = _StubService(cfg)
    with patch("backend.api.routes_settings.get_service", return_value=stub), \
         patch("backend.core.llm_probe.probe_thinking_params", new=fake_probe):
        resp = _post(client, {})

    assert resp.status_code == 200
    assert seen["base_url"] == cfg.api.base_url
    assert seen["api_key"] == cfg.api.api_key
    assert seen["model"] == cfg.api.model


def test_probe_failure_maps_to_502():
    """端点不可达 → 502 + 后端原因，不裸抛 500。"""
    import asyncio

    cfg = AppConfig()

    async def fake_probe(base_url, api_key, model, provider):
        raise RuntimeError("connect timeout")

    stub = _StubService(cfg)
    with patch("backend.api.routes_settings.get_service", return_value=stub), \
         patch("backend.core.llm_probe.probe_thinking_params", new=fake_probe):
        resp = _post(client, {})

    assert resp.status_code == 502
    assert "connect timeout" in resp.json()["detail"]
