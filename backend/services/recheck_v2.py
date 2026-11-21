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
import re
import time as _time_mod
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
#: 审核输出**格式非法**时允许的重试次数（上限 1）。
#: 只对格式失败重试：模型没说清话，再问一次可能说清。
#: 语义拒绝（restating/off_topic）**绝不重试**——重试到"通过"等于放宽审核标准。
AUDIT_FORMAT_RETRY_LIMIT = 1

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
    audit_routes: dict[str, dict] = field(default_factory=dict)
    audit_calls: int = 0
    audit_cache_hits: int = 0
    recheck_cache_hits: int = 0
    audit_pending_ids: list[str] = field(default_factory=list)
    #: 因**输出格式非法**而重试的次数（语义拒绝不计入、不重试）
    audit_format_retries: int = 0

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "audit_calls": self.audit_calls,
            "audit_cache_hits": self.audit_cache_hits,
            "recheck_cache_hits": self.recheck_cache_hits,
            "audit_format_retries": self.audit_format_retries,
            "audit_pending_ids": list(self.audit_pending_ids),
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
        "locate_records": dict(outcome.locate_records),
        "audit_routes": dict(outcome.audit_routes),
        "audit_calls": outcome.audit_calls,
        "audit_cache_hits": outcome.audit_cache_hits,
        "recheck_cache_hits": outcome.recheck_cache_hits,
        "audit_pending_ids": list(outcome.audit_pending_ids),
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


def _compute_cited_ordering(
    *,
    chapters: Mapping[int, str] | None,
    audit_item: Mapping[str, Any],
    nli_obj: Any,
) -> dict | None:
    """按**裁决实际引用的句子**算时序。

    窗口只当来源容器：裁决没引用的句子不该参与判断，
    窗口包住锚点也不该把窗口内部的句子一并豁免。

    时序结果**不进请求身份**：它由同一份证据推导，放进 audit_item 会改掉
    input_fingerprint，使已有审计缓存全部失效。
    """
    try:
        from backend.common.evidence_ordering import resolve_cited_ordering

        return resolve_cited_ordering(
            chapters=chapters,
            anchor_quote=str(audit_item.get("anchor_quote") or ""),
            evidence_segments=audit_item.get("evidence_segments") or [],
            nli=nli_obj if isinstance(nli_obj, Mapping) else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"时序判定失败，按时序待定处理：{exc}")
        return None


