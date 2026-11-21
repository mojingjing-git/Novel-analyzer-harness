"""证据索引（EvidenceIndex）、同章多候选证据隔离与伏笔复检（recheck_v2）核心契约测试。"""
from __future__ import annotations

import asyncio
import json
import re

from backend.core.evidence import EvidenceIndex
from backend.services.recheck_v2 import AUTO_RESOLVED, PROPOSED_RESOLVED, run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem

EVIDENCE_RE = re.compile(r"evidence_id=([0-9a-f]{12})")

CHAPTER_TEXT = (
    "第一章 起\n"
    "方醒打开铜匣，里面只有一张旧契。\n"
    "第二章 承\n"
    "旧契上写着三年后归还城南米铺。\n"
    "第三章 转\n"
    "三年期满，米铺早已换了东家。\n"
)


def _item(ident: str, quote: str, chapter: int = 1, description: str = "铜匣里的旧契") -> ForeshadowItem:
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


def _model_once(payload: dict):
    async def _call(prompt: str) -> str:
        match = EVIDENCE_RE.search(prompt)
        body = dict(payload)
        if match:
            eid = match.group(1)
            body.setdefault("evidence_id", eid)
            counter = dict(body.get("counter_search") or {})
            counter["checked_evidence_ids"] = [eid]
            body["counter_search"] = counter
        return json.dumps(body, ensure_ascii=False)

    return _call


def _payload(
    candidate_id: str,
    quote: str,
    verdict: str = "SUPPORTED",
    answer: str = "已交代",
    prompt: str = "",
) -> str:
    ids = re.findall(r"\[evidence_id=([0-9a-f]{12})", prompt)
    return json.dumps(
        [
            {
                "candidate_id": candidate_id,
                "verdict": verdict,
                "answer": answer,
                "evidence_id": ids[0] if ids else "",
                "supporting_quote": quote,
                "counter_search": {
                    "performed": True,
                    "checked_evidence_ids": ids,
                    "counter_quotes": [],
                },
            }
        ],
        ensure_ascii=False,
    )


def _two_anchor_chapter() -> dict[int, str]:
    filler = "无关内容。" * 300
    return {
        1: (
            "第一章\n方醒打开铜匣，铜匣底下那层是空的。"
            + filler
            + "旧契上写着三年后归还城南米铺，落款是癸卯年。"
            + filler
        )
    }


def test_evidence_index_bundle_and_mentions():
    chapters = {
        1: "第一章 起\n方醒打开铜匣，发现底下那层是空的。",
        2: "第二章 承\n街上人来人往，无人提起铜匣。",
        3: "第三章 转\n他忽然想起当年搬动铜匣的人，原来是自己。",
    }
    index = EvidenceIndex(chapters)
    assert index.find_mentions(["铜匣"], after=1) == [2, 3]
    bundle = index.bundle_for(anchor_chapter=1, anchor_quote="底下那层是空的", hint_texts=["铜匣"])
    assert "第 1 章（埋设处）" in bundle.text and "原来是自己" in bundle.text
    assert EvidenceIndex({}).bundle_for(anchor_chapter=1, anchor_quote="不存在").empty


async def test_recheck_v2_verdicts_and_anchor_guard():
    item1 = _item("fs_001", "旧契上写着三年后归还城南米铺。", 2)
    out1 = await run_recheck_v2(
        [item1],
        book_text=CHAPTER_TEXT,
        call_model=_model_once({
            "candidate_id": "fs_001",
            "verdict": "SUPPORTED",
            "answer": "后文交代了旧契的归还承诺",
            "supporting_quote": "三年期满，米铺早已换了东家。",
            "counter_search": {"performed": True, "checked_evidence_ids": [], "counter_quotes": []},
        }),
    )
    assert item1.status == "resolved" and out1.applied["fs_001"] == AUTO_RESOLVED

    item2 = _item("fs_002", "旧契上写着三年后归还城南米铺。", 2)
    out2 = await run_recheck_v2(
        [item2],
        book_text=CHAPTER_TEXT,
        call_model=_model_once({
            "candidate_id": "fs_002",
            "verdict": "INSUFFICIENT",
            "answer": "",
            "material_insufficient_reason": "原文未交代结果",
            "counter_search": {"performed": True, "checked_evidence_ids": [], "counter_quotes": []},
        }),
    )
    assert item2.status == "active" and item2.needs_review is True and out2.applied["fs_002"] == PROPOSED_RESOLVED

    item3 = _item("fs_003", "", 0)
    out3 = await run_recheck_v2([item3], book_text=CHAPTER_TEXT, call_model=_model_once({}))
    assert out3.calls == 0 and out3.not_checked == ["fs_003"]


