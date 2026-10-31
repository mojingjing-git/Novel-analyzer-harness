"""伏笔复检 V2 的服务层：从账本条目到状态写回。

守三条底线：
1. 没有锚点 = 没有证据 → 记"未核对"，不许判回收；
2. 模型说 SUPPORTED 但引文在原文里找不到 → 不算；
3. 预算用尽 → 明确报"未核对"，不静默当作通过。
"""

from __future__ import annotations

import json
import re

import pytest

EVIDENCE_RE = re.compile(r"evidence_id=([0-9a-f]{12})")

from backend.services.recheck_v2 import (
    AUTO_RESOLVED,
    PROPOSED_RESOLVED,
    run_recheck_v2,
)
from backend.utils.foreshadow_ledger import ForeshadowItem

CHAPTER_TEXT = (
    "第一章 起\n"
    "方醒打开铜匣，里面只有一张旧契。\n"
    "第二章 承\n"
    "旧契上写着三年后归还城南米铺。\n"
    "第三章 转\n"
    "三年期满，米铺早已换了东家。\n"
)


def _item(ident: str, quote: str, chapter: int, description: str = "铜匣里的旧契") -> ForeshadowItem:
    return ForeshadowItem(
        id=ident,
        description=description,
        first_seen_chapter=chapter,
        first_seen_batch=0,
        last_seen_chapter=chapter,
        last_seen_batch=0,
        status="active",
        anchor_quote=quote,
        anchor_chapter=chapter,
    )


def _model_once(payload):
    """模拟模型：把提示词里给出的 evidence_id 原样回填（真实模型也被要求这么做）。"""
    calls: list[str] = []

    async def _call(prompt: str) -> str:
        calls.append(prompt)
        match = EVIDENCE_RE.search(prompt)
        body = dict(payload)
        if match:
            eid = match.group(1)
            body.setdefault("evidence_id", eid)
            counter = dict(body.get("counter_search") or {})
            counter["checked_evidence_ids"] = [eid]
            body["counter_search"] = counter
        return json.dumps(body, ensure_ascii=False)

    _call.calls = calls  # type: ignore[attr-defined]
    return _call


@pytest.mark.asyncio
async def test_supported_with_located_quote_marks_resolved():
    item = _item("fs_001", "旧契上写着三年后归还城南米铺。", 2)
    model = _model_once(
        {
            "candidate_id": "fs_001",
            "verdict": "SUPPORTED",
            "answer": "后文交代了旧契的归还承诺",
            "supporting_quote": "三年期满，米铺早已换了东家。",
            "counter_search": {"performed": True, "checked_evidence_ids": [], "counter_quotes": []},
        }
    )
    outcome = await run_recheck_v2([item], book_text=CHAPTER_TEXT, call_model=model)

    assert outcome.calls == 1
    assert item.status == "resolved"
    assert item.source_type == "auto_recheck"
    assert item.needs_review is False
    assert item.resolution == "后文交代了旧契的归还承诺"
    assert outcome.applied["fs_001"] == AUTO_RESOLVED


@pytest.mark.asyncio
async def test_insufficient_leaves_item_open_but_flags_review():
    item = _item("fs_002", "旧契上写着三年后归还城南米铺。", 2)
    model = _model_once(
        {
            "candidate_id": "fs_002",
            "verdict": "INSUFFICIENT",
            "answer": "",
            "material_insufficient_reason": "原文没有交代归还结果",
            "counter_search": {"performed": True, "checked_evidence_ids": [], "counter_quotes": []},
        }
    )
    outcome = await run_recheck_v2([item], book_text=CHAPTER_TEXT, call_model=model)

    assert item.status == "active"  # 没有被判回收
    assert item.needs_review is True
    assert outcome.applied["fs_002"] == PROPOSED_RESOLVED


@pytest.mark.asyncio
async def test_fabricated_supporting_quote_is_rejected():
    item = _item("fs_003", "旧契上写着三年后归还城南米铺。", 2)
    model = _model_once(
        {
            "candidate_id": "fs_003",
            "verdict": "SUPPORTED",
            "answer": "编一个理由",
            "supporting_quote": "这句话原文里根本没有出现过哦。",
            "counter_search": {"performed": True, "checked_evidence_ids": [], "counter_quotes": []},
        }
    )
    outcome = await run_recheck_v2([item], book_text=CHAPTER_TEXT, call_model=model)

    assert item.status == "active"
    assert outcome.applied["fs_003"] != AUTO_RESOLVED
    # 编造引文属于"这次响应不可用"（retryable），所以计入 not_checked 而不是 invalid：
    # 下一轮还要再问一次。关键是它**不能**被算成已核对通过。
    assert outcome.coverage["invalid"] == 0
    assert outcome.coverage["not_checked"] == 1
    assert outcome.coverage["checked"] == 0


