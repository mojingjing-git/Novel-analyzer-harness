"""S7 生产入口闭环：首次执行 → 落盘 → 重启 → 缓存复用（复审三第 1 条）。

这组用例的存在理由：零件各自绿了不代表链路成立。这里用**真实的项目目录 + 真实正文**
喂给执行入口，模型用固定桩替代，验的是"跑一次会花一次钱、第二次不花"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.vnext import project
from backend.vnext.budget import RECHECK, Budget, BudgetCap
from backend.vnext.recheck_plan import RecheckUnit
from backend.vnext.runner import (
    load_ledger,
    run_recheck,
    save_ledger,
    summarize_run,
)
from backend.vnext.tasks import SUCCEEDED, TaskLedger

CHAPTER = "第1章\n方醒从棺中坐起，四下寂静。棺外的雨声渐密。\n"


def _book(tmp_path: Path):
    book_dir = project.create_project(tmp_path / "bk", "测书", [(1, CHAPTER)], book_id="bk_1")
    return book_dir, project.load_manifest(book_dir)


def _unit(manifest) -> RecheckUnit:
    chapter = manifest.chapters[0]
    return RecheckUnit(
        entity_id="fs_1",
        candidate_ids=("cd_1",),
        inputs={"model": "m1", "chapter": chapter.chapter_id, "hash": chapter.content_hash},
    )


def _good_payload(start: int = 1, end: int = 12) -> dict:
    """一份"处处合规"的模型回包。

    引文取 ``CHAPTER[start + 3 : end + 3]``（与 ``_verifier`` 同一口径，模拟章节头偏移），
    默认区间 1..12 对应 11 个字：D-19 起引文短于 ``resolution.MIN_QUOTE_CHARS``(10)
    会被判 ``QUOTE_TOO_SHORT``，所以桩数据必须给足长度。
    """
    return {
        "candidate_id": "cd_1",
        "verdict": "SUPPORTED",
        "answer": "方醒活着",
        "evidence_id": "ev_1",
        "supporting_span": [start, end],
        "supporting_quote": CHAPTER[start + 3 : end + 3],
        "counter_search": {"performed": True, "checked_evidence_ids": ["ev_2"]},
    }


def _verifier(text: str, start: int, end: int) -> bool:
    return CHAPTER[start + 3 : end + 3] == text


def test_first_run_then_restart_reuses_without_calling(tmp_path) -> None:
    book_dir, manifest = _book(tmp_path)
    path = book_dir / "recheck_ledger.json"
    unit = _unit(manifest)
    calls = {"n": 0}

    def model(_unit):
        calls["n"] += 1
        return _good_payload()

    budget = Budget(caps={RECHECK: BudgetCap(max_calls=10, max_tokens=10_000)})
    out1 = run_recheck(
        [unit], ledger=load_ledger(path), call_model=model, budget=budget,
        verify=_verifier, allowed_evidence_ids=lambda u: {"ev_1", "ev_2"},
        persist=lambda l: save_ledger(path, l),
    )
    assert calls["n"] == 1
    assert out1.executed == ["fs_1"]
    assert path.is_file()

    # 重启：新账本从磁盘读回，预算也是新的
    reloaded = load_ledger(path)
    assert len(reloaded) == 1
    budget2 = Budget(caps={RECHECK: BudgetCap(max_calls=10, max_tokens=10_000)})
    out2 = run_recheck(
        [unit], ledger=reloaded, call_model=model, budget=budget2,
        verify=_verifier, allowed_evidence_ids=lambda u: {"ev_1", "ev_2"},
        persist=lambda l: save_ledger(path, l),
    )
    assert calls["n"] == 1, "第二轮不该再调用模型"
    assert out2.reused == ["fs_1"]
    assert budget2.used[RECHECK].calls == 0, "复用不能消耗预算"


def test_ledger_survives_restart_with_usage(tmp_path) -> None:
    book_dir, manifest = _book(tmp_path)
    path = book_dir / "recheck_ledger.json"
    budget = Budget(caps={RECHECK: BudgetCap(max_calls=10, max_tokens=10_000)})
    run_recheck(
        [_unit(manifest)], ledger=TaskLedger(), call_model=lambda u: _good_payload(),
        budget=budget, verify=_verifier, allowed_evidence_ids=lambda u: {"ev_1", "ev_2"},
        persist=lambda l: save_ledger(path, l), tokens_per_call=7,
    )
    from backend.vnext.budget import rebuild_usage

    rows = load_ledger(path).to_dicts()
    usage = rebuild_usage(rows)
    assert usage[RECHECK].calls == 1
    assert usage[RECHECK].prompt_tokens == 7      # group 字段必须能回填用量


def test_budget_is_reserved_across_units(tmp_path) -> None:
    """预算只够一次时，第二个单元必须被跳过——预留要持有到执行结束。"""
    book_dir, manifest = _book(tmp_path)
    units = [_unit(manifest), RecheckUnit("fs_2", ("cd_2",), {"model": "m1"})]
    budget = Budget(caps={RECHECK: BudgetCap(max_calls=1, max_tokens=10_000)})
    out = run_recheck(
        units, ledger=TaskLedger(), call_model=lambda u: _good_payload(), budget=budget,
        verify=_verifier, allowed_evidence_ids=lambda u: {"ev_1", "ev_2"},
    )
    assert out.executed == ["fs_1"]
    assert out.skipped == ["fs_2"]
    assert out.stopped_reason.startswith("1 个单元")


def test_model_failure_still_charges_and_records(tmp_path) -> None:
    book_dir, manifest = _book(tmp_path)
    budget = Budget(caps={RECHECK: BudgetCap(max_calls=10, max_tokens=10_000)})

    def boom(_unit):
        raise TimeoutError("网络超时")

    out = run_recheck(
        [_unit(manifest)], ledger=TaskLedger(), call_model=boom, budget=budget,
        verify=_verifier, allowed_evidence_ids=lambda u: {"ev_1", "ev_2"},
    )
    assert out.failed == ["fs_1"]
    assert budget.used[RECHECK].calls == 1          # 失败也扣
    assert out.usage.calls == 1
    assert budget.outstanding() == ()               # 预留必须结算掉，不能挂在途


def test_missing_counter_evidence_ids_is_not_support(tmp_path) -> None:
    """模型自己声明"查过反证"但给不出本任务内的 ID → 不够支持。"""
    book_dir, manifest = _book(tmp_path)
    payload = _good_payload()
    payload["counter_search"] = {"performed": True, "checked_evidence_ids": ["outside"]}
    out = run_recheck(
        [_unit(manifest)], ledger=TaskLedger(), call_model=lambda u: payload,
        verify=_verifier, allowed_evidence_ids=lambda u: {"ev_1", "ev_2"},
    )
    verdict = out.verdicts["fs_1"]
    assert verdict.verdict != "SUPPORTED"
    assert verdict.reason == "COUNTER_SEARCH_UNVERIFIED"


def test_summary_marks_skipped_run_as_incomplete(tmp_path) -> None:
    book_dir, manifest = _book(tmp_path)
    budget = Budget(caps={RECHECK: BudgetCap(max_calls=0, max_tokens=0)})
    out = run_recheck(
        [_unit(manifest)], ledger=TaskLedger(), call_model=lambda u: _good_payload(),
        budget=budget, verify=_verifier, allowed_evidence_ids=lambda u: {"ev_1", "ev_2"},
    )
    summary = summarize_run(out, candidates_total=1)
    assert summary.not_completed_reason() is not None
