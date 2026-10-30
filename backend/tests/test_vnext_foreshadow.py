"""vnext 稳定伏笔实体与"截至当前卷"的候选视图。

用例重点在**两个不该变的东西**与**一个必须严格的上界**：

* 身份不该变：同一处正文证据重跑不该改 ``occurrence_id``；补入更早证据不该改
  ``entity_id``。变了就意味着缓存与审计日志全部失效。
* 上界必须严格：只要某实体有一条证据落在当前卷及之后，它在这个视图里就**整条**
  不出现。这是防"未来信息泄漏"的唯一可靠做法——截掉越界几条再输出，模型仍会把那段
  被削过的历史当成完整历史。
* 输出不该依赖输入顺序：同一批证据三種遍历顺序必须给出完全相同的候选列表。
"""

from __future__ import annotations

import copy
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.vnext import foreshadow as fs
from backend.vnext.fingerprint import canonical_json
from backend.vnext.knowledge import SourceRef

BOOK = "book-a"


def _eid(index: int) -> str:
    """第 ``index`` 个实体的 ID（E1/E2/E3 即 index 1/2/3）。

    用 make_entity_id 生成而不是手写字符串，顺便把"ID 只由 (book_id, seed) 决定"
    这件事固定在夹具层：改 ID 公式时这里会一起变，不会悄悄写死一批假 ID。
    """
    return fs.make_entity_id(book_id=BOOK, seed_occurrence_id=f"anchor-{index}")


E1 = _eid(1)
E2 = _eid(2)
E3 = _eid(3)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _source(
    ordinal: int,
    *,
    chapter_id: str | None = None,
    start: int = 0,
    end: int = 10,
    content_hash: str = "hash-0",
) -> SourceRef:
    return SourceRef(
        chapter_id=chapter_id if chapter_id is not None else f"ch{ordinal:03d}",
        chapter_ordinal=ordinal,
        start=start,
        end=end,
        content_hash=content_hash,
    )


def _occ(
    entity_id: str,
    ordinal: int,
    *,
    book_id: str = BOOK,
    start: int = 0,
    end: int = 10,
    kind: str = "mention",
    quote: str = "那把伞",
    chapter_id: str | None = None,
    content_hash: str = "hash-0",
) -> fs.Occurrence:
    source = _source(
        ordinal,
        chapter_id=chapter_id,
        start=start,
        end=end,
        content_hash=content_hash,
    )
    return fs.Occurrence(
        occurrence_id=fs.make_occurrence_id(book_id=book_id, source=source, kind=kind),
        entity_id=entity_id,
        source=source,
        quote=quote,
        kind=kind,
    )


def _entities_of(occurrences: list[fs.Occurrence]) -> list[fs.Entity]:
    entities, _ = fs.register(book_id=BOOK, occurrences=occurrences)
    return entities


def _view(occurrences: list[fs.Occurrence], **kwargs) -> list[dict]:
    """一步到位：先把出现记录登记成实体，再出视图。"""
    return fs.cutoff_view(entities=_entities_of(occurrences), occurrences=occurrences, **kwargs)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


def test_schema_version_and_display_constant() -> None:
    """视图结构版本与展示长度都是约定值，改动必须显式发生（而不是随手改）。"""
    assert fs.SCHEMA_VERSION == 1
    assert fs.MAX_DISPLAY_ID == 12


# ---------------------------------------------------------------------------
# occurrence_id
# ---------------------------------------------------------------------------


def test_occurrence_id_is_full_64_hex_and_stable() -> None:
    """同一处正文、同一种 kind 反复算必须得到同一个 64 位十六进制串。"""
    source = _source(3, start=12, end=20, content_hash="h3")
    first = fs.make_occurrence_id(book_id=BOOK, source=source, kind="mention")
    second = fs.make_occurrence_id(book_id=BOOK, source=source, kind="mention")
    assert first == second
    assert len(first) == 64
    assert all(ch in "0123456789abcdef" for ch in first)


