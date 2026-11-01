"""两条复检路径的**逐条判定落盘**契约。

背景：跑完一本长书之后，如果产物里只剩"SUPPORTED 19 / INSUFFICIENT 20"这种计数，
判定质量就只能靠猜——不知道判的是哪一条、依据是哪句原文、模型自报的位置对不对。
两条路径都必须把逐条结果写成文件：

- V2（带证据复检）：`recheck_v2.py` 的 `CandidateVerdict.to_dict()` 全字段
- 旧路径（全书复检）：模型回包的 `reconciliation` 逐条收据

写失败不许把整轮总结带崩：明细是审计材料，不是复检本身的成败判据。
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.services.final_summary import (
    LEGACY_RECHECK_DETAIL_FILENAME,
    FinalSummaryRunner,
    _recheck_progress_message,
)
from backend.services.recheck_v2 import (
    RECHECK_DETAIL_FILENAME,
    RecheckOutcome,
    dump_recheck_detail,
)
from backend.vnext.resolution import INSUFFICIENT, SUPPORTED, CandidateVerdict


def _outcome() -> RecheckOutcome:
    outcome = RecheckOutcome()
    outcome.status = "PARTIAL"
    outcome.status_reason = "存在未核对候选"
    outcome.calls = 3
    outcome.not_checked = ["fs_003"]
    outcome.dropped_ids = ["fs_004"]
    outcome.applied = {"fs_001": "AUTO_RESOLVED"}
    outcome.coverage = {"candidates_total": 3, "checked": 2, "supported": 1, "insufficient": 1}
    outcome.verdicts = [
        CandidateVerdict(
            "fs_001",
            SUPPORTED,
            answer="钥匙在阿罗手里。",
            evidence_id="abc123",
            supporting_span=(0, 12),
            located_span=(31, 38),
            reported_span=(0, 12),
            quote_hash="e6e898308840",
            quote="钥匙交给了阿罗",
            counter_search_performed=True,
        ),
        CandidateVerdict("fs_002", INSUFFICIENT, answer=""),
    ]
    return outcome


def _runner(tmp_path: Path) -> FinalSummaryRunner:
    """造一个只带必要属性的 runner：绕开 __init__（它会建 LLMClient、探书名）。"""
    runner = object.__new__(FinalSummaryRunner)
    runner.output_dir = tmp_path
    runner.config = type("Cfg", (), {"api": type("Api", (), {"model": "m", "base_url": "u"})()})()
    return runner


def test_dump_recheck_detail_carries_every_verdict_field():
    payload = dump_recheck_detail(_outcome(), model="m", base_url="u")

    assert payload["model"] == "m"
    assert payload["base_url"] == "u"
    assert payload["status"] == "PARTIAL"
    assert payload["status_reason"] == "存在未核对候选"
    assert payload["candidates_total"] == 3
    assert payload["not_checked"] == ["fs_003"]
    assert payload["dropped_ids"] == ["fs_004"]
    assert payload["applied"] == {"fs_001": "AUTO_RESOLVED"}
    assert len(payload["verdicts"]) == 2

    first = payload["verdicts"][0]
    assert first["candidate_id"] == "fs_001"
    assert first["verdict"] == SUPPORTED
    assert first["answer"] == "钥匙在阿罗手里。"
    assert first["quote"] == "钥匙交给了阿罗"
    assert first["quote_hash"] == "e6e898308840"
    assert first["located_span"] == [31, 38]
    assert first["reported_span"] == [0, 12]
    assert first["counter_search_performed"] is True


def test_dump_recheck_detail_is_json_serialisable():
    text = json.dumps(dump_recheck_detail(_outcome()), ensure_ascii=False)

    assert "钥匙交给了阿罗" in text
    assert "\\u" not in text


def test_persist_v2_detail_writes_file_and_returns_path(tmp_path: Path):
    runner = _runner(tmp_path)

    path = runner._persist_recheck_detail(_outcome())

    assert path is not None
    assert path == tmp_path / RECHECK_DETAIL_FILENAME
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["verdicts"][0]["quote"] == "钥匙交给了阿罗"
    assert saved["candidates_total"] == 3


def test_persist_v2_detail_failure_does_not_raise(tmp_path: Path):
    """落盘失败只返回 None，不抛——写不出去不该把整轮总结带崩。"""
    runner = _runner(tmp_path)
    # 让 output_dir 指向一个不存在的目录，safe_save_json 会失败并返回 False
    runner.output_dir = tmp_path / "no-such-dir"

    assert runner._persist_recheck_detail(_outcome()) is None


def test_persist_legacy_detail_writes_every_row(tmp_path: Path):
    runner = _runner(tmp_path)
    rows = [
        {"batch_index": 0, "foreshadow_id": "fs_001", "is_resolved_in_this_batch": True,
         "resolution_summary": "第 9 章回收", "resolved_chapter": 9, "confidence": "高"},
        {"batch_index": 1, "foreshadow_id": "fs_002", "is_resolved_in_this_batch": False,
         "reason": "未提到", "confidence": "低"},
    ]

    path = runner._persist_legacy_recheck_detail(rows)

    assert path is not None
    assert path == tmp_path / LEGACY_RECHECK_DETAIL_FILENAME
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["items_total"] == 2
    assert saved["path"] == "legacy_global_recheck"
    assert saved["items"][0]["foreshadow_id"] == "fs_001"
    assert saved["items"][0]["resolution_summary"] == "第 9 章回收"
    assert saved["items"][1]["is_resolved_in_this_batch"] is False


def test_persist_legacy_detail_skips_when_nothing_judged(tmp_path: Path):
    runner = _runner(tmp_path)

    assert runner._persist_legacy_recheck_detail([]) is None
    assert not (tmp_path / LEGACY_RECHECK_DETAIL_FILENAME).exists()


def test_progress_message_accepts_both_emit_payloads():
    """`run_recheck_v2` 有两种 emit 载荷，消费方必须都认。

    只认 ``coverage`` 的写法在第一波进度事件上就 KeyError——而复检阶段外面没有
    try/except 兜着，于是"打开 recheck_v2"必然整轮崩。
    """
    wave = {"stage": "recheck", "calls": 1, "checked": 4}
    final = {"calls": 1, "coverage": {"checked": 6}, "status": "COMPLETED"}

    assert "4" in _recheck_progress_message(wave)
    assert "6" in _recheck_progress_message(final)
    # 两种载荷都不得抛异常
    assert _recheck_progress_message({}) == "伏笔复检：已核对 0 条"
