"""probe-thinking 路由的 model 参数透传（2026-09-30，总结模型探测支持）

ModelPreviewRequest 新增可选 model：总结页对 summary_model 独立探测
（复用同一 base_url/api_key）。不传时必须与旧行为逐字节等价（探测主模型）。

不挂 test_anthropic_provider.py——那是 probe_thinking_params 函数级测试
（直测检测逻辑），本文件是路由层契约（参数透传与回退）。
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.app import app


@pytest.fixture
def client():
    # 不用 with：避免触发 lifespan 的工作区扫描
    return TestClient(app)


@pytest.fixture
def probe_spy(monkeypatch):
    """替换 llm_probe.probe_thinking_params（routes_settings 在函数体内 import，
    patch 模块属性即可生效），记录收到的 model 参数并返回固定结果。"""
    calls: list[str] = []

    async def fake_probe(base_url, api_key, model, provider):
        calls.append(model)
        return {"results": [], "best": None, "default_thinks": False,
                "default_detection_breakdown": {}, "note": "mock"}

    monkeypatch.setattr("backend.core.llm_probe.probe_thinking_params", fake_probe)
    return calls


class TestProbeThinkingModelParam:
    def test_model_param_passthrough(self, client, probe_spy):
        """显式传 model → 透传给 probe_thinking_params（总结模型探测）"""
        r = client.post("/api/settings/probe-thinking", json={"model": "MiniMax-M3"})
        assert r.status_code == 200
        assert probe_spy == ["MiniMax-M3"], f"model 未透传: {probe_spy}"

    def test_no_model_falls_back_to_main_model(self, client, probe_spy):
        """不传 model → 回退已保存配置的主模型（旧前端兼容，行为逐字节等价）"""
        r = client.post("/api/settings/probe-thinking", json={})
        assert r.status_code == 200
        assert len(probe_spy) == 1
        assert probe_spy[0], "回退模型不应为空（config.json 必有 api.model）"
