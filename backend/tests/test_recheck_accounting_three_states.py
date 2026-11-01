"""复检记账的三态：**已核对 / 终态但没结论 / 还没核对（可重试）**。

背景：16 块真实语料实测 46 个候选里有 6 条是"模型没答好"（4 条串号、
2 条引文找不到）。原来的记账把它们全塞进"未核对"，看不出是哪一类，
而且**不会再问一次**——等于白丢 13%。

现在：
- 可重试的 ``INVALID`` → 计入"未核对"，并进补问轮；
- 不可重试的 ``INVALID`` → 计入"无效"桶并带原因，不再被反复重跑；
- 四个语义判定 → "已核对"。
"""

from __future__ import annotations

import json
import re

import pytest

from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem
from backend.vnext.resolution import (
    INVALID,
    REASON_FOREIGN_ID,
    REASON_NO_VERIFIER,
    SUPPORTED,
    CandidateVerdict,
    count_verdicts,
)

CHAPTER_TEXT = (
    "第一章 起\n"
    "方醒打开铜匣，里面只有一张旧契。\n"
    "第二章 承\n"
    "旧契上写着三年后归还城南米铺。\n"
    "第三章 转\n"
    "三年期满，米铺早已换了东家。\n"
)

EVIDENCE_RE = re.compile(r"evidence_id=([0-9a-f]{12})")


def _item(ident: str) -> ForeshadowItem:
    return ForeshadowItem(
        id=ident,
        description="铜匣里的旧契",
        first_seen_chapter=2,
        first_seen_batch=0,
        last_seen_chapter=2,
        last_seen_batch=0,
        status="active",
        anchor_quote="旧契上写着三年后归还城南米铺。",
        anchor_chapter=2,
    )


def _model_returning(payloads):
    """按调用顺序回放给定的 payload 列表。

    模拟真实模型：把提示词里给出的 ``evidence_id`` 原样回填进反证清单
    （提示词要求它这么做），但 ``evidence_id`` 字段本身按用例给的值——
    有的用例就是要它串号。
    """
    seen: list[str] = []

    async def _call(prompt: str) -> str:
        seen.append(prompt)
        payload = payloads[min(len(seen) - 1, len(payloads) - 1)]
        body = dict(payload)
        match = EVIDENCE_RE.search(prompt)
        if match:
            eid = match.group(1)
            body.setdefault("evidence_id", eid)
            counter = dict(body.get("counter_search") or {})
            counter.setdefault("performed", True)
            counter.setdefault("counter_quotes", [])
            counter["checked_evidence_ids"] = [eid]
            body["counter_search"] = counter
        return json.dumps([body], ensure_ascii=False)

    return _call, seen


def _conserved(coverage: dict) -> bool:
    """守恒式：候选总数 = 各桶之和（少一条就是记账漏了）。"""
    total = coverage["candidates_total"]
    return total == (
        coverage["supported"]
        + coverage["insufficient"]
        + coverage["conflict"]
        + coverage["invalid"]
        + coverage["stale"]
        + coverage["not_checked"]
    )


def test_retryable_invalid_is_counted_as_not_checked():
    """可重试的无效响应 → "未核对"，不是"无效"。"""
    row = CandidateVerdict("cd_1", INVALID, reason=REASON_FOREIGN_ID, retryable=True)

    counts = count_verdicts([row], expected_candidate_ids=["cd_1"], candidates_total=1)

    assert counts.not_checked == 1
    assert counts.invalid == 0
    assert counts.checked == 0
    assert counts.conservation_error() is None


def test_non_retryable_invalid_is_counted_as_invalid_with_reason():
    """不可重试的无效响应 → "无效"桶，并且带原因，不再被当成"还没判"。"""
    row = CandidateVerdict("cd_1", INVALID, reason=REASON_NO_VERIFIER, retryable=False)

    counts = count_verdicts([row], expected_candidate_ids=["cd_1"], candidates_total=1)

    assert counts.invalid == 1
    assert counts.invalid_by_reason == {REASON_NO_VERIFIER: 1}
    assert counts.not_checked == 0
    assert counts.checked == 0
    assert counts.conservation_error() is None


