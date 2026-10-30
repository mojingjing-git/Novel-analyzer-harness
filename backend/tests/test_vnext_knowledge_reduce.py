"""归约规则的一致性：增量合并 ≡ 全量重建（S3-4/S3-5）。

这一组用例守的是**同一条不变量**：无论知识是分批到达还是一次给全，无论先到的是
哪一块，最终库与快照指纹必须完全相同。做不到这一点，"续跑"与"重跑"就会得到两个
都自称正确的结论——那是最难查的一类问题，因为两边都不报错。
"""

from __future__ import annotations

import pytest

from backend.vnext.knowledge import (
    CharacterStateRecord,
    FactRecord,
    KnowledgeStore,
    SourceRef,
    reduce_knowledge,
    snapshot_for_block,
)


def _src(chapter: int, start: int = 0, end: int = 10) -> SourceRef:
    return SourceRef(f"ch_{chapter:04d}", chapter, start, end, f"h{chapter}")


def _fact(fid: str, chapter: int, *, start: int = 0, text: str = "", state: str = "UNVERIFIED") -> FactRecord:
    return FactRecord(
        fact_id=fid,
        text=text or f"事实{fid}",
        source=_src(chapter, start),
        verification_state=state,
    )


def _char(name: str, state: str, chapter: int, start: int = 0, *, uncertain: bool = False) -> CharacterStateRecord:
    return CharacterStateRecord(name, state, _src(chapter, start), uncertain)


# ---------------------------------------------------------------------------
# 核心不变量：分批 ≡ 一次给全
# ---------------------------------------------------------------------------


def test_incremental_equals_bulk() -> None:
    blocks = [
        ("blk1", [_fact("f1", 1), _fact("f2", 2)], [_char("甲", "立", 1)]),
        ("blk2", [_fact("f3", 3)], [_char("甲", "困", 3), _char("乙", "现", 3)]),
        ("blk3", [_fact("f4", 4)], [_char("甲", "胜", 4)]),
    ]
    store = KnowledgeStore()
    for sig, facts, chars in blocks:
        store.add_block(block_signature=sig, facts=facts, character_records=chars)

    inc_facts, inc_chars = store.reduced()
    bulk_facts, bulk_chars = reduce_knowledge(
        [f for _, facts, _ in blocks for f in facts],
        [c for _, _, chars in blocks for c in chars],
    )
    assert inc_facts == bulk_facts
    assert inc_chars == bulk_chars


def test_block_order_does_not_matter() -> None:
    """先跑第 3 块还是先跑第 1 块，最终库必须一样。"""
    forward = KnowledgeStore()
    forward.add_block(block_signature="a", facts=[_fact("f1", 1)], character_records=[_char("甲", "立", 1)])
    forward.add_block(block_signature="b", facts=[_fact("f2", 5)], character_records=[_char("甲", "胜", 5)])

    backward = KnowledgeStore()
    backward.add_block(block_signature="b", facts=[_fact("f2", 5)], character_records=[_char("甲", "胜", 5)])
    backward.add_block(block_signature="a", facts=[_fact("f1", 1)], character_records=[_char("甲", "立", 1)])

    assert forward.reduced() == backward.reduced()
    assert forward.snapshot(exclusive_before=9).dependency_fingerprint() == (
        backward.snapshot(exclusive_before=9).dependency_fingerprint()
    )


def test_snapshot_after_incremental_matches_direct_snapshot() -> None:
    """增量攒出来的快照，与"直接拿全量数据构造"的快照指纹相同。"""
    facts = [_fact("f1", 1), _fact("f2", 2), _fact("f3", 3)]
    chars = [_char("甲", "立", 1), _char("甲", "胜", 3)]

    store = KnowledgeStore()
    store.add_block(block_signature="a", facts=facts[:2], character_records=chars[:1])
    store.add_block(block_signature="b", facts=facts[2:], character_records=chars[1:])

    direct = snapshot_for_block(block_first_ordinal=4, facts=facts, character_records=chars)
    assert store.snapshot(exclusive_before=4).dependency_fingerprint() == (
        direct.dependency_fingerprint()
    )


def test_adding_same_block_twice_is_idempotent() -> None:
    store = KnowledgeStore()
    payload = dict(
        block_signature="a", facts=[_fact("f1", 1)], character_records=[_char("甲", "立", 1)]
    )
    store.add_block(**payload)
    once = store.to_dict()
    store.add_block(**payload)
    assert store.to_dict() == once
    assert store.block_signatures == ("a",)


