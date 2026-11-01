"""伏笔复检 V2：把"这条伏笔回收了吗"当成一次**带证据的核对**，而不是一次自述。

与旧实现的区别只有一句话：**模型不能说"我支持"，除非它能指出原文哪一句。**
程序负责定位、负责计数、负责在证据不足时如实说"不知道"——判错要能被看出来。

判定核（枚举、门禁顺序、九条判据）复用 `backend/vnext/resolution.py`：
那里的每一条都被三轮复审压过，重写一遍只会引入新的偏差。本模块只做 1.x 侧的胶水：
账本条目 → 候选 → 提示词 → 调用 → 落回账本。
"""

from __future__ import annotations

import asyncio
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
DEFAULT_BATCH_SIZE = 6
#: 同时在途的批次上限（只影响墙钟时间，不影响花费）
DEFAULT_CONCURRENCY = 4

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

共 {count} 个候选，必须输出**长度 {count} 的 JSON 数组**，
每个候选恰好一条，`candidate_id` 必须原样取自：{candidate_ids}。
只输出 1 条会被判为不完整，那几条会记成"未核对"。

待核对伏笔：
{items}

输出：纯 JSON 数组，长度必须是 {count}。形状如下（数组元素同构，candidate_id 换成各候选的真实 id）：
[
{schema},
  {{
    ... 第 2 条，直到 {count} 条 ...
  }}
]
"""

ITEM_TEMPLATE = """### 候选 candidate_id={candidate_id}
伏笔描述：{description}
待交代事项（question）：{question}
判定为已回答的标准（answer_criteria）：{answer_criteria}
首次出现：第{first_seen}章；最后提及：第{last_seen}章

以下 {segment_count} 段原文各自有 evidence_id。引文必须**逐字取自其中一段**，
并把**那一段的** evidence_id 原样回填——不同段是不同的证据，不要混：

{segments}
{truncation_note}
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
    #: 因为"没有证据"而没进入候选的条目 id（它们不是"待查未查"）
    dropped_ids: list[str] = field(default_factory=list)
    #: 因为漏答而补问过的批次数（用于观察模型是否稳定答满）
    retried_batches: int = 0
    #: 抛异常的批次数（它们的候选会落到 not_checked，不静默算通过）
    failed_batches: int = 0

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
            "dropped_ids": list(self.dropped_ids),
            "retried_batches": self.retried_batches,
            "failed_batches": self.failed_batches,
        }


def _evidence_id(chapter: int, text: str) -> str:
    """证据身份 = 章 + 该段正文本身。

    原来用"章 + 证据包内起止"，同章两段等长窗口会算出同一个 id，
    后写的覆盖先写的全局验证表——A 引用自己看到的原文被判 QUOTE_NOT_FOUND，
    而引用只展示给 B 的文字反而通过。按正文哈希后，内容相同才算同一份证据。
    """
    raw = f"{chapter}\x1f{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


