"""vnext 稳定伏笔实体，以及"截至当前卷"的候选视图。

为什么单独一个模块（而不是塞进 ``knowledge.py``）：

* **身份要稳，展示要短**。一条伏笔实体一旦被用户看到、被候选引用、进了审计日志，
  它的 ID 就不能因为"后来又补进了一条更早的证据"而变号。所以实体 ID 只由
  ``(book_id, seed_occurrence_id)`` 决定，**不含**排序序号、不含章号、不含"第几个"——
  这些量都会随数据补入而漂移。补入更早证据的正确效果是：视图里的
  ``first_seen_ordinal`` 变小，``entity_id`` 不动。
* **视图必须能"整条排除"**。若某条实体的证据里还有一条落在当前卷及之后，把它算进
  "截至本卷"的视图就等于把未来信息喂给生成模型。只截掉那几条仍然输出，是同一种泄漏，
  只是更隐蔽——模型会看到一段被削过的历史，却以为那是全部。所以规则是：**只要有一条
  越界，整个实体在这个视图里不出现**。
* **排序必须与输入顺序无关**。同一批证据换个遍历顺序就换一个候选顺序，会让"同一次
  分析重跑"产出不同结果，缓存命中与人工校对全部作废。这里每个排序键都是全序
  （尾部用 ``entity_id`` / ``occurrence_id`` 兜底），不依赖 ``sorted`` 的稳定性。

本模块是纯函数：无 IO、无全局状态、不改入参；输出是 frozen dataclass 或 JSON 友好的
dict，可直接落库或塞进 Prompt 上下文。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, Sequence

from .fingerprint import canonical_json
from .knowledge import SourceRef

#: 本模块产出的视图结构版本。改"视图里放哪些字段/怎么排序"就 +1。
SCHEMA_VERSION = 1

#: 展示用短 ID 的长度。与 ``fingerprint.evidence_id`` 取同一长度，是为了界面上各种
#: ID 看起来一致；**它只是展示**，不能当键：12 位前缀在十万级数据上就会撞，且撞了
#: 不报错，只会悄悄把两条证据当成一条。
MAX_DISPLAY_ID = 12


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def _sha256_hex(text: str) -> str:
    """统一在这里 encode("utf-8") 后取 sha256。

    编码方式集中一处，避免调用方各自 ``.encode()``（默认编码不同平台可能不同）算出一份
    互不兼容的"同一个"ID。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_key(source: SourceRef) -> tuple[int, int, str]:
    """一条来源的"故事顺序"：章序 → 章内起始偏移 → 章 ID。

    末尾带 ``chapter_id`` 是为了让"同一章序、同一偏移"（不同分卷里章号重复的真实情况）
    也有确定顺序；没有它，排序结果就取决于输入顺序。
    """
    return (source.chapter_ordinal, source.start, source.chapter_id)


def _occurrence_key(occ: Occurrence) -> tuple[str, int, int, str, str]:
    """出现记录的全序键：实体 → 故事顺序 → occurrence_id。

    最后的 ``occurrence_id`` 兜底，保证"同一实体在同一章同一起点有两条记录"时顺序仍然
    确定——它们的引文可能不同，但身份不同，顺序不该随输入顺序抖动。
    """
    return (
        occ.entity_id,
        occ.source.chapter_ordinal,
        occ.source.start,
        occ.source.chapter_id,
        occ.occurrence_id,
    )


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------


def make_occurrence_id(*, book_id: str, source: SourceRef, kind: str) -> str:
    """一条"出现"的身份：完整 64 位十六进制 sha256。

    参与哈希的只有"这条证据锚在哪里"：书、章 ID、该章正文哈希、码点区间、kind。
    刻意**不含**引文文本（引文是证据内容，改了措辞但仍指向同一处正文，仍是同一条
    证据）；也刻意**不含**实体 ID——同一个锚点可以同时是实体 A 的"提及"和实体 B 的
    "埋设"，身份应当由锚点决定。若把 entity_id 塞进来，每次重新归并都会造出一批新
    occurrence，去重与幂等立刻失效。

    为什么返回完整 64 位而不是展示用的短串：短串只够给人看。拿短串当键，十万级规模上
    就会有两个不同锚点撞成同一条证据，而且不会报错。
    """
    payload = [
        book_id,
        source.chapter_id,
        source.content_hash,
        source.start,
        source.end,
        kind,
    ]
    return _sha256_hex(canonical_json(payload))


def make_entity_id(*, book_id: str, seed_occurrence_id: str) -> str:
    """伏笔实体身份：``"fs_" + 16 位十六进制``。

    只由"种子出现"决定，**不含**章号、排序序号、当前最大章号、"第几个实体"。原因：
    实体通常是先在某一处证据上被认出来，之后才陆续补入更早/更晚的佐证。ID 里一旦带上
    "第一次被认出的章号"或"遍历到它的次序"，补一条更早证据就会改号——收藏、候选、
    审计日志里的全部引用随即失效。

    注意：``seed_occurrence_id`` 是**实体被锚定的那条证据**，不是"每次重新扫描后最先
    出现的那条"。已经存在的实体补入更早证据时，上游应沿用原 seed（出现记录自带
    ``entity_id`` 即可），:func:`register` 不重算 ID。
    """
    digest = _sha256_hex(canonical_json([book_id, seed_occurrence_id]))
    return "fs_" + digest[:16]


