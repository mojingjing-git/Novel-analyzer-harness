"""Shared Class C Claim-Level Audit Gate & Prompt Builder (Production + Evaluation).

Extracted from `scripts/acceptance_gate.py` so production (`backend/services/recheck_v2.py`)
and evaluation (`scripts/acceptance_gate.py`) execute
the exact same prompt construction, mechanical increment checks, and claim-evidence gate
without production depending on standalone scripts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from backend.common.claim_evidence import (
    PROMPT as CLAIM_PROMPT,
    VERSION as CLAIM_VERSION,
    validate_claim_audit,
)
from backend.common.claim_evidence_v2 import (
    VERSION as CLAIM_V2,
    build_claim_prompt,
    validate_claim_audit_v2,
)
from backend.common.closure import (
    closing_satisfied,
    validate_branch_hits,
    validate_prereg_row,
)

GATE_PROTOCOL_VERSION = "c_claim_audit_gate_v2"

#: 高重合仅提示复述风险，不能据此证明没有新增命题。
ANSWER_COVERAGE_REJECT = 0.85
#: 引文最短长度（低于此不参与子集判定）
MIN_QUOTE_CHARS = 12

#: 排他性表述（criteria 要求排除时，答案必须含其中之一）
EXCLUSION_MARKERS = ("不是", "没有其他", "只有", "排除", "并非", "唯一")

BINDING_FIELDS = (
    "fs_id",
    "question",
    "criteria",
    "description",
    "anchor_quote",
    "anchor_chapter",
    "answer",
    "quote",
    "chapter",
    "prereg",
    "evidence_segments",
    "context_error",
)


def compute_gate_code_snapshot() -> str:
    """Compute a deterministic SHA-256 snapshot of the gate and claim-evidence modules."""
    h = hashlib.sha256()
    h.update(GATE_PROTOCOL_VERSION.encode("utf-8"))
    for p in (
        Path(__file__),
        Path(__file__).with_name("claim_evidence.py"),
        Path(__file__).with_name("claim_evidence_v2.py"),
        Path(__file__).with_name("closure.py"),
    ):
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(p.name.encode("utf-8"))
    return h.hexdigest()[:16]


def norm(s: str) -> str:
    """归一化：去空白与常见标点（语义比较用）。"""
    return re.sub(r"[\s，,。；;：:、（）()\"'“”‘’！!？?…—\-]", "", s or "")


def ngrams(s: str, n: int = 2) -> set[str]:
    if len(s) < n:
        return set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def ngram_dice(a: str, b: str, n: int = 2) -> float:
    """字符 n-gram Dice 系数（0~1）。"""
    ga, gb = ngrams(a, n), ngrams(b, n)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def criteria_logic(criteria: str, prereg: dict | None = None) -> str:
    """只消费已登记结构；旧数据交语义层判断，绝不按关键词猜 AND/OR。"""
    if not prereg:
        return "UNKNOWN"
    ok, _ = validate_prereg_row(prereg, criteria)
    if not ok or prereg.get("criteria_source", criteria) != criteria:
        return "UNKNOWN"
    return prereg["closing"]["type"]


def mechanical_increment_check(item: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """硬缺陷限于引用完全落在旧锚点；扩展引用与高相似度仅作软信号。"""
    hard: list[str] = []
    soft: list[str] = []
    quote_n = norm(item.get("quote") or "")
    answer_n = norm(item.get("answer") or "")
    anchor_n = norm(item.get("anchor_quote") or "")
    desc_n = norm(item.get("description") or "")
    known_n = anchor_n + desc_n
    if len(quote_n) >= MIN_QUOTE_CHARS and anchor_n:
        if quote_n in anchor_n:
            hard.append("quote_equals_anchor")
        elif anchor_n in quote_n:
            soft.append("quote_contains_anchor")
        elif known_n and quote_n in known_n:
            soft.append("quote_in_known")
    if len(answer_n) >= 10 and known_n:
        ga, gb = ngrams(answer_n), ngrams(known_n)
        coverage = len(ga & gb) / len(ga)
        if coverage >= ANSWER_COVERAGE_REJECT:
            soft.append("answer_in_known")
    chapter = item.get("chapter")
    anchor_ch = item.get("anchor_chapter")
    # 时序判定：优先用**同坐标系、原文哈希绑定**的细粒度结果。
    #
    # 旧的章粒度判断（chapter <= anchor_ch 即标记）看不见同章内的先后，
    # 会把"证据确实在锚点之后"的同章情形一并标成疑点。
    # 细粒度结果把证据逐段判定，并按**全部**证据都在锚点之后才算通过——
    # 挑一段靠后的不算。
    #
    # 时序通过**只解除时间疑点**：答案正确性、条件满足、信息增量仍由下面的
    # 门禁判断，不因时序通过而放宽。
    ordering = item.get("evidence_ordering")
    if isinstance(ordering, Mapping):
        verdict = str(ordering.get("verdict") or "")
        if verdict == "all_after":
            pass  # 时间疑点解除
        elif verdict == "any_before":
            soft.append("evidence_not_after_anchor")
        else:
            # 重叠 / 定位不唯一 / 定位失败 / 引文重复 —— 位置未定，保持疑点
            soft.append("evidence_order_indeterminate")
    elif (
        isinstance(chapter, int)
        and isinstance(anchor_ch, int)
        and chapter > 0
        and anchor_ch > 0
        and chapter <= anchor_ch
    ):
        soft.append("evidence_not_after_anchor")
    if re.search(r"排[除他]|只[有是]|无其他|没有其他", item.get("criteria") or ""):
        if not any(m in (item.get("answer") or "") for m in EXCLUSION_MARKERS):
            soft.append("exclusivity_unverified")
    return hard, soft


NLI_PROMPT = """你是打捞结果验收员。判断一条候选答案是否构成对问题的有效回答。

