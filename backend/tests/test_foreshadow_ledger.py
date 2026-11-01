"""
伏笔账本测试
- 休眠以章距为主判据（不依赖批次阈值）
- _apply_reconciliation 未回收分支不推进 last_seen
- needs_review: 低置信回收标记
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.utils.foreshadow_ledger import (
    ForeshadowLedger, ForeshadowItem, DORMANT_CHAPTER_THRESHOLD,
)


def make_item(fid: str, first_seen: int, last_seen: int,
              last_seen_batch: int = 0, status: str = "active") -> ForeshadowItem:
    return ForeshadowItem(
        id=fid, description=f"伏笔{fid}", first_seen_chapter=first_seen,
        first_seen_batch=0, last_seen_chapter=last_seen,
        last_seen_batch=last_seen_batch,
        status=status, evidence_chapters=[first_seen],
    )


def test_dormant_by_chapter_span_only():
    """章距 ≥ 阈值即休眠，不依赖批次条件"""
    ledger = ForeshadowLedger()
    # 早期埋设、久未出现：即使 last_seen_batch 很大（旧数据残留 13/16）也应休眠
    ledger.items.append(make_item("fs_old", first_seen=10, last_seen=100, last_seen_batch=13))
    # 近期出现：不休眠
    ledger.items.append(make_item("fs_recent", first_seen=800, last_seen=850, last_seen_batch=2))
    # 已回收：不动
    ledger.items.append(make_item("fs_done", first_seen=5, last_seen=900, status="resolved"))

    # 批次总数少（12 批）也能休眠——休眠只看章距
    ledger.reconcile_and_update(batch_idx=12, current_chapter=909)

    statuses = {i.id: i.status for i in ledger.items}
    assert statuses["fs_old"] == "dormant"
    assert statuses["fs_recent"] == "active"
    assert statuses["fs_done"] == "resolved"
    # 章距恰好等于阈值边界：909-100=809 >= 200 → dormant
    assert (909 - 100) >= DORMANT_CHAPTER_THRESHOLD


def test_dormant_not_below_threshold():
    """章距 < 阈值不休眠"""
    ledger = ForeshadowLedger()
    ledger.items.append(make_item("fs_mid", first_seen=700, last_seen=750))
    ledger.reconcile_and_update(batch_idx=30, current_chapter=909)
    assert ledger.items[0].status == "active"  # 909-750=159 < 200


def test_apply_reconciliation_no_last_seen_advance():
    """未回收分支不得推进 last_seen"""
    from backend.services.final_summary import FinalSummaryRunner

    ledger = ForeshadowLedger()
    ledger.items.append(make_item("fs_a", first_seen=10, last_seen=100, last_seen_batch=0))

    runner = FinalSummaryRunner.__new__(FinalSummaryRunner)
    runner.ledger = ledger

    # 批次 5（ch 121-150）：fs_a 未被标记回收
    runner._apply_reconciliation(5, 121, 150, {
        "reconciliation": [{"foreshadow_id": "fs_a", "is_resolved_in_this_batch": False}],
    })
    item = ledger.items[0]
    assert item.last_seen_chapter == 100   # 保持 catalog 证据章，不被推到 150
    assert item.last_seen_batch == 0       # 不推进批次
    assert item.status == "active"


def test_apply_reconciliation_resolved_updates():
    """回收分支：状态/章节/批次更新，且默认高置信不标 needs_review"""
    from backend.services.final_summary import FinalSummaryRunner

    ledger = ForeshadowLedger()
    ledger.items.append(make_item("fs_b", first_seen=10, last_seen=100))
    runner = FinalSummaryRunner.__new__(FinalSummaryRunner)
    runner.ledger = ledger

    runner._apply_reconciliation(3, 61, 90, {
        "reconciliation": [{
            "foreshadow_id": "fs_b", "is_resolved_in_this_batch": True,
            "resolved_chapter": 85, "resolution_summary": "真相揭晓",
        }],
    })
    item = ledger.items[0]
    assert item.status == "resolved"
    assert item.resolved_chapter == 85
    assert item.last_seen_chapter == 90
    assert item.needs_review is False


def test_low_confidence_sets_needs_review():
    """低置信回收（confidence=中/低）→ needs_review=True"""
    from backend.services.final_summary import FinalSummaryRunner

    ledger = ForeshadowLedger()
    ledger.items.append(make_item("fs_c", first_seen=10, last_seen=100))
    runner = FinalSummaryRunner.__new__(FinalSummaryRunner)
    runner.ledger = ledger

    runner._apply_reconciliation(3, 61, 90, {
        "reconciliation": [{
            "foreshadow_id": "fs_c", "is_resolved_in_this_batch": True,
            "resolved_chapter": 85, "resolution_summary": "疑似回收",
            "confidence": "中",
        }],
    })
    item = ledger.items[0]
    assert item.status == "resolved"
    assert item.needs_review is True
    assert any("建议人工复核" in n for n in item.notes)


def test_reconcile_uses_total_chapters_fallback():
    """current_chapter=None 时回退 total_chapters（真实末章号语义）"""
    ledger = ForeshadowLedger()
    ledger.total_chapters = 909  # total_chapters 存真实末章号而非块数
    ledger.items.append(make_item("fs_old2", first_seen=10, last_seen=100))
    ledger.reconcile_and_update(batch_idx=16, current_chapter=None)
    assert ledger.items[0].status == "dormant"
