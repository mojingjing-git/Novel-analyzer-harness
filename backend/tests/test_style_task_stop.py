"""stop() 必须 cancel 风格提取任务——
该任务持有独立 LLMClient，不受 _llm.request_stop() 控制。"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config.settings import AppConfig
from backend.services.final_summary import FinalSummaryRunner


def _make_runner(tmp_path):
    return FinalSummaryRunner(
        config=AppConfig(), output_dir=tmp_path,
        start_chapter=1, end_chapter=4, batch_size=2, concurrency=2,
        on_progress=lambda p: None, on_token_stats=lambda p: None)


def test_stop_cancels_running_style_task(tmp_path):
    runner = _make_runner(tmp_path)
    started = asyncio.Event()

    async def hang_forever():
        started.set()
        await asyncio.sleep(60)   # 模拟在途 LLM 调用（带完整重试链）

    async def main():
        runner._style_task = asyncio.create_task(hang_forever())
        await asyncio.wait_for(started.wait(), timeout=2)
        runner.stop()             # 此前 stop() 对该任务无任何作用
        try:
            await asyncio.wait_for(runner._style_task, timeout=3)
        except asyncio.CancelledError:
            # 任务被 stop() 取消时 CancelledError 会穿透 wait_for 传播到等待方，
            # 属预期行为；能在此收到取消即证明修复生效
            pass

    asyncio.run(main())
    assert runner._style_task.cancelled()


def test_stop_is_noop_without_style_task(tmp_path):
    runner = _make_runner(tmp_path)
    runner.stop()                 # 不应抛 AttributeError
    assert runner._stop_requested is True


def _write_results(output_dir: Path, chapters: int):
    """写入最小 chapter_*_result.json + 两个归一化文件，
    保证 run() 不会在 style_task 创建前早退（缺章节结果/归一化文件会提前 raise）"""
    output_dir.mkdir(parents=True, exist_ok=True)
    for ch in range(1, chapters + 1):
        data = {
            "chapter_number": ch,
            "cross_block": {"summary": f"第{ch}章摘要内容", "unresolved_questions": [], "new_leads": []},
            "core_events": [],
            "character_arcs": [],
            "foreshadowing": [],
            "plot_holes": [],
            "long_context_insights": {},
            "updated_knowledge": {},
        }
        (output_dir / f"chapter_{ch}_result.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    output_subdir = output_dir / "output" if (output_dir / "output").is_dir() else output_dir
    (output_subdir / "locations_normalized.json").write_text(
        json.dumps({"schema_version": 1, "locations": [], "chapter_mtimes_hash": ""}, ensure_ascii=False),
        encoding="utf-8")
    (output_subdir / "spatial_relationships_normalized.json").write_text(
        json.dumps({"schema_version": 1, "relationships": [], "chapter_mtimes_hash": ""}, ensure_ascii=False),
        encoding="utf-8")


def test_exception_path_reaps_pending_style_task(tmp_path, monkeypatch):
    """run() 中途抛异常（如所有批次均失败）不得遗留孤儿 style_task"""
    _write_results(tmp_path, 4)
    runner = _make_runner(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def hang_style():
        started.set()
        await release.wait()

    async def fail_all(*a, **k):
        return None

    async def main():
        monkeypatch.setattr(runner, "_run_style_extraction", hang_style)
        monkeypatch.setattr(type(runner), "_call_llm_summary", fail_all)
        monkeypatch.setattr(type(runner), "_call_llm_final", fail_all)
        task_holder = {}

        real_create = asyncio.create_task

        def spy(coro):
            t = real_create(coro)
            task_holder["t"] = t
            return t

        monkeypatch.setattr(asyncio, "create_task", spy)

        run_task = asyncio.create_task(runner.run())
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.sleep(0.1)          # 让 run() 走到“所有批次均失败”的 raise
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(run_task, timeout=10)
        st = task_holder["t"]
        assert st.done(), "finally 必须已回收挂起的 style_task"

    asyncio.run(main())
