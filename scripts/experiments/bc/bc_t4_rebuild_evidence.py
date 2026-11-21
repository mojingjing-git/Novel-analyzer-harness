"""重建历史审核请求的证据包，并用缓存里记录的 prompt_sha256 校验。

目的：判定"证据未送达"还是"送到了却没判断出来"。
方法：审核提示词由 evidence_segments 确定性生成，
     重建后与缓存记录的 prompt_sha256 比对——一致即证明重建无误。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.common.c_claim_audit_gate import CLAIM_V2, build_nli_prompt  # noqa: E402
from backend.core.evidence import EvidenceIndex  # noqa: E402
from backend.core.text_units import build_display_labels, load_blocks_as_files  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
TRIAL = ROOT / "workspace/bc-evaluation/t4-trial-20261106/runs"
BLOCK_SIZE = 4


def rebuild(wid: str, arm: str, fs_id: str) -> dict:
    arm_dir = TRIAL / wid / arm
    units, _excluded = load_blocks_as_files(arm_dir / "blocks")
    index = EvidenceIndex(units)

    ledger = json.loads((arm_dir / "foreshadow_ledger.json").read_text(encoding="utf-8"))
    item = next(
        (i for i in (ledger.get("items") or []) if str(i.get("id")) == fs_id), None
    )
    if item is None:
        return {"status": "no_item"}

    vdoc = json.loads(
        (arm_dir / "output" / "recheck_verdicts.json").read_text(encoding="utf-8")
    )
    verdict = next(
        (v for v in (vdoc.get("verdicts") or []) if str(v.get("candidate_id")) == fs_id), None
    )
    if verdict is None:
        return {"status": "no_verdict"}

    enhanced = arm in ("b_fixed", "candidate")
    anchor_chapter = int(item.get("anchor_chapter") or 0)
    hint_texts = [
        item.get("question") or "",
        item.get("description") or "",
    ]
    nom_chapters: list[int] = []
    if enhanced:
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
        res_ch = int(item.get("resolved_chapter") or 0)
        if res_ch > anchor_chapter:
            nom_chapters.append(res_ch)
            for off in range(1, BLOCK_SIZE):
                if (res_ch - off) > anchor_chapter:
                    nom_chapters.append(res_ch - off)
        for ev_ch in (item.get("evidence_chapters") or []):
            if int(ev_ch or 0) > anchor_chapter:
                nom_chapters.append(int(ev_ch))

    kwargs = {}
    if enhanced:
        kwargs = {"nominated_chapters": nom_chapters, "density_window": True}
    bundle = index.bundle_for(
        anchor_chapter=anchor_chapter,
        anchor_quote=str(item.get("anchor_quote") or "").strip(),
        hint_texts=hint_texts,
        unit_noun="文件",
        display_labels=build_display_labels(units),
        **kwargs,
    )
    segments = []
    for chapter, start, end in bundle.spans:
        body = bundle.text[start:end].strip()
        segments.append({"evidence_id": _eid(chapter, body), "chapter": chapter, "text": body})

    audit_item = {
        "fs_id": fs_id,
        "question": item.get("question") or "",
        "criteria": item.get("answer_criteria") or "",
        "description": item.get("description") or "",
        "anchor_quote": str(item.get("anchor_quote") or ""),
        "anchor_chapter": anchor_chapter,
        "answer": verdict.get("answer") or "",
        "quote": verdict.get("quote") or "",
        "chapter": int(verdict.get("chapter") or anchor_chapter),
        "prereg": item.get("prereg"),
        "evidence_segments": segments,
        "evidence_contract_version": CLAIM_V2,
    }
    prompt = build_nli_prompt(audit_item)
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    recorded = None
    cache_p = arm_dir / "output" / "c_audit_cache.json"
    if cache_p.is_file():
        cd = json.loads(cache_p.read_text(encoding="utf-8"))
        for v in cd.values():
            if str(v.get("fs_id")) == fs_id:
                recorded = v
                break
    return {
        "status": "ok",
        "segments": segments,
        "prompt_sha256": prompt_sha,
        "recorded_prompt_sha256": (recorded or {}).get("prompt_sha256"),
        "matched": bool(
            recorded and recorded.get("prompt_sha256") == prompt_sha
        ),
        "verdict_answer": verdict.get("answer"),
        "verdict_quote": verdict.get("quote"),
        "item": item,
    }


def _eid(chapter: int, body: str) -> str:
    from backend.services.recheck_v2 import _evidence_id

    return _evidence_id(chapter, body)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-all", action="store_true", help="扫描全部臂的全部候选")
    ap.add_argument("--checks", help="JSON: {candidate_id: [关键词...]}，检查关键句是否在证据里")
    args = ap.parse_args()

    checks = json.loads(args.checks) if args.checks else {}

    if args.scan_all:
        rows = []
        for wid in ("t4_ind_holdout", "t4_game_holdout", "t4_urban_holdout", "t4_hist_holdout"):
            for arm in ("baseline", "b_fixed", "candidate"):
                arm_dir = TRIAL / wid / arm
                ledger_p = arm_dir / "foreshadow_ledger.json"
                if not ledger_p.is_file():
                    continue
                items = json.loads(ledger_p.read_text(encoding="utf-8")).get("items") or []
                for it in items:
                    fs_id = str(it.get("id") or "")
                    if not fs_id:
                        continue
                    try:
                        r = rebuild(wid, arm, fs_id)
                    except Exception as exc:  # noqa: BLE001
                        rows.append(
                            {
                                "work_id": wid,
                                "arm": arm,
                                "candidate_id": fs_id,
                                "status": f"error:{type(exc).__name__}",
                            }
                        )
                        continue
                    if r.get("status") != "ok":
                        continue
                    joined = "\n".join(s["text"] for s in r["segments"])
                    ks = checks.get(fs_id, [])
                    rows.append(
                        {
                            "work_id": wid,
                            "arm": arm,
                            "candidate_id": fs_id,
                            "status": "ok",
                            "matched": r.get("matched"),
                            "segment_count": len(r["segments"]),
                            "evidence_chars": len(joined),
                            "hits": [k for k in ks if k in joined],
                            "miss": [k for k in ks if k not in joined],
                            "segments": [
                                {"chapter": s["chapter"], "text": s["text"]}
                                for s in r["segments"]
                            ],
                        }
                    )
        p = ROOT / "workspace/bc-evaluation/t4-trial-20261106/t4-evidence-rebuild-all.json"
        p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print("saved:", p, "rows:", len(rows))
        return

    targets = [
        ("t4_hist_holdout", "candidate", "fs_006"),
        ("t4_hist_holdout", "candidate", "fs_011"),
        ("t4_hist_holdout", "candidate", "fs_007"),
        ("t4_urban_holdout", "candidate", "fs_011"),
        ("t4_game_holdout", "candidate", "fs_003"),
    ]
    out = []
    for wid, arm, fs_id in targets:
        r = rebuild(wid, arm, fs_id)
        out.append(
            {
                "work_id": wid,
                "arm": arm,
                "candidate_id": fs_id,
                "status": r.get("status"),
                "matched": r.get("matched"),
                "prompt_sha256": r.get("prompt_sha256"),
                "recorded_prompt_sha256": r.get("recorded_prompt_sha256"),
                "segment_count": len(r.get("segments") or []),
                "segments": [
                    {"chapter": s["chapter"], "text": s["text"][:1500]}
                    for s in (r.get("segments") or [])
                ],
            }
        )
        print(
            f"{wid}/{arm}/{fs_id}: matched={r.get('matched')} segs={len(r.get('segments') or [])}",
            flush=True,
        )
    p = ROOT / "workspace/bc-evaluation/t4-trial-20261106/t4-evidence-rebuild.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p)


if __name__ == "__main__":
    main()
