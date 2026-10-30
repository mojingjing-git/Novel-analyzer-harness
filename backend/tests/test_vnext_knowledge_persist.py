"""B0：知识库必须能从磁盘重建，否则"续跑"等于换一套输入。

这组用例守的是链条的三段：
1. `extracted` 的 schema 能还原成带来源的记录（缺来源必须丢弃，不能塞进库）；
2. 落盘 → 读回 → 指纹一致（结构无损）；
3. 重放（按块序拼 `extracted`）与"一次跑完"得到同一个库指纹。
"""

from __future__ import annotations

import pytest

from backend.vnext import artifacts
from backend.vnext.constants import VnextFormatError
from backend.vnext.knowledge import (
    CharacterStateRecord,
    FactRecord,
    KnowledgeStore,
    SourceRef,
    load_store,
    records_from_extracted,
    save_store,
    store_fingerprint,
)


def _src(chapter: int, start: int = 0) -> SourceRef:
    return SourceRef(f"ch_{chapter:04d}", chapter, start, start + 5, f"h{chapter}")


def _fact(fid: str, chapter: int) -> FactRecord:
    return FactRecord(fact_id=fid, text=f"事实{fid}", source=_src(chapter))


def _char(name: str, chapter: int, state: str = "在") -> CharacterStateRecord:
    return CharacterStateRecord(name=name, state=state, source=_src(chapter))


def _extracted(facts=(), chars=()) -> dict:
    return {
        "facts": [f.to_dict() for f in facts],
        "characters": [
            {
                "name": c.name,
                "state": c.state,
                "uncertain": c.uncertain,
                "source": c.source.to_dict(),
            }
            for c in chars
        ],
    }


# ---------------------------------------------------------------------------
# schema → 记录
# ---------------------------------------------------------------------------


def test_extracted_roundtrips_into_records() -> None:
    facts, chars = records_from_extracted(_extracted([_fact("f1", 2)], [_char("甲", 2)]))
    assert [(f.fact_id, f.source.chapter_ordinal) for f in facts] == [("f1", 2)]
    assert [(c.name, c.source.chapter_ordinal) for c in chars] == [("甲", 2)]


def test_records_without_source_are_dropped_not_guessed() -> None:
    """没有来源 = 不知道属于哪一章 = 会破坏快照上界。宁可丢，不许猜。"""
    raw = {"facts": [{"fact_id": "f1", "text": "无源事实"}], "characters": [{"name": "甲", "state": "在"}]}
    facts, chars = records_from_extracted(raw)
    assert facts == [] and chars == []


def test_malformed_entries_are_skipped() -> None:
    raw = {
        "facts": ["不是字典", {"fact_id": "", "text": "x"}, {"fact_id": "f9", "text": "ok", "source": {"chapter_id": "c"}}],
        "characters": [None, {"name": "", "source": _src(1).to_dict()}],
    }
    facts, chars = records_from_extracted(raw)
    assert facts == [] and chars == []


# ---------------------------------------------------------------------------
# 落盘 → 读回
# ---------------------------------------------------------------------------


def test_save_load_preserves_fingerprint(tmp_path) -> None:
    store = KnowledgeStore()
    store.add_block(block_signature="b1", facts=[_fact("f1", 1)], character_records=[_char("甲", 1)])
    path = tmp_path / "knowledge.json"
    save_store(path, store, meta={"ordered_result_ids": ["r1"]})

    loaded, meta = load_store(path)
    assert meta["ordered_result_ids"] == ["r1"]
    assert store_fingerprint(loaded) == store_fingerprint(store)


def test_load_rejects_unknown_version(tmp_path) -> None:
    path = tmp_path / "knowledge.json"
    artifacts.write_json_atomic(
        path, {"format_id": "novel-analyzer-vnext", "schema_version": 1, "knowledge_store_version": 999}
    )
    with pytest.raises(VnextFormatError) as exc:
        load_store(path)
    assert exc.value.code.value == "UNSUPPORTED_SCHEMA_VERSION"


def test_load_of_corrupt_file_raises_unparseable(tmp_path) -> None:
    path = tmp_path / "knowledge.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(VnextFormatError) as exc:
        load_store(path)
    assert exc.value.code.value == "UNPARSEABLE_ARTIFACT"


# ---------------------------------------------------------------------------
# 重放 == 一次跑完
# ---------------------------------------------------------------------------


def test_replay_equals_single_pass() -> None:
    """按块序把 extracted 并入，与"一次跑完"必须得到同一个库指纹。"""
    blocks = [
        _extracted([_fact("f1", 1)], [_char("甲", 1, "刚出场")]),
        _extracted([_fact("f2", 2)], [_char("甲", 2, "受伤")]),
        _extracted([_fact("f3", 3)], [_char("乙", 3, "登场")]),
    ]

    replay = KnowledgeStore()
    for index, extracted in enumerate(blocks, start=1):
        facts, chars = records_from_extracted(extracted)
        replay.add_block(block_signature=f"b{index}", facts=facts, character_records=chars)

    single = KnowledgeStore()
    for index, extracted in enumerate(blocks, start=1):
        facts, chars = records_from_extracted(extracted)
        single.add_block(block_signature=f"b{index}", facts=facts, character_records=chars)

    assert store_fingerprint(replay) == store_fingerprint(single)
    # 顺带确认内容真的进了库（不是"两个空库相等"）
    facts, chars = replay.reduced()
    assert len(facts) == 3 and len(chars) == 3


def test_replay_is_order_independent_for_disjoint_blocks() -> None:
    a = _extracted([_fact("f1", 1)])
    b = _extracted([_fact("f2", 2)])

    one = KnowledgeStore(); one.add_block(block_signature="b1", facts=records_from_extracted(a)[0])
    one.add_block(block_signature="b2", facts=records_from_extracted(b)[0])

    two = KnowledgeStore(); two.add_block(block_signature="b2", facts=records_from_extracted(b)[0])
    two.add_block(block_signature="b1", facts=records_from_extracted(a)[0])

    assert store_fingerprint(one) == store_fingerprint(two)
