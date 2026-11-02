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


# ==================== 惰性构建（A）的记账契约 ====================

BOOK_TEXT = "\n\n".join(
    f"第{i}段：主角{i}号做了一件前文从未出现的事{i}，谁也没料到后续。" for i in range(1, 13)
)


def _many_items(n: int = 12):
    items = []
    for i in range(1, n + 1):
        items.append(
            ForeshadowItem(
                id=f"fs_{i:02d}",
                description=f"第{i}段的独有伏笔",
                first_seen_chapter=1,
                first_seen_batch=0,
                last_seen_chapter=1,
                last_seen_batch=0,
                status="active",
                anchor_quote=f"主角{i}号做了一件前文从未出现的事{i}",
                anchor_chapter=1,
            )
        )
    return items


async def _answer_everything(prompt: str) -> str:
    """对批内每个候选回一条合法 SUPPORTED（引文取其证据段首行前 12 字，必逐字命中）。"""
    quote_re = re.compile(r"\[evidence_id=([0-9a-f]{12})[^\n]*\]\n\"\"\"\n([^\n]{1,12})")
    payloads = []
    for block in prompt.split("### 候选 candidate_id=")[1:]:
        cid = block.split("\n", 1)[0].strip()
        m = quote_re.search(block)
        if m is None:
            continue
        eid, quote = m.group(1), m.group(2)
        payloads.append({
            "candidate_id": cid,
            "verdict": "SUPPORTED",
            "answer": "mock",
            "supporting_quote": quote,
            "evidence_id": eid,
            "counter_search": {"performed": True, "checked_evidence_ids": [eid], "counter_quotes": []},
        })
    return json.dumps(payloads, ensure_ascii=False)


@pytest.mark.asyncio
async def test_budget_exhaustion_leaves_unbuilt_in_not_checked():
    """调用预算耗尽：没建也没查的候选必须落 not_checked，定位记录仍要齐全。

    这是惰性构建（A）最容易错的地方——"没建"不能变成"凭空消失"，
    否则 coverage 守恒被打破、报告的分母悄悄变小。
    """
    outcome = await run_recheck_v2(
        _many_items(12),
        book_text=BOOK_TEXT,
        call_model=_answer_everything,
        max_calls=1,
        batch_size=6,
    )

    assert outcome.calls == 1
    assert outcome.coverage["checked"] == 6
    assert outcome.coverage["candidates_total"] == 12
    assert len(outcome.not_checked) == 6
    # 守恒：每个候选恰好落一个桶
    total = (
        outcome.coverage["supported"]
        + outcome.coverage["insufficient"]
        + outcome.coverage["conflict"]
        + outcome.coverage["invalid"]
        + outcome.coverage["stale"]
        + outcome.coverage["not_checked"]
    )
    assert total == 12
    # 骨架段（定位/记账）对全部 12 条都要完成，与"建没建证据"无关
    assert len(outcome.locate_records) == 12
    assert outcome.status == "PARTIAL"


@pytest.mark.asyncio
async def test_build_progress_events_are_emitted():
    """构建进度事件：构建期不能静默（上一轮"卡死"事故的直接教训）。"""
    events: list[dict] = []
    outcome = await run_recheck_v2(
        _many_items(12),
        book_text=BOOK_TEXT,
        call_model=_answer_everything,
        max_calls=5,
        batch_size=6,
        emit=events.append,
    )

    builds = [e for e in events if e.get("stage") == "build_candidates"]
    assert builds, "没有任何构建进度事件"
    assert builds[-1]["built"] == builds[-1]["total"] == 12
    waves = [e for e in events if e.get("stage") == "recheck"]
    assert waves, "没有任何波次进度事件"
    assert outcome.coverage["checked"] == 12
