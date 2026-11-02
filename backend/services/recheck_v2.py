"""伏笔复检 V2：把"这条伏笔回收了吗"当成一次**带证据的核对**，而不是一次自述。

核心区别只有一句话：**模型不能说"我支持"，除非它能指出原文哪一句。**
程序负责定位、负责计数、负责在证据不足时如实说"不知道"——判错要能被看出来。

判定核（枚举、门禁顺序、九条判据）复用 `backend/vnext/resolution.py`：
那里的每一条都经过反复推敲，重写一遍只会引入新的偏差。本模块只做 1.x 侧的胶水：
账本条目 → 候选 → 提示词 → 调用 → 落回账本。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from backend.core.evidence import EvidenceIndex
from backend.core.text_units import locate_with_mode, unit_label
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

logger = logging.getLogger(__name__)

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
   {unit_note}

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
    #: 锚点章号被修正过的条目（记录下来的章号 → 引文真正所在章）。
    #: 章号只是提示：书里同一个章号可能重复出现，采集记的那一份未必是引文所在。
    resolved_chapters: dict[str, int] = field(default_factory=dict)
    #: 被修正的条目的**物理单元键**（2.0 里是文件序号）。与 resolved_chapters 分开：
    #: 后者是文学章号语义（可写进账本），前者是检索单元语义（不能混进章号字段）。
    resolved_units: dict[str, int] = field(default_factory=dict)
    #: 逐条定位诊断：candidate_id -> {unit, mode, source_chapter, range, low_confidence}。
    #: 与 verdicts 分开存——**没进入候选的条目（掉队条目）也要有定位记录**，
    #: 否则"锚点找不到"这类掉队在明细里彻底失灵（AGY 复审 P1-3）。
    locate_records: dict[str, dict] = field(default_factory=dict)
    #: 首个非空原始回包（截断 2000 字）。只在"0 条判定落账"这类事故里才有价值：
    #: 有了它就能分清"模型答了但格式不对"与"根本没答"。
    raw_sample: str = ""

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

    def detail(self) -> dict:
        """汇总计数 + **逐条明细**。

        ``summary()`` 只给计数，够做进度与看板；但"这条 SUPPORTED 是拿哪句话判的"
        追不出来，判定质量就只能靠推断。明细单独走这个出口，而不是塞进
        ``summary()``：``summary()`` 会进进度事件（每次 emit 广播）和报告快照，
        48 条候选的全文引文挂在里面会把它们撑坏。
        """
        return {
            **self.summary(),
            "verdicts": [v.to_dict() for v in self.verdicts],
            # 定位诊断放在**顶层**：没进入候选的条目（dropped_anchor_missing / dropped_no_anchor）
            # 没有 verdict 实例，它们的定位信息只能落在这里，否则掉队条目在明细里彻底失灵。
            "locate_records": dict(self.locate_records),
            # 0 条判定落账时的取证材料（正常轮次为空串）
            "raw_sample": self.raw_sample,
        }


#: 逐条判定明细的落盘文件名（放在书的 output 目录里，和账本、审计报告同层）。
RECHECK_DETAIL_FILENAME = "recheck_verdicts.json"


def dump_recheck_detail(
    outcome: RecheckOutcome, *, model: str = "", base_url: str = ""
) -> dict:
    """逐条判定明细（带跑批参数），供落盘与审计。

    为什么必须落盘：``summary()`` 只有计数。跑完一本长书之后，能看到的只有
    "SUPPORTED 19 / INSUFFICIENT 20"——**判的是哪一条、拿哪句话判的，全都没了**。
    没有这层，判定质量只能靠重跑和抽样推断，而重跑得到的还是另一批结果。

    明细里每条都带 `CandidateVerdict.to_dict()` 的全字段（判定、答案、逐字引文、
    程序定位的位置、模型自报的位置、反证是否执行），所以"这条 SUPPORTED 是不是
    真答上了那个问题"可以直接对着原文查，不必再猜。
    """
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "base_url": base_url,
        "status": outcome.status,
        "status_reason": outcome.status_reason,
        "coverage": dict(outcome.coverage),
        "candidates_total": outcome.coverage.get("candidates_total", 0),
        "not_checked": list(outcome.not_checked),
        "dropped_ids": list(outcome.dropped_ids),
        "applied": dict(outcome.applied),
        "resolved_chapters": dict(outcome.resolved_chapters),
        "verdicts": [v.to_dict() for v in outcome.verdicts],
    }


def _evidence_id(chapter: int, text: str) -> str:
    """证据身份 = 章 + 该段正文本身。

    用"章 + 证据包内起止"会让同章两段等长窗口算出同一个 id，
    后写的覆盖先写的全局验证表——A 引用自己看到的原文被判 QUOTE_NOT_FOUND，
    而引用只展示给 B 的文字反而通过。按正文哈希后，内容相同才算同一份证据。
    """
    raw = f"{chapter}\x1f{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def _display_chapter(
    unit: int,
    display_labels: Mapping[int, str] | None,
    fallback: int = 0,
    *,
    noun: str = "章",
) -> int:
    """单元键 → **文学章号**（写进账本的必须是章号，不能是文件序号）。

    2.0 的单元键是物理文件序号（1..922），而账本的 ``resolved_chapter``、
    ``first_seen_chapter`` 以及休眠跨度计算都是**章号语义**——把 366 写进
    ``resolved_chapter`` 会让"在第几章回收"整体错位。

    - ``noun="章"``（1.x）：单元键**就是**章号，原样返回；
    - ``noun="文件"``（2.0）：章号来自文件内解析出的回号；解析不出就退回采集侧
      记的 ``anchor_chapter``，再不行是 0（未知）。**任何情况下都不写文件序号**。
    """
    if noun != "文件":
        return int(unit)
    disp = (display_labels or {}).get(unit)
    if disp:
        digits = disp.replace("第", "").replace("回", "").strip()
        try:
            from backend.core.chapter_split import cn_to_int

            parsed = cn_to_int(digits)
        except Exception:  # pragma: no cover - 解析失败就走回退
            parsed = None
        if parsed:
            return parsed
    return int(fallback or 0)


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
    unit_noun: str = "章",
    display_labels: Mapping[int, str] | None = None,
    block_size: int = 4,
) -> RecheckOutcome:
    """对账本里的伏笔做一轮核对。

    - ``items``：`ForeshadowItem` 列表（只处理 `status == "open"/"active"` 且有锚点的）
    - ``chapters``：按章号索引的**原文**。给了它就只取相关窗口（`EvidenceIndex`），
      否则退回 ``book_text`` 全文（该模式只在短文本/测试里能用——整本会爆上下文）。
      **2.0 里单元键是物理文件序号**（不是文学章号），见 `backend/core/text_units.py`
    - ``call_model``：给提示词返回模型文本（注入以便离线测试）
    - ``max_calls``：本轮调用上限；超出记 ``not_checked``，**不静默当作通过**
    - ``concurrency``：同时在途的批次上限。整本复检是"多批一次问完"，
      串行跑 150 批要两小时；并行只省墙钟时间，不省 token，所以要配上限
      免得把账户打到限流
    - ``limiter``：外部传入的并发信号量。总结阶段把它自己的 `_llm_sem` 传进来，
      于是"复检 + 卷摘要 + 风格"共用**同一个**在途上限，而不是各算各的
      （各建各的 Semaphore 会让峰值达到 2× 配置值）
    - ``unit_noun`` / ``display_labels``：2.0 的展示口径（"文件366（第12回）"）；
      默认保持 1.x 的"第 12 章"。
    """
    outcome = RecheckOutcome()
    candidates: list[dict] = []
    index = EvidenceIndex(chapters) if chapters else None

    # 全书文本 + 章偏移：章号只是**提示**，不是身份。
    #
    # 源文件里行首出现的"第N回"（正文引用回次）会被标题正则当成章节边界，
    # 于是同一个章号可能出现在多个块里——实测《大奉打更人》922 个源文件切出的
    # 合并块 spans 是 [229, 230, 1, 2] 这种形态，章号 1、2 在书里反复出现。
    # 采集侧记的 anchor_chapter 因此可能指向"另一份同号章"的文本：
    # 实测 1669 条带锚点的候选里，1303 条（78%）因此被记成"锚点找不到"，
    # 复检等于只查了两成。
    #
    # 修法：**单元键才是身份**（2.0 里是物理文件序号）。先用采集侧记的章号找；
    # 找不到就在**全书按纯字面**找那句引文，取最早出现的那一单元。
    # 全书搜索只允许 `str.find`：绝不能在这里调 `locate_quote`——
    # 它找不到时会回退 `core/citations.py:174` 的 `difflib.SequenceMatcher`，
    # 那是 O(N·M)，拿 435 万字符去跑单次就能卡住几十秒到几分钟。

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
        # 定位：**候选块范围优先**。
        #
        # 采集是 block_size 章合并分析，一条伏笔的引文只能落在产生它的那一块里
        # （物理文件序号区间 [N, N+block_size-1]）。实测 1669/1669 = 100% 命中，
        # 范围外 0 条 → 范围唯一，不会出现"同回号多文件按升序取第一个"的跨卷截胡。
        #
        # N 从 `item.first_seen_chapter` 取——它存的就是物理文件序号（去重时取最早那条），
        # 与锚点引文的来源严格一致，**旧账本同样可用**（无需迁移）。
        unit = chapter if index is not None else 0
        start = int(getattr(item, "first_seen_chapter", 0) or 0)
        cand_range = (start, start + max(block_size, 1) - 1) if start > 0 else None
        hit = locate_with_mode(
            chapters or {},
            quote,
            candidate_range=cand_range,
            display_labels=display_labels,
            anchor_round=chapter or None,
        )
        located = None
        if hit.unit is not None:
            owner = hit.unit
            # 命中后要在**那一单元内**再定位一次，拿到窗口内的偏移
            located = locate_quote(quote, index.chapter_text(owner)) if index is not None else None
            if located is not None and located.found:
                if owner != chapter:
                    outcome.resolved_units[getattr(item, "id", "")] = owner
                    outcome.resolved_chapters[getattr(item, "id", "")] = _display_chapter(
                        owner, display_labels, getattr(item, "anchor_chapter", 0),
                        noun=unit_noun,
                    )
                unit = owner
                chapter = owner
            else:
                located = None
        if located is None and index is not None:
            # 纯字面没命中：退到**章级**模糊匹配（difflib 只跑在几千字的章上，安全）
            located = locate_quote(quote, index.chapter_text(chapter))
        if located is None and not index and book_text:
            located = locate_quote(quote, book_text)
        outcome.locate_records[str(getattr(item, "id", ""))] = {
            "unit": hit.unit,
            "mode": hit.mode,
            "source_chapter": start,
            "range": list(cand_range) if cand_range else None,
            "low_confidence": hit.mode != "range",
        }
        if located is None or not located.found:
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
                "unit": unit,
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
    #
    # 组装是**惰性**的：`_build_evidence` 在波次循环里、发请求前才对本波候选调用。
    # 它是每条候选最贵的一步（每条都要拿自己的检索词扫一遍全书选证据章），
    # 提前建完全部候选既拖慢首调用，又让 `--max-calls` 很小的运行白算全书。
    texts_by_evidence: dict[str, str] = {}
    chapter_by_evidence: dict[str, int] = {}

    def _build_evidence(cand: dict) -> None:
        if "segments" in cand:
            return
        if index is None:
            texts_by_evidence[cand["evidence_id"]] = book_text
            chapter_by_evidence[cand["evidence_id"]] = cand["chapter"]
            cand["segments"] = [
                {"evidence_id": cand["evidence_id"], "chapter": cand["chapter"], "text": book_text}
            ]
            return
        item = cand["item"]
        bundle = index.bundle_for(
            anchor_chapter=cand["chapter"],
            anchor_quote=(getattr(item, "anchor_quote", "") or "").strip(),
            hint_texts=[
                getattr(item, "question", "") or "",
                getattr(item, "description", "") or "",
            ],
            unit_noun=unit_noun,
            display_labels=display_labels,
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

    if index is not None:
        # 检索位图只在后台线程建（整本约 2s 纯 CPU），不冻结与卷摘要/风格共用的事件循环
        await asyncio.to_thread(index.ensure_indexed)

    by_id = {c["candidate_id"]: c for c in candidates}

    def _seg_label(ch: int) -> str:
        """证据段标签。

        1.x：``第12章``（原样不动）。2.0：``文件366（第12回）``——**必须同时给出
        两个体系**：只给文件序号，模型会拿它去和候选元数据里的"首次出现：第12章"
        对不上，判定就会跑偏（AGY 复审 P1-3）。
        """
        if unit_noun == "章":
            return f"第{ch}章"
        return unit_label(ch, display_labels, noun="文件")

    def _render_batch(batch: list[dict]) -> str:
        rendered = "".join(
            ITEM_TEMPLATE.format(
                candidate_id=c["candidate_id"],
                description=getattr(c["item"], "description", ""),
                question=getattr(c["item"], "question", "") or "（未提供）",
                answer_criteria=getattr(c["item"], "answer_criteria", "") or "（未提供）",
                first_seen=getattr(c["item"], "first_seen_chapter", 0),
                last_seen=getattr(c["item"], "last_seen_chapter", 0),
                unit_note=(
                    ""
                    if unit_noun == "章"
                    else "（注意：下面的证据标签里，“文件N”是物理文件序号，"
                         "括号里的“第N回”是文学回号；它们与上面的章号不是同一套编号，"
                         "不要因为编号不同就以为给错了证据）"
                ),
                segment_count=len(c["segments"]),
                truncation_note=(
                    f"（注意：后文还有 {c.get('omitted', 0)} 处提到相关词但未展示，"
                    "判「没有回收」时要考虑这一点）"
                    if c.get("truncated")
                    else ""
                ),
                segments="\n".join(
                    f'[evidence_id={seg["evidence_id"]} {_seg_label(seg["chapter"])}]\n"""\n'
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
        if raw and not outcome.raw_sample:
            # 只留第一份非空样本：它的用途是"0 条判定时能看见模型到底回了什么"
            outcome.raw_sample = raw[:2000]
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
            verdict = parse_recheck(
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
            # 补问后同一候选可能留下两行结论——以最新一条为准，否则
            # count_verdicts 会把它们判成"重复候选"，两条都作废。
            for idx in range(len(outcome.verdicts) - 1, -1, -1):
                if outcome.verdicts[idx].candidate_id == verdict.candidate_id:
                    outcome.verdicts.pop(idx)
                    break
            outcome.verdicts.append(verdict)
            # 只有"拿到结论"或"注定拿不到"的才算处理完：
            # 可重试的无效响应（串号／引文找不到／没按格式答）不算——
            # 它要留给补问轮再试一次，否则 16 块实测里 13% 的候选会白丢。
            if verdict.checked or verdict.cached_as_terminal:
                done.add(c["candidate_id"])
        return done

    sem = limiter if limiter is not None else asyncio.Semaphore(max(1, concurrency))

    async def _ask(batch: list[dict]) -> tuple[list[dict], str]:
        async with sem:
            return batch, await call_model(_render_batch(batch))

    done: set[str] = set()
    built_count = 0
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
            # 惰性构建：只建**本波**要用的证据包——这是每条候选最贵的一步，
            # 预算耗尽后未建的候选永远不用付这笔钱
            newly_built = 0
            for b in wave:
                for cand in b:
                    if "segments" not in cand:
                        _build_evidence(cand)
                        newly_built += 1
            built_count += newly_built
            if emit is not None and newly_built:
                emit({
                    "stage": "build_candidates",
                    "built": built_count,
                    "total": len(candidates),
                })
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
            _apply_state(
                item, state, group,
                chapter_of=_chapter_of,
                unit_to_chapter=lambda u: _display_chapter(
                    u, display_labels, getattr(item, "anchor_chapter", 0),
                    noun=unit_noun,
                ),
            )

    pending = [cid for cid in outcome.not_checked if cid not in set(outcome.dropped_ids)]
    # 本轮**确实跑完了**（最多两轮：整批 + 补问），"有没有全部核对上"交给 counts 判定。
    # 原来传 finished=not pending，于是"跑完了但仍有候选没判定出结论"永远落成 RUNNING
    # ——而那恰恰是真实书上的常态（真书 16 块实测就有 1 条如此）。
    # 结果就是产物里写着"运行中"，看板和报告都以为这轮还没结束。
    outcome.status = recheck_status(
        started=True, finished=True, stopped=False, counts=counts
    )
    reasons: list[str] = []
    if pending:
        reasons.append(f"存在未核对候选（{len(pending)} 条）")
    dropped = outcome.dropped_no_anchor + outcome.dropped_anchor_missing
    if dropped:
        reasons.append(f"部分条目无可用证据（{dropped} 条未进入候选）")
    if reasons:
        outcome.status_reason = "；".join(reasons)
    if dropped and outcome.status == "COMPLETED":
        # 有条目因为没有证据而整条没查：这不是"完成"，只是"候选都处理完了"
        outcome.status = "PARTIAL"
        outcome.status_reason = (
            f"部分条目无可用证据（{dropped} 条未进入候选）"
            + (f"；存在未核对候选（{len(pending)} 条）" if pending else "")
        )
    # 「空判定」哨兵：调用发生了，却一条判定都没落账。
    #
    # 这个组合过去是**静默**的——2026-11-02 那轮整本复检 280 次调用全被官方
    # 端点以 400 unknown model 拒绝，而失败被 `content or ""` 吞掉，于是只留下
    # `calls=280 / checked=0` 的"PARTIAL"，跑了 28 分钟没人知道白跑。
    # 现在一律记 ERROR，并把首包样本留在 detail 里。
    zero_verdict = outcome.calls > 0 and int(outcome.coverage.get("checked", 0) or 0) == 0
    if zero_verdict:
        reason = (
            f"{outcome.calls} 次调用全部未返回可解析判定"
            "（检查判定模型是否被端点支持、以及网关/额度是否正常）"
        )
        outcome.status = "PARTIAL"
        outcome.status_reason = (outcome.status_reason + "；" + reason) if outcome.status_reason else reason
        logger.error(
            "复检 0 判定落账：calls=%s failed_batches=%s retried_batches=%s；回包样本=%r",
            outcome.calls, outcome.failed_batches, outcome.retried_batches,
            outcome.raw_sample[:200],
        )
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

    不能只依赖 ``extract_json_from_text``：它只抽出**第一个**对象——
    三条候选的数组进来，出去只有一条，看起来像"模型只答了一条"，
    实测模型其实答全了，是解析把它吃掉。因此这里自己做括号配平扫描。
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
    unit_to_chapter: Callable[[int], int] | None = None,
) -> None:
    """把实体级结论写回账本条目。**只有 AUTO_RESOLVED 才置 resolved。**

    `resolved_chapter` 取**引文所在章**，不是埋设章：一条伏笔在第 12 章埋、在第 900 章收，
    写回埋设章等于把"什么时候解决的"记错，而且账面看不出来。

    `chapter_of` 返回的是**检索单元键**（2.0 里是物理文件序号）；写进账本前必须经
    `unit_to_chapter` 转回**文学章号**——把 366 写进 `resolved_chapter` 会让
    "在第几章回收"整体错位，也会污染休眠跨度计算（AGY 复审 P1-2）。
    """
    supported = [v for v in group if v.verdict == "SUPPORTED"]
    if state == AUTO_RESOLVED and supported:
        best = supported[0]
        located_unit = None
        if chapter_of is not None:
            start = best.located_span[0] if best.located_span else None
            located_unit = chapter_of(best.evidence_id, start)
        located_chapter = None
        if located_unit is not None:
            located_chapter = unit_to_chapter(located_unit) if unit_to_chapter else located_unit
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


