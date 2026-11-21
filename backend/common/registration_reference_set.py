"""参考标注集的格式、合并、冻结与盲审匹配。

**流程固定为**：两份独立发现 → 合并去重、保留争议 → 冻结参考集 →
运行两臂 → 盲审匹配。

参考发现阶段**不得填写 `registered`**：那是盲审对照系统输出后才确定的字段，
且必须**分别记录 A、B 两臂**的匹配结果。无法确定不能默认算漏登记。

模型标注与人工标注共用同一份格式——校验、合并、冻结、匹配都只认字段，
不认来源。人的盲点与模型的盲点一样存在，**不能仅凭"人标"就当成真值**。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "registration_reference_set_v1"
PROTOCOL_VERSION = "registration_annotation_v1"

#: 条目级判断
LABEL_GROUNDED_UNRESOLVED = "有依据且未解"
LABEL_ALREADY_ANSWERED = "已经回答"
LABEL_UNDECIDED = "无法确定"
ITEM_LABELS = (LABEL_GROUNDED_UNRESOLVED, LABEL_ALREADY_ANSWERED, LABEL_UNDECIDED)

#: 每条标注必须有这些字段
REQUIRED_ITEM_FIELDS = (
    "question",
    "answer_criteria",
    "supporting_quote",
    "acceptable_spans",
    "label",
    "reason",
)

#: 整份标注必须记录这些
REQUIRED_HEADER_FIELDS = (
    "work_id",
    "prefix_id",
    "annotator_id",
    "protocol_version",
    "scope_read",
    "completed",
    "completed_at",
    "freeze_sha256",
)


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    return "".join((text or "").split())


def validate_annotation(doc: Mapping[str, Any]) -> dict:
    """校验一份标注（模型或人工）。

    返回 {ok, errors, header, items}。
    发现阶段若出现 `registered` 字段，直接判不合格——
    那是盲审阶段的字段，填了就破坏了"先发现、后对照"的顺序。
    """
    errors: list[str] = []
    if not isinstance(doc, Mapping):
        return {"ok": False, "errors": ["annotation_not_an_object"], "header": {}, "items": []}

    header = {}
    for f in REQUIRED_HEADER_FIELDS:
        v = doc.get(f)
        if f == "completed":
            if not isinstance(v, bool):
                errors.append("header_invalid:" + f)
            continue
        if v is None or str(v).strip() == "":
            errors.append("header_missing:" + f)
        header[f] = v
    if header.get("protocol_version") and header["protocol_version"] != PROTOCOL_VERSION:
        errors.append("header_protocol_version_mismatch")

    raw_items = doc.get("items")
    if not isinstance(raw_items, list):
        errors.append("items_not_a_list")
        raw_items = []

    items: list[dict] = []
    for i, it in enumerate(raw_items):
        if not isinstance(it, Mapping):
            errors.append(f"item{i}_not_an_object")
            continue
        if "registered" in it:
            errors.append(f"item{i}_must_not_fill_registered")
            continue
        missing = [f for f in REQUIRED_ITEM_FIELDS if it.get(f) in (None, "", [])]
        if missing:
            errors.append(f"item{i}_missing:" + ",".join(missing))
            continue
        label = str(it.get("label"))
        if label not in ITEM_LABELS:
            errors.append(f"item{i}_unknown_label:{label}")
            continue
        spans = it.get("acceptable_spans")
        if not isinstance(spans, list) or not spans:
            errors.append(f"item{i}_acceptable_spans_empty")
            continue
        bad_spans = [
            s
            for s in spans
            if not isinstance(s, Mapping)
            or not isinstance(s.get("start"), int)
            or not isinstance(s.get("end"), int)
            or s.get("end") <= s.get("start")
        ]
        if bad_spans:
            errors.append(f"item{i}_bad_spans:{len(bad_spans)}")
            continue
        items.append(
            {
                "question": str(it.get("question")),
                "answer_criteria": str(it.get("answer_criteria")),
                "supporting_quote": str(it.get("supporting_quote")),
                "acceptable_spans": [dict(s) for s in spans],
                "label": label,
                "reason": str(it.get("reason")),
                "unit_id": it.get("unit_id"),
            }
        )
    return {"ok": not errors, "errors": errors, "header": header, "items": items}


def _shared_span(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """两个条目是否**共享同一段跨度**。

    共享同一锚点是最强的"疑似同一个问题"信号：措辞不同但锚在同一处。
    只在同一场景附近（不共享跨度）不足以判重——同一场景里
    本来就可能有多个不同的问题。
    """
    sa = {
        (s.get("unit_id"), s.get("start"), s.get("end"))
        for s in (a.get("acceptable_spans") or [])
    }
    sb = {
        (s.get("unit_id"), s.get("start"), s.get("end"))
        for s in (b.get("acceptable_spans") or [])
    }
    return bool(sa & sb)


def merge_annotations(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    work_id: str,
    prefix_id: str,
) -> dict:
    """合并两份独立发现：去重、保留争议、记录单方条目。

    - 逐字同题 → 合并，但**不自动视为回答条件一致**：
      同题同标签不等于条件一致，两边原始条件与跨度都要保留；
    - 同引文但措辞不同 → 标 `possible_duplicate`，**不自动合并**（交给人工看）；
    - 同一题但判断不同 → 标 `disputed`，两方意见都保留；
    - 只有一方提到 → 标 `single_annotator`。

    合并结果**一律是待裁决状态**：没有任何条目自动进入合格集合。
    资格由 `apply_qualification` 单独裁决。
    """
    lv = validate_annotation(left)
    rv = validate_annotation(right)
    if not lv["ok"] or not rv["ok"]:
        return {
            "ok": False,
            "errors": [f"left:{e}" for e in lv["errors"]] + [f"right:{e}" for e in rv["errors"]],
            "items": [],
        }

    def _key(it: Mapping[str, Any]) -> str:
        return _norm(it.get("question"))

    def _quote_key(it: Mapping[str, Any]) -> str:
        return _norm(it.get("supporting_quote"))

    litems = lv["items"]
    ritems = rv["items"]
    lby = {_key(i): i for i in litems}
    rby = {_key(i): i for i in ritems}

    def _mk(it: Mapping[str, Any], status: str, annotator: str, opinions: list[dict]) -> dict:
        return {
            "question": it["question"],
            "answer_criteria": it["answer_criteria"],
            "supporting_quote": it["supporting_quote"],
            "acceptable_spans": it["acceptable_spans"],
            "label": it["label"],
            "reason": it["reason"],
            "status": status,
            "annotators": [a for a in annotator.split("+") if a],
            "opinions": opinions,
            "unit_id": it.get("unit_id"),
            # 两边的原始条件与跨度都保留下来，供人工裁决时对照
            "source_conditions": [
                {
                    "annotator_id": o["annotator_id"],
                    "answer_criteria": o.get("answer_criteria"),
                    "acceptable_spans": o.get("acceptable_spans"),
                    "label": o.get("label"),
                    "reason": o.get("reason"),
                }
                for o in opinions
            ],
            # 资格状态：合并阶段一律待裁决
            "qualification": {
                "status": "pending_review",
                "qualified": False,
                "qualified_label": None,
                "accepted_answer_criteria": None,
                "accepted_spans": None,
                "reviewer_id": None,
                "reason": None,
            },
        }

    merged: list[dict] = []
    used_r: set[str] = set()
    for li in litems:
        k = _key(li)
        if k in rby:
            ri = rby[k]
            used_r.add(k)
            same_label = li["label"] == ri["label"]
            opinions = [
                {
                    "annotator_id": lv["header"].get("annotator_id"),
                    "label": li["label"],
                    "reason": li["reason"],
                    "answer_criteria": li["answer_criteria"],
                    "acceptable_spans": li["acceptable_spans"],
                },
                {
                    "annotator_id": rv["header"].get("annotator_id"),
                    "label": ri["label"],
                    "reason": ri["reason"],
                    "answer_criteria": ri["answer_criteria"],
                    "acceptable_spans": ri["acceptable_spans"],
                },
            ]
            merged.append(
                _mk(
                    li,
                    "agreed" if same_label else "disputed",
                    f"{lv['header'].get('annotator_id')}+{rv['header'].get('annotator_id')}",
                    opinions,
                )
            )
        else:
            merged.append(
                _mk(
                    li,
                    "single_annotator",
                    str(lv["header"].get("annotator_id")),
                    [
                        {
                            "annotator_id": lv["header"].get("annotator_id"),
                            "label": li["label"],
                            "reason": li["reason"],
                            "answer_criteria": li["answer_criteria"],
                            "acceptable_spans": li["acceptable_spans"],
                        }
                    ],
                )
            )
    for ri in ritems:
        k = _key(ri)
        if k in used_r or k in lby:
            continue
        merged.append(
            _mk(
                ri,
                "single_annotator",
                str(rv["header"].get("annotator_id")),
                [
                    {
                        "annotator_id": rv["header"].get("annotator_id"),
                        "label": ri["label"],
                        "reason": ri["reason"],
                        "answer_criteria": ri["answer_criteria"],
                        "acceptable_spans": ri["acceptable_spans"],
                    }
                ],
            )
        )

    # 同引文但措辞不同 → 可能重复，交给人工看，不自动合并
    by_quote: dict[str, list[dict]] = {}
    for it in merged:
        by_quote.setdefault(_quote_key(it), []).append(it)
    for q, group in by_quote.items():
        if len(group) > 1:
            for it in group:
                it["possible_duplicate_with"] = [
                    g["question"] for g in group if g is not it
                ]

    # 共享同一锚点 → 疑似同一个问题。
    # 措辞与引文都不同的时候，机械去重看不出来，但共享锚点是最强的信号；
    # 只标记，不自动合并——合并与否由人判断。
    for i, it in enumerate(merged):
        near = [
            g["question"]
            for g in merged
            if g is not it and g.get("label") == it.get("label") and _shared_span(it, g)
        ]
        if near:
            it.setdefault("possible_duplicate_with", [])
            it["possible_duplicate_with"] = sorted(set(it["possible_duplicate_with"]) | set(near))
            it["duplicate_hint"] = "与上述条目共享同一锚点跨度，可能是同一问题"

    for i, it in enumerate(merged):
        it["ref_id"] = f"r{i}"

    return {
        "ok": True,
        "errors": [],
        "work_id": work_id,
        "prefix_id": prefix_id,
        "items": merged,
        "summary": {
            "total": len(merged),
            "agreed": sum(1 for i in merged if i["status"] == "agreed"),
            "disputed": sum(1 for i in merged if i["status"] == "disputed"),
            "single_annotator": sum(1 for i in merged if i["status"] == "single_annotator"),
            "possible_duplicate_groups": sum(
                1 for group in by_quote.values() if len(group) > 1
            ),
        },
    }


def freeze_reference_set(
    merged: Mapping[str, Any],
    *,
    protocol_version: str = PROTOCOL_VERSION,
    annotator_ids: Sequence[str] = (),
) -> dict:
    """冻结参考集。

    冻结后**拒绝原位覆盖**——修订另建版本。
    """
    items = [dict(i) for i in merged.get("items") or []]
    payload = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return {
        "schema": SCHEMA,
        "work_id": merged.get("work_id"),
        "prefix_id": merged.get("prefix_id"),
        "protocol_version": protocol_version,
        "annotator_ids": list(annotator_ids),
        "items": items,
        "item_count": len(items),
        "freeze_sha256": _sha(payload),
        "summary": merged.get("summary") or {},
        "frozen": True,
    }


def save_reference_set(path: Path, record: Mapping[str, Any]) -> bool:
    """落盘冻结参考集；内容不同则拒绝原位覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"参考集已存在但无法解析，拒绝覆盖：{path}") from exc
        if json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) != payload:
            raise ValueError(f"参考集已存在且内容不同，拒绝原位覆盖：{path}")
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return True


