"""离线验证运行器部件（不调用模型、不启动小试）。

用假模型走一遍完整流程，验证契约里的硬要求：
1. 前缀清单冻结：单元 ID、内容哈希、可见范围终点；
2. 模板装配确定性 + 逐请求哈希；
3. 宿主定位：唯一命中给跨度；歧义/越界/不存在**单列**，不默取第一个；
4. 输出截断 → 整次任务标 incomplete（不能当成"少登记"）；
5. token 预检与预算记账：重试也占预算，装不下就别发。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.experiments.bc.bc_t4_registration_pilot_runner import (  # noqa: E402
    Budget,
    build_prefix_manifest,
    convert_to_host_schema,
    estimate_tokens,
    locate_citation,
    parse_generator_output,
    render_source_units,
)

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal"

UNITS = {
    1: "第一章：他把令牌藏进石匣，匾额上写着甲乙丙丁。",
    2: "第二章：城主打开石匣，确认令牌确为真品。",
    3: "第三章：令牌后来被转手三次。",
    4: "第四章：众人议论那把钥匙的来历。钥匙的来历至今不明。",
}


def _ok(item: dict) -> dict:
    return {
        "complete": True,
        "foreshadowing": [item],
    }


def main() -> None:
    report: dict = {}

    # 1) 前缀清单冻结
    man = build_prefix_manifest(
        work_id="w1", prefix_id="p1", units=UNITS, checkpoint=2, registration_checkpoint=2
    )
    report["prefix_manifest"] = {
        "unit_count": man["unit_count"],
        "visible_unit_count": man["visible_unit_count"],
        "visible_range_end": man["visible_range_end"],
        "units_have_sha256": all(u.get("sha256") for u in man["units"]),
        "units_sha256_present": bool(man["units_sha256"]),
    }

    # 2) 渲染：单元标签不是原文的一部分
    rendered = render_source_units(UNITS, 2)
    report["render"] = {
        "contains_only_visible_units": "unit_id=3" not in rendered,
        "has_stable_ids": "[unit_id=1]" in rendered,
        "labels_not_in_unit_text": all(
            "[unit_id=" not in UNITS[u] for u in UNITS
        ),
    }

    # 3) 宿主定位
    loc_ok = locate_citation(
        units=UNITS, source_unit_id=2, anchor_quote="城主打开石匣，确认令牌确为真品。", checkpoint=2
    )
    loc_amb = locate_citation(
        units=UNITS, source_unit_id=4, anchor_quote="钥匙的来历", checkpoint=4
    )
    loc_oob = locate_citation(
        units=UNITS, source_unit_id=3, anchor_quote="令牌后来被转手三次。", checkpoint=2
    )
    loc_miss = locate_citation(
        units=UNITS, source_unit_id=1, anchor_quote="原文里没有这一句", checkpoint=2
    )
    report["locating"] = {
        "unique_hit": loc_ok,
        "ambiguous": {"ok": loc_amb["ok"], "reason": loc_amb["reason"]},
        "out_of_visible_range": {"ok": loc_oob["ok"], "reason": loc_oob["reason"]},
        "not_found": {"ok": loc_miss["ok"], "reason": loc_miss["reason"]},
    }

    # 4) 输出解析与截断
    good_raw = json.dumps(
        _ok(
            {
                "clue": "匾额四方位",
                "question": "匾额四方位分别指什么？",
                "answer_criteria": "交代四个方位的含义",
                "implication": "尚待交代之事",
                "motif_key": "方位",
                "source_unit_id": 1,
                "anchor_quote": "他把令牌藏进石匣，匾额上写着甲乙丙丁。",
            }
        ),
        ensure_ascii=False,
    )
    parsed = parse_generator_output(good_raw)
    rows = convert_to_host_schema(
        parsed,
        units=UNITS,
        checkpoint=2,
        prefix_id="p1",
        registration_checkpoint=2,
    )
    trunc_raw = json.dumps({"complete": False, "foreshadowing": []}, ensure_ascii=False)
    parsed_trunc = parse_generator_output(trunc_raw)
    bad_raw = "{not json"
    parsed_bad = parse_generator_output(bad_raw)
    report["parsing"] = {
        "good_parse_ok": parsed["parse_ok"],
        "host_rows": rows,
        "host_row_has_span": bool(rows and rows[0].get("citation_start") is not None),
        "truncated_complete_flag": parsed_trunc["complete"],
        "truncated_task_marked_incomplete": parsed_trunc["complete"] is False,
        "broken_json": {"parse_ok": parsed_bad["parse_ok"], "reason": parsed_bad["reason"]},
    }

    # 5) token 预检与预算
    b = Budget(max_attempts=42, max_input_tokens=336000, max_output_tokens=84000)
    small = b.precheck(estimate_tokens(good_raw), 2000)
    big = b.precheck(9000, 200)
    b.record(estimate_tokens(good_raw), 2000)
    exhausted = Budget(max_attempts=1)
    exhausted.record(10, 10)
    over = exhausted.precheck(10, 10)
    report["budget"] = {
        "small_request_ok": small["ok"],
        "oversize_input_blocked": not big["ok"],
        "oversize_reason": big["reason"],
        "attempts_counted_after_one_call": b.summary()["attempts"],
        "attempts_remaining": b.summary()["attempts_remaining"],
        "max_attempts_enforced": not over["ok"],
        "max_attempts_reason": over["reason"],
        "summary": b.summary(),
    }

    out = OUT / "runner-offline-verification.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", out)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
