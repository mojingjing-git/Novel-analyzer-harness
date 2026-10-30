"""vnext 块规划与失效判定（PLAN v2 §4 S2 门槛：改块组成或正文必须使缓存失效）。"""

from __future__ import annotations

import pytest

from backend.vnext import blocks, project
from backend.vnext.constants import VnextFormatError


def _book(tmp_path, n: int = 7, *, book_id: str = "bk_blocks00001"):
    chapters = [(i, f"第{i}章\n正文{i}\n") for i in range(1, n + 1)]
    return project.create_project(tmp_path / book_id, "书", chapters, book_id=book_id)


def test_plan_blocks_splits_in_order_without_overlap(tmp_path) -> None:
    book = _book(tmp_path, 7)
    m = project.load_manifest(book)
    plans = blocks.plan_blocks(m, block_size=3)

    assert [len(p.chapter_ids) for p in plans] == [3, 3, 1]
    flat = [cid for p in plans for cid in p.chapter_ids]
    assert flat == [c.chapter_id for c in m.chapters]  # 顺序一致且不丢不重
    assert len({p.block_signature for p in plans}) == 3


def test_plan_blocks_handles_degenerate_sizes(tmp_path) -> None:
    m = project.load_manifest(_book(tmp_path, 3))
    assert [len(p.chapter_ids) for p in blocks.plan_blocks(m, block_size=1)] == [1, 1, 1]
    assert [len(p.chapter_ids) for p in blocks.plan_blocks(m, block_size=99)] == [3]


def test_plan_blocks_rejects_bad_block_size(tmp_path) -> None:
    m = project.load_manifest(_book(tmp_path, 2))
    with pytest.raises(VnextFormatError) as exc:
        blocks.plan_blocks(m, block_size=0)
    assert exc.value.code.value == "INVALID_FIELD_VALUE"


def test_block_roundtrip_through_disk(tmp_path) -> None:
    book = _book(tmp_path, 4)
    m = project.load_manifest(book)
    plan = blocks.plan_blocks(m, block_size=2)[0]

    path = blocks.write_block(book, plan)
    assert path.name == f"{plan.block_signature}.json"

    loaded = blocks.load_block(book, plan.block_signature)
    assert loaded.chapter_ids == plan.chapter_ids
    assert loaded.start_display_no == plan.start_display_no
    assert blocks.is_block_fresh(m, loaded)


def test_load_block_rejects_name_signature_mismatch(tmp_path) -> None:
    """文件名与内部签名不一致 → 明确报错，不能"按文件名信以为真"。"""
    book = _book(tmp_path, 2)
    m = project.load_manifest(book)
    plan = blocks.plan_blocks(m, block_size=1)[0]
    path = blocks.write_block(book, plan)

    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["block_signature"] = "0" * 64
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(VnextFormatError) as exc:
        blocks.load_block(book, plan.block_signature)
    assert exc.value.code.value == "BLOCK_SIGNATURE_MISMATCH"


# ---------------------------------------------------------------------------
# 失效判定（S2 门槛）
# ---------------------------------------------------------------------------


def test_changed_chapter_text_invalidates_block(tmp_path) -> None:
    """改一章正文 → 含它的块失效；不含它的块仍然有效（局部失效，不是全库失效）。"""
    book = _book(tmp_path, 4)
    old_manifest = project.load_manifest(book)
    plans = blocks.plan_blocks(old_manifest, block_size=2)  # [ch1,ch2] [ch3,ch4]

    # 用同一 book_id 另建一个"第 2 章正文变了"的项目
    new_root = tmp_path / "edited"
    chapters = [(i, f"第{i}章\n正文{i}\n") for i in range(1, 5)]
    chapters[1] = (2, "第2章\n这一章被改过了\n")
    edited = project.create_project(new_root, "书", chapters, book_id="bk_blocks00001")
    new_manifest = project.load_manifest(edited)

    assert not blocks.is_block_fresh(new_manifest, plans[0])  # 含第 2 章的块失效
    assert blocks.is_block_fresh(new_manifest, plans[1])      # 不含第 2 章的块仍有效


def test_changed_block_composition_invalidates_block(tmp_path) -> None:
    """同样的前两章，块大小变了 → 块组成变了 → 旧签名不再成立。"""
    book = _book(tmp_path, 4)
    m = project.load_manifest(book)
    size2 = blocks.plan_blocks(m, block_size=2)[0]   # [ch1, ch2]
    size1 = blocks.plan_blocks(m, block_size=1)[0]   # [ch1]

    assert size2.chapter_ids != size1.chapter_ids
    assert size2.block_signature != size1.block_signature
    # 拿 size1 的计划去校验 size2 的签名必然不符
    forged = blocks.BlockPlan(
        block_signature=size1.block_signature,
        chapter_ids=size2.chapter_ids,
        block_strategy_version=size1.block_strategy_version,
    )
    assert not blocks.is_block_fresh(m, forged)


def test_strategy_version_change_invalidates_block(tmp_path) -> None:
    m = project.load_manifest(_book(tmp_path, 3))
    v1 = blocks.plan_blocks(m, block_size=2, block_strategy_version=1)[0]
    v2 = blocks.plan_blocks(m, block_size=2, block_strategy_version=2)[0]
    assert v1.chapter_ids == v2.chapter_ids
    assert v1.block_signature != v2.block_signature  # 组成相同、策略变了也要重算


def test_removed_chapter_invalidates_block(tmp_path) -> None:
    m = project.load_manifest(_book(tmp_path, 3))
    plan = blocks.plan_blocks(m, block_size=3)[0]

    m.chapters = m.chapters[:2]  # 模拟"第 3 章被删"
    with pytest.raises(VnextFormatError):
        blocks.verify_block(m, plan)


def test_same_composition_ignores_display_fields(tmp_path) -> None:
    """展示章号不同但成员相同 → 视为同一组成（可复用结果）。"""
    m = project.load_manifest(_book(tmp_path, 2))
    a = blocks.plan_blocks(m, block_size=2)[0]
    b = blocks.BlockPlan(
        block_signature=a.block_signature,
        chapter_ids=a.chapter_ids,
        block_strategy_version=a.block_strategy_version,
        start_display_no=999,
    )
    assert blocks.same_composition(a, b)


def test_active_signatures_feeds_manifest(tmp_path) -> None:
    book = _book(tmp_path, 5)
    m = project.load_manifest(book)
    sigs = blocks.active_signatures(blocks.plan_blocks(m, block_size=2))
    assert len(sigs) == 3

    project.commit_manifest(book, lambda mm: mm.active_blocks.extend(sigs))
    assert project.load_manifest(book).active_blocks == sigs