def test_occurrence_id_changes_with_kind() -> None:
    """同一锚点上的"提及"与"埋设"是两条证据，不能共用一个 ID。"""
    source = _source(3, start=12, end=20)
    assert fs.make_occurrence_id(
        book_id=BOOK, source=source, kind="mention"
    ) != fs.make_occurrence_id(book_id=BOOK, source=source, kind="plant")


def test_occurrence_id_changes_with_book_and_offsets() -> None:
    """书、章、正文哈希、区间都是身份的一部分：任一处变了就是另一条证据。"""
    base = _source(3, start=12, end=20, content_hash="h3")
    base_id = fs.make_occurrence_id(book_id=BOOK, source=base, kind="mention")
    assert base_id != fs.make_occurrence_id(book_id="book-b", source=base, kind="mention")
    assert base_id != fs.make_occurrence_id(
        book_id=BOOK, source=_source(3, start=12, end=21, content_hash="h3"), kind="mention"
    )
    assert base_id != fs.make_occurrence_id(
        book_id=BOOK, source=_source(3, start=12, end=20, content_hash="h9"), kind="mention"
    )


def test_occurrence_id_does_not_depend_on_entity() -> None:
    """occurrence_id 是"锚点身份"，不含 entity_id。

    同一处正文既可能是 E1 的提及，也可能是 E2 的埋设；若把 entity_id 塞进哈希，
    每次重新归并都会造出一批新 occurrence，幂等和去重立刻失效。
    """
    left = _occ(E1, 4, start=5, end=9)
    right = _occ(E2, 4, start=5, end=9)
    assert left.occurrence_id == right.occurrence_id


# ---------------------------------------------------------------------------
# entity_id
# ---------------------------------------------------------------------------


def test_entity_id_shape_and_determinism() -> None:
    """``fs_`` + 16 位十六进制，且只由 (book_id, seed_occurrence_id) 决定。"""
    entity_id = fs.make_entity_id(book_id=BOOK, seed_occurrence_id="occ-x")
    assert entity_id.startswith("fs_")
    assert len(entity_id) == len("fs_") + 16
    assert all(ch in "0123456789abcdef" for ch in entity_id[3:])
    assert entity_id == fs.make_entity_id(book_id=BOOK, seed_occurrence_id="occ-x")
    assert entity_id != fs.make_entity_id(book_id=BOOK, seed_occurrence_id="occ-y")
    assert entity_id != fs.make_entity_id(book_id="book-b", seed_occurrence_id="occ-x")


def test_earlier_evidence_keeps_entity_id_but_moves_first_seen() -> None:
    """补入更早证据后：``entity_id`` 不变，视图里的 ``first_seen_ordinal`` 变小。

    这是本模块存在的理由。ID 里不含章号/序号，所以后来发现"其实第 2 章就埋过"时，
    引用不必改号；变化的只是视图可见范围。
    """
    anchor = fs.make_entity_id(book_id=BOOK, seed_occurrence_id="occ-first-seen")
    only_late = [_occ(anchor, 5)]
    entities, occurrences = fs.register(book_id=BOOK, occurrences=only_late)
    before = fs.cutoff_view(entities=entities, occurrences=occurrences, cutoff_ordinal=11)
    assert before[0]["entity_id"] == anchor
    assert before[0]["first_seen_ordinal"] == 5

    with_earlier = only_late + [_occ(anchor, 2)]
    entities, occurrences = fs.register(book_id=BOOK, occurrences=with_earlier)
    after = fs.cutoff_view(entities=entities, occurrences=occurrences, cutoff_ordinal=11)
    assert after[0]["entity_id"] == anchor
    assert after[0]["first_seen_ordinal"] == 2
    assert after[0]["mention_count"] == 2


# ---------------------------------------------------------------------------
# candidate_id
# ---------------------------------------------------------------------------


