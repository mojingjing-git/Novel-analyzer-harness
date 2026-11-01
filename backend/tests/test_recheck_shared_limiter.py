"""复检复用分析阶段的在途上限：同一个信号量，而不是各建一个。

原来 pipeline 与 final_summary 各建一个 Semaphore(concurrency)，
两阶段真的并行时在途峰值能到 2× 配置值——配置写着 8，实际发 16 个请求，
这就是"配置的上限没生效"。复检现在把总结阶段的 `_llm_sem` 传进来共用。
"""

from __future__ import annotations

import asyncio
import json
import re

from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem


CHAPTERS = {1: "第一章\n" + "铜匣底下那层是空的。旧契上写着三年后归还。玉印被人换过。" * 4}
QUOTES = ["铜匣底下那层是空的", "旧契上写着三年后归还", "玉印被人换过"]


def _item(i: int) -> ForeshadowItem:
    return ForeshadowItem(
        id=f"fs{i}",
        description=f"第{i}条",
        first_seen_chapter=1,
        first_seen_batch=0,
        last_seen_chapter=1,
        last_seen_batch=0,
        anchor_quote=QUOTES[(i - 1) % 3],
        anchor_chapter=1,
    )


def _answer(prompt: str) -> str:
    rows = []
    for block in prompt.split("### 候选 candidate_id=")[1:]:
        cid = block.split()[0] if block.split() else ""
        ids = re.findall(r"\[evidence_id=([0-9a-f]{12})", block)
        if not ids:
            continue
        rows.append(
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
        )
    return json.dumps(rows, ensure_ascii=False)


def test_external_limiter_caps_inflight_even_if_concurrency_is_higher():
    """外部信号量是硬上限：concurrency=8 也不能超过 limiter=2。"""
    peak = 0
    inflight = 0
    lock = asyncio.Lock()
    shared = asyncio.Semaphore(2)

    async def call_model(prompt: str) -> str:
        nonlocal peak, inflight
        async with lock:
            inflight += 1
            peak = max(peak, inflight)
        await asyncio.sleep(0.03)
        async with lock:
            inflight -= 1
        return _answer(prompt)

    items = [_item(i) for i in range(1, 13)]
    outcome = asyncio.run(
        run_recheck_v2(
            items,
            chapters=CHAPTERS,
            call_model=call_model,
            max_calls=20,
            batch_size=1,
            concurrency=8,      # 波次宽度 8
            limiter=shared,     # 但在途硬上限 2
        )
    )
    assert len(outcome.verdicts) == 12
    assert peak <= 2, f"外部限流没生效，峰值 {peak}"


def test_shared_limiter_is_reused_across_recheck_and_other_stage():
    """模拟"复检与别的阶段同时跑"：两边共用一个信号量，总在途不超上限。"""
    shared = asyncio.Semaphore(2)
    inflight = 0
    peak = 0
    lock = asyncio.Lock()

    async def take_slot():
        nonlocal inflight, peak
        async with lock:
            inflight += 1
            peak = max(peak, inflight)

    async def drop_slot():
        nonlocal inflight
        async with lock:
            inflight -= 1

    async def call_model(prompt: str) -> str:
        await take_slot()
        try:
            await asyncio.sleep(0.02)
            return _answer(prompt)
        finally:
            await drop_slot()

    async def other_stage() -> None:
        # 另一个阶段自己也要占槽（例如风格分析）
        async with shared:
            await take_slot()
            try:
                await asyncio.sleep(0.02)
            finally:
                await drop_slot()

    async def main():
        await asyncio.gather(
            run_recheck_v2(
                [_item(i) for i in range(1, 7)],
                chapters=CHAPTERS,
                call_model=call_model,
                max_calls=20,
                batch_size=1,
                concurrency=4,
                limiter=shared,
            ),
            other_stage(),
        )

    asyncio.run(main())
    assert peak <= 2, f"共用信号量后总在途不该超过 2，实际 {peak}"
