"""把首轮小试的流程用**模拟响应**串起来跑通（不调用模型）。

流程：两份独立参考发现 → 合并去重、保留争议 → 冻结原始标注 →
资格裁决 → 冻结计分用合格参考集 → 运行两臂 → 盲审匹配（绑定实际系统条目）
→ 分臂指标 → 用量归档。

配对结果**必须绑定真实存在的 system_item_id**——不能只靠手填布尔值制造指标。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.common.registration_eval_metrics import compute_metrics  # noqa: E402
from backend.common.registration_reference_set import (  # noqa: E402
    PROTOCOL_VERSION,
    apply_qualification,
    freeze_reference_set,
    match_against_arms,
    merge_annotations,
    save_reference_set,
    to_metric_inputs,
    validate_annotation,
)
from backend.common.registration_usage_archive import (  # noqa: E402
    STAGE_BLIND_REVIEW,
    STAGE_GENERATION,
    STAGE_REFERENCE,
    UsageArchive,
)

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal"

WORK = "w_sim"
PREFIX = "p_sim"


def _ann(annotator: str, items: list[dict]) -> dict:
    return {
        "work_id": WORK,
        "prefix_id": PREFIX,
        "annotator_id": annotator,
        "protocol_version": PROTOCOL_VERSION,
        "scope_read": {"unit_ids": [1, 2, 3], "units_sha256": "u1"},
        "completed": True,
        "completed_at": "2026-11-07T00:00:00",
        "freeze_sha256": "fp1",
        "items": items,
    }


def _item(q: str, label: str = "有依据且未解", quote: str = "匾额上写着甲乙丙丁") -> dict:
    return {
        "question": q,
        "answer_criteria": "交代清楚",
        "supporting_quote": quote,
        "acceptable_spans": [{"unit_id": 1, "start": 10, "end": 18}],
        "label": label,
        "reason": "原文提出但尚未解释",
    }


def main() -> None:
    archive = UsageArchive(price_table={"sim-model": {"input_per_1k": 0.01, "output_per_1k": 0.02}})

    # --- 阶段一：两份独立参考发现（模拟） ---
    ann_a = _ann(
        "ref_1",
        [
            _item("匾额四方位分别指什么？"),
            _item("钥匙的来历是什么？", quote="那把钥匙的来历"),
        ],
    )
    ann_b = _ann(
        "ref_2",
        [
            _item("匾额四方位分别指什么？"),
            _item("石匣里是什么？", quote="他把令牌藏进石匣"),
        ],
    )
    for i, ann in enumerate((ann_a, ann_b), start=1):
        archive.record_attempt(
            work_id=WORK, prefix_id=PREFIX, arm="-", stage=STAGE_REFERENCE,
            request_sha256=f"req_ref_{i}", model="sim-model", input_tokens=1200,
            output_tokens=300, success=True, attempt=1,
        )
    va = validate_annotation(ann_a)
    vb = validate_annotation(ann_b)

    # --- 阶段二：合并去重、保留争议 ---
    merged = merge_annotations(ann_a, ann_b, work_id=WORK, prefix_id=PREFIX)

    # --- 阶段三：冻结原始标注 ---
    raw_frozen = freeze_reference_set(merged, annotator_ids=["ref_1", "ref_2"])
    ref_path = OUT / "sim-reference-set-raw.json"
    created_raw = save_reference_set(ref_path, raw_frozen)

    # --- 阶段四：资格裁决（模拟人工裁决）---
    ref_ids = [i["ref_id"] for i in raw_frozen["items"]]
    decisions = []
    for item in raw_frozen["items"]:
        if item["status"] == "agreed":
            # 同题同标签也**不自动**认为条件一致：仍需逐条确认条件与跨度
            decisions.append(
                {
                    "ref_id": item["ref_id"],
                    "qualified": True,
                    "qualified_label": "有依据且未解",
                    "accepted_answer_criteria": "交代四个方位的含义",
                    "accepted_spans": item["acceptable_spans"],
                    "reviewer_id": "human_1",
                    "reason": "前缀内已提出，条件已核对",
                }
            )
        else:
            # 争议与单方条目：未确认 → 不进合格集
            decisions.append(
                {
                    "ref_id": item["ref_id"],
                    "qualified": False,
                    "qualified_label": item["label"] if item["status"] == "disputed" else None,
                    "reviewer_id": "human_1",
                    "reason": "双方判断不一致，暂不确认" if item["status"] == "disputed"
                    else "仅一方提到，待独立复核",
                }
            )
    eligible = apply_qualification(raw_frozen, decisions, reviewer_id="human_1")
    elig_path = OUT / "sim-reference-set-eligible.json"
    created_elig = save_reference_set(elig_path, eligible)

    # --- 阶段五：运行两臂（模拟输出）---
    arm_items = {
        "A": [
            {"item_id": "A0", "question": "匾额四方位分别指什么？"},
            {"item_id": "A1", "question": "钥匙的来历是什么？"},
        ],
        "B": [
            {"item_id": "B0", "question": "匾额四方位分别指什么？"},
        ],
    }
    for arm, items in arm_items.items():
        archive.record_attempt(
            work_id=WORK, prefix_id=PREFIX, arm=arm, stage=STAGE_GENERATION,
            request_sha256=f"req_gen_{arm}", model="sim-model", input_tokens=2000,
            output_tokens=400, success=True, attempt=1,
        )
    archive.record_attempt(
        work_id=WORK, prefix_id=PREFIX, arm="B", stage=STAGE_GENERATION,
        request_sha256="req_gen_B", model="sim-model", input_tokens=2000,
        output_tokens=0, success=False, error="timeout", attempt=2,
    )

    # --- 阶段六：盲审匹配（绑定真实 system_item_id）---
    arm_pairs = {
        "A": [
            {"ref_id": ref_ids[0], "matched": True, "system_item_id": "A0"},
            {"ref_id": ref_ids[1], "matched": False},
        ],
        "B": [
            {"ref_id": ref_ids[0], "matched": True, "system_item_id": "B0"},
            {"ref_id": ref_ids[1], "matched": True, "system_item_id": "B0"},
        ],
    }
    archive.record_attempt(
        work_id=WORK, prefix_id=PREFIX, arm="-", stage=STAGE_BLIND_REVIEW,
        request_sha256="req_blind", model="sim-model", input_tokens=900,
        output_tokens=200, success=True, attempt=1,
    )
    per_arm = match_against_arms(
        eligible, arm_pairs=arm_pairs, arm_system_items=arm_items
    )

    # --- 阶段七：分臂指标 ---
    metrics = {}
    for arm, items in arm_items.items():
        metrics[arm] = compute_metrics(
            system_items=[{"item_id": i["item_id"]} for i in items],
            adjudicated_items=[
                {"item_id": i["item_id"], "label": "有依据", "span_located": True}
                for i in items
            ],
            independent_qualified=to_metric_inputs(eligible, per_arm, arm),
        )

    out = {
        "note": "全流程用模拟响应跑通，验证流程与口径接得通；不产生质量结论",
        "reference_annotations": {
            "left_valid": va["ok"],
            "right_valid": vb["ok"],
            "merge_summary": merged["summary"],
            "raw_frozen_path": str(ref_path.relative_to(ROOT)),
            "raw_frozen_created_this_run": created_raw,
            "raw_freeze_sha256": raw_frozen["freeze_sha256"],
            "eligible_path": str(elig_path.relative_to(ROOT)),
            "eligible_created_this_run": created_elig,
            "eligible_freeze_sha256": eligible["freeze_sha256"],
            "eligible_count": eligible["eligible_count"],
            "reviewed_count": eligible["reviewed_count"],
            "pending_count": eligible["pending_count"],
            "excluded": eligible["excluded"],
        },
        "per_arm_matching": per_arm,
        "per_arm_metrics": {
            arm: {
                "missed_registration_rate": m["metrics"]["missed_registration_rate"],
                "denominators": m["denominators"],
                "coverage": m["coverage"],
                "excluded": m["excluded"],
            }
            for arm, m in metrics.items()
        },
        "usage_archive": archive.summary(),
    }
    p = OUT / "sim-end-to-end-check.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    archive.save(OUT / "sim-usage-archive.json")
    print("saved:", p)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
