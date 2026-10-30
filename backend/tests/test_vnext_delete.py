"""B2：删除项目。顺序与失败姿态是这组用例的重点。

删书不可撤销，所以三件事必须成立：
1. 二次确认要原样输入 book_id；
2. **先 rename 再清理**——删到一半失败只留下 `.trash-*` 垃圾，不会留下"没有 manifest 的目录"
   （那种目录会被判成 LEGACY_DIR，界面上就再也删不掉了）；
3. 队列里的悬挂条目要一起摘掉，但"没入队"不能导致删除失败。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_vnext
from backend.services import vnext_service


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_ROOT", str(tmp_path))
    app = FastAPI()
    app.include_router(routes_vnext.router)
    return TestClient(app)


def _make(tmp_path, book_id="bk_del0000001"):
    from backend.vnext import project

    return project.create_project(
        tmp_path / "workspace_vnext", "书", [(1, "第一章\n正文。\n")], book_id=book_id
    )


def test_delete_removes_project_and_reports(client, tmp_path) -> None:
    _make(tmp_path)
    response = client.request("DELETE", "/api/vnext/books/bk_del0000001?confirm=bk_del0000001")
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert client.get("/api/vnext/books").json()["books"] == []


def test_delete_requires_matching_confirm(client, tmp_path) -> None:
    _make(tmp_path)
    response = client.request("DELETE", "/api/vnext/books/bk_del0000001?confirm=nope")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_FIELD_VALUE"
    # 没删掉
    assert len(client.get("/api/vnext/books").json()["books"]) == 1


def test_delete_missing_book_is_not_found(client) -> None:
    response = client.request("DELETE", "/api/vnext/books/bk_nope?confirm=bk_nope")
    assert response.status_code == 404


def test_delete_also_removes_queue_entry(client, tmp_path) -> None:
    _make(tmp_path)
    client.post("/api/vnext/queue", json={"book_id": "bk_del0000001", "name": "书"})
    assert len(client.get("/api/vnext/queue").json()["entries"]) == 1

    client.request("DELETE", "/api/vnext/books/bk_del0000001?confirm=bk_del0000001")
    assert client.get("/api/vnext/queue").json()["entries"] == []


def test_delete_works_when_not_queued(client, tmp_path) -> None:
    """没入队也要能删——否则用户得先入队再删除，荒谬。"""
    _make(tmp_path)
    response = client.request("DELETE", "/api/vnext/books/bk_del0000001?confirm=bk_del0000001")
    assert response.status_code == 200


def test_trash_dir_is_not_listed_as_rejected_dir(client, tmp_path, monkeypatch) -> None:
    """删到一半（rmtree 失败）只应留下 `.trash-*`，它必须被扫描跳过，不能冒出来当 LEGACY_DIR。"""
    import shutil as _shutil

    _make(tmp_path)
    monkeypatch.setattr(_shutil, "rmtree", lambda *a, **k: None)
    client.request("DELETE", "/api/vnext/books/bk_del0000001?confirm=bk_del0000001")

    payload = client.get("/api/vnext/books").json()
    assert payload["books"] == []
    assert payload["rejected"] == []
    leftovers = [p.name for p in (tmp_path / "workspace_vnext").iterdir() if p.name.startswith(".trash-")]
    assert len(leftovers) == 1


def test_service_rejects_empty_book_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_ROOT", str(tmp_path))
    with pytest.raises(Exception) as exc:
        vnext_service.delete_book("   ", confirm="")
    assert "book_id" in str(exc.value)
