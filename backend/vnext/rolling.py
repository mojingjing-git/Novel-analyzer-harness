"""有序 rolling 与缺口恢复：回答"现在到底完成了哪些块、能安全消费到哪里"。

这个模块不执行分析，也不写摘要。它只做一件事：把"各块的结果"整理成一份
**可按故事顺序消费的覆盖视图**，并明确说出哪里有洞、哪里已脏。

为什么需要它：并发执行时，块是乱序完成的。若"完成了多少"用"完成了几个"
来表示，就会把 100/102 已完成而 101 缺失的情况算成"进度 98%，接近完成"——
接着 rolling 摘要会拿着 [1..100] + [102] 去写"前 102 章的梗概"，
把第 101 章凭空跳过，而且没有任何地方会报错。

因此这里的每个判断都基于**实际输入序列中的相邻关系**，而不是自然章号连续：

* ``commit_cursor``：从第一块开始，连续满足"已完成且依赖新鲜"的最长前缀长度；
* ``consumable_upto``：可以安全喂给摘要的最远位置，且必须**对齐到批次边界**
  ——半个批次的信息进入摘要，等于摘要覆盖声明与实际不符；
* ``gaps``：被策略允许越过（``TERMINAL_SKIPPED``）但确实缺失的位置，保留并标 ``PARTIAL``；
* ``dirty_from``：已提交结果发生版本变化或迟到修复的位置；从这里往后的摘要都不再可信。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .blocks import BlockPlan

#: 块在覆盖视图中的状态。
DONE = "DONE"                        # 有结果且依赖新鲜
PENDING = "PENDING"                  # 未完成（含从未开始）
PENDING_RETRY = "PENDING_RETRY"      # 失败但可重试：**暂停推进**，不能当成洞跳过
TERMINAL_SKIPPED = "TERMINAL_SKIPPED"  # 永久跳过：可越过，但保留 gap 并标 PARTIAL

#: 完成状态与当前块签名不符（源改过、块边界变了）→ 降为未完成
REASON_SIGNATURE_CHANGED = "SIGNATURE_CHANGED"


@dataclass(frozen=True)
class BlockSlot:
    """一个块在覆盖视图中的位置与状态。"""

    index: int
    block_signature: str
    state: str = PENDING
    result_id: str | None = None
    reason: str = ""

    @property
    def committed(self) -> bool:
        return self.state == DONE and bool(self.result_id)


@dataclass(frozen=True)
class Coverage:
    """一次覆盖计算的结果。所有字段都可用于对外声明"覆盖到哪"。"""

    slots: tuple[BlockSlot, ...]
    batch_size: int
    commit_cursor: int
    committed_batches: int
    consumable_upto: int
    batch_aligned_upto: int
    gaps: tuple[int, ...]
    dirty_from: int | None
    missing: tuple[int, ...]

    @property
    def total(self) -> int:
        return len(self.slots)

    @property
    def prefix_complete(self) -> bool:
        """是否"从头连续覆盖到尾部"。100/102 已完成而 101 缺失时为 ``False``。"""
        return self.commit_cursor == self.total and not self.missing and not self.gaps

    @property
    def partial(self) -> bool:
        """覆盖里存在被跳过或尚未完成的块。"""
        return bool(self.gaps or self.missing)

    @property
    def rebuild_required(self) -> bool:
        """是否需要按预算重建摘要（有已提交结果变脏，且脏点在上游）。"""
        return self.dirty_from is not None and self.dirty_from < self.batch_aligned_upto

    def claimed_range(self, *, chapter_ordinals: Mapping[int, tuple[int, int]] | None = None
                      ) -> tuple[int, int] | None:
        """可对外声明"覆盖了哪一段"的章号区间（左闭右闭）。

        **不是**全文前缀：分析从第 100 章开始时，声明也只能从 100 开始。
        """
        if self.consumable_upto <= 0 or chapter_ordinals is None:
            return None
        first = chapter_ordinals.get(0)
        last = chapter_ordinals.get(self.consumable_upto - 1)
        if first is None or last is None:
            return None
        return (first[0], last[1])


def build_coverage(
    plans: Sequence[BlockPlan],
    *,
    batch_size: int,
    states: Mapping[int, BlockSlot] | None = None,
    dirty_from: int | None = None,
) -> Coverage:
    """按块的有序列表计算覆盖视图。

    ``states`` 以块下标为键；缺省即"全部未完成"。
    ``dirty_from`` 由调用方在"已提交结果版本变化 / 迟到修复"时给出。
    """
    if batch_size < 1:
        raise ValueError(f"batch_size 必须 >= 1，收到 {batch_size}")

    slots: list[BlockSlot] = []
    for index, plan in enumerate(plans):
        given = (states or {}).get(index)
        if given is None:
            slots.append(BlockSlot(index=index, block_signature=plan.block_signature))
        elif given.block_signature != plan.block_signature:
            # 签名不符 = 这份完成状态属于**另一个块**（源改过、块边界变了）。
            # 2026-10-30 复审：原实现把签名"纠正"成新的却保留 DONE 与 result_id，
            # 等于把旧块的结果挂在新块下面，还宣称前缀完整。
            # 正确处置是降为未完成，并留下原因，交给上层决定重跑还是重建。
            slots.append(
                BlockSlot(
                    index=index,
                    block_signature=plan.block_signature,
                    state=PENDING,
                    reason=REASON_SIGNATURE_CHANGED,
                )
            )
        else:
            slots.append(
                BlockSlot(
                    index=index,
                    block_signature=plan.block_signature,
                    state=given.state,
                    result_id=given.result_id,
                    reason=given.reason,
                )
            )

    # 连续前缀：DONE 的块推进；TERMINAL_SKIPPED 也推进（但记 gap）；PENDING_RETRY 停住。
    cursor = 0
    gaps: list[int] = []
    for slot in slots:
        if slot.state == DONE and slot.result_id:
            cursor += 1
            continue
        if slot.state == TERMINAL_SKIPPED:
            gaps.append(slot.index)
            cursor += 1
            continue
        break

    missing = tuple(slot.index for slot in slots if not slot.committed
                    and slot.state not in (TERMINAL_SKIPPED,))

    committed = sum(1 for slot in slots if slot.committed)
    committed_batches = committed // batch_size
    # 消费位置对齐批次：半个批次的信息不许进摘要
    batch_aligned = (cursor // batch_size) * batch_size
    consumable = min(batch_aligned, len(slots))

    # 脏点之前（含）的内容仍可信：脏块本身及其后都不再可消费
    batch_aligned_upto = consumable
    if dirty_from is not None:
        dirty_batch = (dirty_from // batch_size) * batch_size
        consumable = min(consumable, dirty_batch)

    return Coverage(
        slots=tuple(slots),
        batch_size=batch_size,
        commit_cursor=cursor,
        committed_batches=committed_batches,
        consumable_upto=consumable,
        batch_aligned_upto=batch_aligned_upto,
        gaps=tuple(gaps),
        dirty_from=dirty_from,
        missing=missing,
    )


def slots_from_results(
    plans: Sequence[BlockPlan],
    results: Iterable[object],
    *,
    skipped: Iterable[int] = (),
    retryable: Iterable[int] = (),
) -> dict[int, BlockSlot]:
    """把结果对象整理成 ``build_coverage`` 需要的 ``states``。

    只认"结果登记的块签名与当前块签名一致"的结果：签名不符说明块组成变了，
    那份结果属于另一个块，不能拿来充数。
    """
    skipped_set = set(skipped)
    retry_set = set(retryable)
    index_of = {plan.block_signature: index for index, plan in enumerate(plans)}

    states: dict[int, BlockSlot] = {}
    for result in results:
        signature = getattr(result, "block_signature", None)
        result_id = getattr(result, "result_id", None)
        status = getattr(result, "status", None)
        index = index_of.get(signature)
        if index is None:
            continue  # 属于已不存在的块
        plan = plans[index]
        if index in retry_set:
            states[index] = BlockSlot(index, plan.block_signature, PENDING_RETRY,
                                      result_id=result_id, reason="可重试失败")
            continue
        if status == "SUCCEEDED" and result_id:
            states[index] = BlockSlot(index, plan.block_signature, DONE, result_id=result_id)
        elif status in ("FAILED", "CANCELLED"):
            states[index] = BlockSlot(index, plan.block_signature, PENDING_RETRY,
                                      result_id=result_id, reason=f"结果状态 {status}")
        else:
            states[index] = BlockSlot(index, plan.block_signature, PENDING_RETRY,
                                      result_id=result_id, reason=f"结果状态 {status}")

    for index in skipped_set:
        if index not in states:
            plan = plans[index]
            states[index] = BlockSlot(index, plan.block_signature, TERMINAL_SKIPPED,
                                      reason="永久跳过")

    return states


def uncommitted_prefix(coverage: Coverage) -> tuple[int, ...]:
    """尚未提交的前缀位置（从 0 开始连续的那一段里未提交的块）。

    补跑与"待提交"判断都基于它，而不是"missing"全集——因为后面还有大量块
    本来就没轮到跑。
    """
    return tuple(slot.index for slot in coverage.slots[: coverage.commit_cursor]
                 if not slot.committed)
