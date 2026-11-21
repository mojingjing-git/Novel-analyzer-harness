"""两臂生成冻结隔离核验与去标识盲审运行器（严格受控单次传输，原子预留与结算）。

功能：
1. 对 12 份两臂生成产物 (`generation-<work>-<prefix>-arm_<a|b>.json`) 生成正式冻结记录
   (`freeze-generation-<work>-<prefix>-arm_<a|b>.json`) 并执行硬隔离核验 (`check_isolation`)。
2. 构造 6 个前缀的去标识盲审包 (`blind-package-<work>-<prefix>.json`)：
   - 混排 A/B 两臂条目并重编号为 `cand_01 .. cand_N`，隐藏 `arm` 与原始 `item_id`；
   - 包含完整可见前缀原文 `<source_prefix>` (`unit_id <= checkpoint`) 与已冻结的合格参考集 `eligible_references`；
   - 本地保存 `blind_key` 供宿主解盲还原。
3. 执行受控单次盲审调用 (`stage=STAGE_BLIND_REVIEW`)，解析并校验：
   - 每条系统候选的完整前缀复核标签 (`有依据` / `无依据` / `已经回答` / `无法确定`)；
   - 每条系统候选在引文末尾处的局部自足性 (`anchor_sufficient_at_quote`)；
   - 系统候选与合格参考集的双向配对 (`matched_ref_ids` 与 `ref_matches`)，显式防范合并池多候选遮蔽；
   - 系统候选之间的同题聚类 (`question_cluster_id`)，用于解盲后提取两臂同题配对子集与单臂独有提问。
4. 解盲并衔接 `match_against_arms`、`to_metric_inputs` 与 `compute_metrics`，生成各前缀盲审结果
   (`blind-review-<work>-<prefix>.json`) 与全量汇总报告 (`pilot-metrics-summary.json`)。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend.common.registration_eval_freeze import (  # noqa: E402
    build_freeze_record,
    check_isolation,
    save_freeze,
)
from backend.common.registration_eval_metrics import (  # noqa: E402
    ITEM_LABELS as METRIC_ITEM_LABELS,
    SETTLED_LABELS,
    compute_metrics,
)
from backend.common.registration_reference_set import (  # noqa: E402
    match_against_arms,
    to_metric_inputs,
)
from backend.common.registration_usage_archive import (  # noqa: E402
    STAGE_BLIND_REVIEW,
    UsageArchive,
)
from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.llm_client import LLMClient  # noqa: E402
from scripts.experiments.bc.bc_t4_registration_pilot_runner import (  # noqa: E402
    _sha,
    render_source_units,
    request_token_bound,
    run_guarded_llm_request,
)
from scripts.experiments.bc.bc_t4_run_arm_generation import (  # noqa: E402
    CHECKPOINTS,
    PROP,
    load_units,
)

WORKS = ("t4_hist_holdout", "t4_game_holdout")
PREFIXES = ("p1", "p2", "p3")
ARMS = ("A", "B")

BLIND_SYSTEM_PROMPT = """你是一个独立的小说伏笔登记盲审复核员。
你将收到：
1. 当前允许阅读的完整可见小说前缀 `<source_prefix>`（包含 `unit_id <= visible_range_end` 的全部原文，禁止使用任何可见前缀之外的后文知识或外部先验）；
2. 当前前缀已冻结的合格参考问题集合 `<eligible_references>`（每条含 `ref_id`、`question`、`accepted_answer_criteria`）；
3. 去标识混排后的系统候选登记列表 `<system_candidates>`（编号为 `cand_01 .. cand_N`，可能来自多次不同运行，因此列表中可能存在多条候选提出相同或高度等价的核心问题）。

你的任务是在不依赖任何外部知识的前提下，严格基于 `<source_prefix>` 对全部 `<system_candidates>` 与 `<eligible_references>` 进行逐条复核与双向配对。

### 一、逐条候选复核规则 (`candidate_reviews`)
对 `<system_candidates>` 中的每一个 `cand_id`（不得遗漏任何一条），输出：
1. `cand_id`: 候选编号（如 `"cand_01"`）。
2. `label`: 站在**整个可见前缀末尾**（读完 `<source_prefix>` 全部可见单元后）对该候选问题的判定，只能取以下四个值之一：
   - `"有依据"`：可见前缀原文确实给出了提出该问题的明确文本依据，且截至可见前缀末尾，该问题仍未得到回答；
   - `"已经回答"`：截至可见前缀末尾（包括该候选引文所在单元的后半部分或后续可见单元），原文已经给出了满足其回答条件的事实或明确交代；
   - `"无依据"`：可见前缀原文并无提出该问题的文本根据（纯属臆测、强加设定或引文与问题无关）；
   - `"无法确定"`：证据含糊，无法可靠判定。
3. `anchor_sufficient_at_quote`: 布尔值 (`true` 或 `false`)。
   - 检查：假设读者**把阅读范围暂时停在该候选给出的 `anchor_quote` 末尾**（仅凭该处及此前原文，不借助 `anchor_quote` 之后才出现的正文信息），是否**已经足以提出当前 `question`**，且**在 `anchor_quote` 当时尚未被回答**？
   - 若该候选 `host_span_located` 为 `false`，或提问借用了 `anchor_quote` 之后才出现的事实/设定，或仅凭 `anchor_quote` 及此前原文还不足以提出该问题，必须填 `false`。
