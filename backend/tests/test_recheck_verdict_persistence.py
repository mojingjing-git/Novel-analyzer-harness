"""逐条判定落盘：`RecheckOutcome.detail()` 的契约。

为什么要单独钉这一层：`summary()` 只有计数，事后审计就只剩"它说判了 19 条
SUPPORTED"，查不了"每条是拿哪句话判的"。而 `summary()` 本身**不能**塞明细——
它会进进度事件（每次 emit 广播一次）和报告快照，48 条候选的全文引文挂在里面
会把两者撑坏。所以明细必须走单独的出口，并且两条出口的边界要固定下来。
"""

from __future__ import annotations

import json

from backend.services.recheck_v2 import RecheckOutcome
from backend.vnext.resolution import INSUFFICIENT, SUPPORTED, CandidateVerdict


def _outcome() -> RecheckOutcome:
    outcome = RecheckOutcome()
    outcome.calls = 2
    outcome.status = "COMPLETED"
    outcome.coverage = {"candidates_total": 2, "checked": 2, "supported": 1, "insufficient": 1}
    outcome.verdicts = [
        CandidateVerdict(
            "fs_001",
            SUPPORTED,
            answer="铜匣里是半张地图。",
            evidence_id="abc123",
            supporting_span=(0, 12),
            reason="",
            located_span=(31, 38),
            reported_span=(0, 12),
            quote_hash="e6e898308840",
            quote="铜匣里只有半张残破的地图",
            counter_search_performed=True,
        ),
        CandidateVerdict("fs_002", INSUFFICIENT, answer=""),
    ]
    return outcome


def test_detail_keeps_every_summary_key_and_adds_verdicts():
    outcome = _outcome()
    summary = outcome.summary()
    detail = outcome.detail()

    assert "verdicts" not in summary, "summary() 会进进度事件，不能带明细"
    assert set(detail) == set(summary) | {"verdicts", "locate_records"}
    # locate_records 在顶层：没进入候选的掉队条目没有 verdict 实例，
    # 它们的定位信息只能落在这里（AGY 复审 P1-3）
    assert {k: v for k, v in detail.items() if k not in ("verdicts", "locate_records")} == summary


def test_detail_verdicts_are_the_candidate_to_dict_payloads():
    outcome = _outcome()
    detail = outcome.detail()

    assert detail["verdicts"] == [v.to_dict() for v in outcome.verdicts]
    first = detail["verdicts"][0]
    assert first["candidate_id"] == "fs_001"
    assert first["verdict"] == SUPPORTED
    # 审计入口：引文原文与程序定位的位置都要落盘
    assert first["quote"] == "铜匣里只有半张残破的地图"
    assert first["located_span"] == [31, 38]
    assert first["quote_hash"] == "e6e898308840"


def test_detail_is_json_serialisable_without_extra_encoding():
    payload = json.dumps(_outcome().detail(), ensure_ascii=False)
    reloaded = json.loads(payload)

    assert reloaded["verdicts"][0]["quote"] == "铜匣里只有半张残破的地图"
    assert "\\u" not in payload, "中文引文不该被转义——那是审计要逐字比对的原文"


def test_empty_outcome_detail_has_empty_verdicts():
    detail = RecheckOutcome().detail()

    assert detail["verdicts"] == []
    assert detail["status"] == "NOT_RUN"
    assert detail["calls"] == 0
