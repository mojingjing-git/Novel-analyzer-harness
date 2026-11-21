"""登记评估的指标计算。

分母口径（写死在这里，不允许在报告里临时换）：

- 无据提问率 / 已知答案仍登记率 / 定位成功率
  → 分母是**有效裁决数**：有裁决、标签合法、且判定确定（不是"无法确定"、不是争议）。
- 漏登记率
  → 分母是**有效参考问题数**：独立标注的合格问题，去重、配对已解决。
- 登记延迟
  → 用**检查点差**或**同一固定坐标里的字符距离**；
  不直接相减含义不明的"跨度ID"。

必须分离并分别报告：系统原始总数、有效裁决数、争议数、未审数。
排除后的比率要同时报告**裁决覆盖率**——否则不确定项增多反而显得指标改善。

重复ID、未知标签、缺 `registered` 状态一律**不静默参与计算**：
要么排除并计入对应报告项，要么直接报错。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: 条目级判据（与《登记标注协议》一致）
LABEL_GROUNDED = "有依据"
LABEL_NO_GROUND = "无依据"
LABEL_ALREADY_ANSWERED = "已经回答"
LABEL_UNDECIDED = "无法确定"
ITEM_LABELS = (LABEL_GROUNDED, LABEL_NO_GROUND, LABEL_ALREADY_ANSWERED, LABEL_UNDECIDED)

#: 判定确定的标签——只有这些进入指标分母
SETTLED_LABELS = (LABEL_GROUNDED, LABEL_NO_GROUND, LABEL_ALREADY_ANSWERED)

#: 登记延迟的合法基准
DELAY_BASIS_PREFIX_CHECKPOINT = "prefix_checkpoint"
DELAY_BASIS_CHAR_OFFSET = "char_offset"
DELAY_BASES = (DELAY_BASIS_PREFIX_CHECKPOINT, DELAY_BASIS_CHAR_OFFSET)


class MetricInputError(ValueError):
    """输入本身不可用——重复ID、缺字段、未知标签等。"""


def _rate(numerator: int, denominator: int) -> dict:
    if denominator <= 0:
        return {
            "count": 0,
            "denominator": 0,
            "rate": None,
            "status": "N/A：有效分母为零",
        }
    return {
        "count": int(numerator),
        "denominator": int(denominator),
        "rate": round(numerator / denominator, 6),
        "status": "可计算",
    }


def _dedupe(items: Sequence[Mapping[str, Any]], key: str, what: str) -> tuple[list, list]:
    """去重并报出重复ID。重复项不静默参与计算。"""
    seen: dict[str, int] = {}
    dupes: list[str] = []
    kept: list[Mapping[str, Any]] = []
    for it in items:
        ident = str(it.get(key) or "")
        if not ident:
            dupes.append("<缺ID>")
            continue
        if ident in seen:
            seen[ident] += 1
            dupes.append(ident)
            continue
        seen[ident] = 1
        kept.append(it)
    return kept, sorted(set(dupes))


def compute_metrics(
    *,
    system_items: Sequence[Mapping[str, Any]],
    adjudicated_items: Sequence[Mapping[str, Any]],
    independent_qualified: Sequence[Mapping[str, Any]],
) -> dict:
    """算指标。

    `system_items`：系统生成的条目，每项 {item_id, ...}。
    `adjudicated_items`：标注结果，每项 {item_id, label, span_located, disputed?}。
    `independent_qualified`：独立标注的合格问题，每项 {question_id, registered,
    pairing_status?, delay?}。
    """
    sys_kept, sys_dupes = _dedupe(system_items, "item_id", "system_items")
    system_total = len(sys_kept)
    system_ids = [str(i.get("item_id")) for i in sys_kept]

    adj_kept, adj_dupes = _dedupe(adjudicated_items, "item_id", "adjudicated_items")
    adj_by_id: dict[str, Mapping[str, Any]] = {}
    for a in adj_kept:
        adj_by_id[str(a.get("item_id"))] = a

    label_of: dict[str, str] = {}
    span_located: dict[str, bool] = {}
    disputed_ids: list[str] = []
    undecided_ids: list[str] = []
    unknown_label_ids: list[str] = []
    orphan_adj_ids: list[str] = []

    for a in adj_kept:
        aid = str(a.get("item_id"))
        if aid not in system_ids:
            orphan_adj_ids.append(aid)
            continue
        label = str(a.get("label") or "")
        if label not in ITEM_LABELS:
            unknown_label_ids.append(aid)
            continue
        if a.get("disputed"):
            disputed_ids.append(aid)
            continue
        if label == LABEL_UNDECIDED:
            # 判据本身就是"无法确定"：不进分子也不进分母
            undecided_ids.append(aid)
            continue
        label_of[aid] = label
        span_located[aid] = bool(a.get("span_located"))

    settled_ids = [i for i in system_ids if i in label_of]
    unadjudicated_ids = [i for i in system_ids if i not in adj_by_id]

    n_settled = len(settled_ids)

    def _count(label: str) -> int:
        return sum(1 for i in settled_ids if label_of.get(i) == label)

    n_no_ground = _count(LABEL_NO_GROUND)
    n_answered = _count(LABEL_ALREADY_ANSWERED)
    n_located = sum(1 for i in settled_ids if span_located.get(i))

    # ---- 参考集（漏登记）----
    ref_kept, ref_dupes = _dedupe(independent_qualified, "question_id", "independent_qualified")
    ref_valid: list[Mapping[str, Any]] = []
    ref_missing_registered: list[str] = []
    ref_unresolved: list[str] = []
    for q in ref_kept:
        qid = str(q.get("question_id"))
        if "registered" not in q:
            # 缺 registered 状态不能静默参与计算
            ref_missing_registered.append(qid)
            continue
        pairing = str(q.get("pairing_status") or "resolved")
        if pairing != "resolved":
            # 语义配对失败 → unresolved，不默认为未登记
            ref_unresolved.append(qid)
            continue
        ref_valid.append(q)

    n_ref_valid = len(ref_valid)
    n_missed = sum(1 for q in ref_valid if not q.get("registered"))

    # ---- 登记延迟 ----
    delay_records: list[dict] = []
    delay_rejected: list[dict] = []
    for q in ref_valid:
        d = q.get("delay")
        if not isinstance(d, Mapping):
            continue
        basis = str(d.get("basis") or "")
        if basis not in DELAY_BASES:
            # 不接受"跨度ID相减"这类含义不明的算法
            delay_rejected.append({"question_id": q.get("question_id"), "reason": f"unsupported_basis:{basis}"})
            continue
        if basis == DELAY_BASIS_PREFIX_CHECKPOINT:
            f = d.get("first_qualified_prefix_id")
            r = d.get("registered_prefix_id")
            if f is None or r is None:
                delay_rejected.append({"question_id": q.get("question_id"), "reason": "missing_prefix_checkpoint"})
                continue
            delay_records.append(
                {
                    "question_id": q.get("question_id"),
                    "basis": basis,
                    "delay": int(r) - int(f),
                    "unit": "prefix_checkpoint",
                }
            )
        else:
            f = d.get("first_qualified_char")
            r = d.get("registered_char")
            space = d.get("coordinate_space")
            if f is None or r is None or not space:
                delay_rejected.append(
                    {"question_id": q.get("question_id"), "reason": "missing_char_or_coordinate_space"}
                )
                continue
            delay_records.append(
                {
                    "question_id": q.get("question_id"),
                    "basis": basis,
                    "delay": int(r) - int(f),
                    "unit": "char_offset",
                    "coordinate_space": space,
                }
            )
    n_delay_positive = sum(1 for d in delay_records if d["delay"] > 0)

    coverage = _rate(n_settled, system_total)
    return {
        "denominators": {
            "system_total": system_total,
            "settled_adjudications": n_settled,
            "disputed": len(disputed_ids),
            "undecided": len(undecided_ids),
            "unadjudicated": len(unadjudicated_ids),
            "unknown_label": len(unknown_label_ids),
            "duplicate_ids": len(set(sys_dupes) | set(adj_dupes)),
            "independent_qualified_total": len(ref_kept),
            "independent_qualified_valid": n_ref_valid,
        },
        "coverage": {
            "adjudication_coverage": coverage["rate"],
            "adjudication_coverage_status": coverage["status"],
            "note": "比率的分母是有效裁决数；覆盖率单独报告，避免不确定项增多显得指标改善",
        },
        "metrics": {
            "no_ground_rate": _rate(n_no_ground, n_settled),
            "already_answered_rate": _rate(n_answered, n_settled),
            "span_localization_rate": _rate(n_located, n_settled),
            "missed_registration_rate": _rate(n_missed, n_ref_valid),
            "registration_delay": {
                "unit": "见每条记录的 basis",
                "baseline": "首次观察到合格的位置",
                "measured": len(delay_records),
                "positive_delay_count": n_delay_positive,
                "rate": round(n_delay_positive / len(delay_records), 6) if delay_records else None,
                "status": "可计算" if delay_records else "N/A：无可比样本",
                "records": delay_records,
                "rejected": delay_rejected,
                "caveat": (
                    "只有在若干前缀位置做过检查时，基准是"
                    "“首次观察到合格的位置”，不能表述为“全文最早位置”。"
                    "延迟只按检查点差或同一固定坐标里的字符距离计算。"
                ),
            },
        },
        "excluded": {
            "disputed_ids": sorted(disputed_ids),
            "undecided_ids": sorted(undecided_ids),
            "unknown_label_ids": sorted(unknown_label_ids),
            "unadjudicated_ids": sorted(unadjudicated_ids),
            "orphan_adjudication_ids": sorted(orphan_adj_ids),
            "duplicate_system_ids": sorted(set(sys_dupes)),
            "duplicate_adjudication_ids": sorted(set(adj_dupes)),
            "reference_missing_registered": sorted(ref_missing_registered),
            "reference_unresolved_pairing": sorted(ref_unresolved),
            "note": "以上各项都不进任何指标的分子或分母",
        },
        "label_distribution": {
            label: sum(1 for i in settled_ids if label_of.get(i) == label)
            for label in SETTLED_LABELS
        },
    }
