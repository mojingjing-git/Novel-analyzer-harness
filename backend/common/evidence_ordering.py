"""证据与锚点的先后关系判定（同一坐标系 + 原文哈希绑定）。

要回答的问题是"**支撑答案的那段原文，发生在问题（锚点）之前还是之后**"。
只比两段文字的起点是不够的——要看**完整跨度**的位置。

规则（刻意保守，宁可"时序待定"也不猜）：
  1. 锚点与证据都在**同一份原文**、**同一套字符坐标**下定位；
     原文按单元哈希绑定，坐标一旦对不上原文就作废。
  2. **定位必须唯一**：引文在原文里出现多次、定位失败 → 时序待定。
  3. 跨度**重叠** → 时序待定。
  4. 一条答案依赖多段证据时，**逐段**给出先后关系；只有**每一段**都在锚点之后，
     才算"全部在之后"。挑一段靠后的就算通过是不允许的。

判定结果只解除"时间疑点"：答案正确性、条件满足、信息增量仍由原门禁判断。
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

SCHEMA = "evidence_ordering_v1"

#: 段落标签形如 "【文件5（第 5 回）（埋设处）】" —— 证据段文本会带这个前缀，
#: 它不在原文里，定位时必须剥掉。
_LABEL_END = "】\n"

#: 证据窗口首尾的省略号标记（窗口被截断时加），同样不在原文里。
_ELLIPSIS = "…"


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _find_all(text: str, needle: str) -> list[int]:
    """全部出现位置（不能用 str.count：它对重叠出现会算错）。"""
    out: list[int] = []
    if not needle:
        return out
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx < 0:
            break
        out.append(idx)
        start = idx + 1
    return out


def _strip_markers(text: str) -> str:
    """剥掉证据段文本里的非原文部分，返回原文片段。

    证据段文本 = 标签前缀（可选）+ 窗口首尾省略号（可选）+ 原文片段。
    剥掉之后剩下的必须是原文的**连续**子串，否则定位必然失败——
    这正是我们要的：定位不到就时序待定，不猜。
    """
    t = text or ""
    if t.startswith("【"):
        end = t.find(_LABEL_END)
        if end >= 0:
            t = t[end + len(_LABEL_END) :]
    t = t.strip()
    if t.startswith(_ELLIPSIS):
        t = t[len(_ELLIPSIS) :]
    if t.endswith(_ELLIPSIS):
        t = t[: -len(_ELLIPSIS)]
    return t.strip()


def _unit_hashes(chapters: Mapping[int, str]) -> dict:
    return {str(k): _sha(v) for k, v in sorted(chapters.items(), key=lambda kv: int(kv[0]))}


def _combined_hash(unit_hashes: Mapping[str, str]) -> str:
    payload = "\n".join(f"{k}:{v}" for k, v in sorted(unit_hashes.items()))
    return _sha(payload)


def _locate_anchor(
    chapters: Mapping[int, str], anchor_quote: str
) -> dict:
    q = (anchor_quote or "").strip()
    if not q:
        return {"resolved": False, "reason": "empty_anchor_quote"}
    hits: list[tuple[int, int]] = []
    for unit in sorted(chapters, key=lambda k: int(k)):
        text = chapters[unit]
        for pos in _find_all(text, q):
            hits.append((unit, pos))
    if not hits:
        return {"resolved": False, "reason": "anchor_not_found"}
    if len(hits) > 1:
        return {
            "resolved": False,
            "reason": "anchor_not_unique",
            "occurrences": len(hits),
            "locations": [{"unit": u, "start": p} for u, p in hits[:8]],
        }
    unit, pos = hits[0]
    return {
        "resolved": True,
        "unit": int(unit),
        "start": pos,
        "end": pos + len(q),
        "occurrences": 1,
    }


def _locate_segment(
    chapters: Mapping[int, str], segment: Mapping[str, Any]
) -> dict:
    unit = segment.get("chapter")
    raw_text = str(segment.get("text") or "")
    body = _strip_markers(raw_text)
    if not body:
        return {"resolved": False, "reason": "empty_segment_text"}
    if unit is None or int(unit) not in chapters:
        return {"resolved": False, "reason": "segment_unit_missing"}
    text = chapters[int(unit)]
    positions = _find_all(text, body)
    if not positions:
        return {"resolved": False, "reason": "segment_text_not_found"}
    if len(positions) > 1:
        return {
            "resolved": False,
            "reason": "segment_text_not_unique",
            "occurrences": len(positions),
            "positions": positions[:8],
        }
    pos = positions[0]
    return {
        "resolved": True,
        "unit": int(unit),
        "start": pos,
        "end": pos + len(body),
        "occurrences": 1,
    }


def _relation(anchor: Mapping[str, Any], seg: Mapping[str, Any]) -> str:
    if not anchor.get("resolved") or not seg.get("resolved"):
        return "indeterminate"
    au = int(anchor["unit"])
    su = int(seg["unit"])
    a_start, a_end = int(anchor["start"]), int(anchor["end"])
    s_start, s_end = int(seg["start"]), int(seg["end"])
    # 锚点自身的窗口（埋设处）必然含住锚点：它是**锚点上下文**，
    # 不是"支撑答案的另一段材料"，不参与先后统计。
    if su == au and s_start <= a_start and s_end >= a_end:
        return "anchor_own"
    if su > au:
        return "after"
    if su < au:
        return "before"
    # 同单元：比较完整跨度
    if s_start >= a_end:
        return "after"
    if s_end <= a_start:
        return "before"
    return "overlap"


def _relation_span(
    anchor: Mapping[str, Any], unit: int, start: int, end: int
) -> str:
    """按章内绝对坐标判定一段引用相对锚点的先后。

    与窗口级判定不同，这里**不因窗口包住锚点而豁免**：
    引用与锚点完全相同时才算锚点自身。
    """
    if not anchor.get("resolved"):
        return "indeterminate"
    au = int(anchor["unit"])
    a_start, a_end = int(anchor["start"]), int(anchor["end"])
    if unit > au:
        return "after"
    if unit < au:
        return "before"
    if start == a_start and end == a_end:
        return "anchor_own"
    if start >= a_end:
        return "after"
    if end <= a_start:
        return "before"
    return "overlap"


def extract_citations(nli: Mapping[str, Any] | None) -> list[dict]:
    """从审核裁决里取出**它实际依赖的引用**。

    两类都要：
    - ``claim_audit.units[].evidence[]``：答案断言逐条依赖的证据；
    - ``branch_hits[]``：关闭分支依赖的引文。

    只看"证据包里有哪几个窗口"是不够的——窗口是来源容器，
    裁决未必用到窗口里的每一句；反过来，窗口里包住锚点，
    也不代表它内部的句子都能豁免时序检查。
    """
    out: list[dict] = []
    if not isinstance(nli, Mapping):
        return out
    units = ((nli.get("claim_audit") or {}).get("units") or [])
    for u in units:
        for ev in u.get("evidence") or []:
            quote = str(ev.get("quote") or "").strip()
            if not quote:
                continue
            out.append(
                {
                    "evidence_id": ev.get("evidence_id"),
                    "quote": quote,
                    "kind": "claim_unit",
                    "unit_id": u.get("unit_id"),
                    "relation": u.get("relation"),
                }
            )
    for bh in nli.get("branch_hits") or []:
        quote = str(bh.get("supporting_quote") or "").strip()
        if not quote:
            continue
        out.append(
            {
                "evidence_id": bh.get("evidence_id"),
                "quote": quote,
                "kind": "branch_hit",
                "branch_id": bh.get("branch_id"),
            }
        )
    return out


def _locate_quote_in_unit(text: str, quote: str) -> dict:
    positions = _find_all(text, quote)
    if not positions:
        return {"resolved": False, "reason": "quote_not_found"}
    if len(positions) > 1:
        return {
            "resolved": False,
            "reason": "quote_not_unique",
            "occurrences": len(positions),
        }
    pos = positions[0]
    return {"resolved": True, "start": pos, "end": pos + len(quote), "occurrences": 1}


def resolve_cited_ordering(
    *,
    chapters: Mapping[int, str] | None,
    anchor_quote: str,
    evidence_segments: Sequence[Mapping[str, Any]] | None,
    nli: Mapping[str, Any] | None,
) -> dict:
    """按**裁决实际引用的句子**判定先后关系。

    窗口只当来源容器：先定位窗口片段在章内的位置，再在窗口片段里定位被引用的句子。
    这样"窗口包住锚点"不会把窗口内部的句子一并豁免——
    答案若真在锚点之前，就会被判为"有在前"。
    """
    citations = extract_citations(nli)
    if not chapters:
        return {
            "schema": SCHEMA,
            "resolved": False,
            "verdict": "indeterminate",
            "reason": "no_source_text",
            "anchor": {"resolved": False, "reason": "no_source_text"},
            "citations": [],
            "segments": [],
            "summary": {
                "total": 0,
                "after": 0,
                "before": 0,
                "overlap": 0,
                "indeterminate": 0,
                "anchor_own": 0,
            },
        }

    unit_hashes = _unit_hashes(chapters)
    source = {
        "unit_hashes": unit_hashes,
        "combined_sha256": _combined_hash(unit_hashes),
        "unit_count": len(unit_hashes),
    }
    anchor = _locate_anchor(chapters, anchor_quote)

    # 窗口 → 章内位置（来源容器）
    seg_by_id: dict[str, dict] = {}
    for seg in evidence_segments or []:
        loc = _locate_segment(chapters, seg)
        eid = str(seg.get("evidence_id"))
        seg_by_id[eid] = {
            "evidence_id": seg.get("evidence_id"),
            "chapter": seg.get("chapter"),
            "resolved": bool(loc.get("resolved")),
            "start": loc.get("start"),
            "end": loc.get("end"),
            "locate_reason": loc.get("reason"),
            "snippet": _strip_markers(str(seg.get("text") or "")),
        }

    rows: list[dict] = []
    for cit in citations:
        eid = str(cit.get("evidence_id"))
        seg = seg_by_id.get(eid)
        row = {
            "evidence_id": cit.get("evidence_id"),
            "quote": cit.get("quote"),
            "kind": cit.get("kind"),
            "unit_id": cit.get("unit_id"),
            "branch_id": cit.get("branch_id"),
            "relation": "indeterminate",
            "resolved": False,
            "locate_reason": None,
            "chapter": (seg or {}).get("chapter"),
            "start": None,
            "end": None,
        }
        if seg is None:
            row["locate_reason"] = "evidence_id_unknown"
            rows.append(row)
            continue
        if not seg["resolved"]:
            row["locate_reason"] = f"segment_{seg['locate_reason']}"
            rows.append(row)
            continue
        unit = int(seg["chapter"])
        # 在**窗口片段内**定位被引用的句子；片段本身在章内的起点已知，
        # 因此可以换算成章内绝对坐标。
        hit = _locate_quote_in_unit(seg["snippet"], cit["quote"])
        if not hit.get("resolved"):
            row["locate_reason"] = hit.get("reason")
            rows.append(row)
            continue
        abs_start = int(seg["start"]) + int(hit["start"])
        abs_end = int(seg["start"]) + int(hit["end"])
        row["resolved"] = True
        row["start"] = abs_start
        row["end"] = abs_end
        row["relation"] = _relation_span(anchor, unit, abs_start, abs_end)
        rows.append(row)

    n_after = sum(1 for r in rows if r["relation"] == "after")
    n_before = sum(1 for r in rows if r["relation"] == "before")
    n_overlap = sum(1 for r in rows if r["relation"] == "overlap")
    n_indet = sum(1 for r in rows if r["relation"] == "indeterminate")
    n_anchor_own = sum(1 for r in rows if r["relation"] == "anchor_own")

    if not anchor.get("resolved"):
        verdict = "indeterminate"
        reason = f"anchor_{anchor.get('reason')}"
    elif not rows:
        verdict = "indeterminate"
        reason = "no_citations"
    elif n_before:
        verdict = "any_before"
        reason = f"{n_before} citation(s) before anchor"
    elif n_overlap:
        verdict = "indeterminate"
        reason = f"{n_overlap} citation(s) overlap anchor"
    elif n_indet:
        verdict = "indeterminate"
        reason = f"{n_indet} citation(s) not uniquely located"
    elif n_after == len(rows):
        verdict = "all_after"
        reason = "all cited spans after anchor"
    else:
        verdict = "indeterminate"
        reason = "no later citations to judge"

    return {
        "schema": SCHEMA,
        "resolved": verdict in ("all_after", "any_before"),
        "verdict": verdict,
        "reason": reason,
        "source": source,
        "anchor": {
            "quote": (anchor_quote or "")[:120],
            "resolved": bool(anchor.get("resolved")),
            "unit": anchor.get("unit"),
            "start": anchor.get("start"),
            "end": anchor.get("end"),
            "occurrences": anchor.get("occurrences"),
            "locate_reason": anchor.get("reason"),
        },
        "citations": rows,
        "segments": [
            {
                "evidence_id": s["evidence_id"],
                "chapter": s["chapter"],
                "resolved": s["resolved"],
                "start": s["start"],
                "end": s["end"],
                "locate_reason": s["locate_reason"],
            }
            for s in seg_by_id.values()
        ],
        "summary": {
            "total": len(rows),
            "after": n_after,
            "before": n_before,
            "overlap": n_overlap,
            "indeterminate": n_indet,
            "anchor_own": n_anchor_own,
        },
    }


def verify_ordering_record(
    record: Mapping[str, Any],
    *,
    chapters: Mapping[int, str] | None,
    anchor_quote: str,
    evidence_segments: Sequence[Mapping[str, Any]] | None,
    nli: Mapping[str, Any] | None = None,
) -> dict:
    """从落盘记录复算并比对——用于确认"保存后恢复"的结果一致。

    记录里绑定了原文哈希：复算前先核对哈希，坐标对不上原文就作废。
    """
    recorded_source = (record.get("source") or {}) if isinstance(record, Mapping) else {}
    if not chapters:
        return {"matched": False, "reason": "no_source_text"}
    unit_hashes = _unit_hashes(chapters)
    combined = _combined_hash(unit_hashes)
    if recorded_source.get("combined_sha256"):
        if recorded_source["combined_sha256"] != combined:
            return {
                "matched": False,
                "reason": "source_hash_mismatch",
                "recorded": recorded_source["combined_sha256"],
                "recomputed": combined,
            }
    if nli is None:
        return {"matched": False, "reason": "nli_required"}
    recomputed = resolve_cited_ordering(
        chapters=chapters,
        anchor_quote=anchor_quote,
        evidence_segments=evidence_segments,
        nli=nli,
    )
    same_verdict = recomputed.get("verdict") == record.get("verdict")
    same_rows = [
        (s.get("evidence_id"), s.get("relation"), s.get("start"), s.get("end"))
        for s in (recomputed.get("citations") or [])
    ] == [
        (s.get("evidence_id"), s.get("relation"), s.get("start"), s.get("end"))
        for s in (record.get("citations") or [])
    ]
    return {
        "matched": bool(same_verdict and same_rows),
        "reason": "ok" if (same_verdict and same_rows) else "ordering_changed",
        "recorded_verdict": record.get("verdict"),
        "recomputed_verdict": recomputed.get("verdict"),
        "source_hash_matched": recorded_source.get("combined_sha256") == combined,
    }
