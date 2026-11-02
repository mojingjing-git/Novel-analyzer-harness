"""源头收口（PLAN-foreshadow-adjudication D-11 段 B）。

采集侧 reconciliation 的 `is_resolved` 自述不再直接置 resolved——
"模型觉得答对了"不是证据。自述降为提名（status 保持 active + needs_review），
由 V2 复检按 answer_criteria 裁决后才落 resolved。
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.services.final_summary import FinalSummaryRunner
from backend.utils.foreshadow_ledger import ForeshadowLedger, ForeshadowItem


def _runner_with(items: list[ForeshadowItem]) -> FinalSummaryRunner:
    """_apply_reconciliation / _build_active_foreshadows_block 只用 self.ledger，stub 即可。"""
    return SimpleNamespace(ledger=ForeshadowLedger(items=items))


def _recon(fid: str, resolved: bool, summary: str = "", chapter: int = 5, confidence: str = "高") -> dict:
    return {
        "foreshadow_id": fid,
        "is_resolved_in_this_batch": resolved,
        "resolution_summary": summary,
        "resolved_chapter": chapter,
        "confidence": confidence,
    }


def test_reconciliation_claim_becomes_nomination_not_resolution():
    item = ForeshadowItem(
        id="fs_001", description="铜匣之秘", first_seen_chapter=3, first_seen_batch=0,
        last_seen_chapter=3, last_seen_batch=0, status="active", source_type="catalog_scan",
    )
    runner = _runner_with([item])
    FinalSummaryRunner._apply_reconciliation(
        runner, batch_idx=2, ch_start=9, ch_end=12,
        foreshadow_data={"reconciliation": [_recon("fs_001", True, "铜匣在第10章被打开", chapter=10)]},
    )

    # 核心断言：自述不再置 resolved，也不改写来源标记
    assert item.status == "active"
    assert item.needs_review is True
    assert item.resolution == "铜匣在第10章被打开"
    assert item.source_type == "catalog_scan"
    assert any("待证据裁决" in n for n in (item.notes or []))
    # 置信度为"高"也一样待裁决——置信与回收质量不相关
    item2 = ForeshadowItem(
        id="fs_002", description="另一伏笔", first_seen_chapter=3, first_seen_batch=0,
        last_seen_chapter=3, last_seen_batch=0, status="active",
    )
    runner.ledger.items.append(item2)
    FinalSummaryRunner._apply_reconciliation(
        runner, batch_idx=3, ch_start=13, ch_end=15,
        foreshadow_data={"reconciliation": [_recon("fs_002", True, "高置信自述", chapter=14, confidence="高")]},
    )
    assert item2.status == "active"
    assert item2.needs_review is True


def test_reconciliation_unresolved_branch_unchanged():
    """未回收分支行为保持：不推进 last_seen、不改状态。"""
    item = ForeshadowItem(
        id="fs_003", description="未回收", first_seen_chapter=3, first_seen_batch=0,
        last_seen_chapter=3, last_seen_batch=0, status="active",
    )
    runner = _runner_with([item])
    FinalSummaryRunner._apply_reconciliation(
        runner, batch_idx=1, ch_start=4, ch_end=6,
        foreshadow_data={"reconciliation": [_recon("fs_003", False)]},
    )
    assert item.status == "active"
    assert item.last_seen_chapter == 3  # 未回收不推进 last_seen（语义由 catalog 证据章维护）


def test_nominated_items_excluded_from_later_reconciliation_batches():
    """已提名条目不得进入后续批次的【当前伏笔清单】——否则被反复提问并覆写提名。"""
    nominated = ForeshadowItem(
        id="fs_nom", description="已提名", first_seen_chapter=3, first_seen_batch=0,
        last_seen_chapter=12, last_seen_batch=2, status="active", needs_review=True,
    )
    fresh = ForeshadowItem(
        id="fs_new", description="新条目", first_seen_chapter=3, first_seen_batch=0,
        last_seen_chapter=12, last_seen_batch=2, status="active",
    )
    runner = _runner_with([nominated, fresh])
    catalog = [
        {"id": "fs_nom", "clue": "已提名", "first_seen": 3, "last_seen": 12,
         "evidence_chapters": [3], "merged_count": 1},
        {"id": "fs_new", "clue": "新条目", "first_seen": 3, "last_seen": 12,
         "evidence_chapters": [3], "merged_count": 1},
    ]
    block = FinalSummaryRunner._build_active_foreshadows_block(runner, 13, 20, catalog)
    assert "fs_new" in block
    assert "fs_nom" not in block

    # 降级分支（无 catalog）同样排除
    block2 = FinalSummaryRunner._build_active_foreshadows_block(runner, 13, 20, None)
    assert "fs_new" in block2
    assert "fs_nom" not in block2
