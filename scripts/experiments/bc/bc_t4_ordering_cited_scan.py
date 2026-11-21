"""按**裁决实际引用的句子**重扫时序判定（引用级，不调用模型、不扩试产）。

窗口级统计不能作为通过证据——窗口只是来源容器，
裁决未必用到窗口里的每一句，窗口包住锚点也不该把内部句子一并豁免。
这里用审核回包里 claim_audit / branch_hits 的实际引用来定位支撑跨度。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.common.c_claim_audit_gate import extract_json_object_with_repair  # noqa: E402
from backend.common.evidence_ordering import (  # noqa: E402
    extract_citations,
    resolve_cited_ordering,
    verify_ordering_record,
)

ROOT = Path(__file__).resolve().parents[3]
TRIAL = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"


def _units(wid: str, arm: str) -> dict[int, str]:
    from backend.core.text_units import load_blocks_as_files

    units, _ = load_blocks_as_files(TRIAL / "runs" / wid / arm / "blocks")
    return units


def _segments(wid: str, arm: str, item: dict) -> list[dict]:
    """按**生产参数**重建证据包，保证证据 id 与当初那次运行一致。"""
    from backend.core.evidence import EvidenceIndex
    from backend.core.text_units import build_display_labels
    from backend.services.recheck_v2 import _evidence_id

    units = _units(wid, arm)
    index = EvidenceIndex(units)
    anchor_ch = int(item.get("anchor_chapter") or 0)
    nom: list[int] = []
    res_ch = int(item.get("resolved_chapter") or 0)
    if res_ch > anchor_ch:
        nom.append(res_ch)
        for off in range(1, 4):
            if (res_ch - off) > anchor_ch:
                nom.append(res_ch - off)
    for ev_ch in item.get("evidence_chapters") or []:
        if int(ev_ch or 0) > anchor_ch:
            nom.append(int(ev_ch))
    bundle = index.bundle_for(
        anchor_chapter=anchor_ch,
        anchor_quote=str(item.get("anchor_quote") or "").strip(),
        hint_texts=[str(item.get("question") or ""), str(item.get("description") or "")],
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


def _audit_response(wid: str, arm: str, cid: str) -> str:
    p = TRIAL / "runs" / wid / arm / "output" / "c_audit_cache.json"
    if not p.is_file():
        return ""
    doc = json.loads(p.read_text(encoding="utf-8"))
    for k, v in doc.items():
        if not isinstance(v, dict):
            continue
        if k.startswith(cid) or v.get("fs_id") == cid:
            raw = v.get("raw_response_text")
            if raw:
                return raw
    return ""


def main() -> None:
    attr = json.loads(
        (OUT_DIR / "t4-closeout-evidence-attribution.json").read_text(encoding="utf-8")
    )
    seen: set[tuple[str, str]] = set()
    rows = []
    for r in attr.get("rows") or []:
        key = (r["work_id"], r["candidate_id"])
        if key in seen:
            continue
        seen.add(key)
        wid, cid = key
        arm = "candidate"
        units = _units(wid, arm)
        ledger = json.loads(
            (TRIAL / "runs" / wid / arm / "foreshadow_ledger.json").read_text(encoding="utf-8")
        )
        item = next(
            (i for i in ledger.get("items") or [] if str(i.get("id")) == cid), None
        )
        if item is None:
            continue
        segs = _segments(wid, arm, item)
        raw = _audit_response(wid, arm, cid)
        nli, repair = extract_json_object_with_repair(raw) if raw else (None, {})
        citations = extract_citations(nli)
        rec = resolve_cited_ordering(
            chapters=units,
            anchor_quote=str(item.get("anchor_quote") or ""),
            evidence_segments=segs,
            nli=nli,
        )
        restored = json.loads(json.dumps(rec, ensure_ascii=False))
        v = verify_ordering_record(
            restored,
            chapters=units,
            anchor_quote=str(item.get("anchor_quote") or ""),
            evidence_segments=segs,
            nli=nli,
        )
        rows.append(
            {
                "work_id": wid,
                "candidate_id": cid,
                "arm": arm,
                "anchor_chapter": int(item.get("anchor_chapter") or 0),
                "nli_verdict": (nli or {}).get("verdict"),
                "parse_repair_applied": bool(repair.get("applied")),
                "citation_count": len(citations),
                "ordering": rec,
                "save_restore_verification": v,
            }
        )

    out = OUT_DIR / "t4-evidence-ordering-cited-scan.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", out)
    from collections import Counter

    print("分布:", dict(Counter(r["ordering"]["verdict"] for r in rows)))
    for r in rows:
        o = r["ordering"]
        print(
            f"  {r['work_id']}/{r['candidate_id']} nli={r['nli_verdict']} "
            f"引用数={r['citation_count']} verdict={o['verdict']} "
            f"summary={o['summary']} restore={r['save_restore_verification']['matched']}"
        )
        for c in o["citations"]:
            print(
                f"      {c['kind']:<10} ch{c['chapter']} {c['relation']:<12} "
                f"[{c['start']},{c['end']}) {c['locate_reason'] or ''} {c['quote'][:30]!r}"
            )


if __name__ == "__main__":
    main()
