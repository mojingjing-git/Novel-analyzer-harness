"""门禁回放：把时序判定接进完整门禁，记录**门禁自己的**结论。

用途：验证"时序通过只解除时间疑点，答案正确性/条件满足/信息增量仍由原门禁判断"。
`game/fs_003` 是回归案例——**不以"它最终必须通过"作为验收目标**，
这里只如实记录门禁在有时序结果与无时序结果两种情况下的判定差异。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.common.c_claim_audit_gate import gate  # noqa: E402
from backend.common.evidence_ordering import resolve_cited_ordering  # noqa: E402
from backend.common.recheck_request_log import sha256_text  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
TRIAL = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"

CASES = [
    ("t4_game_holdout", "fs_003", "candidate"),
]


def _units(wid: str, arm: str) -> dict[int, str]:
    from backend.core.text_units import load_blocks_as_files

    units, _ = load_blocks_as_files(TRIAL / "runs" / wid / arm / "blocks")
    return units


def _segments(wid: str, arm: str, item: dict) -> list[dict]:
    """按**生产参数**重建证据包，让回放忠实。

    参数必须与当初那次运行一致（unit_noun / display_labels / nominated_chapters /
    density_window），否则证据 id 对不上审核回包里的引用，回放会退化成
    "引文在别的证据里"，盖住真正要看的那一层。
    """
    from backend.core.evidence import EvidenceIndex
    from backend.core.text_units import build_display_labels
    from backend.services.recheck_v2 import _evidence_id

    units = _units(wid, arm)
    index = EvidenceIndex(units)
    display_labels = build_display_labels(units)
    anchor_ch = int(item.get("anchor_chapter") or 0)
    anchor_q = str(item.get("anchor_quote") or "").strip()
    hint_list = [
        str(item.get("question") or ""),
        str(item.get("description") or ""),
    ]
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
        anchor_quote=anchor_q,
        hint_texts=hint_list,
        unit_noun="文件",
        display_labels=display_labels,
        nominated_chapters=nom,
        density_window=True,
    )
    segs = []
    for ch, s, e in bundle.spans:
        body = bundle.text[s:e].strip()
        segs.append(
            {
                "evidence_id": _evidence_id(ch, body),
                "chapter": ch,
                "text": body,
            }
        )
    return segs


def _audit_response(wid: str, arm: str, cid: str) -> str:
    """取该候选的审核回包原文（来自缓存的 c_audit_cache）。"""
    for p in TRIAL.glob(f"runs/{wid}/{arm}/output/c_audit_cache.json"):
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
    out = []
    for wid, cid, arm in CASES:
        ledger = json.loads(
            (TRIAL / "runs" / wid / arm / "foreshadow_ledger.json").read_text(encoding="utf-8")
        )
        item = next(i for i in ledger["items"] if str(i.get("id")) == cid)
        units = _units(wid, arm)
        segs = _segments(wid, arm, item)
        raw = _audit_response(wid, arm, cid)

        from backend.common.c_claim_audit_gate import extract_json_object_with_repair

        nli, repair = extract_json_object_with_repair(raw) if raw else (None, {})

        # 从**复检明细**取真实答案与引文：门禁要核"引文能否在证据里定位"、
        # "答案与 claim units 是否对应"。用拼凑的占位值会让回放退化成
        # "引文未定位"，盖住时序这一层的变化。
        answer = ""
        quote = ""
        vpath = TRIAL / "runs" / wid / arm / "output" / "recheck_verdicts.json"
        if vpath.is_file():
            vdoc = json.loads(vpath.read_text(encoding="utf-8"))
            vrows = vdoc if isinstance(vdoc, list) else (vdoc.get("verdicts") or [])
            for vr in vrows:
                if str(vr.get("candidate_id")) == cid:
                    answer = str(vr.get("answer") or "")
                    quote = str(vr.get("quote") or "")
                    break

        # 时序按**裁决实际引用的句子**算，不是按检索窗口
        ordering = resolve_cited_ordering(
            chapters=units,
            anchor_quote=str(item.get("anchor_quote") or ""),
            evidence_segments=segs,
            nli=nli,
        )

        base_item = {
            "fs_id": cid,
            "question": str(item.get("question") or ""),
            "criteria": str(item.get("answer_criteria") or ""),
            "description": str(item.get("description") or ""),
            "anchor_quote": str(item.get("anchor_quote") or ""),
            "anchor_chapter": int(item.get("anchor_chapter") or 0),
            "answer": answer,
            "quote": quote,
            "chapter": segs[0]["chapter"] if segs else 0,
            "evidence_segments": segs,
            "evidence_contract_version": "claims_v2",
        }
        without = gate(dict(base_item), dict(nli or {}))
        with_ordering = gate({**base_item, "evidence_ordering": ordering}, dict(nli or {}))

        out.append(
            {
                "work_id": wid,
                "candidate_id": cid,
                "arm": arm,
                "nli_verdict": (nli or {}).get("verdict"),
                "parse_repair": repair,
                "ordering_verdict": ordering["verdict"],
                "ordering_summary": ordering["summary"],
                "gate_without_ordering": {
                    "decision": without.get("decision"),
                    "assessment_status": without.get("assessment_status"),
                    "reasons": without.get("reasons"),
                },
                "gate_with_ordering": {
                    "decision": with_ordering.get("decision"),
                    "assessment_status": with_ordering.get("assessment_status"),
                    "reasons": with_ordering.get("reasons"),
                },
                "note": (
                    "两次回放只差 evidence_ordering 一个字段。"
                    "时序通过解除的是时间疑点；门禁的其他判定（答案正确性、条件满足、"
                    "信息增量）不受影响。本案例不设“必须通过”为验收目标。"
                ),
            }
        )

    p = OUT_DIR / "t4-gate-replay-with-ordering.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p)
    for r in out:
        print(f"{r['work_id']}/{r['candidate_id']} nli={r['nli_verdict']} ordering={r['ordering_verdict']}")
        print(f"  无时序: {r['gate_without_ordering']}")
        print(f"  有时序: {r['gate_with_ordering']}")


if __name__ == "__main__":
    main()