#: 复核者类型。**如实标注**，不把 AI 复核冒称人工标注。
REVIEWER_TYPE_AI = "ai_independent_review"
REVIEWER_TYPE_HUMAN = "human"
REVIEWER_TYPES = (REVIEWER_TYPE_AI, REVIEWER_TYPE_HUMAN)


def validate_adjudication(
    decisions: Sequence[Mapping[str, Any]],
    *,
    known_ref_ids: Sequence[str],
) -> dict:
    """校验独立复核给出的裁决。

    - 只接受已知 ref_id；
    - `qualified=True` 时必须有标签、回答条件与合法跨度；
    - `qualified_label` 必须是合法标签；
    - 每条都要有理由（供抽查）。
    """
    errors: list[str] = []
    known = {str(r) for r in known_ref_ids}
    cleaned: list[dict] = []
    seen: set[str] = set()
    for i, d in enumerate(decisions or []):
        if not isinstance(d, Mapping):
            errors.append(f"decision{i}_not_an_object")
            continue
        rid = str(d.get("ref_id") or "")
        if rid not in known:
            errors.append(f"decision{i}_unknown_ref_id:{rid}")
            continue
        if rid in seen:
            errors.append(f"decision{i}_duplicate_ref_id:{rid}")
            continue
        seen.add(rid)
        qualified = bool(d.get("qualified"))
        label = d.get("qualified_label")
        reason = str(d.get("reason") or "").strip()
        if not reason:
            errors.append(f"decision{i}_missing_reason")
        if qualified:
            if label not in ITEM_LABELS:
                errors.append(f"decision{i}_invalid_qualified_label")
            elif label != LABEL_GROUNDED_UNRESOLVED:
                # 合格集合只收"有依据且未解"
                errors.append(f"decision{i}_qualified_but_not_unresolved:{label}")
            if not str(d.get("accepted_answer_criteria") or "").strip():
                errors.append(f"decision{i}_missing_accepted_answer_criteria")
            spans = d.get("accepted_spans")
            if not isinstance(spans, list) or not spans:
                errors.append(f"decision{i}_missing_accepted_spans")
            else:
                for s in spans:
                    if (
                        not isinstance(s, Mapping)
                        or not isinstance(s.get("start"), int)
                        or not isinstance(s.get("end"), int)
                        or s.get("end") <= s.get("start")
                    ):
                        errors.append(f"decision{i}_bad_accepted_span")
                        break
        cleaned.append(
            {
                "ref_id": rid,
                "qualified": qualified,
                "qualified_label": label,
                "accepted_answer_criteria": d.get("accepted_answer_criteria"),
                "accepted_spans": d.get("accepted_spans"),
                "reviewer_id": d.get("reviewer_id"),
                "reviewer_type": d.get("reviewer_type"),
                "reason": reason,
            }
        )
    missing = sorted(known - seen)
    return {
        "ok": not errors,
        "errors": errors,
        "decisions": cleaned,
        "missing_ref_ids": missing,
        "note": (
            "未裁决的 ref_id 保持待裁决，不进合格集合；"
            "不能因为没裁决就默认合格或默认不合格。"
        ),
    }


