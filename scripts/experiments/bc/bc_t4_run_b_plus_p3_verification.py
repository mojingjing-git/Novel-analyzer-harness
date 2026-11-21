"""Rule B+ (`arm-b-plus-rule.txt`) 在 `p3` 前缀上的靶向修复验证运行器。

执行流程：
1. 读取已冻结的 `reference-eligible-t4_hist_holdout-p3.json` 与 `reference-eligible-t4_game_holdout-p3.json`（只读，0 发现调用）。
2. 对 `t4_hist_holdout/p3` 与 `t4_game_holdout/p3` 各执行 1 次受控 `Arm B+` 生成调用（共 2 次），
   落盘至独立的 `generation-<work>-p3-arm_b_plus.json`，并经 `build_freeze_record` + `check_isolation` +
   `save_freeze` 生成 `freeze-generation-<work>-p3-arm_b_plus.json`（绝不覆盖既有 A/B 产物）。
3. 将 `Arm A` 与 `Arm B+` 的 `p3` 候选去标识确定性混排（`blind-package-<work>-p3-b_plus.json`），
   各执行 1 次受控盲审调用（共 2 次），落盘至 `blind-review-<work>-p3-b_plus.json`。
4. 汇总生成 `b-plus-p3-verification-summary.json`，同时报告：
   - `arm_a_in_ab_batch`（原 A+B 批次中的 A 臂表现）
   - `arm_a_in_ab_plus_batch`（新 A+B+ 批次中的 A 臂表现）
   - `arm_a_cross_batch_agreement`（A 臂跨批次盲审标签一致性核查）
   - `arm_b_original`（原版 Rule B 在 p3 的表现）
   - `arm_b_plus`（修复版 Rule B+ 在 p3 的表现）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend.common.registration_eval_freeze import (  # noqa: E402
    build_freeze_record,
    check_isolation,
    save_freeze,
)
from backend.common.registration_eval_metrics import compute_metrics  # noqa: E402
from backend.common.registration_reference_set import to_metric_inputs  # noqa: E402
from backend.common.registration_usage_archive import (  # noqa: E402
    STAGE_BLIND_REVIEW,
    STAGE_GENERATION,
    UsageArchive,
)
from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.llm_client import LLMClient  # noqa: E402
from scripts.experiments.bc.bc_t4_registration_pilot_runner import (  # noqa: E402
    _sha,
    convert_to_host_schema,
    parse_generator_output,
    render_source_units,
    request_token_bound,
    run_guarded_llm_request,
)
from scripts.experiments.bc.bc_t4_run_arm_generation import (  # noqa: E402
    CHECKPOINTS,
    PROP,
    assemble,
    load_units,
)
from scripts.experiments.bc.bc_t4_run_blind_review import (  # noqa: E402
    BLIND_SYSTEM_PROMPT,
    WORKS,
    _later_units_text,
    parse_blind_review_output,
    unblind_and_score_prefix,
)

TARGET_PREFIX = "p3"
CHECKPOINT = CHECKPOINTS[TARGET_PREFIX]


async def run_single_b_plus_generation(
    *,
    work_id: str,
    model: str = "cn:deepseek-v4.1-flash",
    temperature: float = 0.1,
    max_output_tokens: int = 2000,
) -> dict:
    prefix_id = TARGET_PREFIX
    checkpoint = CHECKPOINT
    units = load_units(work_id)

    elig_path = PROP / f"reference-eligible-{work_id}-{prefix_id}.json"
    if not elig_path.is_file():
        raise FileNotFoundError(f"合格参考集缺失：{elig_path}")
    elig_doc = json.loads(elig_path.read_text(encoding="utf-8"))
    if not elig_doc.get("frozen") or int(elig_doc.get("pending_count", 1)) != 0:
        raise ValueError(f"合格参考集未完成冻结：{elig_path}")

    common_sys = (PROP / "common-system.txt").read_text(encoding="utf-8").strip()
    rule_file = PROP / "arm-b-plus-rule.txt"
    if not rule_file.is_file():
        raise FileNotFoundError(f"Rule B+ 规则文件缺失：{rule_file}")
    arm_rule = rule_file.read_text(encoding="utf-8").strip()
    sys_txt = common_sys + "\n\n" + arm_rule

    user_tpl = (PROP / "common-user-template.txt").read_text(encoding="utf-8")
    source = render_source_units(units, checkpoint)
    fields = {
        "work_id": work_id,
        "prefix_id": prefix_id,
        "visible_range_end": checkpoint,
        "registration_checkpoint": checkpoint,
        "source_units_with_stable_ids": source,
    }
    user_txt = assemble(user_tpl, fields)
    request_sha = _sha(sys_txt + "\n" + user_txt)

    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    cfg = ConfigManager(ROOT / "config.json").load()
    api = replace(
        cfg.api,
        model=model,
        temperature=temperature,
        sdk_max_retries=0,
        max_retries=0,
        temperature_max_retries=1,
        backoff_max_retries=0,
        streaming_enabled=False,
    )
    client = LLMClient(api)
    archive_path = PROP / "reference-discovery-usage.json"
    projected_in = request_token_bound(sys_txt, user_txt, model=model)

    run_res = await run_guarded_llm_request(
        client=client,
        archive_path=archive_path,
        work_id=work_id,
        prefix_id=prefix_id,
        arm="B_PLUS",
        stage=STAGE_GENERATION,
        messages=[
            {"role": "system", "content": sys_txt},
            {"role": "user", "content": user_txt},
        ],
        projected_input_tokens=projected_in,
        projected_output_tokens=max_output_tokens,
        request_sha=request_sha,
        per_request_input_limit=20000,
        per_request_output_limit=2000,
        max_runner_attempts=1,
        extra={"rule_arm": "B_PLUS", "rule_file": "arm-b-plus-rule.txt"},
    )

    if not run_res["ok"]:
        return {
            "ok": False,
            "work_id": work_id,
            "prefix_id": prefix_id,
            "arm": "B_PLUS",
            "reason": run_res.get("reason"),
            "detail": run_res.get("detail"),
            "error": run_res.get("last_error"),
        }

    res = run_res["result"]
    raw = getattr(res, "content", "") or ""
    parsed = parse_generator_output(raw)
    host_items = convert_to_host_schema(
        parsed,
        units=units,
        checkpoint=checkpoint,
        prefix_id=prefix_id,
        registration_checkpoint=checkpoint,
    )

    parse_ok = bool(parsed.get("parse_ok"))
    is_complete = parsed.get("complete") is True
    call_ok = parse_ok and is_complete
    fail_reason = None
    if not parse_ok:
        fail_reason = parsed.get("reason") or "parse_failed"
    elif not is_complete:
        fail_reason = "incomplete_output"

    record = {
        "schema": "registration_generation_v1",
        "work_id": work_id,
        "prefix_id": prefix_id,
        "arm": "B_PLUS",
        "checkpoint": checkpoint,
        "visible_range_end": checkpoint,
        "registration_checkpoint": checkpoint,
        "model": model,
        "sampling": {"temperature": temperature, "reasoning_effort": ""},
        "request_sha256": request_sha,
        "raw_response": raw,
        "parsed": parsed,
        "host_items": host_items,
        "item_count": len(host_items),
        "valid_item_count": sum(1 for h in host_items if h.get("ok")),
        "parse_ok": parse_ok,
        "complete": is_complete,
        "status": "completed" if call_ok else "failed_or_incomplete",
        "reason": fail_reason,
        "completed_at": datetime.now().isoformat(),
    }

    out_path = PROP / f"generation-{work_id}-{prefix_id}-arm_b_plus.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    # 立即构建正式冻结记录并执行硬隔离检查
    iso_res = freeze_and_check_single_b_plus_generation(work_id)

    archive = UsageArchive.load(archive_path)
    return {
        "ok": call_ok and iso_res["isolation_ok"],
        "reason": fail_reason,
        "saved": str(out_path.relative_to(ROOT)),
        "work_id": work_id,
        "prefix_id": prefix_id,
        "arm": "B_PLUS",
        "item_count": len(host_items),
        "valid_item_count": sum(1 for h in host_items if h.get("ok")),
        "parse_ok": parse_ok,
        "complete": is_complete,
        "isolation_ok": iso_res["isolation_ok"],
        "tokens": {
            "input": getattr(res, "tokens", [0, 0])[0],
            "output": getattr(res, "tokens", [0, 0])[1],
        },
        "usage_summary": archive.summary(),
    }


def freeze_and_check_single_b_plus_generation(work_id: str) -> dict:
    """对单个作品 p3 的 Arm B+ 生成产物生成冻结记录并执行硬隔离核验。"""
    prefix_id = TARGET_PREFIX
    checkpoint = CHECKPOINT
    units = load_units(work_id)
    source = render_source_units(units, checkpoint)
    later_text = _later_units_text(units, checkpoint)
    prefix_unit_ids = [u for u in sorted(units) if u <= checkpoint]
    prefix_sha = _sha(source)

    gen_path = PROP / f"generation-{work_id}-{prefix_id}-arm_b_plus.json"
    if not gen_path.is_file():
        raise FileNotFoundError(f"Arm B+ 生成产物缺失：{gen_path}")
    gen_doc = json.loads(gen_path.read_text(encoding="utf-8"))
    if not gen_doc.get("complete") or not gen_doc.get("parse_ok"):
        raise ValueError(f"Arm B+ 生成产物未完成或解析失败：{gen_path}")

    host_items = gen_doc.get("host_items") or []
    freeze_items = []
    observations = []
    for h in host_items:
        freeze_items.append(
            {
                "item_id": h.get("item_id"),
                "question": h.get("question"),
                "criteria": h.get("answer_criteria"),
                "source_span": {
                    "unit_id": h.get("source_unit_id"),
                    "start": h.get("citation_start"),
                    "end": h.get("citation_end"),
                    "quote": h.get("anchor_quote"),
                    "located": bool(h.get("ok")),
                },
                "visible_prefix_id": prefix_id,
            }
        )
        observations.append(
            {
                "item_id": h.get("item_id"),
                "citation_char": h.get("citation_start"),
                "visible_range_end": checkpoint,
                "registration_checkpoint": checkpoint,
                "wall_clock": gen_doc.get("completed_at", ""),
            }
        )

    materials_with_content = [
        {
            "kind": "body_prefix",
            "used": True,
            "sha256": prefix_sha,
            "chars": len(source),
            "source_prefix_id": prefix_id,
            "content": source,
        },
        {
            "kind": "summary",
            "used": False,
            "sha256": "unused",
            "chars": 0,
            "source_prefix_id": prefix_id,
        },
        {
            "kind": "knowledge",
            "used": False,
            "sha256": "unused",
            "chars": 0,
            "source_prefix_id": prefix_id,
        },
        {
            "kind": "retrieval",
            "used": False,
            "sha256": "unused",
            "chars": 0,
            "source_prefix_id": prefix_id,
        },
    ]
    materials_meta = [
        {k: v for k, v in m.items() if k != "content"} for m in materials_with_content
    ]

    freeze_rec = build_freeze_record(
        work_id=work_id,
        prefix_id=prefix_id,
        prefix_unit_ids=prefix_unit_ids,
        prefix_sha256=prefix_sha,
        materials=materials_meta,
        items=freeze_items,
        observations=observations,
        requests=[
            {
                "request_sha256": gen_doc.get("request_sha256"),
                "model": gen_doc.get("model"),
                "sampling": gen_doc.get("sampling"),
            }
        ],
        rules_version="registration_anchor_b_plus_v1",
        prompt_template_version="common-user-template_v1",
        generator_model=str(gen_doc.get("model") or ""),
        code_version="bc_t4_run_b_plus_p3_verification_v1",
        run_id=f"{work_id}__{prefix_id}__arm_b_plus",
        arm="B_PLUS",
    )

    iso = check_isolation(
        record=freeze_rec,
        materials=materials_with_content,
        prefix_text=source,
        later_text=later_text,
    )
    if not iso["ok"]:
        raise ValueError(f"Arm B+ 隔离核验未通过：{iso}")

    freeze_path = PROP / f"freeze-generation-{work_id}-{prefix_id}-arm_b_plus.json"
    created = save_freeze(freeze_path, freeze_rec)
    return {
        "work_id": work_id,
        "prefix_id": prefix_id,
        "arm": "B_PLUS",
        "freeze_file": str(freeze_path.relative_to(ROOT)),
        "created_this_run": created,
        "isolation_ok": iso["ok"],
        "isolation_problems": iso["problems"],
    }


def build_b_plus_blind_package(
    work_id: str,
    *,
    model: str = "cn:deepseek-v4.1-flash",
) -> dict:
    """构建单个作品 p3 的 (Arm A + Arm B+) 去标识混排盲审包。"""
    prefix_id = TARGET_PREFIX
    checkpoint = CHECKPOINT
    units = load_units(work_id)
    source = render_source_units(units, checkpoint)

    elig_path = PROP / f"reference-eligible-{work_id}-{prefix_id}.json"
    elig_doc = json.loads(elig_path.read_text(encoding="utf-8"))

    gen_a_path = PROP / f"generation-{work_id}-{prefix_id}-arm_a.json"
    gen_bp_path = PROP / f"generation-{work_id}-{prefix_id}-arm_b_plus.json"
    gen_a = json.loads(gen_a_path.read_text(encoding="utf-8"))
    gen_bp = json.loads(gen_bp_path.read_text(encoding="utf-8"))
    for arm_label, gdoc, gpath in (("A", gen_a, gen_a_path), ("B_PLUS", gen_bp, gen_bp_path)):
        if not gdoc.get("complete") or not gdoc.get("parse_ok"):
            raise ValueError(f"{arm_label} 生成产物未完成或解析失败：{gpath}")

    # 为复用 unblind_and_score_prefix，内部 blind_key 的第二臂键标记为 "B"，并在包元数据中显式标注 b_arm_variant="B_PLUS"
    raw_pool = []
    for arm_label, gdoc in (("A", gen_a), ("B", gen_bp)):
        for h in gdoc.get("host_items") or []:
            raw_pool.append(
                {
                    "arm": arm_label,
                    "arm_variant": "A" if arm_label == "A" else "B_PLUS",
                    "item_id": str(h.get("item_id")),
                    "question": str(h.get("question") or ""),
                    "answer_criteria": str(h.get("answer_criteria") or ""),
                    "implication": str(h.get("implication") or ""),
                    "source_unit_id": h.get("source_unit_id"),
                    "anchor_quote": str(h.get("anchor_quote") or ""),
                    "host_span_located": bool(h.get("ok")),
                    "citation_start": h.get("citation_start"),
                    "citation_end": h.get("citation_end"),
                    "locate_reason": h.get("reason", ""),
                }
            )

    seed_hex = _sha(f"blind_review_b_plus_v1:{work_id}:{prefix_id}")
    rng = random.Random(int(seed_hex[:16], 16))
    shuffled_indices = list(range(len(raw_pool)))
    rng.shuffle(shuffled_indices)

    blind_candidates = []
    blind_key: dict[str, dict] = {}
    for idx_pos, pool_idx in enumerate(shuffled_indices, start=1):
        entry = raw_pool[pool_idx]
        cand_id = f"cand_{idx_pos:02d}"
        blind_candidates.append(
            {
                "cand_id": cand_id,
                "question": entry["question"],
                "answer_criteria": entry["answer_criteria"],
                "implication": entry["implication"],
                "source_unit_id": entry["source_unit_id"],
                "anchor_quote": entry["anchor_quote"],
                "host_span_located": entry["host_span_located"],
                "citation_start": entry["citation_start"],
                "citation_end": entry["citation_end"],
            }
        )
        blind_key[cand_id] = entry

    eligible_refs_for_prompt = []
    for item in elig_doc.get("items") or []:
        qual = item.get("qualification") or {}
        eligible_refs_for_prompt.append(
            {
                "ref_id": item.get("ref_id"),
                "question": item.get("question"),
                "accepted_answer_criteria": qual.get("accepted_answer_criteria")
                or item.get("answer_criteria"),
                "accepted_spans": qual.get("accepted_spans") or item.get("acceptable_spans"),
            }
        )

    user_txt = (
        f"work_id: {work_id}\n"
        f"prefix_id: {prefix_id}\n"
        f"visible_range_end: {checkpoint}\n\n"
        f"<source_prefix>\n{source}\n</source_prefix>\n\n"
        f"<eligible_references>\n"
        f"{json.dumps(eligible_refs_for_prompt, ensure_ascii=False, indent=2)}\n"
        f"</eligible_references>\n\n"
        f"<system_candidates>\n"
        f"{json.dumps(blind_candidates, ensure_ascii=False, indent=2)}\n"
        f"</system_candidates>\n"
    )
    request_sha = _sha(BLIND_SYSTEM_PROMPT + "\n" + user_txt)
    projected_in = request_token_bound(BLIND_SYSTEM_PROMPT, user_txt, model=model)

    pkg = {
        "schema": "registration_blind_package_b_plus_v1",
        "work_id": work_id,
        "prefix_id": prefix_id,
        "visible_range_end": checkpoint,
        "b_arm_variant": "B_PLUS",
        "eligible_freeze_sha256": elig_doc.get("freeze_sha256"),
        "generation_a_request_sha256": gen_a.get("request_sha256"),
        "generation_b_plus_request_sha256": gen_bp.get("request_sha256"),
        "candidate_count": len(blind_candidates),
        "arm_a_count": len(gen_a.get("host_items") or []),
        "arm_b_plus_count": len(gen_bp.get("host_items") or []),
        "eligible_ref_count": len(eligible_refs_for_prompt),
        "eligible_ref_ids": elig_doc.get("eligible_ref_ids") or [],
        "coverage_groups": elig_doc.get("coverage_groups") or [],
        "shuffle_seed_sha256": seed_hex,
        "blind_candidates": blind_candidates,
        "blind_key": blind_key,
        "request_sha256": request_sha,
        "projected_input_tokens": projected_in,
        "system_prompt": BLIND_SYSTEM_PROMPT,
        "user_prompt": user_txt,
    }
    pkg_path = PROP / f"blind-package-{work_id}-{prefix_id}-b_plus.json"
    pkg_path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    return pkg


async def run_single_b_plus_blind_review(
    *,
    work_id: str,
    model: str = "cn:deepseek-v4.1-flash",
    temperature: float = 0.1,
    max_output_tokens: int = 2000,
) -> dict:
    prefix_id = TARGET_PREFIX
    pkg = build_b_plus_blind_package(work_id, model=model)
    elig_doc = json.loads(
        (PROP / f"reference-eligible-{work_id}-{prefix_id}.json").read_text(encoding="utf-8")
    )

    sys_txt = pkg["system_prompt"]
    user_txt = pkg["user_prompt"]
    request_sha = pkg["request_sha256"]
    projected_in = pkg["projected_input_tokens"]

    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    cfg = ConfigManager(ROOT / "config.json").load()
    api = replace(
        cfg.api,
        model=model,
        temperature=temperature,
        sdk_max_retries=0,
        max_retries=0,
        temperature_max_retries=1,
        backoff_max_retries=0,
        streaming_enabled=False,
    )
    client = LLMClient(api)
    archive_path = PROP / "reference-discovery-usage.json"

    run_res = await run_guarded_llm_request(
        client=client,
        archive_path=archive_path,
        work_id=work_id,
        prefix_id=prefix_id,
        arm="-",
        stage=STAGE_BLIND_REVIEW,
        messages=[
            {"role": "system", "content": sys_txt},
            {"role": "user", "content": user_txt},
        ],
        projected_input_tokens=projected_in,
        projected_output_tokens=max_output_tokens,
        request_sha=request_sha,
        per_request_input_limit=20000,
        per_request_output_limit=2000,
        max_runner_attempts=1,
        extra={
            "batch_variant": "A_vs_B_PLUS",
            "candidate_count": pkg["candidate_count"],
            "eligible_ref_count": pkg["eligible_ref_count"],
        },
    )

    if not run_res["ok"]:
        return {
            "ok": False,
            "work_id": work_id,
            "prefix_id": prefix_id,
            "reason": run_res.get("reason"),
            "detail": run_res.get("detail"),
            "error": run_res.get("last_error"),
        }

    res = run_res["result"]
    raw = getattr(res, "content", "") or ""
    expected_cand_ids = [c["cand_id"] for c in pkg["blind_candidates"]]
    expected_ref_ids = [str(r) for r in (elig_doc.get("eligible_ref_ids") or [])]

    parsed = parse_blind_review_output(
        raw,
        expected_cand_ids=expected_cand_ids,
        expected_ref_ids=expected_ref_ids,
    )
    call_ok = bool(parsed["parse_ok"] and parsed["complete"])
    unblinded = (
        unblind_and_score_prefix(pkg=pkg, elig_doc=elig_doc, parsed_review=parsed)
        if call_ok
        else {}
    )

    record = {
        "schema": "registration_blind_review_b_plus_v1",
        "work_id": work_id,
        "prefix_id": prefix_id,
        "b_arm_variant": "B_PLUS",
        "visible_range_end": pkg["visible_range_end"],
        "model": model,
        "sampling": {"temperature": temperature, "reasoning_effort": ""},
        "request_sha256": request_sha,
        "raw_response": raw,
        "parsed": parsed,
        "parse_ok": parsed["parse_ok"],
        "complete": parsed["complete"],
        "status": "completed" if call_ok else "failed_or_incomplete",
        "reason": parsed["reason"],
        "unblinded": unblinded,
        "completed_at": datetime.now().isoformat(),
    }
    out_path = PROP / f"blind-review-{work_id}-{prefix_id}-b_plus.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    archive = UsageArchive.load(archive_path)
    return {
        "ok": call_ok,
        "reason": parsed["reason"],
        "saved": str(out_path.relative_to(ROOT)),
        "work_id": work_id,
        "prefix_id": prefix_id,
        "candidate_count": pkg["candidate_count"],
        "eligible_ref_count": pkg["eligible_ref_count"],
        "parse_ok": parsed["parse_ok"],
        "complete": parsed["complete"],
        "tokens": {
            "input": getattr(res, "tokens", [0, 0])[0],
            "output": getattr(res, "tokens", [0, 0])[1],
        },
        "usage_summary": archive.summary(),
    }


def build_b_plus_p3_summary() -> dict:
    """汇总 p3 前缀上 Arm A（双批次）、原版 Arm B 与修复版 Arm B+ 的完整对比及跨批次一致性。"""
    prefix_id = TARGET_PREFIX
    variants = ("arm_a_in_ab_batch", "arm_a_in_ab_plus_batch", "arm_b_original", "arm_b_plus")

    agg_sys: dict[str, list[dict]] = {v: [] for v in variants}
    agg_adj: dict[str, list[dict]] = {v: [] for v in variants}
    agg_ref: dict[str, list[dict]] = {v: [] for v in variants}
    agg_ref_strict: dict[str, list[dict]] = {v: [] for v in variants}
    suff_by_variant: dict[str, dict[str, int]] = {
        v: {"total_located": 0, "sufficient": 0, "insufficient": 0} for v in variants
    }

    per_work_p3: dict[str, Any] = {}
    arm_a_cross_batch_items: list[dict] = []

    for w in WORKS:
        elig_doc = json.loads(
            (PROP / f"reference-eligible-{w}-{prefix_id}.json").read_text(encoding="utf-8")
        )
        orig_rev = json.loads(
            (PROP / f"blind-review-{w}-{prefix_id}.json").read_text(encoding="utf-8")
        )
        bplus_rev = json.loads(
            (PROP / f"blind-review-{w}-{prefix_id}-b_plus.json").read_text(encoding="utf-8")
        )
        orig_unb = orig_rev["unblinded"]
        bplus_unb = bplus_rev["unblinded"]

        # 核对 Arm A 在两次独立盲审批次（A+B vs A+B+）中的逐条标签一致性
        orig_a_by_item = {
            uc["item_id"]: uc
            for uc in orig_unb["unblinded_candidates"]
            if uc["arm"] == "A"
        }
        bplus_a_by_item = {
            uc["item_id"]: uc
            for uc in bplus_unb["unblinded_candidates"]
            if uc["arm"] == "A"
        }
        for iid in sorted(orig_a_by_item):
            oa = orig_a_by_item[iid]
            na = bplus_a_by_item[iid]
            arm_a_cross_batch_items.append(
                {
                    "work_id": w,
                    "prefix_id": prefix_id,
                    "item_id": iid,
                    "question": oa["question"],
                    "label_in_ab_batch": oa["label"],
                    "label_in_ab_plus_batch": na["label"],
                    "label_agreed": oa["label"] == na["label"],
                    "sufficient_in_ab_batch": oa["anchor_sufficient_at_quote"],
                    "sufficient_in_ab_plus_batch": na["anchor_sufficient_at_quote"],
                    "sufficient_agreed": (
                        oa["anchor_sufficient_at_quote"] == na["anchor_sufficient_at_quote"]
                    ),
                }
            )

        def _collect(vkey: str, unb_doc: Mapping[str, Any], arm_code: str) -> None:
            for uc in unb_doc["unblinded_candidates"]:
                if uc["arm"] != arm_code:
                    continue
                gid = f"{w}__{prefix_id}__{vkey}__{uc['item_id']}"
                agg_sys[vkey].append({"item_id": gid})
                agg_adj[vkey].append(
                    {
                        "item_id": gid,
                        "label": uc["label"],
                        "span_located": bool(uc["host_span_located"]),
                        "disputed": False,
                    }
                )
                if uc["host_span_located"]:
                    suff_by_variant[vkey]["total_located"] += 1
                    if uc["anchor_sufficient_at_quote"] is True:
                        suff_by_variant[vkey]["sufficient"] += 1
                    else:
                        suff_by_variant[vkey]["insufficient"] += 1

            for q_in in to_metric_inputs(elig_doc, unb_doc["per_arm_matching"], arm_code):
                agg_ref[vkey].append(
                    {
                        "question_id": f"{w}__{prefix_id}__{q_in['question_id']}",
                        "registered": q_in["registered"],
                        "pairing_status": q_in["pairing_status"],
                    }
                )
            for q_in in to_metric_inputs(
                elig_doc, unb_doc["per_arm_matching_strict_1to1"], arm_code
            ):
                agg_ref_strict[vkey].append(
                    {
                        "question_id": f"{w}__{prefix_id}__{q_in['question_id']}",
                        "registered": q_in["registered"],
                        "pairing_status": q_in["pairing_status"],
                    }
                )

        _collect("arm_a_in_ab_batch", orig_unb, "A")
        _collect("arm_b_original", orig_unb, "B")
        _collect("arm_a_in_ab_plus_batch", bplus_unb, "A")
        _collect("arm_b_plus", bplus_unb, "B")

        per_work_p3[f"{w}/{prefix_id}"] = {
            "arm_a_in_ab_batch": orig_unb["per_arm_metrics"]["A"],
            "arm_b_original": orig_unb["per_arm_metrics"]["B"],
            "arm_a_in_ab_plus_batch": bplus_unb["per_arm_metrics"]["A"],
            "arm_b_plus": bplus_unb["per_arm_metrics"]["B"],
            "arm_b_plus_candidates": [
                uc for uc in bplus_unb["unblinded_candidates"] if uc["arm"] == "B"
            ],
            "paired_questions_a_vs_b_plus": bplus_unb["paired_questions"],
        }

    combined_p3_metrics = {}
    combined_p3_metrics_strict_1to1 = {}
    for v in variants:
        combined_p3_metrics[v] = compute_metrics(
            system_items=agg_sys[v],
            adjudicated_items=agg_adj[v],
            independent_qualified=agg_ref[v],
        )
        combined_p3_metrics_strict_1to1[v] = compute_metrics(
            system_items=agg_sys[v],
            adjudicated_items=agg_adj[v],
            independent_qualified=agg_ref_strict[v],
        )

    n_a_items = len(arm_a_cross_batch_items)
    n_label_agreed = sum(1 for x in arm_a_cross_batch_items if x["label_agreed"])
    n_suff_agreed = sum(1 for x in arm_a_cross_batch_items if x["sufficient_agreed"])

    archive = UsageArchive.load(PROP / "reference-discovery-usage.json")
    summary_doc = {
        "schema": "registration_b_plus_p3_verification_summary_v1",
        "methodological_notes": [
            "1. 靶向机制修复验证声明：Rule B+ (arm-b-plus-rule.txt) 系在观察到原版 Rule B 于 hist/p3 与 game/p3 上的已答仍登记倒退后设计的解耦规则；因此本验证属于已暴露前缀上的靶向机制修复回归（Targeted Post-Hoc Mechanism Verification），不等同于未曝光留出集（Unexposed Holdout）的终局证明。生产开关 enable_anchor_b_plus_rule 据此保持默认 False（可选开启）。",
            "2. 跨批次盲审一致性校验：为避免单次输出超过 2,000 tokens 硬上限，p3 新盲审包将 Arm A 与 Arm B+ 混排盲审，并在本汇总中同时并列报告 Arm A 在原 A+B 批次与新 A+B+ 批次中的指标及逐条标签一致性（arm_a_cross_batch_agreement）。",
        ],
        "combined_p3_metrics_by_variant": combined_p3_metrics,
        "combined_p3_metrics_strict_1to1_by_variant": combined_p3_metrics_strict_1to1,
        "anchor_sufficiency_p3_by_variant": suff_by_variant,
        "arm_a_cross_batch_agreement": {
            "total_items": n_a_items,
            "label_agreed_count": n_label_agreed,
            "label_agreement_rate": round(n_label_agreed / n_a_items, 6) if n_a_items else None,
            "sufficiency_agreed_count": n_suff_agreed,
            "sufficiency_agreement_rate": (
                round(n_suff_agreed / n_a_items, 6) if n_a_items else None
            ),
            "per_item": arm_a_cross_batch_items,
        },
        "per_work_p3": per_work_p3,
        "usage_summary": archive.summary(),
        "generated_at": datetime.now().isoformat(),
    }
    out_path = PROP / "b-plus-p3-verification-summary.json"
    out_path.write_text(json.dumps(summary_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary_doc


async def run_all_b_plus_p3(
    *,
    model: str = "cn:deepseek-v4.1-flash",
    temperature: float = 0.1,
    max_output_tokens: int = 2000,
    skip_existing: bool = True,
) -> dict:
    gen_results = []
    for idx, w in enumerate(WORKS, start=1):
        gen_path = PROP / f"generation-{w}-{TARGET_PREFIX}-arm_b_plus.json"
        if skip_existing and gen_path.is_file():
            existing = json.loads(gen_path.read_text(encoding="utf-8"))
            if existing.get("complete") is True and existing.get("parse_ok") is True:
                iso_res = freeze_and_check_single_b_plus_generation(w)
                gen_results.append(
                    {
                        "ok": True,
                        "skipped": True,
                        "work_id": w,
                        "prefix_id": TARGET_PREFIX,
                        "arm": "B_PLUS",
                        "item_count": existing.get("item_count"),
                        "valid_item_count": existing.get("valid_item_count"),
                        "isolation_ok": iso_res["isolation_ok"],
                    }
                )
                continue
        print(f"[生成 {idx}/2] 启动 Arm B+ 生成: work={w}, prefix={TARGET_PREFIX}...")
        res = await run_single_b_plus_generation(
            work_id=w,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        gen_results.append(res)
        if not res.get("ok"):
            return {"ok": False, "stage": "generation", "results": gen_results}

    blind_results = []
    for idx, w in enumerate(WORKS, start=1):
        rev_path = PROP / f"blind-review-{w}-{TARGET_PREFIX}-b_plus.json"
        if skip_existing and rev_path.is_file():
            existing = json.loads(rev_path.read_text(encoding="utf-8"))
            if existing.get("complete") is True and existing.get("parse_ok") is True:
                blind_results.append(
                    {
                        "ok": True,
                        "skipped": True,
                        "work_id": w,
                        "prefix_id": TARGET_PREFIX,
                    }
                )
                continue
        print(f"[盲审 {idx}/2] 启动 A vs B+ 去标识盲审: work={w}, prefix={TARGET_PREFIX}...")
        res = await run_single_b_plus_blind_review(
            work_id=w,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        blind_results.append(res)
        if not res.get("ok"):
            return {
                "ok": False,
                "stage": "blind_review",
                "gen_results": gen_results,
                "blind_results": blind_results,
            }

    summary = build_b_plus_p3_summary()
    return {
        "ok": True,
        "gen_results": gen_results,
        "blind_results": blind_results,
        "summary_saved": "docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal/b-plus-p3-verification-summary.json",
        "usage_summary": summary["usage_summary"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare-only", action="store_true", help="离线预检生成阶段请求 token 上界")
    ap.add_argument("--run-all", action="store_true", help="运行 2 次 B+ 生成与 2 次去标识盲审并汇总")
    ap.add_argument("--summarize-only", action="store_true", help="仅离线汇总已落盘结果")
    ap.add_argument("--model", default="cn:deepseek-v4.1-flash")
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--max-output-tokens", type=int, default=2000)
    args = ap.parse_args()

    if args.prepare_only:
        common_sys = (PROP / "common-system.txt").read_text(encoding="utf-8").strip()
        arm_rule = (PROP / "arm-b-plus-rule.txt").read_text(encoding="utf-8").strip()
        sys_txt = common_sys + "\n\n" + arm_rule
        user_tpl = (PROP / "common-user-template.txt").read_text(encoding="utf-8")
        checks = []
        for w in WORKS:
            units = load_units(w)
            source = render_source_units(units, CHECKPOINT)
            user_txt = assemble(
                user_tpl,
                {
                    "work_id": w,
                    "prefix_id": TARGET_PREFIX,
                    "visible_range_end": CHECKPOINT,
                    "registration_checkpoint": CHECKPOINT,
                    "source_units_with_stable_ids": source,
                },
            )
            proj_in = request_token_bound(sys_txt, user_txt, model=args.model)
            checks.append(
                {
                    "work_id": w,
                    "prefix_id": TARGET_PREFIX,
                    "arm": "B_PLUS",
                    "projected_generation_input_tokens": proj_in,
                    "within_per_request_limit": proj_in <= 20000,
                }
            )
        print(json.dumps({"generation_preflight": checks}, ensure_ascii=False, indent=2))
        return

    if args.summarize_only:
        summary = build_b_plus_p3_summary()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    if args.run_all:
        out = asyncio.run(
            run_all_b_plus_p3(
                model=args.model,
                temperature=args.temperature,
                max_output_tokens=args.max_output_tokens,
            )
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if not out.get("ok"):
            sys.exit(1)
        return

    ap.error("请指定 --prepare-only、--run-all 或 --summarize-only")


if __name__ == "__main__":
    main()
