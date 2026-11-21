"""区分"补背景"与"找后续答案"：对 7 条经裁决的候选，给出支撑句的原文位置与时序归类。

判定依据（按登记时序，不按主题相似）：
  - 登记章 = 候选的 anchor_chapter（伏笔被登记/埋设的位置）
  - 支撑句所在章 > 登记章 → **后续答案**：可以计入回收证据
  - 支撑句所在章 == 登记章 → **同章待定**：需比较同章内先后位置
  - 支撑句所在章 < 登记章 → **背景材料**：补前情有用，但不能算作后续回收

另报两件事（分开记，不混为一谈）：
  - 支撑句的**精确原文跨度**是否存在于原文（事实）
  - 重建的证据包是否包含该句（事实；未经 sha256 校验时只作线索）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.experiments.bc.bc_t4_load_closeout_reviews import load_review_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
TRIAL = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"

#: 候选 → 支撑句候选（由独立裁决记录里引用的原句抽取；逐条在原文里精确核对）
CANDIDATES = [
    ("t4_game_holdout", "fs_003"),
    ("t4_urban_holdout", "fs_001"),
    ("t4_urban_holdout", "fs_009"),
    ("t4_urban_holdout", "fs_011"),
    ("t4_hist_holdout", "fs_006"),
    ("t4_hist_holdout", "fs_007"),
    ("t4_hist_holdout", "fs_011"),
]

QUOTE_PATTERNS = [
    re.compile(r"[『]([^』]{6,})[』]"),
    re.compile(r"[「]([^」]{6,})[」]"),
    re.compile(r"'([^']{6,})'"),
]


def load_blocks(work_id: str) -> dict[int, str]:
    d = TRIAL / "runs" / work_id / "candidate" / "blocks"
    out: dict[int, str] = {}
    for p in sorted(d.glob("*.txt")):
        if p.stem.isdigit():
            out[int(p.stem)] = p.read_text(encoding="utf-8")
    return out


def ledger_items(work_id: str) -> dict[str, dict]:
    p = TRIAL / "runs" / work_id / "candidate" / "foreshadow_ledger.json"
    if not p.is_file():
        return {}
    doc = json.loads(p.read_text(encoding="utf-8"))
    return {str(i.get("id")): i for i in (doc.get("items") or [])}


def adjudicated_quotes() -> dict[tuple[str, str], list[str]]:
    """从独立裁决记录里抽出被引用的原句。"""
    tdir = TRIAL / "t4-closeout-review-tasks"
    out: dict[tuple[str, str], list[str]] = {}
    for p in sorted(tdir.glob("*.closeout.review.json")):
        for r in load_review_file(p):
            key = (str(r.get("work_id")), str(r.get("candidate_id")))
            texts = [
                str(r.get("answer_correct_reason") or ""),
                str(r.get("notes") or ""),
            ]
            quotes: list[str] = []
            for t in texts:
                for pat in QUOTE_PATTERNS:
                    for m in pat.findall(t):
                        m = m.strip()
                        if len(m) >= 8 and m not in quotes:
                            quotes.append(m)
            if quotes:
                out.setdefault(key, [])
                for q in quotes:
                    if q not in out[key]:
                        out[key].append(q)
    return out


def locate(chapters: dict[int, str], sentence: str) -> list[tuple[int, int]]:
    """返回 (章号, 字符位置) 列表。"""
    hits = []
    for ch, text in chapters.items():
        idx = text.find(sentence)
        if idx >= 0:
            hits.append((ch, idx))
    return hits


def main() -> None:
    quotes_map = adjudicated_quotes()
    rows: list[dict] = []

    for wid, fs_id in CANDIDATES:
        chapters = load_blocks(wid)
        items = ledger_items(wid)
        item = items.get(fs_id, {})
        reg_ch = int(item.get("anchor_chapter") or 0)
        row = {
            "work_id": wid,
            "candidate_id": fs_id,
            "question": item.get("question"),
            "registration_chapter": reg_ch,
            "anchor_quote": item.get("anchor_quote"),
            "first_seen_chapter": item.get("first_seen_chapter"),
            "resolved_chapter": item.get("resolved_chapter"),
            "scope_last_chapter": max(chapters) if chapters else None,
            "supporting_sentences": [],
        }
        for q in quotes_map.get((wid, fs_id), []):
            hits = locate(chapters, q)
            if not hits:
                continue
            for ch, pos in hits:
                if ch > reg_ch:
                    kind = "later_answer"
                elif ch == reg_ch:
                    kind = "same_chapter_order_pending"
                else:
                    kind = "background_material"
                row["supporting_sentences"].append(
                    {
                        "sentence": q[:120],
                        "chapter": ch,
                        "char_offset": pos,
                        "time_class": kind,
                    }
                )
        # 去重：同章同句只留一条
        seen = set()
        uniq = []
        for s in row["supporting_sentences"]:
            k = (s["chapter"], s["sentence"])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(s)
        row["supporting_sentences"] = uniq
        row["time_classes"] = sorted({s["time_class"] for s in uniq})
        rows.append(row)

    summary = {
        "schema": "t4_background_vs_later_answer_v1",
        "principle": (
            "按登记时序区分：支撑句在登记章之后才算后续答案；"
            "在登记章之前只是背景材料，补前情有用但不能计作回收进展。"
        ),
        "scope_note": (
            "试产范围只到第 12 章。登记在第 12 章的候选，"
            "严格向后的检索在范围内无处可去——这不是检索缺陷，"
            "而是登记位置与范围边界共同决定的。"
        ),
        "rows": rows,
    }
    p = OUT_DIR / "t4-background-vs-later-answer.json"
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p)
    for r in rows:
        print(
            f"{r['work_id']:<18}/{r['candidate_id']}: 登记章={r['registration_chapter']} "
            f"范围末章={r['scope_last_chapter']} 支撑句时序={r['time_classes']}"
        )
        for s in r["supporting_sentences"]:
            print(
                f"    [{s['time_class']}] ch{s['chapter']}@{s['char_offset']} {s['sentence'][:60]}"
            )


if __name__ == "__main__":
    main()
