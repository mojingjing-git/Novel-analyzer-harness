"""vnext 服务门面与路由（PLAN v2 §4 S2 接线：发现/打开/队列）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.app import app
from backend.services import vnext_service
from backend.vnext import project, queue


@pytest.fixture
def root(tmp_path, monkeypatch):
    """隔离的 NOVEL_ROOT：所有 vnext 路径都从这里派生。"""
    monkeypatch.setenv("NOVEL_ROOT", str(tmp_path))
    return tmp_path


def _make_book(root: Path, *, book_id: str = "bk_svc00000001", n: int = 4) -> Path:
    chapters = [(i, f"第{i}章\n正文{i}\n") for i in range(1, n + 1)]
    return project.create_project(root / "workspace_vnext", "测试书", chapters, book_id=book_id)


@pytest.fixture
def client(root):
    return TestClient(app)


# ---------------------------------------------------------------------------
# 门面
# ---------------------------------------------------------------------------


def test_list_books_separates_accepted_and_rejected(root) -> None:
    """旧目录不是"没读到"，而是"明确拒绝"——这条是 S2 门槛的直接断言。"""
    _make_book(root)
    legacy = root / "workspace_vnext" / "《旧书》"
    (legacy / "blocks").mkdir(parents=True)
    (legacy / "blocks" / "0001.txt").write_text("旧正文", encoding="utf-8")

    payload = vnext_service.list_books()
    assert [b["book_id"] for b in payload["books"]] == ["bk_svc00000001"]
    assert payload["books"][0]["chapter_count"] == 4

    rejected = {r["name"]: r for r in payload["rejected"]}
    assert "《旧书》" in rejected
    assert rejected["《旧书》"]["reason"] == "LEGACY_DIR"
    assert rejected["《旧书》"]["message"]


def test_open_book_reports_block_progress(root) -> None:
    _make_book(root, n=4)
    info = vnext_service.open_book("bk_svc00000001")
    assert info["chapter_count"] == 4
    assert info["block_count"] == 1          # 默认 block_size=4 → 1 块
    assert info["done_blocks"] == 0
    assert info["missing_blocks"] == 1


def test_open_book_unknown_id_is_clear_error(root) -> None:
    with pytest.raises(Exception) as exc:
        vnext_service.open_book("bk_nope000000")
    assert "bk_nope000000" in str(exc.value)


def test_block_size_read_from_config_or_default(root) -> None:
    assert vnext_service.block_size() == 4  # 没有配置文件 → 默认 4

    cfg = root / "config_vnext.json"
    cfg.write_text(
        json.dumps(
            {
                "format_id": "novel-analyzer-vnext",
                "schema_version": 1,
                "analysis": {"block_size": 2},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert vnext_service.block_size() == 2


def test_broken_block_size_is_an_error_not_a_default(root) -> None:
    """配置写坏了必须报错——否则用户改了配置却不知道没生效。"""
    cfg = root / "config_vnext.json"
    cfg.write_text(
        json.dumps(
            {
                "format_id": "novel-analyzer-vnext",
                "schema_version": 1,
                "analysis": {"block_size": "三"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(Exception) as exc:
        vnext_service.block_size()
    assert "block_size" in str(exc.value)


def test_restore_queue_only_resets_running(root) -> None:
    path = queue.queue_state_path(root)
    queue.save_queue_state(
        path,
        queue.QueueState(
            entries=[
                queue.QueueEntry(book_id="b1", name="甲", status=queue.STATUS_RUNNING),
                queue.QueueEntry(book_id="b2", name="乙", status=queue.STATUS_DONE),
            ]
        ),
    )
    state = vnext_service.restore_queue_after_restart()
    assert state.find("b1").status == queue.STATUS_PENDING
    assert state.find("b2").status == queue.STATUS_DONE


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def test_route_list_books(client, root) -> None:
    _make_book(root)
    resp = client.get("/api/vnext/books")
    assert resp.status_code == 200
    body = resp.json()
    assert body["books"][0]["book_id"] == "bk_svc00000001"
    assert body["rejected"] == []


def test_route_get_book_ok(client, root) -> None:
    _make_book(root)
    resp = client.get("/api/vnext/books/bk_svc00000001")
    assert resp.status_code == 200
    assert resp.json()["title"] == "测试书"


def test_route_unknown_book_is_404_with_code(client, root) -> None:
    resp = client.get("/api/vnext/books/bk_missing0001")
    assert resp.status_code == 404
    # 「找不到」用 NOT_FOUND，不用「请求缺字段」——两者对用户意义不同
    assert resp.json()["detail"]["code"] == "NOT_FOUND"


def test_route_legacy_queue_is_409_with_code(client, root) -> None:
    """旧队列文件走门禁 → 409，且带 code，前端据此显示"不属于本版本"。"""
    (root / "queue_state_vnext.json").write_text(
        json.dumps({"items": [{"name": "旧书"}]}, ensure_ascii=False), encoding="utf-8"
    )
    resp = client.get("/api/vnext/queue")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "UNSUPPORTED_ARTIFACT_FORMAT"


def test_route_queue_empty_when_absent(client, root) -> None:
    resp = client.get("/api/vnext/queue")
    assert resp.status_code == 200
    assert resp.json()["entries"] == []
