"""Step2：流式 Token 进度回调工厂的等价性回归（第三批清理，2026-10-14）

被测对象 `backend/core/stream_progress.py::make_stream_progress_callback`，以及它在
三条**生产路径**上的实际接线：

- 章节分析：`backend/core/analyzer.py`（context=analysis，unit_idx=chapter_number）
- 总结各阶段：`backend/services/final_summary.py::FinalSummaryRunner._make_on_progress`
  （薄适配器，计时仍在**拿到并发槽之后**创建）
- 风格提取：`backend/core/style_analyzer.py::call_llm_semantic`（context=summary_phase_3，unit_idx=0）

覆盖点（对应计划 §5）：

- 事件字段/顺序/await：`broadcast_token_delta` 被 await，delta 键顺序与取值一致；
- 过滤：只有 content / usage 广播，reasoning / error 不新增广播；
- 累计值不二次累加：output_tokens 直接取 `estimated_total_tokens or 0`；
- 零/负 elapsed 走 `max(elapsed, 0.1)`；
- 回调隔离：两个回调互不共享计时器；
- 未知/空 session_id 回退 "unknown"；
- 三条真实入口：含总结并发槽等待位置与失败重试不重置计时。

全部使用受控时钟（monkeypatch `time.monotonic`）、mock LLM 与隔离 Hub，
遵循 Step0 的导入前隔离；不触真实 LLM、不读写真实文件。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.core.error_types import LLMResult, UsageStatus  # noqa: E402
from backend.core.llm_stream import StreamChunk  # noqa: E402
from backend.core.stream_progress import (  # noqa: E402
    BROADCAST_CHUNK_TYPES,
    make_stream_progress_callback,
)
from backend.models.knowledge import KnowledgeBase  # noqa: E402
from backend.progress_hub import ProgressHub  # noqa: E402


def chunk(ctype: str, tokens: int = 0) -> StreamChunk:
    return StreamChunk(type=ctype, text="x", estimated_total_tokens=tokens)


class RecordingHub(ProgressHub):
    """记录 broadcast_token_delta 调用（保留 ProgressHub 的真实节流入口不变）"""

    def __init__(self):
        super().__init__()
        self.events: list[dict] = []

    async def broadcast_token_delta(self, context, session_id, unit_idx, delta):
        self.events.append(
            {
                "context": context,
                "session_id": session_id,
                "unit_idx": unit_idx,
                "delta": delta,
            }
        )


@pytest.fixture
def clock(monkeypatch):
    """可控 monotonic 时钟：clock.value 起始值，clock.advance(s) 推进"""

    class Clock:
        def __init__(self):
            self.value = 1000.0

        def now(self):
            return self.value

        def advance(self, seconds: float = 0.5):
            self.value += seconds

        def rewind(self, seconds: float):
            self.value -= seconds

    c = Clock()
    monkeypatch.setattr(time, "monotonic", c.now)
    return c


# ---------------------------------------------------------------------------
# 工厂：字段 / 顺序 / 过滤 / 回退 / 隔离
# ---------------------------------------------------------------------------

async def test_delta_fields_and_order(clock):
    """delta 三个键的顺序与取值：output_tokens / rate_tokens_per_sec / elapsed_sec"""
    hub = RecordingHub()
    cb = make_stream_progress_callback(hub, "analysis", "book", 7)
    clock.advance(0.5)

    await cb(chunk("content", 40))

    event = hub.events[0]
    assert list(event["delta"].keys()) == [
        "output_tokens",
        "rate_tokens_per_sec",
        "elapsed_sec",
    ]
    assert event["context"] == "analysis"
    assert event["session_id"] == "book"
    assert event["unit_idx"] == 7
    assert event["delta"]["output_tokens"] == 40
    assert event["delta"]["rate_tokens_per_sec"] == 40 / 0.5
    assert event["delta"]["elapsed_sec"] == 0.5


async def test_broadcast_is_awaited(clock):
    """broadcast_token_delta 必须被 await（不能是 fire-and-forget）"""
    awaited = {"n": 0}
    hub = RecordingHub()

    orig = hub.broadcast_token_delta

    async def counting(*args, **kwargs):
        await asyncio.sleep(0)  # 若调用方没 await，这一步不会执行
        awaited["n"] += 1
        return await orig(*args, **kwargs)

    hub.broadcast_token_delta = counting

    cb = make_stream_progress_callback(hub, "analysis", "book", 0)
    await cb(chunk("content", 10))

    assert awaited["n"] == 1, "回调未 await broadcast_token_delta"


async def test_only_content_and_usage_broadcast(clock):
    """只有 content / usage 广播；reasoning / error 不新增广播"""
    hub = RecordingHub()
    cb = make_stream_progress_callback(hub, "analysis", "book", 0)

    for ctype in ("content", "usage", "reasoning", "error"):
        await cb(chunk(ctype, 100))

    assert len(hub.events) == 2
    assert BROADCAST_CHUNK_TYPES == ("content", "usage")


async def test_usage_only_chunk(clock):
    """usage-only：单独一个 usage chunk 也广播一次"""
    hub = RecordingHub()
    cb = make_stream_progress_callback(hub, "summary_phase_1", "book", 2)
    clock.advance(1.0)

    await cb(chunk("usage", 120))

    assert len(hub.events) == 1
    assert hub.events[0]["delta"]["output_tokens"] == 120
    assert hub.events[0]["delta"]["elapsed_sec"] == 1.0


async def test_no_double_accumulation(clock):
    """累计值由 LLMClient 维护：这里直接透传，不二次累加"""
    hub = RecordingHub()
    cb = make_stream_progress_callback(hub, "analysis", "book", 0)

    clock.advance(0.5)
    await cb(chunk("content", 40))
    clock.advance(0.5)
    await cb(chunk("content", 40))  # 上游给的仍是累计值 40（非 80）
    clock.advance(0.5)
    await cb(chunk("usage", 100))

    assert [e["delta"]["output_tokens"] for e in hub.events] == [40, 40, 100]


async def test_zero_elapsed_uses_floor(clock):
    """零 elapsed 走 max(elapsed, 0.1)，不产生除零"""
    hub = RecordingHub()
    cb = make_stream_progress_callback(hub, "analysis", "book", 0)

    await cb(chunk("content", 50))  # elapsed = 0

    assert hub.events[0]["delta"]["elapsed_sec"] == 0.0
    assert hub.events[0]["delta"]["rate_tokens_per_sec"] == 50 / 0.1


async def test_negative_elapsed_uses_floor(clock):
    """负 elapsed（时钟回拨）同样走 0.1 下限"""
    hub = RecordingHub()
    cb = make_stream_progress_callback(hub, "analysis", "book", 0)
    clock.rewind(5.0)  # 时钟回拨 → elapsed < 0

    await cb(chunk("content", 20))

    assert hub.events[0]["delta"]["elapsed_sec"] < 0
    assert hub.events[0]["delta"]["rate_tokens_per_sec"] == 20 / 0.1


async def test_unknown_session_id_fallbacks(clock):
    """session_id 为空串/None 时回退 "unknown"；unit_idx 默认 0"""
    hub = RecordingHub()
    cb = make_stream_progress_callback(hub, "summary_phase_3", "")
    await cb(chunk("content", 1))

    cb2 = make_stream_progress_callback(hub, "summary_phase_3", None)
    await cb2(chunk("content", 1))

    assert [e["session_id"] for e in hub.events] == ["unknown", "unknown"]
    assert [e["unit_idx"] for e in hub.events] == [0, 0]


async def test_callbacks_are_isolated(clock):
    """两个回调各自独立计时：先创建的回调不因后创建而重置"""
    hub = RecordingHub()
    cb_a = make_stream_progress_callback(hub, "analysis", "a", 1)
    clock.advance(2.0)
    cb_b = make_stream_progress_callback(hub, "analysis", "b", 2)
    clock.advance(1.0)

    await cb_a(chunk("content", 10))  # elapsed = 3.0
    await cb_b(chunk("content", 10))  # elapsed = 1.0

    assert hub.events[0]["delta"]["elapsed_sec"] == 3.0
    assert hub.events[1]["delta"]["elapsed_sec"] == 1.0


async def test_retry_does_not_reset_timer(clock):
    """重试过程中计时不重置、不前移：同一回调多次调用持续走同一 started_at"""
    hub = RecordingHub()
    cb = make_stream_progress_callback(hub, "analysis", "book", 0)

    clock.advance(1.0)
    await cb(chunk("content", 10))
    # 模拟重试后再次调用：elapsed 继续增长，不回到 0
    clock.advance(1.0)
    await cb(chunk("content", 20))

    assert hub.events[0]["delta"]["elapsed_sec"] == 1.0
    assert hub.events[1]["delta"]["elapsed_sec"] == 2.0


# ---------------------------------------------------------------------------
# 生产入口（真实方法，非源码字符串）
#
# 说明：以下用例**真实调用** `analyze_chapter` / `call_llm_semantic` /
# `_call_llm_*`，用 mock LLM 捕获 `chat_auto` 收到的 `on_progress` 并驱动
# chunks；不直接调工厂来冒充"入口验证"。
# ---------------------------------------------------------------------------


class FakeLLMClient:
    """捕获 chat_auto 入参的 mock LLM；可按需驱动 on_progress 与制造失败。"""

    def __init__(self, clock, outcome=None):
        self.clock = clock
        self.calls: list[dict] = []
        self.outcome = outcome or [("ok", '{"result": 1}')]  # 每次 attempt 的结果
        self._attempt = 0

    async def chat_auto(self, messages, max_tokens=None, validate_response=None,
                        retry_messages_builder=None, on_progress=None):
        self.calls.append({
            "messages": messages,
            "max_tokens": max_tokens,
            "on_progress": on_progress,
        })
        self._attempt += 1
        status, payload = self.outcome[min(self._attempt - 1, len(self.outcome) - 1)]
        if on_progress is not None:
            await on_progress(chunk("content", 40))
            self.clock.advance(0.5)
            await on_progress(chunk("usage", 100))
            await on_progress(chunk("reasoning", 999))  # 必须被过滤
        if status == "ok":
            return LLMResult(True, payload, "", (10, 20), UsageStatus.KNOWN,
                             call_stats={"attempts": self._attempt})
        # U4：chat_auto 返回 LLMResult（失败也照样消耗了本次调用的 token）
        return LLMResult(False, "", payload, (10, 20), UsageStatus.KNOWN,
                         call_stats={"attempts": self._attempt, "failed_tokens": 5})


def _analysis_json() -> str:
    """满足 validate_parsed_analysis 的最小合法响应"""
    return json.dumps({
        "core_events": [],
        "cross_block": {"summary": "ok"},
        "long_context_insights": {},
    }, ensure_ascii=False)


async def test_analyzer_entry_real_call(clock, monkeypatch, tmp_path):
    """真实调用 NovelAnalyzer.analyze_chapter：经 chat_auto 接到的 on_progress 驱动"""
    import backend.core.analyzer as analyzer_mod

    hub = RecordingHub()
    monkeypatch.setattr(analyzer_mod, "get_hub", lambda: hub)

    analyzer = object.__new__(analyzer_mod.NovelAnalyzer)
    analyzer._is_stopped = False
    analyzer.llm_client = FakeLLMClient(clock, [("ok", _analysis_json())])
    analyzer.prompt_builder = type("PB", (), {
        "build_messages": lambda self, c, n, kb: [{"role": "user", "content": c}]
    })()
    analyzer.config = type("Cfg", (), {"api": type("Api", (), {"max_tokens": 1000})()})()

    result, tokens, retry_info = await analyzer.analyze_chapter(
        chapter_number=7,
        chapter_content="第一章内容",
        knowledge_base=KnowledgeBase(),
        block_size=1,
        book_id="测试书",
    )

    assert result is not None, "analyze_chapter 应成功返回"
    assert len(analyzer.llm_client.calls) == 1
    assert analyzer.llm_client.calls[0]["on_progress"] is not None, (
        "analyzer 未把 on_progress 传给 chat_auto"
    )
    # 真实入口观察到的广播：analysis / 测试书 / chapter_number=7
    assert [
        (e["context"], e["session_id"], e["unit_idx"], e["delta"]["output_tokens"])
        for e in hub.events
    ] == [("analysis", "测试书", 7, 40), ("analysis", "测试书", 7, 100)]
    # reasoning 被过滤
    assert len(hub.events) == 2


async def test_analyzer_entry_unknown_session_id(clock, monkeypatch, tmp_path):
    """book_id 为空 → 真实入口的 session_id 回退 unknown"""
    import backend.core.analyzer as analyzer_mod

    hub = RecordingHub()
    monkeypatch.setattr(analyzer_mod, "get_hub", lambda: hub)

    analyzer = object.__new__(analyzer_mod.NovelAnalyzer)
    analyzer._is_stopped = False
    analyzer.llm_client = FakeLLMClient(clock, [("ok", _analysis_json())])
    analyzer.prompt_builder = type("PB", (), {
        "build_messages": lambda self, c, n, kb: [{"role": "user", "content": c}]
    })()
    analyzer.config = type("Cfg", (), {"api": type("Api", (), {"max_tokens": 1000})()})()

    await analyzer.analyze_chapter(
        chapter_number=2, chapter_content="x", knowledge_base=KnowledgeBase(),
        block_size=1, book_id="",
    )

    assert {e["session_id"] for e in hub.events} == {"unknown"}
    assert {e["unit_idx"] for e in hub.events} == {2}


async def test_style_entry_real_call(clock, monkeypatch, tmp_path):
    """真实调用 style_analyzer.call_llm_semantic：context=summary_phase_3、unit_idx=0"""
    from backend.core import style_analyzer as style_mod

    hub = RecordingHub()
    import backend.progress_hub as ph
    monkeypatch.setattr(ph, "get_hub", lambda: hub)

    semantic_json = json.dumps(
        {f: "x" for f in style_mod.SEMANTIC_FIELDS}, ensure_ascii=False
    )
    fake_client = FakeLLMClient(clock, [("ok", semantic_json)])
    monkeypatch.setattr(style_mod, "LLMClient", lambda *a, **k: fake_client)

    result = await style_mod.call_llm_semantic(
        prompt="p",
        api_config={"base_url": "https://example.invalid", "api_key": "",
                    "model": "m", "provider": "auto", "timeout": 1},
        token_sink=None,
        book_name="风格书",
    )

    assert result is not None, "call_llm_semantic 应成功返回"
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["on_progress"] is not None, (
        "style 入口未把 on_progress 传给 chat_auto"
    )
    assert [
        (e["context"], e["session_id"], e["unit_idx"], e["delta"]["output_tokens"])
        for e in hub.events
    ] == [("summary_phase_3", "风格书", 0, 40), ("summary_phase_3", "风格书", 0, 100)]


# ---------------------------------------------------------------------------
# 总结四个 _call_llm_*：真实调用 + 槽位阻塞期间不得创建计时
# ---------------------------------------------------------------------------

SUMMARY_ENTRIES = [
    ("_call_llm_summary", "summary_phase_1", 0),
    ("_call_llm_final", "summary_phase_4", 0),
    ("_call_llm_reconciliation", "summary_phase_1", 5),
    # 注意：全书复检实际用的是 summary_phase_2（既有行为，本步不改）
    ("_call_llm_global_recheck", "summary_phase_2", 2),
]


def _make_runner(clock, hub, monkeypatch, fs_mod):
    """构造一个只依赖 mock 的最小 FinalSummaryRunner"""
    runner = object.__new__(fs_mod.FinalSummaryRunner)
    runner.book_id = "总结书"
    runner._inflight_count = 0
    runner._peak_inflight = 0
    runner.config = type(
        "Cfg", (), {"api": type("Api", (), {"max_tokens": 1000})()}
    )()
    # 用 Event 模拟槽位占用：未 set 时 acquire 会阻塞
    gate = asyncio.Event()

    async def acquire():
        await gate.wait()

    def release():
        pass

    runner._acquire_llm_slot = acquire
    runner._release_llm_slot = release
    runner._record_tokens = lambda *a, **k: None
    return runner, gate


@pytest.mark.parametrize("method,expected_context,batch_idx", SUMMARY_ENTRIES)
async def test_summary_entries_real_call(
    clock, monkeypatch, method, expected_context, batch_idx
):
    """四个总结入口：真实调用；槽位阻塞期间不创建计时，释放后 elapsed 不含排队"""
    import backend.progress_hub as ph
    from backend.services import final_summary as fs_mod

    hub = RecordingHub()
    runner, gate = _make_runner(clock, hub, monkeypatch, fs_mod)
    fake = FakeLLMClient(clock, [("ok", "{}")])
    runner._llm = fake
    monkeypatch.setattr(ph, "get_hub", lambda: hub)

    task = asyncio.create_task(
        getattr(runner, method)("prompt", batch_idx=batch_idx)
    )
    # 等待期间：工厂尚未创建（无任何广播，因为还没进 chat_auto）
    for _ in range(50):
        if hub.events or task.done():
            break
        await asyncio.sleep(0)
    clock.advance(30.0)  # 模拟长时间排队
    assert hub.events == [], "槽位等待期间不应创建计时/广播"

    gate.set()  # 释放槽位
    await asyncio.wait_for(task, timeout=5.0)

    assert len(fake.calls) == 1
    assert fake.calls[0]["on_progress"] is not None, (
        f"{method} 未把 on_progress 传给 chat_auto"
    )
    contexts = {e["context"] for e in hub.events}
    assert contexts == {expected_context}, f"{method} context 不符: {contexts}"
    assert {e["unit_idx"] for e in hub.events} == {batch_idx}
    assert {e["session_id"] for e in hub.events} == {"总结书"}
    # 计时从槽位释放后才开始：elapsed 不含排队的 30 秒
    elapsed_values = [e["delta"]["elapsed_sec"] for e in hub.events]
    assert max(elapsed_values) <= 5.0, f"elapsed 含排队时间: {elapsed_values}"


async def test_summary_entries_no_on_progress_when_absent(
    clock, monkeypatch
):
    """反向：_make_on_progress 若返回 None（模拟未接线），入口不得广播"""
    import backend.progress_hub as ph
    from backend.services import final_summary as fs_mod

    hub = RecordingHub()
    runner, gate = _make_runner(clock, hub, monkeypatch, fs_mod)
    fake = FakeLLMClient(clock, [("ok", "{}")])
    runner._llm = fake
    monkeypatch.setattr(ph, "get_hub", lambda: hub)
    monkeypatch.setattr(runner, "_make_on_progress", lambda *a, **k: None)

    gate.set()
    await asyncio.wait_for(runner._call_llm_summary("p", batch_idx=0), timeout=5.0)

    assert fake.calls[0]["on_progress"] is None
    assert hub.events == [], "未接线时不应有广播"


# ---------------------------------------------------------------------------
# Real LLMClient retry chain (chat_auto -> chat_stream_with_retry).
#
# Only the low-level stream source ``LLMClient.chat_stream`` is patched; the
# retry loop, ``_run_stream_attempt`` and the ``on_progress`` fan-out stay
# real. The fake stream yields an error chunk on attempt 1 (triggering the
# temperature-retry path with a real backoff sleep) and content + usage on
# attempt 2, so the same progress callback is driven across both attempts.
# ---------------------------------------------------------------------------


class _NullFailureLogger:
    """Failure logger stub that keeps records in memory only."""

    def __init__(self):
        self.failures: list[dict] = []

    def record_failure(self, **kwargs):
        self.failures.append(kwargs)

    def get_summary(self):
        return {"count": len(self.failures)}


async def test_retry_chain_reuses_callback_and_keeps_timer(clock, monkeypatch):
    """Retry must reuse one callback and keep one monotonic timer."""
    import backend.core.llm_client as llm_client_mod
    from backend.config.settings import APIConfig

    hub = RecordingHub()
    attempts: list[int] = []

    async def fake_chat_stream(self, messages, temperature=0.1, max_tokens=None):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            yield StreamChunk(type="error", error="429 Too Many Requests")
            return
        yield StreamChunk(type="content", text="hello world")
        yield StreamChunk(
            type="usage",
            usage_prompt_tokens=10,
            usage_completion_tokens=20,
            is_final=True,
        )

    monkeypatch.setattr(llm_client_mod.LLMClient, "chat_stream", fake_chat_stream)

    cfg = APIConfig(
        base_url="https://example.invalid",
        api_key="",
        model="m",
        max_tokens=1000,
        temperature=0.5,
        temperature_step=0.1,
        temperature_max_retries=2,
        backoff_max_retries=1,
        thinking_mode={},
        provider="openai",
        streaming_enabled=True,
    )
    client = llm_client_mod.LLMClient(cfg)

    cb = make_stream_progress_callback(
        hub=hub, context="analysis", session_id="retry-book", unit_idx=3
    )

    async def fake_sleep(seconds):
        clock.advance(seconds)  # keep the backoff wait on the controlled clock
        await asyncio.sleep(0)

    monkeypatch.setattr(client, "_sleep", fake_sleep)
    monkeypatch.setattr(
        llm_client_mod, "_get_failure_logger", lambda: _NullFailureLogger()
    )

    res = await client.chat_auto(
        [{"role": "user", "content": "hi"}], on_progress=cb
    )

    assert res.success is True, f"second attempt should succeed: {res.error}"
    assert res.call_stats.get("attempts") == 2, f"expected 2 attempts: {res.call_stats}"
    assert len(attempts) == 2, "chat_stream should have been called twice"

    assert {e["context"] for e in hub.events} == {"analysis"}
    assert {e["session_id"] for e in hub.events} == {"retry-book"}
    assert {e["unit_idx"] for e in hub.events} == {3}

    elapsed = [e["delta"]["elapsed_sec"] for e in hub.events]
    assert elapsed == sorted(elapsed), f"elapsed must be monotonic: {elapsed}"
    assert elapsed[0] > 0, "elapsed must start positive"
    assert elapsed[-1] >= 1.0, f"backoff wait missing from elapsed: {elapsed}"


async def test_retry_exhaustion_keeps_same_callback(clock, monkeypatch):
    """All attempts failing still reuses one callback and one timer."""
    import backend.core.llm_client as llm_client_mod
    from backend.config.settings import APIConfig

    hub = RecordingHub()
    calls = {"n": 0}

    async def always_error_chat_stream(self, messages, temperature=0.1,
                                       max_tokens=None):
        calls["n"] += 1
        yield StreamChunk(type="error", error="stream boom")

    monkeypatch.setattr(
        llm_client_mod.LLMClient, "chat_stream", always_error_chat_stream
    )

    cfg = APIConfig(
        base_url="https://example.invalid",
        api_key="",
        model="m",
        max_tokens=1000,
        temperature=0.5,
        temperature_step=0.1,
        temperature_max_retries=2,
        backoff_max_retries=1,
        thinking_mode={},
        provider="openai",
        streaming_enabled=True,
    )
    client = llm_client_mod.LLMClient(cfg)

    cb = make_stream_progress_callback(
        hub=hub, context="analysis", session_id="retry-book", unit_idx=4
    )

    async def fake_sleep(seconds):
        clock.advance(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(client, "_sleep", fake_sleep)
    monkeypatch.setattr(
        llm_client_mod, "_get_failure_logger", lambda: _NullFailureLogger()
    )

    res = await client.chat_auto(
        [{"role": "user", "content": "hi"}], on_progress=cb
    )

    assert res.success is False, "every attempt failed so the call must fail"
    assert res.call_stats.get("attempts", 0) >= 2, f"expected retries: {res.call_stats}"
    assert calls["n"] == res.call_stats.get("attempts"), (
        "each attempt should hit chat_stream exactly once"
    )
    # error chunks are never broadcast, and the callback is not re-created
    assert hub.events == [], "error chunks must not broadcast token deltas"


# ---------------------------------------------------------------------------
# End-to-end entry -> retry chain.
#
# This is the only case that goes through a real production entry point AND the
# real LLMClient retry chain at the same time: `analyze_chapter` creates the
# callback with the real factory, then the real `chat_auto` ->
# `chat_stream_with_retry` -> `_run_stream_attempt` drives it across two
# attempts. Only the low-level stream source `LLMClient.chat_stream` and the
# backoff `_sleep` are mocked, so the retry loop, the attempt fan-out and the
# `on_progress` plumbing are all production code.
#
# The first attempt streams partial content and then fails, so there are real
# token events before the failure to compare against the second attempt.
# ---------------------------------------------------------------------------


async def test_analyzer_entry_retry_reuses_callback_across_attempts(
    clock, monkeypatch, tmp_path
):
    """One callback and one timer must span both attempts of a real retry."""
    import inspect

    import backend.core.analyzer as analyzer_mod
    import backend.core.llm_client as llm_client_mod
    import backend.core.stream_progress as sp_mod
    from backend.config.settings import APIConfig

    hub = RecordingHub()
    monkeypatch.setattr(analyzer_mod, "get_hub", lambda: hub)

    real_factory = sp_mod.make_stream_progress_callback
    factory_calls: list[dict] = []
    callbacks: list[object] = []

    def spy_factory(hub, context, session_id, unit_idx=0):
        """Wrap the real factory so we can count creations and keep identity."""
        factory_calls.append({
            "context": context,
            "session_id": session_id,
            "unit_idx": unit_idx,
        })
        inner = real_factory(
            hub=hub, context=context, session_id=session_id, unit_idx=unit_idx
        )

        async def recording(chunk):
            return await inner(chunk)

        callbacks.append(recording)
        return recording

    monkeypatch.setattr(
        analyzer_mod, "make_stream_progress_callback", spy_factory
    )

    # `on_progress` is a free variable of the real `_run_stream_attempt`; read it
    # straight out of that frame so we compare the object each attempt received.
    attempt_on_progress: list[int] = []
    stream_calls: list[int] = []

    async def fake_chat_stream(self, messages, temperature=0.1, max_tokens=None):
        frame = inspect.currentframe()
        while frame is not None:
            if "on_progress" in frame.f_locals:
                attempt_on_progress.append(id(frame.f_locals["on_progress"]))
                break
            frame = frame.f_back

        stream_calls.append(len(stream_calls) + 1)
        if len(stream_calls) == 1:
            clock.advance(0.5)  # streaming takes time
            yield StreamChunk(type="content", text="partial ")
            yield StreamChunk(type="error", error="429 Too Many Requests")
            return
        yield StreamChunk(type="content", text=_analysis_json())
        yield StreamChunk(
            type="usage",
            usage_prompt_tokens=10,
            usage_completion_tokens=20,
            is_final=True,
        )

    monkeypatch.setattr(llm_client_mod.LLMClient, "chat_stream", fake_chat_stream)

    cfg = APIConfig(
        base_url="https://example.invalid",
        api_key="",
        model="m",
        max_tokens=1000,
        temperature=0.5,
        temperature_step=0.1,
        temperature_max_retries=2,
        backoff_max_retries=1,
        thinking_mode={},
        provider="openai",
        streaming_enabled=True,
    )
    client = llm_client_mod.LLMClient(cfg)

    async def fake_sleep(seconds):
        clock.advance(seconds)  # keep the backoff wait on the controlled clock
        await asyncio.sleep(0)

    monkeypatch.setattr(client, "_sleep", fake_sleep)
    monkeypatch.setattr(
        llm_client_mod, "_get_failure_logger", lambda: _NullFailureLogger()
    )

    analyzer = object.__new__(analyzer_mod.NovelAnalyzer)
    analyzer._is_stopped = False
    analyzer.llm_client = client
    analyzer.prompt_builder = type("PB", (), {
        "build_messages": lambda self, c, n, kb: [{"role": "user", "content": c}]
    })()
    analyzer.config = type(
        "Cfg", (), {"api": type("Api", (), {"max_tokens": 1000})()}
    )()

    clock.advance(0.25)
    result, tokens, retry_info = await analyzer.analyze_chapter(
        chapter_number=11,
        chapter_content="chapter text",
        knowledge_base=KnowledgeBase(),
        block_size=1,
        book_id="retry-entry",
    )

    assert result is not None, f"second attempt should succeed: {retry_info}"
    assert retry_info.get("retries") == 1, f"expected one retry: {retry_info}"

    # the production factory ran exactly once, so exactly one callback exists
    assert len(factory_calls) == 1, (
        f"factory must run once, ran {len(factory_calls)} times"
    )
    assert len(callbacks) == 1, f"one callback expected, got {len(callbacks)}"
    assert factory_calls[0] == {
        "context": "analysis",
        "session_id": "retry-entry",
        "unit_idx": 11,
    }

    # both attempts received the very same on_progress object
    assert len(stream_calls) == 2, "chat_stream should have been called twice"
    assert len(attempt_on_progress) == 2, (
        f"expected two attempts, saw {attempt_on_progress}"
    )
    assert attempt_on_progress[0] == attempt_on_progress[1], (
        "both attempts must receive the same on_progress object"
    )
    assert attempt_on_progress[0] == id(callbacks[0]), (
        "the attempt callback must be the one the entry point created"
    )

    # the failed attempt still broadcast its partial content before failing
    assert len(hub.events) >= 2, f"expected broadcasts: {hub.events}"
    assert all(e["context"] == "analysis" for e in hub.events)
    assert all(e["session_id"] == "retry-entry" for e in hub.events)
    assert all(e["unit_idx"] == 11 for e in hub.events)

    elapsed = [e["delta"]["elapsed_sec"] for e in hub.events]
    assert elapsed == sorted(elapsed), f"elapsed must not reset: {elapsed}"
    assert elapsed[0] > 0, "elapsed must start positive"
    assert elapsed[-1] >= 1.0, f"backoff must be inside elapsed: {elapsed}"
    assert elapsed[-1] > elapsed[0], (
        "the timer must span the retry boundary: {elapsed}"
    )

    outputs = [e["delta"]["output_tokens"] for e in hub.events]
    assert outputs == sorted(outputs), (
        f"output_tokens must not go backwards: {outputs}"
    )
