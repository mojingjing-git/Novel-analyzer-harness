"""vnext 知识快照：逐块分析时模型"能看到什么"的唯一来源。

三条硬规则（PLAN v2 §4 S3 第 1–3、6 条）：

1. **显式上界，不用"最新"**。快照由 ``exclusive_before`` 故事位置界定：只纳入
   来源范围**完全早于**该位置的记录。旧实现按"当前最大章号"取全量 KB，于是同一份
   输入在不同进度下会给出不同前情——这既是不可复现的来源，也是未来信息泄漏的入口。
2. **未知范围一律排除**。记录没有可定位的来源（缺章号、缺正文哈希）时不猜它"大概靠前"，
   直接不进快照。宁少不多：漏掉一条事实只是保守，塞进去一条未来事实会污染结论。
3. **内容与覆盖元数据是同一次读取**。快照是**不可变值**：构造时把记录复制进元组，
   之后任何外部改动都影响不到已发出的快照。仅 deepcopy 内容而另外去读一遍元数据，
   会读到"另一个时刻"的覆盖范围。

另外，快照自带依赖指纹（:meth:`KnowledgeSnapshot.dependency_fingerprint`），
它就是块级依赖链里"上下文"那一项的输入——快照变了，依赖它的块自然失效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .fingerprint import dependency_fingerprint as _fingerprint_dependencies

#: 快照构造规则的版本。改"哪些记录算可见"的规则就 +1。
SNAPSHOT_STRATEGY_VERSION = 1


# ---------------------------------------------------------------------------
# 来源
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """一条记录的真实来源：哪一章、哪一段、哪一版正文。"""

    chapter_id: str
    chapter_ordinal: int
    start: int
    end: int
    content_hash: str

    def to_dict(self) -> dict:
        return {
            "chapter_id": self.chapter_id,
            "chapter_ordinal": self.chapter_ordinal,
            "start": self.start,
            "end": self.end,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class FactRecord:
    """一条"已经发生的事"。

    ``verification_state`` 刻意只有两档：

    * ``UNVERIFIED``：模型说了，但没给可定位的原文引用；
    * ``EVIDENCE_LOCATED``：有引用，且引用落在真实正文上（由调用方校验后标）。

    没有"高置信"之类的第三档——置信度是模型的自我评价，不是事实的属性。
    """

    fact_id: str
    text: str
    source: SourceRef | None = None
    verification_state: str = "UNVERIFIED"

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "text": self.text,
            "source": self.source.to_dict() if self.source else None,
            "verification_state": self.verification_state,
        }


@dataclass(frozen=True)
class CharacterStateRecord:
    """人物状态的一条来源版本。

    同一人物可以有多条，按来源位置排序后**只允许较新的有效来源覆盖**。
    """

    name: str
    state: str
    source: SourceRef | None = None
    uncertain: bool = False


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeSnapshot:
    """某一故事位置之前的知识视图。不可变：构造完就与外部数据无关。"""

    exclusive_before: int
    facts: tuple[FactRecord, ...] = ()
    character_states: tuple[tuple[str, str], ...] = ()
    dropped_unknown_range: tuple[str, ...] = field(default=(), compare=False)

    def dependency_fingerprint(self) -> str:
        """这个快照作为"上下文"时对应的依赖指纹。

        只包含**实际进入快照**的内容（含来源章节与哈希），不含 ``exclusive_before``
        之外的东西——所以"同样的可见内容、不同的上界"应被视为同一份上下文。
        """
        return _fingerprint_dependencies(
            {
                "snapshot_strategy_version": SNAPSHOT_STRATEGY_VERSION,
                "facts": [f.to_dict() for f in self.facts],
                "character_states": [list(pair) for pair in self.character_states],
            }
        )

    def as_prompt_payload(self) -> dict:
        """给 Prompt 用的最小结构（不含内部指纹字段）。"""
        return {
            "facts": [f.text for f in self.facts],
            "character_states": [
                {"name": name, "state": state} for name, state in self.character_states
            ],
        }

    def to_dict(self) -> dict:
        return {
            "exclusive_before": self.exclusive_before,
            "facts": [f.to_dict() for f in self.facts],
            "character_states": [list(pair) for pair in self.character_states],
        }


def _max_ordinal(record_source: SourceRef | None) -> int | None:
    """记录所触及的最靠后的故事位置；无法确定就返回 ``None``（= 未知）。"""
    if record_source is None:
        return None
    return record_source.chapter_ordinal


def build_snapshot(
    *,
    exclusive_before: int,
    facts: Iterable[FactRecord] = (),
    character_records: Iterable[CharacterStateRecord] = (),
) -> KnowledgeSnapshot:
    """构造"第 ``exclusive_before`` 章之前"的知识快照。

    ``exclusive_before`` 是**排他**上界：传 5 表示第 1–4 章的内容可见，第 5 章及之后
    一律不可见。用"排他上界"而不是"包含上界"，是因为逐块分析时每块要的是"我之前"，
    写成包含上界会让第 N 块的边界写成 N-1，调用处极易差一。

    任何来源范围触及 ``exclusive_before`` 及之后的记录都会被排除——即使它同时含有
    更早的信息。整条记录要么可见，要么不可见，不做"部分可见"。
    """
    if exclusive_before < 1:
        raise ValueError(f"exclusive_before 必须 >= 1，收到 {exclusive_before}")

    visible_facts: list[FactRecord] = []
    dropped: list[str] = []
    for fact in facts:
        end = _max_ordinal(fact.source)
        if end is None:
            # 没有可定位来源：不猜它"大概靠前"，直接不给模型看
            dropped.append(fact.fact_id)
            continue
        if end < exclusive_before:
            visible_facts.append(fact)

    visible_states: list[CharacterStateRecord] = []
    for record in character_records:
        end = _max_ordinal(record.source)
        if end is None:
            continue
        if end < exclusive_before:
            visible_states.append(record)

    # 同一人物取来源位置最新的那条；位置相同再按稳定字段排序，保证结果确定。
    # "迟到章节的新人物"照样能进来——限制的是覆盖顺序，不是加入资格。
    latest: dict[str, CharacterStateRecord] = {}
    for record in visible_states:
        assert record.source is not None
        current = latest.get(record.name)
        if current is None or _newer(record, current):
            latest[record.name] = record

    ordered = tuple(
        (name, rec.state)
        for name, rec in sorted(latest.items(), key=lambda kv: kv[0])
    )

    return KnowledgeSnapshot(
        exclusive_before=exclusive_before,
        facts=tuple(sorted(visible_facts, key=lambda f: (f.source.chapter_ordinal, f.source.start, f.fact_id))),
        character_states=ordered,
        dropped_unknown_range=tuple(dropped),
    )


def _newer(candidate: CharacterStateRecord, current: CharacterStateRecord) -> bool:
    """判断 candidate 是否应当覆盖 current。

    规则：先比章号，再比段内位置；完全相同时"不确定"的一方不覆盖确定的。这样
    "同章两处描述互相矛盾"时不会因为遍历顺序不同得出不同结论。
    """
    assert candidate.source is not None and current.source is not None
    c_key = (candidate.source.chapter_ordinal, candidate.source.start)
    cur_key = (current.source.chapter_ordinal, current.source.start)
    if c_key != cur_key:
        return c_key > cur_key
    if candidate.uncertain != current.uncertain:
        return not candidate.uncertain
    return False


def snapshot_for_block(
    *,
    block_first_ordinal: int,
    facts: Iterable[FactRecord] = (),
    character_records: Iterable[CharacterStateRecord] = (),
) -> KnowledgeSnapshot:
    """逐块快照的便捷入口：'本块第一"章之前'。"""
    return build_snapshot(
        exclusive_before=block_first_ordinal,
        facts=facts,
        character_records=character_records,
    )


# ---------------------------------------------------------------------------
# 归约：增量合并、子集重建、最终落盘共用同一套规则
# ---------------------------------------------------------------------------


def _fact_sort_key(fact: FactRecord) -> tuple:
    """事实的稳定排序键：来源位置优先，其次 ID。

    时间线/事实窗口按**来源位置**取，不按"哪条后到"——否则重跑顺序一换，
    保留下来的条目就变了，同一份输入给出两份不同结论。
    """
    if fact.source is None:
        return (1, 0, 0, fact.fact_id)
    return (0, fact.source.chapter_ordinal, fact.source.start, fact.fact_id)


def _prefer_fact(candidate: FactRecord, current: FactRecord) -> bool:
    """同一条事实出现两个版本时该留哪个。

    规则：来源更晚的胜；来源相同时**已定位原文的胜**（证据更硬的版本优先）。
    都相同时保留先到的（幂等）。
    """
    if candidate.source is None or current.source is None:
        return current.source is None and candidate.source is not None
    c_key = (candidate.source.chapter_ordinal, candidate.source.start)
    cur_key = (current.source.chapter_ordinal, current.source.start)
    if c_key != cur_key:
        return c_key > cur_key
    if candidate.verification_state != current.verification_state:
        return candidate.verification_state == "EVIDENCE_LOCATED"
    return False


def _character_sort_key(record: CharacterStateRecord) -> tuple:
    if record.source is None:
        return (1, 0, 0, record.name)
    return (0, record.source.chapter_ordinal, record.source.start, record.name)


def reduce_knowledge(
    facts: Iterable[FactRecord] = (),
    character_records: Iterable[CharacterStateRecord] = (),
) -> tuple[tuple[FactRecord, ...], tuple[CharacterStateRecord, ...]]:
    """把任意顺序、可能重复的知识条目归约成规范形态。

    这一份规则被三处共用：增量合并、从磁盘重建快照、最终落盘。共用是**必须**的
    ——否则"边跑边存"与"重跑一次"会得到不同的库，而两者看起来都"正常"。
    """
    by_id: dict[str, FactRecord] = {}
    for fact in facts:
        current = by_id.get(fact.fact_id)
        if current is None or _prefer_fact(fact, current):
            by_id[fact.fact_id] = fact
    reduced_facts = tuple(sorted(by_id.values(), key=_fact_sort_key))

    # 人物记录保留**全部**版本（快照需要按时间点取"最新可见"），只做去重与排序。
    # 去重键按 (name, chapter, start)：同一处位置只留一条，重复到达幂等。
    seen: dict[tuple, CharacterStateRecord] = {}
    for record in character_records:
        if record.source is None:
            key = (record.name, -1, -1)
        else:
            key = (record.name, record.source.chapter_ordinal, record.source.start)
        current = seen.get(key)
        if current is None or (current.uncertain and not record.uncertain):
            seen[key] = record
    reduced_chars = tuple(sorted(seen.values(), key=_character_sort_key))
    return reduced_facts, reduced_chars


class KnowledgeStore:
    """知识的增量容器。

    存在的理由只有一个：让"分批到达"与"一次给全"算出**同一个库**。因此它不缓存
    任何派生状态（最新人物状态、可见集合），每次查询都走 :func:`reduce_knowledge`
    ——快、无状态偏移，且天然可重放。
    """

    def __init__(self) -> None:
        self._facts: dict[str, FactRecord] = {}
        self._characters: list[CharacterStateRecord] = []
        self._blocks: set[str] = set()

    def add_block(
        self,
        *,
        block_signature: str,
        facts: Iterable[FactRecord] = (),
        character_records: Iterable[CharacterStateRecord] = (),
    ) -> None:
        """并入一个块的知识。重复并入同一块是幂等的（结果不变）。"""
        self._blocks.add(block_signature)
        for fact in facts:
            current = self._facts.get(fact.fact_id)
            if current is None or _prefer_fact(fact, current):
                self._facts[fact.fact_id] = fact
        self._characters.extend(character_records)

    @property
    def block_signatures(self) -> tuple[str, ...]:
        return tuple(sorted(self._blocks))

    def reduced(self) -> tuple[tuple[FactRecord, ...], tuple[CharacterStateRecord, ...]]:
        facts, chars = reduce_knowledge(self._facts.values(), self._characters)
        return facts, chars

    def snapshot(self, *, exclusive_before: int) -> KnowledgeSnapshot:
        facts, chars = self.reduced()
        return build_snapshot(
            exclusive_before=exclusive_before, facts=facts, character_records=chars
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "KnowledgeStore":
        """与 :meth:`to_dict` 对称。只认 schema 内的两种记录，其余一律忽略。

        重放路径靠它：先按块序还原 ``extracted`` → 再整体并入。``block_signatures``
        丢失时按空集合处理，会让 ``add_block`` 的幂等判断失效但不影响正确性
        （重复并入本来就是幂等的）。
        """
        store = cls()
        for raw in data.get("facts", []) or []:
            source = _source_from_dict(raw.get("source")) if isinstance(raw, Mapping) else None
            if source is None:
                continue
            record = FactRecord(
                fact_id=str(raw["fact_id"]),
                text=str(raw["text"]),
                source=source,
                verification_state=str(raw.get("verification_state") or "UNVERIFIED"),
            )
            current = store._facts.get(record.fact_id)
            if current is None or _prefer_fact(record, current):
                store._facts[record.fact_id] = record
        for raw in data.get("characters", []) or []:
            source = _source_from_dict(raw.get("source")) if isinstance(raw, Mapping) else None
            if source is None:
                continue
            store._characters.append(
                CharacterStateRecord(
                    name=str(raw["name"]),
                    state=str(raw.get("state") or ""),
                    source=source,
                    uncertain=bool(raw.get("uncertain", False)),
                )
            )
        return store

    def to_dict(self) -> dict:
        facts, chars = self.reduced()
        return {
            "blocks": list(self.block_signatures),
            "facts": [f.to_dict() for f in facts],
            "character_records": [
                {
                    "name": r.name,
                    "state": r.state,
                    "source": r.source.to_dict() if r.source else None,
                    "uncertain": r.uncertain,
                }
                for r in chars
            ],
        }


# ---------------------------------------------------------------------------
# 落盘与重放（B0）：库必须能从磁盘重建，否则"续跑"等于换一套输入
# ---------------------------------------------------------------------------

#: 库结构版本。字段形状变化就 +1，旧文件按"不兼容"处理（走重放重建，不猜）。
KNOWLEDGE_STORE_VERSION = 1


def records_from_extracted(extracted: Mapping[str, Any]) -> tuple[list[FactRecord], list[CharacterStateRecord]]:
    """把结果里的 ``extracted`` 还原成记录。

    ``extracted`` 的 schema 是**定死**的（见 docs/data-contract-vnext.md §16）：
    ``{"facts": [FactRecord.to_dict()...], "characters": [CharacterStateRecord...]}``。
    缺 ``source`` 的条目一律丢弃——重放时拿不到来源就等于拿不到"这条事实属于哪一章"，
    塞进库里会让快照上界失效（那正是要消灭的静默降质）。
    """
    facts: list[FactRecord] = []
    for raw in extracted.get("facts", []) or []:
        if not isinstance(raw, Mapping):
            continue
        source = _source_from_dict(raw.get("source"))
        if source is None:
            continue
        fact_id = str(raw.get("fact_id") or "")
        text = str(raw.get("text") or "")
        if not fact_id or not text:
            continue
        facts.append(
            FactRecord(
                fact_id=fact_id,
                text=text,
                source=source,
                verification_state=str(raw.get("verification_state") or "UNVERIFIED"),
            )
        )

    characters: list[CharacterStateRecord] = []
    for raw in extracted.get("characters", []) or []:
        if not isinstance(raw, Mapping):
            continue
        source = _source_from_dict(raw.get("source"))
        if source is None:
            continue
        name = str(raw.get("name") or "")
        state = str(raw.get("state") or "")
        if not name:
            continue
        characters.append(
            CharacterStateRecord(
                name=name,
                state=state,
                source=source,
                uncertain=bool(raw.get("uncertain", False)),
            )
        )
    return facts, characters


def _source_from_dict(raw: object) -> SourceRef | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        return SourceRef(
            chapter_id=str(raw["chapter_id"]),
            chapter_ordinal=int(raw["chapter_ordinal"]),
            start=int(raw["start"]),
            end=int(raw["end"]),
            content_hash=str(raw["content_hash"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def store_fingerprint(store: "KnowledgeStore") -> str:
    """整库指纹（与排序无关，与上界无关）。用于 store_meta 与"重建是否一致"的比对。"""
    data = store.to_dict()
    return _fingerprint_dependencies(
        {
            "knowledge_store_version": KNOWLEDGE_STORE_VERSION,
            "facts": data.get("facts", []),
            "characters": data.get("characters", []),
        }
    )


def save_store(path: Path, store: "KnowledgeStore", *, meta: Mapping[str, Any]) -> None:
    """落盘库 + meta。调用方必须持有该书锁（见 ``project.commit_manifest``）。"""
    from .artifacts import stamp_payload, write_json_atomic

    payload = stamp_payload(
        {
            "knowledge_store_version": KNOWLEDGE_STORE_VERSION,
            "meta": dict(meta),
            "store": store.to_dict(),
            "store_fingerprint": store_fingerprint(store),
        }
    )
    write_json_atomic(Path(path), payload)


def load_store(path: Path) -> "tuple[KnowledgeStore, dict[str, Any]]":
    """读库 + meta。格式不对就抛 ``UNPARSEABLE_ARTIFACT``，由调用方决定是否重放重建。"""
    from .artifacts import read_json_artifact
    from .constants import ErrorCode, VnextFormatError

    data = read_json_artifact(Path(path))
    if int(data.get("knowledge_store_version", -1)) != KNOWLEDGE_STORE_VERSION:
        raise VnextFormatError(
            ErrorCode.UNSUPPORTED_SCHEMA_VERSION,
            f"知识库结构版本不匹配：{data.get('knowledge_store_version')!r}",
            path=str(path),
        )
    store = KnowledgeStore.from_dict(data.get("store") or {})
    return store, dict(data.get("meta") or {})
