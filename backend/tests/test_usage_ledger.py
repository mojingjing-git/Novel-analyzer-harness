"""持久用量账本（UsageLedger）的累积/持久化/清零/费用契约。

口径（AGY 复审）：per_model 是费用唯一依据（跨书换模型不串价）；
categories 仅展示；cached 走 payload 直读 delta；失败/重试 token 单列不进费用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.auth import _ENV_VAR
from backend.services.usage_ledger import UsageLedger


@pytest.fixture()
def ledger(tmp_path) -> UsageLedger:
    return UsageLedger(tmp_path / "usage_state.json")


def _chapter_payload(**kw):
    base = {"chapter": 1, "status": "done", "elapsed": 12.5,
            "retries": 1, "failed_tokens": 100, "cached_tokens": 500}
    base.update(kw)
    return base


def test_add_token_accumulates_per_model_and_categories(ledger):
    ledger.add_token({"category": "chapter", "input_tokens": 1000, "output_tokens": 200,
                      "model": "MiniMax-M3.1-Flash", "cached_tokens": 300})
    ledger.add_token({"category": "recheck", "input_tokens": 500, "output_tokens": 50,
                      "model": "codex/gpt-6-luna"})
    snap = ledger.snapshot()
    assert snap["per_model"]["MiniMax-M3.1-Flash"] == {"in": 1000, "out": 200, "cached": 300}
    assert snap["per_model"]["codex/gpt-6-luna"] == {"in": 500, "out": 50, "cached": 0}
    assert snap["categories"]["chapter"] == {"in": 1000, "out": 200}
    assert sorted(snap["category_models"]["recheck"]) == ["codex/gpt-6-luna"]


def test_add_token_without_model_goes_to_unknown(ledger):
    ledger.add_token({"category": "chapter", "input_tokens": 10, "output_tokens": 1})
    assert ledger.snapshot()["per_model"]["unknown"] == {"in": 10, "out": 1, "cached": 0}


def test_add_chapter_counts_done_retries_failed_elapsed(ledger):
    ledger.add_chapter(_chapter_payload())
    ledger.add_chapter(_chapter_payload(status="failed", cached_tokens=0))
    ledger.add_chapter(_chapter_payload(model="MiniMax-M3.1-Flash", cached_tokens=500))
    snap = ledger.snapshot()
    assert snap["chapters_done"] == 2          # failed 不计章数
    assert snap["retries"] == 3
    assert snap["failed_tokens"] == 300
    assert abs(snap["elapsed_seconds"] - 37.5) < 1e-9
    # 缓存 delta 挂 per_model（模型名来自 payload；无 model 的 chapter 不计缓存）
    assert snap["per_model"]["MiniMax-M3.1-Flash"]["cached"] == 500


def test_persistence_roundtrip(tmp_path):
    p = tmp_path / "usage_state.json"
    led = UsageLedger(p)
    led.add_token({"category": "chapter", "input_tokens": 42, "output_tokens": 7,
                   "model": "MiniMax-M3.1-Flash"})
    led.save_sync()
    assert p.exists()
    led2 = UsageLedger(p)  # 模拟重启
    assert led2.snapshot()["per_model"]["MiniMax-M3.1-Flash"]["in"] == 42


def test_load_corrupted_file_degrades_to_empty(tmp_path):
    p = tmp_path / "usage_state.json"
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    led = UsageLedger(p)  # 不抛异常
    assert led.snapshot()["per_model"] == {}


def test_clear_zeroes_and_backs_up(ledger):
    ledger.add_token({"category": "chapter", "input_tokens": 5, "output_tokens": 1,
                      "model": "MiniMax-M3.1-Flash"})
    ledger.save_sync()
    ledger.clear()
    assert ledger.snapshot()["per_model"] == {}
    assert ledger.path.with_suffix(".json.bak").exists()
    assert json.loads(ledger.path.read_text(encoding="utf-8"))["per_model"] == {}


def test_usage_route_fees_and_unpriced(ledger, monkeypatch):
    ledger.add_token({"category": "chapter", "input_tokens": 1_000_000, "output_tokens": 100_000,
                      "model": "MiniMax-M3.1-Flash"})
    ledger.add_token({"category": "recheck", "input_tokens": 10, "output_tokens": 0,
                      "model": "自定义未定价模型"})
    ledger.save_sync()
    monkeypatch.setattr("backend.services.usage_ledger.get_usage_ledger", lambda: ledger)

    from backend.api import routes_analysis
    app = FastAPI()
    app.include_router(routes_analysis.router)
    client = TestClient(app)
    resp = client.get("/api/analysis/usage")
    assert resp.status_code == 200
    data = resp.json()
    rows = {r["model"]: r for r in data["per_model"]}
    # 1M×0.15 + 100k×0.6 = 0.21（缓存 0）
    assert abs(rows["MiniMax-M3.1-Flash"]["usd"] - 0.21) < 1e-6
    assert rows["自定义未定价模型"]["usd"] is None
    assert data["unpriced"] == ["自定义未定价模型"]
    assert data["total_usd"] == 0.21
    assert data["catalog_version"]


def test_usage_route_all_unpriced_is_null_not_zero(ledger, monkeypatch):
    ledger.add_token({"category": "chapter", "input_tokens": 999, "output_tokens": 1,
                      "model": "未知模型"})
    monkeypatch.setattr("backend.services.usage_ledger.get_usage_ledger", lambda: ledger)
    from backend.api import routes_analysis
    app = FastAPI()
    app.include_router(routes_analysis.router)
    data = TestClient(app).get("/api/analysis/usage").json()
    assert data["total_usd"] is None


def test_usage_route_empty_is_zero(ledger, monkeypatch):
    monkeypatch.setattr("backend.services.usage_ledger.get_usage_ledger", lambda: ledger)
    from backend.api import routes_analysis
    app = FastAPI()
    app.include_router(routes_analysis.router)
    assert TestClient(app).get("/api/analysis/usage").json()["total_usd"] == 0.0


def test_usage_clear_requires_session_token(monkeypatch, tmp_path):
    ledger = UsageLedger(tmp_path / "usage_state.json")
    monkeypatch.setattr("backend.services.usage_ledger.get_usage_ledger", lambda: ledger)
    monkeypatch.setenv(_ENV_VAR, "secret-token")  # 启用鉴权（开发模式无 env 时放行）
    from backend.api import routes_analysis
    app = FastAPI()
    app.include_router(routes_analysis.router)
    resp = TestClient(app).post("/api/analysis/usage/clear")
    assert resp.status_code == 403  # 破坏性操作必须鉴权（无 header）
    ok = TestClient(app).post("/api/analysis/usage/clear",
                              headers={"X-Session-Token": "secret-token"})
    assert ok.status_code == 200
    assert ok.json()["success"] is True
