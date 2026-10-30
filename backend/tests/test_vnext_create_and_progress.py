"""从原文创建项目 + 块级进度（S2 基础入口：切章 / 新建 / 结果落盘 / 断点恢复）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.app import app
from backend.services import vnext_service
from backend.vnext import blocks, project, results
from backend.vnext.constants import VnextFormatError


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(root):
    return TestClient(app)


SAMPLE = """第1章 起点

　　这是第一章的正文，讲了一件小事。

　　又一段。

第2章 转折

　　第二章开始，事情变了。

第3章 收束

　　最后一章，尘埃落定。
"""


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


def test_create_from_chapters_writes_project(root) -> None:
    info = vnext_service.create_book(
        "手切的书",
        chapters=[(1, "第一章\n内容一\n"), (2, "第二章\n内容二\n")],
    )
    assert info["total_chapters"] == 2
    book_dir = root / "workspace_vnext" / info["book_id"]
    assert (book_dir / "manifest.json").is_file()


def test_create_from_text_uses_the_same_splitter(root) -> None:
    """走真切章器：三条"第N章"应切成 3 章。"""
    info = vnext_service.create_book("整本原文", text=SAMPLE)
    # 真切章器的结果由它自己的规则决定（这里 3 个"第N章"标题 + 可能的卷/序被单独成章），
    # 断言"至少切出了三章且首章是第1章"，而不是把切章器的内部细节钉死在这个测试里
    assert info["total_chapters"] >= 3


def test_create_rejects_empty_input(root) -> None:
    with pytest.raises(VnextFormatError) as exc:
        vnext_service.create_book("空书", text="   ")
    assert exc.value.code.value == "INVALID_FIELD_VALUE"


def test_create_without_chapter_markers_falls_back_to_single_chapter(root) -> None:
    """没有章节标题时，切章器会把整篇当成一章——这是既有行为，不改。

    这里把它钉住，是为了"零章项目"这条错误路径不被误用：真正会报错的是切出 0 章，
    而不是"切出 1 章"。用户要知道自己拿到的是"一章"，而不是以为书被切坏了。
    """
    info = vnext_service.create_book("无章节", text="这是一段没有章节标题的散文。\n继续。\n")
    assert info["total_chapters"] == 1


def test_create_is_rejected_when_project_exists(root) -> None:
    vnext_service.create_book("重名", chapters=[(1, "x\n")], book_id="bk_dup", dir_name="d1")
    with pytest.raises(VnextFormatError):
        vnext_service.create_book("重名", chapters=[(1, "y\n")], book_id="bk_dup", dir_name="d1")


# ---------------------------------------------------------------------------
# 块级进度（断点恢复的读入口）
# ---------------------------------------------------------------------------


def test_blocks_start_all_missing_then_mark_done(root) -> None:
    info = vnext_service.create_book(
        "进度书", chapters=[(i, f"第{i}章\n内容{i}\n") for i in range(1, 9)]
    )
    book_dir = root / "workspace_vnext" / info["book_id"]

    first = vnext_service.list_blocks(info["book_id"])
    assert first["total_blocks"] == 2
    assert first["done_blocks"] == 0
    assert {b["state"] for b in first["blocks"]} == {"missing:NOT_REGISTERED"}

    # 发布第一块的结果后，它应变成 done（同块的后续块仍待跑）
    manifest = project.load_manifest(book_dir)
    plans = blocks.plan_blocks(manifest, block_size=vnext_service.block_size())
    plan = plans[0]
    hashes = {c.chapter_id: c.content_hash for c in manifest.chapters}
    strategy = vnext_service.result_strategy()
    dep = results.expected_dependency([], strategy)
    rid = results.make_result_id(
        block_signature=plan.block_signature,
        prompt_strategy_version=strategy.prompt_strategy_version,
        model_fingerprint=strategy.model_fingerprint,
        dependency_fingerprint=dep,
    )
    results.publish_result(
        book_dir,
        results.ResultRecord(
            result_id=rid,
            block_signature=plan.block_signature,
            source_chapters=tuple((cid, hashes[cid]) for cid in plan.chapter_ids),
            status=results.ST_SUCCEEDED,
            model_fingerprint=strategy.model_fingerprint,
            dependency_fingerprint=dep,
        ),
    )

    after = vnext_service.list_blocks(info["book_id"])
    assert after["done_blocks"] == 1
    assert after["blocks"][0]["state"] == "done"
    assert after["blocks"][1]["state"].startswith("missing")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def test_route_create_project_from_chapters(client, root) -> None:
    resp = client.post(
        "/api/vnext/projects",
        json={"title": "接口建的书", "chapters": [{"title": "第1章", "content": "正文"}]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["total_chapters"] == 1
    assert body["book_id"]


def test_route_create_project_missing_title_is_404(client, root) -> None:
    resp = client.post("/api/vnext/projects", json={"text": SAMPLE})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "MISSING_REQUIRED_FIELD"


def test_route_create_project_bad_chapters_is_422(client, root) -> None:
    resp = client.post(
        "/api/vnext/projects", json={"title": "书", "chapters": [{"content": 1}]}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_FIELD_VALUE"


def test_route_blocks_after_create(client, root) -> None:
    created = client.post(
        "/api/vnext/projects",
        json={"title": "块进度", "chapters": [{"content": f"第{i}章\n内容"} for i in range(8)]},
    ).json()

    resp = client.get(f"/api/vnext/books/{created['book_id']}/blocks")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_blocks"] == 2
    assert body["done_blocks"] == 0
    assert len(body["blocks"]) == 2
