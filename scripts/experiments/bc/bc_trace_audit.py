"""T1：B 类遗漏全链路追踪与归因脚本（纯本地离线回放，0 外部 API 调用）。

用法：
    python -X utf8 scripts/experiments/bc/bc_trace_audit.py --output docs/research/bc-improvement-2026-11-05/t1-omission-attribution.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.common.b_trace import (
    BTraceCollector,
    attribute_reference_observations,
    sha256_text,
)
from backend.config.constants import (
    FORESHADOW_CATEGORY_DEFS,
    FORESHADOW_CATEGORY_FALLBACK,
    normalize_foreshadow_type,
)
from backend.core.anchor import entity_key_for
from backend.core.evidence import EvidenceIndex
from backend.utils.text_utils import deduplicate_foreshadows


IMP_ORDER = {"高": 3, "中": 2, "低": 1}


def _imp_at_least(val: str, threshold: str) -> bool:
    return IMP_ORDER.get(val, 0) >= IMP_ORDER.get(threshold, 0)


def trace_workspace(
    *,
    book_title: str,
    workspace_dir: Path,
    run_id: str,
    max_high: int = 500,
    max_mid: int = 200,
    min_importance: str = "中",
    min_confidence: str = "中",
) -> dict[str, Any]:
    """对已完成章节采集与账本落盘的工作区做全链路 B 类遗漏追踪回放。"""
    t0 = time.perf_counter()
    out_dir = workspace_dir / "output"
    blocks_dir = workspace_dir / "blocks"
    ledger_path = out_dir / "foreshadow_ledger.json"
    verdicts_path = out_dir / "recheck_verdicts.json"
    type_map_path = out_dir / "foreshadow_type_map.json"
    kb_path = workspace_dir / "knowledge.json"

    ledger_bytes = ledger_path.read_bytes() if ledger_path.exists() else b""
    source_sha = sha256_text(ledger_bytes) if ledger_bytes else ""
    trace = BTraceCollector(
        run_id=run_id,
        source_sha256=source_sha,
        artifact_path=str(out_dir.relative_to(ROOT)).replace("\\", "/"),
    )

    # 1. 读取章节提取产物與跳过块
    result_files = sorted(out_dir.glob("chapter_*_result.json"))
    skipped_files = sorted(out_dir.glob("chapter_*_skipped.json"))
    for sf in skipped_files:
        ch_num = int(sf.stem.split("_")[1]) if sf.stem.split("_")[1].isdigit() else 0
        trace.record(
            item_id=f"block_ch{ch_num}",
            stage="parse",
            state_before="raw_llm_output",
            state_after="skipped_block",
            reason_code="PARSE_JSON_FAILED",
            meta={"file": sf.name, "chapter": ch_num},
        )

    valid_names = {name for name, _, _ in FORESHADOW_CATEGORY_DEFS}
    type_map: dict[str, str] = {}
    if type_map_path.exists():
        try:
            raw_map = json.loads(type_map_path.read_text(encoding="utf-8"))
            if isinstance(raw_map, dict):
                type_map = {str(k): str(v) for k, v in raw_map.items()}
        except Exception:
            type_map = {}

    raw_clues_dicts: list[dict[str, Any]] = []
    all_clues: list[tuple[Any, ...]] = []
    empty_foreshadow_blocks = 0

    for rf in result_files:
        data = json.loads(rf.read_text(encoding="utf-8"))
        ch = int(data.get("chapter_number") or data.get("chapter_num") or rf.stem.split("_")[1])
        f_list = data.get("foreshadowing", []) or []
        if not f_list:
            empty_foreshadow_blocks += 1
            trace.record(
                item_id=f"block_ch{ch}",
                stage="extract",
                state_before="source_block",
                state_after="empty_foreshadows",
                reason_code="EMPTY_CHAPTER_FORESHADOWS",
                meta={"chapter": ch, "file": rf.name},
            )
            continue
        for fs_idx, fs in enumerate(f_list):
            if not isinstance(fs, dict):
                continue
            clue = str(fs.get("clue") or "")
            ftype = str(fs.get("type") or "")
            conf = str(fs.get("confidence") or "中") or "中"
            imp = str(fs.get("importance") or "中") or "中"
            aq = str(fs.get("anchor_quote") or "")
            ach = int(fs.get("anchor_chapter") or 0)
            mk = str(fs.get("motif_key") or "")
            q = str(fs.get("question") or "")
            ac = str(fs.get("answer_criteria") or "")
            raw_id = f"raw_ch{ch}_{fs_idx}"
            raw_clues_dicts.append({
                "id": raw_id,
                "chapter": ch,
                "clue": clue,
                "type": ftype,
                "confidence": conf,
                "importance": imp,
                "anchor_quote": aq,
                "anchor_chapter": ach,
                "motif_key": mk,
                "question": q,
                "answer_criteria": ac,
            })
            if not clue or len(clue.strip()) < 15:
                trace.record(
                    item_id=raw_id,
                    stage="catalog_filter",
                    state_before="raw_foreshadow",
                    state_after="dropped",
                    reason_code="SHORT_CLUE_DROPPED",
                    meta={"chapter": ch, "clue_len": len(clue.strip())},
                )
                continue
            all_clues.append((raw_id, ch, clue.strip(), ftype, conf, imp, aq, ach, mk, q, ac))

    # 2. 过滤阶段
    categorized: list[tuple[Any, ...]] = []
    for raw_id, ch, clue, ftype, conf, imp, aq, ach, mk, q, ac in all_clues:
        if not _imp_at_least(imp, min_importance):
            trace.record(
                item_id=raw_id,
                stage="catalog_filter",
                state_before="raw_foreshadow",
                state_after="dropped",
                reason_code="IMPORTANCE_FILTERED",
                meta={"chapter": ch, "importance": imp},
            )
            continue
        if not _imp_at_least(conf, min_confidence):
            trace.record(
                item_id=raw_id,
                stage="catalog_filter",
                state_before="raw_foreshadow",
                state_after="dropped",
                reason_code="CONFIDENCE_FILTERED",
                meta={"chapter": ch, "confidence": conf},
            )
            continue
        if ftype in valid_names:
            category = ftype
        else:
            category = (
                type_map.get(ftype)
                or normalize_foreshadow_type(ftype, valid_names)
                or FORESHADOW_CATEGORY_FALLBACK
            )
        categorized.append((ch, clue, ftype, conf, imp, aq, ach, mk, q, ac))

    # 3. 去重与截断阶段
    catalog = deduplicate_foreshadows(categorized, trace=trace)
    for c in catalog:
        ev_cnt = len(c.get("evidence_chapters", []))
        span = int(c.get("last_seen", 0)) - int(c.get("first_seen", 0))
        score = (
            IMP_ORDER.get(c.get("importance", "中"), 0) * 100
            + IMP_ORDER.get(c.get("confidence", "中"), 0) * 10
            + min(ev_cnt, 50)
            + min(span, 49)
        )
        c["composite_score"] = score
        c["entity_key"] = entity_key_for(c.get("motif_key", ""), str(c.get("id", "")))
    catalog.sort(key=lambda c: c["composite_score"], reverse=True)

    high_items = [c for c in catalog if c.get("importance") == "高"]
    mid_items = [c for c in catalog if c.get("importance") == "中"]
    other_items = [c for c in catalog if c.get("importance") not in ("高", "中")]

    truncated: list[dict[str, Any]] = []
    if max_high == -1 or len(high_items) <= max_high:
        truncated.extend(high_items)
    else:
        truncated.extend(high_items[:max_high])
        for d in high_items[max_high:]:
            trace.record(
                item_id=str(d["id"]),
                stage="catalog_truncate",
                state_before="dedup_catalog",
                state_after="dropped_by_cap",
                reason_code="HIGH_CAP_TRUNCATED",
                meta={"first_seen": d.get("first_seen", 0), "score": d.get("composite_score", 0)},
            )
    if max_mid == -1 or len(mid_items) <= max_mid:
        truncated.extend(mid_items)
    else:
        truncated.extend(mid_items[:max_mid])
        for d in mid_items[max_mid:]:
            trace.record(
                item_id=str(d["id"]),
                stage="catalog_truncate",
                state_before="dedup_catalog",
                state_after="dropped_by_cap",
                reason_code="MID_CAP_TRUNCATED",
                meta={"first_seen": d.get("first_seen", 0), "score": d.get("composite_score", 0)},
            )
    truncated.extend(other_items)
    for k in truncated:
        trace.record(
            item_id=str(k["id"]),
            stage="catalog_truncate",
            state_before="dedup_catalog",
            state_after="catalog_kept",
            reason_code="CATALOG_KEPT",
            meta={"first_seen": k.get("first_seen", 0), "importance": k.get("importance", "")},
        )

    # 4. 账本、证据包与复检裁决追踪
    ledger_items: list[dict[str, Any]] = []
    if ledger_path.exists():
        ledger_data = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger_items = ledger_data.get("items", [])

    verdict_map: dict[str, dict[str, Any]] = {}
    if verdicts_path.exists():
        v_data = json.loads(verdicts_path.read_text(encoding="utf-8"))
        for v in v_data.get("verdicts", []):
            cid = str(v.get("candidate_id") or "")
            if cid:
                verdict_map[cid] = v

    chapters: dict[int, str] = {}
    if blocks_dir.exists():
        for p in sorted(blocks_dir.glob("*.txt")):
            if p.stem.isdigit():
                chapters[int(p.stem)] = p.read_text(encoding="utf-8")
    idx = EvidenceIndex(chapters) if chapters else None
    if idx is not None:
        idx.ensure_indexed()

    bundle_map: dict[str, list[int]] = {}
    for it in ledger_items:
        lid = str(it.get("id") or "")
        status = str(it.get("status") or "")
        aq = str(it.get("anchor_quote") or "").strip()
        ach = int(it.get("anchor_chapter") or 0)
        nom_ch = int(it.get("resolved_chapter") or 0)
        if status == "resolved":
            trace.record(
                item_id=lid,
                stage="persist_report",
                state_before="recheck",
                state_after="resolved",
                reason_code="VERDICT_SUPPORTED",
                meta={"resolved_chapter": nom_ch, "quality": it.get("resolution_quality", "")},
            )
            continue
        if not aq or ach <= 0:
            trace.record(
                item_id=lid,
                stage="recheck_gate",
                state_before=status,
                state_after="dropped_no_anchor",
                reason_code="DROPPED_NO_ANCHOR",
            )
            continue
        if idx is not None:
            b = idx.bundle_for(
                anchor_chapter=ach,
                anchor_quote=aq,
                hint_texts=[str(it.get("question") or ""), str(it.get("description") or "")],
            )
            seg_chs = [int(ch) for ch, _, _ in b.spans]
            bundle_map[lid] = seg_chs
            if nom_ch > 0 and not any(nom_ch <= sc < nom_ch + 4 or sc <= nom_ch < sc + 4 for sc in seg_chs):
                trace.record(
                    item_id=lid,
                    stage="evidence_retrieve",
                    state_before="nominated_resolution",
                    state_after="nominated_chapter_missed",
                    reason_code="NOMINATED_CHAPTER_NOT_IN_BUNDLE",
                    meta={"nominated_chapter": nom_ch, "bundle_chapters": seg_chs},
                )
            if b.truncated:
                trace.record(
                    item_id=lid,
                    stage="evidence_retrieve",
                    state_before="hit_chapters",
                    state_after="bundle_truncated",
                    reason_code="EVIDENCE_HITS_TRUNCATED",
                    meta={"omitted": b.omitted},
                )
            if len(seg_chs) <= 1:
                trace.record(
                    item_id=lid,
                    stage="evidence_retrieve",
                    state_before="anchor_only",
                    state_after="no_post_anchor_hits",
                    reason_code="NO_POST_ANCHOR_HITS",
                    meta={"anchor_chapter": ach},
                )

        v = verdict_map.get(lid)
        if v is not None:
            vv = str(v.get("verdict") or "")
            vr = str(v.get("reason") or "")
            if vv == "INVALID":
                r_code = (
                    "VERDICT_INVALID_FOREIGN_ID"
                    if vr == "FOREIGN_ID"
                    else "VERDICT_INVALID_QUOTE_NOT_FOUND"
                    if vr == "QUOTE_NOT_FOUND"
                    else "VERDICT_INVALID_OTHER"
                )
                trace.record(
                    item_id=lid,
                    stage="recheck_adjudicate",
                    state_before="bundle_built",
                    state_after="invalid",
                    reason_code=r_code,
                    meta={"reason": vr},
                )
            elif vv in ("DIFFERENT_ANSWER", "CONTRADICTED"):
                trace.record(
                    item_id=lid,
                    stage="recheck_adjudicate",
                    state_before="bundle_built",
                    state_after=vv.lower(),
                    reason_code="EVIDENCE_PRESENT_VERDICT_CONFLICT",
                )
            elif vv == "INSUFFICIENT":
                # 若提名章命中但仍判 INSUFFICIENT，记为 B3 候选；否则记普通 INSUFFICIENT
                seg_chs = bundle_map.get(lid, [])
                if nom_ch > 0 and any(nom_ch <= sc < nom_ch + 4 or sc <= nom_ch < sc + 4 for sc in seg_chs):
                    trace.record(
                        item_id=lid,
                        stage="recheck_adjudicate",
                        state_before="nominated_evidence_present",
                        state_after="insufficient",
                        reason_code="EVIDENCE_PRESENT_VERDICT_INSUFFICIENT",
                        meta={"nominated_chapter": nom_ch, "bundle_chapters": seg_chs},
                    )
                else:
                    trace.record(
                        item_id=lid,
                        stage="recheck_adjudicate",
                        state_before="bundle_built",
                        state_after="insufficient",
                        reason_code="VERDICT_INSUFFICIENT_NO_EVIDENCE",
                    )

    # 5. 检查 knowledge.json 容量上限裁剪迹象
    kb_stats: dict[str, int] = {}
    if kb_path.exists():
        kb_data = json.loads(kb_path.read_text(encoding="utf-8"))
        vf = len(kb_data.get("verified_facts", []) or [])
        cp = len(kb_data.get("character_profiles", {}) or {})
        fs_kb = len(kb_data.get("foreshadowing", []) or [])
        kb_stats = {"verified_facts": vf, "character_profiles": cp, "foreshadowing": fs_kb}
        if vf >= 100 or cp >= 50 or fs_kb >= 50:
            trace.record(
                item_id="knowledge_json_cap",
                stage="kb_merge",
                state_before="incremental_kb",
                state_after="trimmed_at_cap",
                reason_code="KB_CONTEXT_TRIMMED",
                meta=kb_stats,
            )

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return {
        "book_title": book_title,
        "workspace_dir": str(workspace_dir.relative_to(ROOT)).replace("\\", "/"),
        "elapsed_ms": elapsed_ms,
        "funnel": {
            "result_files": len(result_files),
            "skipped_files": len(skipped_files),
            "empty_foreshadow_blocks": empty_foreshadow_blocks,
            "raw_clues_total": len(raw_clues_dicts),
            "after_length_check": len(all_clues),
            "after_filter": len(categorized),
            "after_dedup": len(catalog),
            "high_before_cap": len(high_items),
            "mid_before_cap": len(mid_items),
            "high_dropped_by_cap": max(0, len(high_items) - max_high) if max_high != -1 else 0,
            "mid_dropped_by_cap": max(0, len(mid_items) - max_mid) if max_mid != -1 else 0,
            "catalog_after_cap": len(truncated),
            "ledger_total": len(ledger_items),
            "ledger_resolved": sum(1 for x in ledger_items if x.get("status") == "resolved"),
            "ledger_active": sum(1 for x in ledger_items if x.get("status") in ("active", "open")),
            "ledger_dormant": sum(1 for x in ledger_items if x.get("status") == "dormant"),
            "active_with_nominated_chapter": sum(
                1 for x in ledger_items if x.get("status") in ("active", "open") and x.get("resolved_chapter")
            ),
            "kb_stats": kb_stats,
        },
        "trace_summary": trace.summary(),
        "_raw_clues": raw_clues_dicts,
        "_catalog": catalog,
        "_ledger": ledger_items,
        "_bundles": bundle_map,
        "_verdicts": verdict_map,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="T1 B类遗漏全链路追踪与归因")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/research/bc-improvement-2026-11-05/t1-omission-attribution.json",
    )
    args = parser.parse_args()

    daogui = trace_workspace(
        book_title="道诡异仙",
        workspace_dir=ROOT / "workspace/《道诡异仙》",
        run_id="t1_daogui_replay_20261105",
    )
    dafeng = trace_workspace(
        book_title="大奉打更人",
        workspace_dir=ROOT / "workspace/分析结果/《大奉打更人》-20261103-0744",
        run_id="t1_dafeng_replay_20261105",
    )

    # 使用已审计的道诡异仙原文锚定样本集检验端到端参考观察归因可定位率
    daogui_ref_obs = [
        {
            "obs_id": "dg_ref_001",
            "obs_type": "resolution",
            "target_item_id": "fs_002",
            "chapter": 1,
            "resolution_chapter": 5,
            "anchor_quote": "玉佩跟丹药",
            "keywords": ["玉佩", "现实世界"],
            "note": "fs_002 第5章玉佩带出至现实，因提名章未进证据包且串号导致 INVALID/FOREIGN_ID",
        },
        {
            "obs_id": "dg_ref_002",
            "obs_type": "resolution",
            "target_item_id": "fs_047",
            "chapter": 29,
            "resolution_chapter": 41,
            "anchor_quote": "佛爷求子",
            "keywords": ["西京", "正德寺", "和尚"],
            "note": "fs_047 第41章线索导向正德寺，复检时因 QUOTE_NOT_FOUND 落入 INVALID 未重试成功",
        },
        {
            "obs_id": "dg_ref_003",
            "obs_type": "resolution",
            "target_item_id": "fs_080",
            "chapter": 49,
            "resolution_chapter": 237,
            "anchor_quote": "心素",
            "keywords": ["心素", "体质", "正德寺"],
            "note": "fs_080 提名第237章未进入证据包，且复检返回 FOREIGN_ID",
        },
        {
            "obs_id": "dg_ref_004",
            "obs_type": "resolution",
            "target_item_id": "fs_120",
            "chapter": 77,
            "resolution_chapter": 93,
            "anchor_quote": "竹简",
            "keywords": ["竹简", "巴虺", "献祭"],
            "note": "fs_120/fs_121 提名第93章竹林献祭未进入证据包，复检落入 INVALID",
        },
        {
            "obs_id": "dg_ref_005",
            "obs_type": "resolution",
            "target_item_id": "fs_327",
            "chapter": 213,
            "resolution_chapter": 819,
            "anchor_quote": "可注定是没有答案",
            "keywords": ["幻觉", "亡魂"],
            "note": "fs_327 第819章死者幻觉真相未进入 top-5 证据窗口（B2 取证遗漏）",
        },
        {
            "obs_id": "dg_ref_006",
            "obs_type": "resolution",
            "target_item_id": "fs_876",
            "chapter": 581,
            "resolution_chapter": 589,
            "anchor_quote": "掌管着迷惘天道的司命",
            "keywords": ["季灾", "迷惘天道", "司命"],
            "note": "fs_876 第589章已进入证据窗口但模型判 INSUFFICIENT（B3 判定遗漏）",
        },
        {
            "obs_id": "dg_ref_007",
            "obs_type": "registration",
            "chapter": 801,
            "anchor_quote": "季灾的声音在李火旺耳边响起",
            "keywords": ["季灾", "三清"],
            "note": "第754章之后的高重要度伏笔在总表 [:500] 截断中100%被丢弃（B4 流转丢失）",
        },
        {
            "obs_id": "dg_ref_008",
            "obs_type": "registration",
            "chapter": 901,
            "anchor_quote": "红中看着眼前的李火旺",
            "keywords": ["红中", "坐忘道"],
            "note": "第901章块的高重要度伏笔在总表 [:500] 截断中被丢弃（B4 流转丢失）",
        },
        {
            "obs_id": "dg_ref_009",
            "obs_type": "resolution",
            "target_item_id": "fs_014",
            "chapter": 14,
            "resolution_chapter": 14,
            "anchor_quote": "丹阳子确实不认识字",
            "keywords": ["丹阳子", "认识字"],
            "note": "fs_014 正常正向控制（已关闭）",
        },
        {
            "obs_id": "dg_ref_010",
            "obs_type": "resolution",
            "target_item_id": "fs_025",
            "chapter": 22,
            "resolution_chapter": 22,
            "anchor_quote": "那根本就不是什么成仙天书",
            "keywords": ["成仙天书", "上古经文"],
            "note": "fs_025 正常正向控制（已关闭）",
        },
    ]

    ref_attr = attribute_reference_observations(
        daogui_ref_obs,
        raw_clues=daogui["_raw_clues"],
        catalog_items=daogui["_catalog"],
        ledger_items=daogui["_ledger"],
        recheck_bundles=daogui["_bundles"],
        recheck_verdicts=daogui["_verdicts"],
        run_id="t1_daogui_ref_attribution",
        source_sha256=daogui["trace_summary"]["source_sha256"],
        block_size=4,
    )

    for d in (daogui, dafeng):
        for k in ("_raw_clues", "_catalog", "_ledger", "_bundles", "_verdicts"):
            d.pop(k, None)

    payload = {
        "schema": "t1_omission_attribution_report_v1",
        "generated_at": "2026-11-05T14:40:00+08:00",
        "workspaces": {
            "daogui": daogui,
            "dafeng": dafeng,
        },
        "reference_sample_attribution": ref_attr,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "daogui_funnel": daogui["funnel"],
        "daogui_loss_by_reason": daogui["trace_summary"]["loss_by_reason"],
        "daogui_locatable_rate": daogui["trace_summary"]["locatable_rate"],
        "dafeng_funnel": dafeng["funnel"],
        "dafeng_loss_by_reason": dafeng["trace_summary"]["loss_by_reason"],
        "dafeng_locatable_rate": dafeng["trace_summary"]["locatable_rate"],
        "ref_locatable_rate": ref_attr["locatable_rate"],
        "ref_by_category": ref_attr["by_category"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
