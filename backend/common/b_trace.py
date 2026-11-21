"""B 类遗漏全链路追踪与归因器（仅依赖 Python 标准库）。

追踪范围覆盖：
原文 -> 模型原始输出 -> 解析结果 -> 合并/知识库/总表过滤去重截断 -> 证据检索 -> 判定 -> 落盘展示。

分类口径（对应 docs/BC改进任务书.md）：
- B1_UNREGISTERED：登记遗漏（原文明确写出，未形成问题、线索、人物状态或事件记录）
- B2_EVIDENCE_MISSED：取证遗漏（问题已登记，后文证据未被检索或未进入证据窗口）
- B3_JUDGMENT_MISSED：判定遗漏（问题和证据均已进入窗口，模型或规则未完成关联/关闭）
- B4_FLOW_LOST：流转丢失（解析、归一化、去重合并、排序截断、缓存、重试、落盘或展示阶段丢失）
- KEPT：正常保留/流转
- UNLOCATED：无法定位（必须显式计数，不得静默归零）
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "b_trace_v1"

# 顶层归因分类枚举
CAT_B1_UNREGISTERED = "B1_UNREGISTERED"
CAT_B2_EVIDENCE_MISSED = "B2_EVIDENCE_MISSED"
CAT_B3_JUDGMENT_MISSED = "B3_JUDGMENT_MISSED"
CAT_B4_FLOW_LOST = "B4_FLOW_LOST"
CAT_KEPT = "KEPT"
CAT_UNLOCATED = "UNLOCATED"

VALID_CATEGORIES = frozenset({
    CAT_B1_UNREGISTERED,
    CAT_B2_EVIDENCE_MISSED,
    CAT_B3_JUDGMENT_MISSED,
    CAT_B4_FLOW_LOST,
    CAT_KEPT,
    CAT_UNLOCATED,
})

# 阶段名称枚举
STAGE_EXTRACT = "extract"
STAGE_PARSE = "parse"
STAGE_KB_MERGE = "kb_merge"
STAGE_CATALOG_FILTER = "catalog_filter"
STAGE_CATALOG_DEDUP = "catalog_dedup"
STAGE_CATALOG_TRUNCATE = "catalog_truncate"
STAGE_RECONCILE = "reconcile"
STAGE_RECHECK_GATE = "recheck_gate"
STAGE_EVIDENCE_RETRIEVE = "evidence_retrieve"
STAGE_RECHECK_ADJUDICATE = "recheck_adjudicate"
STAGE_PERSIST_REPORT = "persist_report"

# 原因码到 B1..B4 分类的确定性映射
REASON_TO_CATEGORY: dict[str, str] = {
    # B1 登记遗漏
    "NOT_EXTRACTED_FROM_SOURCE": CAT_B1_UNREGISTERED,
    "CHAPTER_ACQUISITION_MISSED": CAT_B1_UNREGISTERED,
    "EMPTY_CHAPTER_FORESHADOWS": CAT_B1_UNREGISTERED,
    # B2 取证遗漏
    "NOMINATED_CHAPTER_NOT_IN_BUNDLE": CAT_B2_EVIDENCE_MISSED,
    "EVIDENCE_CHAPTER_NOT_IN_BUNDLE": CAT_B2_EVIDENCE_MISSED,
    "TARGET_SPAN_NOT_IN_BUNDLE": CAT_B2_EVIDENCE_MISSED,
    "EVIDENCE_HITS_TRUNCATED": CAT_B2_EVIDENCE_MISSED,
    "NO_POST_ANCHOR_HITS": CAT_B2_EVIDENCE_MISSED,
    # B3 判定遗漏
    "EVIDENCE_PRESENT_VERDICT_INSUFFICIENT": CAT_B3_JUDGMENT_MISSED,
    "EVIDENCE_PRESENT_VERDICT_CONFLICT": CAT_B3_JUDGMENT_MISSED,
    "COUNTER_QUOTES_DOWNGRADED": CAT_B3_JUDGMENT_MISSED,
    "COUNTER_SEARCH_UNVERIFIED": CAT_B3_JUDGMENT_MISSED,
    # B4 流转丢失
    "PARSE_JSON_FAILED": CAT_B4_FLOW_LOST,
    "SHORT_CLUE_DROPPED": CAT_B4_FLOW_LOST,
    "IMPORTANCE_FILTERED": CAT_B4_FLOW_LOST,
    "CONFIDENCE_FILTERED": CAT_B4_FLOW_LOST,
    "CATEGORY_FILTERED": CAT_B4_FLOW_LOST,
    "DEDUP_MERGED_INTO_GROUP": CAT_B4_FLOW_LOST,
    "DEDUP_ANCHOR_LOST_ON_MERGE": CAT_B4_FLOW_LOST,
    "DEDUP_QUESTION_LOST_ON_MERGE": CAT_B4_FLOW_LOST,
    "HIGH_CAP_TRUNCATED": CAT_B4_FLOW_LOST,
    "MID_CAP_TRUNCATED": CAT_B4_FLOW_LOST,
    "KB_CONTEXT_TRIMMED": CAT_B4_FLOW_LOST,
    "LEAD_SKIPPED": CAT_B4_FLOW_LOST,
    "DROPPED_NO_ANCHOR": CAT_B4_FLOW_LOST,
    "DROPPED_ANCHOR_MISSING": CAT_B4_FLOW_LOST,
    "VERDICT_INVALID_FOREIGN_ID": CAT_B4_FLOW_LOST,
    "VERDICT_INVALID_QUOTE_NOT_FOUND": CAT_B4_FLOW_LOST,
    "VERDICT_INVALID_OTHER": CAT_B4_FLOW_LOST,
    "NOT_CHECKED_AFTER_RETRY": CAT_B4_FLOW_LOST,
    "NOT_CHECKED_BUDGET_EXHAUSTED": CAT_B4_FLOW_LOST,
    "REPORT_SNAPSHOT_STALE": CAT_B4_FLOW_LOST,
    # 正常保留
    "EXTRACTED": CAT_KEPT,
    "CATALOG_KEPT": CAT_KEPT,
    "DEDUP_REPRESENTATIVE_KEPT": CAT_KEPT,
    "RECONCILE_NOMINATED": CAT_KEPT,
    "EVIDENCE_BUNDLE_BUILT": CAT_KEPT,
    "VERDICT_SUPPORTED": CAT_KEPT,
    "VERDICT_INSUFFICIENT_NO_EVIDENCE": CAT_KEPT,
}


def sha256_text(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class TraceRecord:
    """单步流转追踪记录（与任务书 T1 统一字段完全对应）。"""

    run_id: str
    source_sha256: str
    source_span: list[int] | None
    reference_obs_id: str | None
    item_id: str
    stage: str
    state_before: str
    state_after: str
    reason_code: str
    category: str = ""
    artifact_path: str = ""
    elapsed_ms: float = 0.0
    call_stats: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.category:
            mapped = REASON_TO_CATEGORY.get(self.reason_code, CAT_UNLOCATED)
            object.__setattr__(self, "category", mapped)
        elif self.category not in VALID_CATEGORIES:
            object.__setattr__(self, "category", CAT_UNLOCATED)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BTraceCollector:
    """可选的零侵入追踪收集器。未传入时业务代码走原分支，结果零变化。"""

    def __init__(
        self,
        *,
        run_id: str,
        source_sha256: str = "",
        artifact_path: str = "",
    ) -> None:
        self.run_id = run_id
        self.source_sha256 = source_sha256
        self.default_artifact_path = artifact_path
        self.records: list[TraceRecord] = []

    def record(
        self,
        *,
        item_id: str,
        stage: str,
        state_before: str,
        state_after: str,
        reason_code: str,
        category: str = "",
        source_span: Sequence[int] | None = None,
        reference_obs_id: str | None = None,
        artifact_path: str = "",
        elapsed_ms: float = 0.0,
        call_stats: Mapping[str, Any] | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> TraceRecord:
        rec = TraceRecord(
            run_id=self.run_id,
            source_sha256=self.source_sha256,
            source_span=list(source_span) if source_span is not None else None,
            reference_obs_id=reference_obs_id,
            item_id=str(item_id),
            stage=stage,
            state_before=state_before,
            state_after=state_after,
            reason_code=reason_code,
            category=category or REASON_TO_CATEGORY.get(reason_code, CAT_UNLOCATED),
            artifact_path=artifact_path or self.default_artifact_path,
            elapsed_ms=float(elapsed_ms),
            call_stats=dict(call_stats) if call_stats else {},
            meta=dict(meta) if meta else {},
        )
        self.records.append(rec)
        return rec

    def summary(
        self,
        *,
        reference_omissions: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        by_stage = Counter(r.stage for r in self.records)
        by_reason = Counter(r.reason_code for r in self.records)
        by_category = Counter(r.category for r in self.records)
        losses = [r for r in self.records if r.category != CAT_KEPT]
        loss_by_cat = Counter(r.category for r in losses)
        loss_by_reason = Counter(r.reason_code for r in losses)
        unlocated = loss_by_cat.get(CAT_UNLOCATED, 0)
        located_losses = len(losses) - unlocated
        event_reason_coverage_rate = (
            round(located_losses / len(losses), 4) if losses else None
        )

        ref_total = 0
        ref_attributed = 0
        ref_unmatched = 0
        ref_by_cat: Counter[str] = Counter()
        ref_by_reason: Counter[str] = Counter()
        reference_attribution_rate: float | None = None

        if reference_omissions is not None:
            ref_total = len(reference_omissions)
            by_ref_id: dict[str, list[TraceRecord]] = {}
            for r in losses:
                if r.reference_obs_id:
                    by_ref_id.setdefault(r.reference_obs_id, []).append(r)
            for om in reference_omissions:
                oid = str(om.get("obs_id") or "")
                explicit_reason = str(om.get("reason_code") or "")
                explicit_cat = str(om.get("category") or "")
                matched_recs = by_ref_id.get(oid, [])
                if matched_recs:
                    last_rec = matched_recs[-1]
                    cat = last_rec.category
                    rc = last_rec.reason_code
                elif explicit_reason in REASON_TO_CATEGORY:
                    rc = explicit_reason
                    cat = explicit_cat or REASON_TO_CATEGORY[explicit_reason]
                else:
                    rc = "UNMATCHED_REFERENCE_OMISSION"
                    cat = CAT_UNLOCATED
                ref_by_cat[cat] += 1
                ref_by_reason[rc] += 1
                if cat != CAT_UNLOCATED:
                    ref_attributed += 1
                else:
                    ref_unmatched += 1
            if ref_total > 0:
                reference_attribution_rate = round(ref_attributed / ref_total, 4)

        return {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "source_sha256": self.source_sha256,
            "total_records": len(self.records),
            "total_loss_events": len(losses),
            "located_loss_events": located_losses,
            "unlocated_loss_events": unlocated,
            "event_reason_coverage_rate": event_reason_coverage_rate,
            "reference_omissions_total": ref_total if reference_omissions is not None else None,
            "reference_omissions_attributed": (
                ref_attributed if reference_omissions is not None else None
            ),
            "reference_omissions_unmatched": (
                ref_unmatched if reference_omissions is not None else None
            ),
            "reference_attribution_rate": reference_attribution_rate,
            "reference_attribution_status": (
                "evaluated" if (reference_omissions is not None and ref_total > 0) else "N/A"
            ),
            "reference_by_category": dict(ref_by_cat) if reference_omissions is not None else None,
            "reference_by_reason": dict(ref_by_reason) if reference_omissions is not None else None,
            "locatable_rate": (
                reference_attribution_rate
                if reference_omissions is not None
                else event_reason_coverage_rate
            ),
            "by_stage": dict(by_stage),
            "by_category": dict(by_category),
            "by_reason": dict(by_reason),
            "loss_by_category": dict(loss_by_cat),
            "loss_by_reason": dict(loss_by_reason),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "records": [r.to_dict() for r in self.records],
        }

    def dump_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _match_text_cue(
    obs: Mapping[str, Any],
    candidate_texts: Iterable[str],
    *,
    min_sub_len: int = 6,
) -> bool:
    """判断参考观察与候选记录是否指向同一原文线索或实体。"""
    joined = "\n".join(t for t in candidate_texts if t)
    if not joined:
        return False
    quote = str(obs.get("anchor_quote") or obs.get("source_quote") or "").strip()
    if quote and len(quote) >= min_sub_len:
        if quote in joined or quote[:min_sub_len] in joined or quote[-min_sub_len:] in joined:
            return True
    keywords = [str(k).strip() for k in (obs.get("keywords") or []) if str(k).strip()]
    entity = str(obs.get("entity") or obs.get("subject") or "").strip()
    if entity and entity not in joined:
        return False
    if keywords:
        hits = sum(1 for kw in keywords if kw in joined)
        required = 1 if (entity and len(keywords) == 1) else min(2, len(keywords))
        return hits >= required
    return False


def attribute_reference_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    raw_clues: Sequence[Mapping[str, Any]],
    catalog_items: Sequence[Mapping[str, Any]],
    ledger_items: Sequence[Mapping[str, Any]],
    recheck_bundles: Mapping[str, Sequence[int]] | None = None,
    recheck_verdicts: Mapping[str, Mapping[str, Any]] | None = None,
    collector: BTraceCollector | None = None,
    run_id: str = "reference_attribution",
    source_sha256: str = "",
    block_size: int = 1,
) -> dict[str, Any]:
    """将原文优先的参考观察集（登记项 + 回答项）逐条对齐到管线各阶段，定位遗漏类别。

    每条观察必须给出明确归宿：
    - ``HIT_REGISTERED``（登记项已进入最终账本）
    - ``HIT_RESOLVED``（回答项已正确完成回收/支持）
    - ``B1_UNREGISTERED``（原始章节提取即未覆盖）
    - ``B4_FLOW_LOST``（原始提取存在，但在过滤/去重/截断/锚点门禁/INVALID/未查中丢失）
    - ``B2_EVIDENCE_MISSED``（账本存在待核项，但目标回答章未进入复检证据包）
    - ``B3_JUDGMENT_MISSED``（目标回答章已进入复检证据包，但判定未给 SUPPORTED）
    - ``UNLOCATED``（异常状态，显式统计）
    """
    bundles = recheck_bundles or {}
    verdicts = recheck_verdicts or {}
    results: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()

    for obs in observations:
        obs_id = str(obs.get("obs_id") or obs.get("id") or "")
        obs_type = str(obs.get("obs_type") or "registration")  # "registration" | "resolution"
        obs_ch = int(obs.get("chapter") or obs.get("anchor_chapter") or 0)
        res_ch = int(obs.get("resolution_chapter") or obs.get("answer_chapter") or obs_ch or 0)
        span = obs.get("source_span")
        target_item_id = str(obs.get("target_item_id") or "").strip()

        # 1. 在最终账本中查找匹配项（若指定 target_item_id 则精确锁定该条目）
        if target_item_id:
            matched_ledger = [it for it in ledger_items if str(it.get("id") or "") == target_item_id]
        else:
            matched_ledger = [
                it for it in ledger_items
                if _match_text_cue(obs, [
                    str(it.get("description") or it.get("clue") or ""),
                    str(it.get("anchor_quote") or ""),
                    str(it.get("question") or ""),
                    str(it.get("answer_criteria") or ""),
                    str(it.get("resolution") or ""),
                    str(it.get("entity_key") or it.get("motif_key") or ""),
                ])
            ]
        # 2. 在截断前 catalog 中查找
        matched_catalog = [
            it for it in catalog_items
            if _match_text_cue(obs, [
                str(it.get("clue") or it.get("description") or ""),
                str(it.get("anchor_quote") or ""),
                str(it.get("question") or ""),
                str(it.get("answer_criteria") or ""),
                str(it.get("motif_key") or ""),
            ])
        ]
        # 3. 在原始章节提取记录中查找
        matched_raw = [
            it for it in raw_clues
            if _match_text_cue(obs, [
                str(it.get("clue") or it.get("description") or ""),
                str(it.get("anchor_quote") or ""),
                str(it.get("question") or ""),
                str(it.get("answer_criteria") or ""),
                str(it.get("motif_key") or ""),
            ])
        ]

        outcome_cat = CAT_UNLOCATED
        reason_code = "UNLOCATED"
        stage = STAGE_EXTRACT
        matched_id = ""

        if obs_type == "registration":
            if matched_ledger:
                outcome_cat = CAT_KEPT
                reason_code = "HIT_REGISTERED_IN_LEDGER"
                stage = STAGE_PERSIST_REPORT
                matched_id = str(matched_ledger[0].get("id") or "")
            elif matched_catalog:
                outcome_cat = CAT_B4_FLOW_LOST
                reason_code = "HIGH_CAP_TRUNCATED"
                stage = STAGE_CATALOG_TRUNCATE
                matched_id = str(matched_catalog[0].get("id") or "")
            elif matched_raw:
                raw_it = matched_raw[0]
                matched_id = str(raw_it.get("id") or f"raw_ch{raw_it.get('chapter', 0)}")
                clue_txt = str(raw_it.get("clue") or "").strip()
                if len(clue_txt) < 15:
                    outcome_cat = CAT_B4_FLOW_LOST
                    reason_code = "SHORT_CLUE_DROPPED"
                    stage = STAGE_CATALOG_FILTER
                else:
                    outcome_cat = CAT_B4_FLOW_LOST
                    reason_code = "IMPORTANCE_FILTERED"
                    stage = STAGE_CATALOG_FILTER
            else:
                outcome_cat = CAT_B1_UNREGISTERED
                reason_code = "NOT_EXTRACTED_FROM_SOURCE"
                stage = STAGE_EXTRACT
        else:
            # resolution（回答项）归因
            if not matched_raw and not matched_catalog and not matched_ledger:
                outcome_cat = CAT_B1_UNREGISTERED
                reason_code = "NOT_EXTRACTED_FROM_SOURCE"
                stage = STAGE_EXTRACT
            elif not matched_ledger:
                outcome_cat = CAT_B4_FLOW_LOST
                reason_code = "HIGH_CAP_TRUNCATED" if matched_catalog else "IMPORTANCE_FILTERED"
                stage = STAGE_CATALOG_TRUNCATE if matched_catalog else STAGE_CATALOG_FILTER
                if matched_catalog:
                    matched_id = str(matched_catalog[0].get("id") or "")
            else:
                # 已经在账本里：检查是否已 resolved，或卡在 B2 / B3 / B4
                resolved_matches = [
                    it for it in matched_ledger
                    if str(it.get("status") or "") == "resolved"
                ]
                if resolved_matches:
                    outcome_cat = CAT_KEPT
                    reason_code = "HIT_RESOLVED_IN_LEDGER"
                    stage = STAGE_PERSIST_REPORT
                    matched_id = str(resolved_matches[0].get("id") or "")
                else:
                    led_it = matched_ledger[0]
                    lid = str(led_it.get("id") or "")
                    matched_id = lid
                    v = verdicts.get(lid, {})
                    v_verdict = str(v.get("verdict") or "")
                    v_reason = str(v.get("reason") or "")
                    if not str(led_it.get("anchor_quote") or "").strip() or int(led_it.get("anchor_chapter") or 0) <= 0:
                        outcome_cat = CAT_B4_FLOW_LOST
                        reason_code = "DROPPED_NO_ANCHOR"
                        stage = STAGE_RECHECK_GATE
                    elif v_verdict == "INVALID":
                        outcome_cat = CAT_B4_FLOW_LOST
                        reason_code = (
                            "VERDICT_INVALID_FOREIGN_ID"
                            if v_reason == "FOREIGN_ID"
                            else "VERDICT_INVALID_QUOTE_NOT_FOUND"
                            if v_reason == "QUOTE_NOT_FOUND"
                            else "VERDICT_INVALID_OTHER"
                        )
                        stage = STAGE_RECHECK_ADJUDICATE
                    else:
                        bundle_chs = set(bundles.get(lid, []))
                        span_width = max(int(block_size), 1)
                        target_in_bundle = (
                            res_ch > 0
                            and any(ch <= res_ch < ch + span_width for ch in bundle_chs)
                        )
                        if not target_in_bundle:
                            outcome_cat = CAT_B2_EVIDENCE_MISSED
                            reason_code = (
                                "NOMINATED_CHAPTER_NOT_IN_BUNDLE"
                                if led_it.get("resolved_chapter")
                                else "TARGET_SPAN_NOT_IN_BUNDLE"
                            )
                            stage = STAGE_EVIDENCE_RETRIEVE
                        elif v_verdict in ("INSUFFICIENT", "DIFFERENT_ANSWER", "CONTRADICTED"):
                            outcome_cat = CAT_B3_JUDGMENT_MISSED
                            reason_code = (
                                "EVIDENCE_PRESENT_VERDICT_INSUFFICIENT"
                                if v_verdict == "INSUFFICIENT"
                                else "EVIDENCE_PRESENT_VERDICT_CONFLICT"
                            )
                            stage = STAGE_RECHECK_ADJUDICATE
                        else:
                            outcome_cat = CAT_B4_FLOW_LOST
                            reason_code = "NOT_CHECKED_AFTER_RETRY"
                            stage = STAGE_RECHECK_ADJUDICATE

        counts[outcome_cat] += 1
        reason_counts[reason_code] += 1
        row = {
            "obs_id": obs_id,
            "obs_type": obs_type,
            "chapter": obs_ch,
            "resolution_chapter": res_ch,
            "matched_item_id": matched_id,
            "stage": stage,
            "category": outcome_cat,
            "reason_code": reason_code,
        }
        results.append(row)
        if collector is not None:
            collector.record(
                item_id=matched_id or obs_id,
                stage=stage,
                state_before="reference_observation",
                state_after="hit" if outcome_cat == CAT_KEPT else "omitted",
                reason_code=reason_code,
                category=outcome_cat,
                source_span=span if isinstance(span, list) else None,
                reference_obs_id=obs_id,
            )

    total_obs = len(observations)
    hits = counts.get(CAT_KEPT, 0)
    omissions = total_obs - hits
    unlocated = counts.get(CAT_UNLOCATED, 0)
    locatable_omissions = omissions - unlocated
    locatable_rate = (locatable_omissions / omissions) if omissions > 0 else 1.0

    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "source_sha256": source_sha256,
        "total_observations": total_obs,
        "hits": hits,
        "omissions": omissions,
        "locatable_omissions": locatable_omissions,
        "unlocated_omissions": unlocated,
        "locatable_rate": round(locatable_rate, 4),
        "by_category": dict(counts),
        "by_reason": dict(reason_counts),
        "details": results,
    }
