"""S8 写入一致性与交互保护（PLAN v2 §4 S8 C-09/C-16/C-18）。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend.vnext.consistency import (
    AFTER_COMMIT,
    AFTER_WRITE,
    BEFORE_COMMIT,
    BEFORE_WRITE,
    orphan_files,
    publish_atomically,
)
from backend.vnext.concurrency import LockGuard, run_thread_job


# ---------------------------------------------------------------------------
# 发布：两件事要么都成，要么都不成
# ---------------------------------------------------------------------------


class _Store:
    """最小的"文件 + 登记"假实现，用来观察崩溃后的世界。"""

    def __init__(self) -> None:
        self.files: set[str] = set()
        self.committed: set[str] = set()

    def write(self, name: str) -> None:
        self.files.add(name)

    def commit(self, name: str) -> None:
        self.committed.add(name)

    def remove(self, name: str) -> None:
        self.files.discard(name)


def _fault(point_to_raise: str):
    def hook(point: str) -> None:
        if point == point_to_raise:
            raise RuntimeError(f"崩在 {point}")
    return hook


def test_happy_path_publishes_both() -> None:
    store = _Store()
    outcome = publish_atomically(
        write_file=lambda: store.write("r1"),
        commit=lambda: store.commit("r1"),
        remove_file=lambda: store.remove("r1"),
    )
    assert outcome.published and outcome.wrote_file and outcome.committed
    assert store.files == store.committed == {"r1"}


def test_crash_before_write_touches_nothing() -> None:
    store = _Store()
    outcome = publish_atomically(
        write_file=lambda: store.write("r1"),
        commit=lambda: store.commit("r1"),
        remove_file=lambda: store.remove("r1"),
        fault=_fault(BEFORE_WRITE),
    )
    assert outcome.published is False
    assert store.files == set() and store.committed == set()


def test_crash_after_write_rolls_back_file() -> None:
    """文件写了、登记没成 → 必须删掉，否则留下一个看起来像成功的孤儿。"""
    store = _Store()
    outcome = publish_atomically(
        write_file=lambda: store.write("r1"),
        commit=lambda: store.commit("r1"),
        remove_file=lambda: store.remove("r1"),
        fault=_fault(AFTER_WRITE),
    )
    assert outcome.published is False
    assert outcome.cleanup_done is True
    assert store.files == set()
    assert store.committed == set()


def test_crash_before_commit_rolls_back_file() -> None:
    store = _Store()
    outcome = publish_atomically(
        write_file=lambda: store.write("r1"),
        commit=lambda: store.commit("r1"),
        remove_file=lambda: store.remove("r1"),
        fault=_fault(BEFORE_COMMIT),
    )
    assert outcome.published is False and store.files == set()


def test_crash_after_commit_is_reported_as_error_but_data_is_consistent() -> None:
    """登记已成功、只是收尾炸了：数据其实是一致的，必须如实区分。"""
    store = _Store()
    outcome = publish_atomically(
        write_file=lambda: store.write("r1"),
        commit=lambda: store.commit("r1"),
        remove_file=lambda: store.remove("r1"),
        fault=_fault(AFTER_COMMIT),
    )
    assert outcome.published is False
    assert outcome.committed is True          # 确实登记了
    assert store.files == store.committed == {"r1"}   # 磁盘与登记一致


def test_cleanup_failure_is_recorded_not_swallowed() -> None:
    """删不掉也要说：留下的孤儿必须在扫描时被识别。"""
    store = _Store()

    def _boom() -> None:
        raise OSError("文件被占用")

    outcome = publish_atomically(
        write_file=lambda: store.write("r1"),
        commit=lambda: store.commit("r1"),
        remove_file=_boom,
        fault=_fault(AFTER_WRITE),
    )
    assert outcome.published is False
    assert outcome.cleanup_done is False
    assert store.files == {"r1"}


def test_orphan_scan_finds_unreferenced_files(tmp_path) -> None:
    (tmp_path / "known.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    (tmp_path / "orphan.json").write_text(json.dumps({"a": 2}), encoding="utf-8")
    found = orphan_files(tmp_path, known_ids={"known"})
    assert [p.name for p in found] == ["orphan.json"]


def test_orphan_scan_on_missing_dir_is_empty(tmp_path) -> None:
    assert orphan_files(tmp_path / "nope", known_ids=set()) == []


# ---------------------------------------------------------------------------
# 取消不丢账
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def test_finish_callback_runs_even_when_waiter_cancelled() -> None:
    """取消等待方不该让"这次消耗"消失——记账由线程结束触发。"""
    recorded: list[str] = []

    async def scenario() -> None:
        def slow() -> str:
            time.sleep(0.15)
            return "ok"

        task = asyncio.ensure_future(
            run_thread_job(slow, on_finish=lambda result, exc: recorded.append(result or "ERR"))
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # 线程仍在跑，稍等它结束
        await asyncio.sleep(0.3)

    _run(scenario())
    assert recorded == ["ok"]


def test_finish_callback_sees_exception_from_worker() -> None:
    seen: list[str] = []

    async def scenario() -> None:
        def boom() -> str:
            raise ValueError("模型超时")

        with pytest.raises(ValueError):
            await run_thread_job(boom, on_finish=lambda r, exc: seen.append(type(exc).__name__))

    _run(scenario())
    assert seen == ["ValueError"]


def test_result_is_available_after_normal_completion() -> None:
    seen: list[int] = []

    async def scenario() -> int:
        job = await run_thread_job(lambda: 42, on_finish=lambda r, e: seen.append(r or 0))
        return await job.future

    assert _run(scenario()) == 42
    assert seen == [42]


def test_lock_guard_flags_await_while_holding() -> None:
    guard = LockGuard()
    guard.check(holding=True, awaiting=True, where="publish")
    guard.check(holding=True, awaiting=False, where="commit")
    assert guard.violations == ["publish"]
