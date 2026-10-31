"""批量复检：多条候选一次问完，漏答的补问一次。

为什么值得单独钉：一次一条是"能跑但贵"。真实大书去重后仍有近千条候选，
每条一次调用意味着近千次调用；实测 6 条一批时模型能一次答满，
调用数降到 1/6、完成 token 降到约 1/2。

同时钉住两个曾经的误判：
- 曾经以为"模型只答一条"，其实是解析器只抽了第一个对象（`_extract_payloads`）。
- 漏答不能静默丢：必须补问一次，仍漏才记 not_checked。
"""

from __future__ import annotations

import asyncio
import json
import re

from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem


CHAPTERS = {1: "第一章\n" + "铜匣底下那层是空的。旧契上写着三年后归还。玉印被人换过。" * 4}


def _item(i: int) -> ForeshadowItem:
    quotes = ["铜匣底下那层是空的", "旧契上写着三年后归还", "玉印被人换过"]
    return ForeshadowItem(
        id=f"fs{i}",
        description=f"第{i}条",
        first_seen_chapter=1,
        first_seen_batch=0,
        last_seen_chapter=1,
        last_seen_batch=0,
        anchor_quote=quotes[(i - 1) % 3],
        anchor_chapter=1,
    )


def _answer_all(prompt: str) -> str:
    quotes = {
        "铜匣底下那层是空的": "铜匣底下那层是空的",
        "旧契上写着三年后归还": "旧契上写着三年后归还",
        "玉印被人换过": "玉印被人换过",
    }
    rows = []
    for block in prompt.split("### 候选 candidate_id=")[1:]:
        cid = block.split()[0] if block.split() else ""
        ids = re.findall(r"\[evidence_id=([0-9a-f]{12})", block)
        if not ids:
            continue
        # 用该候选自己那段的引文
        quote = next((q for q in quotes if q in block), "")
        rows.append(
            {
                "candidate_id": cid,
                "verdict": "INSUFFICIENT",
                "answer": "",
                "evidence_id": ids[0],
                "supporting_quote": quote,
                "counter_search": {
                    "performed": True,
                    "checked_evidence_ids": ids,
                    "counter_quotes": [],
                },
            }
        )
    return json.dumps(rows, ensure_ascii=False)


def test_six_candidates_take_one_call():
    prompts: list[str] = []

    async def call_model(prompt: str) -> str:
        prompts.append(prompt)
        return _answer_all(prompt)

    items = [_item(i) for i in range(1, 7)]
    outcome = asyncio.run(
        run_recheck_v2(items, chapters=CHAPTERS, call_model=call_model, max_calls=10, batch_size=6)
    )
    assert outcome.calls == 1, "6 条候选应一次问完"
    assert len(prompts[0].split("### 候选")) - 1 == 6
    assert len(outcome.verdicts) == 6
    assert not outcome.not_checked


def test_missing_answer_triggers_one_retry():
    """第一轮漏答 → 补问漏掉的那条；补问仍漏才算 not_checked。"""
    calls: list[int] = []

    async def call_model(prompt: str) -> str:
        count = len(re.findall(r"### 候选", prompt))
        calls.append(count)
        if len(calls) == 1:
            # 第一轮只答第一条（模拟模型漏答）
            first_block = prompt.split("### 候选 candidate_id=")[1]
            cid = first_block.split()[0]
            ids = re.findall(r"\[evidence_id=([0-9a-f]{12})", first_block)
            return json.dumps(
                [
                    {
                        "candidate_id": cid,
                        "verdict": "INSUFFICIENT",
                        "answer": "",
                        "evidence_id": ids[0],
                        "supporting_quote": "",
                        "counter_search": {
                            "performed": True,
                            "checked_evidence_ids": ids,
                            "counter_quotes": [],
                        },
                    }
                ],
                ensure_ascii=False,
            )
        return _answer_all(prompt)

    items = [_item(i) for i in range(1, 5)]
    outcome = asyncio.run(
        run_recheck_v2(items, chapters=CHAPTERS, call_model=call_model, max_calls=10, batch_size=4)
    )
    assert calls == [4, 3], f"第二轮应只补问漏掉的 3 条，实际 {calls}"
    assert outcome.retried_batches == 1
    assert len(outcome.verdicts) == 4
    assert not outcome.not_checked


def test_still_missing_after_retry_is_not_checked():
    async def call_model(prompt: str) -> str:
        return "抱歉，我无法完成。"

    items = [_item(i) for i in range(1, 4)]
    outcome = asyncio.run(
        run_recheck_v2(items, chapters=CHAPTERS, call_model=call_model, max_calls=10, batch_size=3)
    )
    assert outcome.calls == 2, "首轮 + 一次补问"
    assert outcome.retried_batches == 1
    assert sorted(outcome.not_checked) == ["fs1", "fs2", "fs3"]
    assert outcome.verdicts == []