4. `matched_ref_ids`: 字符串数组（如 `["r0"]` 或 `[]`）。
   - 若该候选与 `<eligible_references>` 中的某条或多条参考问题追问的是**同一个核心未知问题**（核心未知与回答指向一致，允许表面措辞不同），填入对应的 `ref_id`；否则填 `[]`。
   - 若一条复合候选同时完整包含了两条拆分参考问题的追问，可同时填入这两个 `ref_id`。
5. `question_cluster_id`: 字符串（如 `"q_01"`、`"q_02"`）。
   - 将 `<system_candidates>` 中追问**同一个核心问题**的所有 `cand_id` 归入同一个 `question_cluster_id`（即使该问题不在 `<eligible_references>` 中，只要两条候选问的是同一件事，就必须共享相同的 `question_cluster_id`；若某候选是唯一提问，则独占一个编号）。
6. `reason`: 简明扼要的判定理由（建议 25 字以内，直接指明依据或已在何处回答）。

### 二、合格参考双向核对规则 (`ref_matches`)
对 `<eligible_references>` 中的每一个 `ref_id`（不得遗漏任何一条），输出：
1. `ref_id`: 参考条目编号（如 `"r0"`）。
2. `status`: 只能取以下三个值之一：
   - `"matched"`：`<system_candidates>` 中至少有一条候选提出了同一核心问题；
   - `"missed_by_all"`：`<system_candidates>` 中没有任何候选提出该问题；
   - `"uncertain"`：语义边界模糊，无法确定是否配对成功。
3. `matched_cand_ids`: 字符串数组。
   - **极其重要**：由于 `<system_candidates>` 是多次运行的混排池，**可能有多条不同的 `cand_id` 都匹配同一个 `ref_id`**。你必须把 `<system_candidates>` 中所有匹配该 `ref_id` 的 `cand_id` **全部列出**（必须与 `candidate_reviews` 中的 `matched_ref_ids` 严格双向一致），绝不能只列第一条而漏掉其余同题候选！
4. `reason`: 简明配对理由（建议 20 字以内）。

