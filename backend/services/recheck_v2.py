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
3. 必须做**反证检索**：在原文里找"是否其实没有回收"的迹象，`counter_search.performed` 标 true，
   `checked_evidence_ids` 必须是**JSON 数组**（如 ["b36e071a4ab8"]），不要写成逗号串；
   `counter_quotes` 也是数组，没有反证就写 []。
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
待交代事项（question）：{question}
判定为已回答的标准（answer_criteria）：{answer_criteria}
首次出现：第{first_seen}章；最后提及：第{last_seen}章

以下 {segment_count} 段原文各自有 evidence_id。引文必须**逐字取自其中一段**，
并把**那一段的** evidence_id 原样回填——不同段是不同的证据，不要混：

{segments}

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
    #: 输入条目总数（含被跳过的）——覆盖报告必须能从输入追到分母
    input_total: int = 0
    #: 没有锚点 / 锚点在章内定位不到：这两类都不是"查了没答案"，要单列
    dropped_no_anchor: int = 0
    dropped_anchor_missing: int = 0

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "applied": dict(self.applied),
            "coverage": dict(self.coverage),
            "status": self.status,
            "status_reason": self.status_reason,
            "leads_skipped": self.leads_skipped,
            "input_total": self.input_total,
            "dropped_no_anchor": self.dropped_no_anchor,
            "dropped_anchor_missing": self.dropped_anchor_missing,
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
    outcome.input_total = len(items)
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
            outcome.dropped_no_anchor += 1
            outcome.not_checked.append(getattr(item, "id", ""))
            continue
        # 定位用**整章**（锚点可能落在章末，窗口只够展示）
        source = index.chapter_text(chapter) if index else book_text
        located = locate_quote(quote, source)
        if not located.found:
            outcome.dropped_anchor_missing += 1
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

    # 证据按**段**编号：一条伏笔的埋设处在第 2 章、答案在第 9 章，若把两段当成
    # 一份证据，模型只能回填埋设处那个 id，"引文落在被点名证据里"这条门禁就永远过不了。
    texts_by_evidence: dict[str, str] = {}
    chapter_by_evidence: dict[str, int] = {}
    for cand in candidates:
        if index is None:
            texts_by_evidence[cand["evidence_id"]] = book_text
            chapter_by_evidence[cand["evidence_id"]] = cand["chapter"]
            cand["segments"] = [
                {"evidence_id": cand["evidence_id"], "chapter": cand["chapter"], "text": book_text}
            ]
            continue
        item = cand["item"]
        bundle = index.bundle_for(
            anchor_chapter=cand["chapter"],
            anchor_quote=(getattr(item, "anchor_quote", "") or "").strip(),
            hint_texts=[
                getattr(item, "question", "") or "",
                getattr(item, "description", "") or "",
            ],
        )
        segments: list[dict] = []
        for chapter, start, end in bundle.spans:
            eid = _evidence_id(chapter, start, end)
            body = bundle.text[start:end].strip()
            texts_by_evidence[eid] = body
            chapter_by_evidence[eid] = chapter
            segments.append({"evidence_id": eid, "chapter": chapter, "text": body})
        if not segments:
            fallback_text = index.chapter_text(cand["chapter"])
            texts_by_evidence[cand["evidence_id"]] = fallback_text
            chapter_by_evidence[cand["evidence_id"]] = cand["chapter"]
            segments.append(
                {"evidence_id": cand["evidence_id"], "chapter": cand["chapter"], "text": fallback_text}
            )
        cand["segments"] = segments

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
                question=getattr(c["item"], "question", "") or "（未提供）",
                answer_criteria=getattr(c["item"], "answer_criteria", "") or "（未提供）",
                first_seen=getattr(c["item"], "first_seen_chapter", 0),
                last_seen=getattr(c["item"], "last_seen_chapter", 0),
                segment_count=len(c["segments"]),
                segments="\n".join(
                    f'[evidence_id={seg["evidence_id"]} 第{seg["chapter"]}章]\n"""\n'
                    f'{seg["text"]}\n"""'
                    for seg in c["segments"]
                ),
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
            payload = _normalize_payload(payload)
            allowed = [seg["evidence_id"] for seg in c["segments"]]
            if not payload.get("evidence_id") and len(allowed) == 1:
                # 只有一段证据时才替它补；多段时必须由模型点名是哪一段，
                # 否则"引文落在被点名的那段里"这条门禁形同虚设。
                payload = {**payload, "evidence_id": allowed[0]}
            outcome.verdicts.append(
                parse_recheck(
                    payload,
                    allowed_candidate_ids=[c["candidate_id"]],
                    allowed_evidence_ids=allowed,
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
    def _chapter_of(evidence_id: str, _offset: int | None = None) -> int | None:
        """引文所在段对应的章号——证据已按段编号，不必再按偏移反查。"""
        return chapter_by_evidence.get(evidence_id)

    for entity_id, group in _group_by_candidate(outcome.verdicts).items():
        state, _best = reduce_entity(group)
        outcome.applied[entity_id] = state
        item = by_id.get(entity_id, {}).get("item")
        if item is not None:
            _apply_state(item, state, group, chapter_of=_chapter_of)

    outcome.status = recheck_status(
        started=True, finished=not outcome.not_checked, stopped=False, counts=counts
    )
    if outcome.not_checked:
        outcome.status_reason = "存在未核对候选"
    dropped = outcome.dropped_no_anchor + outcome.dropped_anchor_missing
    if dropped and outcome.status == COMPLETED:
        # 有条目因为没有证据而整条没查：这不是"完成"，只是"候选都处理完了"
        outcome.status = "PARTIAL"
        outcome.status_reason = f"部分条目无可用证据（{dropped} 条未进入候选）"
    if emit is not None:
        emit(outcome.summary())
    return outcome


def _normalize_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """把模型爱写的"逗号串"归一成数组。

    这不是放松门禁：`checked_evidence_ids` 仍要逐个比对是否属于本题给出的证据，
    只是把"格式写歪"和"证据造假"分开——前者不该被记成后者。
    """
    fixed = dict(payload)
    counter = fixed.get("counter_search")
    if isinstance(counter, Mapping):
        ids = counter.get("checked_evidence_ids")
        if isinstance(ids, str):
            parsed = [x.strip() for x in ids.replace("，", ",").split(",") if x.strip()]
            counter = {**counter, "checked_evidence_ids": parsed}
        quotes = counter.get("counter_quotes")
        if isinstance(quotes, str):
            counter = {**counter, "counter_quotes": [quotes] if quotes.strip() else []}
        fixed["counter_search"] = counter
    return fixed


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


def _apply_state(
    item: Any,
    state: str,
    group: list[CandidateVerdict],
    *,
    chapter_of: Callable[[str, int | None], int | None] | None = None,
) -> None:
    """把实体级结论写回账本条目。**只有 AUTO_RESOLVED 才置 resolved。**

    `resolved_chapter` 取**引文所在章**，不是埋设章：一条伏笔在第 12 章埋、在第 900 章收，
    写回埋设章等于把"什么时候解决的"记错，而且账面看不出来。
    """
    supported = [v for v in group if v.verdict == "SUPPORTED"]
    if state == AUTO_RESOLVED and supported:
        best = supported[0]
        located_chapter = None
        if chapter_of is not None:
            start = best.located_span[0] if best.located_span else None
            located_chapter = chapter_of(best.evidence_id, start)
        item.status = "resolved"
        item.resolution = best.answer or ""
        item.resolved_chapter = int(
            located_chapter or getattr(item, "anchor_chapter", 0) or 0
        )
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