def test_candidate_id_shape_and_sensitivity() -> None:
    """候选 ID 由"实体 × 卷起点 × 批号"决定：换卷或换批就是另一条候选。"""
    base = fs.make_candidate_id(entity_id=E1, volume_start_ordinal=3, batch_index=1)
    assert base.startswith("cd_")
    assert len(base) == len("cd_") + 16
    assert base == fs.make_candidate_id(entity_id=E1, volume_start_ordinal=3, batch_index=1)
    assert base != fs.make_candidate_id(entity_id=E2, volume_start_ordinal=3, batch_index=1)
    assert base != fs.make_candidate_id(entity_id=E1, volume_start_ordinal=4, batch_index=1)
    assert base != fs.make_candidate_id(entity_id=E1, volume_start_ordinal=3, batch_index=2)


def test_display_id_is_a_lossy_projection() -> None:
    """展示串只削前缀与前 12 位，可读但不可当键。"""
    full = fs.make_entity_id(book_id=BOOK, seed_occurrence_id="anchor-1")
    shown = fs.display_id(full)
    assert shown == full[len("fs_") : len("fs_") + fs.MAX_DISPLAY_ID]
    assert len(shown) == fs.MAX_DISPLAY_ID
    assert fs.display_id("raw-identifier-value") == "raw-identifi"


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def test_register_seed_is_earliest_not_first_in_list() -> None:
    """seed 取故事顺序最早的那条，且与列表顺序无关（两种顺序同结果）。"""
    batch = [_occ(E1, 9), _occ(E1, 3), _occ(E1, 7)]
    two_entities = batch + [_occ(E2, 1)]

    shuffled = list(two_entities)
    random.Random(7).shuffle(shuffled)

    first_entities, _ = fs.register(book_id=BOOK, occurrences=two_entities)
    second_entities, _ = fs.register(book_id=BOOK, occurrences=shuffled)
    assert [e.entity_id for e in first_entities] == [
        e.entity_id for e in second_entities  # already sorted by entity_id
    ]

    seeds = {e.entity_id: e.seed_occurrence_id for e in first_entities}
    assert seeds[E1] == _occ(E1, 3).occurrence_id
    assert seeds[E2] == _occ(E2, 1).occurrence_id
    # 列表首项是第 9 章那条，若实现取"第一个"，这里会失败。
    assert seeds[E1] != _occ(E1, 9).occurrence_id


def test_register_is_idempotent() -> None:
    """同一批反复登记：实体数量不变、seed 不变；把输出再喂回来也稳定。"""
    batch = [_occ(E1, 2), _occ(E1, 8), _occ(E2, 4)]
    once_entities, once_occurrences = fs.register(book_id=BOOK, occurrences=batch)
    twice_entities, twice_occurrences = fs.register(book_id=BOOK, occurrences=batch)
    assert once_entities == twice_entities
    assert once_occurrences == twice_occurrences

    third_entities, third_occurrences = fs.register(
        book_id=BOOK, occurrences=list(once_occurrences)
    )
    assert third_entities == once_entities
    assert third_occurrences == once_occurrences


def test_register_uses_entity_hints_with_empty_default() -> None:
    """``entity_hints`` 缺失时 motif 记空串；给了就用给的那个。"""
    batch = [_occ(E1, 2), _occ(E2, 3)]
    entities, _ = fs.register(
        book_id=BOOK, occurrences=batch, entity_hints={E2: "反复出现的红伞"}
    )
    motifs = {e.entity_id: e.motif for e in entities}
    assert motifs[E1] == ""
    assert motifs[E2] == "反复出现的红伞"


def test_register_returns_sorted_output() -> None:
    """实体按 entity_id 排序；出现记录按 (entity_id, 章序, 起点) 排序。"""
    unsorted_batch = [_occ(E3, 1), _occ(E2, 5), _occ(E1, 9), _occ(E2, 2)]
    entities, occurrences = fs.register(book_id=BOOK, occurrences=unsorted_batch)
    assert [e.entity_id for e in entities] == sorted([E1, E2, E3])
    keys = [
        (occurrence.entity_id, occurrence.source.chapter_ordinal, occurrence.source.start)
        for occurrence in occurrences
    ]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# cutoff_view：排他上界与整条排除
