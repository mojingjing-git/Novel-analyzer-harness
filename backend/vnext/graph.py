"""vnext 图谱边：**共现**与**语义关系**分表存放，永不合表。

设计取舍：

1. **共现不是关系**。"同一章出现了两个人"只说明两条记录挨得近，不说明他们之间
   发生了什么。旧实现把同章共现直接当成关系，于是「甲在城东，乙在城西」被拼成
   「乙心情变好；甲心情变差」这样一句根本不存在的关系描述。本模块用两个**互不相干**
   的 dataclass 承载两种边：:class:`CooccurrenceEdge` 只记位置（事实层面的邻近），
   :class:`SemanticRelationship` 必须有 ``interaction is True`` 的显式互动证据与
   可定位的 :class:`~backend.vnext.knowledge.SourceRef`。
2. **两种边没有共同父类，也没有"合表"函数**。:func:`merge_edges` 只接受
   :class:`CooccurrenceEdge`，收到语义关系会 ``TypeError``——"永不合表"由类型守住，
   而不是靠调用方自觉。
3. **两类错误区别对待**：模型产出（claim）是脏数据，过滤优于中断——
   :func:`semantic_relationships` 对不合格记录静默丢弃（缺 ``interaction``、缺来源、
   ``a == b``、``relation`` 为空……），绝不抛异常；程序自身的错误（把同一个名字
   配成一对、把语义关系塞进共现表）一律抛异常，不能悄悄糊过去。
4. **确定性**：所有输出按固定键排序。同一批输入无论遍历顺序如何都得到同一个列表，
   否则"同样的输入、不同的顺序"会算出不同产物。

位置一律按 **Unicode 码点**计（即 Python ``str.find`` 的语义），与
:mod:`backend.vnext.fingerprint` 中 ``source_ref`` 偏移的口径一致。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import chain, combinations

from .knowledge import SourceRef

#: 共现边的 ``edge_type`` 取值。
COOCCURRENCE = "COOCCURRENCE"

#: 语义关系边的 ``edge_type`` 取值。
SEMANTIC = "SEMANTIC"

__all__ = [
    "COOCCURRENCE",
    "SEMANTIC",
    "CooccurrenceEdge",
    "SemanticRelationship",
    "Edge",
    "cooccurrences",
    "semantic_relationships",
    "normalize_pair",
    "merge_edges",
]


# ---------------------------------------------------------------------------
# 两种边（刻意不共享基类：混用应当在类型检查阶段就暴露）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CooccurrenceEdge:
    """两个实体在同一章同时出现。这是"事实层面的邻近"，不是关系。

    它只回答"谁和谁在同一章里出现过、各自第一次出现在第几个码点"，不回答
    "他们之间发生了什么"。要拿它当关系用，必须先有语义证据。
    """

    a: str
    b: str
    chapter_id: str
    chapter_ordinal: int
    positions: tuple[int, int]   # 两者在正文中的首次出现位置（码点）
    edge_type: str = COOCCURRENCE

    def __post_init__(self) -> None:
        # 只做形状归一（list -> tuple），不改变任何值：位置来自正文偏移，
        # 一旦动手就等于伪造证据。
        positions = self.positions
        if not isinstance(positions, tuple) or len(positions) != 2:
            positions = tuple(positions)
        if len(positions) != 2:
            raise ValueError(f"positions 必须恰好两个位置，收到 {self.positions!r}")
        for value in positions:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"positions 只能放整数码点位置，收到 {self.positions!r}")
            if value < 0:
                raise ValueError(f"positions 不能为负，收到 {self.positions!r}")
        if positions is not self.positions:
            object.__setattr__(self, "positions", (positions[0], positions[1]))

    def to_dict(self) -> dict:
        """可直接 ``json.dumps`` 的形状（``positions`` 转成 list）。"""
        return {
            "a": self.a,
            "b": self.b,
            "chapter_id": self.chapter_id,
            "chapter_ordinal": self.chapter_ordinal,
            "positions": list(self.positions),
            "edge_type": self.edge_type,
        }


@dataclass(frozen=True)
class SemanticRelationship:
    """有明确互动证据的关系。

    ``evidence`` 必须指向真实正文区间：没有可定位证据的"关系"就是模型的猜测，
    不该进图谱。``relation`` 是关系类型（"结盟"/"师徒"……），``description`` 是
    这一处的具体描述——两者都不是从共现推出来的。
    """

    a: str
    b: str
    relation: str
    description: str
    evidence: SourceRef
    edge_type: str = SEMANTIC

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, SourceRef):
            raise TypeError(
                "语义关系的 evidence 必须是 knowledge.SourceRef，"
                f"收到 {type(self.evidence).__name__}"
            )

    def to_dict(self) -> dict:
        """可直接 ``json.dumps`` 的形状（``evidence`` 展开成 dict）。"""
        return {
            "a": self.a,
            "b": self.b,
            "relation": self.relation,
            "description": self.description,
            "evidence": self.evidence.to_dict(),
            "edge_type": self.edge_type,
        }


#: 两种边唯一被允许的"共同"出现方式：类型注解里的联合类型，不是容器。
Edge = CooccurrenceEdge | SemanticRelationship


# ---------------------------------------------------------------------------
# 名字对归一
# ---------------------------------------------------------------------------


def normalize_pair(a: str, b: str) -> tuple[str, str]:
    """把无序名字对归一成 ``(较小, 较大)``（按码点排序）。

    ``(甲, 乙)`` 与 ``(乙, 甲)`` 是同一对：图上一条边只有一个身份，方向不该由
    调用方传参顺序决定。两端相同则不构成"一对"，直接 ``ValueError``。
    """
    if not isinstance(a, str) or not isinstance(b, str):
        raise TypeError(f"名字必须是 str，收到 {type(a).__name__} / {type(b).__name__}")
    if a == b:
        raise ValueError(f"两端实体相同，不构成一对: {a!r}")
    return (a, b) if a < b else (b, a)


# ---------------------------------------------------------------------------
# 共现表
# ---------------------------------------------------------------------------


def cooccurrences(
    chapter_id: str,
    chapter_ordinal: int,
    chapter_text: str,
    names: Sequence[str],
) -> list[CooccurrenceEdge]:
    """同一章里两两共现的边（**只记位置，不记关系**）。

    规则：

    * 每个名字用自己的**首次出现位置**（``str.find``，码点下标）；找不到就跳过该名字——
      找不到就是没在正文里出现，绝不退化成"位置 0"或"大概在开头"。
    * 每个无序对只产出一条边，``positions`` 与归一后的 ``(a, b)`` 顺序对应。
    * 名字重复只算一次（重复会凭空造出 ``(甲, 甲)`` 自环）；空白名字直接忽略
      （``"".find`` 恒为 0，会伪造位置 0）。
    * 结果按 ``(a, b)`` 排序：与传入名字的顺序无关。

    注意本函数**不做**最长匹配：``"甲三"`` 里的 ``"甲"`` 也算出现了一次。名字表
    来自人物库时这是既定口径；若要做别名/子串消歧，应由调用方在入库前处理。
    """
    if not chapter_text or not names:
        return []

    first_seen: dict[str, int] = {}
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        if name in first_seen:
            continue
        position = chapter_text.find(name)
        if position < 0:
            continue
        first_seen[name] = position

    if len(first_seen) < 2:
        return []

    # 按名字排序后再组合，天然满足 normalize_pair 的 (较小, 较大) 约定。
    ordered = sorted(first_seen)
    return [
        CooccurrenceEdge(
            a=a,
            b=b,
            chapter_id=chapter_id,
            chapter_ordinal=chapter_ordinal,
            positions=(first_seen[a], first_seen[b]),
        )
        for a, b in combinations(ordered, 2)
    ]


# ---------------------------------------------------------------------------
# 语义关系表
# ---------------------------------------------------------------------------

#: 一条语义关系必须具备的来源字段（``content_hash`` 刻意不在其中：它是正文本体的
#: 附加锚点，缺了不影响"这一段在哪"，缺了整条却丢掉一条真关系不划算）。
_REQUIRED_SOURCE_FIELDS = ("chapter_id", "chapter_ordinal", "start", "end")


def semantic_relationships(
    claims: Sequence[Mapping[str, object]],
) -> list[SemanticRelationship]:
    """从模型给出的候选里挑出**有互动证据**的关系。

    每条形如::

        {"a": "甲", "b": "乙", "relation": "结盟", "description": "...",
         "chapter_id": "ch_1", "chapter_ordinal": 3, "start": 10, "end": 24,
         "content_hash": "abc", "interaction": True}

    只接受 ``interaction is True``（严格同一性：``1`` / ``"true"`` 不算）且四个
    来源字段齐备、数值可用的记录；``a == b``、``relation`` 为空、来源区间
    倒置或为负 → **静默丢弃**。输入是模型产出，容错优于中断。

    保留的 ``a`` / ``b`` 用 :func:`normalize_pair` 归一；顺序按
    ``(chapter_ordinal, start, a, b, relation)`` 稳定排序；完全相同的重复记录只留一条。
    """
    kept: list[SemanticRelationship] = []
    seen: set[tuple] = set()

    for claim in claims:
        relationship = _parse_claim(claim)
        if relationship is None:
            continue
        evidence = relationship.evidence
        key = (
            relationship.a,
            relationship.b,
            relationship.relation,
            relationship.description,
            evidence.chapter_id,
            evidence.chapter_ordinal,
            evidence.start,
            evidence.end,
            evidence.content_hash,
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(relationship)

    kept.sort(
        key=lambda r: (
            r.evidence.chapter_ordinal,
            r.evidence.start,
            r.a,
            r.b,
            r.relation,
        )
    )
    return kept


def _parse_claim(claim: object) -> SemanticRelationship | None:
    """一条候选 → 一条语义关系；任何一项不合格就返回 ``None``（丢弃，不抛）。"""
    if not isinstance(claim, Mapping):
        return None

    # 严格 is True：模型常把"共现"写成 interaction: 1 或不写。宁可少一条，
    # 也不能让"看起来像互动"的记录混进语义表。
    if claim.get("interaction") is not True:
        return None

    a = _clean_text(claim.get("a"))
    b = _clean_text(claim.get("b"))
    if not a or not b or a == b:
        return None

    relation = _clean_text(claim.get("relation"))
    if not relation:
        return None

    # 来源字段"齐备"先按存在性判断：缺字段与类型不对是两种脏，但处置一样。
    if any(claim.get(field) is None for field in _REQUIRED_SOURCE_FIELDS):
        return None

    chapter_id = _clean_text(claim.get("chapter_id"))
    chapter_ordinal = _as_int(claim.get("chapter_ordinal"))
    start = _as_int(claim.get("start"))
    end = _as_int(claim.get("end"))
    if not chapter_id or chapter_ordinal is None or start is None or end is None:
        return None
    if start < 0 or end < start:
        return None

    a, b = normalize_pair(a, b)
    return SemanticRelationship(
        a=a,
        b=b,
        relation=relation,
        description=_clean_text(claim.get("description")),
        evidence=SourceRef(
            chapter_id=chapter_id,
            chapter_ordinal=chapter_ordinal,
            start=start,
            end=end,
            content_hash=_clean_text(claim.get("content_hash")),
        ),
    )


def _clean_text(value: object) -> str:
    """模型给的字符串字段：去首尾空白；非字符串当空值（后续按缺字段丢弃）。"""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _as_int(value: object) -> int | None:
    """把模型给的"看起来是整数"的值取成 int；取不到就 ``None``。

    容错但不错读：``bool`` 是 ``int`` 的子类，``True`` 不能当 1 用来定章号；
    整数值的 ``float``（``3.0``）与纯数字字符串（``"3"``）照收——模型常见手滑，
    丢掉一条真关系比多接受一种写法更贵。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# 合并（只合并共现表）