@pytest.mark.asyncio
async def test_counter_quotes_block_supported_verdict():
    item = _item("fs_004", "旧契上写着三年后归还城南米铺。", 2)
    model = _model_once(
        {
            "candidate_id": "fs_004",
            "verdict": "SUPPORTED",
            "answer": "想判回收",
            "supporting_quote": "三年期满，米铺早已换了东家。",
            "counter_search": {
                "performed": True,
                "checked_evidence_ids": [],
                "counter_quotes": ["换东家不等于归还"],
            },
        }
    )
    await run_recheck_v2([item], book_text=CHAPTER_TEXT, call_model=model)
    assert item.status == "active"


@pytest.mark.asyncio
async def test_item_without_anchor_is_not_checked():
    item = _item("fs_005", "", 0)
    model = _model_once({"candidate_id": "fs_005", "verdict": "SUPPORTED", "answer": "x"})
    outcome = await run_recheck_v2([item], book_text=CHAPTER_TEXT, call_model=model)

    assert outcome.calls == 0
    assert "fs_005" in outcome.not_checked
    assert item.status == "active"


@pytest.mark.asyncio
async def test_quote_not_in_book_is_not_checked():
    item = _item("fs_006", "这句锚点根本不在正文里而且足够长", 2)
    model = _model_once({"candidate_id": "fs_006", "verdict": "SUPPORTED", "answer": "x"})
    outcome = await run_recheck_v2([item], book_text=CHAPTER_TEXT, call_model=model)

    assert outcome.calls == 0
    assert "fs_006" in outcome.not_checked


@pytest.mark.asyncio
async def test_budget_exhausted_reports_not_checked():
    items = [
        _item("fs_010", "旧契上写着三年后归还城南米铺。", 2),
        _item("fs_011", "三年期满，米铺早已换了东家。", 3),
    ]
    model = _model_once({"candidate_id": "fs_010", "verdict": "INSUFFICIENT"})
    outcome = await run_recheck_v2(
        items, book_text=CHAPTER_TEXT, call_model=model, max_calls=0, batch_size=1
    )

    assert outcome.calls == 0
    assert set(outcome.not_checked) == {"fs_010", "fs_011"}
    assert outcome.status != "COMPLETED"


@pytest.mark.asyncio
async def test_garbage_response_is_counted_not_swallowed():
    item = _item("fs_012", "旧契上写着三年后归还城南米铺。", 2)

    async def _call(prompt: str) -> str:
        return "抱歉，我无法完成这个任务。"

    outcome = await run_recheck_v2([item], book_text=CHAPTER_TEXT, call_model=_call)

    assert outcome.calls == 1
    assert "fs_012" in outcome.not_checked
    assert item.status == "active"


@pytest.mark.asyncio
async def test_coverage_conservation_holds():
    items = [
        _item("fs_020", "旧契上写着三年后归还城南米铺。", 2),
        _item("fs_021", "三年期满，米铺早已换了东家。", 3),
    ]

    async def _call(prompt: str) -> str:
        eid = EVIDENCE_RE.search(prompt).group(1)
        return json.dumps(
            [
                {
                    "candidate_id": "fs_020",
                    "verdict": "SUPPORTED",
                    "answer": "已归还",
                    "supporting_quote": "三年期满，米铺早已换了东家。",
                    "evidence_id": eid,
                    "counter_search": {
                        "performed": True,
                        "checked_evidence_ids": [eid],
                        "counter_quotes": [],
                    },
                }
            ],
            ensure_ascii=False,
        )

    outcome = await run_recheck_v2(items, book_text=CHAPTER_TEXT, call_model=_call)
    cov = outcome.coverage
    total = (
        cov["supported"]
        + cov["insufficient"]
        + cov["conflict"]
        + cov["invalid"]
        + cov["stale"]
        + cov["not_checked"]
    )
    assert total == cov["candidates_total"]
    assert cov["candidates_total"] == 2


@pytest.mark.asyncio
async def test_prompt_carries_schema_and_evidence_text():
    item = _item("fs_030", "旧契上写着三年后归还城南米铺。", 2)
    model = _model_once({"candidate_id": "fs_030", "verdict": "INSUFFICIENT"})
    await run_recheck_v2([item], book_text=CHAPTER_TEXT, call_model=model)

    prompt = model.calls[0]  # type: ignore[attr-defined]
    assert "candidate_id" in prompt
    assert "supporting_quote" in prompt
    assert "旧契上写着三年后归还城南米铺。" in prompt


@pytest.mark.asyncio
async def test_only_open_items_are_rechecked():
    done = _item("fs_040", "旧契上写着三年后归还城南米铺。", 2)
    done.status = "resolved"
    model = _model_once({"candidate_id": "fs_040", "verdict": "SUPPORTED"})
    outcome = await run_recheck_v2([done], book_text=CHAPTER_TEXT, call_model=model)
    assert outcome.calls == 0
    assert outcome.status == "COMPLETED"
