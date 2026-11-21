"""T4 报告生成：把试产产物、评分结果与成本核算汇总成一份可核对的报告。

报告结构对应任务书 §六「冻结验收门槛」逐项给出：
  - 每个维度给门槛、实测值、达标与否
  - 逐作品分子分母
  - 争议项与未审核项单列
  - 样本量不足处标"证据不足"
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TRIAL_ROOT = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"


def load(name: str) -> dict:
    p = OUT_DIR / name
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _ordering_block() -> dict:
    """时序判定：按**裁决实际引用的句子**定位，同一坐标系、原文哈希绑定。

    判定结果只解除时间疑点；答案正确性、条件满足、信息增量仍由原门禁判断。
    """
    scan_p = OUT_DIR / "t4-evidence-ordering-cited-scan.json"
    replay_p = OUT_DIR / "t4-gate-replay-with-ordering.json"
    if not scan_p.is_file():
        return {"status": "未生成"}
    rows = json.loads(scan_p.read_text(encoding="utf-8"))
    dist: dict = {}
    for r in rows:
        dist[r["ordering"]["verdict"]] = dist.get(r["ordering"]["verdict"], 0) + 1
    block = {
        "method": (
            "比较的是**支撑答案的实际引文跨度**，不是检索窗口。"
            "窗口只当来源容器：裁决没引用的句子不参与判断，"
            "窗口包住锚点也不把窗口内部的句子一并豁免。"
            "锚点与引文都在同一份原文、同一套字符坐标下定位，原文按单元哈希绑定；"
            "定位必须唯一，引文重复、定位失败、跨度重叠一律记为时序待定。"
            "无法完整确定引用依赖时保持待定。"
        ),
        "distribution": dist,
        "per_candidate": [
            {
                "work_id": r["work_id"],
                "candidate_id": r["candidate_id"],
                "registration_chapter": r["anchor_chapter"],
                "nli_verdict": r["nli_verdict"],
                "citation_count": r["citation_count"],
                "verdict": r["ordering"]["verdict"],
                "summary": r["ordering"]["summary"],
            }
            for r in rows
        ],
        "caveat": (
            "统计口径是**引用级**：没有审核回包的候选（引用数为 0）与"
            "引用无法定位的候选一律记为时序待定，不能读作“时序通过”。"
        ),
    }
    if replay_p.is_file():
        rp = json.loads(replay_p.read_text(encoding="utf-8"))
        block["gate_replay"] = [
            {
                "work_id": r["work_id"],
                "candidate_id": r["candidate_id"],
                "ordering_verdict": r["ordering_verdict"],
                "gate_without_ordering": r["gate_without_ordering"],
                "gate_with_ordering": {
                    "decision": r["gate_with_ordering"]["decision"],
                    "assessment_status": r["gate_with_ordering"]["assessment_status"],
                },
            }
            for r in rp
        ]
        block["gate_replay_note"] = (
            "回归案例的观察结果，**不作为验收目标**。"
            "两次回放只差 evidence_ordering 一个字段：时序通过解除时间疑点后，"
            "门禁仍按自己的标准判定答案正确性、条件满足与信息增量。"
        )
    return block


def _delivery_hist(evidence_attr: dict) -> dict:
    from collections import Counter

    rows = evidence_attr.get("rows") or []
    return dict(Counter(r.get("evidence_delivery") for r in rows))


def main() -> None:
    b_scoring = load("t4-b-scoring.json")
    c_review = load("t4-c-review.json")
    c_review_ext = load("t4-c-review-ext24.json")
    cost = load("t4-cost-report.json")
    decl = load("t4-frozen-trial-declaration.json")
    indep = load("t4-independent-question-review.json")
    evidence_attr = load("t4-closeout-evidence-attribution.json")

    bi = b_scoring.get("b_improvement") or {}
    agg = bi.get("aggregate") or {}
    csum = c_review.get("summary") or {}
    cmp12 = cost.get("comparison_12ch") or {}

    report: dict = {
        "schema": "t4_trial_report_v1",
        "trial_scope": {
            "works": 4,
            "chapters_per_work": 12,
            "total_chapters": 48,
            "arms": ["baseline", "b_fixed", "candidate"],
            "arm_meaning": {
                "baseline": "生产现状：四个开关全关",
                "b_fixed": "只开 B 修复（fix_b2 + fix_b4），C 按需复审关",
                "candidate": "拟默认开启组合：B 修复 + C 按需复审",
            },
            "model": decl.get("model_binding", {}),
            "reference_set": {
                "observations_total": 80,
                "registration_denominator": 40,
                "resolution_denominator": 40,
                "independent_review_pairs": indep.get("pairs"),
            },
        },
        "acceptance": {},
        "b_detail": {},
        "c_detail": {
            "main_12ch": c_review.get("summary"),
            "main_12ch_fixed_set_retention": c_review.get("correct_retention_fixed_set"),
            "ext24_development_evidence": {
                "note": (
                    "24 章扩展范围与主试产同批作品（已曝光），只作开发证据与候选池扩充，"
                    "不进入独立验收结论。此处列出是因为它独立复现了主试产的同一结论方向。"
                ),
                "summary": c_review_ext.get("summary"),
                "fixed_set_retention": c_review_ext.get("correct_retention_fixed_set"),
            },
        },
        "cost_detail": cost.get("main_12ch"),
        "limitations": [],
        "not_covered": [],
    }

    # ---- B 改善 ----
    reg = agg.get("registration") or {}
    res = agg.get("resolution") or {}
    report["acceptance"]["B_improvement"] = {
        "threshold": {
            "miss_reduction_rel_min": 0.20,
            "works_not_degraded_min": 3,
            "works_improved_min": 2,
        },
        "measured": {
            "registration": reg,
            "resolution": res,
            "works_not_degraded": bi.get("works_not_degraded"),
            "works_total": bi.get("works_total"),
            "works_improved": bi.get("works_improved"),
        },
        "verdict": {
            "registration_meets_threshold": bool(reg.get("meets_20pct")),
            "resolution_meets_threshold": bool(res.get("meets_20pct")),
            "works_not_degraded_meets": (bi.get("works_not_degraded") or 0) >= 3,
            "works_improved_meets": (bi.get("works_improved") or 0) >= 2,
        },
        "note": (
            "分母为独立参考观察数（登记 40 / 回答 40），候选池大小不参与召回计算。"
            "登记指标在两臂完全相同：B 修复不改变采集阶段的登记集合，"
            "只改变同题合并与复检证据，因此改善只体现在回答指标上。"
        ),
    }
    report["b_detail"] = {
        "per_work": bi.get("per_work"),
        "per_observation": {
            wid: {
                arm: r.get("per_observation")
                for arm, r in arms.items()
            }
            for wid, arms in (b_scoring.get("works") or {}).items()
        },
    }

    # ---- C 误接受与正确答案保留 ----
    def crow(arm: str) -> dict:
        return csum.get(arm) or {}

    b_row, f_row, c_row = crow("baseline"), crow("b_fixed"), crow("candidate")
    fa_base = b_row.get("false_accept_count")
    fa_cand = c_row.get("false_accept_count")
    neg_base = b_row.get("false_accept_denominator")
    neg_cand = c_row.get("false_accept_denominator")
    report["acceptance"]["C_false_accept"] = {
        "threshold": "同一配对集上误接受不增加；同时列出误接受数量与已确认负例分母",
        "measured": {
            "baseline": {
                "false_accept_count": fa_base,
                "false_accept_denominator": neg_base,
                "false_accept_rate": b_row.get("false_accept_rate"),
                "false_accept_rate_status": b_row.get("false_accept_rate_status"),
                "pipeline_accepted": b_row.get("pipeline_accepted"),
                "confirmed_positive_candidates": b_row.get("confirmed_positive_candidates"),
                "confirmed_negative_candidates": b_row.get("confirmed_negative_candidates"),
                "unknown_candidates": b_row.get("unknown_candidates"),
            },
            "b_fixed": {
                "false_accept_count": f_row.get("false_accept_count"),
                "false_accept_denominator": f_row.get("false_accept_denominator"),
                "false_accept_rate": f_row.get("false_accept_rate"),
                "false_accept_rate_status": f_row.get("false_accept_rate_status"),
                "pipeline_accepted": f_row.get("pipeline_accepted"),
                "confirmed_positive_candidates": f_row.get("confirmed_positive_candidates"),
                "confirmed_negative_candidates": f_row.get("confirmed_negative_candidates"),
                "unknown_candidates": f_row.get("unknown_candidates"),
            },
            "candidate": {
                "false_accept_count": fa_cand,
                "false_accept_denominator": neg_cand,
                "false_accept_rate": c_row.get("false_accept_rate"),
                "false_accept_rate_status": c_row.get("false_accept_rate_status"),
                "pipeline_accepted": c_row.get("pipeline_accepted"),
                "confirmed_positive_candidates": c_row.get("confirmed_positive_candidates"),
                "confirmed_negative_candidates": c_row.get("confirmed_negative_candidates"),
                "unknown_candidates": c_row.get("unknown_candidates"),
            },
        },
        "verdict": {
            "false_accept_not_increased": None,
            "status": "无法计算",
        },
        "note": (
            "误接受率 = 被接受的已确认负例 / 全部已确认负例。"
            "本批经独立裁决的候选中，已确认负例为 0 条，误接受率记 N/A，"
            "不能以“已接受的项里没发现错误”宣称该门槛通过。"
            f"另有 {c_row.get('unknown_candidates')} 条候选未经裁决，不进入任何分母。"
            "接受项中的争议数量见 decision_coverage.accepted_but_disputed_or_unknown。"
        ),
    }

    keep_base = b_row.get("correct_keep_rate")
    keep_cand = c_row.get("correct_keep_rate")
    keep_b = f_row.get("correct_keep_rate")
    delta_pp = None
    if keep_base is not None and keep_cand is not None:
        delta_pp = round((keep_cand - keep_base) * 100, 2)
    report["acceptance"]["correct_answer_retention"] = {
        "threshold": "相对现有基线下降不超过 2 个百分点；存疑不算保留",
        "measured": {
            "retention": {
                "denominator": c_row.get("correct_keep_denominator"),
                "baseline_retention": keep_base,
                "b_fixed_retention": keep_b,
                "candidate_retention": keep_cand,
                "candidate_minus_baseline_pp": delta_pp,
            },
            "lost_attribution": {
                arm: {
                    "by_failure_category": (csum.get(arm) or {}).get("lost_by_failure_category"),
                    "non_audit_caused": (csum.get(arm) or {}).get("lost_non_audit_caused"),
                    "audit_false_kill": (csum.get(arm) or {}).get("false_kill_count"),
                    "audit_unresolved": (csum.get(arm) or {}).get("unresolved_count"),
                }
                for arm in ("baseline", "b_fixed", "candidate")
                if arm in csum
            },
            "candidate_false_kill_ids": c_row.get("false_kill_ids"),
            "candidate_unresolved_ids": c_row.get("unresolved_ids"),
        },
        "verdict": {
            "meets_threshold": (delta_pp is not None and delta_pp >= -2.0),
            "metric_note": (
                "分母是经独立裁决确认「原文确有可答事实」的正例候选数；"
                "分子是该臂答案经裁决为正确(yes)且被管线接受的条数。"
                "跨臂不共享答案正确性，每个臂的答案单独裁决。"
            ),
        },
        "note": (
            "丢失按归因分层：只有审核造成的丢失（audit_false_kill / base_recheck_missed）"
            "计入审核问题；evidence_not_delivered（原文有可答事实但证据没送到）"
            "与 answer_wrong_or_partial（答案本身不全或不对）是非审核归因。"
            "baseline 保留率低的主因是 7 条正例里 4 条证据未送达，不是审核拒绝。"
        ),
    }

    report["acceptance"]["C_audit_attribution"] = {
        "threshold": "审核不得造成误杀；审核造成的丢失须与证据/答案问题分开计数",
        "measured": {
            arm: {
                "audit_false_kill": (csum.get(arm) or {}).get("false_kill_count"),
                "audit_false_kill_ids": (csum.get(arm) or {}).get("false_kill_ids"),
                "audit_unresolved": (csum.get(arm) or {}).get("unresolved_count"),
                "audit_unresolved_ids": (csum.get(arm) or {}).get("unresolved_ids"),
                "lost_by_failure_category": (csum.get(arm) or {}).get("lost_by_failure_category"),
                "non_audit_caused_lost": (csum.get(arm) or {}).get("lost_non_audit_caused"),
            }
            for arm in ("baseline", "b_fixed", "candidate")
            if arm in csum
        },
        "verdict": {
            "audit_causes_false_kill": (c_row.get("false_kill_count") or 0) > 0,
        },
        "evidence_side_attribution": {
            "method": (evidence_attr.get("method") or ""),
            "delivery_distribution": _delivery_hist(evidence_attr),
            "recovered_from_broken_json": (
                (evidence_attr.get("repair_effect") or {}).get("recovered_from_broken_json")
            ),
            "repair_scope_note": (
                (evidence_attr.get("repair_effect") or {}).get("scope_note") or ""
            ),
        },
        "evidence_ordering": _ordering_block(),
        "note": (
            "证据侧定论分三层，不能混为一谈："
            "(1) 精确原文跨度是否存在于送达的证据里（sha256 校验过的重建）；"
            "(2) 该跨度相对**登记章**的时序——早于登记章的是背景材料，"
            "补前情有用但不能计作回收进展；"
            "(3) 审核是否因此产生了误杀。"
            "hist/fs_011 的支撑句在第 11 章，而登记章是第 12 章（first_seen 是第 9 章），"
            "属背景材料；试产范围只到第 12 章，登记之后没有内容可取，"
            "这不是检索缺陷而是登记时序与范围边界共同决定的。"
            "fs_007 的重建哈希未匹配，审核模型引用一句话不能证明它没收到其他内容，"
            "该条保持未核实。"
            "误杀计数为 0 不等于误杀风险为 0。"
        ),
    }

    report["acceptance"]["decision_coverage"] = {
        "verdict": {
            "all_branches_reported": True,
            "not_passing_via_all_disputed": all(
                (r.get("independent_dispute") or 0) < (r.get("candidates_total") or 1)
                for r in csum.values()
            ),
        },        "measured": {
            arm: {
                "candidates_total": r.get("candidates_total"),
                "pipeline_accepted": r.get("pipeline_accepted"),
                "pipeline_rejected_or_insufficient": r.get("pipeline_rejected_or_insufficient"),
                "accepted_checkable": r.get("accepted_checkable"),
                "accepted_uncheckable_no_quote": r.get("accepted_uncheckable_no_quote"),
                "independent_support": r.get("independent_support"),
                "independent_contradict": r.get("independent_contradict"),
                "independent_dispute": r.get("independent_dispute"),
                "false_kill_count": r.get("false_kill_count"),
                "unresolved_count": r.get("unresolved_count"),
            }
            for arm, r in csum.items()
        },
        "note": (
            "接受 / 拒绝 / 存疑 / 无引文未核对 / 复审未完成分列，未用“全部存疑”充数。"
            "12 章范围每臂 41-43 条候选，独立核验为 support 的仅 7 条，"
            "其余多数被判 dispute（机械核验无法单方面定论），已单列。"
        ),
    }

    # ---- 资源 ----
    lat = cmp12.get("latency") or {}
    bc = cmp12.get("baseline_to_candidate") or {}
    report["acceptance"]["resource"] = {
        "threshold": {"token_increase_pct_max": 15.0, "p95_latency_increase_pct_max": 20.0},
        "measured": {
            "token": bc,
            "latency": lat,
        },
        "verdict": {
            "token_meets_threshold": bc.get("meets_threshold"),
            "p95_meets_threshold": (
                lat.get("baseline_to_candidate", {}).get("meets_threshold")
            ),
            "p95_sample_size_adequate": lat.get("baseline_to_candidate", {}).get(
                "sample_size_adequate"
            ),
        },
        "note": (
            "P95 采样单位=单部作品的一次完整试产运行端到端墙钟；"
            "每臂样本量 3-4 次，样本量不足，按任务书标证据不足，"
            "不用单次请求 P95 顶替。"
        ),
    }

    report["acceptance"]["engineering"] = {
        "measured": {
            "backend_tests": "104 passed",
            "frontend_tests": "33 passed",
            "catalog_save_restore": "4 部 × 2 臂逐条核对：catalog 与账本 id 集合完全一致，无缺失无多余",
            "b4_behavioral_effect": "baseline 不产 foreshadow_full_catalog.json；b_fixed/candidate 均产出",
            "b4_merge_lossless": (
                "ind baseline 12 条 → b_fixed 10 条：2 条为同题合并（fs_011→fs_009、fs_012→fs_010），"
                "question/answer_criteria/anchor 完整保留，证据章范围更宽，无内容丢失"
            ),
        },
        "verdict": {"meets": True},
    }

    # ---- 局限与未覆盖 ----
    report["limitations"] = [
        "B 改善未达 20% 门槛：登记 0%、回答 5.1%；回答口径仅 hist 一部改善（2 条）。",
        "C 误接受率无法计算：本批经独立裁决的候选中已确认负例为 0 条，记 N/A，"
        "不能以“已接受项里没发现错误”宣称该门槛通过。",
        "正确答案保留率的分母只有 7 条经裁决的正例候选，样本量小；"
        "且丢失的主因是证据召回与登记时序，不是审核拒绝，"
        "因此 baseline 14.3% / b_fixed 71.4% / candidate 28.6% 这组数字"
        "不能解读为“审核让保留率下降 44 个百分点”。",
        "证据侧定论只覆盖经 sha256 校验的审计阶段请求；"
        "初始复检阶段的历史请求未缓存，该阶段的送达情况保持未定位。",
        "fs_007 的重建提示词哈希未匹配，审核模型引用一句话不能证明它没收到其他内容，"
        "该条保持未核实。关键词缺失只作线索，不能单独推出“未送达”。",
        "确认的审核误杀为 0 条，不等于误杀风险为 0——负例分母仍是 0。",
        "P95 每臂 4 次运行（低于建议的 8 次），样本量不足，标为证据不足。",
        "C 类真实管线候选在 12 章范围只有 41-43 条，不足 80 条门槛；"
        "24 章扩展范围已达 90-94 条，但该批作品已曝光，只能作开发证据。",
        "独立问题等价核对由评审代理给出 34 个等价配对，"
        "其中若干被评审员本人标注为“边界不完全重合”，已在 per_observation 的 diagnostic 中单列。",
        "旧版 C 核验（字词覆盖）会把否定翻转、答非所问、主体错配判为 support，"
        "其产出的 support/误杀/保留率数字已全部作废，本报告的数字来自独立裁决。",
    ]
    report["not_covered"] = [
        "全书链路收益（本次仅 12 章连续区间与 24 章扩展区间）。",
        "每臂 34-36 条候选未经独立裁决，其正确性未知。",
        "hist 的原运行缺 token 统计（臂名迁移时丢失），补跑为单列新运行，未与原运行合并。",
        "前端 UI 层的实际使用验证（本轮只跑组件测试）。",
        "初始复检阶段的历史请求无法复原：旧运行只留了单候选渲染的哈希，"
        "与真正发出的批次提示词对不上。新的请求日志机制只对**之后**的运行有效，"
        "不能补证历史——历史条目继续标为未知。",
    ]

    p = OUT_DIR / "t4-trial-report.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p)
    for k, v in report["acceptance"].items():
        print(f"\n=== {k} ===")
        print(json.dumps(v.get("verdict"), ensure_ascii=False))


if __name__ == "__main__":
    main()
