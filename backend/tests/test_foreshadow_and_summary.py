"""伏笔锚点校验、伏笔账本状态机与全书四阶段总结断点续跑测试。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.config.settings import AppConfig
from backend.core.anchor import build_chapter_spans, locate_anchor, verify_result_anchors
from backend.models.analysis_result import AnalysisResult, Foreshadowing
from backend.services.final_summary import FinalSummaryRunner
from backend.utils.foreshadow_ledger import DORMANT_CHAPTER_THRESHOLD, ForeshadowItem, ForeshadowLedger

BLOCK = (
    "第一章 起\n"
    "方醒睁眼，先摸了摸怀里的铜匣。\n"
    "第二章 承\n"
    "铜匣打开，里面只有一张旧契。\n"
    "纸角已经发脆，字迹却清楚。\n"
)


def test_anchor_location_and_verification():
    spans = build_chapter_spans(BLOCK, 1)
    assert [c for c, _s, _e in spans] == [1, 2]

    hit = locate_anchor(BLOCK, "铜匣打开，里面只有一张旧契。")
    assert hit is not None and hit[2] == "EXACT"
    assert locate_anchor(BLOCK, "铜匣打开") is None  # 过短
    assert locate_anchor(BLOCK, "原文根本没这句话。") is None

    res = AnalysisResult(
        chapter_number=1,
        foreshadowing=[
            Foreshadowing(clue="真引文", anchor_quote="铜匣打开，里面只有一张旧契。"),
            Foreshadowing(clue="伪造引文", anchor_quote="方醒其实是前朝太子，血书为证。"),
            Foreshadowing(clue="跨章拼接", anchor_quote="先摸了摸怀里的铜匣。\n第二章 承\n铜匣打开"),
        ],
    )
    stats = verify_result_anchors(res, BLOCK, 1)
    assert stats["located"] == 1 and stats["missing"] == 2
    assert res.foreshadowing[0].anchor_chapter == 2
    assert res.foreshadowing[1].anchor_quote == ""
    assert res.foreshadowing[2].anchor_quote == ""


def test_foreshadow_ledger_dormant_and_reconciliation():
    def _mk(fid: str, first_seen: int, last_seen: int, status: str = "active") -> ForeshadowItem:
        return ForeshadowItem(
            id=fid, description=f"伏笔{fid}", first_seen_chapter=first_seen,
            first_seen_batch=0, last_seen_chapter=last_seen, last_seen_batch=0,
            status=status, evidence_chapters=[first_seen],
        )

    ledger = ForeshadowLedger()
    done_item = _mk("fs_done", 5, 900, status="resolved")
    done_item.resolution = "V2证据结论"
    done_item.resolved_chapter = 900
    done_item.needs_review = False
    ledger.items.extend([
        _mk("fs_old", 10, 100),
        _mk("fs_recent", 800, 850),
        done_item,
    ])
    ledger.reconcile_and_update(batch_idx=12, current_chapter=100 + DORMANT_CHAPTER_THRESHOLD + 10)
    statuses = {i.id: i.status for i in ledger.items}
    assert statuses == {"fs_old": "dormant", "fs_recent": "active", "fs_done": "resolved"}

    runner = FinalSummaryRunner.__new__(FinalSummaryRunner)
    runner.ledger = ledger
    payload = {
        "reconciliation": [
            {
                "foreshadow_id": "fs_recent", "is_resolved_in_this_batch": True,
                "resolved_chapter": 860, "resolution_summary": "真相揭晓",
            },
            {
                "foreshadow_id": "fs_done", "is_resolved_in_this_batch": True,
                "resolved_chapter": 860, "resolution_summary": "过期阶段1自述",
            },
        ],
    }
    runner._apply_reconciliation(3, 851, 880, payload)
    runner._apply_reconciliation(3, 851, 880, payload)
    item = next(i for i in ledger.items if i.id == "fs_recent")
    assert item.status == "active" and item.needs_review is True
    assert item.resolution == "真相揭晓" and item.resolved_chapter == 860
    assert sum(1 for n in item.notes if "批次3" in n) == 1

    # 已回收条目不得被断点重放的阶段 1 自述污染
    assert done_item.status == "resolved"
    assert done_item.needs_review is False
    assert done_item.resolution == "V2证据结论"
    assert done_item.resolved_chapter == 900


async def test_final_summary_checkpoint_and_resume(tmp_path):
    for ch in range(1, 5):
        (tmp_path / f"chapter_{ch}_result.json").write_text(json.dumps({
            "chapter_number": ch,
            "cross_block": {"summary": f"第{ch}章摘要内容", "unresolved_questions": [], "new_leads": []},
            "core_events": [], "character_arcs": [], "foreshadowing": [], "plot_holes": [],
            "long_context_insights": {}, "updated_knowledge": {},
        }, ensure_ascii=False), encoding="utf-8")

    def _runner(bs: int = 2) -> FinalSummaryRunner:
        return FinalSummaryRunner(
            config=AppConfig(), output_dir=tmp_path, start_chapter=1, end_chapter=4,
            batch_size=bs, concurrency=2, on_progress=lambda p: None, on_token_stats=lambda p: None,
        )

    r1 = _runner(2)
    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=AsyncMock(return_value="卷摘要正文")), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})), \
         patch.object(FinalSummaryRunner, "_run_atlas_extraction", new=AsyncMock(return_value=None)), \
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="最终报告正文")):
        report = await r1.run()
    assert report and "最终报告" in report
    assert (tmp_path / "final_summary_checkpoint" / "volume_1.md").exists()

    # 第二次重跑应命中 checkpoint，不重调卷摘要与 reconciliation LLM
    r2 = _runner(2)
    sum_mock = AsyncMock(return_value="不应调用")
    recon_mock = AsyncMock(return_value={"reconciliation": []})
    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=sum_mock), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=recon_mock), \
         patch.object(FinalSummaryRunner, "_run_atlas_extraction", new=AsyncMock(return_value=None)), \
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="重跑最终报告")):
        report2 = await r2.run()
    assert "重跑最终报告" in report2
    assert sum_mock.call_count == 0 and recon_mock.call_count == 0


async def test_final_summary_stop_cancels_dedicated_recheck_client_and_stops_waves(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from backend.config.settings import AnalysisConfig, APIConfig

    blocks_dir = tmp_path / "blocks"
    out_dir = tmp_path / "output"
    blocks_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    (blocks_dir / "0001.txt").write_text("第一章 方醒打开铜匣，底下那层是空的。\n", encoding="utf-8")

    ledger = ForeshadowLedger()
    for i in range(6):
        ledger.items.append(
            ForeshadowItem(
                id=f"fs_{i:03d}",
                description=f"伏笔{i}",
                first_seen_chapter=1,
                first_seen_batch=0,
                last_seen_chapter=1,
                last_seen_batch=0,
                status="active",
                anchor_quote="方醒打开铜匣，底下那层是空的。",
                anchor_chapter=1,
                question="何人放的",
                answer_criteria="交代身份",
            )
        )
    ledger_path = out_dir / "foreshadow_ledger.json"
    ledger.save(ledger_path)

    cfg = AppConfig(
        api=APIConfig(base_url="https://api.example.com/v1", api_key="k", model="main-m"),
        analysis=AnalysisConfig(recheck_model="recheck-m", recheck_v2_batch_size=2, recheck_concurrency=1),
        working_directory=str(tmp_path),
    )
    runner = FinalSummaryRunner(
        config=cfg, output_dir=out_dir, start_chapter=1, end_chapter=1,
        batch_size=1, concurrency=1, ledger_path=ledger_path,
    )

    clients_seen = []

    async def fake_chat_auto(self, messages, max_tokens=None):
        clients_seen.append(self)
        runner.stop()
        return SimpleNamespace(success=False, error="用户请求停止", content="", tokens=(0, 0))

    monkeypatch.setattr("backend.core.llm_client.LLMClient.chat_auto", fake_chat_auto)
    await runner._run_recheck_v2([])

    assert len(clients_seen) == 1
    assert clients_seen[0] is not runner._llm
    assert clients_seen[0]._stop_event.is_set() is True
    assert runner._recheck_client is None
    assert runner.last_recheck_summary["status"] == "STOPPED"


def test_b4_anchor_expected_range_filters_false_heading():
    block_text = (
        "第八百三十一章 旧档重开\n"
        "他翻开那本泛黄的笔记，只见扉页写着：\n"
        "第一章 创世之初\n"
        "那枚青铜古印静静躺在祭坛中央。\n"
        "第八百三十二章 祭坛异变\n"
        "古印忽然发出刺目的红光。\n"
    )
    spans_legacy = build_chapter_spans(block_text, 831)
    assert any(ch == 1 for ch, _s, _e in spans_legacy)

    spans_fixed = build_chapter_spans(block_text, 831, expected_range=(831, 834))
    assert [ch for ch, _s, _e in spans_fixed] == [831, 832]

    res = SimpleNamespace(
        foreshadowing=[
            SimpleNamespace(anchor_quote="那枚青铜古印静静躺在祭坛中央。", anchor_chapter=0)
        ]
    )
    verify_result_anchors(res, block_text, 831, expected_range=(831, 834))
    assert res.foreshadowing[0].anchor_chapter == 831


def _write_chapter_with_foreshadow(out_dir: Path, ch: int, fs_id_clue: str | None = None) -> None:
    foreshadowing = []
    if fs_id_clue:
        foreshadowing.append({
            "clue": fs_id_clue,
            "type": "身世",
            "importance": "高",
            "confidence": "高",
            "anchor_quote": "方醒打开铜匣，底下那层是空的。",
            "anchor_chapter": ch,
            "question": "铜匣底层藏着什么",
            "answer_criteria": "交代暗格内容",
        })
    (out_dir / f"chapter_{ch}_result.json").write_text(json.dumps({
        "chapter_number": ch,
        "cross_block": {"summary": f"第{ch}章摘要内容", "unresolved_questions": [], "new_leads": []},
        "core_events": [],
        "character_arcs": [],
        "foreshadowing": foreshadowing,
        "plot_holes": [],
        "long_context_insights": {},
        "updated_knowledge": {},
    }, ensure_ascii=False), encoding="utf-8")


async def test_stage1_clean_ledger_isolation_and_human_override(tmp_path):
    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True)
    _write_chapter_with_foreshadow(out_dir, 1, "铜匣底层似乎藏有夹层暗格与密信")

    # 预先写入脏账本：包含一条旧规则留下的 stale 条目 + 一条人工覆写条目
    dirty = ForeshadowLedger()
    dirty.items.append(ForeshadowItem(
        id="fs_stale_dirty", description="上次残余脏伏笔",
        first_seen_chapter=1, first_seen_batch=0, last_seen_chapter=1, last_seen_batch=0,
        status="resolved", resolution="旧残留结论", resolved_chapter=99, source_type="catalog_scan",
    ))
    dirty.items.append(ForeshadowItem(
        id="fs_manual", description="人工标定伏笔",
        first_seen_chapter=1, first_seen_batch=0, last_seen_chapter=1, last_seen_batch=0,
        status="resolved", resolution="人工终裁", resolved_chapter=2, source_type="human_override",
    ))
    ledger_path = out_dir / "foreshadow_ledger.json"
    dirty.save(ledger_path)

    runner = FinalSummaryRunner(
        config=AppConfig(), output_dir=out_dir, start_chapter=1, end_chapter=1,
        batch_size=1, concurrency=1, ledger_path=ledger_path,
    )
    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=AsyncMock(return_value="卷摘要")), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})), \
         patch.object(FinalSummaryRunner, "_run_atlas_extraction", new=AsyncMock(return_value=None)), \
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="最终报告")):
        report = await runner.run()

    assert report == "最终报告"
    ids = {i.id for i in runner.ledger.items}
    assert "fs_stale_dirty" not in ids
    assert "fs_manual" in ids
    assert any(i.source_type == "catalog_scan" and i.status == "active" for i in runner.ledger.items)


async def test_stage2_and_stage3_checkpoints_and_wipe_preservation(tmp_path):
    from backend.config.settings import AnalysisConfig, APIConfig
    from backend.services.recheck_v2 import RecheckOutcome

    blocks_dir = tmp_path / "blocks"
    out_dir = tmp_path / "output"
    blocks_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    (blocks_dir / "0001.txt").write_text("第一章 方醒打开铜匣，底下那层是空的。\n", encoding="utf-8")
    _write_chapter_with_foreshadow(out_dir, 1, "铜匣底层似乎藏有夹层暗格与密信")

    cfg = AppConfig(
        api=APIConfig(base_url="https://api.example.com/v1", api_key="k", model="main-m"),
        analysis=AnalysisConfig(recheck_version="v3"),
        working_directory=str(tmp_path),
    )

    events_seen: list[dict] = []

    def _mk_runner() -> FinalSummaryRunner:
        return FinalSummaryRunner(
            config=cfg, output_dir=out_dir, start_chapter=1, end_chapter=1,
            batch_size=1, concurrency=1, on_progress=events_seen.append,
        )

    partial_outcome = RecheckOutcome(
        calls=2,
        status="PARTIAL",
        status_reason="预算用尽",
        not_checked=["fs_001"],
        coverage={"checked": 0, "candidates_total": 1, "not_checked": 1},
    )
    complete_outcome = RecheckOutcome(
        calls=3,
        status="COMPLETED",
        not_checked=[],
        coverage={
            "checked": 1, "candidates_total": 1, "supported": 1,
            "v3_tiers": {"RESOLVED_EXPLICIT": 1, "RESOLVED_IMPLICIT": 0, "PARTIALLY_RESOLVED": 0, "UNRESOLVED": 0},
        },
    )

    bad_style = {"statistical": {"avg_sentence_length": 12.0}, "semantic": {"signature_expressions": "（提取失败）"}}
    good_style = {
        "statistical": {"avg_sentence_length": 14.5},
        "semantic": {"signature_expressions": "短句冷峻", "narrative_rhythm": "紧凑"},
    }

    recheck_mock = AsyncMock(return_value=partial_outcome)
    style_mock = AsyncMock(return_value=bad_style)

    # Run 1：Stage 2 PARTIAL 与 Stage 3 含「提取失败」均不写断点
    r1 = _mk_runner()
    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=AsyncMock(return_value="卷摘要")), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})), \
         patch.object(FinalSummaryRunner, "_run_atlas_extraction", new=AsyncMock(return_value=None)), \
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="报告1")), \
         patch("backend.services.recheck_v3.run_recheck_v3", new=recheck_mock), \
         patch("backend.core.style_analyzer.extract_style_profile", new=style_mock):
        await r1.run()

    cp_dir = out_dir / "final_summary_checkpoint"
    assert (cp_dir / "stage1_ledger_baseline.json").exists()
    assert not (cp_dir / "stage2_recheck_state.json").exists()
    assert not (cp_dir / "style_profile.json").exists()

    # Run 2：Stage 2 COMPLETED + 有效 style 落盘
    recheck_mock2 = AsyncMock(return_value=complete_outcome)
    style_mock2 = AsyncMock(return_value=good_style)
    r2 = _mk_runner()
    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=AsyncMock(return_value="不应调用")), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})), \
         patch.object(FinalSummaryRunner, "_run_atlas_extraction", new=AsyncMock(return_value=None)), \
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="报告2")), \
         patch("backend.services.recheck_v3.run_recheck_v3", new=recheck_mock2), \
         patch("backend.core.style_analyzer.extract_style_profile", new=style_mock2):
        await r2.run()

    assert recheck_mock2.await_count == 1
    assert style_mock2.await_count == 1
    assert (cp_dir / "stage2_recheck_state.json").exists()
    assert (cp_dir / "style_profile.json").exists()

    # Run 3：Stage 1/2/3 全部命中断点跳过 LLM，且命中时发出 recheck_done + ledger_updated 事件
    events_seen.clear()
    recheck_mock3 = AsyncMock(return_value=complete_outcome)
    style_mock3 = AsyncMock(return_value=good_style)
    r3 = _mk_runner()
    acquire_orig = r3._acquire_llm_slot
    acquire_style_calls = 0

    async def spy_acquire():
        nonlocal acquire_style_calls
        acquire_style_calls += 1
        return await acquire_orig()

    r3._acquire_llm_slot = spy_acquire  # type: ignore[method-assign]
    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=AsyncMock(return_value="不应调用")), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})), \
         patch.object(FinalSummaryRunner, "_run_atlas_extraction", new=AsyncMock(return_value=None)), \
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="报告3")), \
         patch("backend.services.recheck_v3.run_recheck_v3", new=recheck_mock3), \
         patch("backend.core.style_analyzer.extract_style_profile", new=style_mock3):
        rep3 = await r3.run()

    assert rep3 == "报告3"
    assert recheck_mock3.await_count == 0
    assert style_mock3.await_count == 0
    # 唯一占槽应仅为 _call_llm_final（若未打桩），因 _call_llm_final 被打桩故 acquire_style_calls == 0（证明 style 命中未占槽）
    assert acquire_style_calls == 0
    etypes = [e.get("type") for e in events_seen]
    assert "recheck_done" in etypes and "ledger_updated" in etypes

    # _wipe_checkpoint_dir 清理 Stage 1/2 断点但保留 style_profile.json 与 recheck_v3_u1_cache.json
    (cp_dir / "recheck_v3_u1_cache.json").write_text("{}", encoding="utf-8")
    r3._wipe_checkpoint_dir()
    assert (cp_dir / "style_profile.json").exists()
    assert (cp_dir / "recheck_v3_u1_cache.json").exists()
    assert not (cp_dir / "manifest.json").exists()
    assert not (cp_dir / "stage1_ledger_baseline.json").exists()
    assert not (cp_dir / "stage2_recheck_state.json").exists()


async def test_stage4_atlas_concurrent_dual_fingerprint_and_isolation(tmp_path):
    """验证 Stage 4 并发全书图谱提取、双子指纹缓存、局部重试、atlas token 记账与异常隔离。"""
    import json
    from unittest.mock import AsyncMock, patch
    from backend.config.constants import STORY_ATLAS_FILE
    from backend.config.settings import AnalysisConfig, APIConfig, AppConfig
    from backend.services.final_summary import ATLAS_CHAR_SYSTEM_PROMPT, FinalSummaryRunner

    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True)
    for ch in (1, 2):
        (out_dir / f"chapter_{ch}_result.json").write_text(
            json.dumps({
                "chapter_number": ch,
                "cross_block": {"summary": f"第{ch}章摘要", "unresolved_questions": [], "new_leads": []},
                "core_events": [{"id": f"e{ch}", "event": "主角破局", "characters": "李火旺", "function": "推进", "importance": "高"}],
                "character_arcs": [],
                "foreshadowing": [], "plot_holes": [], "locations": [], "spatial_relationships": [],
                "long_context_insights": {}, "updated_knowledge": {},
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    cfg = AppConfig()
    cfg.api = APIConfig(base_url="http://127.0.0.1:9", api_key="k", model="m-atlas")
    cfg.analysis = AnalysisConfig(summary_batch_size=2, summary_concurrency=2)

    valid_char = json.dumps({
        "characters": [{
            "canonical_name": "李火旺",
            "aliases": ["坐忘道红中"],
            "faction": "清风观",
            "role_tier": "protagonist",
            "first_chapter": 1,
            "identity_summary": "核心主角",
            "arc_milestones": [{"chapter_range": "1-2", "milestone": "初醒"}],
        }],
        "relationships": [],
    }, ensure_ascii=False)
    valid_world = json.dumps({
        "stages": [{"stage_no": 1, "title": "大齐开篇", "start_chapter": 1, "end_chapter": 2, "world_realm": "大齐", "primary_location": "清风观"}],
        "world_tree": [{"name": "大齐", "node_type": "world", "summary": "主界", "children": []}],
    }, ensure_ascii=False)

    # 1. 首轮：char 成功 + world 返回畸形 JSON -> 仅锁定 char_atlas_fp，world_atlas_fp 留空
    calls_1: list[str] = []

    async def atlas_part_partial_fail(self, sys_p: str, _user_p: str):
        self._record_tokens("atlas", (10, 5))
        if sys_p == ATLAS_CHAR_SYSTEM_PROMPT:
            calls_1.append("char")
            return valid_char
        calls_1.append("world")
        return "{not-valid-json"

    r1 = FinalSummaryRunner(cfg, out_dir, 1, 2, batch_size=2, concurrency=2)
    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=AsyncMock(return_value="卷摘要正文")), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})), \
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="最终报告")), \
         patch.object(FinalSummaryRunner, "_call_llm_atlas_part", new=atlas_part_partial_fail):
        rep1 = await r1.run()

    assert rep1 == "最终报告"
    assert sorted(calls_1) == ["char", "world"]
    assert r1.get_token_stats()["atlas"] == {"input_tokens": 20, "output_tokens": 10, "model": "m-atlas"}
    atlas_data = json.loads((out_dir / STORY_ATLAS_FILE).read_text(encoding="utf-8"))
    assert atlas_data["char_atlas_fp"] and atlas_data["world_atlas_fp"] == ""
    assert len(atlas_data["characters"]) == 1

    # 2. 局部重跑：仅补跑未锁定的 world，char 命中缓存不再调用
    calls_2: list[str] = []

    async def atlas_part_retry_world(self, sys_p: str, _user_p: str):
        self._record_tokens("atlas", (15, 8))
        if sys_p == ATLAS_CHAR_SYSTEM_PROMPT:
            calls_2.append("char")
            return valid_char
        calls_2.append("world")
        return valid_world

    r2 = FinalSummaryRunner(cfg, out_dir, 1, 2, batch_size=2, concurrency=2)
    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=AsyncMock(return_value="卷摘要正文")), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})), \
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="最终报告")), \
         patch.object(FinalSummaryRunner, "_call_llm_atlas_part", new=atlas_part_retry_world):
        await r2.run()

    assert calls_2 == ["world"]
    atlas_data_2 = json.loads((out_dir / STORY_ATLAS_FILE).read_text(encoding="utf-8"))
    assert atlas_data_2["char_atlas_fp"] and atlas_data_2["world_atlas_fp"]
    assert len(atlas_data_2["stages"]) == 1 and len(atlas_data_2["world_tree"]) == 1

    # 3. 双子指纹均已锁定 -> 重跑 0 次图谱 LLM 调用
    calls_3: list[str] = []

    async def atlas_part_should_skip(self, sys_p: str, _user_p: str):
        calls_3.append(sys_p)
        return valid_char

    r3 = FinalSummaryRunner(cfg, out_dir, 1, 2, batch_size=2, concurrency=2)
    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=AsyncMock(return_value="卷摘要正文")), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})), \
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="最终报告")), \
         patch.object(FinalSummaryRunner, "_call_llm_atlas_part", new=atlas_part_should_skip):
        await r3.run()
    assert calls_3 == []

    # 4. 异常隔离：图谱子任务抛异常时不阻塞最终报告返回
    (out_dir / STORY_ATLAS_FILE).unlink()

    async def atlas_part_boom(self, _sys_p: str, _user_p: str):
        raise RuntimeError("图谱请求超时")

    r4 = FinalSummaryRunner(cfg, out_dir, 1, 2, batch_size=2, concurrency=2)
    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=AsyncMock(return_value="卷摘要正文")), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})), \
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="最终报告降级成功")), \
         patch.object(FinalSummaryRunner, "_call_llm_atlas_part", new=atlas_part_boom):
        rep4 = await r4.run()
    assert rep4 == "最终报告降级成功"


async def test_final_summary_runner_emits_real_lanes_and_tracks_inflight(tmp_path):
    """验证总结流水线推送 lane_start/lane_done 与分阶段 concurrency，并维护在途车道与两参图谱兼容。"""
    from backend.common.error_types import LLMResult, UsageStatus
    from backend.config.constants import STORY_ATLAS_FILE
    from backend.config.settings import AnalysisConfig, APIConfig
    from backend.core.llm_client import LLMClient
    from backend.core.llm_stream import StreamChunk
    from backend.services.recheck_v2 import RecheckOutcome

    blocks_dir = tmp_path / "blocks"
    out_dir = tmp_path / "output"
    blocks_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    (blocks_dir / "0001.txt").write_text("第一章 方醒打开铜匣，底下那层是空的。\n", encoding="utf-8")
    _write_chapter_with_foreshadow(out_dir, 1, "铜匣底层似乎藏有夹层暗格与密信")

    cfg = AppConfig(
        api=APIConfig(base_url="https://api.example.com/v1", api_key="k", model="main-m"),
        analysis=AnalysisConfig(recheck_version="v3", summary_concurrency=2, recheck_concurrency=6),
        working_directory=str(tmp_path),
    )

    events: list[dict] = []
    inflight_snapshots: list[tuple[int, list[dict]]] = []

    runner = FinalSummaryRunner(
        config=cfg,
        output_dir=out_dir,
        start_chapter=1,
        end_chapter=1,
        batch_size=1,
        concurrency=2,
        on_progress=events.append,
    )

    async def fake_chat_auto(self, messages, max_tokens=None, on_progress=None, **kwargs):
        inflight_snapshots.append((runner._active_concurrency, runner.get_inflight_lanes()))
        if on_progress is not None:
            await on_progress(StreamChunk(type="content", text="x", estimated_total_tokens=12))
        payload = json.dumps({
            "reconciliation": [],
            "characters": [],
            "relationships": [],
            "stages": [],
            "world_tree": [],
        }, ensure_ascii=False)
        return LLMResult(
            success=True,
            content=payload,
            error="",
            tokens=(10, 12),
            usage_status=UsageStatus.KNOWN,
        )

    async def fake_run_recheck_v3(*args, **kwargs):
        call_model = kwargs.get("call_model")
        if callable(call_model):
            await call_model("check")
        return RecheckOutcome(calls=1, status="COMPLETED", not_checked=[], coverage={"checked": 1, "candidates_total": 1})

    style_stub = AsyncMock(return_value={
        "statistical": {"avg_sentence_length": 12.0},
        "semantic": {"signature_expressions": "克制白描"},
    })

    two_arg_calls = 0

    async def two_arg_atlas_part(self, system_prompt: str, user_prompt: str) -> str:
        nonlocal two_arg_calls
        assert isinstance(self, FinalSummaryRunner)
        assert isinstance(system_prompt, str) and isinstance(user_prompt, str)
        two_arg_calls += 1
        return json.dumps({"characters": [], "relationships": [], "stages": [], "world_tree": []}, ensure_ascii=False)

    with patch.object(LLMClient, "chat_auto", new=fake_chat_auto), \
         patch("backend.core.style_analyzer.extract_style_profile", new=style_stub), \
         patch("backend.services.recheck_v3.run_recheck_v3", new=fake_run_recheck_v3):
        report = await runner.run()

    assert report
    assert runner.get_inflight_lanes() == []

    starts = [e for e in events if e.get("type") == "lane_start"]
    dones = [e for e in events if e.get("type") == "lane_done"]
    phases = [e for e in events if e.get("type") == "phase"]
    started_ids = {int(e.get("lane_id", 0)) for e in starts}
    done_ids = {int(e.get("lane_id", 0)) for e in dones}

    assert started_ids <= done_ids
    assert any(p.get("phase") == "batch" and p.get("concurrency") == 2 and p.get("real_lanes") is True for p in phases)
    assert any(p.get("phase") == "recheck" and p.get("concurrency") == 6 and p.get("real_lanes") is True for p in phases)
    assert any(p.get("phase") == "report" and p.get("concurrency") == 2 and p.get("real_lanes") is True for p in phases)
    assert any(900001 <= lid < 905000 for lid in started_ids)
    assert 910001 in started_ids
    assert 920001 in started_ids
    assert 930001 in started_ids
    assert 930002 in started_ids
    assert 930003 in started_ids
    assert any(conc == 2 and len(lanes) >= 1 for conc, lanes in inflight_snapshots)
    assert any(conc == 6 and any(int(l.get("id", 0)) == 910001 for l in lanes) for conc, lanes in inflight_snapshots)

    # 验证 _call_llm_atlas_part 维持 (self, system_prompt, user_prompt) 两参签名兼容外部打桩
    atlas_file = out_dir / STORY_ATLAS_FILE
    if atlas_file.exists():
        atlas_file.unlink()
    runner2 = FinalSummaryRunner(
        config=cfg, output_dir=out_dir, start_chapter=1, end_chapter=1, batch_size=1, concurrency=2,
    )
    with patch.object(LLMClient, "chat_auto", new=fake_chat_auto), \
         patch.object(FinalSummaryRunner, "_call_llm_atlas_part", new=two_arg_atlas_part):
        await runner2._run_atlas_extraction(
            volume_summaries=["摘要"],
            batch_tasks=[(1, 1, 1, "prompt")],
            stage1_fp="fp-test",
        )
    assert two_arg_calls == 2
    assert runner2.get_inflight_lanes() == []


def test_summary_runner_concurrency_and_batch_size_fallback_to_main(tmp_path):
    """summary_concurrency=0 与 batch_size=0 时，FinalSummaryRunner 自动回退至 analysis.concurrency 与 summary_batch_size。"""
    from backend.config.settings import AnalysisConfig, APIConfig

    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True)
    cfg = AppConfig(
        api=APIConfig(base_url="https://api.example.com/v1", api_key="k", model="m"),
        analysis=AnalysisConfig(concurrency=6, summary_concurrency=0, summary_batch_size=42),
        working_directory=str(tmp_path),
    )
    runner = FinalSummaryRunner(
        config=cfg,
        output_dir=out_dir,
        start_chapter=1,
        end_chapter=10,
        batch_size=0,
        concurrency=0,
    )
    assert runner.concurrency == 6
    assert runner.batch_size == 42

    cfg_override = AppConfig(
        api=APIConfig(base_url="https://api.example.com/v1", api_key="k", model="m"),
        analysis=AnalysisConfig(concurrency=6, summary_concurrency=3, summary_batch_size=42),
        working_directory=str(tmp_path),
    )
    runner_override = FinalSummaryRunner(
        config=cfg_override,
        output_dir=out_dir,
        start_chapter=1,
        end_chapter=10,
        batch_size=None,
        concurrency=None,
    )
    assert runner_override.concurrency == 3
    assert runner_override.batch_size == 42


async def test_summary_service_tracks_stage1_and_recheck_progress(tmp_path):
    """SummaryService 在 status() 与 WS 广播中跟踪分卷/调和计数和复检子进度，recheck_done 后清空子进度。"""
    from unittest.mock import MagicMock
    from backend.services.summary_service import SummaryService

    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True)
    (out_dir / "chapter_1_result.json").write_text('{"chapter_number": 1}', encoding="utf-8")

    published: list[dict] = []
    status_snapshots: list[dict] = []

    class FakeHub:
        async def publish(self, msg):
            published.append(msg)

        async def log(self, *_args, **_kwargs):
            return None

        async def state_change(self, *_args, **_kwargs):
            return None

    fake_analysis = MagicMock()
    fake_analysis.is_running = False
    fake_analysis.config_manager.load.return_value = AppConfig()

    svc = SummaryService()

    class FakeRunner:
        def __init__(self, *, on_progress, **_kwargs):
            self._on_progress = on_progress
            self._active_concurrency = 2
            self.last_recheck_summary = {}

        def get_inflight_lanes(self):
            return []

        def get_token_stats(self):
            return {}

        async def run(self):
            self._on_progress({"type": "phase", "phase": "batch", "total_batches": 3, "message": "[阶段1/4]"})
            self._on_progress({"type": "stage1_progress", "summary_done": 2, "recon_done": 1, "total_batches": 3})
            status_snapshots.append(svc.status())
            self._on_progress({"type": "phase", "phase": "recheck", "concurrency": 4, "message": "[阶段2/4]"})
            self._on_progress({
                "type": "recheck_progress",
                "recheck_progress": {
                    "engine": "v3",
                    "stage": "u3_hop",
                    "hop": 1,
                    "stage_done": 4,
                    "stage_total": 10,
                    "remaining": 6,
                    "items_total": 10,
                    "checked": 4,
                    "calls": 2,
                    "unit": "条",
                },
                "message": "",
            })
            status_snapshots.append(svc.status())
            self.last_recheck_summary = {"status": "COMPLETED", "calls": 3, "coverage": {"checked": 10, "candidates_total": 10}}
            self._on_progress({"type": "recheck_done", "recheck": self.last_recheck_summary, "message": "完成"})
            status_snapshots.append(svc.status())
            self._on_progress({"type": "complete", "elapsed": 1.0, "message": "ok"})
            return "# 最终报告"

    with patch("backend.services.summary_service.book_service.get_output_dir", return_value=out_dir), \
         patch("backend.services.summary_service.get_analysis_service", return_value=fake_analysis), \
         patch("backend.services.summary_service.get_hub", return_value=FakeHub()), \
         patch("backend.services.summary_service.FinalSummaryRunner", FakeRunner):
        svc.start("Book1", 1, 10, batch_size=5, concurrency=2)
        assert svc._task is not None
        await svc._task

    s_batch, s_recheck, s_after_done = status_snapshots
    assert s_batch["summary_done"] == 2 and s_batch["recon_done"] == 1 and s_batch["total_batches"] == 3
    assert s_recheck["recheck_progress"]["remaining"] == 6
    assert s_recheck["recheck_progress"]["stage_total"] == 10
    assert s_after_done["recheck_progress"] == {}
    assert any(
        m.get("type") == "summary_progress"
        and m["payload"].get("summary_done") == 2
        and m["payload"].get("recon_done") == 1
        for m in published
    )
    assert any(
        m.get("type") == "summary_progress"
        and m["payload"].get("recheck_progress", {}).get("remaining") == 6
        for m in published
    )


async def test_volume_summary_refusal_guard_and_checkpoint_rejection(tmp_path):
    """分卷摘要拒答校验拦截缺正文幻觉回复，且 _load_checkpoint 跳过已污染的卷摘要断点。"""
    from backend.services.final_summary import build_volume_summary_retry_messages, validate_volume_summary

    refusal_text = (
        "你只提供了“分析要求”和“字段说明”，但没有提供要分析的具体小说章节正文（例如第1章内容）。"
        "因此我无法进行“单章分析”。\n\n请补充以下任一种输入：\n1. 需要分析的单章完整正文"
    )
    assert validate_volume_summary(refusal_text)[0] is False
    assert validate_volume_summary("   ")[0] is False
    assert validate_volume_summary("卷摘要正文")[0] is True

    retried = build_volume_summary_retry_messages(
        "检测到模型拒答", [{"role": "user", "content": "请撰写卷摘要"}]
    )
    assert "结构化分析 JSON" in retried[-1]["content"]

    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True)
    for ch in (1, 2):
        (out_dir / f"chapter_{ch}_result.json").write_text(
            json.dumps({
                "chapter_number": ch,
                "cross_block": {"summary": f"第{ch}章摘要", "unresolved_questions": [], "new_leads": []},
                "core_events": [],
                "character_arcs": [],
                "foreshadowing": [],
                "plot_holes": [],
                "long_context_insights": {},
                "updated_knowledge": {},
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    runner = FinalSummaryRunner(
        config=AppConfig(), output_dir=out_dir, start_chapter=1, end_chapter=2, batch_size=1, concurrency=1
    )
    runner._load_results = lambda: [{"ch": 1}, {"ch": 2}]  # type: ignore[method-assign]

    async def summary_mock(self, prompt: str, batch_idx: int = 0) -> str:
        return refusal_text if batch_idx == 1 else "第2卷正常剧情摘要。"

    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=summary_mock), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})), \
         patch.object(FinalSummaryRunner, "_run_atlas_extraction", new=AsyncMock(return_value=None)), \
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="最终报告")):
        report = await runner.run()

    assert report == "最终报告"
    cp_dir = out_dir / "final_summary_checkpoint"
    assert not (cp_dir / "volume_1.md").exists()
    assert (cp_dir / "volume_2.md").exists()

    # 若磁盘上遗留已被拒答污染的 volume_1.md，断点加载必须丢弃它并在续跑时重生成
    (cp_dir / "volume_1.md").write_text(refusal_text, encoding="utf-8")
    manifest_p = cp_dir / "manifest.json"
    m_data = json.loads(manifest_p.read_text(encoding="utf-8"))
    m_data["batches"]["1"] = {"start": 1, "end": 1, "summary": "volume_1.md", "recon": "recon_2.json"}
    manifest_p.write_text(json.dumps(m_data, ensure_ascii=False), encoding="utf-8")

    runner2 = FinalSummaryRunner(
        config=AppConfig(), output_dir=out_dir, start_chapter=1, end_chapter=2, batch_size=1, concurrency=1
    )
    vols_loaded, _ = await runner2._load_checkpoint([(1, 1, 1, "p1"), (2, 2, 2, "p2")])
    assert 1 not in vols_loaded
    assert vols_loaded[2] == "第2卷正常剧情摘要。"


def test_stage2_fingerprint_reacts_to_v3_switches(tmp_path):
    """切换 recheck_v3_gray_u4 / recheck_v3_mid_single_hop 时应改变 V3 Stage 2 指纹。"""
    from backend.config.settings import AnalysisConfig

    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True)
    fps: list[str] = []
    for gray, mid in [(False, False), (True, False), (False, True)]:
        cfg = AppConfig(
            analysis=AnalysisConfig(recheck_version="v3", recheck_v3_gray_u4=gray, recheck_v3_mid_single_hop=mid),
            working_directory=str(tmp_path),
        )
        r = FinalSummaryRunner(config=cfg, output_dir=out_dir, start_chapter=1, end_chapter=1)
        fps.append(r._compute_stage2_fingerprint("s1", "blk", "v3"))
    assert len(set(fps)) == 3


async def test_ledger_apply_human_override_and_recheck_skip():
    """人工复核覆写状态机应正确流转，且 source_type='human_override' 在 V3/V2 中均被跳过。"""
    import pytest
    from backend.services.recheck_v2 import run_recheck_v2
    from backend.services.recheck_v3 import run_recheck_v3

    ledger = ForeshadowLedger(
        items=[
            ForeshadowItem(
                id="fs_h1",
                description="旧玉佩的来历",
                first_seen_chapter=2,
                first_seen_batch=1,
                last_seen_chapter=8,
                last_seen_batch=2,
                status="active",
                needs_review=True,
                anchor_quote="他从怀里摸出一枚残缺旧玉佩。",
                anchor_chapter=2,
                question="旧玉佩究竟属于谁？",
            )
        ]
    )

    confirmed = ledger.apply_human_override(
        "fs_h1", "confirm_resolved", resolution="第12章确认归主角生母所有", resolved_chapter=12, note="人工核验"
    )
    assert confirmed.status == "resolved"
    assert confirmed.resolution_quality == "verified"
    assert confirmed.source_type == "human_override"
    assert confirmed.needs_review is False
    assert confirmed.resolved_chapter == 12
    assert 12 in confirmed.evidence_chapters

    dormant = ledger.apply_human_override("fs_h1", "mark_dormant")
    assert dormant.status == "dormant"
    assert dormant.resolution is None
    assert dormant.resolved_chapter is None

    reopened = ledger.apply_human_override("fs_h1", "reject_to_active")
    assert reopened.status == "active"
    assert reopened.source_type == "human_override"
    assert reopened.needs_review is False

    with pytest.raises(ValueError):
        ledger.apply_human_override("fs_h1", "bad_action")
    with pytest.raises(ValueError):
        ledger.apply_human_override("fs_h1", "confirm_resolved", resolved_chapter=True)
    with pytest.raises(KeyError):
        ledger.apply_human_override("fs_missing", "mark_dormant")

    # 即使 status=='active'，human_override 条目在 V3/V2 复检中也不得再次调用模型或被覆盖
    calls = 0

    async def never_call(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return "[]"

    chapters = {2: "他从怀里摸出一枚残缺旧玉佩。"}
    v3_out = await run_recheck_v3(ledger.items, chapters=chapters, call_model=never_call)
    v2_out = await run_recheck_v2(ledger.items, chapters=chapters, call_model=never_call, max_calls=5)
    assert calls == 0
    assert v3_out.coverage["candidates_total"] == 0
    assert v2_out.coverage["candidates_total"] == 0
    assert reopened.status == "active"
    assert reopened.source_type == "human_override"
