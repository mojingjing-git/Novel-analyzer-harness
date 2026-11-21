"""生成 7 条名义正确候选的逐条复核任务包。

背景（独立复核 F1/F2/F3）：
- 旧的 `bc_t4_review_c.py` 核验器只做"答案能否在原文找到对应"的字词检查，
  会把否定翻转、答非所问、主体错配一律判为 support；
- 因此旧报告里的 support / 误杀 / 保留率数字全部不成立；
- 本任务包让独立评审代理对**每条答案本身**做语义裁决：
  原文说了什么 → 问题问什么 → 条件要求什么 → 这条答案是否真的答对。

任务包按 (work_id, candidate_id, arm) 组织，含：
  - 原文全文（供裁决者查证）
  - 候选问题 / 答案标准 / 锚点
  - 该臂实际给出的答案与引文
  - 送审证据（模型当时看到的）
  - 管线判定与复审原因
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TRIAL_ROOT = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = TRIAL_ROOT / "t4-closeout-review-tasks"
WORK_IDS = ["t4_ind_holdout", "t4_game_holdout", "t4_urban_holdout", "t4_hist_holdout"]
ARMS = ["baseline", "b_fixed", "candidate"]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    review_doc = json.loads(
        (
            ROOT
            / "docs/research/bc-production-readiness-2026-11-06/t4-c-review.json"
        ).read_text(encoding="utf-8")
    )
    fixed = review_doc.get("correct_retention_fixed_set") or {}
    target_ids: set[tuple[str, str]] = set()
    for arm_rows in fixed.values():
        for rec in arm_rows.get("lost_records") or []:
            target_ids.add((rec["work_id"], rec["candidate_id"]))
        # kept 的也在范围内
    # 复原被固定集合覆盖的全部候选 id
    for wid, arms in (review_doc.get("works") or {}).items():
        for arm_rows in arms.values():
            for r in arm_rows:
                target_ids.add((wid, r["candidate_id"]))
    # 只保留出现在 fixed set 里的（名义正确候选）
    for arm, rows in fixed.items():
        for rec in rows.get("lost_records") or []:
            target_ids.add((rec["work_id"], rec["candidate_id"]))

    packages = []
    for wid in WORK_IDS:
        chapters = {
            ch: (TRIAL_ROOT / wid / "blocks" / f"{ch:04d}.txt").read_text(encoding="utf-8")
            for ch in range(1, 13)
            if (TRIAL_ROOT / wid / "blocks" / f"{ch:04d}.txt").is_file()
        }
        for arm in ARMS:
            base = TRIAL_ROOT / "runs" / wid / arm
            ledger_p = base / "foreshadow_ledger.json"
            verdict_p = base / "output" / "recheck_verdicts.json"
            if not ledger_p.is_file() or not verdict_p.is_file():
                continue
            items = {
                str(i.get("id")): i
                for i in (json.loads(ledger_p.read_text(encoding="utf-8")).get("items") or [])
            }
            vdoc = json.loads(verdict_p.read_text(encoding="utf-8"))
            routes = vdoc.get("audit_routes") or {}
            for v in vdoc.get("verdicts") or []:
                cid = str(v.get("candidate_id") or "")
                if (wid, cid) not in target_ids:
                    continue
                item = items.get(cid, {})
                packages.append(
                    {
                        "work_id": wid,
                        "arm": arm,
                        "candidate_id": cid,
                        "candidate": {
                            "question": item.get("question"),
                            "answer_criteria": item.get("answer_criteria"),
                            "description": item.get("description"),
                            "anchor_quote": item.get("anchor_quote"),
                            "anchor_chapter": item.get("anchor_chapter"),
                            "status": item.get("status"),
                        },
                        "pipeline": {
                            "verdict": v.get("verdict"),
                            "answer": v.get("answer"),
                            "quote": v.get("quote"),
                            "evidence_id": v.get("evidence_id"),
                            "supporting_quote": v.get("supporting_quote"),
                            "counter_search": v.get("counter_search"),
                        },
                        "audit": (routes.get(cid) or {}),
                    }
                )

    # 只保留真正在 fixed set 中被判过 support 的那些 (work, candidate)
    support_pairs: set[tuple[str, str]] = set()
    for wid, arms in (review_doc.get("works") or {}).items():
        for arm_rows in arms.values():
            for r in arm_rows:
                if r.get("independent_verdict") == "support":
                    support_pairs.add((wid, r["candidate_id"]))
    packages = [p for p in packages if (p["work_id"], p["candidate_id"]) in support_pairs]

    payload = {
        "instructions": (
            "你是独立裁决员。逐条判断**这一个臂给出的这个答案**是否真的正确回答了问题。"
            "对每条包，必须自己核对原文，不要相信任何已有标签。"
            "\n\n判定顺序："
            "\n1. 原文里到底有没有可回答这个问题的事实？"
            "\n2. 送审证据（evidence_id / supporting_quote 指向的原文）里有没有这个事实？"
            "\n3. 这条答案的陈述，与原文的事实是否一致？"
            "   注意否定方向（原文“不是真品”，答“是真品”是错的）、"
            "主体对象（问令牌答人的去向）、"
            "以及答案是否越过原文给出了新断言。"
            "\n4. 答案是否满足答案标准（answer_criteria）要求的全部要素？只答了一半要标 partial。"
        ),
        "output_schema": {
            "work_id": "作品 id",
            "arm": "臂名",
            "candidate_id": "候选 id",
            "answer_correct": "yes / no / partial",
            "answer_correct_reason": "为什么（引用原文原句）",
            "source_has_answerable_fact": "yes / no",
            "evidence_delivered_fact": "yes / no / partial / na（证据根本没送到时）",
            "failure_category": (
                "none（答案正确）| evidence_not_delivered（原文有可答事实但证据没送到）"
                "| base_recheck_missed（证据到了但初次判定拒绝/漏答）"
                "| audit_false_kill（初次答案正确但升级审核错误拒绝）"
                "| answer_wrong_or_partial（原答案本身不对/不全，不算误杀）"
                "| engineering（判定对但状态流转或保存出错）"
            ),
            "notes": "补充说明",
        },
        "packages": packages,
        "chapter_text_by_work": {
            wid: {
                str(ch): (TRIAL_ROOT / wid / "blocks" / f"{ch:04d}.txt").read_text(encoding="utf-8")
                for ch in range(1, 13)
                if (TRIAL_ROOT / wid / "blocks" / f"{ch:04d}.txt").is_file()
            }
            for wid in WORK_IDS
        },
    }
    p = OUT_DIR / "t4-closeout-7-candidates.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p)
    print("packages:", len(packages))
    print("unique candidates:", len({(p["work_id"], p["candidate_id"]) for p in packages}))


if __name__ == "__main__":
    main()