def test_same_chapter_candidates_do_not_share_evidence_ids():
    chapters = _two_anchor_chapter()
    item_a = _item("fsA", "铜匣底下那层是空的", description="铜匣底部空了")
    item_b = _item("fsB", "旧契上写着三年后归还城南米铺", description="旧契还款期限")

    async def call_model(prompt: str) -> str:
        quotes = {
            "fsA": "铜匣底下那层是空的",
            "fsB": "旧契上写着三年后归还城南米铺",
        }
        rows = []
        for block in prompt.split("### 候选 candidate_id=")[1:]:
            cid = block.split()[0] if block.split() else ""
            ids = re.findall(r"\[evidence_id=([0-9a-f]{12})", block)
            if cid not in quotes or not ids:
                continue
            rows.append(
                {
                    "candidate_id": cid,
                    "verdict": "SUPPORTED",
                    "answer": "已交代",
                    "evidence_id": ids[0],
                    "supporting_quote": quotes[cid],
                    "counter_search": {
                        "performed": True,
                        "checked_evidence_ids": ids,
                        "counter_quotes": [],
                    },
                }
            )
        return json.dumps(rows, ensure_ascii=False)

    outcome = asyncio.run(
        run_recheck_v2([item_a, item_b], chapters=chapters, call_model=call_model, max_calls=4)
    )
    assert [v.verdict for v in outcome.verdicts] == ["SUPPORTED", "SUPPORTED"]
    assert len({v.evidence_id for v in outcome.verdicts}) == 2


def test_citing_other_candidates_text_is_rejected():
    chapters = _two_anchor_chapter()
    item_a = _item("fsA", "铜匣底下那层是空的", description="铜匣底部空了")

    async def call_model(prompt: str) -> str:
        return _payload(
            "fsA", "旧契上写着三年后归还城南米铺，落款是癸卯年。", answer="三年后归还", prompt=prompt
        )

    outcome = asyncio.run(
        run_recheck_v2([item_a], chapters=chapters, call_model=call_model, max_calls=2)
    )
    assert outcome.verdicts
    assert all(v.verdict != "SUPPORTED" for v in outcome.verdicts)


def test_status_fixup_does_not_nameerror_and_marks_partial():
    good = _item("fsG", "铜匣底下那层是空的")

    async def call_model(prompt: str) -> str:
        return _payload("fsG", "", verdict="INSUFFICIENT", answer="", prompt=prompt)

    outcome = asyncio.run(
        run_recheck_v2(
            [good, _item("fsN", "")],
            chapters={1: "第一章\n铜匣底下那层是空的。" * 3},
            call_model=call_model,
            max_calls=2,
        )
    )
    assert outcome.status == "PARTIAL"
    assert outcome.dropped_no_anchor == 1


def test_all_dropped_reports_partial_not_completed():
    async def call_model(prompt: str) -> str:
        raise AssertionError("没有候选时不该调用模型")

    outcome = asyncio.run(
        run_recheck_v2([_item("fsN", "")], chapters={1: "正文"}, call_model=call_model)
    )
    assert outcome.status == "PARTIAL"
    assert outcome.status != "COMPLETED"


def test_truncated_false_when_all_hits_fit():
    filler = "无关内容。" * 200
    chapters = {1: "第一章\n铜匣底下那层是空的" + filler}
    for ch in (3, 4, 5):
        chapters[ch] = f"第{ch}章\n有人提起铜匣，说匣底动过。" + filler
    index = EvidenceIndex(chapters)
    bundle = index.bundle_for(
        anchor_chapter=1, anchor_quote="铜匣底下那层是空的", hint_texts=["铜匣"]
    )
    assert bundle.omitted == 0
    assert bundle.truncated is False


def test_truncated_true_when_hits_exceed_windows():
    filler = "无关内容。" * 200
    chapters = {1: "第一章\n铜匣底下那层是空的" + filler}
    for ch in range(3, 9):
        chapters[ch] = f"第{ch}章\n有人提起铜匣，说匣底动过。" + filler
    index = EvidenceIndex(chapters)
    bundle = index.bundle_for(
        anchor_chapter=1, anchor_quote="铜匣底下那层是空的", hint_texts=["铜匣"]
    )
    assert bundle.truncated is True
    assert bundle.omitted > 0