def _regate_cached_record(
    entry: Mapping[str, Any],
    *,
    audit_item: dict,
    chapters: Mapping[int, str] | None,
    gate_fn: Callable[..., dict],
    parse_fn: Callable[..., Any],
) -> dict:
    """缓存命中时**重跑本地门禁**，而不是沿用旧裁决。

    复用的只有模型原始响应——**不增加模型调用**。
    但最终裁决依赖时序判定，时序依赖原文与实际引用跨度：
    原文变了、引用变了，旧裁决就可能不再成立，所以必须重裁。

    **重裁失败就待审**，不沿用旧裁决：重裁做不成恰恰说明旧结论是否仍有效
    无法确认——没有原始响应就无从重裁，解析不了也一样。
    ``regated:false`` 只说明"没重裁"，不能替旧结论背书。
    旧裁决保留在缓存里供追查，但不控制当前通过状态。
    """
    raw = str(entry.get("raw_response_text") or "")
    if not raw:
        return {
            "decision": None,
            "assessment_status": "unconfirmed",
            "reasons": [],
            "claim_check": None,
            "regated": False,
            "pending_review": True,
            "regate_note": "缓存记录没有原始响应，无法重裁；旧裁决不作数，转待审",
        }
    nli_obj = parse_fn(raw)
    if not isinstance(nli_obj, dict):
        return {
            "decision": None,
            "assessment_status": "unconfirmed",
            "reasons": [],
            "claim_check": None,
            "regated": False,
            "pending_review": True,
            "regate_note": "缓存的原始响应无法解析，无法重裁；旧裁决不作数，转待审",
        }
    ordering = _compute_cited_ordering(
        chapters=chapters, audit_item=audit_item, nli_obj=nli_obj
    )
    if ordering is not None:
        audit_item["evidence_ordering"] = ordering
    fresh = gate_fn(audit_item, nli_obj)
    return {
        "decision": fresh.get("decision"),
        "assessment_status": fresh.get("assessment_status", "unconfirmed"),
        "reasons": list(fresh.get("reasons") or []),
        "claim_check": fresh.get("claim_check"),
        "regated": True,
        "pending_review": False,
        "regate_note": "缓存命中后按最新原文绑定与时序结果重跑本地门禁",
    }


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
    trace: Any = None,
    enhanced_retrieval: bool = False,
    enable_c_on_demand_audit: bool = False,
    call_audit_model: Callable[[str], Awaitable[str]] | None = None,
    audit_cache: dict[str, dict[str, Any]] | None = None,
    recheck_cache: dict[str, dict[str, Any]] | None = None,
    audit_model_config: Mapping[str, Any] | None = None,
    request_log: dict[str, Any] | None = None,
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
            if trace is not None:
                trace.record(
                    item_id=str(getattr(item, "id", "")),
                    stage="recheck_gate",
                    state_before="active",
                    state_after="skipped_lead",
                    reason_code="LEAD_SKIPPED",
                )
            continue
        quote = (getattr(item, "anchor_quote", "") or "").strip()
        chapter = int(getattr(item, "anchor_chapter", 0) or 0)
        if not quote or chapter <= 0:
            # 没有锚点 = 没有证据。这类条目只能标"证据不足"，不能凭模型自述判回收。
            outcome.dropped_no_anchor += 1
            outcome.not_checked.append(getattr(item, "id", ""))
            outcome.dropped_ids.append(getattr(item, "id", ""))
            if trace is not None:
                trace.record(
                    item_id=str(getattr(item, "id", "")),
                    stage="recheck_gate",
                    state_before="active",
                    state_after="dropped_no_anchor",
                    reason_code="DROPPED_NO_ANCHOR",
                )
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
            if trace is not None:
                trace.record(
                    item_id=str(getattr(item, "id", "")),
                    stage="recheck_gate",
                    state_before="active",
                    state_after="dropped_anchor_missing",
                    reason_code="DROPPED_ANCHOR_MISSING",
                    meta={"anchor_chapter": chapter},
                )
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
        if enhanced_retrieval:
            ek = str(getattr(item, "entity_key", "") or "").strip()
            ek_hint = "" if ek.startswith("clue:") else ek
            hint_list = [
                getattr(item, "question", "") or "",
                getattr(item, "description", "") or "",
                getattr(item, "answer_criteria", "") or "",
                getattr(item, "resolution", "") or "",
                ek_hint,
            ]
            nom_chapters: list[int] = []
            res_ch = int(getattr(item, "resolved_chapter", 0) or 0)
            if res_ch > cand["chapter"]:
                nom_chapters.append(res_ch)
                for offset in range(1, max(block_size, 1)):
                    if (res_ch - offset) > cand["chapter"]:
                        nom_chapters.append(res_ch - offset)
            for ev_ch in (getattr(item, "evidence_chapters", None) or []):
                if int(ev_ch or 0) > cand["chapter"]:
                    nom_chapters.append(int(ev_ch))
            bundle = index.bundle_for(
                anchor_chapter=cand["chapter"],
                anchor_quote=(getattr(item, "anchor_quote", "") or "").strip(),
                hint_texts=hint_list,
                unit_noun=unit_noun,
                display_labels=display_labels,
                nominated_chapters=nom_chapters,
                density_window=True,
            )
        else:
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
        if trace is not None:
            seg_chs = [int(s["chapter"]) for s in segments]
            nom_ch = int(getattr(item, "resolved_chapter", 0) or 0)
            if nom_ch > 0 and not any(abs(ch - nom_ch) < max(block_size, 1) for ch in seg_chs):
                trace.record(
                    item_id=cand["candidate_id"],
                    stage="evidence_retrieve",
                    state_before="nominated_resolution",
                    state_after="nominated_chapter_missed",
                    reason_code="NOMINATED_CHAPTER_NOT_IN_BUNDLE",
                    meta={"nominated_chapter": nom_ch, "bundle_chapters": seg_chs},
                )
            if getattr(bundle, "truncated", False):
                trace.record(
                    item_id=cand["candidate_id"],
                    stage="evidence_retrieve",
                    state_before="hit_chapters",
                    state_after="bundle_truncated",
                    reason_code="EVIDENCE_HITS_TRUNCATED",
                    meta={"omitted": getattr(bundle, "omitted", 0), "bundle_chapters": seg_chs},
                )
            trace.record(
                item_id=cand["candidate_id"],
                stage="evidence_retrieve",
                state_before="candidate_located",
                state_after="bundle_built",
                reason_code="EVIDENCE_BUNDLE_BUILT",
                meta={"bundle_chapters": seg_chs},
            )

    if index is not None:
        # 检索位图只在后台线程建（整本约 2s 纯 CPU），不冻结与卷摘要/风格共用的事件循环
        await asyncio.to_thread(index.ensure_indexed)

    by_id = {c["candidate_id"]: c for c in candidates}

    # ---- 行车记录仪（只记录，不参与判定）----
    _current_round = [0]
    _batch_counter = [0]

    def _candidate_meta(c: dict) -> dict:
        """重建提示词所需的候选元数据（不含模型输出）。"""
        item_obj = c.get("item")
        return {
            "candidate_id": c["candidate_id"],
            "description": getattr(item_obj, "description", "") or "",
            "question": getattr(item_obj, "question", "") or "",
            "answer_criteria": getattr(item_obj, "answer_criteria", "") or "",
            "first_seen": getattr(item_obj, "first_seen_chapter", 0),
            "last_seen": getattr(item_obj, "last_seen_chapter", 0),
            "chapter": int(c.get("chapter") or 0),
            "omitted": int(c.get("omitted", 0) or 0),
            "truncated": bool(c.get("truncated")),
        }

    def _new_request_record(
        log: dict, *, batch: list[dict], prompt: str, round_index: int
    ) -> dict:
        from backend.common.recheck_request_log import build_request_record

        _batch_counter[0] += 1
        evidence_batches = []
        for c in batch:
            for seg in c.get("segments") or []:
                evidence_batches.append(
                    {
                        "candidate_id": c["candidate_id"],
                        "evidence_id": seg.get("evidence_id"),
                        "chapter": seg.get("chapter"),
                        "text": seg.get("text"),
                        "text_sha256": hashlib.sha256(
                            str(seg.get("text") or "").encode("utf-8")
                        ).hexdigest(),
                    }
                )
        rec = build_request_record(
            run=log.get("run") or {},
            batch_index=_batch_counter[0],
            round_index=round_index,
            candidate_ids=[c["candidate_id"] for c in batch],
            prompt=prompt,
            evidence_batches=evidence_batches,
            attempt_records=[],
            contract_version=str((log.get("run") or {}).get("contract_version") or "recheck_v2"),
        )
        rec["candidate_meta"] = [_candidate_meta(c) for c in batch]
        rec["recorded_at"] = _time_mod.strftime("%Y-%m-%dT%H:%M:%S%z")
        log.setdefault("requests", []).append(rec)
        return rec

    def _append_attempt(
        rec: dict,
        *,
        seconds: float,
        response_text: str,
        error: str | None,
        request_log: dict | None,
        prompt: str,
    ) -> None:
        from backend.common.recheck_request_log import sha256_text

        usage = None
        if request_log is not None:
            usage = (request_log.get("usage_by_prompt_sha256") or {}).get(
                sha256_text(prompt)
            )
        rec.setdefault("attempts", []).append(
            {
                "attempt": len(rec.get("attempts") or []) + 1,
                "seconds": seconds,
                "response_text": response_text,
                "response_sha256": sha256_text(response_text),
                "response_length": len(response_text or ""),
                "error": error,
                "usage": usage,
            }
        )

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
            elif enhanced_retrieval and payload.get("verdict") in ("SUPPORTED", "DIFFERENT_ANSWER", "CONTRADICTED") and payload.get("supporting_quote"):
                raw_eid = str(payload.get("evidence_id") or "")
                raw_quote = str(payload.get("supporting_quote") or "")
                need_align = (
                    raw_eid not in allowed
                    or not locate_quote(raw_quote, texts_by_evidence.get(raw_eid, "")).found
                )
                if need_align:
                    matching_eids = [
                        seg["evidence_id"]
                        for seg in c["segments"]
                        if locate_quote(raw_quote, texts_by_evidence.get(seg["evidence_id"], "")).found
                    ]
                    if len(matching_eids) == 1:
                        aligned_eid = matching_eids[0]
                        raw_cs = payload.get("counter_search")
                        if isinstance(raw_cs, Mapping) and isinstance(
                            raw_cs.get("checked_evidence_ids"), (list, tuple)
                        ):
                            raw_checked = list(raw_cs["checked_evidence_ids"])
                            if raw_eid and raw_eid in raw_checked:
                                cs = dict(raw_cs)
                                cs["checked_evidence_ids"] = [
                                    aligned_eid if x == raw_eid else x for x in raw_checked
                                ]
                                payload = {
                                    **payload,
                                    "evidence_id": aligned_eid,
                                    "counter_search": cs,
                                }
                            else:
                                payload = {**payload, "evidence_id": aligned_eid}
                        else:
                            payload = {**payload, "evidence_id": aligned_eid}
                        if trace is not None:
                            trace.record(
                                item_id=c["candidate_id"],
                                stage="recheck_parse",
                                state_before=f"eid:{raw_eid}",
                                state_after=f"eid:{aligned_eid}",
                                reason_code="INTRA_CANDIDATE_EVIDENCE_ID_ALIGNED",
                            )
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
            anchor_quote_raw = str(
                getattr(c.get("item"), "anchor_quote", "") or c.get("anchor_quote") or ""
            )
            if (
                enhanced_retrieval
                and verdict.verdict in ("SUPPORTED", "DIFFERENT_ANSWER")
                and verdict.quote
                and anchor_quote_raw
            ):
                q_norm = re.sub(r"\W+", "", verdict.quote)
                a_norm = re.sub(r"\W+", "", anchor_quote_raw)
                if len(q_norm) >= 6 and a_norm and q_norm in a_norm:
                    verdict = CandidateVerdict(
                        c["candidate_id"],
                        "INSUFFICIENT",
                        reason="QUOTE_EQUALS_ANCHOR",
                        counter_search_performed=verdict.counter_search_performed,
                    )
            if enable_c_on_demand_audit and verdict.verdict in ("SUPPORTED", "DIFFERENT_ANSWER"):
                from backend.common.c_audit_selector import select_audit_route
                item_obj = c.get("item")
                route_dec = select_audit_route(
                    {
                        "candidate_id": c["candidate_id"],
                        "question": getattr(item_obj, "question", "") or c.get("question", ""),
                        "answer_criteria": getattr(item_obj, "answer_criteria", "") or c.get("answer_criteria", ""),
                        "answer": verdict.answer,
                        "supporting_quote": verdict.quote,
                        "anchor_quote": anchor_quote_raw,
                        "anchor_chapter": int(getattr(item_obj, "anchor_chapter", 0) or c.get("anchor_chapter") or 0),
                        "chapter": int(chapter_by_evidence.get(verdict.evidence_id, 0) or 0),
                        "description": getattr(item_obj, "description", "") or c.get("description", ""),
                    },
                    enabled=True,
                )
                outcome.audit_routes[c["candidate_id"]] = {
                    "route": route_dec.route,
                    "trigger_claims_v2": route_dec.trigger_claims_v2,
                    "trigger_reasons": list(route_dec.trigger_reasons),
                    "hard_signals": list(route_dec.hard_signals),
                    "soft_signals": list(route_dec.soft_signals),
                    "answer_unit_count": route_dec.answer_unit_count,
                    "criteria_logic_type": route_dec.criteria_logic_type,
                    "audit_executed": False,
                    "audit_status": (
                        "rejected_mechanical"
                        if route_dec.route == "hard_reject_mechanical"
                        else ("pending_review" if route_dec.trigger_claims_v2 else "not_triggered")
                    ),
                }
                if route_dec.route == "hard_reject_mechanical":
                    outcome.audit_routes[c["candidate_id"]]["audit_decision"] = "拒绝"
                    verdict = CandidateVerdict(
                        c["candidate_id"],
                        "INSUFFICIENT",
                        answer=verdict.answer,
                        evidence_id=verdict.evidence_id,
                        located_span=verdict.located_span,
                        quote_hash=verdict.quote_hash,
                        quote=verdict.quote,
                        reason="MECHANICAL_INCREMENT_REJECTED",
                        counter_search_performed=verdict.counter_search_performed,
                    )
                elif route_dec.trigger_claims_v2:
                    # T1: 初次 SUPPORTED 仅是暂定结果。复审完成前不得写为 SUPPORTED/resolved。
                    pending_upgrade_audits[c["candidate_id"]] = (c, verdict, route_dec)
                    verdict = CandidateVerdict(
                        c["candidate_id"],
                        "INSUFFICIENT",
                        answer=verdict.answer,
                        evidence_id=verdict.evidence_id,
                        located_span=verdict.located_span,
                        quote_hash=verdict.quote_hash,
                        quote=verdict.quote,
                        reason="C_AUDIT_PENDING:UNFINISHED",
                        counter_search_performed=verdict.counter_search_performed,
                    )
            elif not enable_c_on_demand_audit and verdict.verdict in ("SUPPORTED", "DIFFERENT_ANSWER"):
                # 开关关闭时不触发任何升级审核，保留原行为
                pass
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
                if recheck_cache is not None:
                    _store_recheck_cache_entry(c, raw, verdict)
        return done

    pending_upgrade_audits: dict[str, tuple[dict, CandidateVerdict, Any]] = {}
    sem = limiter if limiter is not None else asyncio.Semaphore(max(1, concurrency))

    def _set_verdict(new_v: CandidateVerdict) -> None:
        for idx in range(len(outcome.verdicts) - 1, -1, -1):
            if outcome.verdicts[idx].candidate_id == new_v.candidate_id:
                outcome.verdicts.pop(idx)
                break
        outcome.verdicts.append(new_v)

    def _build_recheck_identity(c: dict) -> tuple[dict[str, Any], str, dict[str, Any]]:
        from backend.common.provenance_cache import build_request_identity
        item_obj = c.get("item")
        rc_item = {
            "fs_id": c["candidate_id"],
            "question": getattr(item_obj, "question", "") or c.get("question", ""),
            "criteria": getattr(item_obj, "answer_criteria", "") or c.get("answer_criteria", ""),
            "description": getattr(item_obj, "description", "") or c.get("description", ""),
            "anchor_quote": str(getattr(item_obj, "anchor_quote", "") or c.get("anchor_quote") or ""),
            "anchor_chapter": int(getattr(item_obj, "anchor_chapter", 0) or c.get("anchor_chapter") or 0),
            "answer": "",
            "quote": "",
            "chapter": int(c.get("chapter") or 0),
            "prereg": getattr(item_obj, "prereg", None),
            "evidence_segments": list(c.get("segments") or []),
        }
        single_prompt = _render_batch([c])
        ident = build_request_identity(
            rc_item,
            prompt=single_prompt,
            model_config=audit_model_config or {},
            contract_version="recheck_v2",
        )
        return rc_item, single_prompt, ident

    def _store_recheck_cache_entry(c: dict, raw_text: str, v: CandidateVerdict) -> None:
        if recheck_cache is None or not raw_text:
            return
        from backend.common.provenance_cache import build_verified_cache_record
        rc_item, _, ident = _build_recheck_identity(c)
        rec = build_verified_cache_record(
            rc_item,
            ident,
            raw_response_text=raw_text,
            gate_result={"decision": v.verdict, "assessment_status": "accepted" if v.verdict == "SUPPORTED" else "unconfirmed", "reasons": [v.reason] if v.reason else []},
            tokens=0,
            seconds=0.0,
            attempts=1,
            success=v.checked,
        )
        recheck_cache[ident["request_id"]] = rec
        recheck_cache[c["candidate_id"]] = rec

    async def _execute_pending_upgrade_audits() -> None:
        if not pending_upgrade_audits:
            return
        import time as _time
        from backend.common.c_claim_audit_gate import (
            CLAIM_V2,
            build_nli_prompt,
            extract_json_object,
            extract_json_object_with_repair,
            gate,
        )
        from backend.common.provenance_cache import (
            build_request_identity,
            build_verified_cache_record,
            re_adjudicate_cached_record,
            verify_cache_entry,
        )

        audit_fn = call_audit_model or call_model
        for cid, (c, tentative_v, _route_dec) in list(pending_upgrade_audits.items()):
            pending_upgrade_audits.pop(cid, None)
            item_obj = c.get("item")
            allowed_segs = list(c.get("segments") or [])
            allowed_eids = {str(s.get("evidence_id") or "") for s in allowed_segs}
            if (
                not allowed_segs
                or not tentative_v.evidence_id
                or tentative_v.evidence_id not in allowed_eids
                or not locate_quote(
                    tentative_v.quote,
                    texts_by_evidence.get(tentative_v.evidence_id, ""),
                ).found
            ):
                _set_verdict(
                    CandidateVerdict(
                        cid,
                        "INSUFFICIENT",
                        answer=tentative_v.answer,
                        evidence_id=tentative_v.evidence_id,
                        located_span=tentative_v.located_span,
                        quote_hash=tentative_v.quote_hash,
                        quote=tentative_v.quote,
                        reason="C_AUDIT_REJECTED:EVIDENCE_MISSING_OR_FOREIGN",
                        counter_search_performed=tentative_v.counter_search_performed,
                    )
                )
                outcome.audit_routes[cid].update(
                    {
                        "audit_executed": False,
                        "audit_status": "rejected_evidence_missing",
                        "audit_decision": "拒绝",
                        "audit_reasons": ["EVIDENCE_MISSING_OR_FOREIGN"],
                    }
                )
                continue

            ev_ch = int(chapter_by_evidence.get(tentative_v.evidence_id, 0) or c.get("chapter") or 0)
            audit_item = {
                "fs_id": cid,
                "question": getattr(item_obj, "question", "") or c.get("question", ""),
                "criteria": getattr(item_obj, "answer_criteria", "") or c.get("answer_criteria", ""),
                "description": getattr(item_obj, "description", "") or c.get("description", ""),
                "anchor_quote": str(getattr(item_obj, "anchor_quote", "") or c.get("anchor_quote") or ""),
                "anchor_chapter": int(getattr(item_obj, "anchor_chapter", 0) or c.get("anchor_chapter") or 0),
                "answer": tentative_v.answer,
                "quote": tentative_v.quote,
                "chapter": ev_ch,
                "prereg": getattr(item_obj, "prereg", None) or c.get("prereg"),
                "evidence_segments": allowed_segs,
                "evidence_contract_version": CLAIM_V2,
            }
            prompt = build_nli_prompt(audit_item)
            identity = build_request_identity(
                audit_item,
                prompt=prompt,
                model_config=audit_model_config or {},
                contract_version=CLAIM_V2,
            )

            gate_res: dict[str, Any] | None = None
            cache_status = "miss"
            if audit_cache is not None:
                cand_entry = (
                    audit_cache.get(identity["request_id"])
                    or audit_cache.get(f"{cid}::{CLAIM_V2}")
                    or audit_cache.get(cid)
                )
                if cand_entry is not None:
                    v_check = verify_cache_entry(cand_entry, identity)
                    cache_status = v_check.status
                    if v_check.status == "verified_hit":
                        outcome.audit_cache_hits += 1
                        # 命中缓存**不等于**沿用旧裁决。
                        #
                        # 请求身份覆盖的是提示词与证据指纹；最终裁决还依赖
                        # 时序判定——而时序依赖**原文**与**实际引用的跨度**，
                        # 两者都不在请求身份里。原文变了、引用变了，
                        # 旧裁决就可能不再成立。
                        #
                        # 这里复用同一份模型原始响应，**不增加模型调用**，
                        # 但用最新的原文绑定与时序结果**重跑本地门禁**。
                        gate_res = _regate_cached_record(
                            cand_entry,
                            audit_item=audit_item,
                            chapters=chapters,
                            gate_fn=gate,
                            # extract_json_object_with_repair 返回 (dict|None, 修复记录)，
                            # 这里只需要解析出的对象
                            parse_fn=lambda raw: extract_json_object_with_repair(raw)[0],
                        )
                        if gate_res.get("pending_review"):
                            # 重裁没做成 → **待审**，不是沿用旧裁决。
                            #
                            # 重裁失败恰恰说明旧结论是否仍有效**无法确认**：
                            # 没有原始响应就无从重裁，解析不了也一样。
                            # `regated:false` 只说明"没重裁"，不能替旧结论背书。
                            # 旧裁决保留在缓存里供追查，但不控制当前通过状态。
                            _set_verdict(
                                CandidateVerdict(
                                    cid,
                                    "INSUFFICIENT",
                                    answer=tentative_v.answer,
                                    evidence_id=tentative_v.evidence_id,
                                    located_span=tentative_v.located_span,
                                    quote_hash=tentative_v.quote_hash,
                                    quote=tentative_v.quote,
                                    reason="C_AUDIT_PENDING:REGATE_UNVERIFIED",
                                    counter_search_performed=tentative_v.counter_search_performed,
                                )
                            )
                            outcome.audit_routes[cid].update(
                                {
                                    "audit_executed": True,
                                    "audit_status": "pending_review",
                                    "audit_decision": "待审",
                                    "audit_reasons": [str(gate_res.get("regate_note") or "")],
                                    "request_id": identity["request_id"],
                                    "cache_status": cache_status,
                                    "regated": False,
                                    "prior_decision_for_trace": cand_entry.get("decision"),
                                    "recovery": (
                                        "旧裁决因无法重裁而不作数；再次提交会重新调用审核"
                                    ),
                                }
                            )
                            if cid not in outcome.audit_pending_ids:
                                outcome.audit_pending_ids.append(cid)
                            continue
                    elif v_check.status == "re_adjudicate_needed":
                        re_adj = re_adjudicate_cached_record(
                            cand_entry,
                            audit_item,
                            identity,
                            ordering_fn=lambda it, nli: _compute_cited_ordering(
                                chapters=chapters, audit_item=it, nli_obj=nli
                            ),
                        )
                        audit_cache[identity["request_id"]] = re_adj
                        audit_cache[f"{cid}::{CLAIM_V2}"] = re_adj
                        outcome.audit_cache_hits += 1
                        gate_res = {
                            "decision": re_adj["decision"],
                            "assessment_status": re_adj.get("assessment_status", "unconfirmed"),
                            "reasons": list(re_adj.get("reasons", [])),
                            "claim_check": re_adj.get("claim_check"),
                        }

            if gate_res is None:
                if outcome.calls >= max_calls:
                    _set_verdict(
                        CandidateVerdict(
                            cid,
                            "INSUFFICIENT",
                            answer=tentative_v.answer,
                            evidence_id=tentative_v.evidence_id,
                            located_span=tentative_v.located_span,
                            quote_hash=tentative_v.quote_hash,
                            quote=tentative_v.quote,
                            reason="C_AUDIT_PENDING:BUDGET_EXHAUSTED",
                            counter_search_performed=tentative_v.counter_search_performed,
                        )
                    )
                    outcome.audit_routes[cid].update(
                        {
                            "audit_executed": False,
                            "audit_status": "pending_review",
                            "audit_decision": "待审",
                            "audit_reasons": ["BUDGET_EXHAUSTED"],
                            "request_id": identity["request_id"],
                            "cache_status": cache_status,
                        }
                    )
                    if cid not in outcome.audit_pending_ids:
                        outcome.audit_pending_ids.append(cid)
                    continue

                outcome.calls += 1
                outcome.audit_calls += 1
                t0 = _time.perf_counter()
                try:
                    async with sem:
                        raw_audit = await audit_fn(prompt)
                    sec = round(_time.perf_counter() - t0, 3)
                except BaseException as exc:
                    sec = round(_time.perf_counter() - t0, 3)
                    outcome.failed_batches += 1
                    err_tag = (
                        "TIMEOUT"
                        if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                        else type(exc).__name__
                    )
                    if audit_cache is not None:
                        audit_cache[identity["request_id"]] = build_verified_cache_record(
                            audit_item,
                            identity,
                            raw_response_text="",
                            gate_result=None,
                            tokens=0,
                            seconds=sec,
                            attempts=1,
                            success=False,
                            error=f"{err_tag}:{str(exc)[:80]}",
                        )
                    _set_verdict(
                        CandidateVerdict(
                            cid,
                            "INSUFFICIENT",
                            answer=tentative_v.answer,
                            evidence_id=tentative_v.evidence_id,
                            located_span=tentative_v.located_span,
                            quote_hash=tentative_v.quote_hash,
                            quote=tentative_v.quote,
                            reason=f"C_AUDIT_PENDING:MODEL_ERROR:{err_tag}",
                            counter_search_performed=tentative_v.counter_search_performed,
                        )
                    )
                    outcome.audit_routes[cid].update(
                        {
                            "audit_executed": False,
                            "audit_status": "pending_review",
                            "audit_decision": "待审",
                            "audit_reasons": [f"MODEL_ERROR:{err_tag}"],
                            "request_id": identity["request_id"],
                            "cache_status": "live_failed",
                        }
                    )
                    if cid not in outcome.audit_pending_ids:
                        outcome.audit_pending_ids.append(cid)
                    continue

                nli_obj, parse_repair = extract_json_object_with_repair(raw_audit)
                audit_attempts: list[dict] = [
                    {
                        "attempt": 1,
                        "seconds": sec,
                        "outcome": (
                            "parsed"
                            if isinstance(nli_obj, dict)
                            and nli_obj.get("verdict")
                            in ("answer_ok", "restating", "off_topic", "unclear")
                            else "invalid_format"
                        ),
                        "parse_repair": parse_repair,
                        "raw_response_length": len(raw_audit or ""),
                        "raw_response_text": raw_audit or "",
                    }
                ]
                if not isinstance(nli_obj, dict) or nli_obj.get("verdict") not in (
                    "answer_ok",
                    "restating",
                    "off_topic",
                    "unclear",
                ):
                    # 格式失败：**预算内允许再试一次**。
                    #
                    # 为什么只重试格式失败、不重试语义拒绝：
                    # 格式失败是模型没说清话，再问一次可能说清；
                    # 语义拒绝是模型对证据的判断，重试到"通过"等于放宽审核标准。
                    # 重试仍失败就保留待审——保留待审是正确的保护措施，
                    # 下次提交会重新调用审核，不是永久卡死。
                    retry_used = 0
                    retry_raw = raw_audit
                    last_error: str | None = None
                    while (
                        outcome.calls < max_calls
                        and retry_used < AUDIT_FORMAT_RETRY_LIMIT
                    ):
                        retry_used += 1
                        outcome.calls += 1
                        outcome.audit_calls += 1
                        outcome.audit_format_retries += 1
                        t0 = _time.perf_counter()
                        try:
                            async with sem:
                                retry_raw = await audit_fn(prompt)
                            attempt_sec = round(_time.perf_counter() - t0, 3)
                            sec += attempt_sec
                        except BaseException as exc:  # noqa: BLE001
                            attempt_sec = round(_time.perf_counter() - t0, 3)
                            sec += attempt_sec
                            outcome.failed_batches += 1
                            err_tag = (
                                "TIMEOUT"
                                if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                                else type(exc).__name__
                            )
                            last_error = f"MODEL_ERROR:{err_tag}"
                            audit_attempts.append(
                                {
                                    "attempt": 1 + retry_used,
                                    "seconds": attempt_sec,
                                    "outcome": "model_error",
                                    "error": last_error,
                                }
                            )
                            break
                        retry_obj, retry_repair = extract_json_object_with_repair(retry_raw)
                        retry_ok = (
                            isinstance(retry_obj, dict)
                            and retry_obj.get("verdict")
                            in ("answer_ok", "restating", "off_topic", "unclear")
                        )
                        audit_attempts.append(
                            {
                                "attempt": 1 + retry_used,
                                "seconds": attempt_sec,
                                "outcome": "parsed" if retry_ok else "invalid_format",
                                "parse_repair": retry_repair,
                                "raw_response_length": len(retry_raw or ""),
                                "raw_response_text": retry_raw or "",
                            }
                        )
                        if retry_ok:
                            nli_obj = retry_obj
                            raw_audit = retry_raw
                            break
                    if not isinstance(nli_obj, dict) or nli_obj.get("verdict") not in (
                        "answer_ok",
                        "restating",
                        "off_topic",
                        "unclear",
                    ):
                        # 仍失败：保留待审。缓存里记**每一次**尝试的原文与耗时，
                        # 便于事后分辨"模型没说清"与"调用出错"。
                        if audit_cache is not None:
                            audit_cache[identity["request_id"]] = build_verified_cache_record(
                                audit_item,
                                identity,
                                raw_response_text=raw_audit or "",
                                gate_result=None,
                                tokens=0,
                                seconds=sec,
                                attempts=len(audit_attempts),
                                success=False,
                                error=last_error or "INVALID_FORMAT",
                            )
                            audit_cache[identity["request_id"]]["audit_attempts"] = audit_attempts
                            audit_cache[identity["request_id"]]["attempt_raw_responses"] = [
                                a.get("raw_response_text")
                                for a in audit_attempts
                                if a.get("raw_response_text")
                            ]
                        _set_verdict(
                            CandidateVerdict(
                                cid,
                                "INSUFFICIENT",
                                answer=tentative_v.answer,
                                evidence_id=tentative_v.evidence_id,
                                located_span=tentative_v.located_span,
                                quote_hash=tentative_v.quote_hash,
                                quote=tentative_v.quote,
                                reason="C_AUDIT_PENDING:INVALID_FORMAT",
                                counter_search_performed=tentative_v.counter_search_performed,
                            )
                        )
                        outcome.audit_routes[cid].update(
                            {
                                "audit_executed": True,
                                "audit_status": "pending_review",
                                "audit_decision": "待审",
                                "audit_reasons": [last_error or "INVALID_FORMAT"],
                                "request_id": identity["request_id"],
                                "cache_status": "live_invalid_format",
                                "format_retries": retry_used,
                                "audit_attempts": audit_attempts,
                                "recovery": (
                                    "再次提交会重新调用审核；若预算内重试仍失败，"
                                    "该候选保持待审，不自动转正也不自动丢弃"
                                ),
                            }
                        )
                        if cid not in outcome.audit_pending_ids:
                            outcome.audit_pending_ids.append(cid)
                        continue
                    if retry_used:
                        outcome.audit_routes[cid]["format_retries"] = retry_used
                    outcome.audit_routes[cid]["audit_attempts"] = audit_attempts
                    outcome.audit_routes[cid]["parse_repair"] = parse_repair

                # 时序判定按**裁决实际引用的句子**算，因此必须等响应解析完再算。
                _ordering = _compute_cited_ordering(
                    chapters=chapters, audit_item=audit_item, nli_obj=nli_obj
                )
                if _ordering is not None:
                    audit_item["evidence_ordering"] = _ordering
                gate_res = gate(audit_item, nli_obj)
                claim_chk = gate_res.get("claim_check") or {}
                protocol_errs = list(claim_chk.get("errors") or [])
                if protocol_errs and gate_res["decision"] != "拒绝" and nli_obj.get("answer_supported") is True:
                    if audit_cache is not None:
                        audit_cache[identity["request_id"]] = build_verified_cache_record(
                            audit_item,
                            identity,
                            raw_response_text=raw_audit or "",
                            gate_result=None,
                            tokens=0,
                            seconds=sec,
                            attempts=1,
                            success=False,
                            error=f"PROTOCOL_ERROR:{','.join(protocol_errs)}",
                        )
                    _set_verdict(
                        CandidateVerdict(
                            cid,
                            "INSUFFICIENT",
                            answer=tentative_v.answer,
                            evidence_id=tentative_v.evidence_id,
                            located_span=tentative_v.located_span,
                            quote_hash=tentative_v.quote_hash,
                            quote=tentative_v.quote,
                            reason=f"C_AUDIT_PENDING:PROTOCOL_ERROR:{','.join(protocol_errs)}",
                            counter_search_performed=tentative_v.counter_search_performed,
                        )
                    )
                    outcome.audit_routes[cid].update(
                        {
                            "audit_executed": True,
                            "audit_status": "pending_review",
                            "audit_decision": "待审",
                            "audit_reasons": protocol_errs,
                            "request_id": identity["request_id"],
                            "cache_status": "live_protocol_error",
                        }
                    )
                    if cid not in outcome.audit_pending_ids:
                        outcome.audit_pending_ids.append(cid)
                    continue

                cache_status = "live_recorded"
                if audit_cache is not None:
                    rec = build_verified_cache_record(
                        audit_item,
                        identity,
                        raw_response_text=raw_audit,
                        gate_result=gate_res,
                        tokens=0,
                        seconds=sec,
                        attempts=1,
                        success=True,
                    )
                    audit_cache[identity["request_id"]] = rec
                    audit_cache[f"{cid}::{CLAIM_V2}"] = rec

            # Apply gate_res to control final candidate verdict and ledger status
            dec = gate_res["decision"]
            assess = gate_res.get("assessment_status", "unconfirmed")
            g_reasons = list(gate_res.get("reasons", []))
            if dec == "接受":
                _set_verdict(tentative_v)
                outcome.audit_routes[cid].update(
                    {
                        "audit_executed": True,
                        "audit_status": "accepted",
                        "audit_decision": "接受",
                        "audit_assessment": assess,
                        "audit_reasons": g_reasons,
                        "request_id": identity["request_id"],
                        "cache_status": cache_status,
                    }
                )
            elif dec == "拒绝":
                _set_verdict(
                    CandidateVerdict(
                        cid,
                        "INSUFFICIENT",
                        answer=tentative_v.answer,
                        evidence_id=tentative_v.evidence_id,
                        located_span=tentative_v.located_span,
                        quote_hash=tentative_v.quote_hash,
                        quote=tentative_v.quote,
                        reason=f"C_AUDIT_REJECTED:{';'.join(g_reasons)}",
                        counter_search_performed=tentative_v.counter_search_performed,
                    )
                )
                outcome.audit_routes[cid].update(
                    {
                        "audit_executed": True,
                        "audit_status": "rejected",
                        "audit_decision": "拒绝",
                        "audit_assessment": assess,
                        "audit_reasons": g_reasons,
                        "request_id": identity["request_id"],
                        "cache_status": cache_status,
                    }
                )
            else:
                if assess == "partial_answer":
                    r_prefix = "C_AUDIT_PARTIAL_ANSWER"
                    a_stat = "partial_answer"
                else:
                    r_prefix = "C_AUDIT_DOUBT"
                    a_stat = "doubt"
                _set_verdict(
                    CandidateVerdict(
                        cid,
                        "INSUFFICIENT",
                        answer=tentative_v.answer,
                        evidence_id=tentative_v.evidence_id,
                        located_span=tentative_v.located_span,
                        quote_hash=tentative_v.quote_hash,
                        quote=tentative_v.quote,
                        reason=f"{r_prefix}:{';'.join(g_reasons)}",
                        counter_search_performed=tentative_v.counter_search_performed,
                    )
                )
                outcome.audit_routes[cid].update(
                    {
                        "audit_executed": True,
                        "audit_status": a_stat,
                        "audit_decision": "存疑",
                        "audit_assessment": assess,
                        "audit_reasons": g_reasons,
                        "request_id": identity["request_id"],
                        "cache_status": cache_status,
                    }
                )

    async def _ask(batch: list[dict]) -> tuple[list[dict], str]:
        # 行车记录仪：在**实际调用处**落盘最终提示词、证据包与哈希。
        # 这里渲染的才是真正发出去的批次提示词——缓存里存的是单候选渲染，
        # 两者对不上，历史请求因此无法复原。
        prompt = _render_batch(batch)
        record = None
        if request_log is not None:
            record = _new_request_record(
                request_log,
                batch=batch,
                prompt=prompt,
                round_index=_current_round[0],
            )
        t0 = _time_mod.perf_counter()
        try:
            async with sem:
                raw = await call_model(prompt)
        except BaseException as exc:  # noqa: BLE001
            if record is not None:
                _append_attempt(
                    record,
                    seconds=round(_time_mod.perf_counter() - t0, 3),
                    response_text="",
                    error=f"{type(exc).__name__}: {exc}",
                    request_log=request_log,
                    prompt=prompt,
                )
            raise
        if record is not None:
            _append_attempt(
                record,
                seconds=round(_time_mod.perf_counter() - t0, 3),
                response_text=raw,
                error=None,
                request_log=request_log,
                prompt=prompt,
            )
        return batch, raw

    done: set[str] = set()
    built_count = 0
    for attempt in range(2):
        _current_round[0] = attempt
        queue = [c for c in candidates if c["candidate_id"] not in done]
        if not queue:
            break
        if attempt == 1:
            # 第一轮漏掉的候选单独补一次：模型偶尔只答前几条，补问时只给它漏的那条，
            # 命中率高；仍漏就如实记 not_checked，不再反复花钱。
            outcome.retried_batches += 1
        step = (
            max(batch_size, 1)
            if enhanced_retrieval
            else (len(queue) if attempt == 1 else max(batch_size, 1))
        )
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
            live_wave: list[list[dict]] = []
            for b in wave:
                uncached_b: list[dict] = []
                for cand in b:
                    if "segments" not in cand:
                        _build_evidence(cand)
                        newly_built += 1
                    if recheck_cache is not None:
                        from backend.common.provenance_cache import verify_cache_entry
                        _, _, rc_ident = _build_recheck_identity(cand)
                        rc_entry = recheck_cache.get(rc_ident["request_id"]) or recheck_cache.get(cand["candidate_id"])
                        if rc_entry is not None and verify_cache_entry(rc_entry, rc_ident).reusable:
                            outcome.recheck_cache_hits += 1
                            done |= _consume([cand], str(rc_entry.get("raw_response_text") or ""))
                            continue
                    uncached_b.append(cand)
                if uncached_b:
                    live_wave.append(uncached_b)
            built_count += newly_built
            if emit is not None and newly_built:
                emit({
                    "stage": "build_candidates",
                    "built": built_count,
                    "total": len(candidates),
                })
            if live_wave:
                # 先记账再发：并发下"先发后记"会超预算，而超支是**已经花掉的钱**，
                # 事后记账会把账做平却掩盖了超支事实。
                outcome.calls += len(live_wave)
                results = await asyncio.gather(*(_ask(b) for b in live_wave), return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        # 某一批失败不拖垮其他批：它的候选留在未完成集合里，
                        # 由补问轮或收尾如实记 not_checked
                        outcome.failed_batches += 1
                        continue
                    batch, raw = result
                    done |= _consume(batch, raw)
            if pending_upgrade_audits:
                await _execute_pending_upgrade_audits()
            if emit is not None:
                emit({"stage": "recheck", "calls": outcome.calls, "checked": len(done)})
        if outcome.calls >= max_calls:
            break

    outcome.not_checked.extend(
        c["candidate_id"] for c in candidates if c["candidate_id"] not in done
    )
    if trace is not None:
        v_by_cid = {v.candidate_id: v for v in outcome.verdicts}
        for c in candidates:
            cid = c["candidate_id"]
            v = v_by_cid.get(cid)
            if v is None:
                trace.record(
                    item_id=cid,
                    stage="recheck_adjudicate",
                    state_before="bundle_built",
                    state_after="not_checked",
                    reason_code="NOT_CHECKED_BUDGET_EXHAUSTED" if outcome.calls >= max_calls else "NOT_CHECKED_AFTER_RETRY",
                )
            elif v.verdict == "SUPPORTED":
                trace.record(
                    item_id=cid,
                    stage="recheck_adjudicate",
                    state_before="bundle_built",
                    state_after="supported",
                    reason_code="VERDICT_SUPPORTED",
                )
            elif v.verdict == "INVALID":
                r_code = (
                    "VERDICT_INVALID_FOREIGN_ID"
                    if v.reason == "FOREIGN_ID"
                    else "VERDICT_INVALID_QUOTE_NOT_FOUND"
                    if v.reason == "QUOTE_NOT_FOUND"
                    else "VERDICT_INVALID_OTHER"
                )
                trace.record(
                    item_id=cid,
                    stage="recheck_adjudicate",
                    state_before="bundle_built",
                    state_after="invalid",
                    reason_code=r_code,
                    meta={"reason": v.reason},
                )
            elif v.verdict in ("DIFFERENT_ANSWER", "CONTRADICTED"):
                trace.record(
                    item_id=cid,
                    stage="recheck_adjudicate",
                    state_before="bundle_built",
                    state_after=v.verdict.lower(),
                    reason_code="EVIDENCE_PRESENT_VERDICT_CONFLICT",
                )
            else:
                trace.record(
                    item_id=cid,
                    stage="recheck_adjudicate",
                    state_before="bundle_built",
                    state_after="insufficient",
                    reason_code="VERDICT_INSUFFICIENT_NO_EVIDENCE",
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
    if outcome.audit_pending_ids:
        reasons.append(f"存在待复审候选（{len(outcome.audit_pending_ids)} 条）")
    dropped = outcome.dropped_no_anchor + outcome.dropped_anchor_missing
    if dropped:
        reasons.append(f"部分条目无可用证据（{dropped} 条未进入候选）")
    if reasons:
        outcome.status_reason = "；".join(reasons)
    if outcome.audit_pending_ids and outcome.status == "COMPLETED":
        outcome.status = "PARTIAL"
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
        # V2 全证据门禁（逐字引文 + 反证检索）亲证的回收，落账即 verified——
        # 不留空到"未复审"栏再靠一次性迁移补（道诡异仙跑批暴露的漏）
        item.resolution_quality = "verified"
        item.needs_review = False
        return
    if state == PROPOSED_RESOLVED:
        item.needs_review = True
        notes = getattr(item, "notes", None)
        note_text = "复检：后文有线索但不足以判回收，请人工确认"
        for v in group:
            r = str(v.reason or "")
            if r.startswith("C_AUDIT_PENDING:"):
                item.resolution_quality = "pending_audit"
                note_text = f"复审待审（未完成）：{r}"
                break
            if r.startswith("C_AUDIT_REJECTED:"):
                item.resolution_quality = "rejected_by_audit"
                note_text = f"复审拒绝：{r}"
                break
            if r.startswith("C_AUDIT_PARTIAL_ANSWER:"):
                item.resolution_quality = "partial_answer"
                note_text = f"复审部分回答：{r}"
                break
            if r.startswith("C_AUDIT_DOUBT:"):
                item.resolution_quality = "questioned"
                note_text = f"复审存疑：{r}"
                break
            if r == "MECHANICAL_INCREMENT_REJECTED":
                item.resolution_quality = "rejected_mechanical"
                note_text = "复审机械拒止：引文等于锚点或无增量"
                break
        if isinstance(notes, list) and note_text not in notes:
            notes.append(note_text)
        return
    # OPEN：保持原状，但把"查过但没结论"记进备注，避免与"没查"混淆
    notes = getattr(item, "notes", None)
    if isinstance(notes, list) and any(v.verdict == INSUFFICIENT for v in group):
        if "复检：证据不足" not in notes:
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


def migrate_auto_recheck_quality(items: list[Any]) -> int:
    """D-12 存量迁移（v6 一次性）：V2 open 复检亲证的条目补 quality=verified。

    auto_recheck 的 resolved 本来就是过全证据门禁（逐字引文 + 反证检索）的结论，
    v6 加字段后它们默认落"未复审"栏是口径错误。返回迁移条数。
    """
    n = 0
    for item in items:
        if (
            getattr(item, "status", "") == "resolved"
            and getattr(item, "source_type", "") == "auto_recheck"
            and not (getattr(item, "resolution_quality", "") or "")
        ):
            item.resolution_quality = "verified"
            n += 1
    return n


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
