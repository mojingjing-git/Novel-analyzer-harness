"""固定答案片段 + 正交限定维度；仍只验证模型声明，不证明语义真值。"""
from __future__ import annotations

import hashlib
import json
import re

from backend.common.claim_evidence import validate_claim_audit

VERSION = "claims_v2"
FRAME_VALUES = {
    "attribution": {"narrated", "attributed", "unknown"},
    "world": {"actual", "dream", "hypothetical", "unknown"},
    "modality": {"factual", "planned", "possible", "unknown"},
    "realization": {"not_established", "in_progress", "completed", "unspecified", "unknown"},
}


def answer_units(answer: str) -> list[dict]:
    """Deterministic surface clauses, not a claim of semantic atomicity.

    Bound IDs to the entire answer so a response to a different answer cannot
    silently reuse the same IDs. Preserve every character, including punctuation.
    """
    if not isinstance(answer, str) or not answer.strip():
        return []
    prefix = hashlib.sha256(answer.encode("utf-8")).hexdigest()[:12]
    ends = [m.end() for m in re.finditer(r"[，,。！？!?；;\n]+[”’」』\"']*", answer)]
    if not ends or ends[-1] != len(answer):
        ends.append(len(answer))
    spans, start = [], 0
    for end in ends:
        part = answer[start:end]
        if not spans and not any(ch.isalnum() for ch in part):
            continue
        if spans and not any(ch.isalnum() for ch in part):
            spans[-1][1] = end
        else:
            spans.append([start, end])
        start = end
    return [{"unit_id": f"{prefix}_u{i+1}", "text": answer[start:end], "span": [start, end]}
            for i, (start, end) in enumerate(spans)]


PROMPT = """
还必须返回 claim_audit，version="claims_v2"。下方固定片段由程序划分，你不得重新划分或抄写答案，
只按 unit_id 对每个片段取证。每个编号恰好一次，不可省略不利片段。
这些片段不保证是独立命题，必须结合完整答案理解：冒号前的“某人计划/认为/命令”等限定
作用于后续并列片段；不要因为后片段省略说话者或时态，就取消上文限定。
每项包含 unit_id、relation(supported/partial/contradicted/insufficient)、same_referent(boolean)、
answer_frame、evidence_frame、evidence([{evidence_id,quote}])。supported必须有逐字唯一引文，至少6字。
两个 frame 各自填写下面互相独立的维度，不能把“谁说的”和“是否计划”当作互斥类别：
- attribution: narrated=叙述层事实，attributed=某个角色的断言/命令/看法，unknown；
  holder: attributed时必须填明确的同一人物名，否则空字符串。
- world: actual=故事当前实际世界，dream=梦中世界，hypothetical=假设世界，unknown。
- modality: factual=作为事实陈述，planned=计划/命令/承诺做某事，possible=可能/推测，unknown。
- realization: not_established=未证实已经实现（计划或命令），in_progress=正在进行，completed=已实现，
  unspecified=本片段不主张执行进度，unknown=材料不足以判断。
例如某人宣布计划，attribution可以是attributed且modality为planned，realization为not_established；
说过“去做”不代表“做完”。这些分类针对片段所表达的内容，不要用“说话发生过”替代其内层行动。
answer_frame按完整答案的实际措辞判定，evidence_frame只按原文判定，不要为了让二者匹配而改类别。
same_referent核对同一人物/物件、事件和时段。原文只支持计划时，答案声称完成，应为insufficient或contradicted，
而非依据相同动词认定supported。无法确认的维度填unknown。引文定位不等于语义成立。
输出示意："claim_audit":{"version":"claims_v2","units":[{"unit_id":"下方编号",
"relation":"supported","same_referent":true,
"answer_frame":{"attribution":"attributed","holder":"人物名","world":"actual","modality":"planned","realization":"not_established"},
"evidence_frame":{"attribution":"attributed","holder":"人物名","world":"actual","modality":"planned","realization":"not_established"},
"evidence":[{"evidence_id":"原文段ID","quote":"逐字原文至少六字"}]}]}
固定答案片段：
"""


def build_claim_prompt(answer: str) -> str:
    return PROMPT + json.dumps(answer_units(answer), ensure_ascii=False)


def validate_claim_audit_v2(answer: str, segments: object, audit: object) -> dict:
    expected = answer_units(answer)
    errors, blockers = [], []
    if not isinstance(audit, dict) or audit.get("version") != VERSION:
        return {"eligible": False, "errors": ["missing_or_unknown_claim_contract"], "blockers": [], "units": []}
    raw_units = audit.get("units")
    if not isinstance(raw_units, list) or not 1 <= len(raw_units) <= 64 or not 1 <= len(expected) <= 64:
        return {"eligible": False, "errors": ["invalid_claim_units"], "blockers": [], "units": []}
    by_id, known = {}, {row["unit_id"] for row in expected}
    for row in raw_units:
        uid = row.get("unit_id") if isinstance(row, dict) else None
        if not isinstance(uid, str) or uid not in known or uid in by_id:
            errors.append("unknown_or_duplicate_answer_unit")
            continue
        by_id[uid] = row
    normalized = []
    for unit in expected:
        uid = unit["unit_id"]
        if uid not in by_id:
            errors.append(uid + ":missing_answer_unit")
            continue
        row = by_id[uid]
        normalized.append({"answer_text": unit["text"], "relation": row.get("relation"),
                           "answer_scope": "event", "evidence_scope": "event",
                           "same_referent": row.get("same_referent"), "evidence": row.get("evidence")})
        frames = []
        for name in ("answer_frame", "evidence_frame"):
            frame = row.get(name)
            if not isinstance(frame, dict):
                errors.append(uid + ":invalid_" + name)
                frames.append(None)
                continue
            invalid = False
            for field, values in FRAME_VALUES.items():
                value = frame.get(field)
                if not isinstance(value, str) or value not in values:
                    errors.append(uid + ":invalid_" + name + "." + field)
                    invalid = True
                elif value == "unknown":
                    blockers.append(uid + ":unknown_" + name + "." + field)
            if frame.get("attribution") == "attributed" and (not isinstance(frame.get("holder"), str) or not frame["holder"].strip()):
                errors.append(uid + ":missing_" + name + ".holder")
                invalid = True
            frames.append(None if invalid else frame)
        a, e = frames
        if a is not None and e is not None:
            for field in FRAME_VALUES:
                if a[field] != e[field]:
                    blockers.append(uid + ":changed_" + field)
            if a["attribution"] == e["attribution"] == "attributed" and a["holder"] != e["holder"]:
                blockers.append(uid + ":changed_holder")
    # Reuse the existing exact-source locator and relation checks; v2 frame
    # comparisons above replace v1's single mutually exclusive scope category.
    result = validate_claim_audit(answer, segments, {"version": "claims_v1", "units": normalized})
    result["errors"] += errors
    result["blockers"] += blockers
    located = iter(result["units"])
    for unit in expected:
        if unit["unit_id"] in by_id:
            row = next(located, None)
            if row is not None:
                row["unit_id"] = unit["unit_id"]
                row["answer_span"] = unit["span"]
    result["eligible"] = not result["errors"] and not result["blockers"]
    return result
