"""API Session Token 鉴权、ProgressHub 广播与 WebSocket 实时通道测试。"""
from __future__ import annotations

import asyncio
import json

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from backend.api import ws as ws_module
from backend.api.auth import _ENV_VAR
from backend.app import app
from backend.progress_hub import ProgressHub


def test_session_token_auth(monkeypatch):
    client = TestClient(app)
    monkeypatch.delenv(_ENV_VAR, raising=False)
    assert client.get("/api/analysis/logs").status_code != 403

    token = "test-token-32-chars-aaaaaaaaaaaa"
    monkeypatch.setenv(_ENV_VAR, token)
    assert client.get("/api/analysis/logs").status_code == 403
    assert client.get("/api/analysis/logs", headers={"X-Session-Token": "wrong"}).status_code == 403
    assert client.get("/api/analysis/logs", headers={"X-Session-Token": token}).status_code == 200


async def test_progress_hub_pubsub():
    hub = ProgressHub()
    q1 = await hub.subscribe()
    q2 = await hub.subscribe()
    msg = {"type": "log", "payload": {"level": "info", "text": "hello"}}
    await hub.publish(msg)
    assert await asyncio.wait_for(q1.get(), timeout=1.0) == msg
    assert await asyncio.wait_for(q2.get(), timeout=1.0) == msg
    await hub.unsubscribe(q1)
    await hub.unsubscribe(q2)
    assert len(hub._queues) == 0


async def test_throttled_broadcaster_relaxes_interval_and_keeps_latest_frame():
    from backend.progress_hub import ThrottledBroadcaster

    hub = ProgressHub()
    q = await hub.subscribe()
    try:
        broadcaster = ThrottledBroadcaster(hub)
        assert broadcaster.MIN_INTERVAL == 0.25
        # 测试中缩短间隔，验证 1) 首帧立即发布；2) 窗口内连续覆盖只留最后一帧
        broadcaster.MIN_INTERVAL = 0.05

        await broadcaster.emit("k1", {"seq": 1})
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        assert first == {"seq": 1}

        await broadcaster.emit("k1", {"seq": 2})
        await broadcaster.emit("k1", {"seq": 3})
        latest = await asyncio.wait_for(q.get(), timeout=1.0)
        assert latest == {"seq": 3}
        assert q.empty()
    finally:
        await hub.unsubscribe(q)


async def test_ws_progress_heartbeat_and_disconnect(monkeypatch):
    hub = ProgressHub()
    monkeypatch.setattr(ws_module, "get_hub", lambda: hub)
    monkeypatch.setattr(ws_module, "_HEARTBEAT_INTERVAL", 0.01)

    class DisconnectingSocket:
        def __init__(self):
            self.headers = {}
            self.sent = []
            self.disconnected = asyncio.Event()

        async def accept(self):
            pass

        async def send_json(self, message):
            if self.sent:
                self.disconnected.set()
                raise WebSocketDisconnect()
            self.sent.append(message)

        async def close(self, code=1000):
            pass

    sock = DisconnectingSocket()
    task = asyncio.create_task(ws_module.ws_progress(sock))
    await asyncio.wait_for(sock.disconnected.wait(), timeout=2.0)
    await asyncio.wait_for(task, timeout=2.0)
    assert sock.sent == [{"type": "ping"}]
    assert len(hub._queues) == 0