def test_foreign_id_response_is_retried_and_can_be_recovered():
    """第一轮串号（引用了别人的证据 id）→ 补问一轮 → 救回来。

    这正是 16 块实测里 4 条 ``FOREIGN_ID`` 的下场：原来它们算"已处理"，
    永远不会被再问一次。
    """
    first = {
        "candidate_id": "fs_1",
        "verdict": "SUPPORTED",
        "answer": "三年期满，米铺换了东家",
        "supporting_quote": "三年期满，米铺早已换了东家",
        "evidence_id": "ffffffffffff",  # 不属于本候选 → FOREIGN_ID
        "counter_search": {"performed": True, "checked_evidence_ids": [], "counter_quotes": []},
    }
    second = {
        "candidate_id": "fs_1",
        "verdict": "SUPPORTED",
        "answer": "三年期满，米铺换了东家",
        "supporting_quote": "三年期满，米铺早已换了东家",
        "counter_search": {"performed": True, "checked_evidence_ids": [], "counter_quotes": []},
    }
    call_model, seen = _model_returning([first, second])

    outcome = __import__("asyncio").run(
        run_recheck_v2([_item("fs_1")], book_text=CHAPTER_TEXT, call_model=call_model, max_calls=5)
    )

    assert len(seen) == 2, "串号之后必须补问一次"
    assert outcome.retried_batches == 1
    # 补问后的结论以最新一条为准，不能留下两行（否则会被算成"重复候选"）
    assert len(outcome.verdicts) == 1
    assert outcome.verdicts[0].verdict == SUPPORTED
    assert outcome.verdicts[0].checked is True
    assert outcome.coverage["not_checked"] == 0
    assert outcome.coverage["invalid"] == 0
    assert _conserved(outcome.coverage)


def test_unrecoverable_invalid_stays_not_checked_without_retry():
    """补问也救不回来的（模型连续串号）→ 如实记"未核对"，不假装核对过。"""
    bad = {
        "candidate_id": "fs_1",
        "verdict": "SUPPORTED",
        "answer": "随便写的",
        "supporting_quote": "这句话原文里根本没有出现过哦",
        "evidence_id": "ffffffffffff",
        "counter_search": {"performed": True, "checked_evidence_ids": [], "counter_quotes": []},
    }
    call_model, seen = _model_returning([bad])

    outcome = __import__("asyncio").run(
        run_recheck_v2([_item("fs_1")], book_text=CHAPTER_TEXT, call_model=call_model, max_calls=5)
    )

    assert len(seen) == 2, "第一轮失败后应补问一次"
    assert outcome.coverage["checked"] == 0
    assert outcome.coverage["not_checked"] == 1
    assert outcome.coverage["invalid"] == 0
    assert _conserved(outcome.coverage)


def test_finished_run_with_unchecked_candidate_is_partial_not_running():
    """跑完了但有候选没判定出结论 → ``PARTIAL``，不是 ``RUNNING``。

    真实书上这是常态（8 块探针里就有 1 条如此）。原来传 ``finished=not pending``，
    于是这种状态永远显示成"运行中"——看板和报告都以为这轮还没结束。
    """
    async def call_model(prompt: str) -> str:
        return "抱歉，我无法完成。"

    outcome = __import__("asyncio").run(
        run_recheck_v2([_item("fs_1")], book_text=CHAPTER_TEXT, call_model=call_model, max_calls=5)
    )

    assert outcome.status == "PARTIAL", f"状态不该是 {outcome.status}"
    assert outcome.status != "RUNNING"
    assert outcome.coverage["not_checked"] == 1
    assert "未核对" in outcome.status_reason
