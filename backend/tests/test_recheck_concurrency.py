"""并发复检：波次并行、预算先记后发、单批失败不拖垮整轮。

并发只省墙钟时间、不省 token，所以这三条都得钉住：
1. 真的并发了（在途批次 > 1），不是"看起来并行"；
2. 预算按**发出前**记账（并发下先发后记必然超支，而超支是已经花掉的钱）；
3. 某一批抛异常，其他批照常产出判定，失败批的候选落到 not_checked。
"""

from __future__ import annotations

import asyncio
import json
import re
import time

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


def _answer_all(prompt: str) -> str:
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


def test_batches_run_in_parallel():
    inflight = 0
    peak = 0
    lock = asyncio.Lock()

    async def call_model(prompt: str) -> str:
        nonlocal inflight, peak
        async with lock:
            inflight += 1
            peak = max(peak, inflight)
        await asyncio.sleep(0.05)
        async with lock:
            inflight -= 1
        return _answer_all(prompt)

    items = [_item(i) for i in range(1, 13)]
    started = time.monotonic()
    outcome = asyncio.run(
        run_recheck_v2(
            items,
            chapters=CHAPTERS,
            call_model=call_model,
            max_calls=20,
            batch_size=2,
            concurrency=4,
        )
    )
    elapsed = time.monotonic() - started
    assert len(outcome.verdicts) == 12
    assert peak > 1, f"应真的并发，实际峰值 {peak}"
    assert peak <= 4, f"并发不该超过上限，实际 {peak}"
    # 6 批 × 0.05s：串行至少 0.3s，四路并发应在 0.15s 内
    assert elapsed < 0.28, f"并发没生效，耗时 {elapsed:.3f}s"


def test_budget_is_reserved_before_dispatch():
    """max_calls 是"最多花几次"，并发下不能超支。"""

    async def call_model(prompt: str) -> str:
        await asyncio.sleep(0.02)
        return _answer_all(prompt)

    items = [_item(i) for i in range(1, 11)]
    outcome = asyncio.run(
        run_recheck_v2(
            items,
            chapters=CHAPTERS,
            call_model=call_model,
            max_calls=3,
            batch_size=1,
            concurrency=4,
        )
    )
    assert outcome.calls <= 3, f"超预算了：{outcome.calls} 次"
    assert len(outcome.not_checked) == 10 - len(outcome.verdicts)
    assert outcome.status != "COMPLETED"


def test_one_failed_batch_does_not_break_the_rest():
    calls: list[str] = []

    async def call_model(prompt: str) -> str:
        if "fs1" in prompt:
            calls.append("boom")
            raise RuntimeError("模拟某一批炸了")
        calls.append("ok")
        return _answer_all(prompt)

    items = [_item(i) for i in range(1, 5)]
    outcome = asyncio.run(
        run_recheck_v2(
            items,
            chapters=CHAPTERS,
            call_model=call_model,
            max_calls=10,
            batch_size=1,
            concurrency=4,
        )
    )
    # 其余三条照常产出判定；失败那条经补问仍失败 → not_checked
    assert len(outcome.verdicts) == 3
    assert outcome.not_checked == ["fs1"]
    assert outcome.failed_batches >= 1
    assert outcome.status != "COMPLETED"
