"""锚点定位：章号只是提示，不是身份。

书里同一个章号可能重复出现——源文件里行首的"第N回"（正文引用回次）会被标题
正则当成章节边界。实测《大奉打更人》922 个源文件切出的合并块 spans 形如
``[229, 230, 1, 2]``：章号 1、2 在整本书里反复出现。

采集侧记下的 ``anchor_chapter`` 因此可能指向"另一份同号章"的文本——
实测 1669 条带锚点的候选里 **1303 条（78%）** 被记成"锚点找不到"，
复检等于只查了两成候选。

修法：先用 anchor_chapter 找；找不到就在**全书范围内**找那句引文，
再反查它属于哪一章，用那一章做证据包。
"""

from __future__ import annotations

import json
import re

import pytest

from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem

EVIDENCE_RE = re.compile(r"evidence_id=([0-9a-f]{12})")

#: 两份"第 1 回"：章号重复。真正的引文在**第二份**里。
CHAPTERS = {
    1: "第一回 甲\n这是另一处内容，与伏笔无关。\n",
    2: "第一回 乙\n方醒打开铜匣，里面只有一张旧契。\n",
    3: "第二回 丙\n三年期满，米铺早已换了东家。\n",
}


def _item(anchor_chapter: int) -> ForeshadowItem:
    return ForeshadowItem(
        id="fs_1",
        description="铜匣里的旧契",
        first_seen_chapter=anchor_chapter,
        first_seen_batch=0,
        last_seen_chapter=anchor_chapter,
        last_seen_batch=0,
        status="active",
        anchor_quote="方醒打开铜匣，里面只有一张旧契。",
        anchor_chapter=anchor_chapter,
    )


async def _model(prompt: str) -> str:
    eid = EVIDENCE_RE.search(prompt).group(1)
    return json.dumps([{
        "candidate_id": "fs_1",
        "verdict": "SUPPORTED",
        "answer": "匣里只有一张旧契",
        "material_insufficient_reason": "",
        "evidence_id": eid,
        "supporting_span": [0, 12],
        "supporting_quote": "方醒打开铜匣",
        "counter_search": {"performed": True, "checked_evidence_ids": [eid], "counter_quotes": []},
    }], ensure_ascii=False)


@pytest.mark.asyncio
async def test_anchor_found_in_book_when_chapter_number_is_wrong():
    """章号指错时仍要能定位——引文在全书里能找到就不能丢。"""
    outcome = await run_recheck_v2(
        [_item(anchor_chapter=1)], chapters=CHAPTERS, call_model=_model, max_calls=3
    )

    assert outcome.dropped_anchor_missing == 0, "章号指错不该把候选丢掉"
    assert outcome.coverage["candidates_total"] == 1
    assert len(outcome.verdicts) == 1
    assert outcome.verdicts[0].verdict == "SUPPORTED"
    # 锚点真正所在章被记下来，供审计回看
    assert outcome.resolved_chapters == {"fs_1": 2}


@pytest.mark.asyncio
async def test_quote_absent_from_whole_book_is_still_dropped():
    """引文全都不存在时，仍然要如实记"锚点找不到"——不能靠回退把假的放进来。"""
    item = ForeshadowItem(
        id="fs_9", description="不存在的引文", first_seen_chapter=1, first_seen_batch=0,
        last_seen_chapter=1, last_seen_batch=0, status="active",
        anchor_quote="这句话全书里根本没有出现过好吗", anchor_chapter=1,
    )
    outcome = await run_recheck_v2([item], chapters=CHAPTERS, call_model=_model, max_calls=3)

    assert outcome.dropped_anchor_missing == 1
    assert outcome.coverage["candidates_total"] == 0
    assert not outcome.verdicts


@pytest.mark.asyncio
async def test_correct_chapter_needs_no_book_wide_fallback():
    """章号本来就对时，不该触发全书回退（也不该动 resolved_chapters）。"""
    outcome = await run_recheck_v2(
        [_item(anchor_chapter=2)], chapters=CHAPTERS, call_model=_model, max_calls=3
    )

    assert outcome.dropped_anchor_missing == 0
    assert outcome.resolved_chapters == {}
    assert outcome.verdicts[0].verdict == "SUPPORTED"