def apply_qualification(
    frozen: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    *,
    reviewer_id: str = "",
    reviewer_type: str = REVIEWER_TYPE_AI,
) -> dict:
    """对冻结的原始标注做资格裁决，产出**用于计分的合格参考集**。

    裁决要逐条给出：是否合格、合格标签、接受的回答条件与跨度。
    原始标注（两边的原始条件与跨度）保持不变，供追查。

    只有 `qualified=True` 且标签为"有依据且未解"的条目进入 `eligible`——
    `disputed`、`single_annotator` 与"已经回答"都不进。
    未做裁决的条目保持待裁决，不进 eligible。
    """
    by_id = {str(i.get("ref_id")): dict(i) for i in frozen.get("items") or []}
    applied: set[str] = set()
    for d in decisions or []:
        rid = str(d.get("ref_id"))
        item = by_id.get(rid)
        if item is None:
            continue
        applied.add(rid)
        q = item.setdefault("qualification", {})
        qualified = bool(d.get("qualified"))
        label = d.get("qualified_label")
        q["status"] = "reviewed"
        q["qualified"] = qualified
        q["qualified_label"] = label
        q["accepted_answer_criteria"] = d.get("accepted_answer_criteria")
        q["accepted_spans"] = d.get("accepted_spans")
        q["reviewer_id"] = d.get("reviewer_id") or reviewer_id
        q["reviewer_type"] = d.get("reviewer_type") or reviewer_type
        q["reason"] = d.get("reason")

    eligible = []
    for rid, item in by_id.items():
        q = item.get("qualification") or {}
        if q.get("qualified") and q.get("qualified_label") == LABEL_GROUNDED_UNRESOLVED:
            eligible.append(rid)
    eligible.sort()
    payload = json.dumps(
        [
            {
                "ref_id": rid,
                "question": by_id[rid].get("question"),
                "qualified_label": (by_id[rid].get("qualification") or {}).get("qualified_label"),
                "accepted_answer_criteria": (by_id[rid].get("qualification") or {}).get(
                    "accepted_answer_criteria"
                ),
                "accepted_spans": (by_id[rid].get("qualification") or {}).get("accepted_spans"),
            }
            for rid in eligible
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "schema": SCHEMA,
        "work_id": frozen.get("work_id"),
        "prefix_id": frozen.get("prefix_id"),
        "protocol_version": frozen.get("protocol_version"),
        "source_freeze_sha256": frozen.get("freeze_sha256"),
        "reviewer_id": reviewer_id,
        "reviewer_type": reviewer_type,
        "items": [dict(by_id[rid]) for rid in eligible],
        "eligible_ref_ids": eligible,
        "eligible_count": len(eligible),
        "reviewed_count": len(applied),
        "pending_count": sum(
            1
            for i in by_id.values()
            if (i.get("qualification") or {}).get("status") != "reviewed"
        ),
        "excluded": {
            "disputed": sorted(
                rid for rid, i in by_id.items() if i.get("status") == "disputed"
            ),
            "single_annotator": sorted(
                rid for rid, i in by_id.items() if i.get("status") == "single_annotator"
            ),
            "not_qualified": sorted(
                rid
                for rid, i in by_id.items()
                if (i.get("qualification") or {}).get("reviewed")
                and not (i.get("qualification") or {}).get("qualified")
            ),
            "pending_review": sorted(
                rid
                for rid, i in by_id.items()
                if (i.get("qualification") or {}).get("status") != "reviewed"
            ),
        },
        "freeze_sha256": _sha(payload),
        "frozen": True,
    }


def match_against_arms(
    eligible: Mapping[str, Any],
    *,
    arm_pairs: Mapping[str, Sequence[Mapping[str, Any]]],
    arm_system_items: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict:
    """盲审匹配：按臂分别统计。

    `arm_pairs[arm]`：该臂的配对结果，每项
    {ref_id, matched: True|False|None, system_item_id?}。
    ``matched=None`` 表示语义配对失败 → ``unresolved``。

    规则：
    - 只有 `eligible_ref_ids` 里的参考条目参与统计；
    - **缺配对记录 = unresolved**，不是漏登记；
    - 同一 ref_id 有冲突的配对记录 = unresolved；
    - 非法值 = unresolved；
    - ``matched=True`` 必须绑定**真实存在**的 ``system_item_id``，
      孤立的 true 记 unresolved；
    - 只有明确的 ``matched=False`` 才算漏登记。
    """
    ref_ids = [str(r) for r in (eligible.get("eligible_ref_ids") or [])]
    system_ids_by_arm: dict[str, set[str]] = {}
    for arm, items in (arm_system_items or {}).items():
        system_ids_by_arm[arm] = {
            str(i.get("item_id")) for i in (items or []) if i.get("item_id")
        }

    out: dict = {}
    for arm, pairs in (arm_pairs or {}).items():
        by_ref: dict[str, list[Mapping[str, Any]]] = {}
        for p in pairs or []:
            by_ref.setdefault(str(p.get("ref_id")), []).append(p)

        registered: list[str] = []
        missed: list[str] = []
        unresolved: list[str] = []
        details: list[dict] = []
        for rid in ref_ids:
            recs = by_ref.get(rid) or []
            verdict = None
            reason = ""
            if not recs:
                verdict, reason = "unresolved", "缺少配对记录"
            else:
                values = {r.get("matched") for r in recs}
                if len(recs) > 1 and len(values) > 1:
                    verdict, reason = "unresolved", "配对记录冲突"
                elif recs[0].get("matched") not in (True, False, None):
                    verdict, reason = "unresolved", "非法配对值"
                elif recs[0].get("matched") is None:
                    verdict, reason = "unresolved", "语义配对未解决"
                elif recs[0].get("matched") is True:
                    sid = str(recs[0].get("system_item_id") or "")
                    known = system_ids_by_arm.get(arm)
                    if not sid:
                        verdict, reason = "unresolved", "匹配成功但未绑定 system_item_id"
                    elif known is not None and sid not in known:
                        verdict, reason = "unresolved", "绑定的 system_item_id 不存在"
                    else:
                        verdict = "registered"
                else:
                    verdict = "missed"
            if verdict == "registered":
                registered.append(rid)
            elif verdict == "missed":
                missed.append(rid)
            else:
                unresolved.append(rid)
            details.append({"ref_id": rid, "verdict": verdict, "reason": reason})

        judged = [r for r in ref_ids if r not in unresolved]
        out[arm] = {
            "reference_total": len(ref_ids),
            "judged_total": len(judged),
            "registered": len(registered),
            "missed": len(missed),
            "missed_ref_ids": sorted(missed),
            "unresolved_total": len(unresolved),
            "unresolved_ref_ids": sorted(unresolved),
            "registered_ref_ids": sorted(registered),
            "per_ref": details,
        }
    return out


def to_metric_inputs(
    eligible: Mapping[str, Any],
    per_arm_match: Mapping[str, Mapping[str, Any]],
    arm: str,
) -> list[dict]:
    """把合格参考集与匹配结果转成指标模块的输入。

    只输出**已确认合格**的参考；unresolved 标 `pairing_status="unresolved"`，
    不进漏登记分子。`registered` 由匹配结果直接给出，不由调用方手填。
    """
    m = per_arm_match.get(arm) or {}
    per_ref = {d.get("ref_id"): d for d in m.get("per_ref") or []}
    out = []
    for rid in eligible.get("eligible_ref_ids") or []:
        d = per_ref.get(str(rid)) or {}
        verdict = d.get("verdict")
        out.append(
            {
                "question_id": str(rid),
                "registered": verdict == "registered",
                "pairing_status": "resolved" if verdict in ("registered", "missed") else "unresolved",
            }
        )
    return out
