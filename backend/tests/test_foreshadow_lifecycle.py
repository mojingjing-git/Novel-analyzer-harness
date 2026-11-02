"""生命周期底表与状态机修复（PLAN-foreshadow-adjudication 段 A）。

七栏底表必须互斥且完备（加总=总数）；needs_review 条目不允许被自动休眠改变状态——
"请人工确认"被自动冻结等于人工环节永远不发生（生产账本 221 条事故）。
"""

from __future__ import annotations

from backend.services.final_summary import split_lifecycle
from backend.utils.foreshadow_ledger import DORMANT_CHAPTER_THRESHOLD, ForeshadowLedger, ForeshadowItem


def _item(**kw) -> ForeshadowItem:
    defaults = dict(
        id="fs_1",
        description="测试伏笔",
        first_seen_chapter=1,
        first_seen_batch=0,
        last_seen_chapter=1,
        last_seen_batch=0,
        status="active",
    )
    defaults.update(kw)
    return ForeshadowItem(**defaults)


# ==================== 七栏底表 ====================

def test_lifecycle_bars_partition_and_conserv():
    ledger = ForeshadowLedger()
    ledger.items = [
        _item(id="a1", status="active"),
        _item(id="a2", status="active", needs_review=True),
        _item(id="r1", status="resolved", resolution_quality="verified"),
        _item(id="r2", status="resolved", resolution_quality="questioned"),
        _item(id="r3", status="resolved"),  # 未复审（无 quality 字段值）
        _item(id="d1", status="dormant"),
        _item(id="d2", status="dormant", needs_review=True),
    ]
    bars = ledger.lifecycle_bars()
    assert bars == {
        "active_open": 1,
        "active_review": 1,
        "resolved_verified": 1,
        "resolved_questioned": 1,
        "resolved_unverified": 1,
        "dormant_plain": 1,
        "dormant_review": 1,
    }
    assert sum(bars.values()) == len(ledger.items)


def test_lifecycle_bars_pre_v6_items_default_to_unverified():
    """v5 旧条目（无 resolution_quality 属性）必须落"未复审"栏，不报错。"""
    ledger = ForeshadowLedger()
    item = _item(id="old", status="resolved")
    assert not hasattr(item, "resolution_quality") or not getattr(item, "resolution_quality")
    ledger.items = [item]
    assert ledger.lifecycle_bars()["resolved_unverified"] == 1


# ==================== 休眠与待确认解耦 ====================

def test_reconcile_does_not_freeze_needs_review_items():
    """needs_review 条目即使超过休眠章距也保持 active——冻结它们 = "请人工确认"永远没有人工环节。"""
    ledger = ForeshadowLedger()
    ledger.total_chapters = 1000
    ledger.items = [
        _item(id="plain", last_seen_chapter=10),                    # 990 章未见 → 应冻结
        _item(id="review", last_seen_chapter=10, needs_review=True),  # 同样超距 → 必须保持 active
    ]
    ledger.reconcile_and_update(batch_idx=9, current_chapter=1000)

    by_id = {i.id: i for i in ledger.items}
    assert by_id["plain"].status == "dormant"
    assert by_id["review"].status == "active"
    assert not any("休眠" in n for n in (by_id["review"].notes or []))


def test_reconcile_still_freezes_normal_items_within_threshold():
    ledger = ForeshadowLedger()
    ledger.total_chapters = 150
    ledger.items = [_item(id="near", last_seen_chapter=100)]  # 50 章 < 阈值 → 不冻
    ledger.reconcile_and_update(batch_idx=1, current_chapter=150)
    assert ledger.items[0].status == "active"
    assert DORMANT_CHAPTER_THRESHOLD == 200  # 阈值语义不被本次改动挪动


# ==================== 报告分组（活跃组排除待确认） ====================

def test_split_lifecycle_separates_pending_from_active():
    items = [
        {"id": "open", "status": "active", "needs_review": False},
        {"id": "prop", "status": "active", "needs_review": True},
        {"id": "done", "status": "resolved", "needs_review": False},
        {"id": "sleep", "status": "dormant", "needs_review": False},
        {"id": "frozen", "status": "dormant", "needs_review": True},
    ]
    g = split_lifecycle(items)
    assert [i["id"] for i in g["active"]] == ["open"]
    assert [i["id"] for i in g["pending"]] == ["prop"]
    assert [i["id"] for i in g["resolved"]] == ["done"]
    assert [i["id"] for i in g["dormant"]] == ["sleep"]
    assert [i["id"] for i in g["frozen"]] == ["frozen"]


def test_split_lifecycle_missing_needs_review_defaults_false():
    """旧行 dict 无 needs_review 键（消费方兼容）→ 视为普通条目。"""
    g = split_lifecycle([{"id": "x", "status": "active"}])
    assert [i["id"] for i in g["active"]] == ["x"]