### 三、输出格式要求
只输出一个合法的 JSON 对象，不要输出任何额外解释文字：
{
  "complete": true,
  "candidate_reviews": [
    {
      "cand_id": "cand_01",
      "label": "有依据 | 无依据 | 已经回答 | 无法确定",
      "anchor_sufficient_at_quote": true,
      "matched_ref_ids": ["r0"],
      "question_cluster_id": "q_01",
      "reason": "简短理由"
    }
  ],
  "ref_matches": [
    {
      "ref_id": "r0",
      "status": "matched | missed_by_all | uncertain",
      "matched_cand_ids": ["cand_01", "cand_08"],
      "reason": "简短理由"
    }
  ]
}"""


def _later_units_text(units: Mapping[int, str], checkpoint: int) -> str:
    parts = []
    for uid in sorted(units):
        if uid > checkpoint:
            parts.append(units[uid])
    return "\n\n".join(parts)


def freeze_and_check_all_generations() -> dict:
    """对全部 12 份两臂生成产物构建正式冻结记录并执行隔离检查。"""
    summary_records = []
    all_ok = True

    for w in WORKS:
        units = load_units(w)
        for p in PREFIXES:
            checkpoint = CHECKPOINTS[p]
            source = render_source_units(units, checkpoint)
            later_text = _later_units_text(units, checkpoint)
            prefix_unit_ids = [u for u in sorted(units) if u <= checkpoint]
            prefix_sha = _sha(source)

            for arm in ARMS:
                gen_path = PROP / f"generation-{w}-{p}-arm_{arm.lower()}.json"
                if not gen_path.is_file():
                    raise FileNotFoundError(f"生成产物缺失：{gen_path}")
                gen_doc = json.loads(gen_path.read_text(encoding="utf-8"))
                if not gen_doc.get("complete") or not gen_doc.get("parse_ok"):
                    raise ValueError(f"生成产物未完成或解析失败：{gen_path}")

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
                            "visible_prefix_id": p,
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
                        "source_prefix_id": p,
                        "content": source,
                    },
                    {
                        "kind": "summary",
                        "used": False,
                        "sha256": "unused",
                        "chars": 0,
                        "source_prefix_id": p,
                    },
                    {
                        "kind": "knowledge",
                        "used": False,
                        "sha256": "unused",
                        "chars": 0,
                        "source_prefix_id": p,
                    },
                    {
                        "kind": "retrieval",
                        "used": False,
                        "sha256": "unused",
                        "chars": 0,
                        "source_prefix_id": p,
                    },
                ]

                # 落盘记录不重复冗余存储正文字符串，隔离核验时传入完整 materials_with_content
                materials_meta = [
                    {k: v for k, v in m.items() if k != "content"}
                    for m in materials_with_content
                ]

                freeze_rec = build_freeze_record(
                    work_id=w,
                    prefix_id=p,
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
                    rules_version="registration_anchor_ab_v1",
                    prompt_template_version="common-user-template_v1",
                    generator_model=str(gen_doc.get("model") or ""),
                    code_version="bc_t4_run_arm_generation_v1",
                    run_id=f"{w}__{p}__arm_{arm.lower()}",
                    arm=arm,
                )

                iso = check_isolation(
                    record=freeze_rec,
                    materials=materials_with_content,
                    prefix_text=source,
                    later_text=later_text,
                )
                if not iso["ok"]:
                    all_ok = False

                freeze_path = PROP / f"freeze-generation-{w}-{p}-arm_{arm.lower()}.json"
                created = save_freeze(freeze_path, freeze_rec)
                summary_records.append(
                    {
                        "work_id": w,
                        "prefix_id": p,
                        "arm": arm,
                        "freeze_file": str(freeze_path.relative_to(ROOT)),
                        "created_this_run": created,
                        "prefix_sha256": prefix_sha,
                        "request_sha256": gen_doc.get("request_sha256"),
                        "item_count": len(host_items),
                        "valid_item_count": sum(1 for h in host_items if h.get("ok")),
                        "isolation_ok": iso["ok"],
                        "isolation_problems": iso["problems"],
                        "declared_unused_kinds": iso["declared_unused_kinds"],
                    }
                )

    report = {
        "schema": "generation_freeze_isolation_summary_v1",
        "all_ok": all_ok,
        "total_generations": len(summary_records),
        "records": summary_records,
        "checked_at": datetime.now().isoformat(),
    }
    out_path = PROP / "generation-freeze-isolation-check.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def build_blind_review_package(
    work_id: str,
    prefix_id: str,
    *,
    model: str = "cn:deepseek-v4.1-flash",
) -> dict:
    """构建单个前缀的去标识混排盲审包并落盘。"""
    if prefix_id not in CHECKPOINTS:
        raise ValueError(f"未知前缀：{prefix_id}")
    checkpoint = CHECKPOINTS[prefix_id]
    units = load_units(work_id)
    source = render_source_units(units, checkpoint)

    elig_path = PROP / f"reference-eligible-{work_id}-{prefix_id}.json"
    if not elig_path.is_file():
        raise FileNotFoundError(f"合格参考集缺失：{elig_path}")
    elig_doc = json.loads(elig_path.read_text(encoding="utf-8"))
    if not elig_doc.get("frozen") or int(elig_doc.get("pending_count", 1)) != 0:
        raise ValueError(f"合格参考集未完成冻结：{elig_path}")

    gen_a_path = PROP / f"generation-{work_id}-{prefix_id}-arm_a.json"
    gen_b_path = PROP / f"generation-{work_id}-{prefix_id}-arm_b.json"
    gen_a = json.loads(gen_a_path.read_text(encoding="utf-8"))
    gen_b = json.loads(gen_b_path.read_text(encoding="utf-8"))
    for arm_label, gdoc, gpath in (("A", gen_a, gen_a_path), ("B", gen_b, gen_b_path)):
        if not gdoc.get("complete") or not gdoc.get("parse_ok"):
            raise ValueError(f"{arm_label} 臂生成产物未完成或解析失败：{gpath}")

    raw_pool = []
    for arm_label, gdoc in (("A", gen_a), ("B", gen_b)):
        for h in gdoc.get("host_items") or []:
            raw_pool.append(
                {
                    "arm": arm_label,
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

    # 使用确定性种子混排，确保可复现且隐藏 A/B 顺序
    seed_hex = _sha(f"blind_review_v1:{work_id}:{prefix_id}")
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
        "schema": "registration_blind_package_v1",
        "work_id": work_id,
        "prefix_id": prefix_id,
        "visible_range_end": checkpoint,
        "eligible_freeze_sha256": elig_doc.get("freeze_sha256"),
        "generation_a_request_sha256": gen_a.get("request_sha256"),
        "generation_b_request_sha256": gen_b.get("request_sha256"),
        "candidate_count": len(blind_candidates),
        "arm_a_count": len(gen_a.get("host_items") or []),
        "arm_b_count": len(gen_b.get("host_items") or []),
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
    pkg_path = PROP / f"blind-package-{work_id}-{prefix_id}.json"
    pkg_path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    return pkg


def parse_blind_review_output(
    raw_text: str,
    *,
    expected_cand_ids: Sequence[str],
    expected_ref_ids: Sequence[str],
) -> dict:
    """解析并严格校验盲审模型输出。"""
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        obj = json.loads(cleaned)
    except Exception as exc:  # noqa: BLE001
        return {
            "parse_ok": False,
            "complete": False,
            "reason": f"json_error:{exc}",
            "errors": [f"json_error:{exc}"],
            "candidate_reviews": [],
            "ref_matches": [],
        }

    if not isinstance(obj, dict):
        return {
            "parse_ok": False,
            "complete": False,
            "reason": "root_not_object",
            "errors": ["root_not_object"],
            "candidate_reviews": [],
            "ref_matches": [],
        }

    errors: list[str] = []
    complete = obj.get("complete") is True
    if not complete:
        errors.append("complete_not_true")

    raw_cands = obj.get("candidate_reviews")
    if not isinstance(raw_cands, list):
        errors.append("candidate_reviews_not_list")
        raw_cands = []

    raw_refs = obj.get("ref_matches")
    if not isinstance(raw_refs, list):
        errors.append("ref_matches_not_list")
        raw_refs = []

    expected_cand_set = set(expected_cand_ids)
    expected_ref_set = set(expected_ref_ids)

    cand_by_id: dict[str, dict] = {}
    for i, c in enumerate(raw_cands):
        if not isinstance(c, Mapping):
            errors.append(f"cand_{i}_not_object")
            continue
        cid = str(c.get("cand_id") or "")
        if cid not in expected_cand_set:
            errors.append(f"unknown_cand_id:{cid}")
            continue
        if cid in cand_by_id:
            errors.append(f"duplicate_cand_id:{cid}")
            continue
        label = str(c.get("label") or "")
        if label not in METRIC_ITEM_LABELS:
            errors.append(f"invalid_label:{cid}:{label}")
        matched_refs = c.get("matched_ref_ids")
        if not isinstance(matched_refs, list):
            matched_refs = []
        clean_matched_refs = []
        for r in matched_refs:
            rs = str(r)
            if rs not in expected_ref_set:
                errors.append(f"cand_matched_unknown_ref:{cid}:{rs}")
            else:
                clean_matched_refs.append(rs)
        cand_by_id[cid] = {
            "cand_id": cid,
            "label": label,
            "anchor_sufficient_at_quote": (
                bool(c.get("anchor_sufficient_at_quote"))
                if isinstance(c.get("anchor_sufficient_at_quote"), bool)
                else None
            ),
            "matched_ref_ids": sorted(set(clean_matched_refs)),
            "question_cluster_id": str(c.get("question_cluster_id") or cid),
            "reason": str(c.get("reason") or "").strip(),
        }

    missing_cands = sorted(expected_cand_set - set(cand_by_id))
    if missing_cands:
        errors.append("missing_cand_ids:" + ",".join(missing_cands))

    ref_by_id: dict[str, dict] = {}
    for i, r in enumerate(raw_refs):
        if not isinstance(r, Mapping):
            errors.append(f"ref_{i}_not_object")
            continue
        rid = str(r.get("ref_id") or "")
        if rid not in expected_ref_set:
            errors.append(f"unknown_ref_id:{rid}")
            continue
        if rid in ref_by_id:
            errors.append(f"duplicate_ref_id:{rid}")
            continue
        status = str(r.get("status") or "")
        if status not in ("matched", "missed_by_all", "uncertain"):
            errors.append(f"invalid_ref_status:{rid}:{status}")
        mcands = r.get("matched_cand_ids")
        if not isinstance(mcands, list):
            mcands = []
        clean_mcands = []
        for cid in mcands:
            cs = str(cid)
            if cs not in expected_cand_set:
                errors.append(f"ref_matched_unknown_cand:{rid}:{cs}")
            else:
                clean_mcands.append(cs)
        ref_by_id[rid] = {
            "ref_id": rid,
            "status": status,
            "matched_cand_ids": sorted(set(clean_mcands)),
            "reason": str(r.get("reason") or "").strip(),
        }

    missing_refs = sorted(expected_ref_set - set(ref_by_id))
    if missing_refs:
        errors.append("missing_ref_ids:" + ",".join(missing_refs))

    return {
        "parse_ok": not errors,
        "complete": complete and not errors,
        "reason": ";".join(errors) if errors else None,
        "errors": errors,
        "candidate_reviews": [cand_by_id[cid] for cid in expected_cand_ids if cid in cand_by_id],
        "ref_matches": [ref_by_id[rid] for rid in expected_ref_ids if rid in ref_by_id],
    }


def unblind_and_score_prefix(
    *,
    pkg: Mapping[str, Any],
    elig_doc: Mapping[str, Any],
    parsed_review: Mapping[str, Any],
) -> dict:
    """解盲并将盲审结果连接到 match_against_arms 与 compute_metrics。"""
    blind_key: Mapping[str, dict] = pkg["blind_key"]
    cand_reviews = {c["cand_id"]: c for c in parsed_review["candidate_reviews"]}
    ref_matches = {r["ref_id"]: r for r in parsed_review["ref_matches"]}
    eligible_ref_ids = [str(r) for r in (elig_doc.get("eligible_ref_ids") or [])]

    arm_system_items: dict[str, list[dict]] = {"A": [], "B": []}
    arm_adjudicated_items: dict[str, list[dict]] = {"A": [], "B": []}
    unblinded_candidates: list[dict] = []

    for cid, meta in blind_key.items():
        arm = meta["arm"]
        item_id = meta["item_id"]
        crev = cand_reviews.get(cid, {})
        arm_system_items[arm].append({"item_id": item_id, "question": meta["question"]})
        arm_adjudicated_items[arm].append(
            {
                "item_id": item_id,
                "label": crev.get("label", ""),
                "span_located": bool(meta.get("host_span_located")),
                "disputed": False,
            }
        )
        unblinded_candidates.append(
            {
                "cand_id": cid,
                "arm": arm,
                "item_id": item_id,
                "question": meta["question"],
                "answer_criteria": meta["answer_criteria"],
                "source_unit_id": meta["source_unit_id"],
                "anchor_quote": meta["anchor_quote"],
                "host_span_located": bool(meta.get("host_span_located")),
                "citation_start": meta["citation_start"],
                "citation_end": meta["citation_end"],
                "label": crev.get("label"),
                "anchor_sufficient_at_quote": (
                    False
                    if not meta.get("host_span_located")
                    else crev.get("anchor_sufficient_at_quote")
                ),
                "matched_ref_ids": crev.get("matched_ref_ids") or [],
                "question_cluster_id": crev.get("question_cluster_id") or cid,
                "reason": crev.get("reason") or "",
            }
        )

    # 按臂构造配对记录：结合 candidate_reviews.matched_ref_ids 与 ref_matches.matched_cand_ids
    # 若双向不一致（一边认为该臂匹配、另一边认为未匹配），注入冲突对以触发 match_against_arms 的 unresolved 保护！
    arm_pairs: dict[str, list[dict]] = {"A": [], "B": []}
    bidirectional_notes: list[dict] = []

    for rid in eligible_ref_ids:
        rm = ref_matches.get(rid, {})
        r_status = rm.get("status")
        from_ref_cands = set(rm.get("matched_cand_ids") or [])
        from_cand_cands = {
            cid
            for cid, crev in cand_reviews.items()
            if rid in (crev.get("matched_ref_ids") or [])
        }
        if from_ref_cands != from_cand_cands:
            bidirectional_notes.append(
                {
                    "ref_id": rid,
                    "from_ref_matches": sorted(from_ref_cands),
                    "from_candidate_reviews": sorted(from_cand_cands),
                }
            )

        for arm in ("A", "B"):
            arm_cands_from_ref = sorted(
                cid for cid in from_ref_cands if blind_key[cid]["arm"] == arm
            )
            arm_cands_from_cand = sorted(
                cid for cid in from_cand_cands if blind_key[cid]["arm"] == arm
            )

            if r_status == "uncertain":
                arm_pairs[arm].append({"ref_id": rid, "matched": None})
                continue

            # 检查该臂下双向结果是否对"是否命中"存在分歧
            has_ref = bool(arm_cands_from_ref)
            has_cand = bool(arm_cands_from_cand)
            if has_ref != has_cand:
                # 双向存在臂级冲突：故意写入两条冲突记录，令 match_against_arms 判为 unresolved（配对记录冲突）
                chosen_cid = (arm_cands_from_ref or arm_cands_from_cand)[0]
                arm_pairs[arm].append(
                    {
                        "ref_id": rid,
                        "matched": True,
                        "system_item_id": blind_key[chosen_cid]["item_id"],
                    }
                )
                arm_pairs[arm].append({"ref_id": rid, "matched": False})
            elif has_ref and has_cand:
                # 双方均确认该臂命中：取交集优先，绑定首个真实 system_item_id
                common_cands = sorted(set(arm_cands_from_ref) & set(arm_cands_from_cand))
                chosen_cid = (common_cands or arm_cands_from_ref)[0]
                arm_pairs[arm].append(
                    {
                        "ref_id": rid,
                        "matched": True,
                        "system_item_id": blind_key[chosen_cid]["item_id"],
                        "all_matched_item_ids": [
                            blind_key[c]["item_id"]
                            for c in sorted(set(arm_cands_from_ref) | set(arm_cands_from_cand))
                        ],
                    }
                )
            else:
                if r_status == "matched" and not from_ref_cands and not from_cand_cands:
                    # 声称 matched 但未给出任何候选 -> unresolved
                    arm_pairs[arm].append({"ref_id": rid, "matched": None})
                else:
                    arm_pairs[arm].append({"ref_id": rid, "matched": False})

    # 检查是否存在单个 system_item_id 同时被绑定到多条独立 eligible_ref_ids（复合提问一对多）
    multi_ref_bindings: dict[str, list[dict]] = {"A": [], "B": []}
    strict_1to1_arm_pairs: dict[str, list[dict]] = {"A": [], "B": []}
    for arm in ("A", "B"):
        used_sys_ids: dict[str, str] = {}
        for p_rec in arm_pairs[arm]:
            if p_rec.get("matched") is True:
                sid = str(p_rec.get("system_item_id") or "")
                all_sids = p_rec.get("all_matched_item_ids") or ([sid] if sid else [])
                # 优先寻找尚未被其他 ref_id 占用的 system_item_id
                free_sid = next((s for s in all_sids if s not in used_sys_ids), None)
                if free_sid is not None:
                    used_sys_ids[free_sid] = p_rec["ref_id"]
                    strict_1to1_arm_pairs[arm].append(
                        {
                            "ref_id": p_rec["ref_id"],
                            "matched": True,
                            "system_item_id": free_sid,
                        }
                    )
                else:
                    # 该臂同一条复合候选被用于覆盖第 2 个独立参考问题
                    multi_ref_bindings[arm].append(
                        {
                            "ref_id": p_rec["ref_id"],
                            "shared_system_item_id": sid,
                            "already_bound_to_ref_id": used_sys_ids.get(sid),
                        }
                    )
                    # 在严格一对一口径下，不重复奖励同一 system_item_id，亦不讹传为漏登记，记为 matched=None (unresolved)
                    strict_1to1_arm_pairs[arm].append(
                        {
                            "ref_id": p_rec["ref_id"],
                            "matched": None,
                        }
                    )
            else:
                strict_1to1_arm_pairs[arm].append(dict(p_rec))

    per_arm_matching = match_against_arms(
        elig_doc,
        arm_pairs=arm_pairs,
        arm_system_items=arm_system_items,
    )
    per_arm_matching_strict_1to1 = match_against_arms(
        elig_doc,
        arm_pairs=strict_1to1_arm_pairs,
        arm_system_items=arm_system_items,
    )

    per_arm_metrics = {}
    per_arm_metrics_strict_1to1 = {}
    for arm in ("A", "B"):
        per_arm_metrics[arm] = compute_metrics(
            system_items=arm_system_items[arm],
            adjudicated_items=arm_adjudicated_items[arm],
            independent_qualified=to_metric_inputs(elig_doc, per_arm_matching, arm),
        )
        per_arm_metrics_strict_1to1[arm] = compute_metrics(
            system_items=arm_system_items[arm],
            adjudicated_items=arm_adjudicated_items[arm],
            independent_qualified=to_metric_inputs(elig_doc, per_arm_matching_strict_1to1, arm),
        )

    # 提取两臂同题配对子集与仅一臂提出的问题（基于 question_cluster_id 与 matched_ref_ids 联合归一）
    clusters: dict[str, dict[str, list[dict]]] = {}
    for uc in unblinded_candidates:
        # 若匹配了参考集，优先以参考集 ref_id 集合作为簇键，否则用模型给出的 question_cluster_id
        if uc["matched_ref_ids"]:
            ckey = "ref:" + "+".join(sorted(uc["matched_ref_ids"]))
        else:
            ckey = "cluster:" + str(uc["question_cluster_id"])
        clusters.setdefault(ckey, {"A": [], "B": []})[uc["arm"]].append(uc)

    paired_questions = []
    arm_only_questions = {"A": [], "B": []}
    checkpoint = int(pkg["visible_range_end"])

    for ckey, by_arm in sorted(clusters.items()):
        a_list = by_arm["A"]
        b_list = by_arm["B"]
        if a_list and b_list:
            a0 = a_list[0]
            b0 = b_list[0]
            same_unit = (
                a0["host_span_located"]
                and b0["host_span_located"]
                and a0["source_unit_id"] == b0["source_unit_id"]
            )
            char_diff_b_minus_a = (
                int(b0["citation_start"]) - int(a0["citation_start"])
                if same_unit
                and isinstance(a0["citation_start"], int)
                and isinstance(b0["citation_start"], int)
                else None
            )
            unit_diff_b_minus_a = (
                int(b0["source_unit_id"]) - int(a0["source_unit_id"])
                if a0["host_span_located"]
                and b0["host_span_located"]
                and isinstance(a0["source_unit_id"], int)
                and isinstance(b0["source_unit_id"], int)
                else None
            )
            paired_questions.append(
                {
                    "cluster_key": ckey,
                    "matched_ref_ids": sorted(
                        set(a0["matched_ref_ids"]) | set(b0["matched_ref_ids"])
                    ),
                    "registration_checkpoint": checkpoint,
                    "arm_a": {
                        "item_id": a0["item_id"],
                        "question": a0["question"],
                        "label": a0["label"],
                        "anchor_sufficient_at_quote": a0["anchor_sufficient_at_quote"],
                        "source_unit_id": a0["source_unit_id"],
                        "citation_start": a0["citation_start"],
                        "citation_end": a0["citation_end"],
                        "anchor_quote": a0["anchor_quote"],
                    },
                    "arm_b": {
                        "item_id": b0["item_id"],
                        "question": b0["question"],
                        "label": b0["label"],
                        "anchor_sufficient_at_quote": b0["anchor_sufficient_at_quote"],
                        "source_unit_id": b0["source_unit_id"],
                        "citation_start": b0["citation_start"],
                        "citation_end": b0["citation_end"],
                        "anchor_quote": b0["anchor_quote"],
                    },
                    "unit_diff_b_minus_a": unit_diff_b_minus_a,
                    "same_unit_char_diff_b_minus_a": char_diff_b_minus_a,
                }
            )
        elif a_list:
            for item in a_list:
                arm_only_questions["A"].append(item)
        elif b_list:
            for item in b_list:
                arm_only_questions["B"].append(item)

    return {
        "unblinded_candidates": unblinded_candidates,
        "bidirectional_notes": bidirectional_notes,
        "multi_ref_bindings": multi_ref_bindings,
        "arm_pairs": arm_pairs,
        "strict_1to1_arm_pairs": strict_1to1_arm_pairs,
        "per_arm_matching": per_arm_matching,
        "per_arm_matching_strict_1to1": per_arm_matching_strict_1to1,
        "per_arm_metrics": per_arm_metrics,
        "per_arm_metrics_strict_1to1": per_arm_metrics_strict_1to1,
        "paired_questions": paired_questions,
        "arm_only_questions": arm_only_questions,
    }


async def run_single_blind_review(
    *,
    work_id: str,
    prefix_id: str,
    model: str = "cn:deepseek-v4.1-flash",
    temperature: float = 0.1,
    max_output_tokens: int = 2000,
) -> dict:
    pkg = build_blind_review_package(work_id, prefix_id, model=model)
    elig_path = PROP / f"reference-eligible-{work_id}-{prefix_id}.json"
    elig_doc = json.loads(elig_path.read_text(encoding="utf-8"))

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
        "schema": "registration_blind_review_v1",
        "work_id": work_id,
        "prefix_id": prefix_id,
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
    out_path = PROP / f"blind-review-{work_id}-{prefix_id}.json"
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


def build_overall_summary() -> dict:
    """汇总全部 6 个前缀的盲审结果、分臂指标、配对同题子集与台账。"""
    per_prefix: dict[str, Any] = {}
    agg_system_items: dict[str, list[dict]] = {"A": [], "B": []}
    agg_adjudicated_items: dict[str, list[dict]] = {"A": [], "B": []}
    agg_independent_qualified: dict[str, list[dict]] = {"A": [], "B": []}
    agg_independent_qualified_strict: dict[str, list[dict]] = {"A": [], "B": []}

    all_paired_questions: list[dict] = []
    all_arm_only_counts = {
        "A": {"total": 0, "有依据": 0, "无依据": 0, "已经回答": 0, "无法确定": 0},
        "B": {"total": 0, "有依据": 0, "无依据": 0, "已经回答": 0, "无法确定": 0},
    }
    anchor_sufficiency_by_arm = {
        "A": {"total_located": 0, "sufficient": 0, "insufficient": 0},
        "B": {"total_located": 0, "sufficient": 0, "insufficient": 0},
    }

    for w in WORKS:
        for p in PREFIXES:
            key = f"{w}/{p}"
            rev_path = PROP / f"blind-review-{w}-{p}.json"
            if not rev_path.is_file():
                raise FileNotFoundError(f"盲审结果文件缺失：{rev_path}")
            rev_doc = json.loads(rev_path.read_text(encoding="utf-8"))
            if not rev_doc.get("complete") or not rev_doc.get("parse_ok"):
                raise ValueError(f"盲审结果未完成或校验未通过：{rev_path}")

            elig_doc = json.loads(
                (PROP / f"reference-eligible-{w}-{p}.json").read_text(encoding="utf-8")
            )
            unb = rev_doc["unblinded"]
            per_prefix[key] = {
                "per_arm_matching": unb["per_arm_matching"],
                "per_arm_matching_strict_1to1": unb["per_arm_matching_strict_1to1"],
                "per_arm_metrics": unb["per_arm_metrics"],
                "per_arm_metrics_strict_1to1": unb["per_arm_metrics_strict_1to1"],
                "paired_question_count": len(unb["paired_questions"]),
                "arm_only_counts": {
                    "A": len(unb["arm_only_questions"]["A"]),
                    "B": len(unb["arm_only_questions"]["B"]),
                },
                "multi_ref_bindings": unb["multi_ref_bindings"],
                "bidirectional_notes": unb["bidirectional_notes"],
            }

            for uc in unb["unblinded_candidates"]:
                arm = uc["arm"]
                global_item_id = f"{w}__{p}__{arm}__{uc['item_id']}"
                agg_system_items[arm].append({"item_id": global_item_id})
                agg_adjudicated_items[arm].append(
                    {
                        "item_id": global_item_id,
                        "label": uc["label"],
                        "span_located": bool(uc["host_span_located"]),
                        "disputed": False,
                    }
                )
                if uc["host_span_located"]:
                    anchor_sufficiency_by_arm[arm]["total_located"] += 1
                    if uc["anchor_sufficient_at_quote"] is True:
                        anchor_sufficiency_by_arm[arm]["sufficient"] += 1
                    else:
                        anchor_sufficiency_by_arm[arm]["insufficient"] += 1

            for arm in ("A", "B"):
                for q_in in to_metric_inputs(elig_doc, unb["per_arm_matching"], arm):
                    agg_independent_qualified[arm].append(
                        {
                            "question_id": f"{w}__{p}__{q_in['question_id']}",
                            "registered": q_in["registered"],
                            "pairing_status": q_in["pairing_status"],
                        }
                    )
                for q_in in to_metric_inputs(elig_doc, unb["per_arm_matching_strict_1to1"], arm):
                    agg_independent_qualified_strict[arm].append(
                        {
                            "question_id": f"{w}__{p}__{q_in['question_id']}",
                            "registered": q_in["registered"],
                            "pairing_status": q_in["pairing_status"],
                        }
                    )
                for ao in unb["arm_only_questions"][arm]:
                    all_arm_only_counts[arm]["total"] += 1
                    lbl = str(ao.get("label") or "")
                    if lbl in all_arm_only_counts[arm]:
                        all_arm_only_counts[arm][lbl] += 1

            for pq in unb["paired_questions"]:
                all_paired_questions.append({"work_id": w, "prefix_id": p, **pq})

    overall_metrics = {}
    overall_metrics_strict_1to1 = {}
    for arm in ("A", "B"):
        overall_metrics[arm] = compute_metrics(
            system_items=agg_system_items[arm],
            adjudicated_items=agg_adjudicated_items[arm],
            independent_qualified=agg_independent_qualified[arm],
        )
        overall_metrics_strict_1to1[arm] = compute_metrics(
            system_items=agg_system_items[arm],
            adjudicated_items=agg_adjudicated_items[arm],
            independent_qualified=agg_independent_qualified_strict[arm],
        )

    # 汇总同题配对子集的 B-A 对比
    same_unit_diffs = [
        pq["same_unit_char_diff_b_minus_a"]
        for pq in all_paired_questions
        if isinstance(pq.get("same_unit_char_diff_b_minus_a"), int)
    ]
    unit_diffs = [
        pq["unit_diff_b_minus_a"]
        for pq in all_paired_questions
        if isinstance(pq.get("unit_diff_b_minus_a"), int)
    ]
    paired_summary = {
        "paired_count": len(all_paired_questions),
        "arm_a_anchor_sufficient_count": sum(
            1 for pq in all_paired_questions if pq["arm_a"]["anchor_sufficient_at_quote"] is True
        ),
        "arm_b_anchor_sufficient_count": sum(
            1 for pq in all_paired_questions if pq["arm_b"]["anchor_sufficient_at_quote"] is True
        ),
        "same_unit_pair_count": len(same_unit_diffs),
        "same_unit_b_earlier_count": sum(1 for d in same_unit_diffs if d < 0),
        "same_unit_same_pos_count": sum(1 for d in same_unit_diffs if d == 0),
        "same_unit_b_later_count": sum(1 for d in same_unit_diffs if d > 0),
        "cross_or_same_unit_b_earlier_unit_count": sum(1 for d in unit_diffs if d < 0),
        "cross_or_same_unit_same_unit_count": sum(1 for d in unit_diffs if d == 0),
        "cross_or_same_unit_b_later_unit_count": sum(1 for d in unit_diffs if d > 0),
        "pairs": all_paired_questions,
    }

    archive = UsageArchive.load(PROP / "reference-discovery-usage.json")
    summary_doc = {
        "schema": "registration_pilot_metrics_summary_v1",
        "overall_metrics_by_arm": overall_metrics,
        "overall_metrics_strict_1to1_by_arm": overall_metrics_strict_1to1,
        "anchor_sufficiency_all_located_by_arm": anchor_sufficiency_by_arm,
        "paired_same_question_comparison": paired_summary,
        "arm_only_questions_summary": all_arm_only_counts,
        "per_prefix": per_prefix,
        "usage_summary": archive.summary(),
        "generated_at": datetime.now().isoformat(),
    }
    out_path = PROP / "pilot-metrics-summary.json"
    out_path.write_text(json.dumps(summary_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary_doc


async def run_all_blind_reviews(
    *,
    model: str = "cn:deepseek-v4.1-flash",
    temperature: float = 0.1,
    max_output_tokens: int = 2000,
    skip_existing: bool = True,
) -> dict:
    # 0. 先执行 12 份两臂生成的冻结与隔离核验
    freeze_report = freeze_and_check_all_generations()
    if not freeze_report["all_ok"]:
        raise ValueError(f"两臂生成产物隔离检查未通过：{freeze_report}")

    sequence = [(w, p) for w in WORKS for p in PREFIXES]
    results = []
    for idx, (w, p) in enumerate(sequence, start=1):
        out_path = PROP / f"blind-review-{w}-{p}.json"
        if skip_existing and out_path.is_file():
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if existing.get("complete") is True and existing.get("parse_ok") is True:
                print(f"[{idx}/6] 跳过已完成盲审: work={w}, prefix={p}")
                results.append(
                    {
                        "ok": True,
                        "skipped": True,
                        "saved": str(out_path.relative_to(ROOT)),
                        "work_id": w,
                        "prefix_id": p,
                        "complete": True,
                    }
                )
                continue
        print(f"[{idx}/6] 启动盲审复核: work={w}, prefix={p}, model={model}...")
        res = await run_single_blind_review(
            work_id=w,
            prefix_id=p,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        results.append(res)
        if not res.get("ok"):
            print(f"盲审在第 {idx} 次调用中止：{res}")
            return {"ok": False, "results": results}
        print(
            f"  -> 完成: candidates={res.get('candidate_count')}, "
            f"refs={res.get('eligible_ref_count')}, tokens={res.get('tokens')}"
        )

    summary = build_overall_summary()
    return {
        "ok": True,
        "results": results,
        "summary_saved": "docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal/pilot-metrics-summary.json",
        "usage_summary": summary["usage_summary"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work")
    ap.add_argument("--prefix")
    ap.add_argument(
        "--prepare-only",
        action="store_true",
        help="仅离线执行 12 份两臂生成冻结隔离检查并生成 6 份去标识盲审包（不调用模型）",
    )
    ap.add_argument(
        "--summarize-only",
        action="store_true",
        help="仅根据已落盘的 6 份盲审结果离线汇总指标（不调用模型）",
    )
    ap.add_argument("--run-all", action="store_true", help="按顺序运行全部 6 个前缀的盲审复核并汇总指标")
    ap.add_argument("--model", default="cn:deepseek-v4.1-flash")
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--max-output-tokens", type=int, default=2000)
    args = ap.parse_args()

    if args.prepare_only:
        freeze_rep = freeze_and_check_all_generations()
        pkgs = []
        for w in WORKS:
            for p in PREFIXES:
                pkg = build_blind_review_package(w, p, model=args.model)
                pkgs.append(
                    {
                        "work_id": w,
                        "prefix_id": p,
                        "candidate_count": pkg["candidate_count"],
                        "arm_a_count": pkg["arm_a_count"],
                        "arm_b_count": pkg["arm_b_count"],
                        "eligible_ref_count": pkg["eligible_ref_count"],
                        "projected_input_tokens": pkg["projected_input_tokens"],
                        "request_sha256": pkg["request_sha256"],
                    }
                )
        print(
            json.dumps(
                {
                    "freeze_isolation_all_ok": freeze_rep["all_ok"],
                    "total_generations_checked": freeze_rep["total_generations"],
                    "blind_packages": pkgs,
                    "total_projected_input_tokens": sum(
                        x["projected_input_tokens"] for x in pkgs
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not freeze_rep["all_ok"]:
            sys.exit(1)
        return

    if args.summarize_only:
        summary = build_overall_summary()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    if args.run_all:
        out = asyncio.run(
            run_all_blind_reviews(
                model=args.model,
                temperature=args.temperature,
                max_output_tokens=args.max_output_tokens,
            )
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if not out.get("ok"):
            sys.exit(1)
        return

    if not (args.work and args.prefix):
        ap.error("请指定 --work 与 --prefix，或使用 --prepare-only / --run-all / --summarize-only")

    res = asyncio.run(
        run_single_blind_review(
            work_id=args.work,
            prefix_id=args.prefix,
            model=args.model,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
        )
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