判定规则（只能输出以下四种之一）：
1. restating：答案内容与"登记时已知信息"相同或仅为转写——没有信息增量。
   注意：即使答案在原文中真实存在，若信息量不超出已知，也算 restating。
2. off_topic：答案回答的不是问题所问。以下情形一律 off_topic：
   a. **合取型**判定标准（下方"判定标准类型"标为 AND）要求多个条件全部满足，
      答案只满足其中一部分；
   b. 合取型标准下，答案自认部分覆盖（"虽未说明…""虽然…但已回答"）——部分覆盖不算覆盖；
      **注意：判定标准类型为 OR 时，回答任一完整分支即算满足，自认未答其它分支不算缺陷。**
   c. 仅当关闭条件明确要求排除其他可能时，答案只确认其一、**没有引用明确的排除性原文**
      （"不是…而是…""没有其他""只有"）——确认其一 ≠ 排除其他；
   d. 答案所述事件、人物或时段与问题指向的不是同一事件（例如用另一条时间线的现象、
      另一个角色的遭遇来解释本条问题）。
3. answer_ok：答案在已知信息之外，给出了针对问题的新事实/新解释/新结果，
   且满足判定标准（OR 型满足任一分支；AND 型须全部满足）。
4. unclear：信息不足以判断。

只依据给出的材料，不要用你对该小说的任何记忆。拿不准时选 unclear，不要选 answer_ok。

问题：{question}
判定标准：{criteria}
**判定标准类型：{logic}**（OR=任一完整分支；AND=全部；UNKNOWN=按完整语义判断，括号/引号内的“或”不代表顶层OR；UNCERTAIN=存疑）
共享预登记（存在时必须沿用，不能重新拆分或放宽）：{prereg}
登记时已知信息：{known}
候选答案：{answer}
答案所依据的原文引文：{quote}
证据章与锚点章：证据章第{chapter}章，锚点（登记）第{anchor_chapter}章

原文上下文（候选答案不是事实依据；只能用下面原文支持答案）：
{context}

