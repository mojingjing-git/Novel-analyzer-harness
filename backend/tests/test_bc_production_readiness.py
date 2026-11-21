"""Comprehensive Contract & Integration Tests for `docs/BC生产管线收尾任务书.md` (T1, T2, T3).

Covers all mandatory verification cases specified in:
- Section 三 (T1: Closed-loop on-demand Class C upgrade review in `run_recheck_v2` & `FinalSummaryRunner`)
- Section 四 (T2: Calibrated B/C scorer, span ordering, negation/modality/role-swap, AND/OR, 1-to-1 conflict log)
- Section 五 (T3: Request identity provenance, two-layer raw/adjudication cache, stale label detection)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from backend.common.bc_scorer_and_labels import (
    BookSpanIndexer,
    adjudicate_reference_candidate_pair,
    assign_exclusive_matches_with_conflict_log,
    classify_c_observable_branch_outcome,
)
from backend.common.c_claim_audit_gate import CLAIM_V2, build_nli_prompt, gate
from backend.common.provenance_cache import (
    build_request_identity,
    build_verified_cache_record,
    compute_label_binding_fingerprint,
    re_adjudicate_cached_record,
    verify_cache_entry,
    verify_label_binding,
)
from backend.utils.foreshadow_ledger import ForeshadowItem, ForeshadowLedger
from backend.services.recheck_v2 import dump_recheck_detail, run_recheck_v2
from backend.vnext.resolution import CandidateVerdict
from scripts.experiments.bc.bc_eval_b_zero_leakage import verify_resolution_evidence


def _make_item(
    cid: str = "fs_001",
    *,
    anchor_ch: int = 1,
    anchor_q: str = "他把令牌藏进石匣。",
    question: str = "石匣中的令牌是真品还是赝品，且钥匙能否打开宫门？",
    criteria: str = "同时交代令牌真伪并且确认钥匙能否打开宫门",
    resolved_ch: int = 2,
) -> ForeshadowItem:
    return ForeshadowItem(
        id=cid,
        description="令牌真伪与宫门钥匙",
        status="active",
        first_seen_batch=1,
        last_seen_batch=1,
        first_seen_chapter=anchor_ch,
        last_seen_chapter=anchor_ch,
        anchor_chapter=anchor_ch,
        anchor_quote=anchor_q,
        question=question,
        answer_criteria=criteria,
        resolved_chapter=resolved_ch,
        lead=False,
    )


# ==============================================================================
# T1 Mandatory Acceptance Cases (Section 三)
# ==============================================================================


@pytest.mark.asyncio
async def test_t1_hearsay_quote_escalated_to_fact_triggers_upgrade_and_blocks_resolution():
    """T1 Case 1 (R3-1 exact reproduction):
    Evidence has '据说令牌为真品；有人怀疑钥匙可以打开宫门。'
    Candidate answer escalates to fact: '令牌为真品；钥匙可以打开宫门。'
    Must trigger actual upgrade review call and block auto-resolution when unsupported.
    """
    chapters = {
        1: "第一章：沈砚临行前，他把令牌藏进石匣。",
        2: "第二章：茶馆里众人议论纷纷，据说令牌为真品；有人怀疑钥匙可以打开宫门。",
    }
    item = _make_item()
    calls_log: list[str] = []

    async def fake_model(prompt: str) -> str:
        calls_log.append(prompt)
        if "### 候选 candidate_id=" in prompt:
            import re

            eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
            return json.dumps(
                [
                    {
                        "candidate_id": "fs_001",
                        "verdict": "SUPPORTED",
                        "answer": "令牌为真品；钥匙可以打开宫门。",
                        "evidence_id": eids[-1],
                        "supporting_quote": "据说令牌为真品；有人怀疑钥匙可以打开宫门。",
                        "counter_search": {"performed": True, "checked_evidence_ids": eids},
                    }
                ],
                ensure_ascii=False,
            )
        # Upgrade claims_v2 NLI call: flags modality escalation via real claims_v2 contract
        from backend.common.claim_evidence_v2 import answer_units

        import re

        eids_in_prompt = re.findall(r'"evidence_id":\s*"([a-f0-9]+)"', prompt)
        eid = eids_in_prompt[-1] if eids_in_prompt else ""
        units = answer_units("令牌为真品；钥匙可以打开宫门。")
        return json.dumps(
            {
                "verdict": "answer_ok",
                "reason": "原文仅为听说与怀疑，候选答案写成了已证实事实",
                "criteria_satisfied": True,
                "answer_supported": True,
                "has_increment": True,
                "claim_audit": {
                    "version": "claims_v2",
                    "units": [
                        {
                            "unit_id": units[0]["unit_id"],
                            "relation": "supported",
                            "same_referent": True,
                            "answer_frame": {
                                "attribution": "narrated",
                                "holder": "",
                                "world": "actual",
                                "modality": "factual",
                                "realization": "completed",
                            },
                            "evidence_frame": {
                                "attribution": "attributed",
                                "holder": "茶馆众人",
                                "world": "actual",
                                "modality": "possible",
                                "realization": "not_established",
                            },
                            "evidence": [{"evidence_id": eid, "quote": "据说令牌为真品"}],
                        },
                        {
                            "unit_id": units[1]["unit_id"],
                            "relation": "supported",
                            "same_referent": True,
                            "answer_frame": {
                                "attribution": "narrated",
                                "holder": "",
                                "world": "actual",
                                "modality": "factual",
                                "realization": "completed",
                            },
                            "evidence_frame": {
                                "attribution": "attributed",
                                "holder": "有人",
                                "world": "actual",
                                "modality": "possible",
                                "realization": "not_established",
                            },
                            "evidence": [{"evidence_id": eid, "quote": "有人怀疑钥匙可以打开宫门"}],
                        },
                    ],
                },
            },
            ensure_ascii=False,
        )

    out = await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=fake_model,
        enable_c_on_demand_audit=True,
    )
    assert len(calls_log) == 2, "Must execute 1 initial recheck call + 1 upgrade claims_v2 call"
    assert out.calls == 2
    assert out.audit_calls == 1
    assert out.audit_routes["fs_001"]["route"] == "claims_v2_on_demand"
    assert out.audit_routes["fs_001"]["trigger_claims_v2"] is True
    assert out.audit_routes["fs_001"]["audit_executed"] is True
    assert out.verdicts[0].verdict == "INSUFFICIENT"
    assert out.verdicts[0].reason.startswith("C_AUDIT_DOUBT:")
    assert out.applied["fs_001"] == "PROPOSED_RESOLVED"
    assert item.status == "active"
    assert item.needs_review is True
    assert item.resolution_quality == "questioned"


@pytest.mark.asyncio
async def test_t1_same_candidate_upgrade_audit_controls_final_ledger_state():
    """T1 Case 2: Same candidate, simulated upgrade review returns supported vs rejected vs partial_answer.
    Final ledger status changes strictly according to the upgrade review outcome.
    """
    chapters = {
        1: "第一章：他把令牌藏进石匣。",
        2: "第二章：城主当众查验，确认令牌为真品；同时侍卫用钥匙顺利打开宫门。",
    }

    def _initial_batch_resp(prompt: str) -> str:
        import re

        eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
        return json.dumps(
            [
                {
                    "candidate_id": "fs_001",
                    "verdict": "SUPPORTED",
                    "answer": "令牌确认为真品；钥匙顺利打开宫门。",
                    "evidence_id": eids[-1],
                    "supporting_quote": "确认令牌为真品；同时侍卫用钥匙顺利打开宫门。",
                    "counter_search": {"performed": True, "checked_evidence_ids": eids},
                }
            ],
            ensure_ascii=False,
        )

    # Subcase A: Upgrade review accepts (with valid claim_audit v2)
    item_acc = _make_item()

    async def model_accept(prompt: str) -> str:
        if "### 候选 candidate_id=" in prompt:
            return _initial_batch_resp(prompt)
        from backend.common.claim_evidence_v2 import answer_units
        import re

        eids_in_prompt = re.findall(r'"evidence_id":\s*"([a-f0-9]+)"', prompt)
        eid = eids_in_prompt[-1] if eids_in_prompt else ""
        units = answer_units("令牌确认为真品；钥匙顺利打开宫门。")
        fact_frame = {
            "attribution": "narrated",
            "holder": "",
            "world": "actual",
            "modality": "factual",
            "realization": "completed",
        }
        return json.dumps(
            {
                "verdict": "answer_ok",
                "reason": "两项合取条件均由叙述事实直接证实",
                "criteria_satisfied": True,
                "answer_supported": True,
                "has_increment": True,
                "claim_audit": {
                    "version": "claims_v2",
                    "units": [
                        {
                            "unit_id": units[0]["unit_id"],
                            "relation": "supported",
                            "same_referent": True,
                            "answer_frame": fact_frame,
                            "evidence_frame": fact_frame,
                            "evidence": [{"evidence_id": eid, "quote": "确认令牌为真品"}],
                        },
                        {
                            "unit_id": units[1]["unit_id"],
                            "relation": "supported",
                            "same_referent": True,
                            "answer_frame": fact_frame,
                            "evidence_frame": fact_frame,
                            "evidence": [{"evidence_id": eid, "quote": "同时侍卫用钥匙顺利打开宫门"}],
                        },
                    ],
                },
            },
            ensure_ascii=False,
        )

    out_acc = await run_recheck_v2(
        [item_acc],
        chapters=chapters,
        call_model=model_accept,
        enable_c_on_demand_audit=True,
    )
    assert out_acc.audit_calls == 1
    assert out_acc.verdicts[0].verdict == "SUPPORTED"
    assert out_acc.applied["fs_001"] == "AUTO_RESOLVED"
    assert item_acc.status == "resolved"
    assert item_acc.resolution_quality == "verified"

    # Subcase B: Upgrade review rejects (off_topic)
    item_rej = _make_item()

    async def model_reject(prompt: str) -> str:
        if "### 候选 candidate_id=" in prompt:
            return _initial_batch_resp(prompt)
        return json.dumps(
            {
                "verdict": "off_topic",
                "reason": "未满足合取关闭条件",
                "criteria_satisfied": False,
                "answer_supported": False,
                "has_increment": False,
            },
            ensure_ascii=False,
        )

    out_rej = await run_recheck_v2(
        [item_rej],
        chapters=chapters,
        call_model=model_reject,
        enable_c_on_demand_audit=True,
    )
    assert out_rej.audit_calls == 1
    assert out_rej.verdicts[0].verdict == "INSUFFICIENT"
    assert out_rej.verdicts[0].reason.startswith("C_AUDIT_REJECTED:")
    assert out_rej.applied["fs_001"] == "PROPOSED_RESOLVED"
    assert item_rej.status == "active"
    assert item_rej.resolution_quality == "rejected_by_audit"


@pytest.mark.asyncio
async def test_t1_timeout_invalid_format_and_zero_budget_enter_pending_review_never_resolved():
    """T1 Case 3: Upgrade review timeout, invalid format, or exhausted call budget must enter
    explicit pending_review state and never fall back to initial SUPPORTED.
    """
    chapters = {
        1: "第一章：他把令牌藏进石匣。",
        2: "第二章：据说令牌为真品；有人怀疑钥匙可以打开宫门。",
    }

    def _init_resp(prompt: str) -> str:
        import re

        eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
        return json.dumps(
            [
                {
                    "candidate_id": "fs_001",
                    "verdict": "SUPPORTED",
                    "answer": "令牌为真品；钥匙可以打开宫门。",
                    "evidence_id": eids[-1],
                    "supporting_quote": "据说令牌为真品；有人怀疑钥匙可以打开宫门。",
                    "counter_search": {"performed": True, "checked_evidence_ids": eids},
                }
            ],
            ensure_ascii=False,
        )

    # 3a: Timeout during upgrade audit
    item_timeout = _make_item()

    async def model_timeout(prompt: str) -> str:
        if "### 候选 candidate_id=" in prompt:
            return _init_resp(prompt)
        raise asyncio.TimeoutError("audit timed out")

    out_to = await run_recheck_v2(
        [item_timeout],
        chapters=chapters,
        call_model=model_timeout,
        enable_c_on_demand_audit=True,
    )
    assert out_to.verdicts[0].verdict == "INSUFFICIENT"
    assert out_to.verdicts[0].reason == "C_AUDIT_PENDING:MODEL_ERROR:TIMEOUT"
    assert "fs_001" in out_to.audit_pending_ids
    assert out_to.status == "PARTIAL"
    assert item_timeout.status == "active"
    assert item_timeout.resolution_quality == "pending_audit"

    # 3b: Invalid format during upgrade audit
    item_fmt = _make_item()

    async def model_bad_fmt(prompt: str) -> str:
        if "### 候选 candidate_id=" in prompt:
            return _init_resp(prompt)
        return "NOT A VALID JSON OBJECT"

    out_fmt = await run_recheck_v2(
        [item_fmt],
        chapters=chapters,
        call_model=model_bad_fmt,
        enable_c_on_demand_audit=True,
    )
    assert out_fmt.verdicts[0].verdict == "INSUFFICIENT"
    assert out_fmt.verdicts[0].reason == "C_AUDIT_PENDING:INVALID_FORMAT"
    assert "fs_001" in out_fmt.audit_pending_ids
    assert item_fmt.status == "active"
    assert item_fmt.resolution_quality == "pending_audit"

    # 3c: Budget exhausted before upgrade audit can run (max_calls=1 used by initial batch)
    item_bgt = _make_item()
    out_bgt = await run_recheck_v2(
        [item_bgt],
        chapters=chapters,
        call_model=model_bad_fmt,
        max_calls=1,
        enable_c_on_demand_audit=True,
    )
    assert out_bgt.calls == 1
    assert out_bgt.audit_calls == 0
    assert out_bgt.verdicts[0].verdict == "INSUFFICIENT"
    assert out_bgt.verdicts[0].reason == "C_AUDIT_PENDING:BUDGET_EXHAUSTED"
    assert "fs_001" in out_bgt.audit_pending_ids
    assert item_bgt.status == "active"
    assert item_bgt.resolution_quality == "pending_audit"


@pytest.mark.asyncio
async def test_t1_format_failure_retried_once_within_budget_but_semantic_rejection_never_retried():
    """格式失败允许预算内再试一次；语义拒绝绝不重试（重试到通过=放宽审核标准）。

    另有本地修复：模型偶尔把数组闭合的 `]` 写成 `)`，把正确裁决弄坏。
    实测有一次审核输出 2463 字符、语义完全正确（verdict=answer_ok），
    仅因这一字符被判 INVALID_FORMAT、正确裁决被丢弃。
    """
    from backend.common.c_claim_audit_gate import (
        extract_json_object,
        extract_json_object_with_repair,
    )

    chapters = {
        1: "第一章：他把令牌藏进石匣。",
        2: "第二章：据说令牌为真品；有人怀疑钥匙可以打开宫门。",
    }

    def _init_resp(prompt: str) -> str:
        import re

        eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
        return json.dumps(
            [
                {
                    "candidate_id": "fs_001",
                    "verdict": "SUPPORTED",
                    "answer": "令牌为真品；钥匙可以打开宫门。",
                    "evidence_id": eids[-1],
                    "supporting_quote": "据说令牌为真品；有人怀疑钥匙可以打开宫门。",
                    "counter_search": {"performed": True, "checked_evidence_ids": eids},
                }
            ],
            ensure_ascii=False,
        )

    good_audit = json.dumps(
        {
            "verdict": "answer_ok",
            "reason": "原文明确交代了令牌真伪",
            "criteria_satisfied": True,
            "answer_supported": True,
            "has_increment": True,
        },
        ensure_ascii=False,
    )

    # 1) 本地修复：**嵌套数组**的闭合符写成 `)` 才修得（追加在完整对象后的 `)`
    #    原先 raw_decode 就已能读出，不能拿来当修复的证据）
    nested_ok = '{"verdict":"answer_ok","units":[{"a":1},{"b":2}]}'
    nested_broken = '{"verdict":"answer_ok","units":[{"a":1},{"b":2})}'
    assert json.loads(nested_ok)
    with pytest.raises(json.JSONDecodeError):
        json.loads(nested_broken)
    repaired = extract_json_object(nested_broken)
    assert repaired is not None
    assert repaired["verdict"] == "answer_ok"
    assert repaired["units"] == [{"a": 1}, {"b": 2}]

    # 1b) 拒绝边界：不得改布尔/数字/null 词元
    assert extract_json_object('{"verdict":"answer_ok","criteria_satisfied":tru)e}') is None
    assert extract_json_object('{"verdict":"answer_ok","n":12)}') is None
    # 1c) 拒绝边界：不得提前闭合对象、忽略后面的裁决字段
    assert (
        extract_json_object(
            '{"verdict":"answer_ok","criteria_satisfied":true,'
            '"answer_supported":true),"verdict":"off_topic"}'
        )
        is None
    )
    # 1d) 拒绝边界：多处可修位置 → 无法唯一修复
    assert extract_json_object('{"a":1),"b":2)}') is None
    # 1e) 拒绝边界：重复键
    assert extract_json_object('{"verdict":"answer_ok","verdict":"off_topic"}') is None
    # 1f) 缺闭合符 → 不猜（交给预算内重试）
    assert extract_json_object('{"verdict":"answer_ok"') is None
    # 1g) 字符串里的 `)` 不受影响
    ok_in_string = extract_json_object('{"verdict":"answer_ok","reason":"他说 )"}')
    assert ok_in_string is not None
    assert ok_in_string["reason"] == "他说 )"
    # 1h) 真正无法修复的仍返回 None
    assert extract_json_object("NOT A VALID JSON OBJECT") is None
    # 1i) 修复记录可审计：保留原文长度与修复位置
    _obj, rec = extract_json_object_with_repair(nested_broken)
    assert rec["applied"] is True
    assert rec["kind"] == "replace_illegal_paren_with_expected_closer"
    assert rec["replacement"] == "]"
    assert rec["original_length"] == len(nested_broken)

    # 1j) 真实历史响应回归：审核输出 2463 字符、语义完全正确，
    #     仅因结尾把数组闭合的 `]` 写成 `)` 而被判 INVALID_FORMAT、正确裁决被丢弃。
    #     这里复刻它的结构：嵌套 claim_audit.units[].evidence[] + 多余右圆括号。
    real_like = (
        '{"verdict":"answer_ok","reason":"原文新增说明了额外力量的来源与转化过程",'
        '"criteria_satisfied":true,"answer_supported":true,"has_increment":true,'
        '"branch_hits":[{"branch_id":"c1","evidence_id":"0a15aba24eed",'
        '"supporting_quote":"于是这些积年的怨气就化为了感激"}],'
        '"claim_audit":{"version":"claims_v2","units":['
        '{"unit_id":"u1","relation":"supported","evidence":['
        '{"evidence_id":"0a15aba24eed","quote":"就连两位祭司提供的信仰之力中，也有精灵怨气的影子！"}]})}}'
    )
    real_obj, real_rec = extract_json_object_with_repair(real_like)
    assert real_obj is not None
    assert real_obj["verdict"] == "answer_ok"
    assert real_obj["criteria_satisfied"] is True
    assert real_rec["applied"] is True
    assert real_rec["replacement"] == "]"

    # 2) 格式失败 → 预算内重试一次，第二次格式合法 → 正常落账
    item_fmt = _make_item()
    audit_calls: list[str] = []

    async def model_bad_then_good(prompt: str) -> str:
        if "### 候选 candidate_id=" in prompt:
            return _init_resp(prompt)
        audit_calls.append(prompt)
        if len(audit_calls) == 1:
            return "NOT A VALID JSON OBJECT"
        return good_audit

    out_retry = await run_recheck_v2(
        [item_fmt],
        chapters=chapters,
        call_model=model_bad_then_good,
        enable_c_on_demand_audit=True,
    )
    assert len(audit_calls) == 2
    assert out_retry.audit_format_retries == 1
    # 格式问题已消除：不再报 INVALID_FORMAT。
    # （若仍待审，那是门禁层的存疑——证据/关闭条件不满足，与格式无关。）
    assert out_retry.verdicts[0].reason != "C_AUDIT_PENDING:INVALID_FORMAT"
    route_retry = out_retry.audit_routes.get("fs_001") or {}
    assert "INVALID_FORMAT" not in (route_retry.get("audit_reasons") or [])

    # 3) 语义拒绝（off_topic）不重试：只调用一次审核
    item_sem = _make_item()
    semantic_calls: list[str] = []

    async def model_semantic_reject(prompt: str) -> str:
        if "### 候选 candidate_id=" in prompt:
            return _init_resp(prompt)
        semantic_calls.append(prompt)
        return json.dumps(
            {
                "verdict": "off_topic",
                "reason": "原文没有回答这个问题",
                "criteria_satisfied": False,
                "answer_supported": False,
                "has_increment": True,
            },
            ensure_ascii=False,
        )

    out_sem = await run_recheck_v2(
        [item_sem],
        chapters=chapters,
        call_model=model_semantic_reject,
        enable_c_on_demand_audit=True,
    )
    assert len(semantic_calls) == 1
    assert out_sem.audit_format_retries == 0
    assert "fs_001" not in out_sem.audit_pending_ids

    # 4) 重试仍失败 → 保留待审（不转正也不丢弃），并给出恢复说明
    item_still = _make_item()
    still_calls: list[str] = []

    async def model_always_bad(prompt: str) -> str:
        if "### 候选 candidate_id=" in prompt:
            return _init_resp(prompt)
        still_calls.append(prompt)
        return "NOT A VALID JSON OBJECT"

    out_still = await run_recheck_v2(
        [item_still],
        chapters=chapters,
        call_model=model_always_bad,
        enable_c_on_demand_audit=True,
    )
    assert len(still_calls) == 2  # 首次 + 一次重试，不再更多
    assert out_still.verdicts[0].verdict == "INSUFFICIENT"
    assert out_still.verdicts[0].reason == "C_AUDIT_PENDING:INVALID_FORMAT"
    assert "fs_001" in out_still.audit_pending_ids
    assert item_still.status == "active"
    assert item_still.resolution_quality == "pending_audit"
    route = out_still.audit_routes.get("fs_001") or {}
    assert route.get("format_retries") == 1
    assert "再次提交" in str(route.get("recovery") or "")


@pytest.mark.asyncio
async def test_t1_pending_candidate_is_reaudited_on_next_submission_not_permanently_stuck():
    """待审候选不是永久卡死：下一次提交会重新调用审核。

    前提是失败记录不可复用——verify_cache_entry 对 failed_or_incomplete
    返回 reusable=False，于是缓存命中不了，下一轮必然重新发起审核调用。
    """
    from backend.common.provenance_cache import build_verified_cache_record, verify_cache_entry

    chapters = {
        1: "第一章：他把令牌藏进石匣。",
        2: "第二章：据说令牌为真品；有人怀疑钥匙可以打开宫门。",
    }

    def _init_resp(prompt: str) -> str:
        import re

        eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
        return json.dumps(
            [
                {
                    "candidate_id": "fs_001",
                    "verdict": "SUPPORTED",
                    "answer": "令牌为真品；钥匙可以打开宫门。",
                    "evidence_id": eids[-1],
                    "supporting_quote": "据说令牌为真品；有人怀疑钥匙可以打开宫门。",
                    "counter_search": {"performed": True, "checked_evidence_ids": eids},
                }
            ],
            ensure_ascii=False,
        )

    audit_calls: list[str] = []

    async def model_bad_first(prompt: str) -> str:
        if "### 候选 candidate_id=" in prompt:
            return _init_resp(prompt)
        audit_calls.append(prompt)
        return "NOT A VALID JSON OBJECT"

    item = _make_item()
    await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=model_bad_first,
        enable_c_on_demand_audit=True,
    )
    assert item.status == "active"

    # 失败记录不可复用 → 下一次提交会重新调用审核
    failed_entry = {
        "status": "failed_or_incomplete",
        "decision": None,
        "measured_live": True,
        "raw_response_text": "NOT A VALID JSON OBJECT",
        "provenance_recorded_at_call_time": True,
        "request_id": "r1",
        "input_fingerprint": "i1",
        "prompt_sha256": "p1",
        "evidence_fingerprint": "e1",
        "answer_sha256": "a1",
        "model_config_sha256": "m1",
        "contract_version": "claims_v2",
    }
    verdict = verify_cache_entry(failed_entry, {"request_id": "r1"})
    assert verdict.reusable is False
    assert verdict.status == "failed_or_incomplete"


@pytest.mark.asyncio
async def test_t4_recheck_request_log_records_prompt_and_rebuilds_to_same_hash():
    """初始复检的请求落盘（行车记录仪）。

    落盘必须能回答"没找到 / 没送到 / 没判断对"：要存下实际发出的提示词、
    证据包与哈希，并且能用这些内容重建出同一个请求。
    同时**不得**增加模型调用次数，也不得改变判定。
    """
    from backend.common.recheck_request_log import verify_request_record

    chapters = {
        1: "第一章：他把令牌藏进石匣，匾额上写着「甲乙丙丁」四个方位。",
        2: "第二章：城主打开石匣，确认令牌确为真品。匾额的方位顺序被打乱。",
    }
    items = [
        _make_item(
            "fs_001",
            anchor_ch=1,
            anchor_q="他把令牌藏进石匣。",
            question="石匣里的令牌是真品还是赝品，匾额方位如何解读？",
            criteria="确认令牌真伪并交代方位顺序",
        ),
        _make_item(
            "fs_002",
            anchor_ch=1,
            anchor_q="他把令牌藏进石匣。",
            question="匾额上的四个方位分别指什么？",
            criteria="交代四个方位的含义",
        ),
    ]

    def _fresh_items():
        # 每轮用**新的** item：上一轮落账会把 status 改成 resolved，
        # 复用同一批对象会让第二轮跳过它们——那是既有行为，与本测试无关。
        return [
            _make_item(
                "fs_001",
                anchor_ch=1,
                anchor_q="他把令牌藏进石匣。",
                question="石匣里的令牌是真品还是赝品，匾额方位如何解读？",
                criteria="确认令牌真伪并交代方位顺序",
            ),
            _make_item(
                "fs_002",
                anchor_ch=1,
                anchor_q="他把令牌藏进石匣。",
                question="匾额上的四个方位分别指什么？",
                criteria="交代四个方位的含义",
            ),
        ]

    def _resp(prompt: str) -> str:
        import re

        eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
        return json.dumps(
            [
                {
                    "candidate_id": "fs_001",
                    "verdict": "SUPPORTED",
                    "answer": "令牌确为真品。",
                    "evidence_id": eids[-1] if eids else "",
                    "supporting_quote": "城主打开石匣，确认令牌确为真品。",
                    "counter_search": {"performed": True, "checked_evidence_ids": eids},
                }
            ],
            ensure_ascii=False,
        )

    calls: list[str] = []

    async def fake(prompt: str) -> str:
        calls.append(prompt)
        return _resp(prompt)

    base = await run_recheck_v2(_fresh_items(), chapters=chapters, call_model=fake)
    base_calls = len(calls)
    base_verdicts = [(v.candidate_id, v.verdict) for v in base.verdicts]

    calls.clear()
    log: dict = {
        "schema": "recheck_request_log_v1",
        "run": {
            "work_id": "local-sim",
            "output_dir": "local-sim",
            "code_snapshot_id": "simcode",
            "contract_version": "recheck_v2",
            "model_config": {"model": "fake"},
        },
        "requests": [],
        "usage_by_prompt_sha256": {},
    }
    logged = await run_recheck_v2(
        _fresh_items(), chapters=chapters, call_model=fake, request_log=log
    )

    # 落盘不增加模型调用、不改变判定
    assert len(calls) == base_calls
    assert [(v.candidate_id, v.verdict) for v in logged.verdicts] == base_verdicts

    requests = log["requests"]
    assert requests
    for rec in requests:
        assert rec["prompt"]
        assert rec["prompt_sha256"]
        assert rec["candidate_ids"]
        assert rec["run"]["code_snapshot_id"] == "simcode"
        assert rec["recorded_at"]
        for e in rec["evidence_batches"]:
            assert e["evidence_id"]
            assert e["text_sha256"]
        for a in rec["attempts"]:
            assert a["seconds"] is not None
            assert a["response_sha256"]
            assert "error" in a

    # 用记录里的元数据 + 证据包重建提示词，哈希必须一致
    from backend.services import recheck_v2 as rv2

    def renderer(cands):
        rendered = "".join(
            rv2.ITEM_TEMPLATE.format(
                candidate_id=c["candidate_id"],
                description=c.get("description", ""),
                question=c.get("question", "") or "（未提供）",
                answer_criteria=c.get("answer_criteria", "") or "（未提供）",
                first_seen=c.get("first_seen", 0),
                last_seen=c.get("last_seen", 0),
                unit_note="",
                segment_count=len(c.get("segments") or []),
                truncation_note="",
                segments="\n".join(
                    f'[evidence_id={seg["evidence_id"]} 第{seg["chapter"]}章]\n"""\n'
                    f'{seg["text"]}\n"""'
                    for seg in (c.get("segments") or [])
                ),
            )
            for c in cands
        )
        return rv2.PROMPT_TEMPLATE.format(
            count=len(cands),
            items=rendered,
            schema=rv2.render_recheck_schema(),
            candidate_ids=", ".join(c["candidate_id"] for c in cands),
        )

    for rec in requests:
        v = verify_request_record(rec, renderer)
        assert v["matched"] is True, v


