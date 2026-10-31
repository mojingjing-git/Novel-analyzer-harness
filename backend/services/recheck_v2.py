"""伏笔复检 V2：把"这条伏笔回收了吗"当成一次**带证据的核对**，而不是一次自述。

与旧实现的区别只有一句话：**模型不能说"我支持"，除非它能指出原文哪一句。**
程序负责定位、负责计数、负责在证据不足时如实说"不知道"——判错要能被看出来。

判定核（枚举、门禁顺序、九条判据）复用 `backend/vnext/resolution.py`：
那里的每一条都被三轮复审压过，重写一遍只会引入新的偏差。本模块只做 1.x 侧的胶水：
账本条目 → 候选 → 提示词 → 调用 → 落回账本。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from backend.core.evidence import EvidenceIndex
from backend.utils.json_utils import extract_json_from_text
from backend.vnext.resolution import (
    INSUFFICIENT,
    CandidateVerdict,
    count_verdicts,
    locate_quote,
    parse_recheck,
    recheck_status,
    reduce_entity,
    render_recheck_schema,
)

#: 判定结论 → 账本状态。只有 ``AUTO_RESOLVED`` 敢直接写 resolved。
AUTO_RESOLVED = "AUTO_RESOLVED"
PROPOSED_RESOLVED = "PROPOSED_RESOLVED"
OPEN = "OPEN"

#: 一次调用最多塞几条候选。太多会让模型串号（它只看到一堆编号）。
DEFAULT_BATCH_SIZE = 1

PROMPT_TEMPLATE = """你是长篇小说的伏笔审校员。任务：判断**这条伏笔**在后文里是否已经被回收。

判定要求（必须遵守）：
1. 回收 = 后文明确交代了这条伏笔指向的悬念/承诺/威胁，而不是"又提了一次"。
2. 只依据给出的原文；原文没有交代就算"证据不足"，不要用常理推测。
3. 必须做**反证检索**：在原文里找"是否其实没有回收"的迹象，并在 `counter_search.performed` 里如实标记 true。
4. `supporting_quote` 必须从原文**逐字照抄**不少于 6 个字，连同标点，不要改写、不要拼接、不要加省略号。
5. `evidence_id` 原样回填该候选给出的那个值（不要自己编）。
6. 找不到符合条件的原句，就答 `INSUFFICIENT`。

判定枚举（只能用这四个词之一）：
- `SUPPORTED`：后文明确交代了结果
- `DIFFERENT_ANSWER`：后文给了线索，但答案与伏笔暗示的不是一回事
- `CONTRADICTED`：后文明确否定了这条伏笔暗示的方向
- `INSUFFICIENT`：原文不足以判断（这是诚实的答案，不是失败）

共 {count} 个候选。

待核对伏笔：
{items}

输出：纯 JSON 数组，每个候选**一条**结果：
{schema}
"""

ITEM_TEMPLATE = """### 候选 candidate_id={candidate_id}
伏笔描述：{description}
首次出现：第{first_seen}章；最后提及：第{last_seen}章
埋设处 evidence_id={evidence_id}（第{chapter}章）

