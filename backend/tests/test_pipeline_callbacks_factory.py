"""Step1-1B：生产回调工厂 `make_pipeline_callbacks` 的真实回归（第三批清理，2026-10-14）

背景：原 `backend/tests/test_queue_service.py::test_on_progress_block_start_publishes`
用的是测试内**复制的** `on_progress_like` 闭包，即使禁用生产 `make_pipeline_callbacks`
仍然全绿——属无效覆盖。本文件直接调用生产工厂，覆盖：

- 结构化 `range` 优先于 `message`（前端车道统一显示 chN-chM）；
- DONE / FAILED / SKIPPED 三个终态都转发 `block_done` 并记每章统计（失败同样计入成本）；
- 只有 DONE 才发 discovery；SKIPPED 的 `ok` 为 False；
- `on_token_stats` 累加 token 统计并转发 `token_stats`（不二次累计）；
- 回调名即事件契约：不改字段、不改顺序、不改广播等待方式。

全部使用隔离 `ProgressHub` 实例（不用进程级 singleton），避免跨用例订阅污染。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.progress_hub import ProgressHub  # noqa: E402
from backend.services.analysis_stats import AnalysisStats  # noqa: E402
from backend.services.pipeline_events import make_pipeline_callbacks  # noqa: E402
from backend.services.queue_manager import QueueItem  # noqa: E402


@pytest.fixture
def harness():
    hub = ProgressHub()
    item = QueueItem(
        name="测试书", blocks_dir=Path("/x/blocks"), workspace_dir=Path("/x")
    )
    item.start_time = time.time()
    stats = AnalysisStats()
    seen: set = set()
    on_progress, on_token_stats = make_pipeline_callbacks(
        hub=hub, item=item, stats=stats, seen_characters=seen
    )
    return hub, item, stats, seen, on_progress, on_token_stats


async def _drain(queue, n: int):
    out = []
    for _ in range(n):
        out.append(await asyncio.wait_for(queue.get(), timeout=2.0))
    return out


# ---------------------------------------------------------------------------
# start：range 优先于 message
# ---------------------------------------------------------------------------

async def test_start_prefers_structured_range_over_message(harness):
    """有 range 时用 range；无 range 时回退 message"""
    hub, _item, _stats, _seen, on_progress, _ = harness
    received = await hub.subscribe()
    try:
        await on_progress({
            "status": "start", "chapter": 9, "progress": 3, "total": 50,
            "message": "开始分析第9-10章...", "range": "ch9-ch10",
        })
        # 生产顺序（已核实）：log → progress → block_start（带 message 时先发 log）
        msgs = await _drain(received, 3)
        assert [m["type"] for m in msgs] == ["log", "progress", "block_start"]
        assert msgs[-1]["payload"]["range"] == "ch9-ch10"

        await on_progress({
            "status": "start", "chapter": 11, "progress": 4, "total": 50,
            "message": "开始分析第11-12章...",
        })
        msgs = await _drain(received, 3)
        assert msgs[-1]["payload"]["range"] == "开始分析第11-12章..."
    finally:
        await hub.unsubscribe(received)


async def test_start_emits_full_block_start_payload(harness):
    """block_start payload 字段与顺序：chapter/range/progress/total/ts"""
    hub, item, _stats, _seen, on_progress, _ = harness
    received = await hub.subscribe()
    try:
        await on_progress({
            "status": "start", "chapter": 5, "progress": 5, "total": 100,
            "range": "ch5-ch8",
        })
        # 无 message → 不发 log，顺序为 progress → block_start
        msgs = await _drain(received, 2)
        assert [m["type"] for m in msgs] == ["progress", "block_start"]
        payload = msgs[-1]["payload"]
        assert list(payload.keys()) == ["chapter", "range", "progress", "total", "ts"]
        assert payload["chapter"] == 5
        assert payload["range"] == "ch5-ch8"
        assert payload["progress"] == 5
        assert payload["total"] == 100
        assert isinstance(payload["ts"], float)
        # progress 事件同步写入 item.completed_chapters
        assert item.completed_chapters == 5
    finally:
        await hub.unsubscribe(received)


# ---------------------------------------------------------------------------
# DONE / FAILED / SKIPPED 终态转发
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status, ok_expected, discovery_expected",
    [
        ("done", True, True),
        ("failed", False, False),
        ("skipped", False, False),
    ],
)
async def test_terminal_statuses_forward_block_done_and_stats(
    harness, status, ok_expected, discovery_expected
):
    """三个终态都发 block_done 且记每章统计；只有 done 发 discovery"""
    hub, _item, stats, _seen, on_progress, _ = harness
    received = await hub.subscribe()
    try:
        payload_in = {
            "status": status, "chapter": 12, "range": "ch12-ch13",
            "elapsed": 2.0, "tokens": (30, 40),
            "input_tokens": 30, "output_tokens": 40,
            "retries": 2, "failed_tokens": 100,
        }
        if status == "done":
            payload_in["result"] = {"core_events": [{"characters": "林动、萧炎"}]}

        await on_progress(payload_in)

        # 终态事件顺序（已核实）：block_done [+ discovery]（本例无 message 故无 log）
        expected = 2 if discovery_expected else 1
        msgs = await _drain(received, expected)
        done = msgs[0]
        assert done["type"] == "block_done"
        assert done["payload"]["ok"] is ok_expected
        assert done["payload"]["chapter"] == 12
        assert done["payload"]["range"] == "ch12-ch13"
        assert done["payload"]["elapsed"] == 2.0
        assert done["payload"]["tokens"] == (30, 40)
        assert ("discovery" in [m["type"] for m in msgs]) is discovery_expected

        # 统计：三个终态都记；失败成本不归零
        assert len(stats.chapter_stats) == 1
        assert stats.chapter_stats[0]["status"] == status
        assert stats.chapter_stats[0]["chapter"] == 12
        assert stats.total_retries == 2
        assert stats.total_failed_tokens == 100
    finally:
        await hub.unsubscribe(received)


async def test_failed_log_uses_error_level(harness):
    """FAILED 的 log 用 error 级别；DONE/SKIPPED 用 info"""
    hub, _item, _stats, _seen, on_progress, _ = harness
    received = await hub.subscribe()
    try:
        await on_progress({"status": "failed", "chapter": 1, "message": "炸了"})
        # 顺序：log(error) → block_done
        log_msg = await asyncio.wait_for(received.get(), timeout=2.0)
        assert log_msg["type"] == "log"
        assert log_msg["payload"]["level"] == "error"
        done_msg = await asyncio.wait_for(received.get(), timeout=2.0)
        assert done_msg["type"] == "block_done"
        assert done_msg["payload"]["ok"] is False

        await on_progress({"status": "skipped", "chapter": 2, "message": "被拦截"})
        log_msg = await asyncio.wait_for(received.get(), timeout=2.0)
        assert log_msg["payload"]["level"] == "info"
        done_msg = await asyncio.wait_for(received.get(), timeout=2.0)
        assert done_msg["payload"]["ok"] is False
    finally:
        await hub.unsubscribe(received)


async def test_discovery_dedups_across_blocks(harness):
    """已见人物不重复进入 discovery（seen_characters 求差）"""
    hub, _item, _stats, seen, on_progress, _ = harness
    received = await hub.subscribe()
    result = {"core_events": [{"characters": "林动、萧炎"}]}
    try:
        await on_progress({
            "status": "done", "chapter": 1, "range": "ch1",
            "input_tokens": 1, "output_tokens": 1, "result": result,
        })
        msgs = await _drain(received, 2)  # block_done → discovery
        assert msgs[-1]["payload"]["characters"] == ["林动", "萧炎"]
        assert seen == {"林动", "萧炎"}

        await on_progress({
            "status": "done", "chapter": 2, "range": "ch2",
            "input_tokens": 1, "output_tokens": 1, "result": result,
        })
        msgs = await _drain(received, 2)
        assert msgs[-1]["payload"]["characters"] == []
    finally:
        await hub.unsubscribe(received)


# ---------------------------------------------------------------------------
# on_token_stats：累加 + 转发，不二次累计
# ---------------------------------------------------------------------------

async def test_token_stats_accumulates_and_forwards(harness):
    """on_token_stats 累加到 stats 并原样转发 token_stats 事件"""
    hub, _item, stats, _seen, _, on_token_stats = harness
    received = await hub.subscribe()
    try:
        await on_token_stats({"category": "chapter", "input_tokens": 100, "output_tokens": 50})
        await on_token_stats({"category": "chapter", "input_tokens": 20, "output_tokens": 10})
        await on_token_stats({"category": "rolling", "input_tokens": 5, "output_tokens": 1})

        msgs = await _drain(received, 3)
        assert all(m["type"] == "token_stats" for m in msgs)
        assert msgs[0]["payload"]["input_tokens"] == 100  # 原样转发，不累计后再发
        assert msgs[1]["payload"]["input_tokens"] == 20

        assert stats.token_stats["chapter"] == {"input_tokens": 120, "output_tokens": 60}
        assert stats.token_stats["rolling"] == {"input_tokens": 5, "output_tokens": 1}
    finally:
        await hub.unsubscribe(received)


async def test_token_stats_unknown_category_default(harness):
    """无 category 时归入 unknown（既有行为）"""
    hub, _item, stats, _seen, _, on_token_stats = harness
    received = await hub.subscribe()
    try:
        await on_token_stats({"input_tokens": 7, "output_tokens": 3})
        await asyncio.wait_for(received.get(), timeout=2.0)
        assert stats.token_stats["unknown"] == {"input_tokens": 7, "output_tokens": 3}
    finally:
        await hub.unsubscribe(received)


# ---------------------------------------------------------------------------
# 反向验证：禁用生产工厂时，本文件必须失败（证明测的是真实工厂）
# ---------------------------------------------------------------------------

async def test_callbacks_come_from_production_factory(harness):
    """回调必须携带工厂的行为特征：无 range 时回退 message 且带 ts 字段

    复制闭包（旧测试那种）不含 ts / 不转发 progress / 不记统计，
    因此本断言可作为"测的是生产工厂"的哨兵。
    """
    hub, item, stats, _seen, on_progress, _ = harness
    received = await hub.subscribe()
    try:
        await on_progress({"status": "start", "chapter": 1, "progress": 1, "total": 2})
        # 哨兵：无 message 时不发 log；block_start 带 ts；start 不记统计
        msgs = await _drain(received, 2)
        assert [m["type"] for m in msgs] == ["progress", "block_start"]
        assert "ts" in msgs[-1]["payload"]
        assert item.completed_chapters == 1
        assert stats.chapter_stats == []  # start 不记统计
    finally:
        await hub.unsubscribe(received)


async def test_progress_eta_uses_item_start_time(harness):
    """ETA 由 item.start_time 推算（工厂捕获 item 引用，非复制闭包的固定值）"""
    hub, item, _stats, _seen, on_progress, _ = harness
    received = await hub.subscribe()
    try:
        # start_time 为 0 → _compute_eta 返回空串（既有契约）
        item.start_time = 0.0
        await on_progress({"status": "start", "chapter": 1, "progress": 1, "total": 10})
        msgs = await _drain(received, 2)
        assert msgs[0]["payload"]["eta"] == ""

        # 有 start_time 且进度过半 → ETA 非空
        item.start_time = time.time() - 10.0
        await on_progress({"status": "start", "chapter": 2, "progress": 5, "total": 10})
        msgs = await _drain(received, 2)
        assert msgs[0]["payload"]["eta"] != ""
    finally:
        await hub.unsubscribe(received)
