"""答案分项证据契约。仅做结构/定位/声明一致性核验，不声称证明语义。"""
from __future__ import annotations

VERSION = "claims_v1"
SCOPES = frozenset({"event", "attributed", "hypothetical", "dream", "planned", "unknown"})
RELATIONS = frozenset({"supported", "partial", "contradicted", "insufficient"})

PROMPT = """
本条还必须返回 claim_audit（claims_v1）。不能用整体 answer_supported 代替逐项取证。
将候选答案按实质断言拆成有序 units；每项 answer_text 是候选答案的连续原文，
所有 answer_text 顺序拼接必须逐字等于完整候选答案（包含标点、空白；不能遗漏限定语）。
每项返回 relation: supported/partial/contradicted/insufficient；
answer_scope 与 evidence_scope: event=叙述层实际事件，attributed=某人的说法或判断，
hypothetical=假设，dream=梦中事件，planned=计划而未实现，unknown=无法确定。
attributed 时分别给 answer_holder/evidence_holder（统一为原文中同一说话者的名字）。
same_referent 必须明确判断答案与证据是否指向同一人物/物件、事件、时段；不确定填 false。
每项 evidence 是数组，每个元素含 evidence_id 与逐字唯一 quote（至少6字）；
supported 必须有证据。无依据可返回 insufficient 和空 evidence，不许编造引文。
即使引文真实，梦境/假设/计划/转述也不能支持无条件现实事实。对转述应保留说话者。
结构示意（不是正文）："claim_audit":{"version":"claims_v1","units":[
{"answer_text":"候选答案中的连续文字", "relation":"supported", "answer_scope":"event",
"evidence_scope":"event", "same_referent":true,
"evidence":[{"evidence_id":"原文段ID","quote":"原文逐字引文"}]}]}
"""


def validate_claim_audit(answer: str, segments: object, audit: object) -> dict:
    """Fail closed, including malformed model JSON and omitted answer clauses.

    `eligible` means the *declared* semantic assessments are consistent and every
    answer character has traceable evidence. It is not a ground-truth label.
    """
    errors, blockers, located = [], [], []
    texts = {}
    if not isinstance(segments, list):
        segments = []
        errors.append("invalid_segments")
    for segment in segments:
        if not isinstance(segment, dict):
            errors.append("invalid_segment")
            continue
        eid, text = segment.get("evidence_id"), segment.get("text")
        if not isinstance(eid, str) or not eid or not isinstance(text, str) or not text:
            errors.append("invalid_segment")
        elif eid in texts:
            errors.append("duplicate_evidence_id")
        else:
            texts[eid] = text
    if not isinstance(audit, dict) or audit.get("version") != VERSION:
        return {"eligible": False, "errors": errors + ["missing_or_unknown_claim_contract"],
                "blockers": [], "units": []}
    units = audit.get("units")
    if not isinstance(units, list) or not 1 <= len(units) <= 64:
        return {"eligible": False, "errors": errors + ["invalid_claim_units"], "blockers": [], "units": []}
    parts, cursor = [], 0
    for i, unit in enumerate(units):
        prefix = f"unit_{i}:"
        if not isinstance(unit, dict) or not isinstance(unit.get("answer_text"), str) or not unit["answer_text"].strip():
            errors.append(prefix + "invalid_answer_text")
            continue
        part = unit["answer_text"]
        parts.append(part)
        row = {"answer_text": part, "answer_span": [cursor, cursor + len(part)], "evidence": []}
        cursor += len(part)
        relation = unit.get("relation")
        if not isinstance(relation, str) or relation not in RELATIONS:
            errors.append(prefix + "invalid_relation")
        elif relation != "supported":
            blockers.append(prefix + relation)
        a_scope, e_scope = unit.get("answer_scope"), unit.get("evidence_scope")
        if any(not isinstance(s, str) or s not in SCOPES for s in (a_scope, e_scope)):
            errors.append(prefix + "invalid_scope")
        elif "unknown" in (a_scope, e_scope) or a_scope != e_scope:
            blockers.append(prefix + "scope_unconfirmed_or_changed")
        elif a_scope == "attributed":
            holder = unit.get("answer_holder")
            if not isinstance(holder, str) or not holder.strip() or holder != unit.get("evidence_holder"):
                blockers.append(prefix + "attribution_unconfirmed_or_changed")
        if unit.get("same_referent") is not True:
            blockers.append(prefix + "referent_unconfirmed")
        refs = unit.get("evidence")
        if not isinstance(refs, list):
            errors.append(prefix + "invalid_evidence")
            refs = []
        if relation == "supported" and not refs:
            errors.append(prefix + "supported_without_evidence")
        seen = set()
        for ref in refs:
            if not isinstance(ref, dict):
                errors.append(prefix + "invalid_reference")
                continue
            eid, quote = ref.get("evidence_id"), ref.get("quote")
            if not isinstance(eid, str) or eid not in texts:
                errors.append(prefix + "foreign_evidence_id")
                continue
            if not isinstance(quote, str) or len(quote.strip()) < 6:
                errors.append(prefix + "missing_quote")
                continue
            key = (eid, quote)
            start = texts[eid].find(quote)
            if key in seen or start < 0 or texts[eid].find(quote, start + 1) >= 0:
                errors.append(prefix + "duplicate_or_unlocated_quote")
                continue
            seen.add(key)
            row["evidence"].append({"evidence_id": eid, "quote": quote, "span": [start, start + len(quote)]})
        located.append(row)
    if not isinstance(answer, str) or not answer.strip() or "".join(parts) != answer:
        errors.append("answer_not_fully_partitioned")
    return {"eligible": not errors and not blockers, "errors": errors, "blockers": blockers, "units": located}