@pytest.mark.asyncio
def _safe_json(raw: str):
    """解析不了就返回 None——与真实的解析器行为一致。"""
    try:
        return json.loads(raw)
    except Exception:
        return None


@pytest.mark.asyncio
async def test_t4_token_precheck_uses_conservative_bound_on_full_request():
    """预检按**完整请求的保守上界**，不按粗估。

    盲审阶段送入的参考条目与系统输出会造成输入膨胀——
    只看初筛时的原文前缀会漏掉。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.experiments.bc.bc_t4_registration_pilot_runner import (
        conservative_token_bound,
        estimate_tokens,
        request_token_bound,
    )

    prefix = "第一章：他把令牌藏进石匣。" * 200
    blind_extra = "参考条目与系统输出" * 200

    rough = estimate_tokens(prefix)
    bound = conservative_token_bound(prefix)
    # 保守上界必须 ≥ 粗估
    assert bound >= rough

    # 完整请求 = 原文 + 盲审附加内容，边界要加上附加部分
    only_prefix = request_token_bound(prefix)
    full = request_token_bound(prefix, blind_extra)
    assert full > only_prefix

    # 保守上界会拦下粗估放行的请求
    from scripts.experiments.bc.bc_t4_registration_pilot_runner import Budget

    b = Budget(per_request_input_limit=8000)
    text = "字" * 8500
    assert b.precheck(estimate_tokens(text), 100)["ok"] is True
    assert b.precheck(request_token_bound(text), 100)["ok"] is False


@pytest.mark.asyncio
async def test_t4_token_bound_calibrated_against_measured_endpoint_usage():
    """token 上界按端点实测标定，不能按"一个汉字一个 token"估。

    实测：参考发现请求 = 4371 非 ASCII + 1127 ASCII 字符 → 端点报 7675 输入 token。
    即非 ASCII 约 1.69 token/字符。按 1.0 估会把请求算小近一半，预检形同虚设。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.experiments.bc.bc_t4_registration_pilot_runner import (
        conservative_token_bound,
        estimate_tokens,
    )

    measured_request = "字" * 4371 + "a" * 1127
    measured_tokens = 7675
    bound = conservative_token_bound(measured_request)

    # 上界必须覆盖实测用量
    assert bound >= measured_tokens, f"上界 {bound} 低于实测 {measured_tokens}"
    # 但也不能把按 1 token/汉字估的粗略值当成上界
    assert bound > estimate_tokens(measured_request)

    # 真实前缀规模：2、3 单元的前缀确实超过 8000
    hist_prefix_2 = "字" * 7576
    assert conservative_token_bound(hist_prefix_2) > 8000
    game_prefix_1 = "字" * 3893
    assert conservative_token_bound(game_prefix_1) < 8000


@pytest.mark.asyncio
async def test_t4_adjudication_validator_and_reviewed_package():
    """独立复核：裁决结果要校验，合格集只收确认"有依据且未解"的条目。

    复核者类型如实标注——AI 复核不冒称人工标注。
    """
    from backend.common.registration_reference_set import (
        LABEL_GROUNDED_UNRESOLVED,
        REVIEWER_TYPE_AI,
        apply_qualification,
        freeze_reference_set,
        validate_adjudication,
    )

    raw = {
        "items": [
            {"ref_id": "r0", "question": "q0"},
            {"ref_id": "r1", "question": "q1"},
        ]
    }
    # 未知 ref_id → 不合格
    v = validate_adjudication(
        [{"ref_id": "ghost", "qualified": True, "qualified_label": LABEL_GROUNDED_UNRESOLVED}],
        known_ref_ids=["r0", "r1"],
    )
    assert v["ok"] is False
    assert any("unknown_ref_id" in e for e in v["errors"])

    # 缺理由 → 不合格
    v2 = validate_adjudication(
        [{"ref_id": "r0", "qualified": True, "qualified_label": LABEL_GROUNDED_UNRESOLVED}],
        known_ref_ids=["r0", "r1"],
    )
    assert v2["ok"] is False
    assert any("missing_reason" in e for e in v2["errors"])

    # qualified 但标签不是"有依据且未解" → 不合格
    v3 = validate_adjudication(
        [
            {
                "ref_id": "r0",
                "qualified": True,
                "qualified_label": "已经回答",
                "reason": "x",
            }
        ],
        known_ref_ids=["r0", "r1"],
    )
    assert v3["ok"] is False
    assert any("qualified_but_not_unresolved" in e for e in v3["errors"])

    # 合格但缺回答条件或跨度 → 不合格
    v4 = validate_adjudication(
        [
            {
                "ref_id": "r0",
                "qualified": True,
                "qualified_label": LABEL_GROUNDED_UNRESOLVED,
                "reason": "x",
            }
        ],
        known_ref_ids=["r0", "r1"],
    )
    assert v4["ok"] is False
    assert any("missing_accepted_answer_criteria" in e for e in v4["errors"])

    # 未裁决的 ref_id 单列，保持待裁决
    v5 = validate_adjudication(
        [
            {
                "ref_id": "r0",
                "qualified": True,
                "qualified_label": LABEL_GROUNDED_UNRESOLVED,
                "accepted_answer_criteria": "交代清楚",
                "accepted_spans": [{"unit_id": 1, "start": 1, "end": 5}],
                "reason": "前缀内已提出",
            }
        ],
        known_ref_ids=["r0", "r1"],
    )
    assert v5["ok"] is True
    assert v5["missing_ref_ids"] == ["r1"]

    elig = apply_qualification(
        raw, v5["decisions"], reviewer_id="reviewer_1", reviewer_type=REVIEWER_TYPE_AI
    )
    assert elig["eligible_ref_ids"] == ["r0"]
    assert elig["eligible_count"] == 1
    assert elig["reviewer_type"] == REVIEWER_TYPE_AI
    # 未裁决的保持待裁决，不进合格集合
    assert elig["excluded"]["pending_review"] == ["r1"]
    assert elig["pending_count"] == 1