# ---------------------------------------------------------------------------


def test_cutoff_is_exclusive_one_to_ten_take_five() -> None:
    """第 1–10 章各有证据、cutoff=5 → 视图里只有 1–4，"本卷第一章"自身不算已读。

    注意这里每章用一个独立实体：若做成"一个实体在第 1..10 章都出现"，它会因为
    "有越界证据"而整条消失（见下一条用例），那就测不到上界的排他性了。
    """
    by_chapter = {ordinal: _eid(ordinal) for ordinal in range(1, 11)}
    batch = [_occ(by_chapter[ordinal], ordinal) for ordinal in range(1, 11)]
    rows = _view(batch, cutoff_ordinal=5)

    assert [row["first_seen_ordinal"] for row in rows] == [1, 2, 3, 4]
    assert [row["entity_id"] for row in rows] == [by_chapter[o] for o in (1, 2, 3, 4)]
    assert all(row["mention_count"] == 1 for row in rows)
    assert all(row["span"] == 1 for row in rows)
    assert batch[4].source.chapter_ordinal == 5  # 第 5 章那条确实不在视图里
    assert by_chapter[5] not in {row["entity_id"] for row in rows}


def test_future_evidence_does_not_erase_past_view() -> None:
    """前缀不变性：追加第 12 章的出现记录，**不能**让同一个早期视图变空。

    2026-10-30 复审推翻了原先的"整条排除"：过去的内容没有变，过去能查询到的视图就不该变。
    截止点之后的记录不进视图，但它不抹掉实体在截止点之前的出现——属性按可见记录重算。
    """
    before = _view([_occ(E1, 3), _occ(E2, 2)], cutoff_ordinal=11)
    after = _view([_occ(E1, 3), _occ(E1, 12), _occ(E2, 2)], cutoff_ordinal=11)

    assert [row["entity_id"] for row in before] == [row["entity_id"] for row in after]
    assert after[0]["entity_id"] == E2          # E2 两次出现、跨度更大，排在前面
    e1_row = next(row for row in after if row["entity_id"] == E1)
    assert e1_row["mention_count"] == 1         # 只看可见记录
    assert e1_row["last_seen_ordinal"] == 3     # 第 12 章那条不进视图
    assert e1_row["span"] == 1


def test_visible_attributes_ignore_future_occurrences() -> None:
    """跨度与首末次都按可见记录算：第 12 章的证据不能让 span 变成 10。"""
    rows = _view([_occ(E1, 3), _occ(E1, 12)], cutoff_ordinal=11)
    assert len(rows) == 1
    assert rows[0]["first_seen_ordinal"] == 3
    assert rows[0]["last_seen_ordinal"] == 3
    assert rows[0]["span"] == 1


def test_mention_count_only_counts_visible_occurrences() -> None:
    """``mention_count`` 是"可见证据条数"，不是"全量条数"。

    E2 在第 2 章与第 8 章各出现一次，cutoff=4 时只有第 2 章可见 ⇒ 计 1 次，
    而不是"整条消失"（旧行为）或"计 2 次"（按全量算）。
    """
    batch = [_occ(E1, 1), _occ(E1, 2), _occ(E1, 3), _occ(E2, 2), _occ(E2, 8)]
    rows = _view(batch, cutoff_ordinal=4)
    by_id = {row["entity_id"]: row for row in rows}
    assert by_id[E1]["mention_count"] == 3
    assert by_id[E2]["mention_count"] == 1

def test_evidence_is_in_story_order_regardless_of_input_order() -> None:
    """``evidence`` 按故事顺序给出，不按输入顺序。"""
    batch = [_occ(E1, 3), _occ(E1, 1), _occ(E1, 2)]
    row = _view(batch, cutoff_ordinal=9)[0]
    assert row["evidence"] == [_occ(E1, ordinal).occurrence_id for ordinal in (1, 2, 3)]


