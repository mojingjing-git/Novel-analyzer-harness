"""vnext 图谱边：共现与语义关系分表（PLAN v3 §图谱）。

这些用例守的不是"函数能跑"，而是三条容易悄悄退化的性质：

1. 同章共现**永远**不升格成关系——两种边类型互不混用，也不存在合表入口；
2. 位置与来源一律照抄，不从"大概在哪"推；
3. 模型脏数据静默丢弃，程序错误大声报错。
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.vnext import graph  # noqa: E402  (sys.path 注入之后再 import 包)

CHAPTER_ID = "ch_0001"
CHAPTER_TEXT = "甲在城东，乙在城西。"


def _edge(
    a: str,
    b: str,
    positions: tuple[int, int] = (0, 5),
    *,
    chapter_id: str = CHAPTER_ID,
    chapter_ordinal: int = 1,
) -> graph.CooccurrenceEdge:
    return graph.CooccurrenceEdge(
        a=a,
        b=b,
        chapter_id=chapter_id,
        chapter_ordinal=chapter_ordinal,
        positions=positions,
    )


def _sorted_pair(a: str, b: str) -> tuple[str, str]:
    return graph.normalize_pair(a, b)


def _claim(**overrides: object) -> dict:
    """一条合法 claim；用 overrides 改坏某一个字段。"""
    base: dict = {
        "a": "甲",
        "b": "乙",
        "relation": "结盟",
        "description": "甲与乙在破庙结盟",
        "chapter_id": "ch_0001",
        "chapter_ordinal": 3,
        "start": 10,
        "end": 24,
        "content_hash": "abc123",
        "interaction": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 分表：共现不是关系
# ---------------------------------------------------------------------------


def test_cooccurrence_never_becomes_semantic_relationship() -> None:
    """同章出现两个人 ⇒ 只有共现边；把它的 dict 喂给语义接口，一条都不收。"""
    edges = graph.cooccurrences(CHAPTER_ID, 1, CHAPTER_TEXT, ["甲", "乙"])

    assert len(edges) == 1
    assert all(type(edge) is graph.CooccurrenceEdge for edge in edges)
    assert not any(isinstance(edge, graph.SemanticRelationship) for edge in edges)
    assert edges[0].edge_type == graph.COOCCURRENCE

    # 共现的"证据"里没有 interaction，语义接口必须一条都不认——这正是旧实现在
    # "甲在城东、乙在城西"上凭空编出关系描述的那一步。
    assert graph.semantic_relationships([edge.to_dict() for edge in edges]) == []


def test_no_shared_table_for_the_two_edge_kinds() -> None:
    """两种边没有共同基类；语义关系塞进 merge_edges 直接 TypeError。"""
    semantic = graph.semantic_relationships([_claim()])
    assert len(semantic) == 1
    assert type(semantic[0]) is graph.SemanticRelationship
    assert not isinstance(semantic[0], graph.CooccurrenceEdge)

    with pytest.raises(TypeError):
        graph.merge_edges([_edge("甲", "乙")], semantic)  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# normalize_pair
# ---------------------------------------------------------------------------


def test_normalize_pair_is_order_insensitive() -> None:
    assert graph.normalize_pair("甲", "乙") == graph.normalize_pair("乙", "甲")


def test_normalize_pair_sorted_and_idempotent() -> None:
    first, second = graph.normalize_pair("甲", "乙")
    assert first < second
    assert graph.normalize_pair(first, second) == (first, second)


def test_normalize_pair_rejects_same_name() -> None:
    with pytest.raises(ValueError):
        graph.normalize_pair("甲", "甲")


# ---------------------------------------------------------------------------
# cooccurrences
# ---------------------------------------------------------------------------


def test_name_absent_from_text_produces_no_edge() -> None:
    """丙没在正文里出现 ⇒ 含丙的边一条都不产出（而不是位置 0）。"""
    text = "甲与乙同行。"
    edges = graph.cooccurrences(CHAPTER_ID, 1, text, ["甲", "乙", "丙"])

    assert [(edge.a, edge.b) for edge in edges] == [graph.normalize_pair("甲", "乙")]
    assert all("丙" not in (edge.a, edge.b) for edge in edges)


def test_single_name_yields_no_edge() -> None:
    assert graph.cooccurrences(CHAPTER_ID, 1, "甲走了。", ["甲"]) == []


@pytest.mark.parametrize("names", [[], ("甲",)])
def test_empty_names_yields_no_edge(names: list[str] | tuple[str, ...]) -> None:
    assert graph.cooccurrences(CHAPTER_ID, 1, "甲走了，乙也走了。", names) == []


def test_empty_text_yields_no_edge() -> None:
    assert graph.cooccurrences(CHAPTER_ID, 1, "", ["甲", "乙"]) == []


def test_three_names_yield_three_unordered_pairs() -> None:
    edges = graph.cooccurrences(CHAPTER_ID, 1, "甲、乙、丙都在场。", ["甲", "乙", "丙"])

    assert len(edges) == 3
    pairs = {frozenset((edge.a, edge.b)) for edge in edges}
    assert pairs == {frozenset(("甲", "乙")), frozenset(("甲", "丙")), frozenset(("乙", "丙"))}
    assert all(edge.chapter_ordinal == 1 for edge in edges)


def test_positions_are_codepoint_offsets() -> None:
    """``🌙`` 在 Python 里是 1 个码点（UTF-16 会是 2），位置必须按码点算。"""
    text = "🌙甲向东，乙向西。"
    assert len("🌙") == 1
    assert text.find("甲") == 1
    assert text.find("乙") == 5

    edge = graph.cooccurrences(CHAPTER_ID, 1, text, ["甲", "乙"])[0]
    assert edge.positions == (text.find(edge.a), text.find(edge.b))
    assert text[edge.positions[0]] == edge.a
    assert text[edge.positions[1]] == edge.b


def test_positions_follow_normalized_pair_not_input_order() -> None:
    """名字按 (甲, 乙) 传进来，位置仍要跟着归一后的 (a, b) 走。"""
    text = "乙先到，甲后到。"
    edge = graph.cooccurrences(CHAPTER_ID, 1, text, ["甲", "乙"])[0]

    assert (edge.a, edge.b) == graph.normalize_pair("甲", "乙")
    assert edge.positions == (text.find(edge.a), text.find(edge.b))


def test_cooccurrence_is_independent_of_name_order() -> None:
    text = "甲、乙、丙都在场。"
    forward = graph.cooccurrences(CHAPTER_ID, 1, text, ["甲", "乙", "丙"])
    backward = graph.cooccurrences(CHAPTER_ID, 1, text, ["丙", "乙", "甲"])

    assert forward == backward


def test_duplicate_names_do_not_create_self_loop() -> None:
    edges = graph.cooccurrences(CHAPTER_ID, 1, CHAPTER_TEXT, ["甲", "甲", "乙"])

    assert len(edges) == 1
    assert edges[0].a != edges[0].b


def test_blank_names_do_not_fabricate_position_zero() -> None:
    """``"".find`` 恒为 0；空白名字必须被忽略，不能造出一条 position=0 的边。"""
    edges = graph.cooccurrences(CHAPTER_ID, 1, CHAPTER_TEXT, ["", "   ", "甲", "乙"])

    assert len(edges) == 1
    assert (edges[0].a, edges[0].b) == graph.normalize_pair("甲", "乙")
    assert edges[0].positions == (CHAPTER_TEXT.find(edges[0].a), CHAPTER_TEXT.find(edges[0].b))


# ---------------------------------------------------------------------------
# merge_edges
# ---------------------------------------------------------------------------


def _sample_edges() -> list[graph.CooccurrenceEdge]:
    return [
        _edge("甲", "乙", (0, 3), chapter_ordinal=1),
        _edge("甲", "丙", (0, 7), chapter_ordinal=2),
        _edge("乙", "丙", (3, 7), chapter_ordinal=2),
    ]


def test_merge_edges_is_idempotent() -> None:
    existing = _sample_edges()
    new = [_edge("甲", "乙", (0, 3), chapter_id="ch_0002", chapter_ordinal=3)]
    merged = graph.merge_edges(existing, new)

    assert graph.merge_edges(merged, new) == merged
    assert graph.merge_edges(merged, merged) == merged


def test_merge_edges_is_order_independent() -> None:
    a = [_edge("甲", "乙", (0, 3), chapter_ordinal=2)]
    b = [_edge("甲", "丙", (0, 7), chapter_ordinal=1)]

    assert graph.merge_edges(a, b) == graph.merge_edges(b, a)


def test_merge_edges_keeps_first_occurrence() -> None:
    """同一个 (a, b, chapter_id) 重复出现 ⇒ 先出现的那条胜出。"""
    first = _edge("乙", "甲", (0, 3))
    later = _edge("乙", "甲", (9, 30))

    assert graph.merge_edges([first], [later])[0].positions == (0, 3)
    assert graph.merge_edges([later], [first])[0].positions == (9, 30)


def test_merge_edges_dedupes_reversed_pair() -> None:
    """同一对、写法颠倒 ⇒ 一条边；位置跟着名字一起换序。"""
    left = _edge("乙", "甲", (0, 4))
    right = _edge("甲", "乙", (4, 0))

    merged = graph.merge_edges([left], [right])

    assert len(merged) == 1
    assert (merged[0].a, merged[0].b) == graph.normalize_pair("甲", "乙")
    assert merged[0].positions == (0, 4)


def test_merge_edges_sorts_by_ordinal_then_names() -> None:
    edges = [
        _edge("乙", "丙", (3, 7), chapter_ordinal=3),
        _edge("甲", "丙", (0, 7), chapter_ordinal=1),
        _edge("甲", "乙", (0, 3), chapter_ordinal=1),
    ]
    merged = graph.merge_edges([], edges)

    assert [(edge.chapter_ordinal, edge.a, edge.b) for edge in merged] == [
        (1, *_sorted_pair("甲", "丙")),
        (1, *_sorted_pair("甲", "乙")),
        (3, *_sorted_pair("乙", "丙")),
    ]


# ---------------------------------------------------------------------------
# semantic_relationships：只收有互动证据的
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("interaction", [False, None, 1, "true", 1.0])
def test_semantic_requires_interaction_true(interaction: object) -> None:
    """严格 ``is True``：缺字段、假值、``1``、``"true"`` 一律丢弃。"""
    assert graph.semantic_relationships([_claim(interaction=interaction)]) == []


def test_semantic_keeps_interaction_true() -> None:
    kept = graph.semantic_relationships([_claim()])

    assert len(kept) == 1
    assert kept[0].edge_type == graph.SEMANTIC
    assert kept[0].relation == "结盟"


@pytest.mark.parametrize(
    "field", ["chapter_id", "chapter_ordinal", "start", "end"]
)
def test_semantic_drops_missing_source_field(field: str) -> None:
    claim = _claim()
    del claim[field]

    assert graph.semantic_relationships([claim]) == []


def test_semantic_drops_self_relationship() -> None:
    assert graph.semantic_relationships([_claim(a="甲", b="甲")]) == []


@pytest.mark.parametrize("relation", ["", "   ", None])
def test_semantic_drops_empty_relation(relation: object) -> None:
    assert graph.semantic_relationships([_claim(relation=relation)]) == []


def test_semantic_preserves_evidence_offsets() -> None:
    """证据区间原样保留：start/end 不许被"修正"，也不许按引文长度重算。"""
    kept = graph.semantic_relationships([_claim(start=10, end=24, content_hash="abc123")])

    evidence = kept[0].evidence
    assert (evidence.start, evidence.end) == (10, 24)
    assert evidence.chapter_id == "ch_0001"
    assert evidence.chapter_ordinal == 3
    assert evidence.content_hash == "abc123"


def test_semantic_normalizes_pair() -> None:
    kept = graph.semantic_relationships([_claim(a="乙", b="甲")])

    assert (kept[0].a, kept[0].b) == graph.normalize_pair("甲", "乙")


def test_semantic_tolerates_garbage_entries() -> None:
    """模型产出里混进非 mapping / 空记录，不该中断整批。"""
    claims = [None, "结盟", 42, {}, _claim()]
    kept = graph.semantic_relationships(claims)  # type: ignore[list-item]

    assert len(kept) == 1
    assert kept[0].relation == "结盟"


def test_semantic_deduplicates_identical_claims() -> None:
    claim = _claim()
    assert graph.semantic_relationships([claim, dict(claim)]) == graph.semantic_relationships([claim])


def test_semantic_sorts_deterministically() -> None:
    later = _claim(a="丙", b="丁", start=40, end=50, chapter_ordinal=2)
    earlier = _claim(a="甲", b="乙", start=10, end=24, chapter_ordinal=1)

    ordered = graph.semantic_relationships([later, earlier])

    assert [
        (r.evidence.chapter_ordinal, r.evidence.start) for r in ordered
    ] == [(1, 10), (2, 40)]
    assert [(r.a, r.b) for r in ordered] == [
        graph.normalize_pair("甲", "乙"),
        graph.normalize_pair("丙", "丁"),
    ]


def test_semantic_drops_inverted_or_negative_range() -> None:
    assert graph.semantic_relationships([_claim(start=24, end=10)]) == []
    assert graph.semantic_relationships([_claim(start=-1, end=10)]) == []


def test_semantic_tolerates_missing_content_hash() -> None:
    """四个来源字段齐备即可入表；缺 content_hash 只意味着"正文版本未知"。"""
    claim = _claim()
    del claim["content_hash"]
    kept = graph.semantic_relationships([claim])

    assert len(kept) == 1
    assert kept[0].evidence.content_hash == ""


# ---------------------------------------------------------------------------
# 序列化与不可变
# ---------------------------------------------------------------------------


def test_to_dict_is_json_serializable() -> None:
    edge = graph.cooccurrences(CHAPTER_ID, 1, CHAPTER_TEXT, ["甲", "乙"])[0]
    relationship = graph.semantic_relationships([_claim()])[0]

    edge_payload = edge.to_dict()
    relationship_payload = relationship.to_dict()

    assert edge_payload["positions"] == list(edge.positions)
    assert isinstance(edge_payload["positions"], list)
    assert edge_payload["edge_type"] == graph.COOCCURRENCE
    assert relationship_payload["evidence"] == {
        "chapter_id": "ch_0001",
        "chapter_ordinal": 3,
        "start": 10,
        "end": 24,
        "content_hash": "abc123",
    }
    json.dumps({"cooccurrences": [edge_payload], "semantic": [relationship_payload]}, ensure_ascii=False)


def test_edges_are_frozen() -> None:
    edge = _edge("甲", "乙")
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.a = "丙"  # type: ignore[misc]


def test_cooccurrence_positions_accept_list_and_normalize_to_tuple() -> None:
    """JSON 读回来是 list；构造时应归一成 tuple，值不变。"""
    edge = graph.CooccurrenceEdge(
        a="甲",
        b="乙",
        chapter_id=CHAPTER_ID,
        chapter_ordinal=1,
        positions=[3, 9],  # type: ignore[arg-type]
    )

    assert edge.positions == (3, 9)
    assert isinstance(edge.positions, tuple)


def test_semantic_requires_source_ref_evidence() -> None:
    with pytest.raises(TypeError):
        graph.SemanticRelationship(
            a="甲",
            b="乙",
            relation="结盟",
            description="",
            evidence={"chapter_id": "ch_0001"},  # type: ignore[arg-type]
        )

# ---------------------------------------------------------------------------
# 证据哈希回填（父级追加）：模型不该给哈希，manifest 才是权威
# ---------------------------------------------------------------------------


def _rel_claim(**over):
    base = dict(
        a="甲", b="乙", relation="结盟", description="两人歃血为盟",
        chapter_id="ch_1", chapter_ordinal=3, start=10, end=24,
        content_hash="", interaction=True,
    )
    base.update(over)
    return base


def test_stamp_fills_hash_from_manifest() -> None:
    from backend.vnext.graph import semantic_relationships, stamp_evidence_hash

    rels = semantic_relationships([_rel_claim()])
    stamped, dropped = stamp_evidence_hash(rels, hashes_by_chapter={"ch_1": "realhash"})
    assert dropped == []
    assert stamped[0].evidence.content_hash == "realhash"


def test_stamp_overwrites_model_supplied_hash() -> None:
    """模型自己编的哈希一律被 manifest 覆盖——它没有资格定义正文版本。"""
    from backend.vnext.graph import semantic_relationships, stamp_evidence_hash

    rels = semantic_relationships([_rel_claim(content_hash="模型编的")])
    stamped, _ = stamp_evidence_hash(rels, hashes_by_chapter={"ch_1": "realhash"})
    assert stamped[0].evidence.content_hash == "realhash"


def test_stamp_drops_relationship_for_unknown_chapter() -> None:
    """章没有已知哈希 ⇒ 这条证据永远无法校验 ⇒ 丢掉并回报，而不是留着装样子。"""
    from backend.vnext.graph import semantic_relationships, stamp_evidence_hash

    rels = semantic_relationships([_rel_claim(chapter_id="ch_404")])
    stamped, dropped = stamp_evidence_hash(rels, hashes_by_chapter={"ch_1": "realhash"})
    assert stamped == []
    # 归一时 a/b 按字符串排序，故"甲、乙"会以"乙、甲"出现
    assert dropped == [("乙", "甲", "ch_404")]
