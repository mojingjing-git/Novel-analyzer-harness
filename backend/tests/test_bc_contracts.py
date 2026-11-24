"""验收与证据协议契约测试：
覆盖 acceptance_gate、claim_evidence、claim_evidence_v2、closure。
"""
from __future__ import annotations

import copy

from backend.common.claim_evidence import validate_claim_audit
from backend.common.claim_evidence_v2 import (
    VERSION as CLAIM_V2_VERSION,
    answer_units,
    validate_claim_audit_v2,
)
from backend.common.closure import (
    closing_satisfied,
    validate_branch_hits,
    validate_prereg_row,
)
from scripts.acceptance_gate import (
    gate,
    label_status,
    mechanical_increment_check,
    ngram_dice,
    sample_fingerprint,
)

BASE_GATE_ITEM = {
    "question": "黑太岁的来源是什么？",
    "criteria": "说明来源或本质",
    "answer": "第499章交代第一次强行修真导致先天一炁不稳定",
    "quote": "强行修真虽然勉强也能达到效果，但也会让自己体内空中的先天一炁变得不稳定",
    "description": "李火旺第一次强行修真的记载",
    "anchor_quote": "他强行修炼了长春功，体内多出一股先天一炁",
    "anchor_chapter": 499,
    "chapter": 500,
    "evidence_segments": [
        {
            "evidence_id": "seg1",
            "chapter": 500,
            "text": "正文：强行修真虽然勉强也能达到效果，但也会让自己体内空中的先天一炁变得不稳定。",
        }
    ],
}

OK_NLI = {
    "verdict": "answer_ok",
    "reason": "新机制",
    "criteria_satisfied": True,
    "answer_supported": True,
    "has_increment": True,
}


def test_acceptance_gate_mechanical_flags_quote_identical_to_anchor():
    item = dict(BASE_GATE_ITEM, quote=BASE_GATE_ITEM["anchor_quote"])
    hard, _soft = mechanical_increment_check(item)
    assert "quote_equals_anchor" in hard
    assert gate(item, OK_NLI)["decision"] == "拒绝"


def test_acceptance_gate_mechanical_flags_answer_contained_in_known():
    item = dict(BASE_GATE_ITEM, answer="李火旺第一次强行修真的记载，他强行修炼了长春功")
    _hard, soft = mechanical_increment_check(item)
    assert "answer_in_known" in soft
    assert gate(item, dict(OK_NLI, has_increment=False))["decision"] == "存疑"


def test_acceptance_gate_mechanical_passes_genuine_increment():
    item = dict(BASE_GATE_ITEM)
    assert mechanical_increment_check(item) == ([], [])
    assert gate(item, OK_NLI)["decision"] == "接受"


def test_acceptance_gate_nli_restating_rejects():
    item = dict(BASE_GATE_ITEM, answer="他修炼长春功后体内多出了先天一炁")
    assert mechanical_increment_check(item) == ([], [])
    assert gate(item, {"verdict": "restating", "reason": "与锚点同义"})["decision"] == "拒绝"


def test_acceptance_gate_unclear_and_nli_error_go_to_doubt():
    item = dict(BASE_GATE_ITEM)
    assert gate(item, {"verdict": "unclear", "reason": "看不出来"})["decision"] == "存疑"
    assert gate(item, None, "调用失败")["decision"] == "存疑"
    assert gate(item, OK_NLI)["decision"] == "接受"


def test_acceptance_gate_nli_error_never_accepts_and_ngram_dice():
    assert gate(dict(BASE_GATE_ITEM), None, "调用失败")["decision"] == "存疑"
    assert ngram_dice("abc", "abc") == 1.0
    assert ngram_dice("abcdef", "abcxyz") < 0.5


