"""Step1-1C：以真实 `ws_progress` 为被测入口的运行时契约（第三批清理，2026-10-14）

不用源码子串断言或手工拼的 {"type": "ping"} 字典当测试——那样即使生产不再发心跳、
或在发出前改变形状，测试仍全绿。本文件直接调用 `backend/api/ws.py::ws_progress`：

- FakeWebSocket 记录 `send_json` 实际收到的对象；
- `_HEARTBEAT_INTERVAL` 压到极小值以确定性触发超时分支（**不等待真实 25 秒**；
  等待用事件驱动，不做定时轮询）；
- 断言实际发送的序列化结果严格为 `{"type": "ping"}` 且不含 payload；
- 断连用例必须**真的走到 WebSocketDisconnect 分支**并让协程自然返回（有可观察标记），
  再用有限 timeout 等待；`cancel()` 只在等待超时/失败时做资源清理，不作为断连替代；
- 普通队列广播断言原样透传。

禁用心跳发送或给心跳加 payload 时，本文件必须失败（已用变异检查验证，见报告）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.api import ws as ws_module  # noqa: E402
from backend.progress_hub import ProgressHub  # noqa: E402
from backend.ws_events import WSType  # noqa: E402


class FakeWebSocket:
    """记录 send_json 的最小 WebSocket 替身（只实现 ws_progress 用到的部分）"""

    def __init__(self, headers=None):
        self.headers = headers or {}
        self.sent: list = []
        self.accepted = False
        self.closed: list[int] = []
        # 每发一条消息就置位，供调用方事件驱动地等待（避免 sleep 轮询）
        self.sent_event = asyncio.Event()

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        self.sent.append(message)
        self.sent_event.set()

    async def close(self, code=1000):
        self.closed.append(code)


class DisconnectingWebSocket(FakeWebSocket):
    """第一条消息正常记录，第二条直接抛 WebSocketDisconnect

    断连必须真的发生：用 `disconnected` 事件做可观察标记，测试据此断言
    “断连分支确实被执行”，而不是靠外部 cancel 后碰巧走到 finally。
    """

    def __init__(self, headers=None):
        super().__init__(headers)
        self.disconnected = asyncio.Event()

    async def send_json(self, message):
        if self.sent:  # 已有记录 → 这条就是第二次发送，断连
            self.disconnected.set()
            raise WebSocketDisconnect()
        await super().send_json(message)


@pytest.fixture
def ws_env(monkeypatch):
    """隔离 hub；interval 可按需覆盖（广播用例需要心跳晚于业务消息）"""

    def _build(interval: float = 0.01):
        hub = ProgressHub()
        monkeypatch.setattr(ws_module, "get_hub", lambda: hub)
        monkeypatch.setattr(ws_module, "_HEARTBEAT_INTERVAL", interval)
        return hub

    return _build


async def _wait_for(coro_factory, event: asyncio.Event, timeout: float = 2.0):
    """启动真实 ws_progress，事件驱动地等到 event 置位；返回 (task, socket)。

    只在超时/异常时 cancel 做清理（不吞非预期异常）。
    """
    socket = coro_factory()
    task = asyncio.create_task(ws_module.ws_progress(socket))
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except BaseException:
        # 仅失败路径做资源清理：取消后抑制预期 CancelledError
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise
    return task, socket


# ---------------------------------------------------------------------------
# 心跳的运行时契约
# ---------------------------------------------------------------------------

async def test_idle_connection_sends_ping_without_payload(ws_env):
    """队列空闲 → 真实 ws_progress 发出严格 {"type": "ping"}，无 payload"""
    build = ws_env
    build()
    socket = FakeWebSocket()

    task, _ = await _wait_for(lambda: socket, socket.sent_event)
    task.cancel()  # 协程是无限循环，拿到心跳后主动结束
    with pytest.raises(asyncio.CancelledError):
        await task

    first = socket.sent[0]
    assert set(first.keys()) == {"type"}, f"心跳形状改变: {first}"
    assert first["type"] == "ping"
    assert "payload" not in first
    assert json.loads(json.dumps(first)) == {"type": "ping"}


async def test_ping_value_matches_protocol_constant(ws_env):
    """心跳的 type 与 WSType.PING 同源"""
    build = ws_env
    build()
    socket = FakeWebSocket()

    task, _ = await _wait_for(lambda: socket, socket.sent_event)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.sent[0]["type"] == WSType.PING
    assert str(socket.sent[0]["type"]) == "ping"


async def test_heartbeat_does_not_alter_queue_broadcast(ws_env):
    """普通队列广播原样透传：心跳不改动业务消息"""
    build = ws_env
    # 心跳间隔远大于业务投递延迟，确保第一条是业务广播
    hub = build(interval=5.0)
    socket = FakeWebSocket()
    business = {"type": "progress", "payload": {"current": 1, "total": 2, "eta": ""}}

    subscribed = asyncio.Event()

    async def feed():
        # 等 ws_progress 完成 subscribe 后再投递，业务消息才能进它的队列
        while not hub._queues and not subscribed.is_set():
            await asyncio.sleep(0)
        subscribed.set()
        await hub.publish(business)

    feeder = asyncio.create_task(feed())
    try:
        task, _ = await _wait_for(lambda: socket, socket.sent_event, timeout=5.0)
    finally:
        feeder.cancel()
        try:
            await feeder
        except asyncio.CancelledError:
            pass
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.sent[0] == business, f"业务广播被改动或未透传: {socket.sent}"
    assert set(socket.sent[0].keys()) == {"type", "payload"}


# ---------------------------------------------------------------------------
# 断连：必须真的走到 WebSocketDisconnect 分支，并等协程自然返回
# ---------------------------------------------------------------------------

async def test_disconnect_unsubscribes(ws_env):
    """断连真的发生后，ws_progress 自然返回并 unsubscribe（订阅者不泄漏）"""
    build = ws_env
    hub = build()
    socket = DisconnectingWebSocket()

    # 等断连真的发生（可观察标记），而不是等第一条消息
    task, sock = await _wait_for(lambda: socket, socket.disconnected, timeout=5.0)

    # 让协程自然走 except WebSocketDisconnect → finally，有限 timeout 内必须结束
    await asyncio.wait_for(task, timeout=5.0)

    assert sock.disconnected.is_set(), "断连分支未被执行"
    assert len(sock.sent) == 1, f"断连前只应记录一条消息: {sock.sent}"
    assert sock.sent[0] == {"type": WSType.PING}
    assert hub._queues == set(), f"disconnect 后仍残留订阅者: {hub._queues}"


async def test_cancel_path_also_unsubscribes(ws_env):
    """对照组：外部 cancel（非断连）路径同样执行 finally 的 unsubscribe

    与上一条用例区分两条路径，避免只验证 cancel 后的 finally。
    """
    build = ws_env
    hub = build()
    socket = FakeWebSocket()

    task, _ = await _wait_for(lambda: socket, socket.sent_event)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.sent, "未收到任何消息"
    assert hub._queues == set(), f"cancel 后仍残留订阅者: {hub._queues}"
