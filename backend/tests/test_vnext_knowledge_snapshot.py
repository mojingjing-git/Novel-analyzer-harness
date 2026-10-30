"""vnext 知识快照：显式上界、未知范围排除、来源顺序覆盖（PLAN v2 §4 S3 第 1–4、6 条）。

这些用例守的不是"功能能用"，而是三条容易悄悄退化的性质：
上界排他、未知来源不进快照、同章冲突不随遍历顺序改变结论。
"""

from __future__ import annotations

import pytest

from backend.vnext.knowledge import (
    CharacterStateRecord,
    FactRecord,
    SourceRef,
    build_snapshot,
    snapshot_for_block,
)


def _src(chapter: int, *, start: int = 0, end: int = 10, cid: str | None = None) -> SourceRef:
    return SourceRef(
        chapter_id=cid or f"ch_{chapter:04d}",
        chapter_ordinal=chapter,
        start=start,
        end=end,
        content_hash=f"hash{chapter}",
    )


def _fact(fid: str, chapter: int | None, *, text: str | None = None, start: int = 0) -> FactRecord:
    return FactRecord(
        fact_id=fid,
        text=text or f"事实{fid}",
        source=_src(chapter, start=start) if chapter is not None else None,
    )


# ---------------------------------------------------------------------------
# 上界：排他
# ---------------------------------------------------------------------------


def test_bound_is_exclusive() -> None:
    """exclusive_before=5 ⇒ 第 1–4 章可见，第 5 章不可见。"""
    facts = [_fact("a", 4), _fact("b", 5), _fact("c", 6)]
    snap = build_snapshot(exclusive_before=5, facts=facts)
    assert [f.fact_id for f in snap.facts] == ["a"]


def test_snapshot_for_block_uses_first_chapter_as_bound() -> None:
    """一个块从第 5 章开始 ⇒ 快照只含第 4 章之前的内容。"""
    facts = [_fact("a", 4), _fact("b", 5)]
    snap = snapshot_for_block(block_first_ordinal=5, facts=facts)
    assert [f.fact_id for f in snap.facts] == ["a"]


def test_empty_snapshot_for_first_block() -> None:
    snap = build_snapshot(exclusive_before=1, facts=[_fact("a", 1)])
    assert snap.facts == ()
    assert snap.character_states == ()


def test_bound_must_be_positive() -> None:
    with pytest.raises(ValueError):
        build_snapshot(exclusive_before=0)


# ---------------------------------------------------------------------------
# 未知来源
# ---------------------------------------------------------------------------


def test_fact_without_source_is_excluded_and_reported() -> None:
    """没有可定位来源的记录不进快照，但要能说清"排除了什么"。"""
    snap = build_snapshot(exclusive_before=10, facts=[_fact("ghost", None), _fact("ok", 3)])
    assert [f.fact_id for f in snap.facts] == ["ok"]
    assert snap.dropped_unknown_range == ("ghost",)


def test_unknown_source_is_never_guessed_as_early() -> None:
    """即使位置"看起来"很早，缺来源也不猜。"""
    snap = build_snapshot(exclusive_before=100, facts=[_fact("x", None)])
    assert snap.facts == ()


# ---------------------------------------------------------------------------
# 确定性
# ---------------------------------------------------------------------------


def test_facts_are_sorted_by_source_position() -> None:
    facts = [_fact("c", 5), _fact("a", 1), _fact("b", 3)]
    snap = build_snapshot(exclusive_before=9, facts=facts)
    assert [f.fact_id for f in snap.facts] == ["a", "b", "c"]


def test_same_input_same_fingerprint() -> None:
    facts = [_fact("a", 1), _fact("b", 2)]
    s1 = build_snapshot(exclusive_before=9, facts=facts)
    s2 = build_snapshot(exclusive_before=9, facts=list(reversed(facts)))
    assert s1.dependency_fingerprint() == s2.dependency_fingerprint()


def test_fingerprint_changes_when_visible_content_changes() -> None:
    base = build_snapshot(exclusive_before=9, facts=[_fact("a", 1)])
    more = build_snapshot(exclusive_before=9, facts=[_fact("a", 1), _fact("b", 2)])
    assert base.dependency_fingerprint() != more.dependency_fingerprint()