# ==================== audit 模式：存量回收清算 ====================
# （PLAN-foreshadow-adjudication D-13/D-14/D-15；AGY 复审 R-1/R-3/R-6 已并入）
#
# 与 run_recheck_v2（open 模式）是**两套判定模式**，不是同一管线的资格过滤变体：
# open 问"这条伏笔被回收了吗"，audit 问"账本里这条已有的回收结论是否成立"。
# 两者提示词、判定核、门禁语义、结论枚举都不同，最关键的一处反转：
# **open 里"SUPPORTED 却列了反证引文"只是降级为 INSUFFICIENT；
#   audit 里反证引文恰恰是"自述不成立"的直接证据。**
# 因此 audit 走独立管线与独立门禁，open 判定核（resolution.parse_recheck）一行不动。

AUDIT_VERIFIED = "VERIFIED"
AUDIT_QUESTIONED = "QUESTIONED"
#: 质疑原因枚举。与 open 路径的 INSUFFICIENT 语义隔离：
#: open 的 INSUFFICIENT 是"伏笔悬而未决"；audit 的 no_evidence 是"这份自述没能被证据支持"。
AUDIT_REASONS = ("supported", "no_evidence", "contradicted", "off_topic")

AUDIT_PROMPT_TEMPLATE = """你是长篇小说的伏笔审校员。任务：**复核一条已标记为"已回收"的伏笔**——
账本里记录了一条回收结论（下称"自述"），请对照给出的原文判断这个结论是否成立。

判定基准（必须遵守）：
1. 自述成立 = ①自述所述的事实能在原文中找到支持，**且**②按"判定为已回答的标准"它确实回答了这条伏笔的问题。
   自述是概括性文字，**不要**要求它与原文逐字对应；但也不要因为"原文里确实有那件事"就放行——
   若它只是阶段性推进、尚未回答问题（对照 answer_criteria），应判 QUESTIONED(off_topic)。
2. 只依据给出的原文；材料不足以判断时判 QUESTIONED(no_evidence)，不要用常理推测。
3. 必须做**反证检索**：在原文里找"自述不成立"的迹象。`counter_search.performed` 标 true，
   `checked_evidence_ids` 必须是 JSON 数组；`counter_quotes` 也是数组，没有反证就写 []。
   **注意：这里找到的反证引文是"自述不成立"的直接证据**——找到了就写进 counter_quotes 并判
   QUESTIONED(contradicted)，这不是自相矛盾，正是复核要找的东西。
4. 判 VERIFIED 时 `supporting_quote` 必须从原文**逐字照抄**不少于 6 个字（支持自述的原句），
   `evidence_id` 原样回填该段证据的值；找不到支持性原句 → 只能 QUESTIONED(no_evidence)。

判定枚举（只能用这两个词之一）：
- `VERIFIED`：自述成立，维持"已回收"
- `QUESTIONED`：自述不成立或无法验证（reason_code 三选一：no_evidence / contradicted / off_topic）

共 {count} 个候选，必须输出**长度 {count} 的 JSON 数组**，
每个候选恰好一条，`candidate_id` 必须原样取自：{candidate_ids}。

待复核候选：
{items}

输出：纯 JSON 数组，长度必须是 {count}。形状如下（数组元素同构，candidate_id 换成各候选的真实 id）：
[
  {{
    "candidate_id": "...",
    "verdict": "VERIFIED",
    "reason_code": "supported",
    "answer": "自述是否成立的一句话结论",
    "supporting_quote": "原文逐字引文（判 QUESTIONED 时可留空）",
    "evidence_id": "引文所在段的 evidence_id（判 QUESTIONED 时可留空）",
    "counter_search": {{"performed": true, "checked_evidence_ids": ["..."], "counter_quotes": []}}
  }},
  {{ ... 第 2 条，直到 {count} 条 ... }}
]
"""

