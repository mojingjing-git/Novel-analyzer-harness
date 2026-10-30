"""块规划：把有序章节切成"分析单位"，并让块的失效可判定（PLAN v2 §2.1 / §4 S2-2）。

块的失效必须能被**程序判定**，不能靠"文件在不在"。因此每个块文件里存签名，
签名由"策略版本 + 有序的（章节 ID，正文哈希）"算出；读回来时重算一遍就知道
它是否还对应当前源。这正是 S2 门槛要求的"更改块组成或正文使相关缓存失效"。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .artifacts import read_json_artifact, stamp_payload, write_json_atomic
from .constants import ErrorCode, VnextFormatError
from .fingerprint import block_signature
from .project import Manifest, block_signature_for, blocks_dir

#: 切块规则版本。改"怎么切"（例如从固定大小改为按情节边界）就 +1。
BLOCK_STRATEGY_VERSION = 1


@dataclass(frozen=True)
class BlockPlan:
    """一个块的定义。不含任何生成结果，只描述"哪几章算一个单位"。"""

    block_signature: str
    chapter_ids: tuple[str, ...]
    block_strategy_version: int
    start_display_no: int | None = None

    def to_dict(self) -> dict:
        return stamp_payload(
            {
                "block_signature": self.block_signature,
                "chapter_ids": list(self.chapter_ids),
                "block_strategy_version": self.block_strategy_version,
                # 展示字段：不参与签名，仅供进度条与报告使用
                "start_display_no": self.start_display_no,
            }
        )

    @classmethod
    def from_dict(cls, data: dict) -> "BlockPlan":
        for key in ("block_signature", "chapter_ids", "block_strategy_version"):
            if key not in data:
                raise VnextFormatError(
                    ErrorCode.MISSING_REQUIRED_FIELD, f"块定义缺字段：{key}"
                )
        return cls(
            block_signature=str(data["block_signature"]),
            chapter_ids=tuple(str(x) for x in data["chapter_ids"]),
            block_strategy_version=int(data["block_strategy_version"]),
            start_display_no=data.get("start_display_no"),
        )


def plan_blocks(
    manifest: Manifest,
    *,
    block_size: int,
    block_strategy_version: int = BLOCK_STRATEGY_VERSION,
) -> list[BlockPlan]:
    """按顺序把章节切成固定大小的块。

    规则刻意简单：不重叠、不重排、最后一块允许短。不做"智能边界"——那需要
    情节信息，而块规划发生在分析之前。
    """
    if block_size < 1:
        raise VnextFormatError(
            ErrorCode.INVALID_FIELD_VALUE, f"block_size 必须 >= 1，收到 {block_size}"
        )
    chapters = manifest.chapters
    plans: list[BlockPlan] = []
    for start in range(0, len(chapters), block_size):
        window = chapters[start : start + block_size]
        ids = tuple(c.chapter_id for c in window)
        plans.append(
            BlockPlan(
                block_signature=block_signature_for(
                    manifest, ids, block_strategy_version=block_strategy_version
                ),
                chapter_ids=ids,
                block_strategy_version=block_strategy_version,
                start_display_no=window[0].display_no,
            )
        )
    return plans


def write_block(book_dir: Path, plan: BlockPlan) -> Path:
    """落盘块定义。文件名即签名——同一签名重复写是幂等的。"""
    path = blocks_dir(book_dir) / f"{plan.block_signature}.json"
    write_json_atomic(path, plan.to_dict())
    return path


def load_block(book_dir: Path, signature: str) -> BlockPlan:
    path = blocks_dir(book_dir) / f"{signature}.json"
    if not path.is_file():
        raise VnextFormatError(
            ErrorCode.MISSING_REQUIRED_FIELD, f"块定义缺失：{signature}", path=str(path)
        )
    plan = BlockPlan.from_dict(dict(read_json_artifact(path)))
    if plan.block_signature != signature:
        raise VnextFormatError(
            ErrorCode.BLOCK_SIGNATURE_MISMATCH,
            f"块文件名与内部签名不一致：{signature} != {plan.block_signature}",
            path=str(path),
        )
    return plan


def verify_block(manifest: Manifest, plan: BlockPlan) -> None:
    """块是否还对应当前源。不符即抛 ``BLOCK_SIGNATURE_MISMATCH``。

    三类失效都会命中这里：成员章不同、某章正文改过、切块策略版本变了。
    """
    expected = block_signature_for(
        manifest,
        list(plan.chapter_ids),
        block_strategy_version=plan.block_strategy_version,
    )
    if expected != plan.block_signature:
        raise VnextFormatError(
            ErrorCode.BLOCK_SIGNATURE_MISMATCH,
            "块签名与当前源不一致（成员、正文或策略版本已变化）",
        )


def is_block_fresh(manifest: Manifest, plan: BlockPlan) -> bool:
    """:func:`verify_block` 的布尔版，供扫描时过滤而不抛异常。"""
    try:
        verify_block(manifest, plan)
        return True
    except VnextFormatError:
        return False


def same_composition(a: BlockPlan, b: BlockPlan) -> bool:
    """两个块是否"组成相同"（成员相同且顺序相同），忽略展示字段。

    用于把"重算出同一块"与"块边界变了"区分开：前者可复用旧结果，后者不行。
    """
    return a.chapter_ids == b.chapter_ids and a.block_strategy_version == b.block_strategy_version


def active_signatures(plans: Sequence[BlockPlan]) -> list[str]:
    """当前有效块签名（有序）。manifest 的 ``active_blocks`` 用它。"""
    return [p.block_signature for p in plans]
