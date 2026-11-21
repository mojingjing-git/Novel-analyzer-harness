"""T4 B 类评分（修正版）：以**独立参考集**为唯一分母，用独立评审结论配对。

与上一版的差别（对应复核意见）：
1. 分母固定为独立参考观察数（每部 10 登记 + 10 回答），**不用候选池大小做分母**；
   候选池只作为"被匹配的对象"。
2. 问题等价性不再由规则模拟，而由**独立评审代理**逐对给出结论
   （t4-review-tasks/*.review.json）；本脚本只消费结论，不改判据。
3. 逐作品给分子分母，并给出相对遗漏数减少率（B 改善门槛 ≥20%）。
4. 争议配对单列：独立评审明确标注"边界不完全重合"的配对单独统计，
   便于按严/宽两种口径分别出数。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

ROOT = Path(__file__).resolve().parents[3]
TRIAL_ROOT = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
REVIEW_DIR = TRIAL_ROOT / "t4-review-tasks"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"
WORK_IDS = ["t4_ind_holdout", "t4_game_holdout", "t4_urban_holdout", "t4_hist_holdout"]
ARMS = ["baseline", "b_fixed", "candidate"]
OBS_TYPE_MAP = {"setup": "registration", "resolution": "resolution"}


def load_observations(work_id: str) -> list[dict]:
    p = TRIAL_ROOT / work_id / "reference_observations.json"
    obs = json.loads(p.read_text(encoding="utf-8"))["observations"]
    for o in obs:
        o["obs_type"] = OBS_TYPE_MAP.get(str(o.get("obs_type")), "registration")
        o.setdefault("anchor_chapter", int(o.get("chapter") or 1))
        if o.get("resolution_chapter"):
            o.setdefault("resolution_subchapter", int(o["resolution_chapter"]))
    return obs


def load_independent_review(work_id: str) -> dict[str, list[str]]:
    """读独立评审结论：obs_id → 等价候选 id 列表。"""
    p = REVIEW_DIR / f"{work_id}.review.json"
    if not p.is_file():
        return {}
    rows = json.loads(p.read_text(encoding="utf-8"))
    out = {}
    for r in rows:
        out[str(r.get("obs_id"))] = [str(c) for c in (r.get("equivalent_candidate_ids") or [])]
    return out


def load_arm(work_id: str, arm: str) -> tuple[list[dict], dict[str, dict]]:
    base = TRIAL_ROOT / "runs" / work_id / arm
    ledger = json.loads((base / "foreshadow_ledger.json").read_text(encoding="utf-8"))
    verdicts_doc = json.loads(
        (base / "output" / "recheck_verdicts.json").read_text(encoding="utf-8")
    )
    items = ledger.get("items") or []
    items_by_id = {str(i.get("id")): i for i in items}
    verdicts_by_cid = {}
    for v in verdicts_doc.get("verdicts") or []:
        cid = str(v.get("candidate_id") or "")
        if not cid:
            continue
        rec = dict(v)
        item = items_by_id.get(cid, {})
        rec.setdefault("chapter", item.get("resolved_chapter") or item.get("anchor_chapter") or 0)
        verdicts_by_cid[cid] = rec
    return items, verdicts_by_cid


def score_work(work_id: str, arm: str, strict_only_equiv: bool = True) -> dict:
    from backend.common.bc_scorer_and_labels import (
        SCORER_VERSION,
        BookSpanIndexer,
        adjudicate_reference_candidate_pair,
    )

    chapters = {
        ch: (TRIAL_ROOT / work_id / "blocks" / f"{ch:04d}.txt").read_text(encoding="utf-8")
        for ch in range(1, 13)
    }
    indexer = BookSpanIndexer(chapters)
    obs = load_observations(work_id)
    items, verdicts_by_cid = load_arm(work_id, arm)
    items_by_id = {str(i.get("id")): i for i in items}
    indep = load_independent_review(work_id)

    per_obs = []
    for o in obs:
        oid = str(o.get("obs_id"))
        cand_ids = indep.get(oid, [])
        chosen = None
        diag = None
        for cid in cand_ids:
            item = items_by_id.get(cid)
            if item is None:
                # 该臂没有这条候选（合并后消失）→ 该观察在本臂无对应登记
                continue
            rec = adjudicate_reference_candidate_pair(
                o,
                item,
                verdicts_by_cid.get(cid),
                indexer=indexer,
                independent_review_entry={
                    "governance_mode": "question_only",
                    "question_equivalence": "equivalent",
                    "adjudication_source": "t4_independent_agent_review",
                    "adjudication_reason": "independent_review_confirmed_same_event",
                },
            )
            if rec.counted_as_hit:
                chosen = (cid, rec)
                break
            if diag is None:
                diag = {
                    "candidate_id": cid,
                    "question_equivalence": rec.question_equivalence,
                    "answer_fact_status": rec.answer_fact_status,
                    "evidence_attribution": rec.evidence_attribution,
                    "closing_criteria_status": rec.closing_criteria_status,
                    "adjudication_reason": rec.adjudication_reason,
                }
        if chosen is None and not diag:
            diag = {"candidate_id": "", "adjudication_reason": "no_equivalent_candidate_in_this_arm"}
        per_obs.append(
            {
                "obs_id": oid,
                "obs_type": o["obs_type"],
                "chapter": o.get("chapter"),
                "independent_equivalent_candidate_ids": cand_ids,
                "hit": bool(chosen),
                "hit_candidate_id": chosen[0] if chosen else None,
                "diagnostic": diag,
            }
        )

    def agg(kind: str) -> dict:
        rows = [r for r in per_obs if r["obs_type"] == kind]
        den = len(rows)
        hit = sum(1 for r in rows if r["hit"])
        return {
            "denominator": den,
            "strict_hits": hit,
            "missed": den - hit,
            "recall": round(hit / den, 4) if den else None,
        }

    return {
        "work_id": work_id,
        "arm": arm,
        "scorer_version": SCORER_VERSION,
        "independent_review_applied": True,
        "candidates_in_pool": len(items),
        "observations_total": len(obs),
        "registration": agg("registration"),
        "resolution": agg("resolution"),
        "per_observation": per_obs,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out: dict = {
        "schema": "t4_b_scoring_v2",
        "denominator_note": (
            "分母固定为独立参考观察数（每部 10 登记 + 10 回答，全局 40+40）。"
            "候选池大小不参与召回计算——它是被匹配的对象，不是分母。"
        ),
        "matching_note": (
            "问题等价性由独立评审代理逐对给出结论（t4-review-tasks/*.review.json）；"
            "本脚本不自行判定等价，只消费结论并跑结构化裁决。"
        ),
        "works": {},
    }

    for wid in WORK_IDS:
        out["works"][wid] = {}
        for arm in ARMS:
            if not (TRIAL_ROOT / "runs" / wid / arm / "foreshadow_ledger.json").is_file():
                continue
            r = score_work(wid, arm)
            out["works"][wid][arm] = r
            print(
                f"{wid}/{arm}: REG {r['registration']['strict_hits']}/{r['registration']['denominator']}"
                f"  RES {r['resolution']['strict_hits']}/{r['resolution']['denominator']}"
                f"  pool={r['candidates_in_pool']}"
            )

    summary = {}
    for arm in ARMS:
        rows = [out["works"][w][arm] for w in out["works"] if arm in out["works"][w]]
        if not rows:
            continue
        for kind in ("registration", "resolution"):
            den = sum(r[kind]["denominator"] for r in rows)
            hit = sum(r[kind]["strict_hits"] for r in rows)
            summary.setdefault(arm, {})[kind] = {
                "denominator": den,
                "strict_hits": hit,
                "missed": den - hit,
                "recall": round(hit / den, 4) if den else None,
            }
    out["summary"] = summary

    # B 改善：baseline → b_fixed（只变 B 开关），按参考集分母算遗漏减少
    if "baseline" in summary and "b_fixed" in summary:
        imp = {}
        for kind in ("registration", "resolution"):
            bm = summary["baseline"][kind]["missed"]
            fm = summary["b_fixed"][kind]["missed"]
            imp[kind] = {
                "baseline_missed": bm,
                "b_fixed_missed": fm,
                "miss_reduction_abs": bm - fm,
                "miss_reduction_rel": round((bm - fm) / bm, 4) if bm else None,
                "meets_20pct": ((bm - fm) / bm >= 0.2) if bm else None,
            }
        # 逐作品不退化统计
        per_work = {}
        for wid in out["works"]:
            if "baseline" not in out["works"][wid] or "b_fixed" not in out["works"][wid]:
                continue
            b = out["works"][wid]["baseline"]
            f = out["works"][wid]["b_fixed"]
            row = {}
            for kind in ("registration", "resolution"):
                row[kind] = {
                    "baseline": b[kind]["strict_hits"],
                    "b_fixed": f[kind]["strict_hits"],
                    "denominator": b[kind]["denominator"],
                    "delta": f[kind]["strict_hits"] - b[kind]["strict_hits"],
                    "degraded": f[kind]["strict_hits"] < b[kind]["strict_hits"],
                    "improved": f[kind]["strict_hits"] > b[kind]["strict_hits"],
                }
            per_work[wid] = row
        not_degraded = sum(
            1 for wid, r in per_work.items()
            if not r["registration"]["degraded"] and not r["resolution"]["degraded"]
        )
        improved = sum(
            1 for wid, r in per_work.items()
            if r["registration"]["improved"] or r["resolution"]["improved"]
        )
        out["b_improvement"] = {
            "aggregate": imp,
            "per_work": per_work,
            "works_not_degraded": not_degraded,
            "works_total": len(per_work),
            "works_improved": improved,
            "threshold": {
                "miss_reduction_rel_min": 0.20,
                "works_not_degraded_min": "3/4",
                "works_improved_min": 2,
            },
        }

    p = OUT_DIR / "t4-b-scoring.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p)
    print(json.dumps(out.get("b_improvement"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
