"""在现有曝光样本上做两件事（不扩试产、不调模型）：

1. **诊断锚点是否选晚**：对同一候选，换用"内容首次出现"的章作锚点重建检索，
   看证据包会多出什么。目的是判断"证据没找到"是检索缺陷还是登记时序问题。
2. **同章先后判定**：证据与锚点同章时，比较字符偏移，给出先后关系。
   现有门禁的 `evidence_not_after_anchor` 是**章粒度**的，同章一律标记，
   无法表达"同章但在后"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.core.evidence import EvidenceIndex  # noqa: E402
from backend.core.text_units import build_display_labels, load_blocks_as_files  # noqa: E402
from backend.services.recheck_v2 import _evidence_id  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
TRIAL = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"
BLOCK_SIZE = 4


def load_arm(wid: str, arm: str = "candidate"):
    arm_dir = TRIAL / "runs" / wid / arm
    units, _ = load_blocks_as_files(arm_dir / "blocks")
    ledger = json.loads((arm_dir / "foreshadow_ledger.json").read_text(encoding="utf-8"))
    items = {str(i.get("id")): i for i in (ledger.get("items") or [])}
    return arm_dir, units, items


def build_bundle(units, item, anchor_chapter: int, anchor_quote: str):
    index = EvidenceIndex(units)
    hint_texts = [
        item.get("question") or "",
        item.get("description") or "",
        item.get("answer_criteria") or "",
        item.get("resolution") or "",
        (
            ""
            if str(item.get("entity_key") or "").startswith("clue:")
            else str(item.get("entity_key") or "")
        ),
    ]
    nom: list[int] = []
    res_ch = int(item.get("resolved_chapter") or 0)
    if res_ch > anchor_chapter:
        nom.append(res_ch)
        for off in range(1, BLOCK_SIZE):
            if (res_ch - off) > anchor_chapter:
                nom.append(res_ch - off)
    for ev_ch in (item.get("evidence_chapters") or []):
        if int(ev_ch or 0) > anchor_chapter:
            nom.append(int(ev_ch))
    bundle = index.bundle_for(
        anchor_chapter=anchor_chapter,
        anchor_quote=anchor_quote.strip(),
        hint_texts=hint_texts,
        unit_noun="文件",
        display_labels=build_display_labels(units),
        nominated_chapters=nom,
        density_window=True,
    )
    segs = []
    for ch, s, e in bundle.spans:
        body = bundle.text[s:e].strip()
        segs.append({"evidence_id": _evidence_id(ch, body), "chapter": ch, "text": body})
    return segs


def main() -> None:
    results: list[dict] = []

    # ---- 1. hist/fs_011：现锚点（ch12） vs 内容首现章（ch11） ----
    wid, fs_id = "t4_hist_holdout", "fs_011"
    _arm_dir, units, items = load_arm(wid)
    item = items[fs_id]
    blocks_dir = TRIAL / "runs" / wid / "candidate" / "blocks"
    chapters = {
        int(p.stem): p.read_text(encoding="utf-8")
        for p in blocks_dir.glob("*.txt")
        if p.stem.isdigit()
    }
    key_sentence = "他想扳倒三衙禁军的将领，以确保他传承权力一路坦途。"
    ch11_pos = chapters[11].find(key_sentence)

    variants = []
    for label, anchor_ch, anchor_q in (
        ("as_recorded_ch12", int(item.get("anchor_chapter") or 0), str(item.get("anchor_quote") or "")),
        ("content_first_ch11", 11, key_sentence),
    ):
        segs = build_bundle(units, item, anchor_ch, anchor_q)
        joined = "\n".join(s["text"] for s in segs)
        variants.append(
            {
                "variant": label,
                "anchor_chapter": anchor_ch,
                "anchor_quote": anchor_q[:60],
                "evidence_chapters": [s["chapter"] for s in segs],
                "evidence_chars": len(joined),
                "contains_key_sentence": key_sentence in joined,
            }
        )
    results.append(
        {
            "work_id": wid,
            "candidate_id": fs_id,
            "question": item.get("question"),
            "registration_chapter_as_recorded": int(item.get("anchor_chapter") or 0),
            "first_seen_chapter": item.get("first_seen_chapter"),
            "last_seen_chapter": item.get("last_seen_chapter"),
            "key_sentence": key_sentence,
            "key_sentence_chapter": 11,
            "key_sentence_offset": ch11_pos,
            "key_sentence_time_class_vs_recorded_registration": "background_material",
            "scope_last_chapter": max(chapters),
            "variants": variants,
            "reading": (
                "登记在第 12 章、范围内没有第 13 章之后，严格向后的检索无处可去；"
                "关键句在第 11 章，属登记前材料，可作背景但不能计作回收进展。"
                "换用内容首现章作锚点只是诊断用——它改变的是登记时序，不是检索范围。"
            ),
        }
    )

    # ---- 2. game/fs_003：同章内先后 ----
    wid, fs_id = "t4_game_holdout", "fs_003"
    _arm_dir, units, items = load_arm(wid)
    item = items[fs_id]
    chapters = {
        int(p.stem): p.read_text(encoding="utf-8")
        for p in (TRIAL / "runs" / wid / "candidate" / "blocks").glob("*.txt")
        if p.stem.isdigit()
    }
    anchor_q = str(item.get("anchor_quote") or "")
    anchor_ch = int(item.get("anchor_chapter") or 0)
    anchor_text = chapters.get(anchor_ch, "")
    anchor_pos = anchor_text.find(anchor_q)
    segs = build_bundle(units, item, anchor_ch, anchor_q)
    ev_pos = []
    for s in segs:
        if s["chapter"] != anchor_ch:
            continue
        for probe in ("于是这些积年的怨气就化为了感激，升华为了信仰之力",):
            # 同一坐标系：章内字符偏移
            idx = anchor_text.find(probe)
            if idx >= 0:
                ev_pos.append(
                    {
                        "sentence": probe[:40],
                        "chapter_offset": idx,
                        "segment_offset": s["text"].find(probe),
                    }
                )
    order = "indeterminate"
    if anchor_pos >= 0 and ev_pos:
        order = (
            "evidence_after_anchor"
            if ev_pos[0]["chapter_offset"] > anchor_pos
            else "evidence_before_anchor"
        )
    results.append(
        {
            "work_id": wid,
            "candidate_id": fs_id,
            "registration_chapter": anchor_ch,
            "anchor_quote": anchor_q,
            "anchor_offset_in_chapter": anchor_pos,
            "evidence_chapters": [s["chapter"] for s in segs],
            "same_chapter_evidence_offsets": ev_pos,
            "same_chapter_order": order,
            "note": (
                "现有门禁 `evidence_not_after_anchor` 只比较章号（chapter <= anchor_ch 即标记），"
                "同章一律判软疑点，看不到同章内的字符先后。"
                "这里给出字符级先后，供后续设计同章判定时参考——不为此删时间边界。"
            ),
        }
    )

    p = OUT_DIR / "t4-background-vs-later-prototype.json"
    p.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