def test_min_mentions_filters_low_frequency_entities() -> None:
    """``min_mentions`` 在算完属性之后再筛，只留够次数的实体。"""
    batch = [_occ(E1, 1), _occ(E1, 2), _occ(E1, 3), _occ(E2, 2)]
    assert {row["entity_id"] for row in _view(batch, cutoff_ordinal=9)} == {E1, E2}
    rows = _view(batch, cutoff_ordinal=9, min_mentions=2)
    assert [row["entity_id"] for row in rows] == [E1]


def test_view_is_identical_under_three_input_orders() -> None:
    """三种遍历顺序（原序 / 逆序 / 随机）必须给出完全相同的候选列表。"""
    batch = [
        _occ(E1, 2),
        _occ(E1, 6),
        _occ(E2, 2),
        _occ(E2, 3),
        _occ(E2, 9),
        _occ(E3, 7),
    ]
    shuffled = list(batch)
    random.Random(11).shuffle(shuffled)

    forward = fs.cutoff_view(
        entities=_entities_of(batch), occurrences=batch, cutoff_ordinal=10
    )
    backward = fs.cutoff_view(
        entities=_entities_of(list(reversed(batch))),
        occurrences=list(reversed(batch)),
        cutoff_ordinal=10,
    )
    mixed = fs.cutoff_view(
        entities=_entities_of(shuffled), occurrences=shuffled, cutoff_ordinal=10
    )
    assert forward == backward == mixed
    assert json.dumps(forward, ensure_ascii=False)


def test_view_orders_by_mentions_then_importance() -> None:
    """排序：次数多者优先；同次数时 span 大者（importance 高）优先。"""
    batch = [
        _occ(E1, 1),
        _occ(E1, 2),
        _occ(E1, 3),
        _occ(E2, 1),
        _occ(E2, 9),  # 2 次但跨度 9，importance = 4 + 9 = 13
        _occ(E3, 5),  # 1 次
    ]
    rows = _view(batch, cutoff_ordinal=10)
    assert [row["entity_id"] for row in rows] == [E1, E2, E3]
    assert fs.importance_score(mention_count=3, span=3) == 9
    assert fs.importance_score(mention_count=3, span=99) == 26  # span 封顶 20


def test_max_entities_truncates_after_sorting() -> None:
    """``max_entities`` 在排序之后截断，留下的是排在前面的那些（且不报告截断）。"""
    batch = [
        _occ(E1, 1),
        _occ(E1, 2),
        _occ(E1, 3),
        _occ(E2, 1),
        _occ(E2, 2),
        _occ(E3, 1),
    ]
    full = _view(batch, cutoff_ordinal=9)
    truncated = _view(batch, cutoff_ordinal=9, max_entities=2)
    assert [row["entity_id"] for row in truncated] == [
        row["entity_id"] for row in full[:2]
    ]
    assert len(truncated) == 2
    assert _view(batch, cutoff_ordinal=9, max_entities=0) == []


def test_duplicate_occurrence_is_counted_once() -> None:
    """同一条证据被重复导入两次，仍然只算"提过一次"。"""
    once = _occ(E1, 4)
    rows = _view([once, once, _occ(E1, 5)], cutoff_ordinal=9)
    assert rows[0]["mention_count"] == 2
    assert rows[0]["evidence"] == [_occ(E1, 4).occurrence_id, _occ(E1, 5).occurrence_id]


def test_entities_sharing_the_same_anchor_stay_distinguishable() -> None:
    """两个实体落在同一章、同一区间：视图里仍按 entity_id 分成两条。

    注意它们的 ``occurrence_id`` 相同（锚点身份不含实体），所以去重必须按
    ``(entity_id, occurrence_id)`` 做，否则后一个实体的证据会被当成重复吞掉。
    """
    batch = [_occ(E1, 2, start=5, end=9), _occ(E2, 2, start=5, end=9)]
    rows = _view(batch, cutoff_ordinal=9)
    assert len(rows) == 2
    assert {row["entity_id"] for row in rows} == {E1, E2}
    assert all(row["mention_count"] == 1 for row in rows)
    assert all(len(row["evidence"]) == 1 for row in rows)
    assert batch[0].occurrence_id == batch[1].occurrence_id


