"""复检的两道"别再白跑一遍"护栏。

背景（2026-11-02 事故）：整本复检 280 次调用全部被官方端点以
``400 unknown model 'codex/gpt-6-luna'`` 拒绝，但流式路径把它变成
``success=False, content=""`` 而**不抛异常**，调用方只取 ``content``——
于是 ``failed_batches=0``、``checked=0``，整轮被标成普通 ``PARTIAL``，
跑了 28 分钟没人知道白跑。

这里钉住三件事：

1. 回包不可解析 → 记为"0 判定落账"，状态降 ``PARTIAL``、原因说清、留原始样本；
2. 调用直接失败 → 进 ``failed_batches``，不再被当成"模型没答"；
3. 失败之外，正常的"围栏 + 逗号串"回包仍然能解析出判定（别把门禁修成筛子）。
"""

from __future__ import annotations

import json
import re

import pytest

from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem

CHAPTER_TEXT = (
    "第一章 起\n"
    "方醒打开铜匣，里面只有一张旧契。\n"
    "第二章 承\n"
    "旧契上写着三年后归还城南米铺。\n"
    "第三章 转\n"
    "三年期满，米铺早已换了东家。\n"
)

EVIDENCE_RE = re.compile(r"evidence_id=([0-9a-f]{12})")


def _item(ident: str = "fs_1") -> ForeshadowItem:
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


@pytest.mark.asyncio
async def test_unparsable_reply_is_reported_as_zero_verdict():
    """模型回了一篇散文而不是 JSON：不能只是"没核对"，要留下取证材料。"""

    async def _call(_prompt: str) -> str:
        return "抱歉，这条伏笔我判断不了，原文里没有交代。"

    outcome = await run_recheck_v2(
        [_item()], book_text=CHAPTER_TEXT, call_model=_call, max_calls=5, batch_size=1
    )

    assert outcome.calls > 0
    assert outcome.coverage["checked"] == 0
    assert outcome.status == "PARTIAL"
    assert "未返回可解析判定" in outcome.status_reason
    # 原始回包必须被留下来，否则事后只能看到"0 判定"四个字
    assert "判断不了" in outcome.raw_sample
    assert outcome.raw_sample in outcome.detail()["raw_sample"]


@pytest.mark.asyncio
async def test_failed_calls_land_in_failed_batches():
    """调用本身失败（4xx/超时/网关拒绝）→ 记 failed_batches，而不是"没答"。"""

    async def _call(_prompt: str) -> str:
        raise RuntimeError("复检调用失败：400 unknown model")

    outcome = await run_recheck_v2(
        [_item()], book_text=CHAPTER_TEXT, call_model=_call, max_calls=5, batch_size=1
    )

    assert outcome.calls > 0
    assert outcome.failed_batches > 0
    assert outcome.coverage["checked"] == 0
    assert outcome.status == "PARTIAL"
    assert "未返回可解析判定" in outcome.status_reason


@pytest.mark.asyncio
async def test_fenced_json_with_comma_strings_still_parses():
    """围栏 + 把 checked_evidence_ids 写成逗号串：这是格式歪，不是判定失效。"""

    async def _call(prompt: str) -> str:
        eid = EVIDENCE_RE.search(prompt).group(1)
        body = {
            "candidate_id": "fs_1",
            "verdict": "SUPPORTED",
            "answer": "三年期满，米铺换了东家",
            "supporting_quote": "三年期满，米铺早已换了东家",
            "evidence_id": eid,
            # 故意写成逗号串（模型常这么写），应被 _normalize_payload 归一成数组
            "counter_search": {
                "performed": True,
                "checked_evidence_ids": eid,
                "counter_quotes": "",
            },
        }
        return "```json\n" + json.dumps([body], ensure_ascii=False) + "\n```"

    outcome = await run_recheck_v2(
        [_item()], book_text=CHAPTER_TEXT, call_model=_call, max_calls=5, batch_size=1
    )

    assert outcome.coverage["checked"] == 1
    assert outcome.coverage["supported"] == 1
    assert outcome.status == "COMPLETED"
    assert outcome.verdicts and outcome.verdicts[0].counter_search_performed is True