AUDIT_ITEM_TEMPLATE = """### 候选 candidate_id={candidate_id}
伏笔描述：{description}
待交代事项（question）：{question}
判定为已回答的标准（answer_criteria）：{answer_criteria}
首次出现：第{first_seen}章
原自述回收（待复核的结论）：{resolution}
自述自报回收章：第{claimed_chapter}章

以下 {segment_count} 段原文各自有 evidence_id（第 1 段是自报回收章的固定窗，其余为按
"自述+问题+描述"检索出的命中段）。supporting_quote / counter_quotes 必须逐字取自
其中一段，evidence_id 原样回填——不同段是不同的证据，不要混：

{segments}
{truncation_note}
"""


def _parse_audit_payload(
    payload: Mapping[str, Any],
    *,
    candidate_id: str,
    allowed_evidence_ids: Iterable[str],
    locate: Callable[[str, str], Any],
) -> CandidateVerdict:
    """audit 门禁（D-13 + AGY 复审 R-3）：VERIFIED 三条件缺一不可，其余一律 QUESTIONED。

    与 open 的 parse_recheck 的语义差异：
    * 反证引文 = 质疑的直接证据（open 里只是降级 INSUFFICIENT）；
    * 没有可重试中间态——静态文本下补问是确定性冗余，miss 就如实落 QUESTIONED(no_evidence)；
    * 结论只有 VERIFIED/QUESTIONED 两态，reason_code 表达质疑原因。
    """

    def _questioned(reason: str, **kw: Any) -> CandidateVerdict:
        return CandidateVerdict(candidate_id, AUDIT_QUESTIONED, reason=reason, **kw)

    if not isinstance(payload, Mapping):
        return _questioned("no_evidence")
    model_verdict = payload.get("verdict")
    answer = payload.get("answer")
    answer = answer.strip() if isinstance(answer, str) else ""
    counter = payload.get("counter_search")
    performed = bool(isinstance(counter, Mapping) and counter.get("performed") is True)
    counter_quotes: list[str] = []
    if isinstance(counter, Mapping):
        raw_quotes = counter.get("counter_quotes") or []
        if isinstance(raw_quotes, list):
            counter_quotes = [q.strip() for q in raw_quotes if isinstance(q, str) and q.strip()]
    evidence_id = payload.get("evidence_id")
    evidence_ok = isinstance(evidence_id, str) and evidence_id in set(allowed_evidence_ids)

    if counter_quotes:
        # 反证引文 = 自述不成立的直接证据（audit 门禁反转点）
        return _questioned("contradicted", answer=answer, counter_search_performed=True)

    quote = payload.get("supporting_quote")
    quote = quote.strip() if isinstance(quote, str) else ""
    quote_ok = False
    located: tuple[int, int] | None = None
    ambiguous = False
    if evidence_ok and len(quote) >= 6:
        result = locate(quote, evidence_id)
        if result.found and not result.ambiguous:
            quote_ok = True
            located = (int(result.start), int(result.end))
        elif result.found:
            # 多处命中：audit 没有补问轮，宁可质疑也不误确认
            ambiguous = True

    if model_verdict == AUDIT_VERIFIED:
        # 三条件：①答案非空；②反证已执行且 checked_evidence_ids 非空且属于本题；
        # ③支持引文逐字落在点名的证据段里（不多处命中）。
        if not answer:
            return _questioned("no_evidence")
        if not performed:
            return _questioned("no_evidence")
        checked_ids = counter.get("checked_evidence_ids") if isinstance(counter, Mapping) else None
        allowed = set(allowed_evidence_ids)
        if not isinstance(checked_ids, (list, tuple)) or not checked_ids:
            return _questioned("no_evidence")
        if any(not isinstance(x, str) or x not in allowed for x in checked_ids):
            return _questioned("no_evidence")
        if not evidence_ok or len(quote) < 6 or ambiguous or not quote_ok:
            return _questioned("no_evidence")
        return CandidateVerdict(
            candidate_id,
            AUDIT_VERIFIED,
            answer=answer,
            evidence_id=evidence_id,
            located_span=located,
            quote_hash=hashlib.sha256(quote.encode("utf-8")).hexdigest()[:12],
            quote=quote,
            counter_search_performed=True,
            reason="supported",
        )

    # 模型自报 QUESTIONED（或响应无法识别）：统一质疑，原因取自报值（校验枚举）
    rc = payload.get("reason_code")
    if rc not in AUDIT_REASONS or rc == "supported":
        rc = "no_evidence"
    return _questioned(
        rc,
        answer=answer,
        counter_search_performed=performed,
        evidence_id=evidence_id if evidence_ok else None,
        quote=quote or None,
        quote_hash=hashlib.sha256(quote.encode("utf-8")).hexdigest()[:12] if quote else None,
        located_span=located,
    )


