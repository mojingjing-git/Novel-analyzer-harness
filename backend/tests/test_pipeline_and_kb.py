"""知识库（KnowledgeBase）、内存状态（MemoryState）、提示词限额与分析流水线（AnalysisPipeline）测试。"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.config.settings import AnalysisConfig, APIConfig, AppConfig
from backend.core.knowledge_base import KnowledgeBaseManager, _temp_kb_cache
from backend.core.memory_state import MemoryState
from backend.core.pipeline import AnalysisPipeline
from backend.core.prompt_limits import DEFAULT_LIMITS, LIMIT_FIELDS, from_dict, to_dict
from backend.models.analysis_result import AnalysisResult
from backend.progress_hub import ProgressHub
from backend.services.analysis_stats import AnalysisStats
from backend.services.pipeline_events import make_pipeline_callbacks
from backend.services.queue_manager import QueueItem


def _write_chapter(d: Path, ch: int, marker: str):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"chapter_{ch}_result.json").write_text(json.dumps({
        "chapter_number": ch,
        "cross_block": {"summary": f"第{ch}章：{marker}", "unresolved_questions": [], "new_leads": []},
        "core_events": [{"event": f"{marker}事件{ch}"}],
        "character_arcs": [], "foreshadowing": [], "plot_holes": [],
        "long_context_insights": {}, "updated_knowledge": {},
    }, ensure_ascii=False), encoding="utf-8")


def test_kb_fingerprint_and_copy_isolation(tmp_path):
    d = tmp_path / "output"
    _write_chapter(d, 1, "旧版第一章")
    kb1 = KnowledgeBaseManager.build_temp_knowledge(d, chapter_limit=1)
    assert any("旧版第一章" in s for s in kb1.recent_summaries)

    baseline_entry = _temp_kb_cache[(str(d), 1)]
    kb_all = KnowledgeBaseManager.build_temp_knowledge(d, chapter_limit=None)
    assert _temp_kb_cache[(str(d), None)][2] is not baseline_entry[2]
    kb_all.recent_summaries.append("污染标记")
    assert "污染标记" not in " ".join(KnowledgeBaseManager.build_temp_knowledge(d, chapter_limit=1).recent_summaries)

    _write_chapter(d, 1, "新版第一章改写")
    os.utime(d / "chapter_1_result.json", ns=(int(time.time() * 1e9) + 10_000_000,) * 2)
    _write_chapter(d, 2, "第二章新增")
    kb3 = KnowledgeBaseManager.build_temp_knowledge(d, chapter_limit=2)
    summaries = " ".join(kb3.recent_summaries)
    assert "新版第一章改写" in summaries and "第二章新增" in summaries


async def test_memory_state_rolling_and_skipped_persistence(tmp_path):
    structured = {"paradigm_layers": [{"chapters": "1-10", "text": "升级流"}], "global_milestones": ["ch1: 开局"]}
    state = MemoryState()
    await state.add_result(AnalysisResult(chapter_number=1))
    await state.add_result(AnalysisResult(chapter_number=2))
    state.rolling = {"rolling_structured": structured}

    snap_big = state.get_kb_snapshot(chapter_limit=99)
    snap_small = state.get_kb_snapshot(chapter_limit=1)
    assert snap_big.rolling_structured == structured
    assert snap_small.rolling_structured == structured
    assert state._snapshot_cache_kb.rolling_structured is not structured

    # Skipped & failed chapters roundtrip
    state.add_skipped(42, "内容审核拦截")
    state.add_failed(43, "普通失败")
    await state.flush_to_disk(tmp_path)

    state2 = MemoryState()
    await state2.restore_from_disk(tmp_path)
    assert state2._skipped_chapters == {42: "内容审核拦截"}
    assert state2._failed_chapters == {43: "普通失败"}

    # 成功后清理陈旧 skipped 文件
    await state2.add_result(AnalysisResult(chapter_number=42))
    await state2.flush_to_disk(tmp_path)
    assert not (tmp_path / "chapter_42_skipped.json").exists()


def test_prompt_limits_validation():
    assert len(LIMIT_FIELDS) == 9
    assert from_dict({}) == DEFAULT_LIMITS
    limits = from_dict({"max_arcs": 12, "unknown_future_key": "ignored"})
    assert limits.max_arcs == 12 and "unknown_future_key" not in to_dict(limits)

    for bad in ({"max_arcs": True}, {"max_character_states": 2.0}, {"max_themes": 0}, {"max_arcs": -1}):
        with pytest.raises(ValueError):
            from_dict(bad)


async def test_pipeline_inflight_and_callbacks():
    cfg = AppConfig(
        api=APIConfig(base_url="https://test.com", api_key="test", model="test"),
        analysis=AnalysisConfig(concurrency=2, block_size=2, encoding_priority=["utf-8"], checkpoint_interval=10),
    )
    pipeline = AnalysisPipeline(config=cfg, directory=Path("/test/blocks"), knowledge_file=Path("/test/kb.json"))
    state = MemoryState(checkpoint_interval=10, kb_limits={})
    fp = MagicMock()
    fp.read_block.return_value = "第一章 内容"

    captured = {}

    async def fake_analyze(block_id, content, kb, block_size=1, book_id=""):
        captured["during"] = dict(pipeline._inflight_blocks)
        return AnalysisResult(chapter_number=block_id), (10, 20), {"retries": 0, "failed_tokens": 0}

    analyzer = MagicMock(analyze_chapter=fake_analyze)
    pipeline._block_map = {100: [100, 101]}
    ok, *_ = await pipeline._analyze_one_block(analyzer, fp, state, 100, [100, 101], block_size=2)
    assert ok is True
    assert 100 in captured["during"]
    assert pipeline._inflight_blocks == {}

    # 回调工厂验证
    hub = ProgressHub()
    item = QueueItem(name="测试书", blocks_dir=Path("/x/blocks"), workspace_dir=Path("/x"), start_time=time.time())
    stats = AnalysisStats()
    on_progress, on_token_stats = make_pipeline_callbacks(hub=hub, item=item, stats=stats, seen_characters=set())
    q = await hub.subscribe()
    try:
        await on_progress({"status": "start", "chapter": 5, "progress": 5, "total": 100, "range": "ch5-ch8"})
        m1 = await asyncio.wait_for(q.get(), timeout=1.0)
        m2 = await asyncio.wait_for(q.get(), timeout=1.0)
        assert [m1["type"], m2["type"]] == ["progress", "block_start"]
        assert m2["payload"]["range"] == "ch5-ch8" and item.completed_chapters == 5
    finally:
        await hub.unsubscribe(q)
