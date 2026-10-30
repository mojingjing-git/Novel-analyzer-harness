"""vnext 队列写端点（S10 操作界面后端）。

守两件事：**重复入队不能写进第二条同 book_id 记录**（那是唯一能毁掉状态文件的操作），
以及"入队不等于开跑"必须由响应明确表达。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.services import vnext_service
from backend.vnext import queue


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _make_project(tmp_path, monkeypatch, *, book_id="bk_q00000001", title="队列书"):
    """建一个真项目，并把服务层的数据根指到临时目录。"""
    monkeypatch.setenv("NOVEL_ROOT", str(tmp_path))
    chapters = [(i, f"第{i}章\n正文{i}\n") for i in range(1, 4)]
    from backend.vnext import project

    book_dir = project.create_project(
        tmp_path / "workspace_vnext", title, chapters, book_id=book_id, dir_name=book_id
    )
    return book_id, book_dir


def test_enqueue_creates_pending_entry(tmp_path, monkeypatch, client):
    book_id, _ = _make_project(tmp_path, monkeypatch)
    resp = client.post("/api/vnext/queue", json={"book_id": book_id})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["analysis_started"] is False       # 不许暗示"已在跑"
    assert payload["created"] is True
    entry = queue.load_queue_state(vnext_service.queue_path()).find(book_id)
    assert entry is not None
    assert entry.status == queue.STATUS_PENDING
    assert entry.name == "队列书"
    assert entry.total_chapters == 3
    assert entry.error_message == ""


def test_enqueue_twice_is_idempotent(tmp_path, monkeypatch, client):
    """重复入队绝不能 append 第二条：from_dict 遇重复 book_id 会抛错，队列从此打不开。"""
    book_id, _ = _make_project(tmp_path, monkeypatch)
    first = client.post("/api/vnext/queue", json={"book_id": book_id}).json()
    second = client.post("/api/vnext/queue", json={"book_id": book_id}).json()
    assert first["created"] is True
    assert second["created"] is False
    state = queue.load_queue_state(vnext_service.queue_path())
    assert [e.book_id for e in state.entries] == [book_id]
    # 状态文件仍然可读（这是真正的验收点）
    assert client.get("/api/vnext/queue").status_code == 200


def test_enqueue_unknown_project_is_rejected(tmp_path, monkeypatch, client):
    monkeypatch.setenv("NOVEL_ROOT", str(tmp_path))
    resp = client.post("/api/vnext/queue", json={"book_id": "bk_missing00001"})
    assert resp.status_code in (404, 422)
    assert resp.json()["detail"]["code"] in {
        "NOT_FOUND", "INVALID_FIELD_VALUE", "UNSUPPORTED_ARTIFACT_FORMAT"
    }


def test_enqueue_without_book_id_is_422(tmp_path, monkeypatch, client):
    monkeypatch.setenv("NOVEL_ROOT", str(tmp_path))
    resp = client.post("/api/vnext/queue", json={})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_FIELD_VALUE"


def test_dequeue_removes_entry(tmp_path, monkeypatch, client):
    book_id, _ = _make_project(tmp_path, monkeypatch)
    client.post("/api/vnext/queue", json={"book_id": book_id})
    resp = client.delete(f"/api/vnext/queue/{book_id}")
    assert resp.status_code == 200
    assert resp.json()["removed"] == book_id
    assert queue.load_queue_state(vnext_service.queue_path()).find(book_id) is None


def test_dequeue_missing_entry_is_404(tmp_path, monkeypatch, client):
    monkeypatch.setenv("NOVEL_ROOT", str(tmp_path))
    resp = client.delete("/api/vnext/queue/bk_nothere00001")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "NOT_FOUND"


def test_create_project_missing_title_is_422_not_404(tmp_path, monkeypatch, client):
    """缺 title 是请求不合法（422），不是"项目没找到"（404）。"""
    monkeypatch.setenv("NOVEL_ROOT", str(tmp_path))
    resp = client.post("/api/vnext/projects", json={"text": "第1章\n正文"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_FIELD_VALUE"