def test_fingerprint_ignores_bound_when_content_identical() -> None:
    """上界不同但可见内容相同 → 同一份上下文（依赖不该因为块前进了就变）。"""
    facts = [_fact("a", 1)]
    assert (
        build_snapshot(exclusive_before=5, facts=facts).dependency_fingerprint()
        == build_snapshot(exclusive_before=9, facts=facts).dependency_fingerprint()
    )


# ---------------------------------------------------------------------------
# 人物状态：来源顺序覆盖
# ---------------------------------------------------------------------------


def test_later_source_overrides_earlier() -> None:
    records = [
        CharacterStateRecord("方醒", "刚穿越", _src(1)),
        CharacterStateRecord("方醒", "已登基", _src(12)),
    ]
    snap = build_snapshot(exclusive_before=13, character_records=records)
    assert dict(snap.character_states)["方醒"] == "已登基"


def test_earlier_source_cannot_override_later_even_if_processed_last() -> None:
    """迟到章节（乱序到达）不得覆盖较新状态——这正是复审第 4 条的靶子。"""
    records = [
        CharacterStateRecord("方醒", "已登基", _src(12)),
        CharacterStateRecord("方醒", "刚穿越", _src(1)),  # 后处理但更早
    ]
    snap = build_snapshot(exclusive_before=13, character_records=records)
    assert dict(snap.character_states)["方醒"] == "已登基"


def test_later_chapter_does_not_leak_before_its_time() -> None:
    records = [
        CharacterStateRecord("方醒", "刚穿越", _src(1)),
        CharacterStateRecord("方醒", "已登基", _src(12)),
    ]
    snap = build_snapshot(exclusive_before=12, character_records=records)
    assert dict(snap.character_states)["方醒"] == "刚穿越"


def test_new_character_from_late_chapter_still_joins() -> None:
    """限制的是覆盖顺序，不是加入资格：新人物照样出现。"""
    records = [
        CharacterStateRecord("方醒", "刚穿越", _src(1)),
        CharacterStateRecord("新人", "登场", _src(9)),
    ]
    snap = build_snapshot(exclusive_before=10, character_records=records)
    assert dict(snap.character_states) == {"方醒": "刚穿越", "新人": "登场"}


def test_same_position_conflict_prefers_certain_record() -> None:
    """"同章同位置两条互相矛盾"时结论不能取决于遍历顺序。"""
    certain = CharacterStateRecord("甲", "确定值", _src(3, start=5), uncertain=False)
    unsure = CharacterStateRecord("甲", "存疑值", _src(3, start=5), uncertain=True)

    a = build_snapshot(exclusive_before=9, character_records=[certain, unsure])
    b = build_snapshot(exclusive_before=9, character_records=[unsure, certain])
    assert dict(a.character_states) == dict(b.character_states) == {"甲": "确定值"}


def test_character_without_source_is_dropped() -> None:
    records = [CharacterStateRecord("无源", "状态", None)]
    snap = build_snapshot(exclusive_before=9, character_records=records)
    assert snap.character_states == ()


# ---------------------------------------------------------------------------
# 不可变性与 Prompt 载荷
# ---------------------------------------------------------------------------


def test_snapshot_is_immutable_after_build() -> None:
    """构造完就与外部数据无关：改原列表不影响已发出的快照。"""
    facts = [_fact("a", 1)]
    snap = build_snapshot(exclusive_before=9, facts=facts)
    facts.append(_fact("b", 2))
    facts[0].__dict__  # 原始对象仍在，但快照不该跟着变
    assert [f.fact_id for f in snap.facts] == ["a"]


def test_prompt_payload_is_text_only() -> None:
    snap = build_snapshot(
        exclusive_before=9,
        facts=[_fact("a", 1, text="他死了")],
        character_records=[CharacterStateRecord("甲", "受伤", _src(2))],
    )
    payload = snap.as_prompt_payload()
    assert payload["facts"] == ["他死了"]
    assert payload["character_states"] == [{"name": "甲", "state": "受伤"}]


def test_verification_state_defaults_to_unverified() -> None:
    """没有原文引用的事实默认 UNVERIFIED——不编造定位。"""
    assert _fact("a", 1).verification_state == "UNVERIFIED"