全文如下（引文必须逐字取自此处）：
\"\"\"
{evidence_text}
\"\"\"
"""


@dataclass
class RecheckOutcome:
    """一次复检的账：判定数、覆盖计数、状态、以及需要人看的那几条。"""

    verdicts: list[CandidateVerdict] = field(default_factory=list)
    applied: dict[str, str] = field(default_factory=dict)
    calls: int = 0
    not_checked: list[str] = field(default_factory=list)
    coverage: dict[str, int] = field(default_factory=dict)
    status: str = "NOT_RUN"
    status_reason: str = ""
    leads_skipped: int = 0

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "applied": dict(self.applied),
            "coverage": dict(self.coverage),
            "status": self.status,
            "status_reason": self.status_reason,
            "leads_skipped": self.leads_skipped,
            "not_checked": list(self.not_checked),
        }


def _evidence_id(chapter: int, start: int, end: int) -> str:
    """证据身份：章 + 区间。同一句话在同章同位置才算同一份证据。"""
    raw = f"{chapter}\x1f{start}\x1f{end}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


async def run_recheck_v2(
    items: Sequence[Any],
    *,
    book_text: str = "",
    chapters: Mapping[int, str] | None = None,
    call_model: Callable[[str], Awaitable[str]],
    max_calls: int = 300,
    batch_size: int = DEFAULT_BATCH_SIZE,
    emit: Callable[[dict], None] | None = None,
) -> RecheckOutcome:
    """对账本里的伏笔做一轮核对。

    - ``items``：`ForeshadowItem` 列表（只处理 `status == "open"/"active"` 且有锚点的）
    - ``chapters``：按章号索引的**原文**。给了它就只取相关窗口（`EvidenceIndex`），
      否则退回 ``book_text`` 全文（该模式只在短文本/测试里能用——整本会爆上下文）
    - ``call_model``：给提示词返回模型文本（注入以便离线测试）
    - ``max_calls``：本轮调用上限；超出记 ``not_checked``，**不静默当作通过**
    """
    outcome = RecheckOutcome()
    candidates: list[dict] = []
    index = EvidenceIndex(chapters) if chapters else None

    leads_skipped = 0
    for item in items:
        if getattr(item, "status", "") not in ("open", "active"):
            continue
        if getattr(item, "lead", False):
            # 只是线索（没给出可关闭事项）：不进复检队列，也不计入 not_checked——
            # 它本来就不是"待核实但没查"，而是"没有可核实的问题"。
            leads_skipped += 1
            continue
        quote = (getattr(item, "anchor_quote", "") or "").strip()
        chapter = int(getattr(item, "anchor_chapter", 0) or 0)
        if not quote or chapter <= 0:
            # 没有锚点 = 没有证据。这类条目只能标"证据不足"，不能凭模型自述判回收。
            outcome.not_checked.append(getattr(item, "id", ""))
            continue
        # 定位用**整章**（锚点可能落在章末，窗口只够展示）
        source = index.chapter_text(chapter) if index else book_text
        located = locate_quote(quote, source)
        if not located.found:
            outcome.not_checked.append(getattr(item, "id", ""))
            continue
        candidates.append(
            {
                "item": item,
                "candidate_id": str(getattr(item, "id", "")),
                "evidence_id": _evidence_id(chapter, located.start, located.end),
                "chapter": chapter,
            }
        )

    outcome.leads_skipped = leads_skipped

    if not candidates:
        empty = count_verdicts([], expected_candidate_ids=[], candidates_total=0)
        outcome.coverage = empty.as_dict()
        outcome.status = recheck_status(started=True, finished=True, stopped=False, counts=empty)
        return outcome

    if index is not None:
        # 有界证据包：埋设处 + 后续命中窗口。模型看到的不是"全书"，而是"这几段"，
        # 所以引文必须落在下面这段文本里——这就是防止模型编引文的机制。
        texts_by_evidence: dict[str, str] = {}
        for cand in candidates:
            item = cand["item"]
            bundle = index.bundle_for(
                anchor_chapter=cand["chapter"],
                anchor_quote=(getattr(item, "anchor_quote", "") or "").strip(),
                hint_texts=[
                    getattr(item, "question", "") or "",
                    getattr(item, "description", "") or "",
                ],
            )
            texts_by_evidence[cand["evidence_id"]] = bundle.text
            cand["bundle_chapters"] = bundle.chapters
    else:
        texts_by_evidence = {c["evidence_id"]: book_text for c in candidates}

    by_id = {c["candidate_id"]: c for c in candidates}

    for offset in range(0, len(candidates), max(batch_size, 1)):
        batch = candidates[offset : offset + max(batch_size, 1)]
        if outcome.calls >= max_calls:
            outcome.not_checked.extend(c["candidate_id"] for c in batch)
            continue

        rendered = "".join(
            ITEM_TEMPLATE.format(
                candidate_id=c["candidate_id"],
                description=getattr(c["item"], "description", ""),
                first_seen=getattr(c["item"], "first_seen_chapter", 0),
                last_seen=getattr(c["item"], "last_seen_chapter", 0),
                evidence_id=c["evidence_id"],
                chapter=c["chapter"],
                evidence_text=texts_by_evidence.get(c["evidence_id"], ""),
            )
            for c in batch
        )
        prompt = PROMPT_TEMPLATE.format(
            count=len(batch), items=rendered, schema=render_recheck_schema()
        )
        outcome.calls += 1
        raw = await call_model(prompt)

        got: dict[str, Mapping[str, Any]] = {}
        for payload in _extract_payloads(raw):
            cid = payload.get("candidate_id")
            if isinstance(cid, str):
                got[cid] = payload

        for c in batch:
            payload = got.get(c["candidate_id"])
            if payload is None:
                outcome.not_checked.append(c["candidate_id"])
                continue
            if not payload.get("evidence_id"):
                # 单证据候选：模型没回填 evidence_id 时补上本候选的唯一证据。
                # 这不放松门禁——"引文必须在被点名的那段原文里"仍由 locate 校验，
                # 只是不让模型为了抄一串哈希而额外产生一次格式失败。
                payload = {**payload, "evidence_id": c["evidence_id"]}
            outcome.verdicts.append(
                parse_recheck(
                    payload,
                    allowed_candidate_ids=[c["candidate_id"]],
                    allowed_evidence_ids=[c["evidence_id"]],
                    # parse_recheck 的 locate 第二个参数是 evidence_id，不是正文——
                    # 传错会让每条 SUPPORTED 都变成 QUOTE_NOT_FOUND（这个坑踩过一次）
                    locate=lambda quote, eid: locate_quote(
                        quote, texts_by_evidence.get(eid, "")
                    ),
                    evidence_text=lambda eid: texts_by_evidence.get(eid, ""),
                )
            )

    counts = count_verdicts(
        outcome.verdicts,
        expected_candidate_ids=[c["candidate_id"] for c in candidates],
        candidates_total=len(candidates),
        stale=0,
    )
    outcome.coverage = counts.as_dict()

    # 候选级 → 实体级：任一 SUPPORTED 才算回收；只有"不足/冲突"标待复核
    for entity_id, group in _group_by_candidate(outcome.verdicts).items():
        state, _best = reduce_entity(group)
        outcome.applied[entity_id] = state
        item = by_id.get(entity_id, {}).get("item")
        if item is not None:
            _apply_state(item, state, group)

    outcome.status = recheck_status(
        started=True, finished=not outcome.not_checked, stopped=False, counts=counts
    )
    if outcome.not_checked:
        outcome.status_reason = "存在未核对候选"
    if emit is not None:
        emit(outcome.summary())
    return outcome


def _extract_payloads(raw: str) -> list[Mapping[str, Any]]:
    """从模型文本里挖出 JSON 数组或单对象；挖不出来就当空（会被记 not_checked）。"""
    if not raw:
        return []
    data = extract_json_from_text(raw)
    if isinstance(data, str):  # extract_json_from_text 返回的是 JSON 文本，不是对象
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return []
    if data is None:
        return []
    candidates: Iterable[Any]
    if isinstance(data, Mapping):
        for key in ("results", "verdicts", "items"):
            value = data.get(key)
            if isinstance(value, list):
                candidates = value
                break
        else:
            candidates = [data]
    elif isinstance(data, list):
        candidates = data
    else:
        return []
    return [c for c in candidates if isinstance(c, Mapping)]


def _group_by_candidate(verdicts: Iterable[CandidateVerdict]) -> dict[str, list[CandidateVerdict]]:
    grouped: dict[str, list[CandidateVerdict]] = {}
    for v in verdicts:
        grouped.setdefault(v.candidate_id, []).append(v)
    return grouped


def _apply_state(item: Any, state: str, group: list[CandidateVerdict]) -> None:
    """把实体级结论写回账本条目。**只有 AUTO_RESOLVED 才置 resolved。**"""
    supported = [v for v in group if v.verdict == "SUPPORTED"]
    if state == AUTO_RESOLVED and supported:
        best = supported[0]
        item.status = "resolved"
        item.resolution = best.answer or ""
        item.resolved_chapter = int(getattr(item, "anchor_chapter", 0) or 0)
        item.source_type = "auto_recheck"
        item.needs_review = False
        return
    if state == PROPOSED_RESOLVED:
        item.needs_review = True
        notes = getattr(item, "notes", None)
        if isinstance(notes, list):
            notes.append("复检：后文有线索但不足以判回收，请人工确认")
        return
    # OPEN：保持原状，但把"查过但没结论"记进备注，避免与"没查"混淆
    notes = getattr(item, "notes", None)
    if isinstance(notes, list) and any(v.verdict == INSUFFICIENT for v in group):
        notes.append("复检：证据不足")