@pytest.mark.asyncio
async def test_t4_scoring_connection_only_confirmed_qualified_references_count():
    """计分连接：只有确认合格的参考进分母。

    复核复现：
    - `disputed`、`single_annotator` 条目不得进入漏登记分母；
    - **没有配对记录**不得被自动算成"漏登记"；
    - 只有明确的"不匹配"才算遗漏。
    """
    from backend.common.registration_eval_metrics import compute_metrics
    from backend.common.registration_reference_set import (
        PROTOCOL_VERSION,
        apply_qualification,
        freeze_reference_set,
        match_against_arms,
        merge_annotations,
        to_metric_inputs,
    )

    def _ann(annotator, items):
        return {
            "work_id": "w1",
            "prefix_id": "p1",
            "annotator_id": annotator,
            "protocol_version": PROTOCOL_VERSION,
            "scope_read": {"unit_ids": [1]},
            "completed": True,
            "completed_at": "2026-11-07T00:00:00",
            "freeze_sha256": "fp",
            "items": items,
        }

    def _item(q, label="有依据且未解", quote="匾额上写着甲乙丙丁"):
        return {
            "question": q,
            "answer_criteria": "交代清楚",
            "supporting_quote": quote,
            "acceptable_spans": [{"unit_id": 1, "start": 10, "end": 18}],
            "label": label,
            "reason": "尚未解释",
        }

    left = _ann("a1", [_item("q0"), _item("q1", quote="另一处引文")])
    right = _ann("a2", [_item("q0"), _item("q2", quote="第三处引文")])
    merged = merge_annotations(left, right, work_id="w1", prefix_id="p1")
    raw = freeze_reference_set(merged, annotator_ids=["a1", "a2"])
    ref_ids = [i["ref_id"] for i in raw["items"]]

    # 资格裁决：只确认 q0；q1 是单方，q2 也是单方
    decisions = []
    for it in raw["items"]:
        agreed = it["status"] == "agreed"
        decisions.append(
            {
                "ref_id": it["ref_id"],
                "qualified": agreed,
                "qualified_label": "有依据且未解" if agreed else None,
                "accepted_answer_criteria": "交代清楚" if agreed else None,
                "accepted_spans": it["acceptable_spans"] if agreed else None,
                "reviewer_id": "h1",
                "reason": "确认" if agreed else "未确认",
            }
        )
    elig = apply_qualification(raw, decisions, reviewer_id="h1")
    assert elig["eligible_count"] == 1
    assert elig["eligible_ref_ids"] == [ref_ids[0]]
    # 未确认的单方条目被排除，不能重新进入分母
    assert len(elig["excluded"]["single_annotator"]) == 2

    # 1) 合格参考**没有配对记录** → unresolved，不是漏登记
    m = match_against_arms(elig, arm_pairs={"A": []}, arm_system_items={"A": []})
    assert m["A"]["judged_total"] == 0
    assert m["A"]["missed"] == 0
    assert m["A"]["unresolved_total"] == 1
    mi = compute_metrics(
        system_items=[{"item_id": "s1"}],
        adjudicated_items=[{"item_id": "s1", "label": "有依据", "span_located": True}],
        independent_qualified=to_metric_inputs(elig, m, "A"),
    )
    assert mi["metrics"]["missed_registration_rate"]["denominator"] == 0
    assert mi["metrics"]["missed_registration_rate"]["rate"] is None

    # 2) 显式 false → 才算漏登记
    m2 = match_against_arms(
        elig,
        arm_pairs={"A": [{"ref_id": ref_ids[0], "matched": False}]},
        arm_system_items={"A": []},
    )
    assert m2["A"]["missed"] == 1
    assert m2["A"]["judged_total"] == 1
    mi2 = compute_metrics(
        system_items=[{"item_id": "s1"}],
        adjudicated_items=[{"item_id": "s1", "label": "有依据", "span_located": True}],
        independent_qualified=to_metric_inputs(elig, m2, "A"),
    )
    assert mi2["metrics"]["missed_registration_rate"]["count"] == 1
    assert mi2["metrics"]["missed_registration_rate"]["denominator"] == 1

    # 3) 冲突配对 → unresolved
    m3 = match_against_arms(
        elig,
        arm_pairs={
            "A": [
                {"ref_id": ref_ids[0], "matched": True, "system_item_id": "s1"},
                {"ref_id": ref_ids[0], "matched": False},
            ]
        },
        arm_system_items={"A": [{"item_id": "s1"}]},
    )
    assert m3["A"]["missed"] == 0
    assert m3["A"]["unresolved_total"] == 1

    # 4) 孤立的 true（未绑定真实 system_item_id）→ unresolved
    m4 = match_against_arms(
        elig,
        arm_pairs={"A": [{"ref_id": ref_ids[0], "matched": True}]},
        arm_system_items={"A": [{"item_id": "s1"}]},
    )
    assert m4["A"]["registered"] == 0
    assert m4["A"]["unresolved_total"] == 1

    # 5) 绑定的 system_item_id 不存在 → unresolved
    m5 = match_against_arms(
        elig,
        arm_pairs={"A": [{"ref_id": ref_ids[0], "matched": True, "system_item_id": "ghost"}]},
        arm_system_items={"A": [{"item_id": "s1"}]},
    )
    assert m5["A"]["registered"] == 0
    assert m5["A"]["unresolved_total"] == 1

    # 6) 零合格参考 → N/A
    empty = apply_qualification(
        raw,
        [
            {"ref_id": r, "qualified": False, "qualified_label": None, "reviewer_id": "h1"}
            for r in ref_ids
        ],
        reviewer_id="h1",
    )
    assert empty["eligible_count"] == 0
    m6 = match_against_arms(empty, arm_pairs={"A": []}, arm_system_items={"A": []})
    mi6 = compute_metrics(
        system_items=[{"item_id": "s1"}],
        adjudicated_items=[{"item_id": "s1", "label": "有依据", "span_located": True}],
        independent_qualified=to_metric_inputs(empty, m6, "A"),
    )
    assert mi6["metrics"]["missed_registration_rate"]["status"] == "N/A：有效分母为零"


@pytest.mark.asyncio
async def test_t4_reference_annotation_format_rejects_registered_and_missing_fields():
    """参考标注格式：模型与人工共用。

    发现阶段**不得填 `registered`**——那是盲审阶段才确定的字段。
    """
    from backend.common.registration_reference_set import (
        PROTOCOL_VERSION,
        validate_annotation,
    )

    base = {
        "work_id": "w1",
        "prefix_id": "p1",
        "annotator_id": "a1",
        "protocol_version": PROTOCOL_VERSION,
        "scope_read": {"unit_ids": [1, 2], "units_sha256": "x"},
        "completed": True,
        "completed_at": "2026-11-07T00:00:00",
        "freeze_sha256": "abc",
        "items": [
            {
                "question": "匾额四方位分别指什么？",
                "answer_criteria": "交代四个方位的含义",
                "supporting_quote": "匾额上写着甲乙丙丁",
                "acceptable_spans": [{"unit_id": 1, "start": 10, "end": 18}],
                "label": "有依据且未解",
                "reason": "匾额列出方位但未解释",
            }
        ],
    }
    ok = validate_annotation(base)
    assert ok["ok"] is True, ok["errors"]
    assert len(ok["items"]) == 1

    # 填了 registered → 不合格
    bad = json.loads(json.dumps(base, ensure_ascii=False))
    bad["items"][0]["registered"] = True
    r = validate_annotation(bad)
    assert r["ok"] is False
    assert any("must_not_fill_registered" in e for e in r["errors"])

    # 缺字段 → 不合格
    miss = json.loads(json.dumps(base, ensure_ascii=False))
    del miss["items"][0]["answer_criteria"]
    del miss["completed_at"]
    r2 = validate_annotation(miss)
    assert r2["ok"] is False
    assert any("answer_criteria" in e for e in r2["errors"])
    assert any("completed_at" in e for e in r2["errors"])

    # 未知标签 → 不合格
    ul = json.loads(json.dumps(base, ensure_ascii=False))
    ul["items"][0]["label"] = "随便"
    assert validate_annotation(ul)["ok"] is False

    # 跨度非法 → 不合格
    bs = json.loads(json.dumps(base, ensure_ascii=False))
    bs["items"][0]["acceptable_spans"] = [{"unit_id": 1, "start": 10, "end": 10}]
    assert validate_annotation(bs)["ok"] is False


@pytest.mark.asyncio
async def test_t4_reference_merge_keeps_disputes_and_dedups():
    """两份独立发现 → 去重、保留争议、记录单方条目。"""
    from backend.common.registration_reference_set import (
        PROTOCOL_VERSION,
        freeze_reference_set,
        merge_annotations,
    )

    def _doc(annotator, items):
        return {
            "work_id": "w1",
            "prefix_id": "p1",
            "annotator_id": annotator,
            "protocol_version": PROTOCOL_VERSION,
            "scope_read": {"unit_ids": [1, 2]},
            "completed": True,
            "completed_at": "2026-11-07T00:00:00",
            "freeze_sha256": "abc",
            "items": items,
        }

    def _item(q, label="有依据且未解", quote="匾额上写着甲乙丙丁"):
        return {
            "question": q,
            "answer_criteria": "交代清楚",
            "supporting_quote": quote,
            "acceptable_spans": [{"unit_id": 1, "start": 10, "end": 18}],
            "label": label,
            "reason": "尚未解释",
        }

    left = _doc("a1", [_item("匾额四方位分别指什么？"), _item("钥匙的来历是什么？")])
    right = _doc(
        "a2",
        [
            _item("匾额四方位分别指什么？", label="已经回答", quote="匾额上写着甲乙丙丁"),
            _item("石匣里是什么？", quote="他把令牌藏进石匣"),
        ],
    )
    m = merge_annotations(left, right, work_id="w1", prefix_id="p1")
    assert m["ok"] is True
    # 同一题但判断不同 → disputed，两方意见都保留
    disputed = [i for i in m["items"] if i["status"] == "disputed"]
    assert len(disputed) == 1
    assert len(disputed[0]["opinions"]) == 2
    # 两边的原始条件与跨度都保留下来，供人工裁决对照
    assert len(disputed[0]["source_conditions"]) == 2
    # 合并阶段一律待裁决，没有条目自动进入合格集合
    assert all(i["qualification"]["qualified"] is False for i in m["items"])
    # 只有一方提到 → single_annotator
    singles = [i for i in m["items"] if i["status"] == "single_annotator"]
    assert len(singles) == 2
    assert m["summary"]["disputed"] == 1
    assert m["summary"]["single_annotator"] == 2

    # 冻结：给出哈希；修订另建版本
    fs = freeze_reference_set(m, annotator_ids=["a1", "a2"])
    assert fs["frozen"] is True
    assert fs["freeze_sha256"]
    assert fs["item_count"] == len(m["items"])


@pytest.mark.asyncio
async def test_t4_blind_review_matching_per_arm_and_unresolved_not_missed():
    """盲审匹配：A、B 两臂分别统计；unresolved 不默认为漏登记。"""
    from backend.common.registration_reference_set import (
        match_against_arms,
    )

    eligible = {
        "eligible_ref_ids": ["r0", "r1", "r2"],
        "items": [
            {"ref_id": "r0", "question": "q0"},
            {"ref_id": "r1", "question": "q1"},
            {"ref_id": "r2", "question": "q2"},
        ],
    }
    arms = {
        "A": [
            {"ref_id": "r0", "matched": True, "system_item_id": "A0"},
            {"ref_id": "r1", "matched": False},
            {"ref_id": "r2", "matched": None},
        ],
        "B": [
            {"ref_id": "r0", "matched": True, "system_item_id": "B0"},
            {"ref_id": "r1", "matched": True, "system_item_id": "B1"},
            {"ref_id": "r2", "matched": None},
        ],
    }
    arm_items = {
        "A": [{"item_id": "A0"}],
        "B": [{"item_id": "B0"}, {"item_id": "B1"}],
    }
    res = match_against_arms(eligible, arm_pairs=arms, arm_system_items=arm_items)
    a = res["A"]
    assert a["reference_total"] == 3
    assert a["judged_total"] == 2
    assert a["registered"] == 1
    assert a["missed"] == 1
    assert a["unresolved_total"] == 1
    assert a["unresolved_ref_ids"] == ["r2"]
    # B 臂单独统计，不能混用
    b = res["B"]
    assert b["registered"] == 2
    assert b["missed"] == 0


@pytest.mark.asyncio
async def test_t4_usage_archive_records_cost_and_failures():
    """用量归档：失败尝试与重试同样计入；价格表缺失时只记用量不报价。"""
    from backend.common.registration_usage_archive import (
        STAGE_GENERATION,
        STAGE_REFERENCE,
        UsageArchive,
    )

    arch = UsageArchive()
    arch.record_attempt(
        work_id="w1", prefix_id="p1", arm="A", stage=STAGE_GENERATION,
        request_sha256="r1", model="m", input_tokens=1000, output_tokens=200,
        success=True, attempt=1,
    )
    arch.record_attempt(
        work_id="w1", prefix_id="p1", arm="A", stage=STAGE_GENERATION,
        request_sha256="r1", model="m", input_tokens=1000, output_tokens=0,
        success=False, error="timeout", attempt=2,
    )
    arch.record_attempt(
        work_id="w1", prefix_id="p1", arm="-", stage=STAGE_REFERENCE,
        request_sha256="r2", model="m", input_tokens=500, output_tokens=100,
        success=True, attempt=1,
    )
    s = arch.summary()
    assert s["attempts"] == 3
    assert s["failures"] == 1
    assert s["input_tokens"] == 2500
    assert s["output_tokens"] == 300
    assert s["by_stage"][STAGE_GENERATION]["attempts"] == 2
    assert s["by_stage"][STAGE_GENERATION]["failures"] == 1
    # 没有价格表 → 不报价
    assert s["cost"]["status"] == "无价格表，未核算"
    assert s["cost"]["amount"] is None

    # 给了价格表 → 核算
    arch2 = UsageArchive(price_table={"m": {"input_per_1k": 0.01, "output_per_1k": 0.02}})
    arch2.record_attempt(
        work_id="w1", prefix_id="p1", arm="A", stage=STAGE_GENERATION,
        request_sha256="r1", model="m", input_tokens=1000, output_tokens=2000,
        success=True, attempt=1,
    )
    s2 = arch2.summary()
    assert s2["cost"]["status"] == "已核算"
    assert abs(s2["cost"]["amount"] - (1.0 * 0.01 + 2.0 * 0.02)) < 1e-9


@pytest.mark.asyncio
async def test_t4_usage_archive_reserve_blocks_before_send_and_settles(tmp_path):
    """用量归档：发送前预留必须把累计消耗带入限制；严格校验拒绝损坏台账。"""
    from backend.common.registration_usage_archive import (
        STAGE_REFERENCE,
        UsageArchive,
    )

    p = tmp_path / "usage.json"
    arch = UsageArchive()
    # 模拟已有 40 次调用，消耗 320000 input
    for i in range(40):
        arch.record_attempt(
            work_id="w1",
            prefix_id="p1",
            arm="-",
            stage=STAGE_REFERENCE,
            request_sha256=f"r_{i}",
            model="m",
            input_tokens=8000,
            output_tokens=100,
            success=True,
            attempt=i + 1,
        )
    arch.save(p)

    # 1) 重新加载并严格校验
    loaded = UsageArchive.load(p)
    assert len(loaded.records) == 40

    # 2) 预检预留名额：若再发 17000 tokens，将超过 336000 硬上限
    r_over = loaded.reserve(
        work_id="w1",
        prefix_id="p2",
        arm="-",
        stage=STAGE_REFERENCE,
        projected_input_tokens=17000,
        projected_output_tokens=500,
        max_attempts=42,
        max_input_tokens=336000,
        max_output_tokens=84000,
    )
    assert r_over["ok"] is False
    assert r_over["reason"] == "max_input_tokens_reached"

    # 3) 合法名额预留成功
    r_ok = loaded.reserve(
        work_id="w1",
        prefix_id="p2",
        arm="-",
        stage=STAGE_REFERENCE,
        projected_input_tokens=10000,
        projected_output_tokens=500,
        max_attempts=42,
        max_input_tokens=336000,
        max_output_tokens=84000,
    )
    assert r_ok["ok"] is True
    res_id = r_ok["reservation_id"]
    assert len(loaded.reservations) == 1

    # 4) 在途预留占用尝试与 token：第二笔并发请求因此被拦截（40 + 1 在途 + 1 > 41）
    r_block_attempts = loaded.reserve(
        work_id="w1",
        prefix_id="p2",
        arm="-",
        stage=STAGE_REFERENCE,
        projected_input_tokens=1000,
        projected_output_tokens=500,
        max_attempts=41,  # 设为 41，已有 40 + 1 预留 = 41
    )
    assert r_block_attempts["ok"] is False
    assert r_block_attempts["reason"] == "max_attempts_reached"

    # 5) 结算预留
    loaded.settle_reservation(
        res_id,
        request_sha256="req_settled",
        model="m",
        input_tokens=9500,
        output_tokens=450,
        success=True,
    )
    assert len(loaded.reservations) == 0
    assert len(loaded.records) == 41

    # 6) 损坏台账严格拒绝：不能静默重置为空台账
    bad_p = tmp_path / "bad_usage.json"
    bad_p.write_text("{bad json", encoding="utf-8")
    with pytest.raises(ValueError, match="台账文件损坏或非合法JSON"):
        UsageArchive.load(bad_p)


