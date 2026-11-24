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


async def test_block_size_append_chapters_reanalyzes_tail_block(tmp_path, monkeypatch):
    from backend.services.queue_manager import QueueManager

    blocks_dir = tmp_path / "blocks"
    blocks_dir.mkdir(parents=True)
    for ch in range(1, 6):
        (blocks_dir / f"{ch:04d}.txt").write_text(f"第{ch}章 内容", encoding="utf-8")

    analyzed_calls: list[int] = []

    async def fake_analyze(self, block_id, content, kb, block_size=1, book_id=""):
        analyzed_calls.append(block_id)
        return (
            AnalysisResult(chapter_number=block_id, block_size=block_size),
            (10, 20),
            {"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN"},
        )

    monkeypatch.setattr("backend.core.pipeline.NovelAnalyzer.analyze_chapter", fake_analyze)

    cfg = AppConfig(
        api=APIConfig(base_url="https://test.com", api_key="test", model="test"),
        analysis=AnalysisConfig(concurrency=1, block_size=4, encoding_priority=["utf-8"], checkpoint_interval=10),
        working_directory=str(tmp_path),
    )
    p1 = AnalysisPipeline(config=cfg, directory=blocks_dir, knowledge_file=tmp_path / "kb.json")
    await p1.run()
    assert analyzed_calls == [1, 5]

    out_dir = tmp_path / "output"
    tail_data = json.loads((out_dir / "chapter_5_result.json").read_text(encoding="utf-8"))
    assert tail_data["block_size"] == 4
    assert tail_data["source_chapters"] == [5]

    # 验证 AnalysisResult.from_dict 保留 lead 字段
    parsed = AnalysisResult.from_dict({"chapter_number": 1, "foreshadowing": [{"clue": "c", "lead": True}]})
    assert parsed.foreshadowing[0].lead is True

    # 追加第 6、7 章后，队列进度与断点续跑都应识别尾块 5 需要重跑
    (blocks_dir / "0006.txt").write_text("第6章 新内容", encoding="utf-8")
    (blocks_dir / "0007.txt").write_text("第7章 新内容", encoding="utf-8")

    qm = QueueManager()
    qitem = QueueItem(name="追更测试", blocks_dir=blocks_dir, workspace_dir=tmp_path, block_size=4, total_chapters=5, completed_chapters=2, status="done")
    qm.add_item(qitem)
    assert qm.infer_progress(qitem) == 1
    qm.add_item(QueueItem(name="追更测试", blocks_dir=blocks_dir, workspace_dir=tmp_path, block_size=4, total_chapters=7, completed_chapters=1))
    assert qitem.total_chapters == 7 and qitem.status == "pending"

    analyzed_calls.clear()
    p2 = AnalysisPipeline(config=cfg, directory=blocks_dir, knowledge_file=tmp_path / "kb.json")
    await p2.run()
    assert analyzed_calls == [5]
    tail_data2 = json.loads((out_dir / "chapter_5_result.json").read_text(encoding="utf-8"))
    assert tail_data2["source_chapters"] == [5, 6, 7]