def make_candidate_id(
    *,
    entity_id: str,
    volume_start_ordinal: int,
    batch_index: int,
) -> str:
    """候选身份：``"cd_" + 16 位十六进制``。

    由"哪个实体 × 哪一卷 × 第几批"决定。含 ``volume_start_ordinal`` 是为了让"同一实体
    在不同卷的候选"互不覆盖（它们回答的是不同问题）；含 ``batch_index`` 是为了让同一卷
    里多次生成（比如先出 5 条、再补 5 条）能各自留档，后一批不会悄悄顶掉前一批。
    不含时间戳：重跑同一批应当命中同一批候选，而不是造出一堆孤儿。
    """
    digest = _sha256_hex(
        canonical_json([entity_id, volume_start_ordinal, batch_index])
    )
    return "cd_" + digest[:16]


def display_id(identifier: str, *, max_display_id: int = MAX_DISPLAY_ID) -> str:
    """把完整 ID 缩成展示串：去掉类型前缀，取前 ``max_display_id`` 位。

    仅供界面与日志阅读。**不得**用它做键、恢复、去重或写进任何被比对的结构——它是
    完整 ID 的有损投影，两份不同的完整 ID 完全可以有同一个展示串。
    """
    body = identifier
    for prefix in ("fs_", "cd_"):
        if body.startswith(prefix):
            body = body[len(prefix) :]
            break
    return body[:max_display_id]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Occurrence:
    """一条"这条伏笔在正文里出现过"的证据。

    ``kind`` 只有两档：``mention``（顺带提到）与 ``plant``（正式的埋设）。不设"疑似"：
    置信度是模型的自我评价，不是证据的属性；要表达"不太确定"，靠的是有没有可定位的
    ``source``，而不是一个额外的档位。

    frozen：证据是不可变值。列表里一条被就地改过，下游所有已发出的视图与缓存就都
    对不上它自己的指纹了。
    """

    occurrence_id: str
    entity_id: str
    source: SourceRef
    quote: str
    kind: str = "mention"


@dataclass(frozen=True)
class Entity:
    """一条伏笔实体：同一桩"还没说破的事"在全书里的身份。"""

    entity_id: str
    book_id: str
    motif: str
    seed_occurrence_id: str


@dataclass(frozen=True)
class Candidate:
    """某实体在某一卷的候选（通常是给模型/人看的问题或解读）。"""

    candidate_id: str
    entity_id: str
    batch_index: int
    volume_start_ordinal: int
    answer: str


# ---------------------------------------------------------------------------
# 登记
# ---------------------------------------------------------------------------


def register(
    *,
    book_id: str,
    occurrences: Sequence[Occurrence],
    entity_hints: Mapping[str, str] | None = None,
) -> tuple[list[Entity], list[Occurrence]]:
    """把一批出现记录登记成"实体 + 去重排序后的出现记录"。

    * 按 ``occurrence.entity_id`` 归组；实体 ID 由上游给定（:func:`make_entity_id`），
      这里不重算——重算就成了一次"重新命名"，而 ID 变号正是本模块要避免的事。
    * 每个实体登记一条 :class:`Entity`，``seed_occurrence_id`` 取**来源故事顺序最靠前**
      的那条出现（键 ``(chapter_ordinal, start)``，同值再用 ``occurrence_id`` 兜底），
      **不取列表第一个**：调用方的遍历顺序可能来自数据库、文件 glob 或并行分片，把它
      当成"时间顺序"迟早会在换一次实现后悄悄改掉 seed。
    * ``entity_hints`` 是 ``{entity_id: motif}`` 的可选补充，缺失的记 ``""``（空串表示
      "还没起名"，不是"起名失败"）。
    * **幂等**：纯函数，同一批输入反复调用得到同一批实体、同一个 seed；把返回的
      occurrence 再喂回来也不会产生新实体、不会改 seed。输入里的重复记录原样保留——
      该不该合并是"每条证据算几次"的问题，留给 :func:`cutoff_view` 的语义去定。
    """
    hints = dict(entity_hints or {})

    groups: dict[str, list[Occurrence]] = {}
    for occ in occurrences:
        groups.setdefault(occ.entity_id, []).append(occ)

    entities: list[Entity] = []
    for entity_id in sorted(groups):
        seed = min(groups[entity_id], key=_occurrence_key)
        entities.append(
            Entity(
                entity_id=entity_id,
                book_id=book_id,
                motif=hints.get(entity_id, ""),
                seed_occurrence_id=seed.occurrence_id,
            )
        )

    return entities, sorted(occurrences, key=_occurrence_key)


# ---------------------------------------------------------------------------
# 截至当前卷的候选视图
# ---------------------------------------------------------------------------