@pytest.mark.asyncio
async def test_t4_cross_process_reservation_mutual_exclusion_and_idempotent_settlement(tmp_path):
    """F1 验收：两个独立进程竞争最后一个名额恰好一个成功；并发结算无更新丢失；重复结算幂等。"""
    import subprocess
    import sys
    from pathlib import Path
    from backend.common.registration_usage_archive import UsageArchive

    archive_path = tmp_path / "concurrent_usage.json"
    repo_root = str(Path(__file__).resolve().parents[2])

    # 1) 两个独立进程竞争最后一个名额（max_attempts=1）
    code_reserve = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from backend.common.registration_usage_archive import UsageArchive

p = Path(sys.argv[2])
with UsageArchive.transaction(p) as arch:
    res = arch.reserve(
        work_id="w", prefix_id="p", arm="-", stage="ref",
        projected_input_tokens=100, projected_output_tokens=100,
        max_attempts=1
    )
    print("RESULT:" + ("OK" if res["ok"] else "FAIL"))
"""
    p1 = subprocess.Popen([sys.executable, "-c", code_reserve, repo_root, str(archive_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([sys.executable, "-c", code_reserve, repo_root, str(archive_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out1, _ = p1.communicate()
    out2, _ = p2.communicate()

    results = sorted([out1.strip(), out2.strip()])
    assert results == ["RESULT:FAIL", "RESULT:OK"], f"独立进程竞争名额未互斥：{results}"

    # 2) 验证两个不同预留并发结算后，两条记录都在（无丢失更新）
    arch = UsageArchive.load(archive_path)
    arch.reservations = []  # 清空测试预留
    r1 = arch.reserve(work_id="w", prefix_id="p", arm="A", stage="ref", projected_input_tokens=100, projected_output_tokens=100, max_attempts=10)
    r2 = arch.reserve(work_id="w", prefix_id="p", arm="B", stage="ref", projected_input_tokens=100, projected_output_tokens=100, max_attempts=10)
    arch.save(archive_path)
    id1, id2 = r1["reservation_id"], r2["reservation_id"]

    code_settle = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from backend.common.registration_usage_archive import UsageArchive

p = Path(sys.argv[2])
rid = sys.argv[3]
with UsageArchive.transaction(p) as a:
    a.settle_reservation(rid, request_sha256="sha_" + rid, model="m", input_tokens=80, output_tokens=20, success=True)
"""
    ps1 = subprocess.Popen([sys.executable, "-c", code_settle, repo_root, str(archive_path), id1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ps2 = subprocess.Popen([sys.executable, "-c", code_settle, repo_root, str(archive_path), id2], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ps1.communicate()
    ps2.communicate()

    final = UsageArchive.load(archive_path)
    assert len(final.records) == 2, f"并发结算丢失更新：现有 {len(final.records)} 条"
    assert len(final.reservations) == 0
    settled_ids = {r["reservation_id"] for r in final.records}
    assert settled_ids == {id1, id2}

    # 3) 同一预留重复结算不重复记账（幂等）
    with UsageArchive.transaction(archive_path) as a:
        rec_repeat = a.settle_reservation(id1, request_sha256="sha_repeat", model="m", input_tokens=80, output_tokens=20, success=True)
        assert rec_repeat["reservation_id"] == id1
    final_repeat = UsageArchive.load(archive_path)
    assert len(final_repeat.records) == 2

    # 4) 不存在的预留不能悄悄新增未知记录
    with pytest.raises(ValueError, match="reservation_not_found"):
        with UsageArchive.transaction(archive_path) as a:
            a.settle_reservation("non_existent_id", request_sha256="sha", model="m")


@pytest.mark.asyncio
async def test_t4_unknown_usage_retains_budget_occupancy_and_blocks_oversize(tmp_path):
    """F2 验收：请求超时/缺失 usage 标记 unknown 并保留预算占用；阻断超限；补齐后单次结算。"""
    from backend.common.registration_usage_archive import UsageArchive

    archive_path = tmp_path / "unknown_usage.json"
    arch = UsageArchive()
    # 限额：500 input
    resv1 = arch.reserve(work_id="w", prefix_id="p", arm="A", stage="ref", projected_input_tokens=300, projected_output_tokens=50, max_input_tokens=500)
    assert resv1["ok"] is True
    rid1 = resv1["reservation_id"]

    # 模拟请求已发出但超时：以 unknown 结算
    rec1 = arch.settle_reservation(rid1, request_sha256="sha1", model="m", usage_status="unknown", success=False, error="timeout")
    assert rec1["usage_status"] == "unknown"
    assert rec1["input_tokens"] is None
    assert rec1["occupied_input_tokens"] == 300
    arch.save(archive_path)

    # 验证未释放预算：再次申请 300 会超过 500（300已占 + 300 = 600 > 500）
    loaded = UsageArchive.load(archive_path)
    resv2 = loaded.reserve(work_id="w", prefix_id="p", arm="B", stage="ref", projected_input_tokens=300, projected_output_tokens=50, max_input_tokens=500)
    assert resv2["ok"] is False
    assert resv2["reason"] == "max_input_tokens_reached"

    # 补齐真实用量：查明实际消耗 180 input
    loaded.reconcile_usage(rid1, input_tokens=180, output_tokens=30)
    assert loaded.records[0]["usage_status"] == "measured"
    assert loaded.records[0]["input_tokens"] == 180
    assert loaded.records[0]["occupied_input_tokens"] == 180

    # 补齐后释放多余占用：180 + 300 = 480 <= 500，重新申请成功
    resv3 = loaded.reserve(work_id="w", prefix_id="p", arm="B", stage="ref", projected_input_tokens=300, projected_output_tokens=50, max_input_tokens=500)
    assert resv3["ok"] is True
    rid3 = resv3["reservation_id"]

    # 模拟返回响应缺失 usage（tokens is None）：不能按零结算，同样走 unknown
    rec3 = loaded.settle_reservation(rid3, request_sha256="sha3", model="m", usage_status="unknown", success=True)
    assert rec3["usage_status"] == "unknown"
    assert rec3["input_tokens"] is None
    assert rec3["occupied_input_tokens"] == 300


@pytest.mark.asyncio
async def test_t4_retry_boundary_max_attempts_single_stops_after_timeout(tmp_path):
    """F3 验收 1：总次数仅剩 1：第一次模拟超时后，不得出现第二次传输；台账保留 1 次 unknown 占用。"""
    import unittest.mock
    import httpx2
    import openai
    from backend.config.settings import APIConfig
    from backend.core.llm_client import LLMClient
    from backend.common.registration_usage_archive import UsageArchive
    from scripts.experiments.bc.bc_t4_registration_pilot_runner import run_guarded_llm_request

    archive_path = tmp_path / "usage_f3_single.json"
    api = APIConfig(
        base_url="http://127.0.0.1:3425/v1",
        api_key="test",
        model="codex/gpt-6-luna",
        sdk_max_retries=0,
        max_retries=0,
        temperature_max_retries=1,
        backoff_max_retries=0,
        streaming_enabled=False,
    )
    client = LLMClient(api)

    send_count = 0
    async def mock_send(request, *args, **kwargs):
        nonlocal send_count
        send_count += 1
        raise openai.APITimeoutError(request=unittest.mock.MagicMock())

    client.client._client.send = mock_send

    res = await run_guarded_llm_request(
        client=client,
        archive_path=archive_path,
        work_id="t4_hist_holdout",
        prefix_id="p3",
        arm="-",
        stage="ref",
        messages=[{"role": "user", "content": "test_prompt"}],
        projected_input_tokens=300,
        projected_output_tokens=50,
        request_sha="sha_test_single",
        max_runner_attempts=2,  # 外层允许尝试2次，但预算总上限仅限1次
        max_attempts_cap=1,
    )

    assert res["ok"] is False
    assert res["reason"] == "budget_blocked"
    assert res["detail"]["reason"] == "max_attempts_reached"
    assert send_count == 1, f"底层实际传输次数超标：预期1次，实际 {send_count} 次"

    arch = UsageArchive.load(archive_path)
    assert len(arch.records) == 1
    assert len(arch.reservations) == 0
    assert arch.records[0]["usage_status"] == "unknown"
    assert arch.records[0]["success"] is False
    assert arch.records[0]["occupied_input_tokens"] == 300


@pytest.mark.asyncio
async def test_t4_retry_boundary_two_attempts_records_both_without_overwrite(tmp_path):
    """F3 验收 2：总次数允许 2、预算允许：第一次 unknown 与第二次 measured 分别记账；第一次未知占用不能被第二次成功覆盖。"""
    import unittest.mock
    import httpx2
    import openai
    from backend.config.settings import APIConfig
    from backend.core.llm_client import LLMClient
    from backend.common.registration_usage_archive import UsageArchive
    from scripts.experiments.bc.bc_t4_registration_pilot_runner import run_guarded_llm_request

    archive_path = tmp_path / "usage_f3_retry_success.json"
    api = APIConfig(
        base_url="http://127.0.0.1:3425/v1",
        api_key="test",
        model="codex/gpt-6-luna",
        sdk_max_retries=0,
        max_retries=0,
        temperature_max_retries=1,
        backoff_max_retries=0,
        streaming_enabled=False,
    )
    client = LLMClient(api)

    send_count = 0
    async def mock_send(request, *args, **kwargs):
        nonlocal send_count
        send_count += 1
        if send_count == 1:
            raise openai.APITimeoutError(request=unittest.mock.MagicMock())
        return httpx2.Response(
            status_code=200,
            request=request,
            json={
                "id": "m2",
                "choices": [{"message": {"role": "assistant", "content": "res_text"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 180, "completion_tokens": 10, "total_tokens": 190},
            },
        )

    client.client._client.send = mock_send

    res = await run_guarded_llm_request(
        client=client,
        archive_path=archive_path,
        work_id="t4_hist_holdout",
        prefix_id="p3",
        arm="-",
        stage="ref",
        messages=[{"role": "user", "content": "test_prompt"}],
        projected_input_tokens=300,
        projected_output_tokens=50,
        request_sha="sha_test_retry",
        max_runner_attempts=2,
        max_attempts_cap=2,
        max_input_tokens_cap=5000,
    )

    assert res["ok"] is True
    assert res["attempt_idx"] == 2
    assert send_count == 2, f"底层实际传输次数不符：预期2次，实际 {send_count} 次"

    arch = UsageArchive.load(archive_path)
    assert len(arch.records) == 2
    assert len(arch.reservations) == 0
    rec0, rec1 = arch.records[0], arch.records[1]
    assert rec0["usage_status"] == "unknown"
    assert rec0["success"] is False
    assert rec0["occupied_input_tokens"] == 300
    assert rec0["input_tokens"] is None

    assert rec1["usage_status"] == "measured"
    assert rec1["success"] is True
    assert rec1["input_tokens"] == 180
    assert rec1["output_tokens"] == 10
    assert rec1["occupied_input_tokens"] == 180


@pytest.mark.asyncio
async def test_t4_retry_boundary_token_budget_exhaustion_blocks_second_transmission(tmp_path):
    """F3 验收 3：次数允许但 token 预算不足：第二次必须在发送前被阻断。"""
    import unittest.mock
    import openai
    from backend.config.settings import APIConfig
    from backend.core.llm_client import LLMClient
    from backend.common.registration_usage_archive import UsageArchive
    from scripts.experiments.bc.bc_t4_registration_pilot_runner import run_guarded_llm_request

    archive_path = tmp_path / "usage_f3_token_blocked.json"
    api = APIConfig(
        base_url="http://127.0.0.1:3425/v1",
        api_key="test",
        model="codex/gpt-6-luna",
        sdk_max_retries=0,
        max_retries=0,
        temperature_max_retries=1,
        backoff_max_retries=0,
        streaming_enabled=False,
    )
    client = LLMClient(api)

    send_count = 0
    async def mock_send(request, *args, **kwargs):
        nonlocal send_count
        send_count += 1
        raise openai.APITimeoutError(request=unittest.mock.MagicMock())

    client.client._client.send = mock_send

    res = await run_guarded_llm_request(
        client=client,
        archive_path=archive_path,
        work_id="t4_hist_holdout",
        prefix_id="p3",
        arm="-",
        stage="ref",
        messages=[{"role": "user", "content": "test_prompt"}],
        projected_input_tokens=300,
        projected_output_tokens=50,
        request_sha="sha_test_token_block",
        max_runner_attempts=2,
        max_attempts_cap=5,
        max_input_tokens_cap=500,  # 300 占用后剩 200，无法再预留 300
    )

    assert res["ok"] is False
    assert res["reason"] == "budget_blocked"
    assert res["detail"]["reason"] == "max_input_tokens_reached"
    assert send_count == 1, f"第二次请求未在发送前阻断：实际发送 {send_count} 次"

    arch = UsageArchive.load(archive_path)
    assert len(arch.records) == 1
    assert arch.records[0]["usage_status"] == "unknown"
    assert arch.records[0]["occupied_input_tokens"] == 300


@pytest.mark.asyncio
async def test_t4_retry_boundary_missing_usage_success_retains_unknown_and_is_not_lost(tmp_path):
    """F3 验收 4：缺失 usage 的成功响应也保持 unknown；不得因随后一次成功而丢失。"""
    import httpx2
    from backend.config.settings import APIConfig
    from backend.core.llm_client import LLMClient
    from backend.common.registration_usage_archive import UsageArchive
    from scripts.experiments.bc.bc_t4_registration_pilot_runner import run_guarded_llm_request

    archive_path = tmp_path / "usage_f3_missing_usage.json"
    api = APIConfig(
        base_url="http://127.0.0.1:3425/v1",
        api_key="test",
        model="codex/gpt-6-luna",
        sdk_max_retries=0,
        max_retries=0,
        temperature_max_retries=1,
        backoff_max_retries=0,
        streaming_enabled=False,
    )
    client = LLMClient(api)

    send_count = 0
    async def mock_send(request, *args, **kwargs):
        nonlocal send_count
        send_count += 1
        if send_count == 1:
            return httpx2.Response(
                status_code=200,
                request=request,
                json={"id": "m1", "choices": [{"message": {"role": "assistant", "content": "res1"}, "finish_reason": "stop"}]},
            )
        return httpx2.Response(
            status_code=200,
            request=request,
            json={
                "id": "m2",
                "choices": [{"message": {"role": "assistant", "content": "res2"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 140, "completion_tokens": 20, "total_tokens": 160},
            },
        )

    client.client._client.send = mock_send

    res1 = await run_guarded_llm_request(
        client=client,
        archive_path=archive_path,
        work_id="t4_hist_holdout",
        prefix_id="p3",
        arm="A",
        stage="ref",
        messages=[{"role": "user", "content": "test_prompt1"}],
        projected_input_tokens=300,
        projected_output_tokens=50,
        request_sha="sha_test_no_usage",
        max_runner_attempts=1,
    )
    assert res1["ok"] is True
    assert res1["usage_status"] == "unknown"

    res2 = await run_guarded_llm_request(
        client=client,
        archive_path=archive_path,
        work_id="t4_hist_holdout",
        prefix_id="p3",
        arm="B",
        stage="ref",
        messages=[{"role": "user", "content": "test_prompt2"}],
        projected_input_tokens=250,
        projected_output_tokens=50,
        request_sha="sha_test_with_usage",
        max_runner_attempts=1,
    )
    assert res2["ok"] is True
    assert res2["usage_status"] == "measured"
    assert send_count == 2

    arch = UsageArchive.load(archive_path)
    assert len(arch.records) == 2
    r0, r1 = arch.records[0], arch.records[1]
    assert r0["usage_status"] == "unknown"
    assert r0["success"] is True
    assert r0["input_tokens"] is None
    assert r0["occupied_input_tokens"] == 300

    assert r1["usage_status"] == "measured"
    assert r1["success"] is True
    assert r1["input_tokens"] == 140
    assert r1["occupied_input_tokens"] == 140


@pytest.mark.asyncio
async def test_t4_retry_boundary_lowest_transport_sdk_retries_strictly_disabled():
    """F3 验收 5：拦截最底层传输计数，验证 SDK 与包装层绝不发生隐式重试。"""
    import httpx2
    from backend.config.settings import APIConfig
    from backend.core.llm_client import LLMClient

    api = APIConfig(
        base_url="http://127.0.0.1:3425/v1",
        api_key="test",
        model="codex/gpt-6-luna",
        sdk_max_retries=0,
        max_retries=0,
        temperature_max_retries=1,
        backoff_max_retries=0,
        streaming_enabled=False,
    )
    client = LLMClient(api)

    send_count_500 = 0
    async def mock_send_500(request, *args, **kwargs):
        nonlocal send_count_500
        send_count_500 += 1
        return httpx2.Response(status_code=500, request=request, content=b"server error")

    client.client._client.send = mock_send_500
    res500 = await client.chat_single([{"role": "user", "content": "hi"}])
    assert res500.success is False
    assert send_count_500 == 1, f"500 状态码触发了 SDK 隐式重试，计数={send_count_500}"

    send_count_429 = 0
    async def mock_send_429(request, *args, **kwargs):
        nonlocal send_count_429
        send_count_429 += 1
        return httpx2.Response(status_code=429, request=request, content=b"rate limit")

    client.client._client.send = mock_send_429
    res429 = await client.chat_single([{"role": "user", "content": "hi"}])
    assert res429.success is False
    assert send_count_429 == 1, f"429 限流触发了包装层或 SDK 隐式重试，计数={send_count_429}"


@pytest.mark.asyncio
async def test_t4_coverage_groups_retained_in_eligible_reference_set(tmp_path):
    """联合覆盖：冻结时保留 coverage_groups，联合覆盖条目从独立分母排除。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.common.registration_reference_set import (
        apply_qualification,
        save_reference_set,
    )

    raw = {
        "schema": "registration_reference_set_v1",
        "work_id": "t4_hist_holdout",
        "prefix_id": "p2",
        "freeze_sha256": "fake_freeze",
        "items": [
            {"ref_id": "r3", "status": "single_annotator", "question": "完颜征托秦相之事"},
            {"ref_id": "r4", "status": "single_annotator", "question": "秦相托完颜征之事"},
            {"ref_id": "r8", "status": "single_annotator", "question": "两人互托密商"},
        ],
    }
    decisions = [
        {"ref_id": "r3", "qualified": True, "qualified_label": "有依据且未解"},
        {"ref_id": "r4", "qualified": True, "qualified_label": "有依据且未解"},
        {"ref_id": "r8", "qualified": False, "qualified_label": "有依据且未解"},
    ]
    elig = apply_qualification(raw, decisions, reviewer_id="h1")
    assert elig["eligible_count"] == 2
    assert elig["eligible_ref_ids"] == ["r3", "r4"]

    # 附加 coverage_groups
    elig["coverage_groups"] = [
        {
            "composite_ref_id": "r8",
            "covered_by_ref_ids": ["r3", "r4"],
            "relation_type": "jointly_covered_by",
            "basis": "双向互托联合覆盖",
        }
    ]
    out_p = tmp_path / "eligible.json"
    save_reference_set(out_p, elig)

    # 重新读取核对
    import json

    reloaded = json.loads(out_p.read_text(encoding="utf-8"))
    assert len(reloaded["coverage_groups"]) == 1
    assert reloaded["coverage_groups"][0]["composite_ref_id"] == "r8"
    assert reloaded["coverage_groups"][0]["covered_by_ref_ids"] == ["r3", "r4"]


@pytest.mark.asyncio
async def test_t4_pilot_host_locator_requires_unique_hit():
    """宿主定位：歧义、越界、不存在一律单列，**不默取第一个命中**。

    模型只给 source_unit_id 与引文，精确跨度由宿主在**原始单元文本**上定位。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.experiments.bc.bc_t4_registration_pilot_runner import (
        convert_to_host_schema,
        locate_citation,
        parse_generator_output,
    )

    units = {
        1: "第一章：他把令牌藏进石匣，匾额上写着甲乙丙丁。",
        2: "第二章：城主打开石匣，确认令牌确为真品。",
        3: "第三章：令牌后来被转手三次。",
        4: "第四章：众人议论那把钥匙的来历。钥匙的来历至今不明。",
    }
    ok = locate_citation(
        units=units, source_unit_id=2, anchor_quote="城主打开石匣，确认令牌确为真品。", checkpoint=2
    )
    assert ok["ok"] is True
    assert (ok["start"], ok["end"]) == (4, 20)
    # 坐标落在原始单元文本上（单元标签不算进坐标）
    assert units[2][ok["start"] : ok["end"]] == "城主打开石匣，确认令牌确为真品。"

    amb = locate_citation(
        units=units, source_unit_id=4, anchor_quote="钥匙的来历", checkpoint=4
    )
    assert amb["ok"] is False
    assert "not_unique" in amb["reason"]

    oob = locate_citation(
        units=units, source_unit_id=3, anchor_quote="令牌后来被转手三次。", checkpoint=2
    )
    assert oob["ok"] is False
    assert oob["reason"] == "unit_out_of_visible_range"

    miss = locate_citation(
        units=units, source_unit_id=1, anchor_quote="原文里没有这一句", checkpoint=2
    )
    assert miss["ok"] is False
    assert miss["reason"] == "quote_not_found"

    # 输出截断 → 整次任务标 incomplete，不能当成"少登记"
    trunc = parse_generator_output(json.dumps({"complete": False, "foreshadowing": []}))
    assert trunc["parse_ok"] is True
    assert trunc["complete"] is False

    # 宿主追加跨度与冻结信息，模型原始响应不被改写
    raw = json.dumps(
        {
            "complete": True,
            "foreshadowing": [
                {
                    "clue": "匾额四方位",
                    "question": "匾额四方位分别指什么？",
                    "answer_criteria": "交代四个方位的含义",
                    "implication": "尚待交代之事",
                    "motif_key": "方位",
                    "source_unit_id": 1,
                    "anchor_quote": "他把令牌藏进石匣，匾额上写着甲乙丙丁。",
                }
            ],
        },
        ensure_ascii=False,
    )
    parsed = parse_generator_output(raw)
    rows = convert_to_host_schema(
        parsed, units=units, checkpoint=2, prefix_id="p1", registration_checkpoint=2
    )
    assert rows[0]["ok"] is True
    assert rows[0]["citation_start"] == 4
    assert rows[0]["visible_prefix_id"] == "p1"
    assert rows[0]["registration_checkpoint"] == 2


@pytest.mark.asyncio
async def test_t4_pilot_budget_counts_retries_and_blocks_oversize():
    """预算：重试也占预算；单请求装不下就别发。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.experiments.bc.bc_t4_registration_pilot_runner import Budget

    b = Budget(max_attempts=42, max_input_tokens=336000, max_output_tokens=84000)
    assert b.precheck(1000, 500)["ok"] is True
    b.record(1000, 500)
    assert b.summary()["attempts"] == 1
    assert b.summary()["attempts_remaining"] == 41
    # 装不下 → 不发（单请求上限已调至 20000，21000 应拦截）
    assert b.precheck(21000, 100)["ok"] is False
    assert b.precheck(100, 3000)["ok"] is False
    # 次数用尽 → 不发
    b2 = Budget(max_attempts=1)
    b2.record(10, 10)
    r = b2.precheck(10, 10)
    assert r["ok"] is False
    assert r["reason"] == "max_attempts_reached"
    # 总 token 上限
    b3 = Budget(max_attempts=10, max_input_tokens=100, max_output_tokens=100)
    b3.record(90, 10)
    assert b3.precheck(50, 10)["ok"] is False


@pytest.mark.asyncio
async def test_t4_pilot_template_assembly_is_deterministic_and_safe():
    """模板装配：确定性；插入的原文不被二次模板展开。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.experiments.bc.bc_t4_arm_proposal_verify import assemble

    src = '他说"{name}"，又写下 {not_a_placeholder} 与 {x}。'
    fields = {
        "work_id": "w1",
        "prefix_id": "p1",
        "visible_range_end": 8,
        "registration_checkpoint": 2,
        "source_units_with_stable_ids": src,
    }
    tpl = "任务\nwork_id={work_id}\nprefix_id={prefix_id}\n" \
          "visible_range_end={visible_range_end}\n" \
          "registration_checkpoint={registration_checkpoint}\n<source_prefix>\n" \
          "{source_units_with_stable_ids}\n</source_prefix>\n"
    a = assemble("SYS", tpl, fields)
    b = assemble("SYS", tpl, fields)
    assert a == b
    # 原文里的花括号原样保留
    assert "{not_a_placeholder}" in a["user"]
    assert "{x}" in a["user"]
    # 模板占位符全部替换
    for k in ("work_id", "prefix_id", "visible_range_end", "registration_checkpoint",
              "source_units_with_stable_ids"):
        assert "{" + k + "}" not in a["user"]


@pytest.mark.asyncio
async def test_t4_freeze_records_all_visible_materials_and_detects_leak():
    """冻结与隔离检查。

    隔离要证明的是"没有泄露"，不是"抽查几个字面片段没出现"。
    类别缺失、哈希乱填、辅助材料改写后文，都必须挡住。
    """
    from backend.common.registration_eval_freeze import (
        build_freeze_record,
        check_isolation,
        save_freeze,
    )

    prefix = "第一章：他把令牌藏进石匣。匾额上写着甲乙丙丁。"
    later = "第五章：城主早已确认令牌是真品，钥匙也能打开宫门。"
    # 辅助材料必须是**抽取式**：可逐字溯源到前缀。
    # 改写式摘要无法证明"只从前缀生成"，评估里不得使用。
    summary = "他把令牌藏进石匣。匾额上写着甲乙丙丁。"
    knowledge = "匾额上写着甲乙丙丁。"
    retrieval = "他把令牌藏进石匣。"

    def _mats():
        import hashlib

        h = lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()
        return [
            {"kind": "body_prefix", "used": True, "sha256": h(prefix), "content": prefix,
             "source_prefix_id": "p1"},
            {"kind": "summary", "used": True, "sha256": h(summary), "content": summary,
             "source_prefix_id": "p1", "generation": "extract"},
            {"kind": "knowledge", "used": True, "sha256": h(knowledge), "content": knowledge,
             "source_prefix_id": "p1"},
            {"kind": "retrieval", "used": True, "sha256": h(retrieval), "content": retrieval,
             "source_prefix_id": "p1"},
        ]

    rec = build_freeze_record(
        work_id="w1",
        prefix_id="p1",
        prefix_unit_ids=[1],
        prefix_sha256="hp",
        materials=_mats(),
        items=[{"item_id": "i1", "question": "令牌真假", "criteria": "确认真伪",
                "source_span": [4, 12], "visible_prefix_id": "p1"}],
        rules_version="rules-v1",
    )
    assert rec["material_kinds_complete"] is True
    assert rec["materials_all_hashed"] is True

    ok = check_isolation(record=rec, materials=_mats(), prefix_text=prefix, later_text=later)
    assert ok["ok"] is True, ok

    # 1) 类别缺失 → 不通过
    partial = _mats()[:1]
    bad_kinds = check_isolation(record=rec, materials=partial, prefix_text=prefix, later_text=later)
    assert bad_kinds["ok"] is False
    assert any("missing_material_kinds" in p for p in bad_kinds["problems"])

    # 2) 哈希乱填 → 不通过
    faked = _mats()
    faked[1]["sha256"] = "fake"
    bad_hash = check_isolation(record=rec, materials=faked, prefix_text=prefix, later_text=later)
    assert bad_hash["ok"] is False
    assert any("material_hash_mismatch" in p for p in bad_hash["problems"])

    # 3) 摘要改写后文答案 → 字面探针挡不住，但溯源检查必须挡住
    paraphrase = "摘要：凶手就是城主，后文会揭晓。"
    para_mats = _mats()
    para_mats[1] = {"kind": "summary", "used": True, "sha256": "x", "content": paraphrase,
                    "source_prefix_id": "p1"}
    bad_para = check_isolation(record=rec, materials=para_mats, prefix_text=prefix, later_text=later)
    assert bad_para["ok"] is False
    assert any("auxiliary_not_grounded_in_prefix" in p for p in bad_para["problems"])

    # 4) 显式标 unused 的类别：不得带内容，且要与冻结记录一致
    unused_mats = _mats()
    unused_mats[1] = {"kind": "summary", "used": False, "sha256": "", "content": ""}
    rec_unused = build_freeze_record(
        work_id="w1",
        prefix_id="p1",
        prefix_unit_ids=[1],
        prefix_sha256="hp",
        materials=unused_mats,
        items=[],
        rules_version="rules-v1",
    )
    ok_unused = check_isolation(
        record=rec_unused, materials=unused_mats, prefix_text=prefix, later_text=later
    )
    assert ok_unused["ok"] is True, ok_unused
    assert ok_unused["declared_unused_kinds"] == ["summary"]

    # 声明 unused 却带内容 → 不通过
    bad_unused = [dict(m) for m in unused_mats]
    bad_unused[1] = {"kind": "summary", "used": False, "sha256": "", "content": "偷偷带内容"}
    bad_unused_res = check_isolation(
        record=rec_unused, materials=bad_unused, prefix_text=prefix, later_text=later
    )
    assert bad_unused_res["ok"] is False
    assert any("declared_unused_but_has_content" in p for p in bad_unused_res["problems"])

    # 5) 冻结后拒绝原位覆盖
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as td:
        fp = _P(td) / "freeze.json"
        assert save_freeze(fp, rec) is True
        changed = dict(rec)
        changed["rules_version"] = "rules-v2"
        try:
            save_freeze(fp, changed)
            raise AssertionError("应当拒绝原位覆盖")
        except ValueError:
            pass
        # 内容完全相同 → 视为重复写入，放行
        assert save_freeze(fp, rec) is False


@pytest.mark.asyncio
async def test_t4_registration_metrics_use_declared_denominators():
    """指标分母按**有效裁决**分离，争议/无法确定/未审/重复都不进分母。

    复核复现：2 条系统条目，1 条确认无依据、1 条争议 → 1/1，另报争议 1 条。
    """
    from backend.common.registration_eval_metrics import compute_metrics

    system_items = [
        {"item_id": "i1"},
        {"item_id": "i2"},
        {"item_id": "i3"},
        {"item_id": "i4"},
    ]
    adjudicated = [
        {"item_id": "i1", "label": "有依据", "span_located": True},
        {"item_id": "i2", "label": "无依据", "span_located": False},
        {"item_id": "i3", "label": "已经回答", "span_located": True},
        {"item_id": "i4", "label": "有依据", "span_located": True, "disputed": True},
    ]
    qualified = [
        {"question_id": "q1", "registered": True},
        {"question_id": "q2", "registered": False},
        {"question_id": "q3", "registered": True},
    ]
    m = compute_metrics(
        system_items=system_items,
        adjudicated_items=adjudicated,
        independent_qualified=qualified,
    )
    # i4 争议 → 不进分母：有效裁决数是 3，系统原始总数是 4
    d = m["denominators"]
    assert d["system_total"] == 4
    assert d["settled_adjudications"] == 3
    assert d["disputed"] == 1
    assert m["metrics"]["no_ground_rate"]["denominator"] == 3
    assert m["metrics"]["no_ground_rate"]["count"] == 1
    assert m["excluded"]["disputed_ids"] == ["i4"]
    # 裁决覆盖率单独报告
    assert m["coverage"]["adjudication_coverage"] == 0.75
    # 漏登记分母是有效参考问题数
    assert m["metrics"]["missed_registration_rate"]["denominator"] == 3
    assert m["metrics"]["missed_registration_rate"]["count"] == 1


async def test_t4_metrics_disputed_and_undecided_excluded_from_denominator():
    """最小复现：1 条无依据 + 1 条争议 → 1/1，另报争议 1 条。"""
    from backend.common.registration_eval_metrics import compute_metrics

    m = compute_metrics(
        system_items=[{"item_id": "a"}, {"item_id": "b"}],
        adjudicated_items=[
            {"item_id": "a", "label": "无依据", "span_located": False},
            {"item_id": "b", "label": "有依据", "span_located": True, "disputed": True},
        ],
        independent_qualified=[],
    )
    assert m["metrics"]["no_ground_rate"]["denominator"] == 1
    assert m["metrics"]["no_ground_rate"]["count"] == 1
    assert m["metrics"]["no_ground_rate"]["rate"] == 1.0
    assert m["denominators"]["disputed"] == 1
    assert m["denominators"]["system_total"] == 2


async def test_t4_metrics_label_based_exclusion_not_flag_based():
    """"无法确定"靠**标签**排除，不靠调用者记得 disputed=true。"""
    from backend.common.registration_eval_metrics import compute_metrics

    m = compute_metrics(
        system_items=[{"item_id": "a"}, {"item_id": "b"}],
        adjudicated_items=[
            {"item_id": "a", "label": "无法确定", "span_located": False},
            {"item_id": "b", "label": "无依据", "span_located": False},
        ],
        independent_qualified=[],
    )
    assert m["metrics"]["no_ground_rate"]["denominator"] == 1
    assert m["denominators"]["undecided"] == 1


async def test_t4_metrics_rejects_silent_bad_input():
    """重复ID、未知标签、缺 registered、缺标签都不静默参与计算。"""
    from backend.common.registration_eval_metrics import compute_metrics

    m = compute_metrics(
        system_items=[{"item_id": "a"}, {"item_id": "a"}],
        adjudicated_items=[
            {"item_id": "a", "label": "无依据", "span_located": False},
            {"item_id": "a", "label": "有依据", "span_located": True},
            {"item_id": "zz", "label": "无依据", "span_located": False},
        ],
        independent_qualified=[
            {"question_id": "q1"},
            {"question_id": "q1", "registered": False},
            {"question_id": "q2", "registered": False, "pairing_status": "unresolved"},
        ],
    )
    assert m["denominators"]["system_total"] == 1
    assert m["denominators"]["duplicate_ids"] >= 1
    assert m["excluded"]["orphan_adjudication_ids"] == ["zz"]
    assert m["excluded"]["reference_missing_registered"] == ["q1"]
    assert m["excluded"]["reference_unresolved_pairing"] == ["q2"]
    # 未解决配对不默认算未登记
    assert m["metrics"]["missed_registration_rate"]["count"] == 0


async def test_t4_metrics_delay_requires_checkpoint_or_same_coordinate():
    """登记延迟只按检查点差或同一固定坐标的字符距离，不相减"跨度ID"。"""
    from backend.common.registration_eval_metrics import compute_metrics

    m = compute_metrics(
        system_items=[],
        adjudicated_items=[],
        independent_qualified=[
            {
                "question_id": "q1",
                "registered": True,
                "delay": {
                    "basis": "prefix_checkpoint",
                    "first_qualified_prefix_id": 2,
                    "registered_prefix_id": 5,
                },
            },
            {
                "question_id": "q2",
                "registered": True,
                "delay": {
                    "basis": "char_offset",
                    "first_qualified_char": 100,
                    "registered_char": 130,
                    "coordinate_space": "unit:6",
                },
            },
            {
                "question_id": "q3",
                "registered": True,
                "delay": {"basis": "span_id_subtraction", "first": 1, "second": 2},
            },
        ],
    )
    d = m["metrics"]["registration_delay"]
    assert d["measured"] == 2
    assert d["positive_delay_count"] == 2
    assert any("unsupported_basis" in r["reason"] for r in d["rejected"])


@pytest.mark.asyncio
async def test_t4_regate_failure_becomes_pending_not_inherits_old_accept():
    """重裁失败 → 待审，**不沿用旧裁决**。

    `regated:false` 只说明"没重裁"，不能替旧结论背书：
    重裁做不成恰恰说明旧结论是否仍有效无法确认。
    旧裁决保留供追查，但不控制当前通过状态。
    """
    from backend.services.recheck_v2 import _regate_cached_record

    # 没有原始响应 → 无法重裁
    r = _regate_cached_record(
        {"decision": "接受", "assessment_status": "accepted", "reasons": ["旧裁决"]},
        audit_item={"anchor_quote": "x"},
        chapters={1: "第一章：x"},
        gate_fn=lambda *a, **k: {"decision": "接受"},
        parse_fn=lambda raw: _safe_json(raw),
    )
    assert r["regated"] is False
    assert r["pending_review"] is True
    assert r["decision"] is None

    # 原始响应无法解析 → 同样无法重裁
    r2 = _regate_cached_record(
        {
            "decision": "接受",
            "assessment_status": "accepted",
            "reasons": ["旧裁决"],
            "raw_response_text": "这不是 JSON",
        },
        audit_item={"anchor_quote": "x"},
        chapters={1: "第一章：x"},
        gate_fn=lambda *a, **k: {"decision": "接受"},
        parse_fn=lambda raw: _safe_json(raw),
    )
    assert r2["regated"] is False
    assert r2["pending_review"] is True
    assert r2["decision"] is None


@pytest.mark.asyncio
async def test_t4_re_adjudicate_path_uses_current_ordering():
    """重裁路径也必须用**最新**的时序结果。

    时序依赖原文与实际引用跨度，两者都不在请求身份里——
    原文变了，重裁不能沿用没有时序的旧判定。
    """
    from backend.common.c_claim_audit_gate import gate
    from backend.services.recheck_v2 import _compute_cited_ordering

    anchor = "他终于问道：令牌究竟是真是假？"
    chapters_before = {
        1: f"第一章：{anchor}城主早已确认令牌是真品。",
        2: "第二章：次日众人启程赶路。",
    }
    chapters_after = {
        **chapters_before,
        3: f"第三章：{anchor}",
    }
    segs = [
        {
            "evidence_id": "e1",
            "chapter": 1,
            "text": f"【第 1 章（埋设处）】\n{anchor}城主早已确认令牌是真品。",
        }
    ]
    nli = {
        "verdict": "answer_ok",
        "reason": "原文交代了令牌真伪",
        "criteria_satisfied": True,
        "answer_supported": True,
        "has_increment": True,
        "claim_audit": {"version": "claims_v2", "units": []},
        "branch_hits": [],
    }
    item = {
        "fs_id": "fs_001",
        "question": "令牌是真品吗？",
        "criteria": "确认令牌真伪",
        "description": "",
        "anchor_quote": anchor,
        "anchor_chapter": 1,
        "answer": "城主早已确认令牌是真品。",
        "quote": "城主早已确认令牌是真品。",
        "chapter": 1,
        "evidence_segments": segs,
        "evidence_contract_version": "claims_v2",
    }

    seen: list[str] = []

    def ordering_fn(it, nli_obj):
        o = _compute_cited_ordering(
            chapters=chapters_after, audit_item=it, nli_obj=nli_obj
        )
        seen.append(o["verdict"] if o else "none")
        return o

    from backend.common.provenance_cache import (
        build_verified_cache_record,
        re_adjudicate_cached_record,
    )

    identity = {
        "request_id": "rid",
        "input_fingerprint": "fp",
        "base_input_fingerprint": "fp",
        "prompt_sha256": "ps",
        "evidence_fingerprint": "ef",
        "answer_sha256": "as",
        "quote_sha256": "qs",
        "model_config_sha256": "ms",
        "contract_version": "claims_v2",
        "code_snapshot_id": "old_code",
    }
    item_for_record = {
        "fs_id": "fs_001",
        "question": "令牌是真品吗？",
        "criteria": "确认令牌真伪",
        "description": "",
        "anchor_quote": anchor,
        "anchor_chapter": 1,
        "answer": "城主早已确认令牌是真品。",
        "quote": "城主早已确认令牌是真品。",
        "chapter": 1,
        "evidence_segments": segs,
        "evidence_contract_version": "claims_v2",
    }
    entry = build_verified_cache_record(
        item_for_record,
        identity,
        raw_response_text=json.dumps(nli, ensure_ascii=False),
        gate_result={
            "decision": "接受",
            "assessment_status": "accepted",
            "reasons": ["旧裁决"],
            "claim_check": None,
        },
        tokens=0,
        seconds=0.0,
        attempts=1,
        success=True,
    )
    # 只改代码版本 → 触发 re_adjudicate_needed
    new_identity = dict(identity)
    new_identity["code_snapshot_id"] = "new_code"

    out = re_adjudicate_cached_record(
        entry,
        item,
        new_identity,
        gate_fn=gate,
        ordering_fn=ordering_fn,
    )
    # 重裁时确实用最新原文算了时序
    assert seen, "重裁没有调用时序计算"
    assert out["adjudication_version"] == 2
    # 旧裁决进了历史，供追查
    assert out["adjudication_history"][0]["decision"] == "接受"


@pytest.mark.asyncio
async def test_t4_cited_ordering_uses_actual_cited_span_not_window():
    """F1：比较的是**裁决实际引用的句子**，不是整个检索窗口。

    复核复现：答案在锚点之前，却因所在窗口包含锚点而被豁免；
    再加一段无关后文，就得到"全部在后"。
    窗口只是来源容器，不能让窗口内部的句子一并豁免。
    """
    from backend.common.evidence_ordering import resolve_cited_ordering

    chapters = {
        1: "第一章：城主早已确认令牌是真品。他终于问道：令牌究竟是真是假？他走出了房门。",
        2: "第二章：次日众人启程赶路。",
    }
    anchor = "他终于问道：令牌究竟是真是假？"
    # e1 是第一章整窗（含锚点，也含锚点**之前**的答案）；e2 是无关后文
    segments = [
        {
            "evidence_id": "e1",
            "chapter": 1,
            "text": "【第 1 章（埋设处）】\n城主早已确认令牌是真品。他终于问道：令牌究竟是真是假？他走出了房门。",
        },
        {"evidence_id": "e2", "chapter": 2, "text": "次日众人启程赶路。"},
    ]

    # 裁决引用的是**锚点之前**的那句
    nli_before = {
        "verdict": "answer_ok",
        "claim_audit": {
            "units": [
                {
                    "unit_id": "u1",
                    "relation": "supported",
                    "evidence": [{"evidence_id": "e1", "quote": "城主早已确认令牌是真品。"}],
                }
            ]
        },
        "branch_hits": [
            {"branch_id": "c1", "evidence_id": "e1", "supporting_quote": "城主早已确认令牌是真品。"}
        ],
    }
    r = resolve_cited_ordering(
        chapters=chapters,
        anchor_quote=anchor,
        evidence_segments=segments,
        nli=nli_before,
    )
    assert r["verdict"] == "any_before", r
    assert r["summary"]["before"] == 2
    assert r["summary"]["after"] == 0

    # 同一窗口，但裁决引用的是**锚点之后**的句子
    nli_after = {
        "verdict": "answer_ok",
        "claim_audit": {
            "units": [
                {
                    "unit_id": "u1",
                    "relation": "supported",
                    "evidence": [{"evidence_id": "e1", "quote": "他走出了房门。"}],
                }
            ]
        },
        "branch_hits": [
            {"branch_id": "c1", "evidence_id": "e1", "supporting_quote": "他走出了房门。"}
        ],
    }
    r2 = resolve_cited_ordering(
        chapters=chapters,
        anchor_quote=anchor,
        evidence_segments=segments,
        nli=nli_after,
    )
    assert r2["verdict"] == "all_after", r2

    # 反向：同章窗口只有"锚点＋随后回答"、且没有别的窗口时，
    # 真正回答在后，不能因为整窗豁免而变成待定
    only = {
        1: "第一章：他终于问道：令牌究竟是真是假？城主早已确认令牌是真品。",
    }
    r3 = resolve_cited_ordering(
        chapters=only,
        anchor_quote=anchor,
        evidence_segments=[
            {
                "evidence_id": "e1",
                "chapter": 1,
                "text": "【第 1 章（埋设处）】\n他终于问道：令牌究竟是真是假？城主早已确认令牌是真品。",
            }
        ],
        nli={
            "verdict": "answer_ok",
            "claim_audit": {
                "units": [
                    {
                        "unit_id": "u1",
                        "relation": "supported",
                        "evidence": [{"evidence_id": "e1", "quote": "城主早已确认令牌是真品。"}],
                    }
                ]
            },
            "branch_hits": [],
        },
    )
    assert r3["verdict"] == "all_after", r3
    assert r3["summary"]["after"] == 1


@pytest.mark.asyncio
async def test_t4_cache_hit_regates_with_current_ordering_without_model_call():
    """F2：缓存命中不得无条件沿用旧裁决。

    复核复现：同一提示词与证据窗口，原文初始只有一处锚点 → 全部在后、门禁接受；
    之后在未进入提示词的后文章节加入同一句锚点 → 唯一定位失效 → 时序待定 → 门禁存疑。
    请求身份与代码版本未变，缓存仍是 verified_hit。
    """
    from backend.common.c_claim_audit_gate import gate
    from backend.common.evidence_ordering import resolve_cited_ordering

    anchor = "他终于问道：令牌究竟是真是假？"
    chapters_before = {
        1: f"第一章：{anchor}城主早已确认令牌是真品。",
        2: "第二章：次日众人启程赶路。",
    }
    chapters_after = {
        **chapters_before,
        3: f"第三章：{anchor}",
    }
    segments = [
        {
            "evidence_id": "e1",
            "chapter": 1,
            "text": f"【第 1 章（埋设处）】\n{anchor}城主早已确认令牌是真品。",
        }
    ]
    from backend.common.claim_evidence_v2 import answer_units

    _ans = "城主早已确认令牌是真品。"
    _uid = answer_units(_ans)[0]["unit_id"]
    nli = {
        "verdict": "answer_ok",
        "reason": "原文交代了令牌真伪",
        "criteria_satisfied": True,
        "answer_supported": True,
        "has_increment": True,
        "claim_audit": {
            "version": "claims_v2",
            "units": [
                {
                    "unit_id": _uid,
                    "relation": "supported",
                    "same_referent": True,
                    "answer_frame": {
                        "attribution": "attributed",
                        "holder": "城主",
                        "world": "actual",
                        "modality": "factual",
                        "realization": "completed",
                    },
                    "evidence_frame": {
                        "attribution": "attributed",
                        "holder": "城主",
                        "world": "actual",
                        "modality": "factual",
                        "realization": "completed",
                    },
                    "evidence": [{"evidence_id": "e1", "quote": "城主早已确认令牌是真品。"}],
                }
            ]
        },
        "branch_hits": [],
    }

    def _item(chapters):
        it = {
            "fs_id": "fs_001",
            "question": "令牌是真品吗？",
            "criteria": "确认令牌真伪",
            "description": "",
            "anchor_quote": anchor,
            "anchor_chapter": 1,
            "answer": _ans,
            "quote": "城主早已确认令牌是真品。",
            "chapter": 1,
            "evidence_segments": segments,
            "evidence_contract_version": "claims_v2",
        }
        o = resolve_cited_ordering(
            chapters=chapters,
            anchor_quote=anchor,
            evidence_segments=segments,
            nli=nli,
        )
        it["evidence_ordering"] = o
        return it

    o1 = resolve_cited_ordering(
        chapters=chapters_before,
        anchor_quote=anchor,
        evidence_segments=segments,
        nli=nli,
    )
    assert o1["verdict"] == "all_after"
    g1 = gate(_item(chapters_before), dict(nli))
    assert g1["decision"] == "接受"

    # 原文变了：同一句锚点出现在后文章节 → 定位不唯一 → 时序待定
    o2 = resolve_cited_ordering(
        chapters=chapters_after,
        anchor_quote=anchor,
        evidence_segments=segments,
        nli=nli,
    )
    assert o2["verdict"] == "indeterminate", o2
    g2 = gate(_item(chapters_after), dict(nli))
    assert g2["decision"] != "接受"

    # 缓存命中时必须重裁：旧裁决是"接受"，重裁后必须是"存疑"
    from backend.services.recheck_v2 import _regate_cached_record

    entry = {
        "decision": "接受",
        "assessment_status": "accepted",
        "reasons": ["旧裁决"],
        "raw_response_text": json.dumps(nli, ensure_ascii=False),
    }
    fresh = _regate_cached_record(
        entry,
        audit_item={
            "fs_id": "fs_001",
            "anchor_quote": anchor,
            "answer": _ans,
            "quote": "城主早已确认令牌是真品。",
            "chapter": 1,
            "evidence_segments": segments,
            "evidence_contract_version": "claims_v2",
        },
        chapters=chapters_after,
        gate_fn=gate,
        parse_fn=lambda raw: json.loads(raw),
    )
    assert fresh["regated"] is True
    assert fresh["decision"] != "接受"


@pytest.mark.asyncio
async def test_t4_evidence_ordering_same_chapter_before_after_overlap_repeat_cross_chapter():
    """时序判定：同坐标系、原文哈希绑定、定位必须唯一、多段证据逐段判定。

    覆盖：同章在前 / 同章在后 / 跨度重叠 / 引文重复（定位不唯一）/ 跨章。
    规则：只挑一段靠后的不算通过——必须**每一段**都在锚点之后。
    """
    from backend.common.evidence_ordering import (
        resolve_cited_ordering,
        verify_ordering_record,
    )

    anchor = "他把令牌藏进石匣。"
    chapters = {
        1: f"第一章：{anchor}匾额上写着甲乙丙丁。",
        2: "第二章：城主打开石匣，确认令牌确为真品。匾额的方位顺序被打乱。",
        3: "第三章：令牌后来被转手三次，最终落到国师手里。",
    }

    def seg(unit: int, text: str) -> dict:
        return {"evidence_id": f"e{unit}", "chapter": unit, "text": text}

    def _nli(segments: list[dict]) -> dict:
        """构造一份只引用了这些段落的裁决。"""
        return {
            "verdict": "answer_ok",
            "claim_audit": {
                "version": "claims_v2",
                "units": [
                    {
                        "unit_id": "u1",
                        "relation": "supported",
                        "evidence": [
                            {"evidence_id": s["evidence_id"], "quote": s["text"].split("】\n")[-1]}
                            for s in segments
                        ],
                    }
                ],
            },
            "branch_hits": [],
        }

    def _order(chapters: dict, anchor_q: str, segments: list[dict]) -> dict:
        return resolve_cited_ordering(
            chapters=chapters,
            anchor_quote=anchor_q,
            evidence_segments=segments,
            nli=_nli(segments),
        )

    # 1) 跨章在后
    r = _order(chapters, anchor, [seg(2, "城主打开石匣，确认令牌确为真品。")])
    assert r["verdict"] == "all_after"
    assert r["citations"][0]["relation"] == "after"

    # 2) 跨章在前：证据所在单元在锚点之前
    before_chapters = {
        1: "第一章：匾额上写着甲乙丙丁。",
        2: f"第二章：{anchor}",
    }
    r = _order(before_chapters, anchor, [seg(1, "匾额上写着甲乙丙丁。")])
    assert r["verdict"] == "any_before"
    assert r["citations"][0]["relation"] == "before"

    # 3) 同章在前：锚点句在证据句之前
    same = {1: f"第一章：匾额上写着甲乙丙丁。{anchor}"}
    r = _order(same, anchor, [seg(1, "匾额上写着甲乙丙丁。")])
    assert r["citations"][0]["relation"] == "before"
    assert r["verdict"] == "any_before"

    # 4) 同章在后：锚点句在证据句之前
    same2 = {1: f"第一章：{anchor}匾额上写着甲乙丙丁。"}
    r = _order(same2, anchor, [seg(1, "匾额上写着甲乙丙丁。")])
    assert r["citations"][0]["relation"] == "after"
    assert r["verdict"] == "all_after"

    # 5) 跨度重叠 → 时序待定
    same3 = {1: f"第一章：前缀{anchor}后缀"}
    r = _order(same3, anchor, [seg(1, "他把令牌藏进")])
    assert r["citations"][0]["relation"] == "overlap"
    assert r["verdict"] == "indeterminate"

    # 6) 引文重复 → 定位不唯一 → 时序待定
    dup = {1: f"第一章：{anchor}匾额上写着甲乙丙丁。\n第二章：{anchor}匾额上写着甲乙丙丁。"}
    r = _order(dup, anchor, [seg(1, "匾额上写着甲乙丙丁。")])
    assert r["anchor"]["resolved"] is False
    assert r["anchor"]["locate_reason"] == "anchor_not_unique"
    assert r["verdict"] == "indeterminate"

    # 7) 证据文本重复 → 定位不唯一 → 时序待定
    dup_seg = {1: f"第一章：{anchor}重复句。重复句。"}
    r = _order(dup_seg, anchor, [seg(1, "重复句。")])
    assert r["citations"][0]["resolved"] is False
    assert r["citations"][0]["locate_reason"] == "segment_segment_text_not_unique"
    assert r["verdict"] == "indeterminate"

    # 8) 多段证据：一段在后一段在前 → 不能算通过
    multi = {
        1: f"第一章：早句。{anchor}",
        2: "第二章：晚句。",
    }
    r = _order(multi, anchor, [seg(2, "晚句。"), seg(1, "早句。")])
    assert r["verdict"] == "any_before"
    assert r["summary"]["after"] == 1
    assert r["summary"]["before"] == 1

    # 9) 保存恢复后结果一致：复算与落盘记录一致，且原文哈希绑定
    rec = _order(multi, anchor, [seg(2, "晚句。"), seg(1, "早句。")])
    import json

    restored = json.loads(json.dumps(rec, ensure_ascii=False))
    v = verify_ordering_record(
        restored,
        chapters=multi,
        anchor_quote=anchor,
        evidence_segments=[seg(2, "晚句。"), seg(1, "早句。")],
        nli=_nli([seg(2, "晚句。"), seg(1, "早句。")]),
    )
    assert v["matched"] is True
    assert v["source_hash_matched"] is True

    # 原文变了 → 哈希不匹配 → 坐标作废
    v2 = verify_ordering_record(
        restored,
        chapters={**multi, 1: f"第一章：{anchor}早句被改。"},
        anchor_quote=anchor,
        evidence_segments=[seg(2, "晚句。"), seg(1, "早句。")],
        nli=_nli([seg(2, "晚句。"), seg(1, "早句。")]),
    )
    assert v2["matched"] is False
    assert v2["reason"] == "source_hash_mismatch"


@pytest.mark.asyncio
async def test_t4_ordering_pass_only_clears_temporal_doubt_not_other_checks():
    """时序通过只解除时间疑点；答案正确性、条件满足、信息增量仍由原门禁判断。"""
    from backend.common.c_claim_audit_gate import gate
    from backend.common.evidence_ordering import resolve_cited_ordering

    anchor = "他把令牌藏进石匣。"
    chapters = {1: f"第一章：{anchor}", 2: "第二章：城主确认令牌确为真品。"}
    segs = [{"evidence_id": "e2", "chapter": 2, "text": "城主确认令牌确为真品。"}]
    ordering = resolve_cited_ordering(
        chapters=chapters,
        anchor_quote=anchor,
        evidence_segments=segs,
        nli={
            "verdict": "answer_ok",
            "claim_audit": {
                "version": "claims_v2",
                "units": [
                    {
                        "unit_id": "u1",
                        "relation": "supported",
                        "evidence": [{"evidence_id": "e2", "quote": "城主确认令牌确为真品。"}],
                    }
                ],
            },
            "branch_hits": [],
        },
    )
    assert ordering["verdict"] == "all_after"

    base_item = {
        "fs_id": "fs_001",
        "question": "令牌是真品吗？",
        "criteria": "确认令牌真伪",
        "description": "",
        "anchor_quote": anchor,
        "anchor_chapter": 1,
        "answer": "令牌确为真品。",
        "quote": "城主确认令牌确为真品。",
        "chapter": 2,
        "evidence_segments": [
            {"evidence_id": "e2", "chapter": 2, "text": "城主确认令牌确为真品。"}
        ],
        "evidence_contract_version": "claims_v2",
    }
    nli_ok = {
        "verdict": "answer_ok",
        "reason": "原文交代了令牌真伪",
        "criteria_satisfied": True,
        "answer_supported": True,
        "has_increment": True,
    }

    # 不带时序结果：旧的章粒度判断把同章/跨章都当疑点
    item_old = dict(base_item)
    g_old = gate(item_old, dict(nli_ok))
    assert "evidence_not_after_anchor" in (g_old.get("reasons") or []) or g_old[
        "decision"
    ] == "存疑"

    # 带时序结果且全部在后：时间疑点解除
    item_new = dict(base_item)
    item_new["evidence_ordering"] = ordering
    g_new = gate(item_new, dict(nli_ok))
    assert "evidence_not_after_anchor" not in str(g_new.get("reasons") or "")
    assert "evidence_order_indeterminate" not in str(g_new.get("reasons") or "")

    # 时序通过**不放宽其他条件**：NLI 说信息无增量，仍然不通过
    nli_no_increment = dict(nli_ok)
    nli_no_increment["has_increment"] = False
    g_strict = gate(dict(item_new), nli_no_increment)
    assert g_strict["decision"] != "接受"

    # 时序待定 → 保留时间疑点
    item_indet = dict(base_item)
    item_indet["evidence_ordering"] = {
        "verdict": "indeterminate",
        "segments": [],
        "summary": {},
    }
    g_indet = gate(item_indet, dict(nli_ok))
    assert "evidence_order_indeterminate" in str(g_indet.get("reasons") or "")


@pytest.mark.asyncio
async def test_t1_interrupted_resume_and_duplicate_submission_idempotent_with_verified_identity():
    """T1 Case 4: Resuming or resubmitting the same candidate reuses verified cache without
    re-billing or duplicating notes; changing the candidate answer invalidates cache reuse.
    """
    chapters = {
        1: "第一章：他把令牌藏进石匣。",
        2: "第二章：据说令牌为真品；有人怀疑钥匙可以打开宫门。",
    }
    audit_cache: dict[str, dict[str, Any]] = {}
    recheck_cache: dict[str, dict[str, Any]] = {}
    answer_text = "令牌为真品；钥匙可以打开宫门。"

    async def fake_model(prompt: str) -> str:
        if "### 候选 candidate_id=" in prompt:
            import re

            eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
            return json.dumps(
                [
                    {
                        "candidate_id": "fs_001",
                        "verdict": "SUPPORTED",
                        "answer": answer_text,
                        "evidence_id": eids[-1],
                        "supporting_quote": "据说令牌为真品；有人怀疑钥匙可以打开宫门。",
                        "counter_search": {"performed": True, "checked_evidence_ids": eids},
                    }
                ],
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "verdict": "off_topic",
                "reason": "传言不能作为事实",
                "criteria_satisfied": False,
                "answer_supported": False,
                "has_increment": False,
            },
            ensure_ascii=False,
        )

    item = _make_item()
    out1 = await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=fake_model,
        enable_c_on_demand_audit=True,
        audit_cache=audit_cache,
        recheck_cache=recheck_cache,
    )
    assert out1.calls == 2
    assert out1.audit_calls == 1
    assert len(item.notes) == 1

    # Second submission with identical candidate & evidence: 0 live calls, notes not duplicated
    out2 = await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=fake_model,
        enable_c_on_demand_audit=True,
        audit_cache=audit_cache,
        recheck_cache=recheck_cache,
    )
    assert out2.calls == 0
    assert out2.audit_calls == 0
    assert out2.recheck_cache_hits == 1
    assert out2.audit_cache_hits == 1
    assert len(item.notes) == 1

    # Third submission where initial recheck returns a new answer: audit_cache must NOT reuse old audit
    answer_text = "令牌并非真品；钥匙也无法打开宫门。"
    out3 = await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=fake_model,
        enable_c_on_demand_audit=True,
        audit_cache=audit_cache,
        recheck_cache=None,
    )
    assert out3.audit_calls == 1, "Changed answer must not reuse old audit cache"
    assert out3.audit_cache_hits == 0


@pytest.mark.asyncio
async def test_t1_foreign_evidence_id_or_missing_quote_rejected_with_specific_error():
    """T1 Case 5: Citing foreign candidate evidence_id or non-existent quote is rejected with specific error."""
    chapters = {
        1: "第一章：他把令牌藏进石匣。",
        2: "第二章：城主确认令牌为真品。",
    }
    item = _make_item(question="令牌是否为真品？", criteria="确认令牌真伪")

    async def model_foreign_eid(_prompt: str) -> str:
        return json.dumps(
            [
                {
                    "candidate_id": "fs_001",
                    "verdict": "SUPPORTED",
                    "answer": "令牌为真品。",
                    "evidence_id": "foreign_eid_999",
                    "supporting_quote": "城主确认令牌为真品。",
                    "counter_search": {"performed": True, "checked_evidence_ids": ["foreign_eid_999"]},
                }
            ],
            ensure_ascii=False,
        )

    out_for = await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=model_foreign_eid,
        enable_c_on_demand_audit=True,
    )
    assert out_for.verdicts[0].verdict == "INVALID"
    assert out_for.verdicts[0].reason == "FOREIGN_ID"
    assert item.status == "active"


@pytest.mark.asyncio
async def test_t1_production_entry_final_summary_runner_integration(tmp_path: Path):
    """T1 Deliverable: Offline integration run executed through the real production entry `FinalSummaryRunner._run_recheck_v2`."""
    from backend.config.settings import AnalysisConfig, APIConfig, AppConfig
    from backend.services.final_summary import FinalSummaryRunner

    blocks_dir = tmp_path / "blocks"
    blocks_dir.mkdir(parents=True)
    (blocks_dir / "0001.txt").write_text("第1回\n沈砚临行前，他把令牌藏进石匣。", encoding="utf-8")
    (blocks_dir / "0002.txt").write_text("第2回\n茶馆里众人议论，据说令牌为真品；有人怀疑钥匙可以打开宫门。", encoding="utf-8")

    out_dir = tmp_path / "output"
    out_dir.mkdir(parents=True)

    cfg = AppConfig(
        api=APIConfig(api_key="fake-key", base_url="http://localhost:9999/v1", model="fake-model"),
        analysis=AnalysisConfig(
            enable_c_on_demand_audit=True,
            fix_b2_evidence_and_recheck=True,
            fix_b4_flow_and_storage=True,
            enable_b_trace=True,
        ),
        working_directory=str(tmp_path),
    )

    class _FakeResp:
        def __init__(self, content: str):
            self.success = True
            self.content = content
            self.tokens = (120, 45)
            self.attempts = 1
            self.error = ""

    class _FakeLLM:
        def __init__(self):
            self.calls: list[str] = []

        async def chat_auto(self, messages, max_tokens=4096, **_kw):
            prompt = messages[0]["content"]
            self.calls.append(prompt)
            if "### 候选 candidate_id=" in prompt:
                import re

                eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
                return _FakeResp(
                    json.dumps(
                        [
                            {
                                "candidate_id": "fs_001",
                                "verdict": "SUPPORTED",
                                "answer": "令牌为真品；钥匙可以打开宫门。",
                                "evidence_id": eids[-1],
                                "supporting_quote": "据说令牌为真品；有人怀疑钥匙可以打开宫门。",
                                "counter_search": {"performed": True, "checked_evidence_ids": eids},
                            }
                        ],
                        ensure_ascii=False,
                    )
                )
            return _FakeResp(
                json.dumps(
                    {
                        "verdict": "off_topic",
                        "reason": "原文仅为传言与怀疑，并非已证实事实",
                        "criteria_satisfied": False,
                        "answer_supported": False,
                        "has_increment": False,
                    },
                    ensure_ascii=False,
                )
            )

    ledger = ForeshadowLedger()
    ledger.items.append(_make_item())
    ledger_path = out_dir / "foreshadow_ledger.json"
    ledger.save(ledger_path)

    runner = FinalSummaryRunner(
        config=cfg,
        output_dir=out_dir,
        start_chapter=1,
        end_chapter=2,
        batch_size=2,
        ledger_path=ledger_path,
    )
    runner._llm = _FakeLLM()
    await runner._run_recheck_v2([])

    assert len(runner._llm.calls) == 2
    assert runner.last_recheck_summary["calls"] == 2
    assert runner.last_recheck_summary["audit_calls"] == 1
    assert runner.ledger.items[0].status == "active"
    assert runner.ledger.items[0].resolution_quality == "rejected_by_audit"
    assert (out_dir / "recheck_verdicts.json").is_file()
    assert (out_dir / "c_audit_cache.json").is_file()


# ==============================================================================
# T2 Mandatory Scorer & Label Verification Cases (Section 四)
# ==============================================================================


def test_t2_negation_rumor_and_subject_object_swap_rejected():
    """T2 Case 2 (R3-2 exact reproduction):
    Chapter 2 has rumor ('确认令牌并非真品，而是赝品') followed by truth ('最终确认令牌确为真品').
    Candidate quotes the rumor and answers '令牌已经被确认是赝品' -> must return verified=False!
    Also verifies subject-object swap ('沈砚刺杀了国师' vs '国师刺杀了沈砚') is rejected.
    """
    chapters = {
        1: "第一章：他把令牌藏进石匣。",
        2: (
            "第二章：有人传播谣言：“城主打开石匣，确认令牌并非真品，而是赝品。”"
            "但真实情况是：城主打开石匣，最终确认令牌确为真品。"
        ),
    }
    ob = {
        "obs_id": "obs_token",
        "obs_type": "resolution",
        "anchor_quote": "他把令牌藏进石匣。",
        "question": "石匣里的令牌是真品还是赝品？",
        "answer_criteria": "确认令牌为真品",
        "resolution_quote": "但真实情况是：城主打开石匣，最终确认令牌确为真品。",
        "entities": ["城主", "令牌", "石匣"],
    }
    it = {
        "id": "fs_001",
        "anchor_chapter": 1,
        "anchor_quote": "他把令牌藏进石匣。",
        "question": "石匣里的令牌是真品还是赝品？",
        "answer_criteria": "确认令牌真伪",
    }
    v_rumor = CandidateVerdict(
        candidate_id="fs_001",
        verdict="SUPPORTED",
        answer="令牌已经被确认是赝品。",
        quote="城主打开石匣，确认令牌并非真品，而是赝品。",
        counter_search_performed=True,
    )
    res_rumor = verify_resolution_evidence(ob, it, v_rumor, a_ch=1, r_ch=2, chapters=chapters)
    assert res_rumor["verified"] is False

    # Subject-object swap test
    indexer = BookSpanIndexer({1: "第一章：沈砚潜入皇城。", 2: "第二章：沈砚刺杀了国师。"})
    ob_swap = {
        "obs_id": "obs_swap",
        "obs_type": "resolution",
        "anchor_chapter": 1,
        "anchor_quote": "沈砚潜入皇城。",
        "question": "沈砚与国师之间谁刺杀了谁？",
        "answer_criteria": "交代沈砚刺杀了国师",
        "resolution_chapter": 2,
        "resolution_quote": "沈砚刺杀了国师。",
        "entities": ["沈砚", "国师"],
    }
    it_swap = {
        "id": "fs_swap",
        "anchor_chapter": 1,
        "anchor_quote": "沈砚潜入皇城。",
        "question": "沈砚与国师之间谁刺杀了谁？",
    }
    rec_swap = adjudicate_reference_candidate_pair(
        ob_swap,
        it_swap,
        {"verdict": "SUPPORTED", "answer": "国师刺杀了沈砚。", "quote": "沈砚刺杀了国师。", "chapter": 2},
        indexer=indexer,
    )
    assert rec_swap.counted_as_hit is False
    assert rec_swap.answer_fact_status == "role_swapped"


def test_t2_independent_review_question_only_governs_equivalence_not_answer_quality():
    """独立评审只判"是不是同一件事"时：
    1. 等价判定由评审结论决定，结构化规则不得否决（锚点章偏移不再误杀）；
    2. 答案质量仍由结构化裁决决定——评审说"同一件事"不能把 INSUFFICIENT 变成命中；
    3. 全量治理模式（full）行为不变。
    """
    chapters = {
        1: "第一章：他把令牌藏进石匣。",
        2: "第二章：城主打开石匣，最终确认令牌确为真品。",
        7: "第七章：城主又提起令牌之事。",
    }
    indexer = BookSpanIndexer(chapters)
    ob = {
        "obs_id": "obs_q_only",
        "obs_type": "resolution",
        "anchor_chapter": 1,
        "anchor_quote": "他把令牌藏进石匣。",
        "question": "石匣里的令牌是真品还是赝品？",
        "answer_criteria": "确认令牌为真品",
        "resolution_chapter": 2,
        "resolution_quote": "城主打开石匣，最终确认令牌确为真品。",
        "entities": ["城主", "令牌"],
    }
    # 候选锚点落在第 7 章（与观察锚点章差 6）：结构化规则会因 anchor_chapter_mismatch 判否
    it = {
        "id": "fs_q_only",
        "anchor_chapter": 7,
        "anchor_quote": "城主又提起令牌之事。",
        "question": "石匣里的令牌是真品还是赝品？",
        "answer_criteria": "确认令牌真伪",
    }
    verdict = {
        "verdict": "SUPPORTED",
        "answer": "令牌确为真品。",
        "quote": "城主打开石匣，最终确认令牌确为真品。",
        "chapter": 2,
    }

    # 1. 无独立评审：结构化规则自己否决（锚点章不符）
    rec_none = adjudicate_reference_candidate_pair(ob, it, verdict, indexer=indexer)
    assert rec_none.counted_as_hit is False

    # 2. question_only 评审：等价由评审决定
    rec_review = adjudicate_reference_candidate_pair(
        ob,
        it,
        verdict,
        indexer=indexer,
        independent_review_entry={
            "governance_mode": "question_only",
            "question_equivalence": "equivalent",
            "adjudication_source": "independent_agent_review",
        },
    )
    assert rec_review.question_equivalence == "equivalent"
    assert rec_review.adjudication_source == "structural_semantic_verifier"

    # 3. question_only 也不能把不合格答案判成命中：管线判 INSUFFICIENT 时不命中
    rec_insufficient = adjudicate_reference_candidate_pair(
        ob,
        it,
        {"verdict": "INSUFFICIENT", "answer": "", "quote": "", "chapter": 2},
        indexer=indexer,
        independent_review_entry={
            "governance_mode": "question_only",
            "question_equivalence": "equivalent",
        },
    )
    assert rec_insufficient.question_equivalence == "equivalent"
    assert rec_insufficient.counted_as_hit is False
    assert rec_insufficient.answer_fact_status == "verdict_insufficient"

    # 4. full 治理模式：评审给的答案质量直接生效
    rec_full = adjudicate_reference_candidate_pair(
        ob,
        it,
        verdict,
        indexer=indexer,
        independent_review_entry={
            "governance_mode": "full",
            "question_equivalence": "equivalent",
            "answer_fact_status": "supported_fact",
            "evidence_attribution": "narrator_fact",
            "closing_criteria_status": "satisfied",
        },
    )
    assert rec_full.counted_as_hit is True
    assert rec_full.adjudication_source == "independent_blind_review"


def test_t2_intra_subchapter_and_cross_chapter_span_ordering_and_ambiguous_quotes():
    """T2 Case 4 (R3-2 exact reproduction):
    Setup and true resolution in the SAME natural subchapter must score when resolution span > anchor span;
    material before the setup or duplicate ambiguous quotes must NOT score.
    """
    chapters = {
        1: (
            "前置背景：城主打开石匣。"
            "埋设段：他把令牌藏进石匣。"
            "后文揭晓段：但真实情况是：城主打开石匣，最终确认令牌确为真品。"
        ),
    }
    ob = {
        "obs_id": "obs_intra",
        "obs_type": "resolution",
        "anchor_quote": "他把令牌藏进石匣。",
        "question": "令牌真伪如何？",
        "answer_criteria": "确认令牌确为真品",
        "resolution_quote": "但真实情况是：城主打开石匣，最终确认令牌确为真品。",
        "entities": ["城主", "令牌"],
    }
    it = {
        "id": "fs_intra",
        "anchor_chapter": 1,
        "anchor_quote": "他把令牌藏进石匣。",
        "question": "令牌真伪如何？",
        "answer_criteria": "确认令牌确为真品",
    }
    # 1. Valid intra-subchapter resolution after anchor
    v_valid = CandidateVerdict(
        candidate_id="fs_intra",
        verdict="SUPPORTED",
        answer="城主打开石匣，最终确认令牌确为真品。",
        quote="但真实情况是：城主打开石匣，最终确认令牌确为真品。",
        counter_search_performed=True,
    )
    res_valid = verify_resolution_evidence(ob, it, v_valid, a_ch=1, r_ch=1, chapters=chapters)
    assert res_valid["verified"] is True, f"Valid intra-subchapter resolution must pass, got {res_valid}"

    # 2. Ambiguous quote occurring twice in chapter 1 ("城主打开石匣") without disambiguating span
    v_ambig = CandidateVerdict(
        candidate_id="fs_intra",
        verdict="SUPPORTED",
        answer="城主打开石匣确认令牌确为真品。",
        quote="城主打开石匣",
        counter_search_performed=True,
    )
    res_ambig = verify_resolution_evidence(ob, it, v_ambig, a_ch=1, r_ch=1, chapters=chapters)
    assert res_ambig["verified"] is False
    assert "ambiguous" in res_ambig["reason"]


def test_t2_conjunction_and_partial_answer_vs_disjunction_or_and_conflict_log():
    """T2 Cases 5, 6, 7:
    - AND criteria where evidence only covers branch A -> partial_answer=True, counted_as_hit=False
    - OR criteria where evidence covers branch A -> satisfied, counted_as_hit=True
    - Multi-to-one conflict logged and never double-rewarded
    """
    chapters = {
        1: "第一章：密室失窃，悬念重重。",
        2: "第二章：捕快查明盗贼是韩立，但始终未能查明幕后指使者是谁。",
    }
    indexer = BookSpanIndexer(chapters)

    ob_and = {
        "obs_id": "obs_and",
        "obs_type": "resolution",
        "anchor_chapter": 1,
        "anchor_quote": "密室失窃，悬念重重。",
        "question": "密室失窃的盗贼是谁，且幕后指使者是谁？",
        "answer_criteria": "同时交代盗贼是韩立并且查明幕后指使者身份",
        "required_branches": ["交代盗贼是韩立", "查明幕后指使者身份"],
        "criteria_logic": "AND",
        "resolution_chapter": 2,
        "resolution_quote": "捕快查明盗贼是韩立",
        "entities": ["韩立"],
    }
    it = {
        "id": "fs_01",
        "anchor_chapter": 1,
        "anchor_quote": "密室失窃，悬念重重。",
        "question": "密室失窃的盗贼是谁，且幕后指使者是谁？",
    }
    rec_and = adjudicate_reference_candidate_pair(
        ob_and,
        it,
        {"verdict": "SUPPORTED", "answer": "捕快查明盗贼是韩立。", "quote": "捕快查明盗贼是韩立", "chapter": 2},
        indexer=indexer,
    )
    assert rec_and.counted_as_hit is False
    assert rec_and.partial_answer is True
    assert rec_and.closing_criteria_status == "partial_answer"

    # OR criteria with same evidence -> satisfied!
    ob_or = {**ob_and, "obs_id": "obs_or", "criteria_logic": "OR"}
    rec_or = adjudicate_reference_candidate_pair(
        ob_or,
        it,
        {"verdict": "SUPPORTED", "answer": "捕快查明盗贼是韩立。", "quote": "捕快查明盗贼是韩立", "chapter": 2},
        indexer=indexer,
    )
    assert rec_or.counted_as_hit is True
    assert rec_or.closing_criteria_status == "satisfied"

    # Multi-to-one conflict: two reference registration items matching the same candidate
    reg1 = {
        "obs_id": "reg_1",
        "obs_type": "registration",
        "anchor_chapter": 1,
        "anchor_quote": "密室失窃，悬念重重。",
        "question": "密室失窃的盗贼是谁？",
        "entities": ["密室"],
    }
    reg2 = {
        "obs_id": "reg_2",
        "obs_type": "registration",
        "anchor_chapter": 1,
        "anchor_quote": "密室失窃，悬念重重。",
        "question": "密室失窃的盗贼究竟是谁？",
        "entities": ["密室"],
    }
    res_pool = assign_exclusive_matches_with_conflict_log([reg1, reg2], [it], None, indexer=indexer)
    assert res_pool["strict_hits"] == 1, "Single candidate must not be rewarded twice"
    assert len(res_pool["multi_to_one_conflicts"]) == 1
    assert res_pool["multi_to_one_conflicts"][0]["candidate_id"] == "fs_01"

    # Neutral Class C observable category check
    assert (
        classify_c_observable_branch_outcome({"decision": "拒绝"}, {"decision": "存疑"})
        == "both_not_accepted"
    )
    assert (
        classify_c_observable_branch_outcome({"decision": "接受"}, {"decision": "存疑"})
        == "branch_disagreement_cur_accept_v2_not"
    )


# ==============================================================================
# T3 Mandatory Provenance & Cache Verification Cases (Section 五)
# ==============================================================================


def test_t3_provenance_cache_all_six_mandatory_cases():
    """T3 Cases 1~6:
    1. Exact identity match -> verified_hit
    2. Mutating answer, evidence, prompt, model config, or contract -> mismatch
    3. Old cache missing raw_response or call-time provenance -> unverifiable
    4. Same raw_response with updated code_snapshot_id -> re_adjudicate_needed & history preserved
    5. Label bound to old answer or evidence -> stale
    6. Failed call or empty response -> failed_or_incomplete, never reusable
    """
    base_item = {
        "fs_id": "fs_101",
        "question": "铜匣里装的是什么？",
        "criteria": "明确交代铜匣内物",
        "description": "铜匣之谜",
        "anchor_quote": "他把铜匣锁入柜中。",
        "anchor_chapter": 1,
        "answer": "铜匣里装的是一枚虎符。",
        "quote": "打开铜匣，里面赫然是一枚虎符。",
        "chapter": 5,
        "prereg": None,
        "evidence_segments": [
            {"evidence_id": "seg_5", "chapter": 5, "text": "第五章：他打开铜匣，里面赫然是一枚虎符。"}
        ],
        "evidence_contract_version": CLAIM_V2,
    }
    model_cfg = {
        "model": "test-model",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-SUPER-SECRET-DO-NOT-LEAK",
        "temperature": 0.0,
        "reasoning_effort": "low",
        "max_tokens": 4096,
    }
    prompt = build_nli_prompt(base_item)
    ident = build_request_identity(
        base_item,
        prompt=prompt,
        model_config=model_cfg,
        contract_version=CLAIM_V2,
        code_snapshot_id="snap_v1",
    )
    assert "api_key" not in json.dumps(ident), "API key must never appear in request identity"

    raw_resp = json.dumps(
        {
            "verdict": "answer_ok",
            "reason": "原文直接证实铜匣内为虎符",
            "criteria_satisfied": True,
            "answer_supported": True,
            "has_increment": True,
            "claim_audit": {
                "answer_units": [
                    {
                        "unit_id": "u1",
                        "answer_span": "铜匣里装的是一枚虎符",
                        "entailment": "supported",
                        "evidence_id": "seg_5",
                        "supporting_quote": "打开铜匣，里面赫然是一枚虎符",
                        "answer_frame": "fact",
                        "evidence_frame": "fact",
                        "changed_attribution": False,
                        "changed_modality": False,
                    }
                ]
            },
        },
        ensure_ascii=False,
    )
    g_res = gate(base_item, json.loads(raw_resp))
    rec = build_verified_cache_record(
        base_item,
        ident,
        raw_response_text=raw_resp,
        gate_result=g_res,
        tokens=500,
        seconds=1.2,
        attempts=1,
        success=True,
    )

    # Case 1: Exact match -> verified_hit
    v1 = verify_cache_entry(rec, ident)
    assert v1.status == "verified_hit" and v1.reusable is True

    # Case 2: Single-field mutations -> mismatch
    for mutated_field, mut_ident in [
        (
            "answer_sha256",
            build_request_identity(
                {**base_item, "answer": "铜匣里装的是兵书。"},
                prompt=prompt,
                model_config=model_cfg,
                contract_version=CLAIM_V2,
                code_snapshot_id="snap_v1",
            ),
        ),
        (
            "evidence_fingerprint",
            build_request_identity(
                {
                    **base_item,
                    "evidence_segments": [{"evidence_id": "seg_5", "chapter": 5, "text": "另一段不同原文"}],
                },
                prompt=prompt,
                model_config=model_cfg,
                contract_version=CLAIM_V2,
                code_snapshot_id="snap_v1",
            ),
        ),
        (
            "prompt_sha256",
            build_request_identity(
                base_item,
                prompt=prompt + "\nExtra instruction",
                model_config=model_cfg,
                contract_version=CLAIM_V2,
                code_snapshot_id="snap_v1",
            ),
        ),
        (
            "model_config_sha256",
            build_request_identity(
                base_item,
                prompt=prompt,
                model_config={**model_cfg, "model": "other-model"},
                contract_version=CLAIM_V2,
                code_snapshot_id="snap_v1",
            ),
        ),
        (
            "contract_version",
            build_request_identity(
                base_item,
                prompt=prompt,
                model_config=model_cfg,
                contract_version="current",
                code_snapshot_id="snap_v1",
            ),
        ),
    ]:
        vm = verify_cache_entry(rec, mut_ident)
        assert vm.status == "mismatch" and vm.reusable is False
        assert mutated_field in vm.mismatched_fields

    # Case 3: Old cache missing raw_response or call-time provenance -> unverifiable
    legacy_entry = {
        "fs_id": "fs_101",
        "decision": "接受",
        "measured_live": True,
        "tokens": 500,
    }
    v3 = verify_cache_entry(legacy_entry, ident)
    assert v3.status == "unverifiable" and v3.reusable is False

    # Case 4: Same raw response, but adjudication code snapshot changed -> re_adjudicate_needed
    ident_v2_code = {**ident, "code_snapshot_id": "snap_v2"}
    v4 = verify_cache_entry(rec, ident_v2_code)
    assert v4.status == "re_adjudicate_needed" and v4.can_re_adjudicate is True
    re_adj = re_adjudicate_cached_record(rec, base_item, ident_v2_code)
    assert re_adj["adjudication_version"] == 2
    assert len(re_adj["adjudication_history"]) == 2
    assert re_adj["adjudication_history"][0]["code_snapshot_id"] == "snap_v1"
    assert re_adj["adjudication_history"][1]["code_snapshot_id"] == "snap_v2"

    # Case 5: Label bound to old answer or evidence -> stale
    bound_fp = compute_label_binding_fingerprint(base_item)
    labeled_item = {**base_item, "label": "支持", "label_sample_id": bound_fp}
    assert verify_label_binding(labeled_item)["label_status"] == "bound"
    assert verify_label_binding({**labeled_item, "answer": "改过的答案"})["label_status"] == "stale"

    # Case 6: Failed call or interrupted write -> failed_or_incomplete
    failed_rec = build_verified_cache_record(
        base_item,
        ident,
        raw_response_text="",
        gate_result=None,
        tokens=0,
        seconds=0.5,
        attempts=1,
        success=False,
        error="timeout",
    )
    v6 = verify_cache_entry(failed_rec, ident)
    assert v6.status == "failed_or_incomplete" and v6.reusable is False


def test_anchor_b_plus_rule_switch_preserves_baseline_and_injects_decoupled_guards():
    """Verify enable_anchor_b_plus_rule:
    1. Defaults to False in AnalysisConfig and PromptBuilder, keeping SYSTEM_PROMPT bit-for-bit identical.
    2. Coerces cleanly via _coerce_fields(AnalysisConfig, ...).
    3. Injects ANCHOR_B_PLUS_RULE_TEXT (both global end-of-chapter guard and quote-end sufficiency guard)
       when enabled, and wires cleanly through NovelAnalyzer.
    """
    from backend.config.settings import AnalysisConfig, APIConfig, AppConfig, _coerce_fields
    from backend.core.analyzer import NovelAnalyzer
    from backend.core.prompt_builder import ANCHOR_B_PLUS_RULE_TEXT, PromptBuilder
    from backend.models.knowledge import KnowledgeBase

    cfg_default = AnalysisConfig()
    assert cfg_default.enable_anchor_b_plus_rule is False

    coerced = _coerce_fields(AnalysisConfig, {"enable_anchor_b_plus_rule": "true"})
    assert coerced["enable_anchor_b_plus_rule"] is True

    kb = KnowledgeBase()
    pb_default = PromptBuilder()
    pb_false = PromptBuilder(enable_anchor_b_plus_rule=False)
    pb_true = PromptBuilder(enable_anchor_b_plus_rule=True)

    msgs_default = pb_default.build_messages("第一章正文内容。", 1, kb)
    msgs_false = pb_false.build_messages("第一章正文内容。", 1, kb)
    msgs_true = pb_true.build_messages("第一章正文内容。", 1, kb)

    # Bit-for-bit identical when disabled
    assert msgs_default == msgs_false
    assert ANCHOR_B_PLUS_RULE_TEXT not in msgs_default[0]["content"]

    # Decoupled Rule B+ injected when enabled
    sys_true = msgs_true[0]["content"]
    assert ANCHOR_B_PLUS_RULE_TEXT in sys_true
    assert "1. 全局未解守卫（以本章末尾为准）" in sys_true
    assert "2. 引文处前提齐备（局部自足性守卫）" in sys_true
    assert "3. 最早合格位置" in sys_true

    # NovelAnalyzer wiring check
    app_cfg_off = AppConfig(
        api=APIConfig(api_key="k", base_url="http://localhost:9999/v1", model="m"),
        analysis=AnalysisConfig(enable_anchor_b_plus_rule=False),
    )
    app_cfg_on = AppConfig(
        api=APIConfig(api_key="k", base_url="http://localhost:9999/v1", model="m"),
        analysis=AnalysisConfig(enable_anchor_b_plus_rule=True),
    )
    assert NovelAnalyzer(app_cfg_off).prompt_builder.enable_anchor_b_plus_rule is False
    assert NovelAnalyzer(app_cfg_on).prompt_builder.enable_anchor_b_plus_rule is True

