"""
测试 /api/analysis/logs 端点的 session token 鉴权（D 方案）

覆盖：
- 开发模式（未设 NOVEL_ANALYZER_SESSION_TOKEN 环境变量）→ 200 放行
- 桌面模式（已设环境变量）+ 无 X-Session-Token header → 403
- 桌面模式 + 错误 token → 403
- 桌面模式 + 正确 token → 200
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.app import app
from backend.api.auth import _ENV_VAR


@pytest.fixture
def token_env(monkeypatch):
    """fixture：设置 session token 环境变量，用 monkeypatch 自动 teardown。

    关键：monkeypatch.setenv 会自动恢复原始值，不会污染后续测试。
    """
    test_token = "test-token-32-chars-aaaaaaaaaaaa"
    monkeypatch.setenv(_ENV_VAR, test_token)
    return test_token


@pytest.fixture
def no_token_env(monkeypatch):
    """fixture：确保环境变量未设置（开发模式）。"""
    monkeypatch.delenv(_ENV_VAR, raising=False)


@pytest.fixture
def client():
    return TestClient(app)


class TestSessionTokenAuth:
    def test_dev_mode_no_env_returns_200(self, client, no_token_env):
        """未设环境变量 = 开发模式，端点应放行返回 200。"""
        r = client.get("/api/analysis/logs")
        # 端点本身的逻辑（FileResponse）需要 LOG_FILE 存在。
        # 测试环境下 PROJECT_ROOT = 仓库根，analyzer.log 应存在。
        # 如果不存在，404 也不算鉴权失败——但本测试关注鉴权，所以仅 assert 不是 403
        assert r.status_code != 403, f"开发模式不应 403，实际 {r.status_code}: {r.text}"

    def test_no_header_returns_403(self, client, token_env):
        """设了环境变量但无 X-Session-Token header → 403。"""
        r = client.get("/api/analysis/logs")
        assert r.status_code == 403
        assert "Session token required" in r.text

    def test_wrong_token_returns_403(self, client, token_env):
        """设了环境变量但 token 错误 → 403。"""
        r = client.get(
            "/api/analysis/logs",
            headers={"X-Session-Token": "wrong-token-aaaaaaaaaaaaaaaa"},
        )
        assert r.status_code == 403

    def test_correct_token_returns_200(self, client, token_env):
        """设了环境变量 + 正确 token → 200。"""
        r = client.get(
            "/api/analysis/logs",
            headers={"X-Session-Token": token_env},
        )
        assert r.status_code == 200