def test_book_style_profile_endpoint(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from backend.api import routes_books

    book_dir = tmp_path / "书名"
    out_dir = book_dir / "output"
    ckpt_dir = out_dir / "final_summary_checkpoint"
    ckpt_dir.mkdir(parents=True)

    monkeypatch.setattr("backend.services.book_service.get_output_dir", lambda bid: out_dir if bid == "b1" else None)
    test_app = FastAPI()
    test_app.include_router(routes_books.router)
    client = TestClient(test_app)

    # 未知书目返回 404
    assert client.get("/api/books/missing/style_profile").status_code == 404

    # 尚未运行最终总结时返回 HTTP 200 exists=False
    empty_resp = client.get("/api/books/b1/style_profile")
    assert empty_resp.status_code == 200
    assert empty_resp.json() == {
        "book_id": "b1",
        "exists": False,
        "statistical": None,
        "semantic": None,
        "sample_chapters": [],
    }

    # Stage 3 落盘 style_profile.json 后正常解包 profile 字段
    (ckpt_dir / "style_profile.json").write_text(
        json.dumps({
            "profile": {
                "statistical": {"avg_sentence_length": 22.5, "literary_ratio": 1.85},
                "semantic": {"narrative_rhythm": "快节奏强推进"},
                "sample_chapters": [1, 10],
            },
            "chapters_fp": "fp",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    ok_resp = client.get("/api/books/b1/style_profile")
    assert ok_resp.status_code == 200
    body = ok_resp.json()
    assert body["exists"] is True
    assert body["statistical"]["literary_ratio"] == 1.85
    assert body["semantic"]["narrative_rhythm"] == "快节奏强推进"
    assert body["sample_chapters"] == [1, 10]


def test_settings_patch_and_put_guards(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from backend.api import routes_settings
    from backend.config.settings import ConfigManager

    cfg_path = tmp_path / "config.json"
    mgr = ConfigManager(cfg_path)
    mgr.save()

    fake_analysis = SimpleNamespace(is_running=True, config_manager=mgr)
    fake_summary = SimpleNamespace(is_running=False)
    monkeypatch.setattr(routes_settings, "get_service", lambda: fake_analysis)
    monkeypatch.setattr("backend.services.summary_service.get_summary_service", lambda: fake_summary)

    test_app = FastAPI()
    test_app.include_router(routes_settings.router)
    client = TestClient(test_app)

    # 分析运行中：允许纯总结参数 PATCH，拒绝非总结 PATCH 与全量 PUT
    ok_resp = client.patch("/api/settings", json={
        "api": {"summary_model": "sum-m", "summary_thinking_mode": {}},
        "analysis": {"summary_batch_size": 45, "summary_concurrency": 3},
    })
    assert ok_resp.status_code == 200
    assert ok_resp.json()["analysis"]["summary_batch_size"] == 45
    assert client.patch("/api/settings", json={"analysis": {"concurrency": 8}}).status_code == 409
    assert client.put("/api/settings", json=mgr.config.to_dict()).status_code == 409

    # 总结运行中：拦截一切 PATCH
    fake_analysis.is_running = False
    fake_summary.is_running = True
    assert client.patch("/api/settings", json={"analysis": {"summary_batch_size": 50}}).status_code == 409

    # 非法参数返回结构化 FastAPI 列表 422 detail
    fake_summary.is_running = False
    bad = client.patch("/api/settings", json={
        "api": {"base_url": "ftp://invalid", "timeout": "bad"},
        "analysis": {"concurrency": 0},
    })
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert isinstance(detail, list)
    locs = [tuple(item["loc"]) for item in detail]
    assert ("body", "api", "base_url") in locs
    assert ("body", "api", "timeout") in locs
    assert ("body", "analysis", "concurrency") in locs


def test_workspace_restore_archive_blocks(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from backend.api import routes_workspace
    from backend.config.settings import AppConfig
    from backend.services import book_service, workspace_service
    from backend.services.queue_manager import QueueItem, QueueManager

    ws = tmp_path / "workspace"
    archive_root = ws / "分析结果"
    arch = archive_root / "《太白》-20261121-0100"
    (arch / "blocks").mkdir(parents=True)
    (arch / "blocks" / "0001.txt").write_text("第一章 正文", encoding="utf-8")
    (arch / "blocks" / "0002.txt").write_text("第二章 正文", encoding="utf-8")
    (arch / "blocks" / "metadata.json").write_text('{"title": "太白"}', encoding="utf-8")
    (arch / "output").mkdir(parents=True)
    (arch / "output" / "chapter_1_result.json").write_text('{"old": true}', encoding="utf-8")
    (arch / "knowledge.json").write_text('{"old": true}', encoding="utf-8")
    (arch / "style.md").write_text("旧风格", encoding="utf-8")

    empty_arch = archive_root / "无切分"
    (empty_arch / "output").mkdir(parents=True)
    (empty_arch / "output" / "foo.json").write_text("{}", encoding="utf-8")

    qm = QueueManager()
    # 预先放入一个指向不存在路径的失效旧条目，验证还原时会清理
    qm.add_item(QueueItem(
        name="失效旧书",
        blocks_dir=ws / "不存在" / "blocks",
        workspace_dir=ws / "不存在",
        status="done",
        total_chapters=5,
        completed_chapters=5,
    ))
    saved_calls: list[bool] = []
    fake_svc = SimpleNamespace(
        is_running=False,
        workspace_path=ws,
        queue=qm,
        config_manager=SimpleNamespace(config=AppConfig()),
        save_queue=lambda: saved_calls.append(True),
    )

    monkeypatch.setattr(workspace_service, "get_service", lambda: fake_svc)
    monkeypatch.setattr(routes_workspace, "get_service", lambda: fake_svc)
    monkeypatch.setattr(book_service, "get_service", lambda: fake_svc)
    book_service.refresh_books()

    test_app = FastAPI()
    test_app.include_router(routes_workspace.router)
    client = TestClient(test_app)

    # 1. list_archives 返回 blocks 元数据
    archives_resp = client.get("/api/workspace/archives")
    assert archives_resp.status_code == 200
    by_name = {a["name"]: a for a in archives_resp.json()["archives"]}
    assert by_name["《太白》-20261121-0100"]["has_blocks"] is True
    assert by_name["《太白》-20261121-0100"]["blocks_count"] == 2
    assert by_name["无切分"]["has_blocks"] is False
    assert by_name["无切分"]["blocks_count"] == 0

    # 2. 默认还原：剥离时间戳并只复制 blocks/，自动入队为 pending 且清掉失效项
    r1 = client.post("/api/workspace/restore_blocks", json={
        "archive_name": "《太白》-20261121-0100",
        "target_name": "",
    })
    assert r1.status_code == 200
    assert r1.json() == {"ok": True, "novel_name": "《太白》", "blocks_count": 2}

    restored_dir = ws / "《太白》"
    assert (restored_dir / "blocks" / "0001.txt").read_text(encoding="utf-8") == "第一章 正文"
    assert (restored_dir / "blocks" / "metadata.json").exists()
    assert not (restored_dir / "output").exists()
    assert not (restored_dir / "knowledge.json").exists()
    assert not (restored_dir / "style.md").exists()
    assert (arch / "output" / "chapter_1_result.json").exists()  # 原归档未受损

    assert len(qm.items) == 1
    assert qm.items[0].name == "《太白》"
    assert qm.items[0].status == "pending"
    assert qm.items[0].completed_chapters == 0
    assert qm.items[0].total_chapters == 2
    assert saved_calls

    # 3. 再次默认还原：自动递增为《太白》-2；显式同名冲突 409；自定义名对比 200
    r2 = client.post("/api/workspace/restore_blocks", json={
        "archive_name": "《太白》-20261121-0100",
        "target_name": "",
    })
    assert r2.status_code == 200 and r2.json()["novel_name"] == "《太白》-2"

    conflict_r = client.post("/api/workspace/restore_blocks", json={
        "archive_name": "《太白》-20261121-0100",
        "target_name": "《太白》",
    })
    assert conflict_r.status_code == 409

    custom_r = client.post("/api/workspace/restore_blocks", json={
        "archive_name": "《太白》-20261121-0100",
        "target_name": "《太白》-dsv4",
    })
    assert custom_r.status_code == 200 and (ws / "《太白》-dsv4" / "blocks" / "0002.txt").exists()

    # 4. 校验拦截：无 blocks 归档 400、非法越界目标名 400、分析运行中 409
    assert client.post("/api/workspace/restore_blocks", json={"archive_name": "无切分"}).status_code == 400
    assert client.post("/api/workspace/restore_blocks", json={
        "archive_name": "《太白》-20261121-0100",
        "target_name": "../escape",
    }).status_code == 400

    fake_svc.is_running = True
    assert client.post("/api/workspace/restore_blocks", json={
        "archive_name": "《太白》-20261121-0100",
        "target_name": "other",
    }).status_code == 409


def test_book_recheck_verdicts_endpoint(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from backend.api import routes_books

    out_dir = tmp_path / "书名" / "output"
    out_dir.mkdir(parents=True)
    monkeypatch.setattr("backend.services.book_service.get_output_dir", lambda bid: out_dir if bid == "b1" else None)

    test_app = FastAPI()
    test_app.include_router(routes_books.router)
    client = TestClient(test_app)

    assert client.get("/api/books/missing/recheck_verdicts").status_code == 404

    empty_resp = client.get("/api/books/b1/recheck_verdicts")
    assert empty_resp.status_code == 200
    assert empty_resp.json()["exists"] is False
    assert empty_resp.json()["verdicts"] == []

    (out_dir / "foreshadow_ledger.json").write_text(json.dumps({
        "items": [{
            "id": "fs_01",
            "description": "青铜钥匙的来历",
            "question": "钥匙究竟能打开什么？",
            "first_seen_chapter": 3,
            "importance": "高",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    (out_dir / "recheck_verdicts.json").write_text(json.dumps({
        "status": "COMPLETED",
        "status_reason": "",
        "model": "test-model",
        "generated_at": "2026-11-23T12:00:00Z",
        "candidates_total": 1,
        "coverage": {"checked": 1, "candidates_total": 1, "v3_tiers": {"RESOLVED_EXPLICIT": 1}},
        "audit_routes": {"fs_01": {"engine": "v3", "hops_used": 2}},
        "applied": {"fs_01": "已回收"},
        "resolved_chapters": {"fs_01": 18},
        "verdicts": [{
            "id": "fs_01",
            "verdict": "RESOLVED_EXPLICIT",
            "tier": "RESOLVED_EXPLICIT",
            "answer": "第18章打开了密室石门。",
            "quote": "青铜钥匙咔哒一声拧开石门。",
            "reason": "原文明确对应",
            "evidence_chain": [{"unit_id": 18, "offset": 120, "quote": "青铜钥匙咔哒一声拧开石门。", "role": "payoff"}],
            "gate_demoted": False,
            "repaired": False,
        }],
    }, ensure_ascii=False), encoding="utf-8")

    ok_resp = client.get("/api/books/b1/recheck_verdicts")
    assert ok_resp.status_code == 200
    data = ok_resp.json()
    assert data["exists"] is True
    assert data["engine"] == "v3"
    assert len(data["verdicts"]) == 1
    v = data["verdicts"][0]
    assert v["id"] == "fs_01"
    assert v["description"] == "青铜钥匙的来历"
    assert v["applied"] == "已回收"
    assert v["resolved_chapter"] == "18"
    assert v["hops_used"] == 2


def test_spa_static_files_fallback(tmp_path):
    from fastapi import FastAPI
    from backend.app import SPAStaticFiles

    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>SPA_INDEX</body></html>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")

    test_app = FastAPI()

    @test_app.get("/api/health")
    async def health():
        return {"status": "ok"}

    test_app.mount("/", SPAStaticFiles(directory=str(dist), html=True), name="frontend")
    client = TestClient(test_app)

    for route in ("/", "/characters", "/world", "/timeline", "/summary"):
        resp = client.get(route)
        assert resp.status_code == 200
        assert "SPA_INDEX" in resp.text

    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/missing-chunk.js").status_code == 404
    assert client.get("/api/missing").status_code == 404
    assert client.get("/favicon.ico").status_code == 404
