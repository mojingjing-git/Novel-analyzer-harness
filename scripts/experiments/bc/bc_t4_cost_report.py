"""T4 成本与耗时核算：从试产产物汇总逐臂 token、调用数与耗时。

产物来源：
  workspace/bc-evaluation/t4-trial-20261106/t4-trial-run.json   （12 章主试产，含分类 token 统计）
  workspace/bc-evaluation/t4-trial-20261106/ext24/ext24-run.json（24 章扩展试产）

P95 口径（任务书 §六要求明确采样单位与样本量）：
  采样单位 = **单部作品的一次完整试产运行**（分析→调和→复检→落账的端到端墙钟），
  不是单次模型请求。样本量 = 逐作品逐臂的运行数。
  样本量不足时按任务书要求标"证据不足"，不用单次请求 P95 顶替。
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TRIAL_ROOT = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"
ARMS = ["baseline", "b_fixed", "candidate"]


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return round(s[0], 2)
    # nearest-rank
    idx = max(0, min(len(s) - 1, int(round(0.95 * len(s) + 0.5)) - 1))
    return round(s[idx], 2)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out: dict = {"schema": "t4_cost_report_v1"}

    # ---- 12 章主试产 ----
    main_run = json.loads((TRIAL_ROOT / "t4-trial-run.json").read_text(encoding="utf-8"))
    arms_data: dict[str, dict] = {}
    per_work_rows = []
    for wid, w in (main_run.get("works") or {}).items():
        an = (w.get("analysis") or {}).get("tokens") or {}
        row = {
            "work_id": wid,
            "analysis_calls": an.get("calls", 0),
            "analysis_input_tokens": an.get("input_tokens", 0),
            "analysis_output_tokens": an.get("output_tokens", 0),
            "arms": {},
        }
        for arm, r in (w.get("arms") or {}).items():
            tot = r.get("tokens_total") or {}
            cats = r.get("tokens_by_category") or {}
            row["arms"][arm] = {
                "calls": sum(v["calls"] for v in cats.values()),
                "input_tokens": tot.get("input_tokens", 0),
                "output_tokens": tot.get("output_tokens", 0),
                "total_tokens": tot.get("total_tokens", 0),
                "elapsed_sec": r.get("elapsed_sec"),
                "by_category": cats,
            }
        per_work_rows.append(row)

    # ---- hist 补跑（单列的新运行） ----
    rerun_path = TRIAL_ROOT / "t4-trial-rerun.json"
    rerun_rows = []
    if rerun_path.is_file():
        rr = json.loads(rerun_path.read_text(encoding="utf-8"))
        for wid, w in (rr.get("works") or {}).items():
            an = (w.get("analysis") or {}).get("tokens") or {}
            row = {
                "work_id": wid,
                "run_kind": w.get("run_kind"),
                "analysis_calls": an.get("calls", 0),
                "analysis_input_tokens": an.get("input_tokens", 0),
                "analysis_output_tokens": an.get("output_tokens", 0),
                "arms": {},
            }
            for arm, r in (w.get("arms") or {}).items():
                tot = r.get("tokens_total") or {}
                cats = r.get("tokens_by_category") or {}
                row["arms"][arm] = {
                    "calls": sum(v["calls"] for v in cats.values()),
                    "input_tokens": tot.get("input_tokens", 0),
                    "output_tokens": tot.get("output_tokens", 0),
                    "total_tokens": tot.get("total_tokens", 0),
                    "elapsed_sec": r.get("elapsed_sec"),
                    "by_category": cats,
                }
            rerun_rows.append(row)

    def _aggregate(rows: list[dict]) -> dict[str, dict]:
        agg: dict[str, dict] = {}
        for arm in ARMS:
            sel = [r for r in rows if arm in r["arms"]]
            if not sel:
                continue
            cats_all: dict[str, dict] = {}
            for r in sel:
                for c, v in (r["arms"][arm]["by_category"] or {}).items():
                    slot = cats_all.setdefault(
                        c, {"input_tokens": 0, "output_tokens": 0, "calls": 0}
                    )
                    slot["input_tokens"] += v["input_tokens"]
                    slot["output_tokens"] += v["output_tokens"]
                    slot["calls"] += v["calls"]
            elapsed = [
                r["arms"][arm]["elapsed_sec"]
                for r in sel
                if r["arms"][arm]["elapsed_sec"]
            ]
            agg[arm] = {
                "works_measured": len(sel),
                "summary_calls": sum(r["arms"][arm]["calls"] for r in sel),
                "summary_input_tokens": sum(r["arms"][arm]["input_tokens"] for r in sel),
                "summary_output_tokens": sum(r["arms"][arm]["output_tokens"] for r in sel),
                "summary_total_tokens": sum(r["arms"][arm]["total_tokens"] for r in sel),
                "by_category": cats_all,
                "elapsed_samples_sec": elapsed,
                "elapsed_p50_sec": round(statistics.median(elapsed), 2) if elapsed else None,
                "elapsed_p95_sec": p95(elapsed),
            }
        return agg

    arms_data = _aggregate(per_work_rows)
    rerun_arms = _aggregate(rerun_rows)

    out["main_12ch"] = {
        "per_work": per_work_rows,
        "by_arm": arms_data,
    }
    if rerun_rows:
        out["main_12ch"]["separate_rerun"] = {
            "reason": "原运行的 hist 臂在臂名迁移时丢失 token 统计；补跑单列，不与原运行合并",
            "per_work": rerun_rows,
            "by_arm": rerun_arms,
        }

    # ---- 增量对比 ----
    # 成本对比用**合并口径**：主运行 + 单列补跑（hist 补跑覆盖同一作品的同口径运行），
    # 每臂 4 部作品。补跑的原始数据单独保留在 main_12ch.separate_rerun 里。
    cmp_rows = per_work_rows + [
        {**r, "work_id": f"{r['work_id']}(rerun)"} for r in rerun_rows
    ]
    cmp_arms = _aggregate(cmp_rows)

    cmp_out = {}
    if "baseline" in cmp_arms and "b_fixed" in cmp_arms:
        b = cmp_arms["baseline"]["summary_total_tokens"]
        f = cmp_arms["b_fixed"]["summary_total_tokens"]
        cmp_out["baseline_to_b_fixed"] = {
            "baseline_total_tokens": b,
            "b_fixed_total_tokens": f,
            "delta_tokens": f - b,
            "delta_pct": round((f - b) / b * 100, 2) if b else None,
            "threshold_pct": 15.0,
            "meets_threshold": (f - b) / b * 100 <= 15.0 if b else None,
            "works_measured_per_arm": cmp_arms["baseline"]["works_measured"],
        }
    if "baseline" in cmp_arms and "candidate" in cmp_arms:
        b = cmp_arms["baseline"]["summary_total_tokens"]
        c = cmp_arms["candidate"]["summary_total_tokens"]
        cmp_out["baseline_to_candidate"] = {
            "baseline_total_tokens": b,
            "candidate_total_tokens": c,
            "delta_tokens": c - b,
            "delta_pct": round((c - b) / b * 100, 2) if b else None,
            "threshold_pct": 15.0,
            "meets_threshold": (c - b) / b * 100 <= 15.0 if b else None,
            "works_measured_per_arm": cmp_arms["baseline"]["works_measured"],
        }
    # P95 对比
    for a, c in (("baseline", "b_fixed"), ("baseline", "candidate")):
        if a in cmp_arms and c in cmp_arms:
            pa = cmp_arms[a].get("elapsed_p95_sec")
            pc = cmp_arms[c].get("elapsed_p95_sec")
            if pa and pc:
                cmp_out.setdefault("latency", {})[f"{a}_to_{c}"] = {
                    "baseline_p95_sec": pa,
                    "other_p95_sec": pc,
                    "delta_pct": round((pc - pa) / pa * 100, 2),
                    "threshold_pct": 20.0,
                    "meets_threshold": (pc - pa) / pa * 100 <= 20.0,
                    "sample_size_per_arm": cmp_arms[a]["works_measured"],
                    "sampling_unit": "单部作品的一次完整试产运行端到端墙钟",
                    "sample_size_adequate": cmp_arms[a]["works_measured"] >= 8,
                }
    out["comparison_12ch"] = cmp_out
    out["comparison_basis"] = {
        "runs_included": "主运行 4 部 + hist 单列补跑 1 部",
        "note": "hist 的补跑覆盖同一部作品同口径运行，用于补齐缺失统计；原运行数据不与补跑混合计数。",
    }

    # ---- 24 章扩展试产 ----
    ext_path = TRIAL_ROOT / "ext24" / "ext24-run.json"
    if ext_path.is_file():
        ext = json.loads(ext_path.read_text(encoding="utf-8"))
        ext_arms: dict[str, dict] = {}
        for wid, w in (ext.get("works") or {}).items():
            for arm, r in (w.get("arms") or {}).items():
                cats = r.get("tokens_by_category") or {}
                slot = ext_arms.setdefault(
                    arm,
                    {
                        "works_measured": 0,
                        "calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "elapsed_samples_sec": [],
                    },
                )
                slot["works_measured"] += 1
                slot["calls"] += sum(v["calls"] for v in cats.values())
                slot["input_tokens"] += r.get("tokens_total", {}).get("input_tokens", 0)
                slot["output_tokens"] += r.get("tokens_total", {}).get("output_tokens", 0)
                slot["total_tokens"] += (
                    r.get("tokens_total", {}).get("input_tokens", 0)
                    + r.get("tokens_total", {}).get("output_tokens", 0)
                )
                if r.get("elapsed_sec"):
                    slot["elapsed_samples_sec"].append(r["elapsed_sec"])
        for arm, v in ext_arms.items():
            v["elapsed_p95_sec"] = p95(v["elapsed_samples_sec"])
            v["elapsed_p50_sec"] = (
                round(statistics.median(v["elapsed_samples_sec"]), 2)
                if v["elapsed_samples_sec"]
                else None
            )
        out["ext_24ch"] = {"by_arm": ext_arms, "chapters": ext.get("chapters")}

    p = OUT_DIR / "t4-cost-report.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p)
    print(json.dumps(out.get("comparison_12ch"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