def _apply_audit_state(item: Any, record: CandidateVerdict) -> None:
    """audit 结论落账（D-13）：不改 status、不改 source_type——

    quality 表达"这份回收有没有被证据复核过"，status 的开/关不受影响；
    UNSUPPORTED 也不回 active（静态文本下重查是确定性冗余，重开交给人工出口）。
    """
    if record.verdict == AUDIT_VERIFIED:
        item.resolution_quality = "verified"
        item.needs_review = False
        notes = getattr(item, "notes", None)
        if isinstance(notes, list):
            notes.append("audit：自述回收经原文复核成立")
        return
    item.resolution_quality = "questioned"
    item.needs_review = True
    notes = getattr(item, "notes", None)
    if isinstance(notes, list):
        notes.append(f"audit：自述回收未获原文支持（{record.reason}），待人工确认")


async def run_recheck_audit(
    items: Sequence[Any],
    chapters: Mapping[int, str] | None,
    call_model: Callable[[str], Awaitable[str]],
    *,
    max_calls: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    concurrency: int = DEFAULT_CONCURRENCY,
    unit_noun: str = "章",
    display_labels: Mapping[int, str] | None = None,
    block_size: int = 4,
    emit: Callable[[dict], None] | None = None,
) -> RecheckOutcome:
    """存量回收清算（audit 模式）：复核 llm_judged 的"已回收"是否成立。

    资格（D-15，全量不抽样）：status=resolved 且 source_type=llm_judged 且
    resolution_quality 为空 且带 ≥6 字锚点且非 lead。confidence 不作抽样层——
    它是采集期变量，与回收期质量不相关。

    产出语义（D-13）：VERIFIED→quality=verified（不改 source_type、清 needs_review）；
    QUESTIONED→quality=questioned + needs_review=True（status 保持 resolved，
    不回 active）。单轮不补问；预算耗尽/无锚点/定位失败如实落 not_checked。
    audit 的候选与账本条目一一对应（复核对象就是条目自身的自述），不做实体归约。
    """
    outcome = RecheckOutcome()
    outcome.input_total = len(items)
    index = EvidenceIndex(chapters) if chapters else None
    candidates: list[dict] = []
    leads_skipped = 0

    for item in items:
        cid = str(getattr(item, "id", ""))
        if getattr(item, "lead", False):
            leads_skipped += 1
            continue
        if getattr(item, "status", "") != "resolved":
            continue
        if getattr(item, "source_type", "") != "llm_judged":
            continue
        if getattr(item, "resolution_quality", "") or "":
            continue  # 已复审过，不重复清算
        quote = (getattr(item, "anchor_quote", "") or "").strip()
        chapter = int(getattr(item, "anchor_chapter", 0) or 0)
        if index is None or not quote or chapter <= 0:
            outcome.dropped_no_anchor += 1
            outcome.not_checked.append(cid)
            outcome.dropped_ids.append(cid)
            continue
        # 锚点定位（与 open 同一套 range 优先逻辑）：llm_judged 的锚点章同样可能是
        # 合并块时代的"另一份同号章"，先修正再建锚窗
        start = int(getattr(item, "first_seen_chapter", 0) or 0)
        cand_range = (start, start + max(block_size, 1) - 1) if start > 0 else None
        hit = locate_with_mode(
            chapters or {},
            quote,
            candidate_range=cand_range,
            display_labels=display_labels,
            anchor_round=chapter or None,
        )
        unit = chapter
        if hit.unit is not None:
            located = locate_quote(quote, index.chapter_text(hit.unit))
            if located is not None and located.found:
                unit = hit.unit
        outcome.locate_records[cid] = {
            "unit": hit.unit,
            "mode": hit.mode,
            "source_chapter": start,
            "range": list(cand_range) if cand_range else None,
            "low_confidence": hit.mode != "range",
        }
        anchor_located = locate_quote(quote, index.chapter_text(unit))
        if anchor_located is None or not anchor_located.found:
            outcome.dropped_anchor_missing += 1
            outcome.not_checked.append(cid)
            outcome.dropped_ids.append(cid)
            continue
        candidates.append({
            "item": item,
            "candidate_id": cid,
            "evidence_id": _evidence_id(unit, quote),
            "chapter": unit,
        })

    outcome.leads_skipped = leads_skipped

    if not candidates:
        dropped = outcome.dropped_no_anchor + outcome.dropped_anchor_missing
        outcome.coverage = {
            "candidates_total": 0,
            "verified": 0,
            "questioned": 0,
            "checked": 0,
            "not_checked": len(outcome.not_checked),
            "dropped_no_anchor": outcome.dropped_no_anchor,
            "dropped_anchor_missing": outcome.dropped_anchor_missing,
            "skipped_ineligible": max(
                0, outcome.input_total - leads_skipped - dropped
            ),
        }
        outcome.status = "PARTIAL" if dropped else "COMPLETED"
        if dropped:
            outcome.status_reason = f"没有可复核条目（{dropped} 条无可用证据）"
        return outcome

    texts_by_evidence: dict[str, str] = {}
    chapter_by_evidence: dict[str, int] = {}

    def _build_evidence(cand: dict) -> None:
        if "segments" in cand:
            return
        item = cand["item"]
        # D-14：hint 带 resolution 文本——它描述了"谁在哪交代了什么"，
        # 是回收位置密度最高的检索线索
        bundle = index.bundle_for(
            anchor_chapter=cand["chapter"],
            anchor_quote=(getattr(item, "anchor_quote", "") or "").strip(),
            hint_texts=[
                (getattr(item, "resolution", "") or ""),
                getattr(item, "question", "") or "",
                getattr(item, "description", "") or "",
            ],
            unit_noun=unit_noun,
            display_labels=display_labels,
        )
        segments: list[dict] = []
        for chapter, seg_start, seg_end in bundle.spans:
            body = bundle.text[seg_start:seg_end].strip()
            eid = _evidence_id(chapter, body)
            texts_by_evidence[eid] = body
            chapter_by_evidence[eid] = chapter
            segments.append({"evidence_id": eid, "chapter": chapter, "text": body})
        # D-14（AGY 复审 R-1）：自报回收章强制固定窗 1——实测仅靠文本 hint，
        # 自报章进前三窗口的命中率只有 27.8%，排除它会让大多数条目连自述材料
        # 都看不到而被误杀成 no_evidence。窗口只是材料，是否支持由判定核裁决。
        claimed = int(getattr(item, "resolved_chapter", 0) or 0)
        if claimed > 0 and all(ch != claimed for ch, _s, _e in bundle.spans):
            body = index.window(claimed, needle=(getattr(item, "resolution", "") or "")[:12]).strip()
            if body:
                eid = _evidence_id(claimed, body)
                texts_by_evidence[eid] = body
                chapter_by_evidence[eid] = claimed
                segments.insert(0, {"evidence_id": eid, "chapter": claimed, "text": body})
        if not segments:
            fallback_text = index.chapter_text(cand["chapter"])
            eid = cand["evidence_id"]
            texts_by_evidence[eid] = fallback_text
            chapter_by_evidence[eid] = cand["chapter"]
            segments.append({"evidence_id": eid, "chapter": cand["chapter"], "text": fallback_text})
        cand["segments"] = segments
        cand["truncated"] = bool(getattr(bundle, "truncated", False))
        cand["omitted"] = int(getattr(bundle, "omitted", 0) or 0)

    def _seg_label(ch: int) -> str:
        if unit_noun == "章":
            return f"第{ch}章"
        return unit_label(ch, display_labels, noun="文件")

    def _render_audit_batch(batch: list[dict]) -> str:
        rendered = "".join(
            AUDIT_ITEM_TEMPLATE.format(
                candidate_id=c["candidate_id"],
                description=getattr(c["item"], "description", ""),
                question=getattr(c["item"], "question", "") or "（未提供）",
                answer_criteria=getattr(c["item"], "answer_criteria", "") or "（未提供）",
                first_seen=getattr(c["item"], "first_seen_chapter", 0),
                resolution=(getattr(c["item"], "resolution", "") or "").strip() or "（无）",
                claimed_chapter=getattr(c["item"], "resolved_chapter", 0) or 0,
                segment_count=len(c["segments"]),
                segments="\n".join(
                    f'[evidence_id={seg["evidence_id"]} {_seg_label(seg["chapter"])}]\n"""\n'
                    f'{seg["text"]}\n"""'
                    for seg in c["segments"]
                ),
                truncation_note=(
                    f"（注意：后文还有 {c.get('omitted', 0)} 处提到相关词但未展示，"
                    "下结论时要考虑这一点）"
                    if c.get("truncated")
                    else ""
                ),
            )
            for c in batch
        )
        return AUDIT_PROMPT_TEMPLATE.format(
            count=len(batch),
            items=rendered,
            candidate_ids=", ".join(c["candidate_id"] for c in batch),
        )

    def _consume_audit(batch: list[dict], raw: str) -> set[str]:
        if raw and not outcome.raw_sample:
            outcome.raw_sample = raw[:2000]
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
                payload = {**payload, "evidence_id": allowed[0]}
            record = _parse_audit_payload(
                payload,
                candidate_id=c["candidate_id"],
                allowed_evidence_ids=allowed,
                locate=lambda q, eid: locate_quote(q, texts_by_evidence.get(eid, "")),
            )
            for idx in range(len(outcome.verdicts) - 1, -1, -1):
                if outcome.verdicts[idx].candidate_id == record.candidate_id:
                    outcome.verdicts.pop(idx)
                    break
            outcome.verdicts.append(record)
            # audit 结论全部终态（VERIFIED/QUESTIONED 都不再补问——静态文本
            # 下补问是确定性冗余），落账即完成
            done.add(c["candidate_id"])
        return done

    if index is not None:
        # 检索位图后台线程构建，不冻结共用的事件循环
        await asyncio.to_thread(index.ensure_indexed)

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _ask(batch: list[dict]) -> tuple[list[dict], str]:
        async with sem:
            return batch, await call_model(_render_audit_batch(batch))

    done: set[str] = set()
    built_count = 0
    step = max(batch_size, 1)
    batches = [candidates[i : i + step] for i in range(0, len(candidates), step)]
    width = max(1, concurrency)
    for wave_at in range(0, len(batches), width):
        if outcome.calls >= max_calls:
            break
        affordable = max_calls - outcome.calls
        if affordable <= 0:
            break
        wave = batches[wave_at : wave_at + width][:affordable]
        if not wave:
            break
        newly_built = 0
        for b in wave:
            for cand in b:
                if "segments" not in cand:
                    _build_evidence(cand)
                    newly_built += 1
        built_count += newly_built
        if emit is not None and newly_built:
            emit({"stage": "build_candidates", "built": built_count, "total": len(candidates)})
        outcome.calls += len(wave)
        results = await asyncio.gather(*(_ask(b) for b in wave), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                outcome.failed_batches += 1
                continue
            batch, raw = result
            done |= _consume_audit(batch, raw)
        if emit is not None:
            emit({"stage": "recheck", "calls": outcome.calls, "checked": len(done)})

    outcome.not_checked.extend(
        c["candidate_id"] for c in candidates if c["candidate_id"] not in done
    )

    verified = sum(1 for v in outcome.verdicts if v.verdict == AUDIT_VERIFIED)
    questioned = sum(1 for v in outcome.verdicts if v.verdict == AUDIT_QUESTIONED)
    pending = [cid for cid in outcome.not_checked if cid not in set(outcome.dropped_ids)]
    dropped = outcome.dropped_no_anchor + outcome.dropped_anchor_missing
    outcome.coverage = {
        "candidates_total": len(candidates),
        "verified": verified,
        "questioned": questioned,
        "checked": verified + questioned,
        # 只计候选中未核对的（掉队的在 dropped_* 单列，不重复计数）
        "not_checked": len(pending),
        "dropped_no_anchor": outcome.dropped_no_anchor,
        "dropped_anchor_missing": outcome.dropped_anchor_missing,
        # 资格不符被跳过的条目（非 resolved / 非 llm_judged / 已复审）——
        # 没有这一栏，"输入总数"就追不到分母，守恒会开天窗
        "skipped_ineligible": max(
            0,
            outcome.input_total - leads_skipped - len(candidates)
            - outcome.dropped_no_anchor - outcome.dropped_anchor_missing,
        ),
    }

    # audit 的候选与账本条目一一对应（复核对象就是条目自身的自述），逐条落账；
    # source_type 与 status 均不改写，保留考古痕迹（AGY R-5）
    by_cid = {c["candidate_id"]: c["item"] for c in candidates}
    for verdict in outcome.verdicts:
        outcome.applied[verdict.candidate_id] = (
            "CONFIRMED" if verdict.verdict == AUDIT_VERIFIED else "UNSUPPORTED"
        )
        item = by_cid.get(verdict.candidate_id)
        if item is not None:
            _apply_audit_state(item, verdict)

    pending = [cid for cid in outcome.not_checked if cid not in set(outcome.dropped_ids)]
    outcome.status = "COMPLETED" if not pending and not dropped else "PARTIAL"
    reasons: list[str] = []
    if pending:
        reasons.append(f"存在未核对候选（{len(pending)} 条）")
    if dropped:
        reasons.append(f"部分条目无可用证据（{dropped} 条未进入候选）")
    outcome.status_reason = "；".join(reasons)
    if outcome.calls > 0 and verified + questioned == 0:
        reason = f"{outcome.calls} 次调用全部未返回可解析判定（检查判定通道与网关是否正常）"
        outcome.status = "PARTIAL"
        outcome.status_reason = (outcome.status_reason + "；" + reason) if outcome.status_reason else reason
        logger.error(
            "audit 0 判定落账：calls=%s failed_batches=%s；回包样本=%r",
            outcome.calls, outcome.failed_batches, outcome.raw_sample[:200],
        )
    if emit is not None:
        emit(outcome.summary())
    return outcome