def test_claim_evidence_v1_and_v2_contracts():
    ans = "李火旺确认丹阳子不识字，且排斥他人认字。"
    segs = [{"evidence_id": "seg1", "text": "正文：丹阳子确实不认识字，并且还非常非常排斥别人认字。"}]
    audit_v1 = {
        "version": "claims_v1",
        "units": [
            {
                "answer_text": ans,
                "relation": "supported",
                "answer_scope": "event",
                "evidence_scope": "event",
                "same_referent": True,
                "evidence": [{"evidence_id": "seg1", "quote": "丹阳子确实不认识字，并且还非常非常排斥别人认字。"}],
            }
        ],
    }
    res_v1 = validate_claim_audit(ans, segs, audit_v1)
    assert res_v1["eligible"] is True

    # Partition mismatch fails closed
    bad_v1 = copy.deepcopy(audit_v1)
    bad_v1["units"][0]["answer_text"] = "李火旺确认丹阳子不识字"
    assert "answer_not_fully_partitioned" in validate_claim_audit(ans, segs, bad_v1)["errors"]

    # claims_v2 deterministic units & frame checks
    units_v2 = answer_units(ans)
    assert len(units_v2) == 2
    frame_ok = {
        "attribution": "narrated",
        "holder": "",
        "world": "actual",
        "modality": "factual",
        "realization": "completed",
    }
    audit_v2 = {
        "version": CLAIM_V2_VERSION,
        "units": [
            {
                "unit_id": u["unit_id"],
                "relation": "supported",
                "same_referent": True,
                "answer_frame": frame_ok,
                "evidence_frame": frame_ok,
                "evidence": [{"evidence_id": "seg1", "quote": "丹阳子确实不认识字"}],
            }
            for u in units_v2
        ],
    }
    res_v2 = validate_claim_audit_v2(ans, segs, audit_v2)
    assert res_v2["eligible"] is True


def test_closure_prereg_and_branch_hits():
    criteria = "交代黑太岁的真实来历以及它对心素身体的改造代价"
    reg = {
        "branches": [
            {"branch_id": "b1", "text": "交代黑太岁的真实来历"},
            {"branch_id": "b2", "text": "对心素身体的改造代价"},
        ],
        "closing": {"type": "AND"},
    }
    ok, tag = validate_prereg_row(reg, criteria)
    assert ok is True and tag == "and"

    # Prefix-cut conjunction under OR is rejected
    bad_or = {
        "branches": reg["branches"],
        "closing": {"type": "OR", "closable": ["b1"]},
    }
    ok_or, reason_or = validate_prereg_row(bad_or, criteria)
    assert ok_or is False and "prefix_cut_conjunction" in reason_or

    segs = [{"evidence_id": "e1", "text": "黑太岁源自古神遗蜕，且会缓慢侵蚀心素五脏。"}]
    hits, errs = validate_branch_hits(
        reg,
        [
            {"branch_id": "b1", "evidence_id": "e1", "supporting_quote": "黑太岁源自古神遗蜕"},
            {"branch_id": "b2", "evidence_id": "e1", "supporting_quote": "会缓慢侵蚀心素五脏"},
        ],
        segs,
    )
    assert not errs and closing_satisfied(reg["closing"], hits, reg) is True


def test_acceptance_gate_label_binding_and_fingerprint():
    base_item = {
        "fs_id": "fs_1",
        "work_id": "w1",
        "label": "支持",
        "question": "q",
        "criteria": "c",
        "description": "d",
        "anchor_quote": "aq",
        "anchor_chapter": 1,
        "answer": "ans",
        "quote": "quote123",
        "chapter": 2,
        "evidence_segments": [{"evidence_id": "e1", "chapter": 2, "text": "quote123"}],
    }
    cur_item = {**base_item, "arm": "current"}
    cur_item["sample_id"] = sample_fingerprint(cur_item)
    cur_item["label_sample_id"] = cur_item["sample_id"]
    assert label_status(cur_item) == "bound"
    assert label_status(dict(cur_item, label_sample_id="stale_hash")) == "stale"
    assert label_status(dict(cur_item, label_sample_id=None)) == "unbound"
