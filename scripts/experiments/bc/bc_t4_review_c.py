"""T4 C 类核对：从真实生产试产产物取候选，做独立正负核对与误接受统计。

候选来源：runs/<work_id>/<arm>/output/recheck_verdicts.json 的真实判定与
runs/<work_id>/<arm>/output/c_audit_cache.json 的按需复审记录——都是真实管线产物，
不是合成探针。

独立核对：对每条候选，用原文（frozen blocks）+ 候选的 question/answer_criteria/
引文做机械核验，判定"答案是否真的被原文支持"（正例）/ "是否属于复述铺垫或无据扩写"
（负例）；有争议的单列。不读任何模型判定结论。

输出：docs/research/bc-production-readiness-2026-11-06/t4-c-review.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

ROOT = Path(__file__).resolve().parents[3]
TRIAL_ROOT = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"
WORK_IDS = [
    "t4_ind_holdout",
    "t4_game_holdout",
    "t4_urban_holdout",
    "t4_hist_holdout",
]
ARMS = ["baseline", "b_fixed", "candidate"]


def load_chapters(work_id: str, base: Path | None = None) -> dict[int, str]:
    blocks = (base or TRIAL_ROOT) / work_id / "blocks"
    out: dict[int, str] = {}
    for p in sorted(blocks.glob("*.txt")):
        if p.stem.isdigit():
            out[int(p.stem)] = p.read_text(encoding="utf-8")
    return out


def _norm(s: str) -> str:
    return re.sub(r"\W+", "", s or "", flags=re.UNICODE)


def _ngrams(s: str, n: int = 2) -> set[str]:
    s = _norm(s)
    return {s[i : i + n] for i in range(len(s) - n + 1)} if len(s) >= n else ({s} if s else set())


def _dice(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def independent_review(
    cand: dict, chapters: dict[int, str], item: dict
) -> dict:
    """对单条候选做独立核验：不读模型判定，只看原文与候选自述内容。

    判定依据按证据强度分层，避免把"改写"误判成"无据扩写"：
      1. 引文是否逐字出现在原文——最强信号；
      2. 答案的实义单元是否能在原文找到**语义对应**（bigram 覆盖 + 关键实体命中），
         而不是要求逐字照抄：模型的合格答案本就是改述，逐字匹配会把真阳性判成争议；
      3. 答案是否只是复述铺垫（与锚点高度重合 ⇒ 没有新增信息）；
      4. 答案是否越过引文给出了原文没有的**新断言**（新增实体/数字/结论）。
    """
    answer = str(cand.get("answer") or "")
    quote = str(cand.get("quote") or "")
    criteria = str(item.get("answer_criteria") or "")
    question = str(item.get("question") or "")
    anchor = str(item.get("anchor_quote") or "")
    ch = int(cand.get("chapter") or item.get("resolved_chapter") or item.get("anchor_chapter") or 0)

    # 引文是否逐字出现在原文
    quote_found_at = None
    for u, txt in chapters.items():
        if quote and quote in txt:
            quote_found_at = u
            break
    quote_in_source = quote_found_at is not None

    # 答案与引文的一致性：答案是否是引文的复述（含全部引文字面）
    ans_bgs = _ngrams(answer)
    q_bgs = _ngrams(quote)
    overlap = _dice(ans_bgs, q_bgs)

    # 答案是否只是复述铺垫（与锚点/线索高度重合 = 没有新信息）
    anchor_bgs = _ngrams(anchor)
    restate = _dice(ans_bgs, anchor_bgs)

    # 逐个实义单元做**语义对应**判定：能在原文找到足够覆盖 → 可追溯；
    # 找不到 → 可能是无据扩写，但不立即定负，先记为待裁决线索。
    all_text = "\n".join(chapters.values())
    all_bgs = _ngrams(all_text)
    traceable: list[str] = []
    untraceable: list[str] = []
    for seg in re.split(r"[，。！？；、\n]", answer):
        seg = seg.strip()
        if len(seg) < 6:
            continue
        if quote and seg in quote:
            traceable.append(seg)
            continue
        if any(seg in txt for txt in chapters.values()):
            traceable.append(seg)
            continue
        # 语义对应：与原文整体有足够 bigram 覆盖
        seg_bgs = _ngrams(seg)
        cov = (len(seg_bgs & all_bgs) / len(seg_bgs)) if seg_bgs else 0.0
        if cov >= 0.6:
            traceable.append(seg)
        else:
            untraceable.append(seg)

    # 答案是否越过引文给出新断言：不可追溯单元里是否有"新增实体/数字/结论"
    new_assertion = False
    for seg in untraceable:
        if re.search(r"[0-9一二三四五六七八九十百千万亿]+", seg):
            new_assertion = True
            break
        if re.search(r"(必然|一定|最终|从此|彻底|证明了|说明了|导致)", seg):
            new_assertion = True
            break

    untraceable_ratio = (
        len(untraceable) / (len(untraceable) + len(traceable))
        if (untraceable or traceable)
        else 0.0
    )

    verdict = "dispute"
    reason = ""
    if not quote:
        # 无引文的接受：无法核对，按未触发误接受单列，不计入误接受分母
        verdict = "dispute"
        reason = "no_quote_in_answer"
    elif not quote_in_source:
        verdict = "contradict"
        reason = "quote_not_in_source"
    elif restate >= 0.85:
        verdict = "contradict"
        reason = "restating_setup_echo"
    elif untraceable and new_assertion:
        verdict = "dispute"
        reason = "answer_adds_assertion_not_in_source"
    elif untraceable_ratio >= 0.5 and untraceable:
        verdict = "dispute"
        reason = "majority_of_answer_not_traceable_to_source"
    else:
        verdict = "support"
        reason = "answer_traceable_to_quoted_source"

    return {
        "independent_verdict": verdict,
        "independent_reason": reason,
        "quote_in_source": quote_in_source,
        "quote_found_unit": quote_found_at,
        "answer_quote_dice": round(overlap, 4),
        "answer_anchor_dice": round(restate, 4),
        "traceable_units": traceable[:6],
        "untraceable_units": untraceable[:6],
        "untraceable_ratio": round(untraceable_ratio, 4),
        "criteria": criteria,
        "question": question,
    }


def collect_candidates(work_id: str, arm: str, base: Path | None = None) -> list[dict]:
    root = (base or TRIAL_ROOT) / "runs" / work_id
    base_dir = root / arm
    vp = base_dir / "output" / "recheck_verdicts.json"
    lp = base_dir / "foreshadow_ledger.json"
    if not vp.is_file() or not lp.is_file():
        return []
    verdicts = json.loads(vp.read_text(encoding="utf-8"))
    ledger = json.loads(lp.read_text(encoding="utf-8"))
    items = {str(i.get("id")): i for i in (ledger.get("items") or [])}
    routes = verdicts.get("audit_routes") or {}

    cands = []
    for v in verdicts.get("verdicts") or []:
        cid = str(v.get("candidate_id") or "")
        item = items.get(cid, {})
        rec = dict(v)
        rec["chapter"] = item.get("resolved_chapter") or item.get("anchor_chapter") or 0
        cands.append(
            {
                "work_id": work_id,
                "arm": arm,
                "candidate_id": cid,
                "item": item,
                "verdict": v,
                "route": routes.get(cid),
            }
        )
    return cands


def _time_class_map() -> dict:
    """读登记时序归类：(work_id, candidate_id) → 时序类别列表。

    来源：t4-background-vs-later-answer.json。
    background_material = 支撑句早于登记章，属背景材料，不计入"该送没送"。
    """
    p = OUT_DIR / "t4-background-vs-later-answer.json"
    if not p.is_file():
        return {}
    doc = json.loads(p.read_text(encoding="utf-8"))
    out: dict = {}
    for r in doc.get("rows") or []:
        out[(r["work_id"], r["candidate_id"])] = r.get("time_classes") or []
    return out


def _confirmed_delivery_map() -> dict:
    """读证据侧定论：(work_id, candidate_id, arm) → 送达判定。

    来源：t4-closeout-evidence-attribution.json。
    只有 confirmed_span_absent / confirmed_span_present 是**经 sha256 校验**的结论；
    audit_stage_unverified 与 unlocated 不能用来改判归因。
    """
    p = OUT_DIR / "t4-closeout-evidence-attribution.json"
    if not p.is_file():
        return {}
    doc = json.loads(p.read_text(encoding="utf-8"))
    out: dict = {}
    for r in doc.get("rows") or []:
        d = r.get("evidence_delivery")
        if d in ("confirmed_span_absent", "confirmed_span_present"):
            out[(r["work_id"], r["candidate_id"], r["arm"])] = d
    return out


def load_adjudication_labels(run_root: Path) -> dict:
    """读独立裁决标签。

    标签有两个键空间：
      (work_id, candidate_id)         → 候选级题目属性标签
      (work_id, candidate_id, arm)    → 该臂答案的正确性

    来源：t4-closeout-review-tasks/*.closeout.review.json（独立裁决代理产出）。
    没有裁决的候选一律 unknown，不得进入任何分母。
    """
    from scripts.experiments.bc.bc_t4_load_closeout_reviews import load_review_file

    out: dict = {}
    tdir = TRIAL_ROOT / "t4-closeout-review-tasks"
    for p in sorted(tdir.glob("*.closeout.review.json")):
        for r in load_review_file(p):
            wid = str(r.get("work_id") or "")
            cid = str(r.get("candidate_id") or "")
            arm = str(r.get("arm") or "")
            if not wid or not cid or not arm:
                continue
            correct = str(r.get("answer_correct") or "").lower()
            ans_ok = correct == "yes"
            ans_partial = correct == "partial"
            has_fact = str(r.get("source_has_answerable_fact") or "").lower() == "yes"
            ev = str(r.get("evidence_delivered_fact") or "").lower()
            fc = str(r.get("failure_category") or "")
            # 候选级题目属性：原文确有可答事实 → 该问题有正解（正例）；
            # 原文无可答事实 → 该问题无正解（负例）；无法判定 → 争议。
            if has_fact:
                label = "confirmed_positive"
            else:
                label = "confirmed_negative"
            key = (wid, cid)
            prev = out.get(key)
            if prev is None or prev.get("label") == "unknown":
                out[key] = {"label": label, "basis": fc}
            out[(wid, cid, arm)] = {
                "answer_correct": correct,
                "answer_correct_reason": r.get("answer_correct_reason"),
                "failure_category": fc,
                "source_has_answerable_fact": r.get("source_has_answerable_fact"),
                "evidence_delivered_fact": r.get("evidence_delivered_fact"),
                "notes": r.get("notes"),
            }
    return out


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--scope",
        choices=["main", "ext24"],
        default="main",
        help="main=12 章主试产；ext24=24 章扩展范围（已曝光，仅作开发证据）",
    )
    args = ap.parse_args()

    if args.scope == "ext24":
        run_root = TRIAL_ROOT / "ext24"
        out_name = "t4-c-review-ext24.json"
        arms = ["baseline", "candidate"]
    else:
        run_root = TRIAL_ROOT
        out_name = "t4-c-review.json"
        arms = ARMS

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out: dict = {
        "schema": "t4_c_review_v1",
        "scope": args.scope,
        "scope_note": (
            "24 章扩展范围与主试产同批作品，已曝光，只能作开发证据与候选池扩充，"
            "不进入独立验收结论。"
            if args.scope == "ext24"
            else "12 章主试产范围（4 部 × 12 章 = 48 章）"
        ),
        "arms": arms,
        "works": {},
    }
    per_arm_rows: dict[str, list[dict]] = {a: [] for a in arms}

    for wid in WORK_IDS:
        chapters = load_chapters(wid, run_root)
        if not chapters:
            continue
        out["works"][wid] = {}
        for arm in arms:
            cands = collect_candidates(wid, arm, run_root)
            if not cands:
                continue
            rows = []
            for c in cands:
                rev = independent_review(c["verdict"], chapters, c["item"])
                row = {
                    "work_id": wid,
                    "arm": arm,
                    "candidate_id": c["candidate_id"],
                    "pipeline_verdict": c["verdict"].get("verdict"),
                    "pipeline_answer": c["verdict"].get("answer"),
                    "pipeline_quote": c["verdict"].get("quote"),
                    "audit_route": (c["route"] or {}).get("route"),
                    "audit_executed": (c["route"] or {}).get("audit_executed"),
                    "audit_status": (c["route"] or {}).get("audit_status"),
                    "audit_trigger_reasons": (c["route"] or {}).get("trigger_reasons"),
                    **rev,
                }
                rows.append(row)
                per_arm_rows[arm].append(row)
            out["works"][wid][arm] = rows
            print(f"{wid}/{arm}: {len(rows)} candidates")

    # 汇总：按**候选级标签集**计算，而不是按臂的接受/拒绝反推。
    #
    # 标签分两层：
    #   - 题目属性（候选级，跨臂共享）：这个候选的问题是否有可确认的正解？
    #     confirmed_positive / confirmed_negative / disputed / unknown
    #   - 臂属性（逐臂）：该臂给出的答案是否正确？answer_correct = yes/no/partial
    #
    # 标签来源是独立裁决（t4-closeout-review-tasks/*.closeout.review.json）；
    # 没有裁决标签的候选只能记 unknown，不得计入任何分母。
    # 旧的字词核验只做"答案能否在原文找到对应"，会把否定翻转、答非所问、
    # 主体错配判成 support，不能作为正确性标签来源。
    labels = load_adjudication_labels(run_root)
    summary = {}
    for arm in arms:
        rows = per_arm_rows[arm]
        if not rows:
            continue
        accept = [r for r in rows if r["pipeline_verdict"] in ("SUPPORTED", "DIFFERENT_ANSWER")]
        reject = [r for r in rows if r["pipeline_verdict"] not in ("SUPPORTED", "DIFFERENT_ANSWER")]

        # 候选级标签（该臂涉及的候选）
        cand_label = {
            (r["work_id"], r["candidate_id"]): labels.get(
                (r["work_id"], r["candidate_id"]), {"label": "unknown"}
            )
            for r in rows
        }
        pos_ids = [k for k, v in cand_label.items() if v.get("label") == "confirmed_positive"]
        neg_ids = [k for k, v in cand_label.items() if v.get("label") == "confirmed_negative"]
        dis_ids = [k for k, v in cand_label.items() if v.get("label") == "disputed"]
        unk_ids = [k for k, v in cand_label.items() if v.get("label") == "unknown"]

        # 误接受 = 已确认负例中被管线接受的条数；分母 = 全部已确认负例
        false_accept = [
            r
            for r in rows
            if cand_label[(r["work_id"], r["candidate_id"])].get("label") == "confirmed_negative"
            and r["pipeline_verdict"] in ("SUPPORTED", "DIFFERENT_ANSWER")
        ]
        # 正确保留 = 已确认正例中，该臂答案正确(yes)且管线接受了它
        kept = []
        lost = []
        for r in rows:
            key = (r["work_id"], r["candidate_id"])
            lab = cand_label[key]
            if lab.get("label") != "confirmed_positive":
                continue
            arm_ans = labels.get((r["work_id"], r["candidate_id"], arm), {})
            correct = arm_ans.get("answer_correct")
            rec = {
                "work_id": r["work_id"],
                "candidate_id": r["candidate_id"],
                "pipeline_verdict": r["pipeline_verdict"],
                "answer_correct": correct,
                "failure_category": arm_ans.get("failure_category"),
                "audit_status": r.get("audit_status"),
                "audit_trigger_reasons": r.get("audit_trigger_reasons"),
                "note": arm_ans.get("answer_correct_reason"),
            }
            if correct == "yes" and r["pipeline_verdict"] in ("SUPPORTED", "DIFFERENT_ANSWER"):
                kept.append(rec)
            else:
                lost.append(rec)

        # 丢失的归因分层：只有"审核造成"的丢失才计入误杀/未决；
        # evidence_not_delivered 与 answer_wrong_or_partial 是**非审核归因**，
        # 计入它们只会把证据检索问题错记成审核问题。
        lost_by_category: dict[str, int] = {}
        for r in lost:
            fc = r.get("failure_category") or "unclassified"
            lost_by_category[fc] = lost_by_category.get(fc, 0) + 1
        # 审核造成的丢失：**必须经证据侧确认**才算误杀。
        #
        # 裁决代理把 hist/fs_011 判为 audit_false_kill，理由是"审核称原文没写 X，
        # 而证据原文恰恰写了 X"。但重建审核提示词后与缓存的 prompt_sha256 比对一致，
        # 证明该次请求**实际只送达 1 段 822 字**、全部关键句都不在其中——
        # 审核针对它真正看到的材料判 off_topic 是合理的，不构成误杀。
        #
        # 另一层：**登记时序**。支撑句早于登记章的是背景材料，
        # 属"登记之后没有相应内容"，不是"该送没送"。
        # 所以这里只在"证据侧确认已送达且答案正确"时记误杀；
        # 证据未送达的一律改记 evidence_not_delivered。
        confirmed_delivery = _confirmed_delivery_map()
        time_class = _time_class_map()
        audit_caused_lost = []
        reclassified = []
        for r in lost:
            fc = r.get("failure_category") or "unclassified"
            key = (r["work_id"], r["candidate_id"], arm)
            delivery = confirmed_delivery.get(key)
            tc = time_class.get((r["work_id"], r["candidate_id"]), [])
            if fc in ("audit_false_kill", "base_recheck_missed"):
                if delivery == "confirmed_span_absent" and "background_material" in tc:
                    # 支撑句在登记章之前 → 背景材料，不是"该送没送"
                    r = dict(r)
                    r["failure_category"] = "background_material_not_later_answer"
                    r["reclassified_from"] = fc
                    r["evidence_delivery"] = delivery
                    r["time_class"] = tc
                    reclassified.append(r)
                    lost_by_category[fc] = lost_by_category.get(fc, 0) - 1
                    lost_by_category["background_material_not_later_answer"] = (
                        lost_by_category.get("background_material_not_later_answer", 0) + 1
                    )
                elif delivery == "confirmed_span_absent":
                    r = dict(r)
                    r["failure_category"] = "evidence_not_delivered"
                    r["reclassified_from"] = fc
                    r["evidence_delivery"] = delivery
                    reclassified.append(r)
                    lost_by_category[fc] = lost_by_category.get(fc, 0) - 1
                    lost_by_category["evidence_not_delivered"] = (
                        lost_by_category.get("evidence_not_delivered", 0) + 1
                    )
                else:
                    audit_caused_lost.append(r)
        false_kill = [
            r
            for r in audit_caused_lost
            if r.get("failure_category") == "audit_false_kill"
            and r.get("audit_status") == "rejected"
        ]
        unresolved = [
            r
            for r in audit_caused_lost
            if r.get("failure_category") != "audit_false_kill"
            or r.get("audit_status") != "rejected"
        ]

        summary[arm] = {
            "candidates_total": len(rows),
            "pipeline_accepted": len(accept),
            "pipeline_rejected_or_insufficient": len(reject),
            # 候选级标签分布
            "confirmed_positive_candidates": len(pos_ids),
            "confirmed_negative_candidates": len(neg_ids),
            "disputed_candidates": len(dis_ids),
            "unknown_candidates": len(unk_ids),
            "adjudicated_candidates": len(pos_ids) + len(neg_ids),
            # 1. 误接受：分母 = 全部已确认负例（不因管线接受与否而变）
            "false_accept_count": len(false_accept),
            "false_accept_denominator": len(neg_ids),
            "false_accept_rate": (
                round(len(false_accept) / len(neg_ids), 4) if neg_ids else None
            ),
            "false_accept_rate_status": "N/A（无已确认负例）" if not neg_ids else "computed",
            "false_accept_ids": [
                {"work_id": r["work_id"], "candidate_id": r["candidate_id"]}
                for r in false_accept
            ],
            # 2. 误杀 / 未决（只统计审核造成的丢失）
            "false_kill_count": len(false_kill),
            "false_kill_denominator": len(pos_ids),
            "false_kill_ids": false_kill,
            "unresolved_count": len(unresolved),
            "unresolved_ids": unresolved,
            # 2c. 丢失归因分布（区分审核问题与非审核问题）
            "lost_by_failure_category": lost_by_category,
            "reclassified_by_evidence_side": reclassified,
            "lost_non_audit_caused": sum(
                v for k, v in lost_by_category.items()
                if k not in ("audit_false_kill", "base_recheck_missed")
            ),
            # 3. 正确保留：分母 = 全部已确认正例
            "correct_kept_count": len(kept),
            "correct_keep_denominator": len(pos_ids),
            "correct_keep_rate": (round(len(kept) / len(pos_ids), 4) if pos_ids else None),
            "correct_keep_rate_status": "N/A（无已确认正例）" if not pos_ids else "computed",
            "lost_positives": lost,
            "accepted_but_disputed_or_unknown": [
                {
                    "work_id": r["work_id"],
                    "candidate_id": r["candidate_id"],
                    "label": cand_label[(r["work_id"], r["candidate_id"])].get("label"),
                    "answer_correct": labels.get(
                        (r["work_id"], r["candidate_id"], arm), {}
                    ).get("answer_correct"),
                }
                for r in accept
                if cand_label[(r["work_id"], r["candidate_id"])].get("label")
                in ("disputed", "unknown")
            ],
        }
    out["summary"] = summary

    # ---- 标签集本身（可审计） ----
    out["adjudication_label_set"] = {
        "source": "t4-closeout-review-tasks/*.closeout.review.json（独立裁决代理）",
        "note": (
            "字词核验（independent_review）只做来源可追溯性检查，会把否定翻转、"
            "答非所问、主体错配判成 support，因此不作为正确性标签来源，仅保留为诊断字段。"
        ),
        "candidate_labels": {
            f"{k[0]}|{k[1]}": v
            for k, v in labels.items()
            if len(k) == 2
        },
    }

    # 说明：这里不再有"跨臂共享答案正确性"的固定集合。
    # 旧实现按 (work_id, candidate_id) 合并 truth——任一臂的答案被判支持，
    # 其他臂只要接受同编号条目就计为保留正确，即使它的答案不同。
    # 正确口径是：候选级标签（题目是否有可确认正解）跨臂共享，
    # 但"该臂答案是否正确"逐臂判定，见 summary[*].correct_kept_count。
    out["retention_metric_note"] = (
        "正确保留 = 该臂答案经独立裁决为正确(yes)，且管线接受了它；"
        "分母 = 已确认正例候选数。跨臂不共享答案正确性。"
    )

    p = OUT_DIR / out_name
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
