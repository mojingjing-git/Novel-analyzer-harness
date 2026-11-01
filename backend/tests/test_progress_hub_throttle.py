"""
ProgressHub ThrottledBroadcaster 测试

覆盖：
- 同 channel key 在 150ms 内合并（多次 emit 只发一次）
- 不同 channel key 独立计时
- delay 任务正确清理
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.progress_hub import ProgressHub, ThrottledBroadcaster


class TestThrottledBroadcaster:
    async def test_first_emit_immediate(self):
        """首次 emit 立即发布"""
        hub = ProgressHub()
        await hub.subscribe()
        tb = ThrottledBroadcaster(hub)

        await tb.emit("test_key", {"type": "test", "n": 1})

        # 取出队列里的消息
        q = list(hub._queues)[0]
        msg = q.get_nowait()
        assert msg["type"] == "test"
        assert msg["n"] == 1

    async def test_same_key_within_150ms_collapsed(self):
        """同 key 在 150ms 内多次 emit 只发最后一次（自动合并）"""
        hub = ProgressHub()
        await hub.subscribe()
        tb = ThrottledBroadcaster(hub)

        # 第一次立即发
        await tb.emit("test_key", {"type": "test", "n": 1})
        # 第二次被节流，延迟发送
        await tb.emit("test_key", {"type": "test", "n": 2})

        # 等待延迟发送完成
        await asyncio.sleep(0.25)

        q = list(hub._queues)[0]
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())

        # 应该有 2 条：第 1 次立即 + 第 2 次延迟
        assert len(msgs) == 2
        assert msgs[0]["n"] == 1
        assert msgs[1]["n"] == 2

    async def test_different_keys_independent(self):
        """不同 channel key 独立计时"""
        hub = ProgressHub()
        await hub.subscribe()
        tb = ThrottledBroadcaster(hub)

        await tb.emit("key_a", {"type": "test", "key": "a", "n": 1})
        await tb.emit("key_b", {"type": "test", "key": "b", "n": 2})

        # 两个 key 都立即发（不互相影响）
        await asyncio.sleep(0.05)
        q = list(hub._queues)[0]
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())

        assert len(msgs) == 2
        keys = {m["key"] for m in msgs}
        assert keys == {"a", "b"}

    async def test_after_150ms_next_emit_immediate(self):
        """150ms 后再次 emit 立即发"""
        hub = ProgressHub()
        await hub.subscribe()
        tb = ThrottledBroadcaster(hub)

        await tb.emit("test_key", {"n": 1})
        # 等待超过 150ms
        await asyncio.sleep(0.20)
        await tb.emit("test_key", {"n": 2})

        q = list(hub._queues)[0]
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())

        # 两次都立即发（间隔 > 150ms）
        assert len(msgs) == 2
        assert msgs[0]["n"] == 1
        assert msgs[1]["n"] == 2

    async def test_pending_task_cleaned_up(self):
        """delay 任务完成后从 _pending_tasks 清理"""
        hub = ProgressHub()
        await hub.subscribe()
        tb = ThrottledBroadcaster(hub)

        await tb.emit("key_a", {"n": 1})
        # 第二次延迟
        await tb.emit("key_a", {"n": 2})

        # 立即检查：_pending_tasks 应有 1 个未完成任务
        assert "key_a" in tb._pending_tasks

        await asyncio.sleep(0.20)

        # 完成后：_pending_tasks 应被清理
        assert "key_a" not in tb._pending_tasks

    async def test_hub_broadcast_token_delta(self):
        """ProgressHub.broadcast_token_delta 端到端：channel key 三元组 + 节流"""
        hub = ProgressHub()
        await hub.subscribe()

        # 模拟多 block 并发
        await hub.broadcast_token_delta(
            context="analysis", session_id="book_001", unit_idx=1,
            delta={"output_tokens": 100, "rate_tokens_per_sec": 50},
        )
        await hub.broadcast_token_delta(
            context="analysis", session_id="book_001", unit_idx=2,  # 不同 block
            delta={"output_tokens": 200, "rate_tokens_per_sec": 60},
        )
        await hub.broadcast_token_delta(
            context="summary_phase_1", session_id="book_001", unit_idx=1,  # 不同 context
            delta={"output_tokens": 300, "rate_tokens_per_sec": 70},
        )

        await asyncio.sleep(0.05)
        q = list(hub._queues)[0]
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())

        # 应该有 3 条 token_delta
        assert len(msgs) == 3
        assert all(m["type"] == "token_delta" for m in msgs)

        # 验证不同 unit_idx 互不覆盖
        contexts = [m["payload"]["context"] for m in msgs]
        unit_idxs = [m["payload"]["unit_idx"] for m in msgs]
        assert "analysis" in contexts
        assert "summary_phase_1" in contexts
        assert 1 in unit_idxs
        assert 2 in unit_idxs

        # 验证 token_delta 消息结构
        first = msgs[0]
        assert "context" in first["payload"]
        assert "session_id" in first["payload"]
        assert "unit_idx" in first["payload"]
        assert "delta" in first["payload"]
        assert "timestamp" in first["payload"]
