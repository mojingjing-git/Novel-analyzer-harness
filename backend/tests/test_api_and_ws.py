"""API Session Token 鉴权、ProgressHub 广播与 WebSocket 实时通道测试。"""
from __future__ import annotations

import asyncio

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
