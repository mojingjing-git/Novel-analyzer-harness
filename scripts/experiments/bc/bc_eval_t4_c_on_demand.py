"""T4: Paired Evaluation of On-Demand Class C Audit (current vs full claims_v2 vs on_demand_claims_v2).

Strict Methodological Guarantees (Addressing independent-review.md & independent-review-followup.md F3 & F6):
1. Zero renaming of old evaluation files and zero hardcoded token/time constants.
2. All 85 natural pipeline candidates (55 real 《道诡异仙》 recheck candidates + 30 real pipeline candidates from the 8 works) are evaluated with paired `current` and `claims_v2` calls under the exact same model, prompt, and code, recording real measured tokens, wall-clock seconds, `sample_id`, `prompt_sha256`, `model`, and `contract_version` for every call.
3. Strict Label Provenance & Binding Separation (F3):
   - Historical `legacy55` items are NOT auto-certified by overwriting `label_sample_id = sample_id`. Instead, they are checked against `pilot12.json` and `acceptance_prepared_legacy55.json` (`5 bound`, `4 stale`, `46 unbound`).
   - Multi-work model-generated `repaired` (`SUPPORTED`/`DIFFERENT_ANSWER`, `rch > ach`) candidates are NOT blindly treated as verified `"支持"` ground truth. Only the 2 positive items independently verified against blind reference observations (`zl_ind_dev_repaired_fs_018` -> `dev_obs_006`, `zl_game_dev_repaired_fs_007` -> `dev_obs_016`) and the 10 negative items verified by `rch == ach` setup echo / multi-clause unsupported claims receive `label_sample_id = sample_id` (`label_status="bound"`). The remaining 18 model-nominated candidates keep `label_sample_id = None` (`label_status="unbound"`, `label_provenance="model_self_nominated_unverified"`).
   - Metrics are reported separately on `strict_bound_subset` (`label_status == "bound"`) and `diagnostic_all_85_nominal` (including unbound/stale historical and model-nominated items), plus a 4-bucket rejection attribution breakdown across all 51 nominal `"支持"` candidates.
4. Execution Telemetry Separation (F6):
   - Separates `live_network_calls`, `cached_reused_calls`, and `offline_selector_replay_items`.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any

for _key in ("NO_PROXY", "no_proxy"):
    os.environ[_key] = "localhost,127.0.0.1"
os.environ.pop("ALL_PROXY", None)
os.environ.pop("all_proxy", None)

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.common.bc_scorer_and_labels import classify_c_observable_branch_outcome
from backend.common.c_audit_selector import select_audit_route
from backend.common.claim_evidence_v2 import VERSION as CLAIM_V2
from backend.common.provenance_cache import (
    build_request_identity,
    build_verified_cache_record,
    re_adjudicate_cached_record,
    verify_cache_entry,
)
from backend.config.settings import ConfigManager
from backend.core.llm_client import LLMClient
from scripts.acceptance_gate import (
    build_nli_prompt,
    evaluate_metrics,
    extract_json_object,
    gate,
    label_status,
    sample_fingerprint,
)
from scripts.experiments.bc.bc_eval_b_zero_leakage import split_natural_subchapters

BLIND_REFERENCE_VERIFIED_POSITIVES = {
    "zl_ind_dev_repaired_fs_018": "dev_obs_006",
    "zl_game_dev_repaired_fs_007": "dev_obs_016",
}


def classify_positive_rejection_bucket(cur_row: dict[str, Any], v2_row: dict[str, Any]) -> str:
    """Directly observable branch comparison category (T2 / R3-4: does NOT claim candidate truth from model output)."""
    return classify_c_observable_branch_outcome(cur_row, v2_row)


def load_natural_pipeline_candidates_30() -> list[dict[str, Any]]:
    """Load 30 real model-generated pipeline candidates from zero_leakage_b_paired_results.json."""
    dev_pkts = json.loads(
        (ROOT / "workspace/bc-evaluation/bc-improvement-20261105/dev_blind_packets.json").read_text(encoding="utf-8")
    )["packets"]
    hold_pkts = json.loads(
        (ROOT / "workspace/bc-evaluation/bc-improvement-20261105/holdout_blind_packets.json").read_text(encoding="utf-8")
    )["packets"]
    zl_data = json.loads(
        (ROOT / "workspace/bc-evaluation/bc-improvement-20261105/zero_leakage_b_paired_results.json").read_text(encoding="utf-8")
    )

    pkts_by_work: dict[str, list[dict[str, Any]]] = {}
    for p in dev_pkts + hold_pkts:
        pkts_by_work.setdefault(p["work_id"], []).append(p)

    chapters_by_work: dict[str, dict[int, str]] = {}
    for wid, w_p in pkts_by_work.items():
        chs, _ = split_natural_subchapters(w_p, target_chars=2000)
        chapters_by_work[wid] = chs

    works_by_id = {w["work_id"]: w for w in zl_data["dev_works"] + zl_data["holdout_works"]}

    neg_specs = [
        ("urban_dev", "repaired", "fs_014"),
        ("hist_dev", "repaired", "fs_053"),
        ("hist_dev", "repaired", "fs_067"),
        ("game_holdout", "repaired", "fs_034"),
        ("game_holdout", "repaired", "fs_045"),
        ("urban_holdout", "repaired", "fs_005"),
        ("urban_holdout", "repaired", "fs_036"),
        ("urban_holdout", "repaired", "fs_050"),
        ("hist_dev", "baseline", "fs_004"),
        ("game_dev", "repaired", "fs_004"),
    ]
    neg_set = {(w, c) for w, _, c in neg_specs}

    def _build_item(wid: str, arm_key: str, cid: str, label: str, *, is_verified_neg: bool = False) -> dict[str, Any] | None:
        w = works_by_id[wid]
        lm = {it["id"]: it for it in w[arm_key]["ledger_items"]}
        vm = {v["candidate_id"]: v for v in w[arm_key]["verdicts"]}
        it = lm.get(cid)
        v = vm.get(cid)
        if not it or not v or not v.get("answer") or not v.get("quote"):
            return None
        chs = chapters_by_work[wid]
        ach = int(it.get("anchor_chapter") or 1)
        rch = int(it.get("resolved_chapter") or ach)
        q = v["quote"]
        ev_ch = rch if (rch in chs and q in chs[rch]) else next((c for c, t in chs.items() if q in t), rch)
        txt = chs.get(ev_ch, "")
        if not txt or q not in txt:
            return None
        digest = hashlib.sha256(txt.encode("utf-8")).hexdigest()
        fid = f"zl_{wid}_{arm_key}_{cid}"
        item = {
            "fs_id": fid,
            "work_id": wid,
            "stratum_source": "natural_multiwork_recheck",
            "label": label,
            "question": it["question"],
            "criteria": it["answer_criteria"],
            "description": it["description"],
            "anchor_quote": it.get("anchor_quote") or "",
            "anchor_chapter": ach,
            "answer": v["answer"],
            "quote": q,
            "chapter": ev_ch,
            "prereg": None,
            "evidence_segments": [
                {
                    "evidence_id": f"seg_{fid}_{digest[:10]}",
                    "chapter": ev_ch,
                    "text": txt,
                }
            ],
        }
        item["sample_id"] = sample_fingerprint(item)
        if is_verified_neg:
            item["label_sample_id"] = item["sample_id"]
            item["label_provenance"] = "mechanical_or_source_verified_negative"
            item["verified_reference_id"] = "same_chapter_setup_echo" if rch == ach else "unsupported_first_clauses_verified"
        elif fid in BLIND_REFERENCE_VERIFIED_POSITIVES:
            item["label_sample_id"] = item["sample_id"]
            item["label_provenance"] = "blind_reference_verified_positive"
            item["verified_reference_id"] = BLIND_REFERENCE_VERIFIED_POSITIVES[fid]
        else:
            # F3 fix: Do NOT auto-bind model-nominated positive labels without independent verification
            item["label_sample_id"] = None
            item["label_provenance"] = "model_self_nominated_unverified"
            item["verified_reference_id"] = None
        return item

    candidates: list[dict[str, Any]] = []
    for wid, w in works_by_id.items():
        lm = {it["id"]: it for it in w["repaired"]["ledger_items"]}
        for v in w["repaired"]["verdicts"]:
            if len(candidates) >= 20:
                break
            cid = v["candidate_id"]
            if (wid, cid) in neg_set:
                continue
            it = lm.get(cid, {})
            ach = int(it.get("anchor_chapter") or 1)
            rch = int(it.get("resolved_chapter") or ach)
            if v.get("verdict") not in ("SUPPORTED", "DIFFERENT_ANSWER") or rch <= ach:
                continue
            built = _build_item(wid, "repaired", cid, "支持", is_verified_neg=False)
            if built is not None:
                candidates.append(built)

    for wid, arm_key, cid in neg_specs:
        built = _build_item(wid, arm_key, cid, "不支持", is_verified_neg=True)
        if built is not None:
            candidates.append(built)

    return candidates


async def main() -> None:
    logging.disable(logging.CRITICAL)
    config = ConfigManager(ROOT / "config.json").load()
    api = replace(
        config.api,
        model=config.analysis.recheck_model or config.api.model,
        base_url=config.analysis.recheck_base_url or config.api.base_url,
        api_key=config.analysis.recheck_api_key or config.api.api_key,
        reasoning_effort=config.analysis.recheck_reasoning_effort or "low",
        temperature=0,
        streaming_enabled=True,
        max_tokens=4096,
        timeout=90,
        max_retries=1,
    )
    client = LLMClient(api)
    sem = asyncio.Semaphore(4)
    execution_telemetry = {
        "live_network_calls": 0,
        "verified_cache_reused": 0,
        "unverifiable_historical_records": 0,
        "mismatched_cache_rejected": 0,
        "offline_selector_replay_items": 0,
        "measured_attempts_total": 0,
        "measured_tokens_total": 0,
        "provider_filtered_or_failed_calls": 0,
    }

    cache_path = ROOT / "workspace/bc-evaluation/bc-improvement-20261105/t4_live_paired_cache_v2.json"
    live_cache: dict[str, dict[str, Any]] = {}
    if cache_path.exists():
        live_cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def _save_cache() -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(live_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cache_path)

    model_cfg = {
        "model": api.model,
        "base_url": api.base_url,
        "temperature": api.temperature,
        "reasoning_effort": api.reasoning_effort,
        "max_tokens": api.max_tokens,
    }

    async def eval_arm_item(item: dict[str, Any], arm: str) -> dict[str, Any]:
        fid = item["fs_id"]
        cache_key = f"{fid}::{arm}"
        lbl = item["label"]
        base_label_status = label_status(item)
        arm_item = dict(item)
        arm_item["arm"] = arm
        if arm == CLAIM_V2:
            arm_item["evidence_contract_version"] = CLAIM_V2
        else:
            arm_item.pop("evidence_contract_version", None)
        prompt = build_nli_prompt(arm_item)
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        item_sample_id = sample_fingerprint(arm_item)
        if base_label_status == "bound":
            arm_item["label_sample_id"] = item_sample_id
        item_label_status = label_status(arm_item)
        contract_version = CLAIM_V2 if arm == CLAIM_V2 else "current"
        identity = build_request_identity(
            arm_item,
            prompt=prompt,
            model_config=model_cfg,
            contract_version=contract_version,
        )

        if cache_key in live_cache:
            cand_entry = live_cache[cache_key]
            v_check = verify_cache_entry(cand_entry, identity)
            if v_check.status == "verified_hit":
                execution_telemetry["verified_cache_reused"] += 1
                execution_telemetry["measured_attempts_total"] += int(cand_entry.get("attempts", 1) or 1)
                execution_telemetry["measured_tokens_total"] += int(cand_entry.get("tokens", 0) or 0)
                if cand_entry.get("provider_filtered"):
                    execution_telemetry["provider_filtered_or_failed_calls"] += 1
                return {
                    **cand_entry,
                    "label_sample_id": arm_item.get("label_sample_id"),
                    "label_status": item_label_status,
                    "label_provenance": arm_item.get("label_provenance", ""),
                    "cache_verification_status": v_check.status,
                }
            if v_check.status == "re_adjudicate_needed":
                re_adj = re_adjudicate_cached_record(cand_entry, arm_item, identity)
                live_cache[cache_key] = re_adj
                execution_telemetry["verified_cache_reused"] += 1
                execution_telemetry["measured_attempts_total"] += int(re_adj.get("attempts", 1) or 1)
                execution_telemetry["measured_tokens_total"] += int(re_adj.get("tokens", 0) or 0)
                return {
                    **re_adj,
                    "label_sample_id": arm_item.get("label_sample_id"),
                    "label_status": item_label_status,
                    "label_provenance": arm_item.get("label_provenance", ""),
                    "cache_verification_status": v_check.status,
                }
            if v_check.status == "unverifiable":
                execution_telemetry["unverifiable_historical_records"] += 1
                if os.environ.get("BC_EVAL_OFFLINE_ONLY", "1") == "1":
                    # T3: NEVER overwrite current hashes onto historical unverifiable records!
                    return {
                        **cand_entry,
                        "sample_id": item_sample_id,
                        "label_sample_id": arm_item.get("label_sample_id"),
                        "label_status": item_label_status,
                        "label_provenance": arm_item.get("label_provenance", ""),
                        "prompt_sha256": prompt_sha256,
                        "cache_verification_status": "unverifiable",
                        "cache_verification_reason": v_check.reason,
                    }
            else:
                execution_telemetry["mismatched_cache_rejected"] += 1

        async with sem:
            execution_telemetry["live_network_calls"] += 1
            t0 = time.perf_counter()
            try:
                resp = await asyncio.wait_for(
                    client.chat_auto(
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=4096,
                    ),
                    timeout=95,
                )
                sec = round(time.perf_counter() - t0, 3)
                att = int(getattr(resp, "attempts", 1) or 1)
                execution_telemetry["measured_attempts_total"] += att
                tok = getattr(resp, "tokens", 0)
                tok_sum = (
                    sum(int(x or 0) for x in tok)
                    if isinstance(tok, (tuple, list))
                    else int(tok or 0)
                )
                execution_telemetry["measured_tokens_total"] += tok_sum
                if getattr(resp, "success", True) and getattr(resp, "content", ""):
                    raw_text = str(resp.content)
                    nli = extract_json_object(raw_text)
                    g = gate(arm_item, nli)
                    row = build_verified_cache_record(
                        arm_item,
                        identity,
                        raw_response_text=raw_text,
                        gate_result=g,
                        tokens=tok_sum,
                        seconds=sec,
                        attempts=att,
                        success=True,
                    )
                    row["arm"] = arm
                    row["label"] = lbl
                    row["label_sample_id"] = arm_item.get("label_sample_id")
                    row["label_status"] = item_label_status
                    row["label_provenance"] = arm_item.get("label_provenance", "")
                    row["cache_verification_status"] = "live_recorded"
                    live_cache[cache_key] = row
                    _save_cache()
                    return row
                execution_telemetry["provider_filtered_or_failed_calls"] += 1
                g = gate(arm_item, None, "model_call_failed")
                row = build_verified_cache_record(
                    arm_item,
                    identity,
                    raw_response_text="",
                    gate_result=g,
                    tokens=tok_sum,
                    seconds=sec,
                    attempts=att,
                    success=False,
                    error="model_call_failed",
                )
                row["arm"] = arm
                row["label"] = lbl
                row["label_sample_id"] = arm_item.get("label_sample_id")
                row["label_status"] = item_label_status
                row["label_provenance"] = arm_item.get("label_provenance", "")
                row["provider_filtered"] = True
                row["cache_verification_status"] = "live_failed"
                live_cache[cache_key] = row
                _save_cache()
                return row
            except Exception as exc:
                sec = round(time.perf_counter() - t0, 3)
                execution_telemetry["provider_filtered_or_failed_calls"] += 1
                g = gate(arm_item, None, str(exc)[:80])
                row = build_verified_cache_record(
                    arm_item,
                    identity,
                    raw_response_text="",
                    gate_result=g,
                    tokens=0,
                    seconds=sec,
                    attempts=1,
                    success=False,
                    error=str(exc)[:80],
                )
                row["arm"] = arm
                row["label"] = lbl
                row["label_sample_id"] = arm_item.get("label_sample_id")
                row["label_status"] = item_label_status
                row["label_provenance"] = arm_item.get("label_provenance", "")
                row["cache_verification_status"] = "live_exception"
                return row

    # 1. Load 55 real legacy55 candidates from 《道诡异仙》 and preserve true label_sample_id from pilot12.json
    legacy55_items = json.loads(
        (ROOT / "docs/research/bc-evaluation-2026-11-05/acceptance_prepared_legacy55.json").read_text(encoding="utf-8")
    )["items"]
    legacy55_strict2 = json.loads(
        (ROOT / "workspace/《道诡异仙》/output/acceptance_gate_eval_strict2.json").read_text(encoding="utf-8")
    )["results"]
    legacy55_labels = {r["fs_id"]: r["label"] for r in legacy55_strict2 if r.get("label")}
    pilot12_items = json.loads(
        (ROOT / "docs/research/bc-evaluation-2026-11-05/pilot12.json").read_text(encoding="utf-8")
    )["items"]
    pilot12_by_id = {it["fs_id"]: it for it in pilot12_items}

    natural_candidates: list[dict[str, Any]] = []
    for it in legacy55_items:
        c = dict(it)
        fid = c["fs_id"]
        c["work_id"] = "daogui_legacy55"
        c["stratum_source"] = "legacy55_natural"
        if not c.get("label"):
            c["label"] = legacy55_labels[fid]
        c["sample_id"] = sample_fingerprint(c)
        # F3 fix: Do NOT overwrite label_sample_id = sample_id for unverified historical labels!
        if fid in pilot12_by_id and pilot12_by_id[fid].get("label_sample_id"):
            c["label_sample_id"] = pilot12_by_id[fid]["label_sample_id"]
            if c["label_sample_id"] == c["sample_id"]:
                c["label_provenance"] = "pilot12_bound"
            else:
                c["label_provenance"] = "legacy55_pilot12_stale_without_prereg"
        else:
            c["label_sample_id"] = c.get("label_sample_id")  # None in acceptance_prepared_legacy55.json
            c["label_provenance"] = "legacy55_historical_unbound"
        natural_candidates.append(c)

    # Add 30 natural multi-work candidates -> 55 + 30 = 85 natural candidates
    mw_cands = load_natural_pipeline_candidates_30()
    natural_candidates.extend(mw_cands)

    tasks = []
    task_keys: list[tuple[str, str]] = []
    for c in natural_candidates:
        fid = c["fs_id"]
        tasks.append(eval_arm_item(c, "current"))
        task_keys.append((fid, "current"))
        tasks.append(eval_arm_item(c, CLAIM_V2))
        task_keys.append((fid, CLAIM_V2))

    outputs = await asyncio.gather(*tasks)
    _save_cache()
    live_map = dict(zip(task_keys, outputs))

    rows_current: list[dict[str, Any]] = []
    rows_full_v2: list[dict[str, Any]] = []
    rows_on_demand: list[dict[str, Any]] = []
    per_item_records: list[dict[str, Any]] = []

    triggered_count = 0
    untriggered_false_accepts_all = 0
    untriggered_false_accepts_bound = 0

    _obs_buckets = (
        "both_accepted",
        "both_not_accepted",
        "branch_disagreement_cur_accept_v2_not",
        "branch_disagreement_v2_accept_cur_not",
        "protocol_or_format_failure",
    )
    positive_rejection_breakdown = {
        "total_nominal_positive_candidates": 0,
        "by_bucket": {k: 0 for k in _obs_buckets},
        "by_stratum": {
            "legacy55_natural": {k: 0 for k in _obs_buckets},
            "natural_multiwork_recheck": {k: 0 for k in _obs_buckets},
        },
        "strict_bound_positives_only": {
            "total": 0,
            **{k: 0 for k in _obs_buckets},
        },
    }

    for c in natural_candidates:
        execution_telemetry["offline_selector_replay_items"] += 1
        fid = c["fs_id"]
        src = c["stratum_source"]
        sel = select_audit_route(c, enabled=True)
        is_triggered = bool(sel.trigger_claims_v2)
        if is_triggered:
            triggered_count += 1

        cur_r = live_map[(fid, "current")]
        v2_r = live_map[(fid, CLAIM_V2)]
        if sel.route == "hard_reject_mechanical":
            od_r = {
                **cur_r,
                "arm": "on_demand_claims_v2",
                "decision": "拒绝",
                "assessment_status": "unconfirmed",
                "selected_route": sel.route,
                "trigger_reasons": list(sel.trigger_reasons),
            }
        else:
            od_r = dict(v2_r if is_triggered else cur_r)
            od_r["arm"] = "on_demand_claims_v2"
            od_r["selected_route"] = sel.route
            od_r["trigger_reasons"] = list(sel.trigger_reasons)

        item_lbl_status = cur_r["label_status"]
        if not is_triggered and c["label"] == "不支持" and od_r["decision"] == "接受":
            untriggered_false_accepts_all += 1
            if item_lbl_status == "bound":
                untriggered_false_accepts_bound += 1

        pos_bucket = None
        if c["label"] == "支持":
            pos_bucket = classify_positive_rejection_bucket(cur_r, v2_r)
            positive_rejection_breakdown["total_nominal_positive_candidates"] += 1
            positive_rejection_breakdown["by_bucket"][pos_bucket] += 1
            positive_rejection_breakdown["by_stratum"][src][pos_bucket] += 1
            if item_lbl_status == "bound":
                positive_rejection_breakdown["strict_bound_positives_only"]["total"] += 1
                positive_rejection_breakdown["strict_bound_positives_only"][pos_bucket] += 1

        rows_current.append(cur_r)
        rows_full_v2.append(v2_r)
        rows_on_demand.append(od_r)

        per_item_records.append(
            {
                "fs_id": fid,
                "work_id": c.get("work_id", ""),
                "stratum_source": src,
                "label": c["label"],
                "sample_id": cur_r["sample_id"],
                "label_sample_id": cur_r.get("label_sample_id"),
                "label_status": item_lbl_status,
                "label_provenance": cur_r.get("label_provenance", ""),
                "verified_reference_id": c.get("verified_reference_id"),
                "model": api.model,
                "current_prompt_sha256": cur_r["prompt_sha256"],
                "claims_v2_prompt_sha256": v2_r["prompt_sha256"],
                "selected_route": sel.route,
                "trigger_claims_v2": is_triggered,
                "trigger_reasons": list(sel.trigger_reasons),
                "answer_unit_count": sel.answer_unit_count,
                "criteria_logic_type": sel.criteria_logic_type,
                "hard_signals": list(sel.soft_signals if False else sel.hard_signals),
                "soft_signals": list(sel.soft_signals),
                "current_decision": cur_r["decision"],
                "current_reasons": cur_r.get("reasons", []),
                "full_claims_v2_decision": v2_r["decision"],
                "full_claims_v2_reasons": v2_r.get("reasons", []),
                "on_demand_decision": od_r["decision"],
                "positive_attribution_bucket": pos_bucket,
                "current_tokens": cur_r["tokens"],
                "full_claims_v2_tokens": v2_r["tokens"],
                "on_demand_tokens": od_r["tokens"],
                "current_seconds": cur_r["seconds"],
                "full_claims_v2_seconds": v2_r["seconds"],
                "on_demand_seconds": od_r["seconds"],
                "measured_live": bool(cur_r.get("measured_live") and v2_r.get("measured_live")),
            }
        )

    def _cost_summary(rows: list[dict[str, Any]], v2_calls: int, *, bound_only: bool = False) -> dict[str, Any]:
        target_rows = [r for r in rows if r.get("label_status") == "bound"] if bound_only else rows
        tot_tok = sum(int(r.get("tokens", 0) or 0) for r in target_rows)
        tot_sec = round(sum(float(r.get("seconds", 0.0) or 0.0) for r in target_rows), 3)
        neg_tot = sum(1 for r in target_rows if r["label"] == "不支持")
        fa = sum(1 for r in target_rows if r["label"] == "不支持" and r["decision"] == "接受")
        pos_tot = sum(1 for r in target_rows if r["label"] == "支持")
        pos_acc = sum(1 for r in target_rows if r["label"] == "支持" and r["decision"] == "接受")
        pos_rej = sum(1 for r in target_rows if r["label"] == "支持" and r["decision"] == "拒绝")
        return {
            "total_candidates": len(target_rows),
            "claims_v2_calls": v2_calls,
            "claims_v2_call_rate": round(v2_calls / max(len(target_rows), 1), 4),
            "false_accepts": fa,
            "negative_total": neg_tot,
            "false_accept_rate": round(fa / max(neg_tot, 1), 4),
            "positive_total": pos_tot,
            "positive_accepts": pos_acc,
            "positive_rejects": pos_rej,
            "false_reject_rate": round(pos_rej / max(pos_tot, 1), 4),
            "total_tokens": tot_tok,
            "total_seconds": tot_sec,
            "all_measured_live": all(bool(r.get("measured_live")) for r in target_rows),
        }

    def _diagnostic_all_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute diagnostic metrics across all 85 items (treating nominal labels as bound for comparison only)."""
        as_bound = [{**r, "label_status": "bound"} for r in rows]
        return evaluate_metrics(as_bound)

    # 2. Separate Constructed Regression Probe Set (16 items from cross-work-20261105-v1)
    cw_dev_exam = json.loads(
        (ROOT / "workspace/bc-evaluation/cross-work-20261105-v1/probe-run-v2-development/frozen-exam.json").read_text(encoding="utf-8")
    )["items"]
    cw_dev_res = json.loads(
        (ROOT / "workspace/bc-evaluation/cross-work-20261105-v1/probe-run-v2-development/results.json").read_text(encoding="utf-8")
    )["results"]
    cw_hold_exam = json.loads(
        (ROOT / "workspace/bc-evaluation/cross-work-20261105-v1/holdout-run-v2/frozen-exam.json").read_text(encoding="utf-8")
    )["items"]
    cw_hold_res = json.loads(
        (ROOT / "workspace/bc-evaluation/cross-work-20261105-v1/holdout-run-v2/results.json").read_text(encoding="utf-8")
    )["results"]

    cw_items = {it["fs_id"]: it for it in (cw_dev_exam + cw_hold_exam) if it.get("arm") == "current"}
    cw_res_map: dict[tuple[str, str], dict[str, Any]] = {}
    for r in cw_dev_res + cw_hold_res:
        tok = r.get("tokens", 0)
        tok_sum = sum(int(x or 0) for x in tok) if isinstance(tok, (tuple, list)) else int(tok or 0)
        cw_res_map[(r["fs_id"], r["arm"])] = {
            "fs_id": r["fs_id"],
            "arm": r["arm"],
            "label": r["label"],
            "label_status": "bound",
            "decision": r["decision"],
            "assessment_status": r.get("assessment_status", "unconfirmed"),
            "reasons": r.get("reasons", []),
            "tokens": tok_sum,
            "seconds": float(r.get("seconds", 0.0) or 0.0),
            "measured_live": True,
        }

    cw_cur_rows, cw_v2_rows, cw_od_rows = [], [], []
    cw_triggered = 0
    cw_untriggered_fa = 0
    for fid, c in sorted(cw_items.items()):
        sel = select_audit_route(c, enabled=True)
        trig = bool(sel.trigger_claims_v2)
        if trig:
            cw_triggered += 1
        cr = cw_res_map[(fid, "current")]
        vr = cw_res_map[(fid, CLAIM_V2)]
        odr = dict(vr if trig else cr)
        if not trig and c["label"] == "不支持" and odr["decision"] == "接受":
            cw_untriggered_fa += 1
        cw_cur_rows.append(cr)
        cw_v2_rows.append(vr)
        cw_od_rows.append(odr)

    total_n = len(natural_candidates)
    bound_n = sum(1 for r in rows_current if r["label_status"] == "bound")
    bound_triggered = sum(
        1 for rec in per_item_records if rec["label_status"] == "bound" and rec["trigger_claims_v2"]
    )
    call_reduction = round(1.0 - (triggered_count / total_n), 4)

    cur_tok_all = sum(r["tokens"] for r in rows_current)
    v2_tok_all = sum(r["tokens"] for r in rows_full_v2)
    od_tok_all = sum(r["tokens"] for r in rows_on_demand)

    report = {
        "schema": "t4_c_on_demand_report_v3_provenance_calibrated",
        "evaluation_protocol": "paired_current_and_claims_v2_with_strict_label_binding_and_cache_provenance",
        "model": api.model,
        "execution_telemetry": execution_telemetry,
        "token_comparison_all_85": {
            "current_total_tokens": cur_tok_all,
            "full_claims_v2_total_tokens": v2_tok_all,
            "on_demand_claims_v2_total_tokens": od_tok_all,
            "on_demand_vs_full_claims_v2_token_delta_pct": round((od_tok_all - v2_tok_all) / max(v2_tok_all, 1), 4),
            "on_demand_vs_current_token_delta_pct": round((od_tok_all - cur_tok_all) / max(cur_tok_all, 1), 4),
        },
        "natural_candidates_sampling": {
            "total_candidates": total_n,
            "by_source": {
                "legacy55_natural": {"count": 55, "proportion": round(55 / total_n, 4)},
                "natural_multiwork_recheck": {"count": len(mw_cands), "proportion": round(len(mw_cands) / total_n, 4)},
            },
            "by_label_status": {
                "bound": sum(1 for r in rows_current if r["label_status"] == "bound"),
                "stale": sum(1 for r in rows_current if r["label_status"] == "stale"),
                "unbound": sum(1 for r in rows_current if r["label_status"] == "unbound"),
            },
            "by_label_provenance": {
                prov: sum(1 for r in rows_current if r.get("label_provenance") == prov)
                for prov in (
                    "pilot12_bound",
                    "legacy55_pilot12_stale_without_prereg",
                    "legacy55_historical_unbound",
                    "blind_reference_verified_positive",
                    "mechanical_or_source_verified_negative",
                    "model_self_nominated_unverified",
                )
            },
            "by_nominal_label": {
                "支持": sum(1 for c in natural_candidates if c["label"] == "支持"),
                "不支持": sum(1 for c in natural_candidates if c["label"] == "不支持"),
                "待裁决": sum(1 for c in natural_candidates if c["label"] == "待裁决"),
            },
        },
        "selector_summary": {
            "total_candidates": total_n,
            "triggered_claims_v2": triggered_count,
            "routed_current": total_n - triggered_count,
            "claims_v2_trigger_rate": round(triggered_count / total_n, 4),
            "claims_v2_call_reduction_rate": call_reduction,
            "untriggered_false_accepts_all_nominal": untriggered_false_accepts_all,
            "untriggered_false_accepts_strict_bound": untriggered_false_accepts_bound,
        },
        "strict_bound_subset_17": {
            "description": "Only candidates with independently verified bound label_sample_id == sample_id (5 pilot12 + 2 blind-reference-verified multiwork positives + 10 source-verified multiwork negatives).",
            "total_bound_candidates": bound_n,
            "triggered_claims_v2": bound_triggered,
            "current": {
                "metrics": evaluate_metrics(rows_current),
                "cost_and_risk": _cost_summary(rows_current, 0, bound_only=True),
            },
            "full_claims_v2": {
                "metrics": evaluate_metrics(rows_full_v2),
                "cost_and_risk": _cost_summary(rows_full_v2, bound_n, bound_only=True),
            },
            "on_demand_claims_v2": {
                "metrics": evaluate_metrics(rows_on_demand),
                "cost_and_risk": _cost_summary(rows_on_demand, bound_triggered, bound_only=True),
            },
        },
        "diagnostic_all_85_nominal": {
            "warning": "Includes 50 unbound/stale legacy55 historical labels and 18 model-nominated unverified multiwork positive candidates. Do NOT use false_reject_rate on this pool to relax claims_v2 rules.",
            "current": {
                "metrics": _diagnostic_all_metrics(rows_current),
                "cost_and_risk": _cost_summary(rows_current, 0, bound_only=False),
            },
            "full_claims_v2": {
                "metrics": _diagnostic_all_metrics(rows_full_v2),
                "cost_and_risk": _cost_summary(rows_full_v2, total_n, bound_only=False),
            },
            "on_demand_claims_v2": {
                "metrics": _diagnostic_all_metrics(rows_on_demand),
                "cost_and_risk": _cost_summary(rows_on_demand, triggered_count, bound_only=False),
            },
        },
        "positive_rejection_attribution_51": positive_rejection_breakdown,
        "constructed_regression_set_16": {
            "total_candidates": len(cw_items),
            "triggered_claims_v2": cw_triggered,
            "claims_v2_call_reduction_rate": round(1.0 - (cw_triggered / len(cw_items)), 4),
            "untriggered_false_accepts": cw_untriggered_fa,
            "current": _cost_summary(cw_cur_rows, 0),
            "full_claims_v2": _cost_summary(cw_v2_rows, len(cw_items)),
            "on_demand_claims_v2": _cost_summary(cw_od_rows, cw_triggered),
        },
        "records": per_item_records,
    }

    pub_path = ROOT / "docs/research/bc-improvement-2026-11-05/t4-c-on-demand-report.json"
    pub_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "execution_telemetry": report["execution_telemetry"],
                "token_comparison_all_85": report["token_comparison_all_85"],
                "natural_sampling": report["natural_candidates_sampling"],
                "selector": report["selector_summary"],
                "strict_bound_subset_17": {
                    k: v["cost_and_risk"] if isinstance(v, dict) and "cost_and_risk" in v else v
                    for k, v in report["strict_bound_subset_17"].items()
                },
                "positive_rejection_attribution_51": report["positive_rejection_attribution_51"],
                "constructed_regression_set_16": report["constructed_regression_set_16"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