# ---------------------------------------------------------------------------


def merge_edges(
    existing: Sequence[CooccurrenceEdge],
    new: Sequence[CooccurrenceEdge],
) -> list[CooccurrenceEdge]:
    """按 ``(a, b, chapter_id)`` 去重合并共现边。

    * 同一个键出现多次时保留**先出现的那条**：``merge_edges(x, x) == x``，
      ``merge_edges(merge_edges(a, b), b) == merge_edges(a, b)``——重复执行不漂移。
    * 入参里的 ``a`` / ``b`` 位置颠倒会被就地归一（连同 ``positions`` 一起换序），
      所以"同一对、两种写法"不会被当成两条。
    * 结果按 ``(chapter_ordinal, a, b)`` 排序，``chapter_id`` 作为决胜键——否则
      "同一对在不同章、章号又恰好相同"的坏数据会让结果重新依赖输入顺序。

    只接受 :class:`CooccurrenceEdge`：语义关系用手滑的方式传进来会 ``TypeError``，
    不允许两种边在同一个列表里共存。
    """
    merged: dict[tuple[str, str, str], CooccurrenceEdge] = {}
    for edge in chain(existing, new):
        if not isinstance(edge, CooccurrenceEdge):
            raise TypeError(
                "merge_edges 只合并 CooccurrenceEdge（共现表）；"
                f"收到 {type(edge).__name__}——共现与语义关系永不合表"
            )
        normalized = _with_normalized_pair(edge)
        merged.setdefault(
            (normalized.a, normalized.b, normalized.chapter_id), normalized
        )
    return sorted(
        merged.values(),
        key=lambda e: (e.chapter_ordinal, e.a, e.b, e.chapter_id),
    )


