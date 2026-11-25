"""知识库（KnowledgeBase）、内存状态（MemoryState）、提示词限额与分析流水线（AnalysisPipeline）测试。"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.config.constants import ROLLING_ARCHIVE_THINKING_MIN_TOKENS
from backend.config.settings import AnalysisConfig, APIConfig, AppConfig
from backend.core.analyzer import BatchAnalysisResult
from backend.core.batch_packer import (
    effective_batch_cap,
    is_thinking_enabled,
    pack_chapter_batches,
)
from backend.core.knowledge_base import KnowledgeBaseManager, _temp_kb_cache
from backend.core.memory_state import MemoryState
from backend.core.pipeline import AnalysisPipeline
from backend.core.prompt_limits import DEFAULT_LIMITS, LIMIT_FIELDS, from_dict, to_dict
from backend.models.analysis_result import AnalysisResult
from backend.progress_hub import ProgressHub
from backend.services.analysis_stats import AnalysisStats
from backend.services.pipeline_events import make_pipeline_callbacks
from backend.services.queue_manager import QueueItem
from backend.services.usage_ledger import UsageLedger


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
    fp.read_chapters_batch.return_value = [(100, "第100章 内容"), (101, "第101章 内容")]

    captured = {}

    async def fake_analyze_batch(chapter_items, kb, book_id=""):
        captured["during"] = dict(pipeline._inflight_blocks)
        return BatchAnalysisResult(
            valid_results={
                100: AnalysisResult(chapter_number=100, block_size=1, source_chapters=[100]),
                101: AnalysisResult(chapter_number=101, block_size=1, source_chapters=[101]),
            },
            missing_chapters=[],
            tokens=(10, 20),
            retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN"},
        )

    analyzer = MagicMock(analyze_batch=fake_analyze_batch)
    outcome, _elapsed = await pipeline._execute_one_batch(analyzer, fp, state, [100, 101])
    assert sorted(outcome.valid_results.keys()) == [100, 101]
    assert 100 in captured["during"]
    assert pipeline._inflight_blocks == {}

    # 回调工厂验证（restored=True 不记 stats/ledger；批完成逐章发 discovery 并累加 chapters_count）
    hub = ProgressHub()
    item = QueueItem(name="测试书", blocks_dir=Path("/x/blocks"), workspace_dir=Path("/x"), start_time=time.time())
    stats = AnalysisStats()
    on_progress, _on_token_stats = make_pipeline_callbacks(hub=hub, item=item, stats=stats, seen_characters=set())
    q = await hub.subscribe()
    try:
        await on_progress({"status": "start", "chapter": 5, "progress": 5, "total": 100, "range": "ch5-ch6"})
        m1 = await asyncio.wait_for(q.get(), timeout=1.0)
        m2 = await asyncio.wait_for(q.get(), timeout=1.0)
        assert [m1["type"], m2["type"]] == ["progress", "block_start"]
        assert m2["payload"]["range"] == "ch5-ch6" and item.completed_chapters == 5

        await on_progress({
            "status": "done", "chapter": 1, "progress": 4, "total": 100,
            "elapsed": 0, "tokens": 0, "restored": True,
        })
        assert len(stats.chapter_stats) == 0
    finally:
        await hub.unsubscribe(q)


async def test_block_size_append_chapters_reanalyzes_tail_block(tmp_path, monkeypatch):
    """请求层按批打包、落盘层强制单章，中途改变 block_size 与追更续跑无缝衔接。"""
    from backend.services.queue_manager import QueueManager

    blocks_dir = tmp_path / "blocks"
    blocks_dir.mkdir(parents=True)
    for ch in range(1, 6):
        (blocks_dir / f"{ch:04d}.txt").write_text(f"第{ch}章 内容", encoding="utf-8")

    analyzed_batches: list[list[int]] = []

    async def fake_analyze_batch(self, chapter_items, kb, book_id=""):
        chs = [int(c) for c, _ in chapter_items]
        analyzed_batches.append(chs)
        return BatchAnalysisResult(
            valid_results={
                c: AnalysisResult(chapter_number=c, block_size=1, source_chapters=[c])
                for c in chs
            },
            missing_chapters=[],
            tokens=(10 * len(chs), 20 * len(chs)),
            retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN"},
        )

    monkeypatch.setattr("backend.core.pipeline.NovelAnalyzer.analyze_batch", fake_analyze_batch)

    cfg = AppConfig(
        api=APIConfig(base_url="https://test.com", api_key="test", model="test", max_tokens=65536),
        analysis=AnalysisConfig(concurrency=1, block_size=4, encoding_priority=["utf-8"], checkpoint_interval=10),
        working_directory=str(tmp_path),
    )
    p1 = AnalysisPipeline(config=cfg, directory=blocks_dir, knowledge_file=tmp_path / "kb.json")
    await p1.run()
    assert analyzed_batches == [[1, 2, 3, 4], [5]]

    out_dir = tmp_path / "output"
    tail_data = json.loads((out_dir / "chapter_5_result.json").read_text(encoding="utf-8"))
    assert tail_data["block_size"] == 1
    assert tail_data["source_chapters"] == [5]
    assert (out_dir / "chapter_2_result.json").exists()

    # 验证 AnalysisResult.from_dict 保留 lead 字段
    parsed = AnalysisResult.from_dict({"chapter_number": 1, "foreshadowing": [{"clue": "c", "lead": True}]})
    assert parsed.foreshadowing[0].lead is True

    # 追加第 6、7 章后：QueueManager 按单章进度统计（5/7），中途改为 block_size=2 续跑只跑 [6, 7]
    (blocks_dir / "0006.txt").write_text("第6章 新内容", encoding="utf-8")
    (blocks_dir / "0007.txt").write_text("第7章 新内容", encoding="utf-8")

    qm = QueueManager()
    qitem = QueueItem(name="追更测试", blocks_dir=blocks_dir, workspace_dir=tmp_path, block_size=4, total_chapters=5, completed_chapters=5, status="done")
    qm.add_item(qitem)
    assert qm.infer_progress(qitem) == 5
    qm.add_item(QueueItem(name="追更测试", blocks_dir=blocks_dir, workspace_dir=tmp_path, block_size=2, total_chapters=7, completed_chapters=5))
    assert qitem.total_chapters == 7 and qitem.status == "pending"

    analyzed_batches.clear()
    cfg2 = AppConfig.from_dict(cfg.to_dict())
    cfg2.analysis.block_size = 2
    p2 = AnalysisPipeline(config=cfg2, directory=blocks_dir, knowledge_file=tmp_path / "kb.json")
    await p2.run()
    assert analyzed_batches == [[6, 7]]
    tail_data7 = json.loads((out_dir / "chapter_7_result.json").read_text(encoding="utf-8"))
    assert tail_data7["block_size"] == 1
    assert tail_data7["source_chapters"] == [7]


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
    state._restore_from_disk_sync(out_dir)
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

    analyzed_batches = []

    async def fake_analyze_batch(self, chapter_items, kb, book_id=""):
        chs = [int(c) for c, _ in chapter_items]
        analyzed_batches.append(chs)
        self.llm_client._breaker_observe(False, verdict, "401 Invalid API Key sk-test-secret-123456")
        return BatchAnalysisResult(
            valid_results={},
            missing_chapters=chs,
            tokens=(0, 0),
            retry_info={
                "retries": 0,
                "failed_tokens": 0,
                "usage_status": "UNKNOWN",
                "cached_tokens": 0,
                "error": "401 Invalid API Key sk-test-secret-123456",
                "verdict": verdict.to_dict(),
            },
            account_tripped=True,
            account_trip_reason="401 Invalid API Key sk-test-secret-123456",
        )

    monkeypatch.setattr("backend.core.pipeline.NovelAnalyzer.analyze_batch", fake_analyze_batch)

    pipeline = AnalysisPipeline(config=cfg, directory=blocks_dir, knowledge_file=tmp_path / "kb.json")
    summary = await pipeline.run()

    assert summary["account_tripped"] is True
    assert summary["stopped"] is True
    assert "401" in summary["account_trip_reason"]
    assert analyzed_batches == [[1]]
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
    assert "=== [ID=5] 开始 ===" in combined and "=== [ID=5] 结束 ===" in combined
    assert "foreshadowing` 中 `高` 原则上不超过 1 条" in combined
    assert "同一批次内后几章已经明确揭晓答案的短期悬念" in combined

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

    async def fake_analyze_batch(self, chapter_items, kb_arg, book_id=""):
        return BatchAnalysisResult(
            valid_results={
                int(c): AnalysisResult(chapter_number=int(c), block_size=1, source_chapters=[int(c)], cross_block=CrossBlock(summary="测试"))
                for c, _ in chapter_items
            },
            missing_chapters=[],
            tokens=(10, 5),
            retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN", "cached_tokens": 0},
        )

    monkeypatch.setattr(NovelAnalyzer, "analyze_batch", fake_analyze_batch)

    analyzer = NovelAnalyzer(cfg)
    assert analyzer.prompt_builder.rolling_max_momentum == 6

    pipeline = AnalysisPipeline(config=cfg, directory=blocks_dir, knowledge_file=tmp_path / "kb.json")
    await pipeline.run()
    assert pipeline.state is not None
    assert pipeline.state._kb_limits["max_kb_character_states"] == 77
    assert pipeline.state._kb_limits["max_kb_character_relationships"] == 88


def test_batch_packer_and_thinking_precedence():
    """验证 thinking 判定优先级、max_tokens<=0 防护与字符/输出/数量三上限动态装包。"""
    # 1. 显式关 > 显式开 > 常开模型兜底 > Anthropic 默认关
    assert is_thinking_enabled(APIConfig(model="deepseek-reasoner", thinking_mode={"enable_thinking": False})) is False
    assert is_thinking_enabled(APIConfig(model="deepseek-reasoner", reasoning_effort="none")) is False
    assert is_thinking_enabled(APIConfig(model="gpt-4o", reasoning_effort="low")) is True
    assert is_thinking_enabled(APIConfig(model="deepseek-reasoner", thinking_mode={}, reasoning_effort="")) is True
    assert is_thinking_enabled(APIConfig(model="claude-sonnet-4-5", provider="auto")) is False
    assert is_thinking_enabled(APIConfig(model="")) is False

    # 2. effective_batch_cap
    assert effective_batch_cap(8, None, True) == 1
    assert effective_batch_cap(8, 0, False) == 1
    assert effective_batch_cap(8, -50, False) == 1
    assert effective_batch_cap(8, 4000, False) == 2
    assert effective_batch_cap(8, 130000, True) == 8

    # 3. pack_chapter_batches（字符预算 + 单章超限独立成批）
    lengths = {1: 5000, 2: 5000, 3: 5000, 4: 18000, 5: 3000}
    packed = pack_chapter_batches(
        [1, 2, 3, 4, 5],
        lengths,
        block_size=8,
        max_content_chars=12000,
        max_tokens=65536,
        thinking_enabled=False,
    )
    assert packed == [[1, 2], [3], [4], [5]]


async def test_pipeline_dynamic_split_partial_legacy_skip_and_rolling_thinking(tmp_path, monkeypatch):
    """覆盖二分拆批隔离、部分成功追回、旧版跳过忽略、max-50 断点补发与思考归档下限。"""
    from backend.core.error_types import LLMResult

    blocks_dir = tmp_path / "blocks"
    blocks_dir.mkdir()
    for ch in range(1, 7):
        (blocks_dir / f"{ch:04d}.txt").write_text(f"第{ch}章 正文内容", encoding="utf-8")

    work_dir = tmp_path / "work"
    out_dir = work_dir / "output"
    out_dir.mkdir(parents=True)

    # 写入旧版 block_size=4 产物与旧版 skipped 标记：续跑恢复必须忽略并重跑
    (out_dir / "chapter_1_result.json").write_text(
        json.dumps({"chapter_number": 1, "block_size": 4, "source_chapters": [1, 2, 3, 4]}),
        encoding="utf-8",
    )
    (out_dir / "chapter_2_skipped.json").write_text(
        json.dumps({"chapter_number": 2, "reason": "旧整块拦截"}),
        encoding="utf-8",
    )

    calls: list[list[int]] = []

    async def fake_analyze_batch(self, chapter_items, kb_arg, book_id=""):
        chs = [int(c) for c, _ in chapter_items]
        calls.append(chs)
        # 批 [1,2,3,4] 模拟输出截断零产出 → 二分拆为 [1,2] 和 [3,4]
        if chs == [1, 2, 3, 4]:
            return BatchAnalysisResult(
                valid_results={},
                missing_chapters=chs,
                tokens=(100, 50),
                retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN", "error": "max_tokens"},
                truncated=True,
            )
        # [1,2] 部分成功：第1章成功，第2章缺失 → 触发 [2] 追回
        if chs == [1, 2]:
            return BatchAnalysisResult(
                valid_results={1: AnalysisResult(chapter_number=1)},
                missing_chapters=[2],
                tokens=(50, 40),
                retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN"},
            )
        # [3,4] 审核拦截 → 二分拆 [3], [4]；仅第3章单章跳过，第4章放行成功
        if chs == [3, 4]:
            return BatchAnalysisResult(
                valid_results={},
                missing_chapters=chs,
                tokens=(20, 0),
                retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN", "error": "[内容审核拦截]"},
            )
        if chs == [3]:
            return BatchAnalysisResult(
                valid_results={},
                missing_chapters=[3],
                tokens=(10, 0),
                retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN", "error": "[内容审核拦截]"},
            )
        return BatchAnalysisResult(
            valid_results={c: AnalysisResult(chapter_number=c) for c in chs},
            missing_chapters=[],
            tokens=(30, 20),
            retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN"},
        )

    monkeypatch.setattr("backend.core.pipeline.NovelAnalyzer.analyze_batch", fake_analyze_batch)

    events: list[dict] = []

    async def on_progress(p: dict):
        events.append(p)

    cfg = AppConfig(
        api=APIConfig(api_key="k", base_url="http://127.0.0.1:9999/v1", model="deepseek-reasoner", max_tokens=65536),
        analysis=AnalysisConfig(concurrency=1, block_size=4, skip_moderation_blocked=True, checkpoint_interval=10),
        working_directory=str(work_dir),
    )
    pipeline = AnalysisPipeline(
        config=cfg,
        directory=blocks_dir,
        knowledge_file=tmp_path / "kb.json",
        on_progress=on_progress,
    )
    summary = await pipeline.run()
    assert summary["total_analyzed"] == 5
    assert summary["skipped_chapters"] == ["3(内容审核拦截)"]
    assert summary["failed_chapters"] == []
    assert [1, 2, 3, 4] in calls and [1, 2] in calls and [2] in calls and [3, 4] in calls and [3] in calls and [4] in calls
    # 每个 DONE/FAILED/SKIPPED 事件都不直接带 model/cached_tokens（避免 UsageLedger 双重计费）
    for ev in events:
        if ev.get("status") in ("done", "failed", "skipped"):
            assert "model" not in ev and "cached_tokens" not in ev

    # max-50 断点补发 + UsageLedger 不重复计费
    big_dir = tmp_path / "big_book"
    big_blocks = big_dir / "blocks"
    big_out = big_dir / "output"
    big_blocks.mkdir(parents=True)
    big_out.mkdir(parents=True)
    for ch in range(1, 61):
        (big_blocks / f"{ch:04d}.txt").write_text(f"第{ch}章", encoding="utf-8")
        (big_out / f"chapter_{ch}_result.json").write_text(
            json.dumps(AnalysisResult(chapter_number=ch, block_size=1, source_chapters=[ch]).to_dict()),
            encoding="utf-8",
        )
    resume_events: list[dict] = []
    ledger = UsageLedger(tmp_path / "usage.json")
    stats = AnalysisStats()
    item = QueueItem(name="大书", blocks_dir=big_blocks, workspace_dir=big_dir)
    cb_prog, _ = make_pipeline_callbacks(hub=ProgressHub(), item=item, stats=stats, seen_characters=set())
    monkeypatch.setattr("backend.services.pipeline_events.ledger", ledger)

    async def capture_prog(p: dict):
        resume_events.append(p)
        await cb_prog(p)

    cfg_big = AppConfig.from_dict(cfg.to_dict())
    cfg_big.working_directory = str(big_dir)
    p_big = AnalysisPipeline(config=cfg_big, directory=big_blocks, knowledge_file=big_dir / "kb.json", on_progress=capture_prog)
    await p_big.run()
    restored_done = [e for e in resume_events if e.get("restored")]
    assert len(restored_done) == 50
    assert restored_done[-1]["progress"] == 60 and restored_done[-1]["total"] == 60
    assert len(stats.chapter_stats) == 0 and ledger.snapshot()["chapters_done"] == 0

    # 思考模型 rolling archive 自动抬升 max_tokens 下限并透传 model/cached_tokens
    rolling_calls: list[dict] = []
    rolling_toks: list[dict] = []

    class FakeRollingClient:
        breaker_tripped = False

        async def chat_with_retry(self, messages, max_tokens=100):
            rolling_calls.append({"max_tokens": max_tokens})
            return LLMResult(True, "ch1-10: 里程碑概括", "", (20, 15), cached_tokens=7)

    async def cap_tok(p: dict):
        rolling_toks.append(p)

    p_arch = AnalysisPipeline(config=cfg, directory=blocks_dir, knowledge_file=tmp_path / "kb.json", on_token_stats=cap_tok)
    sd = {"global_milestones": [], "recent_momentum": ["ch1: a", "ch2: b", "ch3: c"]}
    await p_arch._archive_momentum_to_milestone(FakeRollingClient(), sd, sd["recent_momentum"])
    assert rolling_calls[0]["max_tokens"] >= ROLLING_ARCHIVE_THINKING_MIN_TOKENS
    assert rolling_toks[0]["model"] == "deepseek-reasoner" and rolling_toks[0]["cached_tokens"] == 7


async def test_pipeline_stop_no_failed_markers_and_retry_rounds_halved_cap(tmp_path, monkeypatch):
    """stop() 中止时不写 failed 落盘；补跑轮按减半批上限重装包重试并最终恢复或落 failed。"""
    blocks_dir = tmp_path / "blocks"
    blocks_dir.mkdir()
    for ch in range(1, 5):
        (blocks_dir / f"{ch:04d}.txt").write_text(f"第{ch}章", encoding="utf-8")

    # 1. stop() 触发：不落 chapter_*_failed.json / chapter_*_skipped.json
    w1 = tmp_path / "w1"
    cfg_stop = AppConfig(
        api=APIConfig(api_key="k", base_url="http://127.0.0.1:9999/v1", model="gpt-4o", max_tokens=65536),
        analysis=AnalysisConfig(concurrency=1, block_size=4, failed_block_retry_rounds=2),
        working_directory=str(w1),
    )

    async def stop_analyze(self, chapter_items, kb_arg, book_id=""):
        chs = [int(c) for c, _ in chapter_items]
        return BatchAnalysisResult(
            valid_results={},
            missing_chapters=chs,
            tokens=(0, 0),
            retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN", "error": "用户请求停止"},
            stopped=True,
        )

    monkeypatch.setattr("backend.core.pipeline.NovelAnalyzer.analyze_batch", stop_analyze)
    res_stop = await AnalysisPipeline(config=cfg_stop, directory=blocks_dir, knowledge_file=w1 / "kb.json").run()
    assert res_stop["failed_chapters"] == []
    assert list((w1 / "output").glob("chapter_*_failed.json")) == []

    # 2. 非可拆整批失败 → 补跑第 1 轮批上限减半（4 → 2），第 2 轮（2 → 1）全部追回
    w2 = tmp_path / "w2"
    cfg_retry = AppConfig(
        api=APIConfig(api_key="k", base_url="http://127.0.0.1:9999/v1", model="gpt-4o", max_tokens=65536),
        analysis=AnalysisConfig(concurrency=1, block_size=4, failed_block_retry_rounds=2),
        working_directory=str(w2),
    )
    retry_trace: list[list[int]] = []

    async def halve_retry_analyze(self, chapter_items, kb_arg, book_id=""):
        chs = [int(c) for c, _ in chapter_items]
        retry_trace.append(chs)
        if len(chs) > 1:
            return BatchAnalysisResult(
                valid_results={},
                missing_chapters=chs,
                tokens=(10, 0),
                retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN", "error": "JSON解析失败"},
            )
        return BatchAnalysisResult(
            valid_results={chs[0]: AnalysisResult(chapter_number=chs[0])},
            missing_chapters=[],
            tokens=(10, 10),
            retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN"},
        )

    monkeypatch.setattr("backend.core.pipeline.NovelAnalyzer.analyze_batch", halve_retry_analyze)
    res_retry = await AnalysisPipeline(config=cfg_retry, directory=blocks_dir, knowledge_file=w2 / "kb.json").run()
    assert res_retry["total_analyzed"] == 4
    assert res_retry["failed_chapters"] == []
    # 主批 [1,2,3,4] → 第1轮补跑上限减半 [1,2],[3,4] → 第2轮再减半逐章 [1],[2],[3],[4]
    assert retry_trace == [[1, 2, 3, 4], [1, 2], [3, 4], [1], [2], [3], [4]]


def test_immutable_system_prompt_cached_stats_and_thinking_effort_packer():
    """验证世界观/主题移入 user_context 保 system 不变、cached_tokens 累加与分档 Thinking 装包。"""
    from types import SimpleNamespace
    from backend.config.settings import APIConfig
    from backend.core.batch_packer import effective_batch_cap, pack_chapter_batches, resolve_thinking_effort
    from backend.core.prompt_builder import PromptBuilder
    from backend.models.knowledge import KnowledgeBase
    from backend.services.analysis_stats import AnalysisStats

    pb = PromptBuilder(max_world_items=2, max_themes=2)
    kb1 = KnowledgeBase(world_building=["世界A", "世界B"], thematic_elements=["主题1"])
    kb2 = KnowledgeBase(world_building=["世界C", "世界D"], thematic_elements=["主题2"])
    m1 = pb.build_batch_messages([(1, "第一章正文")], kb1)
    m2 = pb.build_batch_messages([(2, "第二章正文")], kb2)
    assert m1[0]["content"] == m2[0]["content"]
    assert "[已知世界观]" in m2[1]["content"] and "[主题元素]" in m2[1]["content"]

    stats = AnalysisStats()
    stats.reset()
    base = stats.baseline()
    stats.accumulate_token_stats({"category": "chapter", "input_tokens": 100, "output_tokens": 50, "cached_tokens": 40, "model": "m1"})
    stats.accumulate_token_stats({"category": "rolling", "input_tokens": 20, "output_tokens": 10, "cached_tokens": 15, "model": "m1"})
    cons = stats.book_consumption(SimpleNamespace(name="b", start_time=0.0, end_time=1.0), base)
    assert cons["cached_tokens"] == 55
    assert cons["categories"]["chapter"]["cached_tokens"] == 40
    assert cons["categories"]["rolling"]["cached_tokens"] == 15

    assert resolve_thinking_effort(APIConfig(model="m", reasoning_effort="none", thinking_mode={"reasoning_effort": "none"})) == ""
    assert resolve_thinking_effort(APIConfig(model="m", thinking_mode={"reasoning_effort": "medium"})) == "medium"
    assert resolve_thinking_effort(APIConfig(model="m", thinking_mode={"reasoning_effort": "high"})) == "high"
    assert effective_batch_cap(8, 32768, False) == 8
    assert effective_batch_cap(8, 32768, True, reasoning_effort="low") == 5
    assert effective_batch_cap(8, 32768, True, reasoning_effort="medium") == 4
    assert effective_batch_cap(8, 32768, True, reasoning_effort="high") == 3
    assert pack_chapter_batches([1, 2, 5, 6], {1: 100, 2: 100, 5: 100, 6: 100}, block_size=8, max_content_chars=50000, max_tokens=32768, thinking_enabled=False, break_on_gap=True) == [[1, 2], [5, 6]]


async def test_pipeline_adaptive_shrink_on_warmup_timeout_and_partial_truncation(tmp_path, monkeypatch):
    """超时或部分截断触发自适应缩批重排后续队列，且同波次并发失败不把上限雪崩压到 1。"""
    blocks_dir = tmp_path / "blocks"
    blocks_dir.mkdir()
    for ch in range(1, 13):
        (blocks_dir / f"{ch:04d}.txt").write_text(f"第{ch}章", encoding="utf-8")

    trace: list[list[int]] = []

    async def adaptive_analyze(self, chapter_items, kb_arg, book_id=""):
        chs = [int(c) for c, _ in chapter_items]
        trace.append(chs)
        if len(chs) >= 4:
            return BatchAnalysisResult(
                valid_results={},
                missing_chapters=chs,
                tokens=(50, 0),
                retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN", "error": "请求超时 (timeout=300s): TimeoutError"},
            )
        return BatchAnalysisResult(
            valid_results={c: AnalysisResult(chapter_number=c) for c in chs},
            missing_chapters=[],
            tokens=(40, 20),
            retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN"},
        )

    monkeypatch.setattr("backend.core.pipeline.NovelAnalyzer.analyze_batch", adaptive_analyze)
    cfg = AppConfig(
        api=APIConfig(api_key="k", base_url="http://127.0.0.1:9999/v1", model="gpt-4o", max_tokens=65536),
        analysis=AnalysisConfig(concurrency=2, block_size=4, checkpoint_interval=20),
        working_directory=str(tmp_path / "w"),
    )
    res = await AnalysisPipeline(config=cfg, directory=blocks_dir, knowledge_file=tmp_path / "kb.json").run()
    assert res["total_analyzed"] == 12
    assert res["failed_chapters"] == []
    # 首批 [1,2,3,4] 超时拆批后，剩余批次 [5,6,7,8] 与 [9,10,11,12] 应同步缩成 <=2 章，不再出现 4 章批
    assert [5, 6, 7, 8] not in trace
    assert [9, 10, 11, 12] not in trace
    assert [5, 6] in trace and [7, 8] in trace and [9, 10] in trace and [11, 12] in trace


async def test_targeted_rerun_isolation_kb_rebuild_and_failure_preservation(tmp_path, monkeypatch):
    """定点重跑隔离自身与未来剧透、允许覆盖落盘、失败保留旧结果并无重复重建 KB。"""
    from backend.core.knowledge_base import KnowledgeBaseManager

    book_dir = tmp_path / "book"
    blocks_dir = book_dir / "blocks"
    out_dir = book_dir / "output"
    blocks_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)

    for ch in range(1, 5):
        (blocks_dir / f"{ch:04d}.txt").write_text(f"第{ch}章正文", encoding="utf-8")
        res = AnalysisResult.from_dict({
            "chapter_number": ch,
            "block_size": 1,
            "source_chapters": [ch],
            "core_events": [{"id": 1, "event": f"旧事件_{ch}", "characters": f"人{ch}", "function": "f", "importance": "中"}],
            "updated_knowledge": {
                "timeline": f"T{ch}",
                "world_building": [f"旧设定_{ch}"],
            },
        })
        (out_dir / f"chapter_{ch}_result.json").write_text(json.dumps(res.to_dict(), ensure_ascii=False), encoding="utf-8")

    # 写入推到第 4 章的 rolling_summary：重跑第 2 章时严禁把第 4 章未来进展注入 Prompt
    (out_dir / "rolling_summary.json").write_text(
        json.dumps({
            "last_updated_chapter": 4,
            "rolling_structured": {"global_milestones": ["第4章大结局剧透"]},
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    seen_prompts: dict[int, dict] = {}

    async def rerun_analyze(self, chapter_items, kb_arg, book_id=""):
        chs = [int(c) for c, _ in chapter_items]
        seen_prompts[chs[0]] = {
            "batch": list(chs),
            "facts": list(kb_arg.verified_facts),
            "world": list(kb_arg.world_building),
            "rolling": dict(kb_arg.rolling_structured or {}),
        }
        if chs == [2]:
            return BatchAnalysisResult(
                valid_results={
                    2: AnalysisResult.from_dict({
                        "chapter_number": 2,
                        "block_size": 1,
                        "source_chapters": [2],
                        "core_events": [{"id": 1, "event": "新事件_2", "characters": "人2", "function": "f", "importance": "高"}],
                        "updated_knowledge": {"timeline": "T2_new", "world_building": ["新设定_2"]},
                    })
                },
                missing_chapters=[],
                tokens=(20, 15),
                retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN"},
            )
        # 故意让第 4 章失败，验证保留磁盘原有 chapter_4_result.json 且不落 _failed.json
        return BatchAnalysisResult(
            valid_results={},
            missing_chapters=chs,
            tokens=(10, 0),
            retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN", "error": "JSON解析失败"},
        )

    monkeypatch.setattr("backend.core.pipeline.NovelAnalyzer.analyze_batch", rerun_analyze)
    cfg = AppConfig(
        api=APIConfig(api_key="k", base_url="http://127.0.0.1:9999/v1", model="gpt-4o", max_tokens=65536),
        analysis=AnalysisConfig(concurrency=1, block_size=4, failed_block_retry_rounds=0),
        working_directory=str(book_dir),
    )
    summary = await AnalysisPipeline(
        config=cfg,
        directory=blocks_dir,
        knowledge_file=book_dir / "kb.json",
        target_chapters=[2, 4],
    ).run()

    # 1. break_on_gap 拆成了 [2] 和 [4]，且第 2 章只看到第 1 章（不含旧第 2 章、不含第 3/4 章与未来 rolling）
    assert seen_prompts[2]["batch"] == [2]
    assert seen_prompts[2]["facts"] == ["旧事件_1"]
    assert seen_prompts[2]["world"] == ["旧设定_1"]
    assert seen_prompts[2]["rolling"] == {}
    assert seen_prompts[4]["batch"] == [4]
    assert "新事件_2" in seen_prompts[4]["facts"] and "旧事件_2" not in seen_prompts[4]["facts"]

    # 2. 第 2 章已覆写新结果，第 4 章失败但完整保留旧结果且不写 chapter_4_failed.json
    assert summary["total_analyzed"] == 1
    assert summary["failed_chapters"] == [4]
    ch2_disk = json.loads((out_dir / "chapter_2_result.json").read_text(encoding="utf-8"))
    ch4_disk = json.loads((out_dir / "chapter_4_result.json").read_text(encoding="utf-8"))
    assert ch2_disk["core_events"][0]["event"] == "新事件_2"
    assert ch4_disk["core_events"][0]["event"] == "旧事件_4"
    assert not (out_dir / "chapter_4_failed.json").exists()

    # 3. rolling_summary.json 默认未改写，knowledge.json 与 build_temp_knowledge 无重复条目
    assert json.loads((out_dir / "rolling_summary.json").read_text(encoding="utf-8"))["last_updated_chapter"] == 4
    kb_disk = json.loads((book_dir / "kb.json").read_text(encoding="utf-8"))
    assert "新事件_2" in kb_disk["verified_facts"]
    assert "旧事件_2" not in kb_disk["verified_facts"]
    assert "旧事件_4" in kb_disk["verified_facts"]
    rebuilt_temp = KnowledgeBaseManager.build_temp_knowledge(out_dir, single_chapter_only=True)
    assert "新事件_2" in rebuilt_temp.verified_facts
    assert "旧事件_2" not in rebuilt_temp.verified_facts
    KnowledgeBaseManager.invalidate_cache(out_dir)