# ---------------------------------------------------------------------------
# 归约的具体规则
# ---------------------------------------------------------------------------


def test_facts_sorted_by_source_position_not_arrival() -> None:
    facts = [_fact("late", 9), _fact("early", 2), _fact("mid", 5)]
    reduced, _ = reduce_knowledge(facts)
    assert [f.fact_id for f in reduced] == ["early", "mid", "late"]


def test_same_fact_id_keeps_later_source() -> None:
    """同一条事实被两处提到：留来源更晚的那版。"""
    old = _fact("f1", 2, text="旧版本")
    new = _fact("f1", 7, text="新版本")
    reduced, _ = reduce_knowledge([new, old])
    assert reduced[0].text == "新版本"


def test_same_fact_id_same_source_prefers_located_evidence() -> None:
    """来源相同时，已定位原文的版本胜过未验证的版本。"""
    unverified = _fact("f1", 3, state="UNVERIFIED")
    located = _fact("f1", 3, state="EVIDENCE_LOCATED")
    reduced, _ = reduce_knowledge([unverified, located])
    assert reduced[0].verification_state == "EVIDENCE_LOCATED"


def test_unknown_source_fact_sorts_last() -> None:
    """来源未知的条目排在最后：它不该混进"按时间线"的窗口里排在前面。"""
    no_source = FactRecord(fact_id="ghost", text="无来源", source=None)
    reduced, _ = reduce_knowledge([no_source, _fact("f1", 5)])
    assert [f.fact_id for f in reduced] == ["f1", "ghost"]


def test_character_versions_are_all_kept() -> None:
    """人物记录不做"只留最新"——快照要按时间点取最新可见，历史版本必须留着。"""
    records = [_char("甲", "立", 1), _char("甲", "困", 3), _char("甲", "胜", 5)]
    _, chars = reduce_knowledge([], records)
    assert len(chars) == 3


def test_character_duplicate_position_deduped() -> None:
    records = [_char("甲", "立", 1), _char("甲", "立", 1)]
    _, chars = reduce_knowledge([], records)
    assert len(chars) == 1


def test_uncertain_does_not_win_over_certain_at_same_position() -> None:
    certain = _char("甲", "确定", 2, 5, uncertain=False)
    unsure = _char("甲", "存疑", 2, 5, uncertain=True)
    _, a = reduce_knowledge([], [unsure, certain])
    _, b = reduce_knowledge([], [certain, unsure])
    assert [r.state for r in a] == [r.state for r in b] == ["确定"]


# ---------------------------------------------------------------------------
# 快照随时间推进的性质
# ---------------------------------------------------------------------------


def test_snapshot_grows_monotonically_as_time_advances() -> None:
    """同一份知识，上界越往后，可见集合越大（不会因为推进而丢掉已见内容）。"""
    store = KnowledgeStore()
    store.add_block(
        block_signature="a",
        facts=[_fact("f1", 1), _fact("f2", 3), _fact("f3", 5)],
        character_records=[],
    )
    seen = [len(store.snapshot(exclusive_before=n).facts) for n in (1, 2, 4, 6)]
    assert seen == sorted(seen)
    assert seen[-1] == 3


def test_store_dump_is_replayable() -> None:
    """to_dict 的形态要能被重新读回并归约出同样的结果（落盘/重放的基础）。"""
    store = KnowledgeStore()
    store.add_block(
        block_signature="a",
        facts=[_fact("f1", 1, state="EVIDENCE_LOCATED")],
        character_records=[_char("甲", "立", 1)],
    )
    dump = store.to_dict()

    rebuilt = KnowledgeStore()
    rebuilt.add_block(
        block_signature=dump["blocks"][0],
        facts=[
            FactRecord(
                fact_id=f["fact_id"],
                text=f["text"],
                source=SourceRef(**{k: f["source"][k] for k in ("chapter_id", "chapter_ordinal", "start", "end", "content_hash")}),
                verification_state=f["verification_state"],
            )
            for f in dump["facts"]
        ],
        character_records=[
            CharacterStateRecord(
                r["name"],
                r["state"],
                SourceRef(**{k: r["source"][k] for k in ("chapter_id", "chapter_ordinal", "start", "end", "content_hash")}),
                r["uncertain"],
            )
            for r in dump["character_records"]
        ],
    )
    assert rebuilt.to_dict() == dump


def test_empty_store_snapshot_is_empty() -> None:
    store = KnowledgeStore()
    snap = store.snapshot(exclusive_before=50)
    assert snap.facts == () and snap.character_states == ()