def _with_normalized_pair(edge: CooccurrenceEdge) -> CooccurrenceEdge:
    """把边的 ``(a, b)`` 归一；换序时 ``positions`` 同步换序（位置属于名字）。"""
    a, b = normalize_pair(edge.a, edge.b)
    if (a, b) == (edge.a, edge.b):
        return edge
    first, second = edge.positions
    return replace(edge, a=a, b=b, positions=(second, first))


def stamp_evidence_hash(
    relationships: Sequence[SemanticRelationship],
    *,
    hashes_by_chapter: Mapping[str, str],
) -> tuple[list[SemanticRelationship], list[tuple[str, str, str]]]:
    """用**权威来源**（manifest 里的正文哈希）回填证据的 ``content_hash``。

    为什么必须回填：``content_hash`` 不是模型该给的东西——它由我方从 manifest 算出来。
    模型照抄一个哈希只会是编的，照抄不到就留空。空哈希意味着"这条证据永远无法校验"，
    于是它可以一直以"有来源"的样子留在库里，而引用校验永远碰不到它。

    因此这里做两件事：
    1. 用 ``hashes_by_chapter[chapter_id]`` **覆盖**（不是仅在空时填）——manifest 是唯一权威；
    2. 该章没有已知哈希的记录**丢弃**，并把 ``(a, b, chapter_id)`` 回给调用方去统计。
       丢弃而不是保留，是为了让"证据不全"变成可见的数字，而不是沉默的漏洞。
    """
    stamped: list[SemanticRelationship] = []
    dropped: list[tuple[str, str, str]] = []
    for relationship in relationships:
        chapter_id = relationship.evidence.chapter_id
        content_hash = hashes_by_chapter.get(chapter_id)
        if not content_hash:
            dropped.append((relationship.a, relationship.b, chapter_id))
            continue
        evidence = replace(relationship.evidence, content_hash=content_hash)
        stamped.append(replace(relationship, evidence=evidence))
    return stamped, dropped
