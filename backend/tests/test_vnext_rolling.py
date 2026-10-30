"""有序覆盖视图（S4）：缺口不能被算成"接近完成"，半个批次不能进摘要。

这组用例的目标不是覆盖率计算本身，而是挡住三类"看起来正常"的错误：
① 100/102 已完成被说成"基本完成"；② 并发乱序完成导致摘要吃进不连续的内容；
③ 已提交结果改版后，摘要仍然继续用旧结论。
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from backend.vnext.blocks import BlockPlan
from backend.vnext.rolling import (
    DONE,
    PENDING_RETRY,
    TERMINAL_SKIPPED,
    BlockSlot,
    build_coverage,
    slots_from_results,
    uncommitted_prefix,
)


def _plans(n: int) -> list[BlockPlan]:
    return [
        BlockPlan(block_signature=f"sig{i}", chapter_ids=(f"c{i}",), block_strategy_version=1)
        for i in range(n)
    ]


def _done(plans, indices) -> dict[int, BlockSlot]:
    return {
        i: BlockSlot(index=i, block_signature=plans[i].block_signature, state=DONE, result_id=f"r{i}")
        for i in indices
    }


# ---------------------------------------------------------------------------
# 连续前缀
# ---------------------------------------------------------------------------


def test_all_done_is_complete_prefix() -> None:
    plans = _plans(4)
    cov = build_coverage(plans, batch_size=2, states=_done(plans, range(4)))
    assert cov.commit_cursor == 4
    assert cov.committed_batches == 2
    assert cov.consumable_upto == 4
    assert cov.prefix_complete is True


def test_missing_middle_block_is_not_near_complete() -> None:
    """S4 门槛的原样复现：1、3 完成而 2 缺失，不能宣称"完成 2/3，接近完成"。"""
    plans = _plans(3)
    cov = build_coverage(plans, batch_size=2, states=_done(plans, [0, 2]))
    assert cov.commit_cursor == 1            # 从第一块起连续区只有 1 块
    assert cov.missing == (1,)   # 第 3 块已完成，缺的只有中间那块
    assert cov.prefix_complete is False
    assert cov.partial is True


def test_empty_plan_is_trivially_empty() -> None:
    cov = build_coverage([], batch_size=2)
    assert cov.commit_cursor == 0 and cov.consumable_upto == 0
    assert cov.prefix_complete is True


# ---------------------------------------------------------------------------
# 批次对齐
# ---------------------------------------------------------------------------


def test_consumable_is_batch_aligned() -> None:
    """3 块完成、批次 2 → 只能消费 1 个完整批次（2 块）。半个批次不许进摘要。"""
    plans = _plans(6)
    cov = build_coverage(plans, batch_size=2, states=_done(plans, range(3)))
    assert cov.commit_cursor == 3
    assert cov.consumable_upto == 2


def test_consumable_never_exceeds_total() -> None:
    plans = _plans(3)
    cov = build_coverage(plans, batch_size=10, states=_done(plans, range(3)))
    assert cov.consumable_upto == 0   # 一个完整批次都没凑够


# ---------------------------------------------------------------------------
# 跳过与重试
# ---------------------------------------------------------------------------


def test_terminal_skip_advances_but_keeps_gap() -> None:
    """永久跳过可以越过，但必须留下 gap 并整体标 PARTIAL。"""
    plans = _plans(3)
    states = _done(plans, [0, 2])
    states[1] = BlockSlot(index=1, block_signature=plans[1].block_signature,
                          state=TERMINAL_SKIPPED, reason="内容审核永久拦截")
    cov = build_coverage(plans, batch_size=1, states=states)
    assert cov.commit_cursor == 3        # 越过
    assert cov.gaps == (1,)              # 但记洞
    assert cov.partial is True
    assert cov.prefix_complete is False  # 不能因为"越过了"就宣称完整


def test_pending_retry_stops_progress() -> None:
    plans = _plans(3)
    states = _done(plans, [0, 1, 2])
    states[1] = BlockSlot(index=1, block_signature=plans[1].block_signature,
                          state=PENDING_RETRY, reason="限流")
    cov = build_coverage(plans, batch_size=1, states=states)
    assert cov.commit_cursor == 1        # 卡在第一块之后就停
    assert cov.gaps == ()                # 它不是"洞"，是"还没好"


# ---------------------------------------------------------------------------
# 脏点
# ---------------------------------------------------------------------------


def test_dirty_point_shrinks_consumable_and_requires_rebuild() -> None:
    plans = _plans(8)
    cov = build_coverage(plans, batch_size=2, states=_done(plans, range(8)), dirty_from=4)
    assert cov.consumable_upto == 4          # 脏块所在批次起点
    assert cov.rebuild_required is True


def test_dirty_point_beyond_consumable_does_not_require_rebuild() -> None:
    """脏点还没被消费到，就不必重建。"""
    plans = _plans(8)
    cov = build_coverage(plans, batch_size=2, states=_done(plans, range(2)), dirty_from=6)
    assert cov.consumable_upto == 2
    assert cov.rebuild_required is False


def test_dirty_point_respects_batch_alignment() -> None:
    plans = _plans(8)
    cov = build_coverage(plans, batch_size=3, states=_done(plans, range(8)), dirty_from=4)
    assert cov.consumable_upto == 3   # 脏批次起点 3


# ---------------------------------------------------------------------------
# 顺序无关（S4 门槛：10 种完成顺序得到同一覆盖）
# ---------------------------------------------------------------------------


def test_completion_order_does_not_change_coverage() -> None:
    plans = _plans(5)
    signatures = set()
    for order in itertools.permutations(range(5)):
        # 按该顺序逐个完成，每次重算；最终状态都是"全完成"
        states: dict[int, BlockSlot] = {}
        for index in order:
            states.update(_done(plans, [index]))
            build_coverage(plans, batch_size=2, states=dict(states))  # 中间态也要能算
        cov = build_coverage(plans, batch_size=2, states=states)
        signatures.add((cov.commit_cursor, cov.consumable_upto, cov.committed_batches))
    assert signatures == {(5, 4, 2)}


def test_partial_completion_orders_converge_to_same_view() -> None:
    """同一组"完成了哪几块"，无论按什么顺序完成，视图必须相同。"""
    plans = _plans(4)
    target = [0, 2, 3]
    views = set()
    for order in itertools.permutations(target):
        states: dict[int, BlockSlot] = {}
        for index in order:
            states.update(_done(plans, [index]))
        cov = build_coverage(plans, batch_size=1, states=states)
        views.add((cov.commit_cursor, cov.consumable_upto, cov.missing, cov.gaps))
    assert views == {(1, 1, (1,), ())}


# ---------------------------------------------------------------------------
# 结果 → 状态
# ---------------------------------------------------------------------------


def test_slots_from_results_maps_statuses() -> None:
    plans = _plans(3)
    results = [
        SimpleNamespace(block_signature="sig0", result_id="r0", status="SUCCEEDED"),
        SimpleNamespace(block_signature="sig1", result_id="r1", status="FAILED"),
    ]
    states = slots_from_results(plans, results)
    assert states[0].state == DONE
    assert states[1].state == PENDING_RETRY


def test_slots_from_results_ignores_unknown_block_signature() -> None:
    """签名对不上说明块组成变了，那份结果属于另一个块，不能拿来充数。"""
    plans = _plans(2)
    results = [SimpleNamespace(block_signature="已经不存在了", result_id="r", status="SUCCEEDED")]
    assert slots_from_results(plans, results) == {}


def test_retryable_marks_pending_retry_even_if_succeeded() -> None:
    plans = _plans(1)
    results = [SimpleNamespace(block_signature="sig0", result_id="r0", status="SUCCEEDED")]
    states = slots_from_results(plans, results, retryable=[0])
    assert states[0].state == PENDING_RETRY


# ---------------------------------------------------------------------------
# 声明与补跑
# ---------------------------------------------------------------------------


def test_claimed_range_starts_from_actual_first_chapter() -> None:
    """分析从第 100 章开始时，可声明范围也只能从 100 开始，不能称全文前缀。"""
    plans = _plans(4)
    cov = build_coverage(plans, batch_size=2, states=_done(plans, range(4)))
    ordinal_map = {0: (100, 101), 1: (102, 103), 2: (104, 105), 3: (106, 107)}
    claimed = cov.claimed_range(chapter_ordinals=ordinal_map)
    assert claimed is not None
    assert claimed[0] == 100          # 起点是真实首章，不是 1
    assert claimed[1] == 107          # 4 块 = 两个完整批次，全部可声明


def test_claimed_range_is_none_when_nothing_consumable() -> None:
    plans = _plans(2)
    cov = build_coverage(plans, batch_size=2, states={})
    assert cov.claimed_range(chapter_ordinals={0: (1, 2)}) is None


def test_uncommitted_prefix_finds_holes_inside_prefix() -> None:
    plans = _plans(3)
    states = _done(plans, [0, 1, 2])
    states[1] = BlockSlot(index=1, block_signature=plans[1].block_signature,
                          state=TERMINAL_SKIPPED, reason="跳过")
    cov = build_coverage(plans, batch_size=1, states=states)
    assert uncommitted_prefix(cov) == (1,)


def test_batch_size_must_be_positive() -> None:
    with pytest.raises(ValueError):
        build_coverage(_plans(2), batch_size=0)