必须分开判断：criteria_satisfied=关闭条件是否被原文满足；answer_supported=答案每个实质断言是否都有原文支持。
识别梦境、假设、比喻、人物推测和转述，保留说话者与不确定性；不得把“打算/可能/梦中”升级为“已经证实”。
核对同一人物、物件、事件和时段。缺上下文无法判断时选unclear。
有共享预登记时，对满足的每个分支返回其独立引文与evidence_id；不得仅引用编号。
高重合或包含旧句不等于无增量；has_increment表示是否存在确切的新增命题。
输出纯JSON：{{"verdict":"answer_ok", "reason":"依据", "criteria_satisfied":true,
"answer_supported":true, "has_increment":true,
"branch_hits":[{{"branch_id":"c1", "evidence_id":"原文段ID", "supporting_quote":"逐字原句至少6字"}}]}}
"""


def build_nli_prompt(item: Mapping[str, Any]) -> str:
    known = (
        f"description：{item.get('description') or '（无）'}；"
        f"锚点原文（第{item.get('anchor_chapter')}章）：{item.get('anchor_quote') or '（无）'}"
    )
    prompt = NLI_PROMPT.format(
        question=item.get("question") or "",
        criteria=item.get("criteria") or "",
        logic=criteria_logic(item.get("criteria") or "", item.get("prereg")),
        prereg=json.dumps(item.get("prereg"), ensure_ascii=False),
        context=json.dumps(item.get("evidence_segments") or [], ensure_ascii=False),
        known=known,
        answer=item.get("answer") or "",
        quote=item.get("quote") or "",
        chapter=item.get("chapter") or "?",
        anchor_chapter=item.get("anchor_chapter") or "?",
    )
    if item.get("evidence_contract_version") == CLAIM_V2:
        return prompt + build_claim_prompt(item.get("answer") or "")
    return prompt + CLAIM_PROMPT if item.get("evidence_contract_version") == CLAIM_VERSION else prompt


def _skip_json_string(body: str, i: int) -> int | None:
    """跳过 JSON 字符串（i 指向起始引号），返回结束后的下标。"""
    n = len(body)
    j = i + 1
    while j < n:
        ch = body[j]
        if ch == "\\":
            j += 2
            continue
        if ch == '"':
            return j + 1
        j += 1
    return None


def _find_repair_points(body: str) -> list[tuple[int, str]]:
    """扫描 JSON 结构，找出**语法明确期待闭合符**却出现非法 `)` 的位置。

    规则（刻意保守）：
      - 只有当一个完整值刚结束、语法上正等待 `,` 或闭合符时，`)` 才是"缺了一个闭合符"；
      - 值的位置、字符串内部、字面量/数字 token 内部出现 `)` 一律**不可修**
        （那会改坏字段值或布尔词元，例如 `tru)e`）；
      - 返回的每一项是 (位置, 该位置应填的闭合符)。
    多个候选点 → 无法唯一修复 → 调用方放弃本地修复，转预算内重试。
    """
    stack: list[str] = []  # "o"=对象 "a"=数组
    expect = "key"  # key | colon | value | comma_or_close | done
    i = 0
    n = len(body)
    points: list[tuple[int, str]] = []
    # 根对象的开头已经由调用方定位，这里消费掉它
    if not body.startswith("{"):
        return []
    stack.append("o")
    i = 1
    while i < n:
        ch = body[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if expect == "key":
            if ch != '"':
                return []
            j = _skip_json_string(body, i)
            if j is None:
                return []
            i = j
            expect = "colon"
            continue
        if expect == "colon":
            if ch != ":":
                return []
            i += 1
            expect = "value"
            continue
        if expect == "value":
            if ch == '"':
                j = _skip_json_string(body, i)
                if j is None:
                    return []
                i = j
                expect = "comma_or_close"
                continue
            if ch == "{":
                stack.append("o")
                i += 1
                expect = "key"
                continue
            if ch == "[":
                stack.append("a")
                i += 1
                expect = "value"
                continue
            if ch == ")":
                return []  # 值的位置缺值，不可修
            j = i
            while j < n and (body[j].isalnum() or body[j] in "+-."):
                j += 1
            if j == i:
                return []
            i = j
            expect = "comma_or_close"
            continue
        if expect == "comma_or_close":
            if ch == ",":
                i += 1
                expect = "key" if (stack and stack[-1] == "o") else "value"
                continue
            if ch == "}" and stack and stack[-1] == "o":
                stack.pop()
                i += 1
                expect = "comma_or_close" if stack else "done"
                continue
            if ch == "]" and stack and stack[-1] == "a":
                stack.pop()
                i += 1
                expect = "comma_or_close" if stack else "done"
                continue
            if ch == ")":
                if not stack:
                    return []
                points.append((i, "}" if stack[-1] == "o" else "]"))
                # 按"这里填上闭合符"的语义继续扫描，保证栈状态与修复后一致；
                # 若后面还有第二处 `)`，points 会有两项 → 无法唯一修复 → 不修。
                stack.pop()
                i += 1
                expect = "comma_or_close"
                continue
            return []
        if expect == "done":
            # 根对象已正常闭合；后面若有内容由调用方的"尾部检查"处理
            return points
    return points


def _has_duplicate_keys(obj: object) -> bool:
    """检查对象里是否有重复键（json 解析器默认后者覆盖前者，会藏掉矛盾内容）。"""

    def hook(pairs):
        seen = set()
        for k, _v in pairs:
            if k in seen:
                raise ValueError("duplicate key")
            seen.add(k)
        return dict(pairs)

    try:
        json.loads(json.dumps(obj), object_pairs_hook=hook)
    except (ValueError, TypeError):
        # 直接用原对象再走一遍更可靠
        try:
            json.loads(json.dumps(obj), object_pairs_hook=hook)
        except Exception:
            return True
    return False


def _raw_decode_strict(body: str) -> tuple[dict | None, int]:
    """解析并拒绝重复键。返回 (对象, 消费到的位置)。"""
    dup = {"flag": False}

    def hook(pairs):
        seen = set()
        for k, _v in pairs:
            if k in seen:
                dup["flag"] = True
            seen.add(k)
        return dict(pairs)

    try:
        obj, end = json.JSONDecoder(object_pairs_hook=hook).raw_decode(body)
    except json.JSONDecodeError:
        return None, 0
    if dup["flag"] or not isinstance(obj, dict):
        return None, end
    return obj, end


def _tail_is_acceptable(tail: str) -> bool:
    """尾部是否只是无关噪声。

    含第二个 JSON 对象或带 `verdict` 的片段 → 有歧义，拒绝：
    模型输出两个裁决时，取第一个会丢掉矛盾内容。
    纯散文尾巴（"以上是我的分析"）可以接受。
    """
    stripped = tail.strip()
    if not stripped:
        return True
    if "verdict" in stripped:
        return False
    if "{" in stripped or "}" in stripped:
        return False
    return True


def extract_json_object_with_repair(text: str) -> tuple[dict | None, dict]:
    """从模型输出里取 JSON 对象，并返回修复记录。

    修复原则（刻意保守，宁可失败转重试也不猜）：
      - 只在 JSON 语法明确期待闭合符的位置修一个非法 `)`；
      - 绝不改字段名、字符串内容、布尔/数字/null 词元；
      - 修复后必须**完整消费**输入（不允许留下有意义的尾部）；
      - 拒绝重复键；
      - 出现多个可修位置 → 无法唯一修复 → 不修，转预算内重试。
    """
    original = text or ""
    text = original.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start < 0:
        return None, {"applied": False, "reason": "no_object_start"}

    body = text[start:]
    obj, end = _raw_decode_strict(body)
    if obj is not None:
        if not _tail_is_acceptable(body[end:]):
            return None, {
                "applied": False,
                "reason": "ambiguous_tail_with_verdict",
                "tail": body[end:][:200],
            }
        return obj, {"applied": False, "reason": "parsed_as_is"}

    points = _find_repair_points(body)
    if len(points) != 1:
        return None, {
            "applied": False,
            "reason": "no_unique_repair_point" if not points else "ambiguous_repair_points",
            "candidate_points": len(points),
        }
    pos, closer = points[0]
    repaired = body[:pos] + closer + body[pos + 1 :]
    obj, end = _raw_decode_strict(repaired)
    if obj is None:
        return None, {"applied": False, "reason": "repair_did_not_parse"}
    if repaired[end:].strip():
        return None, {
            "applied": False,
            "reason": "repair_left_unconsumed_tail",
            "tail": repaired[end:][:200],
        }
    return obj, {
        "applied": True,
        "kind": "replace_illegal_paren_with_expected_closer",
        "offset": pos,
        "replacement": closer,
        "original_length": len(original),
        "repaired_length": len(repaired),
    }


def extract_json_object(text: str, *, repair: bool = True) -> dict | None:
    """从模型输出里取 JSON 对象（见 extract_json_object_with_repair 的修复原则）。"""
    obj, _record = extract_json_object_with_repair(text)
    if obj is None and not repair:
        return None
    return obj


def _gate_decision(item: Mapping[str, Any], nli: dict | None, nli_error: str | None = None) -> dict[str, Any]:
    """合成裁决：接受 / 拒绝 / 存疑。硬缺陷直接拒绝；软疑点与 NLI 不清 → 存疑。"""
    hard, soft = mechanical_increment_check(item)
    if hard:
        return {"decision": "拒绝", "reasons": [f"机械增量缺陷：{','.join(hard)}"]}
    verdict = (nli or {}).get("verdict") if isinstance(nli, dict) else None
    if nli_error:
        return {"decision": "存疑", "reasons": [f"NLI 调用失败：{nli_error[:80]}"]}
    if verdict in ("restating", "off_topic"):
        return {
            "decision": "拒绝",
            "reasons": [f"NLI={verdict}：{(nli or {}).get('reason', '')[:80]}"],
        }
    if verdict == "unclear":
        return {"decision": "存疑", "reasons": ["NLI=unclear"]}
    if verdict == "answer_ok":
        if nli.get("criteria_satisfied") is not True or nli.get("answer_supported") is not True:
            return {"decision": "存疑", "reasons": ["缺少条件满足或答案忠实性确认"]}
        segments = item.get("evidence_segments") or []
        quote = item.get("quote") or ""
        if item.get("context_error") or not quote or not any(quote in seg.get("text", "") for seg in segments):
            return {"decision": "存疑", "reasons": ["原文上下文缺失或引文未定位"]}
        reg = item.get("prereg")
        if reg is not None:
            logic = criteria_logic(item.get("criteria") or "", reg)
            if logic in ("UNKNOWN", "UNCERTAIN"):
                return {"decision": "存疑", "reasons": ["关闭条件无效、已变更或待澄清"]}
            hits, errors = validate_branch_hits(reg, nli.get("branch_hits"), segments)
            if errors or closing_satisfied(reg["closing"], hits, reg) is not True:
                return {"decision": "存疑", "reasons": errors or ["分支证据未覆盖关闭条件"]}
        lexical = {"quote_contains_anchor", "quote_in_known", "answer_in_known"}
        unresolved = [x for x in soft if x not in lexical or nli.get("has_increment") is not True]
        if unresolved:
            return {"decision": "存疑", "reasons": ["待复核：" + ",".join(unresolved)]}
        return {"decision": "接受", "reasons": [f"NLI=answer_ok：{nli.get('reason', '')[:120]}"]}
    return {"decision": "存疑", "reasons": ["无充分接受依据"]}


def gate(item: Mapping[str, Any], nli: dict | None, nli_error: str | None = None) -> dict[str, Any]:
    """关闭判定之外保留部分回答，状态不等同于人工证实或自动落账。"""
    result = _gate_decision(item, nli, nli_error)
    claim_check = None
    if "evidence_contract_version" in item:
        validate = (
            validate_claim_audit_v2
            if item["evidence_contract_version"] == CLAIM_V2
            else validate_claim_audit
        )
        claim_check = validate(
            item.get("answer"),
            item.get("evidence_segments"),
            nli.get("claim_audit") if isinstance(nli, dict) else None,
        )
        if item["evidence_contract_version"] not in (CLAIM_VERSION, CLAIM_V2):
            claim_check["eligible"] = False
            claim_check["errors"].append("unknown_requested_claim_contract")
        if result["decision"] == "接受" and not claim_check["eligible"]:
            result = {"decision": "存疑", "reasons": claim_check["errors"] + claim_check["blockers"]}
    assessment = "unconfirmed"
    if result["decision"] == "接受":
        assessment = "accepted"
    elif isinstance(nli, dict) and not nli_error:
        quote = item.get("quote") or ""
        located = (
            not item.get("context_error")
            and quote
            and any(quote in seg.get("text", "") for seg in item.get("evidence_segments", []))
        )
        if (
            located
            and (claim_check is None or claim_check["eligible"])
            and nli.get("answer_supported") is True
            and nli.get("criteria_satisfied") is False
            and nli.get("has_increment") is True
        ):
            assessment = "partial_answer"
        elif located and nli.get("criteria_satisfied") is True and nli.get("answer_supported") is False:
            assessment = "answer_overstated"
    return {
        **result,
        "assessment_status": assessment,
        **({"claim_check": claim_check} if claim_check is not None else {}),
    }


def evidence_fingerprint(evidence_segments: list[Mapping[str, Any]] | None) -> str:
    """Compute a canonical SHA-256 fingerprint over evidence segments."""
    norm_segs = [
        {
            "evidence_id": str(seg.get("evidence_id") or ""),
            "chapter": seg.get("chapter"),
            "text": str(seg.get("text") or ""),
        }
        for seg in (evidence_segments or [])
        if isinstance(seg, Mapping)
    ]
    raw = json.dumps(norm_segs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sample_fingerprint(item: Mapping[str, Any]) -> str:
    payload = {k: item.get(k) for k in BINDING_FIELDS}
    if "evidence_contract_version" in item:
        payload["evidence_contract_version"] = item["evidence_contract_version"]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def base_sample_fingerprint(item: Mapping[str, Any]) -> str:
    """Compute sample fingerprint over BINDING_FIELDS independent of evidence_contract_version."""
    payload = {k: item.get(k) for k in BINDING_FIELDS}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def label_status(item: Mapping[str, Any]) -> str:
    if item.get("label") not in ("支持", "不支持", "待裁决"):
        return "unlabelled"
    if not item.get("label_sample_id"):
        return "unbound"
    return "bound" if item["label_sample_id"] == sample_fingerprint(item) else "stale"


def evaluate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    labelled = [r for r in results if r.get("label_status") == "bound"]

    def count(label: str, decision: str | None = None) -> int:
        return sum(
            r.get("label") == label and (decision is None or r["decision"] == decision)
            for r in labelled
        )

    def ratio(n: int, d: int) -> float | None:
        return n / d if d else None

    sup, neg, uncertain = count("支持"), count("不支持"), count("待裁决")
    block, keep, dangerous = (
        count("不支持", "拒绝"),
        count("支持", "接受"),
        count("不支持", "接受"),
    )
    return {
        "total": len(results),
        "labelled": len(labelled),
        "support_total": sup,
        "negative_total": neg,
        "uncertain_total": uncertain,
        "block_rate": ratio(block, neg),
        "prevent_accept_rate": ratio(neg - dangerous, neg),
        "keep_rate": ratio(keep, sup),
        "agreement": ratio(block + keep, sup + neg),
        "uncertain_rate": ratio(count("待裁决", "存疑"), uncertain),
        "dangerous": dangerous if neg else None,
        "decisions": {
            d: sum(r["decision"] == d for r in results) for d in ("接受", "拒绝", "存疑")
        },
        "assessments": {
            s: sum(r.get("assessment_status") == s for r in results)
            for s in ("accepted", "partial_answer", "answer_overstated", "unconfirmed")
        },
        "label_status": {
            k: sum(r.get("label_status") == k for r in results)
            for k in ("bound", "unbound", "stale", "unlabelled")
        },
    }