async def run_recheck_v2(
    items: Sequence[Any],
    *,
    book_text: str = "",
    chapters: Mapping[int, str] | None = None,
    call_model: Callable[[str], Awaitable[str]],
    max_calls: int = 300,
    batch_size: int = DEFAULT_BATCH_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
    limiter: asyncio.Semaphore | None = None,
    emit: Callable[[dict], None] | None = None,
) -> RecheckOutcome:
    """对账本里的伏笔做一轮核对。

    - ``items``：`ForeshadowItem` 列表（只处理 `status == "open"/"active"` 且有锚点的）
    - ``chapters``：按章号索引的**原文**。给了它就只取相关窗口（`EvidenceIndex`），
      否则退回 ``book_text`` 全文（该模式只在短文本/测试里能用——整本会爆上下文）
    - ``call_model``：给提示词返回模型文本（注入以便离线测试）
    - ``max_calls``：本轮调用上限；超出记 ``not_checked``，**不静默当作通过**
    - ``concurrency``：同时在途的批次上限。整本复检是"多批一次问完"，
      串行跑 150 批要两小时；并行只省墙钟时间，不省 token，所以要配上限
      免得把账户打到限流
    - ``limiter``：外部传入的并发信号量。总结阶段把它自己的 `_llm_sem` 传进来，
      于是"复检 + 卷摘要 + 风格"共用**同一个**在途上限，而不是各算各的
      （原来两处各建 Semaphore，峰值能到 2× 配置值）
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
            outcome.dropped_ids.append(getattr(item, "id", ""))
            continue
        # 定位用**整章**（锚点可能落在章末，窗口只够展示）
        source = index.chapter_text(chapter) if index else book_text
        located = locate_quote(quote, source)
        if not located.found:
            outcome.dropped_anchor_missing += 1
            outcome.not_checked.append(getattr(item, "id", ""))
            outcome.dropped_ids.append(getattr(item, "id", ""))
            continue
        candidates.append(
            {
                "item": item,
                "candidate_id": str(getattr(item, "id", "")),
                "evidence_id": _evidence_id(chapter, quote),
                "chapter": chapter,
            }
        )

    outcome.leads_skipped = leads_skipped

    if not candidates:
        empty = count_verdicts([], expected_candidate_ids=[], candidates_total=0)
        outcome.coverage = empty.as_dict()
        outcome.status = recheck_status(started=True, finished=True, stopped=False, counts=empty)
        dropped = outcome.dropped_no_anchor + outcome.dropped_anchor_missing
        if dropped:
            # 一条都没进入候选、却说自己"完成"——这正是最难被发现的那种谎
            outcome.status = "PARTIAL"
            outcome.status_reason = f"没有可用证据（{dropped} 条未进入候选）"
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
            body = bundle.text[start:end].strip()
            eid = _evidence_id(chapter, body)
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

    def _render_batch(batch: list[dict]) -> str:
        rendered = "".join(
            ITEM_TEMPLATE.format(
                candidate_id=c["candidate_id"],
                description=getattr(c["item"], "description", ""),
                question=getattr(c["item"], "question", "") or "（未提供）",
                answer_criteria=getattr(c["item"], "answer_criteria", "") or "（未提供）",
                first_seen=getattr(c["item"], "first_seen_chapter", 0),
                last_seen=getattr(c["item"], "last_seen_chapter", 0),
                segment_count=len(c["segments"]),
                truncation_note=(
                    f"（注意：后文还有 {c.get('omitted', 0)} 处提到相关词但未展示，"
                    "判「没有回收」时要考虑这一点）"
                    if c.get("truncated")
                    else ""
                ),
                segments="\n".join(
                    f'[evidence_id={seg["evidence_id"]} 第{seg["chapter"]}章]\n"""\n'
                    f'{seg["text"]}\n"""'
                    for seg in c["segments"]
                ),
            )
            for c in batch
        )
        return PROMPT_TEMPLATE.format(
            count=len(batch),
            items=rendered,
            schema=render_recheck_schema(),
            candidate_ids=", ".join(c["candidate_id"] for c in batch),
        )

    def _consume(batch: list[dict], raw: str) -> set[str]:
        """解析一批回包，返回真正拿到判定的候选 id。"""
        got: dict[str, Mapping[str, Any]] = {}
        for payload in _extract_payloads(raw):
            cid = payload.get("candidate_id")
            if isinstance(cid, str):
                got[cid] = payload
        done: set[str] = set()
        for c in batch:
            payload = got.get(c["candidate_id"])
            if payload is None:
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
            done.add(c["candidate_id"])
        return done

    sem = limiter if limiter is not None else asyncio.Semaphore(max(1, concurrency))

    async def _ask(batch: list[dict]) -> tuple[list[dict], str]:
        async with sem:
            return batch, await call_model(_render_batch(batch))

    done: set[str] = set()
    for attempt in range(2):
        queue = [c for c in candidates if c["candidate_id"] not in done]
        if not queue:
            break
        if attempt == 1:
            # 第一轮漏掉的候选单独补一次：模型偶尔只答前几条，补问时只给它漏的那条，
            # 命中率高；仍漏就如实记 not_checked，不再反复花钱。
            outcome.retried_batches += 1
        step = len(queue) if attempt == 1 else max(batch_size, 1)
        batches = [queue[i : i + step] for i in range(0, len(queue), step)]
        width = max(1, concurrency)
        for wave_at in range(0, len(batches), width):
            if outcome.calls >= max_calls:
                break
            wave = batches[wave_at : wave_at + width]
            affordable = max_calls - outcome.calls
            if affordable <= 0:
                break
            wave = wave[:affordable]
            # 先记账再发：并发下"先发后记"会超预算，而超支是**已经花掉的钱**，
            # 事后记账会把账做平却掩盖了超支事实。
            outcome.calls += len(wave)
            results = await asyncio.gather(*(_ask(b) for b in wave), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    # 某一批失败不拖垮其他批：它的候选留在未完成集合里，
                    # 由补问轮或收尾如实记 not_checked
                    outcome.failed_batches += 1
                    continue
                batch, raw = result
                done |= _consume(batch, raw)
            if emit is not None:
                emit({"stage": "recheck", "calls": outcome.calls, "checked": len(done)})
        if outcome.calls >= max_calls:
            break

    outcome.not_checked.extend(
        c["candidate_id"] for c in candidates if c["candidate_id"] not in done
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

    pending = [cid for cid in outcome.not_checked if cid not in set(outcome.dropped_ids)]
    outcome.status = recheck_status(
        started=True, finished=not pending, stopped=False, counts=counts
    )
    if pending:
        outcome.status_reason = "存在未核对候选"
    dropped = outcome.dropped_no_anchor + outcome.dropped_anchor_missing
    if dropped and outcome.status == "COMPLETED":
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


def _strip_fence(text: str) -> str:
    """去掉 ```json 围栏；模型常把 JSON 包在代码块里。"""
    text = text.strip()
    if "```" not in text:
        return text
    parts = text.split("```")
    # 取最长的代码块内容
    return max(parts, key=len).lstrip("json").lstrip("JSON").strip()


def _balanced_slice(text: str, opener: str, closer: str) -> str | None:
    """从第一个 opener 起，按括号配平切出完整 JSON 片段（跳过字符串内的括号）。"""
    start = text.find(opener)
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _extract_payloads(raw: str) -> list[Mapping[str, Any]]:
    """从模型文本里挖出**全部**候选结果。

    这里曾经把活全交给 ``extract_json_from_text``，而它只抽出**第一个**对象——
    三条候选的数组进来，出去只有一条，看起来像"模型只答了一条"。
    实测模型其实答全了，是解析把它吃掉。所以这里自己做括号配平扫描。
    """
    if not raw:
        return []
    text = _strip_fence(raw)

    data: Any = None
    for candidate in (
        text,
        _balanced_slice(text, "[", "]"),
        _balanced_slice(text, "{", "}"),
    ):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            break
        except (ValueError, TypeError):
            data = None
    if data is None:
        fallback = extract_json_from_text(raw)
        if isinstance(fallback, str):
            try:
                data = json.loads(fallback)
            except (ValueError, TypeError):
                return []
        else:
            data = fallback
    if data is None:
        return []

    items: Iterable[Any]
    if isinstance(data, Mapping):
        for key in ("results", "verdicts", "items"):
            value = data.get(key)
            if isinstance(value, list):
                items = value
                break
        else:
            items = [data]
    elif isinstance(data, list):
        items = data
    else:
        return []
    return [c for c in items if isinstance(c, Mapping)]



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
