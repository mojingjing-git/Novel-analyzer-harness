"""队列管理（QueueManager）、ETA 估算与用量账本（UsageLedger）测试。"""
from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_analysis
from backend.api.auth import _ENV_VAR
from backend.services.pipeline_events import _compute_eta, _format_eta
from backend.services.queue_manager import QueueItem, QueueManager
from backend.services.queue_service import AnalysisService
from backend.services import summary_service
from backend.services.usage_ledger import UsageLedger


def test_queue_item_and_manager():
    item = QueueItem(
        name="测试小说", blocks_dir=Path("/workspace/测试/blocks"),
        workspace_dir=Path("/workspace/测试"), status="done", total_chapters=100, completed_chapters=80,
    )
    assert QueueItem.from_dict(item.to_dict()).name == "测试小说"

    qm = QueueManager()
    a = QueueItem(name="A", blocks_dir=Path("/a/b"), workspace_dir=Path("/a"))
    b = QueueItem(name="B", blocks_dir=Path("/b/b"), workspace_dir=Path("/b"))
    c = QueueItem(name="C", blocks_dir=Path("/c/b"), workspace_dir=Path("/c"))
    qm.add_item(a)
    qm.add_item(QueueItem(name="A重复", blocks_dir=Path("/a/b"), workspace_dir=Path("/a")))
    assert qm.count == 1
    qm.add_item(b)
    qm.add_item(c)
    assert qm.move_up(2) and [x.name for x in qm.items] == ["A", "C", "B"]

    cur = qm.get_current()
    assert cur is not None and cur.name == "A"
    cur.status = "running"
    nxt = qm.advance()
    assert a.status == "done" and nxt is not None and nxt.name == "C"


def test_start_analysis_reports_summary_conflict(monkeypatch):
    service = AnalysisService.__new__(AnalysisService)
    service._runner_task = None
    service.queue = QueueManager()
    service.queue.add_item(QueueItem(name="待分析", blocks_dir=Path("blocks"), workspace_dir=Path("workspace")))
    summary = SimpleNamespace(is_running=True)
    monkeypatch.setattr(routes_analysis, "get_service", lambda: service)
    monkeypatch.setattr(summary_service, "get_summary_service", lambda: summary)
    app = FastAPI()
    app.include_router(routes_analysis.router)
    client = TestClient(app)

    response = client.post("/api/analysis/start")
    assert response.status_code == 409
    assert "最终总结正在运行" in response.json()["detail"]
    assert service._runner_task is None

    summary.is_running = False
    service.queue.items[0].status = "done"
    response = client.post("/api/analysis/start")
    assert response.status_code == 400
    assert response.json()["detail"] == "队列为空或已全部完成"


def test_eta_formatting_and_computation():
    assert _format_eta(0) == "" and _format_eta(-5) == "" and _format_eta(math.nan) == ""
    assert _format_eta(30) == "30秒"
    assert _format_eta(90) == "1分30秒"
    assert _format_eta(3660) == "1时01分"

    assert _compute_eta(100.0, current=0, total=10, now=110.0) == ""
    assert _compute_eta(100.0, current=10, total=10, now=110.0) == ""
    assert _compute_eta(100.0, current=5, total=10, now=150.0) == "50秒"


def test_usage_ledger_persistence_and_routes(tmp_path, monkeypatch):
    p = tmp_path / "usage_state.json"
    led = UsageLedger(p)
    led.add_token({"category": "chapter", "input_tokens": 1_000_000, "output_tokens": 100_000, "model": "MiniMax-M3.1-Flash"})
    led.add_token({"category": "recheck", "input_tokens": 10, "output_tokens": 0, "model": "自定义未定价模型"})
    led.add_chapter({"chapter": 1, "status": "done", "elapsed": 12.5, "retries": 1, "failed_tokens": 100, "cached_tokens": 500, "model": "MiniMax-M3.1-Flash"})
    led.save_sync()

    led2 = UsageLedger(p)
    snap = led2.snapshot()
    assert snap["chapters_done"] == 1
    assert snap["per_model"]["MiniMax-M3.1-Flash"] == {"in": 1_000_000, "out": 100_000, "cached": 500}

    monkeypatch.setattr("backend.services.usage_ledger.get_usage_ledger", lambda: led2)
    monkeypatch.setenv(_ENV_VAR, "secret-token")
    app = FastAPI()
    app.include_router(routes_analysis.router)
    client = TestClient(app)

    resp = client.get("/api/analysis/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["unpriced"] == ["自定义未定价模型"]

    assert client.post("/api/analysis/usage/clear").status_code == 403
    assert client.post("/api/analysis/usage/clear", headers={"X-Session-Token": "secret-token"}).status_code == 200
    assert led2.snapshot()["per_model"] == {}
