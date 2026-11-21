"""伏笔锚点校验、伏笔账本状态机与全书四阶段总结断点续跑测试。"""
from __future__ import annotations

import json
from pathlib import Path
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
    ledger.items.extend([
        _mk("fs_old", 10, 100),
        _mk("fs_recent", 800, 850),
        _mk("fs_done", 5, 900, status="resolved"),
    ])
    ledger.reconcile_and_update(batch_idx=12, current_chapter=100 + DORMANT_CHAPTER_THRESHOLD + 10)
    statuses = {i.id: i.status for i in ledger.items}
    assert statuses == {"fs_old": "dormant", "fs_recent": "active", "fs_done": "resolved"}

    runner = FinalSummaryRunner.__new__(FinalSummaryRunner)
    runner.ledger = ledger
    runner._apply_reconciliation(3, 851, 880, {
        "reconciliation": [{
            "foreshadow_id": "fs_recent", "is_resolved_in_this_batch": True,
            "resolved_chapter": 860, "resolution_summary": "真相揭晓",
        }],
    })
    item = next(i for i in ledger.items if i.id == "fs_recent")
    assert item.status == "active" and item.needs_review is True
    assert item.resolution == "真相揭晓" and item.resolved_chapter == 860


async def test_final_summary_checkpoint_and_resume(tmp_path):
    for ch in range(1, 5):
        (tmp_path / f"chapter_{ch}_result.json").write_text(json.dumps({
            "chapter_number": ch,
            "cross_block": {"summary": f"第{ch}章摘要内容", "unresolved_questions": [], "new_leads": []},
            "core_events": [], "character_arcs": [], "foreshadowing": [], "plot_holes": [],
            "long_context_insights": {}, "updated_knowledge": {},
        }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "locations_normalized.json").write_text(
        json.dumps({"schema_version": 1, "locations": [], "chapter_mtimes_hash": ""}), encoding="utf-8",
    )
    (tmp_path / "spatial_relationships_normalized.json").write_text(
        json.dumps({"schema_version": 1, "relationships": [], "chapter_mtimes_hash": ""}), encoding="utf-8",
    )

    def _runner(bs: int = 2) -> FinalSummaryRunner:
        return FinalSummaryRunner(
            config=AppConfig(), output_dir=tmp_path, start_chapter=1, end_chapter=4,
            batch_size=bs, concurrency=2, on_progress=lambda p: None, on_token_stats=lambda p: None,
        )

    r1 = _runner(2)
    with patch.object(FinalSummaryRunner, "_call_llm_summary", new=AsyncMock(return_value="卷摘要正文")), \
         patch.object(FinalSummaryRunner, "_call_llm_reconciliation", new=AsyncMock(return_value={"reconciliation": []})), \
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
         patch.object(FinalSummaryRunner, "_call_llm_final", new=AsyncMock(return_value="重跑最终报告")):
        report2 = await r2.run()
    assert "重跑最终报告" in report2
    assert sum_mock.call_count == 0 and recon_mock.call_count == 0