def test_span_is_one_within_a_single_chapter() -> None:
    """同一章内两条出现 → span = 1（不是 0，也不是 2）。"""
    batch = [_occ(E1, 4, start=0, end=5), _occ(E1, 4, start=20, end=30)]
    row = _view(batch, cutoff_ordinal=9)[0]
    assert row["mention_count"] == 2
    assert row["first_seen_ordinal"] == row["last_seen_ordinal"] == 4
    assert row["span"] == 1


def test_empty_inputs_yield_empty_view() -> None:
    """空输入返回空列表（不抛错、不返回 None）。"""
    assert fs.cutoff_view(entities=[], occurrences=[], cutoff_ordinal=5) == []
    assert fs.cutoff_view(
        entities=_entities_of([]), occurrences=[], cutoff_ordinal=5
    ) == []
    # 有实体但一条证据都没有：无论 min_mentions 取多少都不进视图（span 无定义）。
    orphan = fs.Entity(
        entity_id=E1, book_id=BOOK, motif="", seed_occurrence_id="occ-1"
    )
    assert fs.cutoff_view(entities=[orphan], occurrences=[], cutoff_ordinal=5) == []
    assert (
        fs.cutoff_view(
            entities=[orphan], occurrences=[], cutoff_ordinal=5, min_mentions=0
        )
        == []
    )


def test_cutoff_at_or_below_one_yields_empty_view() -> None:
    """cutoff <= 1 时任何证据都落在上界之外，视图必为空。"""
    batch = [_occ(E1, ordinal) for ordinal in (1, 2, 3)]
    assert _view(batch, cutoff_ordinal=1) == []
    assert _view(batch, cutoff_ordinal=0) == []


def test_view_rows_are_json_friendly_with_exact_keys() -> None:
    """每项就是那 7 个键，且整份视图可直接 JSON 序列化（不含 importance_score）。"""
    rows = _view([_occ(E1, 1), _occ(E1, 2)], cutoff_ordinal=9)
    assert set(rows[0]) == {
        "entity_id",
        "motif",
        "mention_count",
        "first_seen_ordinal",
        "last_seen_ordinal",
        "span",
        "evidence",
    }
    assert "importance_score" not in rows[0]
    assert json.loads(json.dumps(rows, ensure_ascii=False)) == rows


def test_view_does_not_mutate_inputs() -> None:
    """入参只读：不排序、不增删——调用方的列表在调用前后必须一模一样。"""
    batch = [_occ(E1, 9), _occ(E1, 2), _occ(E2, 4)]
    entities = _entities_of(batch)
    batch_snapshot = copy.deepcopy(batch)
    entities_snapshot = copy.deepcopy(entities)

    fs.cutoff_view(entities=entities, occurrences=batch, cutoff_ordinal=10)
    fs.register(book_id=BOOK, occurrences=batch)

    assert batch == batch_snapshot
    assert entities == entities_snapshot
    # 传元组也不该炸：实现不得依赖 list 的可变性，也不得就地排序入参。
    assert (
        fs.cutoff_view(
            entities=tuple(entities), occurrences=tuple(batch), cutoff_ordinal=10
        )
        == fs.cutoff_view(entities=entities, occurrences=batch, cutoff_ordinal=10)
    )


def test_canonical_json_matches_fingerprint_convention() -> None:
    """ID 的输入格式沿用 ``fingerprint.canonical_json``（键排序、紧凑、不转义中文）。

    这一条守的是"跨实现可复算"：若哪天有人换成 ``json.dumps`` 默认参数，已落库的 ID
    全部对不上，而错误只表现为"缓存永不命中"。
    """
    payload = [BOOK, "ch003", "hash-0", 12, 20, "mention"]
    assert canonical_json(payload) == '["book-a","ch003","hash-0",12,20,"mention"]'


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