def test_memory_state_out_of_order_and_dict_trim(tmp_path):
    """乱序块不得覆盖更新的实体/关系状态；字典裁剪保尾且保留单调水位线并在恢复时重置。"""
    from backend.models.analysis_result import CharacterArc

    state = MemoryState(
        kb_limits={
            "max_kb_character_states": 2,
            "max_kb_character_relationships": 2,
        }
    )

    state._merge_one(AnalysisResult(
        chapter_number=2,
        character_arcs=[
            CharacterArc(name="老A", surface_action="路过集市", change_delta="初见"),
            CharacterArc(name="老B", surface_action="守门", change_delta="放行"),
        ],
    ))
    state._merge_one(AnalysisResult(
        chapter_number=10,
        character_arcs=[
            CharacterArc(name="主A", surface_action="已是宗门长老", change_delta="结盟"),
            CharacterArc(name="主B", surface_action="执事", change_delta="同路"),
        ],
    ))
    # 乱序历史块不得覆盖第 10 章的更新状态与关系
    state._merge_one(AnalysisResult(
        chapter_number=5,
        character_arcs=[
            CharacterArc(name="主A", surface_action="外门弟子", change_delta="五章敌对"),
            CharacterArc(name="主B", surface_action="外门杂役", change_delta="五章陌生"),
        ],
    ))
    assert state.kb.character_states["主A"] == "已是宗门长老"
    assert state.kb.character_relationships["主A-主B"] == "结盟；同路"

    # 插入更新章节的新人物，再于更大章号复现 主A/主B，应 pop+重插至字典尾部避免被最先淘汰
    state._merge_one(AnalysisResult(
        chapter_number=11,
        character_arcs=[
            CharacterArc(name="新A", surface_action="新出场", change_delta="初入山门"),
            CharacterArc(name="新B", surface_action="师弟", change_delta="同行"),
        ],
    ))
    state._merge_one(AnalysisResult(
        chapter_number=12,
        character_arcs=[
            CharacterArc(name="主A", surface_action="已是宗门大长老", change_delta="生死之交"),
            CharacterArc(name="主B", surface_action="副宗主", change_delta="共担风雨"),
        ],
    ))

    state._trim_kb(state.kb)
    assert list(state.kb.character_states.keys()) == ["主A", "主B"]
    assert list(state.kb.character_relationships.keys()) == ["新A-新B", "主A-主B"]
    # 字典虽已裁剪掉 老A / 老A-老B，但其单调水位线仍在，后续乱序低章号不能复活/覆盖
    assert state._char_state_chapter["老A"] == 2
    assert state._char_rel_chapter["老A-老B"] == 2

    out_dir = tmp_path / "output"
    out_dir.mkdir()
    (out_dir / "chapter_3_result.json").write_text(
        json.dumps(
            AnalysisResult(
                chapter_number=3,
                character_arcs=[
                    CharacterArc(name="林A", surface_action="闭关", change_delta="突破"),
                    CharacterArc(name="林B", surface_action="护法", change_delta="守护"),
                ],
            ).to_dict(),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state._restore_from_disk_sync(out_dir, block_size=1)
    assert "老A" not in state._char_state_chapter
    assert "老A-老B" not in state._char_rel_chapter
    assert state._char_state_chapter["林A"] == 3
    assert state._kb_last_chapter == 3


def test_kb_manager_merge_results_respects_custom_kb_limits(tmp_path):
    """merge_results 不应早熟硬截断列表，并应统一按 kb_limits 裁剪列表与字典。"""
    from backend.models.analysis_result import CharacterArc, UpdatedKnowledge

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for ch in range(1, 5):
        r = AnalysisResult(
            chapter_number=ch,
            updated_knowledge=UpdatedKnowledge(
                world_building=[f"设定_{ch}_{k}" for k in range(3)],
            ),
            character_arcs=[
                CharacterArc(name=f"B角色_{ch}", surface_action=f"状态_{ch}", change_delta=f"变_{ch}"),
                CharacterArc(name="A主角", surface_action=f"主态_{ch}", change_delta=f"主变_{ch}"),
            ],
        )
        (out_dir / f"chapter_{ch}_result.json").write_text(
            json.dumps(r.to_dict(), ensure_ascii=False), encoding="utf-8"
        )

    # 12 条世界观设定在放宽上限时不得被旧式硬编码早熟切掉；字典严格保尾裁剪
    kb_wide = KnowledgeBaseManager.merge_results(
        out_dir,
        kb_limits={
            "max_world_building": 12,
            "max_kb_character_states": 2,
            "max_kb_character_relationships": 2,
        },
    )
    assert len(kb_wide.world_building) == 12
    assert list(kb_wide.character_states.keys()) == ["B角色_4", "A主角"]
    assert list(kb_wide.character_relationships.keys()) == ["A主角-B角色_3", "A主角-B角色_4"]


async def test_pipeline_stops_immediately_on_account_breaker(tmp_path, monkeypatch):
    """账户级熔断触发后流水线应立即中止，不写 failed 落盘并上浮 account_tripped。"""
    from backend.common.error_types import Confidence, ErrorClass, ErrorScope, ErrorVerdict, SourceChannel
    from backend.core.analyzer import NovelAnalyzer

    blocks_dir = tmp_path / "blocks"
    blocks_dir.mkdir()
    for i in range(1, 4):
        (blocks_dir / f"{i:04d}.txt").write_text(f"第{i}章 测试正文 {i}", encoding="utf-8")

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    cfg = AppConfig(
        api=APIConfig(api_key="sk-test-secret-123456", base_url="http://127.0.0.1:9999/v1", model="test-model"),
        analysis=AnalysisConfig(concurrency=1, checkpoint_interval=10, block_size=1),
        working_directory=str(work_dir),
    )

    verdict = ErrorVerdict(
        cls=ErrorClass.AUTH,
        confidence=Confidence.STRUCTURED,
        source_channel=SourceChannel.SDK_ATTRIBUTE,
        http_status=401,
        action="stop",
        scope=ErrorScope.ACCOUNT_GLOBAL,
        message="Invalid API Key sk-test-secret-123456",
    )

    analyzed_blocks = []

    async def fake_analyze(self, block_id, content, kb, block_size=1, book_id=""):
        analyzed_blocks.append(block_id)
        self.llm_client._breaker_observe(False, verdict, "401 Invalid API Key sk-test-secret-123456")
        return None, (0, 0), {
            "retries": 0,
            "failed_tokens": 0,
            "usage_status": "UNKNOWN",
            "cached_tokens": 0,
            "error": "401 Invalid API Key sk-test-secret-123456",
            "verdict": verdict.to_dict(),
        }

    monkeypatch.setattr(NovelAnalyzer, "analyze_chapter", fake_analyze)

    pipeline = AnalysisPipeline(config=cfg, directory=blocks_dir, knowledge_file=tmp_path / "kb.json")
    summary = await pipeline.run()

    assert summary["account_tripped"] is True
    assert summary["stopped"] is True
    assert "401" in summary["account_trip_reason"]
    assert analyzed_blocks == [1]
    assert list((work_dir / "output").glob("chapter_*_failed.json")) == []


async def test_prompt_builder_honors_rolling_max_momentum_and_pipeline_kb_limits(tmp_path, monkeypatch):
    """PromptBuilder 应按 rolling_max_momentum 裁剪势头列表，pipeline 运行期应配置正确的 kb_limits。"""
    from backend.core.analyzer import NovelAnalyzer
    from backend.core.prompt_builder import PromptBuilder
    from backend.models.analysis_result import CrossBlock
    from backend.models.knowledge import KnowledgeBase

    kb = KnowledgeBase()
    kb.rolling_structured = {
        "recent_momentum": [f"势头项_{i}" for i in range(1, 21)],
    }
    pb = PromptBuilder(rolling_max_momentum=3)
    msgs = pb.build_messages("当前章节正文", 5, kb)
    combined = "\n".join(m["content"] for m in msgs)
    assert "势头项_18" in combined
    assert "势头项_20" in combined
    assert "势头项_17" not in combined

    blocks_dir = tmp_path / "blocks"
    blocks_dir.mkdir()
    (blocks_dir / "0001.txt").write_text("第一章 测试正文", encoding="utf-8")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    cfg = AppConfig(
        api=APIConfig(api_key="k", base_url="http://127.0.0.1:9999/v1", model="m"),
        analysis=AnalysisConfig(
            concurrency=1,
            block_size=1,
            max_kb_character_states=77,
            max_kb_character_relationships=88,
            rolling_update_interval=3,
            rolling_max_momentum=6,
        ),
        working_directory=str(work_dir),
    )

    async def fake_analyze(self, block_id, content, kb_arg, block_size=1, book_id=""):
        return AnalysisResult(chapter_number=block_id, cross_block=CrossBlock(summary="测试")), (10, 5), {
            "retries": 0,
            "failed_tokens": 0,
            "usage_status": "OK",
            "cached_tokens": 0,
        }

    monkeypatch.setattr(NovelAnalyzer, "analyze_chapter", fake_analyze)

    analyzer = NovelAnalyzer(cfg)
    assert analyzer.prompt_builder.rolling_max_momentum == 6

    pipeline = AnalysisPipeline(config=cfg, directory=blocks_dir, knowledge_file=tmp_path / "kb.json")
    await pipeline.run()
    assert pipeline.state is not None
    assert pipeline.state._kb_limits["max_kb_character_states"] == 77
    assert pipeline.state._kb_limits["max_kb_character_relationships"] == 88
