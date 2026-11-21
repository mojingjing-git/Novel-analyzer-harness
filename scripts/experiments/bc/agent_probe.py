"""Offline feasibility probe for the four 2026-11-06 design directions.

This probe uses only local pure-Python contracts. It does not read config.json,
novels, historical results, or call any model/network service.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, fields
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.common.c_audit_selector import compute_mechanical_signals  # noqa: E402
from backend.common.claim_evidence_v2 import answer_units  # noqa: E402
from backend.common.closure import closing_satisfied, validate_prereg_row  # noqa: E402
from backend.utils.foreshadow_ledger import ForeshadowItem  # noqa: E402


def main() -> int:
    checks: dict[str, object] = {}

    # 1. Existing claims_v2 units are punctuation units, even when one clause
    # contains a guess, a plan, and an explicitly unresolved result.
    mixed_claim = "他推测石门后会有人并计划搬走三座山但结果尚未交代"
    units = answer_units(mixed_claim)
    checks["claims_v2_surface_units"] = {
        "unit_count": len(units),
        "unit_texts": [u["text"] for u in units],
        "contains_guess_plan_unresolved_in_one_unit": len(units) == 1,
    }

    # 2. The existing prereg/closure logic can preserve OR semantics, but the
    # validator itself does not freeze the full criteria string. The audit
    # selector/gate supplies an exact criteria_source equality check.
    criteria = "持续扩大影响范围，或者发生明确的具体事件"
    prereg = {
        "criteria_source": criteria,
        "branches": [
            {"branch_id": "b1", "text": "持续扩大影响范围"},
            {"branch_id": "b2", "text": "发生明确的具体事件"},
        ],
        "closing": {"type": "OR", "closable": ["b1", "b2"]},
    }
    valid, reason = validate_prereg_row(prereg, criteria)
    expanded = criteria + "且结果必须被证实"
    valid_expanded, expanded_reason = validate_prereg_row(prereg, expanded)
    checks["closure_or_and_freeze"] = {
        "original_valid": valid,
        "original_reason": reason,
        "one_or_branch_closes": closing_satisfied(
            prereg["closing"],
            [{"branch_id": "b1", "evidence_id": "e1", "supporting_quote": "持续扩大影响范围"}],
            prereg,
        ),
        "branch_subset_still_valid_after_expansion": valid_expanded,
        "expanded_reason": expanded_reason,
        "criteria_source_exactly_matches_expansion": prereg["criteria_source"] == expanded,
        "freeze_requires_call_site_equality": True,
    }

    # 3. The runtime foreshadow item supports one question and one criteria
    # string; it has no persisted semantic subpromise collection.
    item = ForeshadowItem.from_dict(
        {
            "id": "fs_probe",
            "description": "复杂伏笔探针",
            "first_seen_chapter": 12,
            "first_seen_batch": 1,
            "last_seen_chapter": 12,
            "last_seen_batch": 1,
            "anchor_quote": "登记时原文",
            "anchor_chapter": 12,
            "question": "旧问题",
            "answer_criteria": criteria,
            "semantic_goals": [{"goal_id": "g1", "status": "unanswered"}],
        }
    )
    checks["ledger_shape"] = {
        "fields": [f.name for f in fields(item)],
        "unknown_semantic_goals_dropped_on_load": "semantic_goals" not in asdict(item),
        "one_question_field": "question" in asdict(item),
        "one_criteria_field": "answer_criteria" in asdict(item),
    }

    # 4. Existing chronology check flags evidence at/before registration, but
    # only as a soft signal. A reverse-link implementation must classify it as
    # pre-registration context rather than an answer to the registered question.
    base = {
        "anchor_quote": "登记时原文",
        "anchor_chapter": 12,
        "description": "旧问题",
        "supporting_quote": "另一段原文证据",
        "answer": "候选回答",
    }
    _, before_soft = compute_mechanical_signals({**base, "chapter": 11})
    _, after_soft = compute_mechanical_signals({**base, "chapter": 13})
    checks["registration_time_boundary"] = {
        "chapter_11_soft": before_soft,
        "chapter_11_flagged": "evidence_not_after_anchor" in before_soft,
        "chapter_13_flagged": "evidence_not_after_anchor" in after_soft,
        "is_hard_reject": "evidence_not_after_anchor" in compute_mechanical_signals(
            {**base, "chapter": 11}
        )[0],
    }

    # 5. Existing artifact forward compatibility: old payloads load because
    # unknown keys are filtered. This avoids crashes, but also means new history
    # must live in an explicit schema field or sidecar or it will be dropped.
    checks["compatibility_observation"] = {
        "unknown_fields_are_filtered_on_load": True,
        "implication": "new persistent history needs schema/versioned fields or a sidecar",
    }

    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
