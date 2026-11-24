"""伏笔复检 V3 响应解析与引文定位工具（Layer 2：仅依赖 prompts 常量）。"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from backend.utils.json_utils import extract_json_from_text

from .prompts import ALLOWED_V3_TIERS


def _safe_int_unit(val: Any) -> int | None:
    try:
        return int(str(val or "").strip().lstrip("Uu"))
    except (TypeError, ValueError):
        return None


def _normalize_verdict(v: str) -> str:
    s = (v or "").strip().upper()
    if s in ALLOWED_V3_TIERS:
        return s
    if s in ("SUPPORTED", "RESOLVED"):
        return "RESOLVED_EXPLICIT"
    if s in ("IMPLICIT", "IMPLICIT_RESOLVED"):
        return "RESOLVED_IMPLICIT"
    if s in ("PARTIAL", "PARTIALLY"):
        return "PARTIALLY_RESOLVED"
    return "UNRESOLVED"


def _parse_json_payload(raw_text: str) -> Any:
    text = (raw_text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    stripped = re.sub(r"^```(?:json)?\s*", "", text)
    stripped = re.sub(r"\s*```\s*$", "", stripped).strip()
    try:
        return json.loads(stripped)
    except Exception:
        pass
    lb, rb = stripped.find("["), stripped.rfind("]")
    lc, rc = stripped.find("{"), stripped.rfind("}")
    candidates: list[str] = []
    if lb != -1 and rb > lb and (lc == -1 or lb < lc):
        candidates.append(stripped[lb : rb + 1])
    if lc != -1 and rc > lc:
        candidates.append(stripped[lc : rc + 1])
    if lb != -1 and rb > lb:
        candidates.append(stripped[lb : rb + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception:
            continue
    extracted = extract_json_from_text(text)
    if isinstance(extracted, str):
        try:
            return json.loads(extracted)
        except Exception:
            return None
    return extracted


def _salvage_json_array_rows(raw_text: str) -> dict[str, dict[str, Any]]:
    """从可能被输出截断的 JSON 数组中抢救已闭合的对象行（按 fs_id 建索引）。"""
    text = (raw_text or "").strip()
    start = text.find("[")
    if start < 0:
        return {}
    out: dict[str, dict[str, Any]] = {}
    in_str = False
    esc = False
    depth = 0
    obj_start = -1
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_start >= 0:
                    try:
                        obj = json.loads(text[obj_start : i + 1])
                        if isinstance(obj, Mapping) and obj.get("fs_id"):
                            out[str(obj["fs_id"])] = dict(obj)
                    except Exception:
                        pass
                    obj_start = -1
    return out


def _parse_rows_by_id(raw_text: str) -> dict[str, dict[str, Any]]:
    """解析模型返回的多条结果并按 fs_id 映射为字典。"""
    parsed = _parse_json_payload(raw_text)
    rows = (
        parsed
        if isinstance(parsed, list)
        else (
            [parsed]
            if isinstance(parsed, dict) and "fs_id" in parsed
            else (parsed.get("items") or parsed.get("results") or [] if isinstance(parsed, dict) else [])
        )
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, Mapping) and row.get("fs_id"):
            out[str(row["fs_id"])] = dict(row)
    return out


def _select_primary_evidence(
    evidence_chain: Sequence[Mapping[str, Any]],
    setup_unit: int = 0,
) -> Mapping[str, Any] | None:
    """选择主落账证据：优先设伏后兑现单元，仅当全为前驱回指时才回退首条。"""
    if not evidence_chain:
        return None
    if setup_unit > 0:
        post: list[tuple[int, int, Mapping[str, Any]]] = []
        for idx, ev in enumerate(evidence_chain):
            u = _safe_int_unit(ev.get("unit_id"))
            if u is not None and u >= setup_unit:
                role = str(ev.get("role") or "").strip().lower()
                is_non_backref = 0 if role != "causal_backref" else 1
                post.append((is_non_backref, u, ev))
        if post:
            post.sort(key=lambda r: (r[0], r[1]))
            return post[0][2]
    return evidence_chain[0]


def _normalize_for_match(s: str) -> str:
    s = re.sub(r"[\s\u3000]+", "", s or "")
    for a, b in [("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'")]:
        s = s.replace(a, b)
    return s


def _find_quote_in_unit(unit_text: str, quote: str) -> int | None:
    q = (quote or "").strip()
    if len(q) < 6 or not unit_text:
        return None
    pos = unit_text.find(q)
    if pos >= 0:
        return pos
    nq = _normalize_for_match(q)
    if len(nq) < 6:
        return None
    norm_chars: list[str] = []
    orig_idx: list[int] = []
    for i, ch in enumerate(unit_text):
        if re.match(r"[\s\u3000]", ch):
            continue
        if ch in ("“", "”"):
            norm_chars.append('"')
        elif ch in ("‘", "’"):
            norm_chars.append("'")
        else:
            norm_chars.append(ch)
        orig_idx.append(i)
    npos = "".join(norm_chars).find(nq)
    return orig_idx[npos] if npos >= 0 else None