def importance_score(*, mention_count: int, span: int) -> int:
    """候选重要性的确定性公式：``mention_count * 2 + min(span, 20)``。

    * ``mention_count`` 权重 2：反复出现比"出现过一次但跨得很远"更能说明这是一条真伏笔。
    * ``span`` 封顶 20：跨度大说明铺得久，但**不封顶会让贯穿全书的一条压住所有近期
      热点**——越往后卷，早期实体的 span 只会越来越大，排序会整体固化成"老实体优先"。
      封顶后 span 只在"同为近期出现"时用来分先后。

    必须是确定性公式而不是模型打分：排序结果要能被人逐条复算、能进测试，也要在重跑
    之后完全一致。
    """
    return mention_count * 2 + min(span, 20)


def cutoff_view(
    *,
    entities: Sequence[Entity],
    occurrences: Sequence[Occurrence],
    cutoff_ordinal: int,
    max_entities: int | None = None,
    min_mentions: int = 1,
) -> list[dict]:
    """"截至当前卷"的候选视图：只看得到 ``chapter_ordinal < cutoff_ordinal`` 的证据。

    处理顺序就是这几步，顺序本身是语义的一部分：

    1. **先筛**：只保留 ``source.chapter_ordinal < cutoff_ordinal`` 的出现记录
       （排他上界——``cutoff_ordinal`` 是"本卷第一章"，它自己不算已读）。
    2. **整条排除**：某实体的出现记录里只要有一条 ``chapter_ordinal >= cutoff_ordinal``，
       该实体在这个视图里**整条不出现**。既不"凭首次出现靠前就放进来"，也不"截掉那几条
       照样输出"——后者看上去没泄漏，实际是把一条被削掉后半段的历史当成完整历史，
       模型会据此给出确定但错误的判断。
    3. **算属性**（只用可见记录）：``mention_count``、``first_seen_ordinal``、
       ``last_seen_ordinal``、``span = last - first + 1``（同章两条出现 → span 1）、
       ``evidence``（可见 ``occurrence_id``，按故事顺序）。同一 ``occurrence_id``
       重复出现只算一次——重复导入不是"又提了一次"。
    4. **再筛**：``mention_count >= min_mentions``。没有可见出现的实体一律不进视图，
       无论 ``min_mentions`` 取多少：没有证据就没有可展示的内容，``span`` 也无定义。
    5. **排序**：``(-mention_count, -importance_score, first_seen_ordinal, entity_id)``，
       其中 ``importance_score = mention_count * 2 + min(span, 20)``。全序、与输入顺序
       完全无关（末位有 ``entity_id`` 兜底）。
    6. **截断**：``max_entities`` 非 None 时在排序后取前 N 条。**不报告**是否发生了
       截断，也不返回"还有多少条被截掉"——本函数只管视图，**截断的披露由调用方负责**
       （它才知道该对用户说什么、要不要提示"还有 N 条未展示"）。

    返回每一项都是 JSON 友好的 dict：``{"entity_id", "motif", "mention_count",
    "first_seen_ordinal", "last_seen_ordinal", "span", "evidence"}``。刻意不放
    ``importance_score``：它是排序的中间量，放进来会诱使下游拿它当"伏笔质量分"用。
    """
    entity_by_id = {entity.entity_id: entity for entity in entities}

    # entity_id -> occurrence_id -> Occurrence：同一个 id 的重复记录只留第一条
    # （按全序取第一条，因此"留哪条"也与输入顺序无关）。
    visible: dict[str, dict[str, Occurrence]] = {}
    has_future_evidence: set[str] = set()

    for occ in sorted(occurrences, key=_occurrence_key):
        if occ.entity_id not in entity_by_id:
            # 没有实体就没有 motif 可展示；静默跳过而不是猜一个空实体。
            continue
        if occ.source.chapter_ordinal >= cutoff_ordinal:
            has_future_evidence.add(occ.entity_id)
            continue
        visible.setdefault(occ.entity_id, {}).setdefault(occ.occurrence_id, occ)

    rows: list[dict] = []
    for entity_id, by_occurrence in visible.items():
        if entity_id in has_future_evidence:
            continue  # 整条排除：有越界证据的实体一条都不出现
        group = sorted(by_occurrence.values(), key=lambda occ: _source_key(occ.source))
        mention_count = len(group)
        if mention_count < min_mentions:
            continue
        first_seen_ordinal = group[0].source.chapter_ordinal
        last_seen_ordinal = group[-1].source.chapter_ordinal
        span = last_seen_ordinal - first_seen_ordinal + 1
        rows.append(
            {
                "entity_id": entity_id,
                "motif": entity_by_id[entity_id].motif,
                "mention_count": mention_count,
                "first_seen_ordinal": first_seen_ordinal,
                "last_seen_ordinal": last_seen_ordinal,
                "span": span,
                "evidence": [occ.occurrence_id for occ in group],
            }
        )

    rows.sort(
        key=lambda row: (
            -row["mention_count"],
            -importance_score(
                mention_count=row["mention_count"],
                span=row["span"],
            ),
            row["first_seen_ordinal"],
            row["entity_id"],
        )
    )

    if max_entities is not None:
        # 负数当 0 处理：切片遇到负值会变成"去掉末尾 N 条"，那是没人想要的语义。
        rows = rows[: max(0, int(max_entities))]

    return rows
