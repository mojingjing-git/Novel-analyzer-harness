"""共享关闭条件：预登记校验与逻辑归约，不从关键词重建语义。"""
from __future__ import annotations

import re

#: 分支文本低于该长度视为"碎分支"（主语/名物词），按 UNCERTAIN 转人工
MIN_BRANCH_CHARS = 6


def _norm_text(s: str) -> str:
    """校验用的归一化：去空白与常见分隔标点（不影响语义比较）。"""
    return re.sub(r"[\s，,。；;：:、（）()\"'“”‘’]", "", s or "")


def _is_subsequence(needle: str, haystack: str) -> bool:
    """needle 的字符是否按序出现在 haystack 里（允许补全谓词，禁止编造内容）。"""
    it = iter(haystack)
    return all(ch in it for ch in needle)


#: 合取截断检测：OR 可关闭分支若是 criteria 真前缀、余量以这些连词开头 → 拒绝。
#: 注意"及其"必须在"及"之前（否则 "及其…" 会被 "及" 提前匹配）。
CONJ_PREFIX_RE = re.compile(r"^(及其|以及|并|且|和|加上|及)")


def validate_prereg_row(row: dict, criteria: str) -> tuple[bool, str]:
    """预登记后验校验（fail-closed）：任一不满足即判 UNCERTAIN。

    - 分支非空，branch_id 唯一，text ≥ MIN_BRANCH_CHARS（防"丹阳子"式碎分支）；
    - 每个 branch text 的字符必须按序出现在 criteria 中（归一化子序列）——
      既拦编造内容，也允许"A/B 各自补全谓词"的合法展开；
    - closing：AND 需 ≥1 分支；OR 需 closable 非空且 ⊆ 已登记 branch_id，且
      closable 分支不得是 criteria 的"合取截断"（真前缀 + 余量以连词开头，
      fs_205 型"并…"、fs_935 型"及其…"）——只拦已知截断形式，不承担完整
      合取语义校验；UNCERTAIN 直接放行。
    """
    if not isinstance(row, dict) or not isinstance(criteria, str):
        return False, "invalid_registration"
    raw_branches = row.get("branches")
    if not isinstance(raw_branches, list) or any(not isinstance(b, dict) for b in raw_branches):
        return False, "invalid_branches"
    branches = raw_branches
    closing = row.get("closing") if isinstance(row.get("closing"), dict) else {}
    ctype = closing.get("type")
    if ctype == "UNCERTAIN":
        return True, "uncertain"
    if not branches:
        return False, "no_branches"
    norm_criteria = _norm_text(criteria)
    ids = [b.get("branch_id") for b in branches]
    if any(not isinstance(i, str) or not i.strip() for i in ids):
        return False, "invalid_branch_id"
    if len(set(ids)) != len(ids):
        return False, "duplicate_branch_id"
    for b in branches:
        if not isinstance(b.get("text"), str):
            return False, "invalid_branch_text"
        text = b["text"].strip()
        norm_branch = _norm_text(text)
        if len(norm_branch) < MIN_BRANCH_CHARS:
            return False, f"fragment_branch:{text[:12]}"
        if not _is_subsequence(norm_branch, norm_criteria):
            return False, f"branch_not_in_criteria:{text[:12]}"
    if ctype == "AND":
        return (True, "and") if len(branches) >= 1 else (False, "and_no_branches")
    if ctype == "OR":
        closable = closing.get("closable") or []
        if not isinstance(closable, list) or not closable or any(not isinstance(c, str) or c not in ids for c in closable):
            return False, "or_bad_closable"
        by_id = {b.get("branch_id"): _norm_text(b.get("text") or "") for b in branches}
        for c in closable:
            norm_b = by_id.get(c, "")
            if norm_b and norm_b != norm_criteria and norm_criteria.startswith(norm_b):
                rest = norm_criteria[len(norm_b):]
                if CONJ_PREFIX_RE.match(rest):
                    return False, f"prefix_cut_conjunction:{norm_b[:12]}"
        return True, "or"
    return False, f"bad_closing_type:{ctype}"


def closing_satisfied(closing: dict, branch_hits: list[dict], reg: dict) -> bool | None:
    """关闭条件是否满足。None = 无法判定（UNCERTAIN → 转人工）。

    AND 的 branch 全集取预登记的 reg["branches"]（PREREG 输出的 closing 里没有
    branches 字段——v2 曾因此恒 False，所有 AND 型 VERIFIED 全部 closing_unmet）。
    """
    ctype = closing.get("type")
    hit_ids = {b.get("branch_id") for b in branch_hits if isinstance(b, dict)}
    if ctype == "AND":
        need = {b.get("branch_id") for b in (reg.get("branches") or []) if isinstance(b, dict)}
        return bool(need) and need <= hit_ids
    if ctype == "OR":
        closable = set(closing.get("closable") or [])
        return bool(closable & hit_ids)
    return None  # UNCERTAIN：转人工


def validate_branch_hits(reg: dict, hits: object, segments: list[dict]) -> tuple[list[dict], list[str]]:
    """逐分支验证来源和唯一引文；返回的命中才可交给 closing_satisfied。

    这只证明证据可定位，命题是否被证据蕴含仍须语义判断。
    同一段可支持多个分支，每个分支必须分别给出引文。
    """
    known = {b["branch_id"] for b in reg.get("branches", []) if isinstance(b, dict) and isinstance(b.get("branch_id"), str)}
    texts = {s["evidence_id"]: s["text"] for s in segments}
    if not isinstance(hits, list) or not hits:
        return [], ["branch_hits_missing"]
    validated, errors, seen = [], [], set()
    for hit in hits:
        if not isinstance(hit, dict):
            errors.append("invalid_branch_hit")
            continue
        bid, eid, quote = hit.get("branch_id"), hit.get("evidence_id"), hit.get("supporting_quote")
        if not isinstance(bid, str) or bid not in known or bid in seen:
            errors.append("unknown_or_duplicate_branch")
            continue
        seen.add(bid)
        if not isinstance(eid, str) or eid not in texts:
            errors.append(f"{bid}:foreign_evidence_id")
            continue
        if not isinstance(quote, str) or len(quote.strip()) < 6:
            errors.append(f"{bid}:missing_branch_quote")
            continue
        quote = quote.strip()
        text = texts[eid]
        start = text.find(quote)
        if start < 0 or text.find(quote, start + 1) >= 0:
            errors.append(f"{bid}:quote_not_unique")
            continue
        validated.append({"branch_id": bid, "evidence_id": eid, "supporting_quote": quote,
                          "span": [start, start + len(quote)]})
    return validated, errors

