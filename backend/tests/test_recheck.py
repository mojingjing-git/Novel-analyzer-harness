"""证据索引（EvidenceIndex）、同章多候选证据隔离与伏笔复检（recheck_v2）核心契约测试。"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

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
    assert index.find_mentions(["匣"], after=1) == [2, 3]
    assert index.find_mentions([""], after=1) == []
    index.ensure_indexed()
    assert index.find_mentions(["铜匣"], after=1) == [2, 3]
    assert index.find_mentions(["匣"], after=1) == [2, 3]
    assert index.find_mentions([""], after=1) == []
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


@pytest.mark.asyncio
async def test_t4_recheck_request_log_records_prompt_and_rebuilds_to_same_hash():
    """初始复检的请求落盘（行车记录仪）。

    落盘必须能回答"没找到 / 没送到 / 没判断对"：要存下实际发出的提示词、
    证据包与哈希，并且能用这些内容重建出同一个请求。
    同时**不得**增加模型调用次数，也不得改变判定。
    """
    from backend.common.recheck_request_log import verify_request_record

    chapters = {
        1: "第一章：他把令牌藏进石匣，匾额上写着「甲乙丙丁」四个方位。",
        2: "第二章：城主打开石匣，确认令牌确为真品。匾额的方位顺序被打乱。",
    }

    def _fresh_items():
        return [
            ForeshadowItem(
                id="fs_001",
                description="石匣令牌之谜",
                first_seen_chapter=1,
                first_seen_batch=0,
                last_seen_chapter=1,
                last_seen_batch=0,
                status="active",
                anchor_quote="他把令牌藏进石匣。",
                anchor_chapter=1,
                question="石匣里的令牌是真品还是赝品，匾额方位如何解读？",
                answer_criteria="确认令牌真伪并交代方位顺序",
            ),
            ForeshadowItem(
                id="fs_002",
                description="匾额方位",
                first_seen_chapter=1,
                first_seen_batch=0,
                last_seen_chapter=1,
                last_seen_batch=0,
                status="active",
                anchor_quote="他把令牌藏进石匣。",
                anchor_chapter=1,
                question="匾额上的四个方位分别指什么？",
                answer_criteria="交代四个方位的含义",
            ),
        ]

    def _resp(prompt: str) -> str:
        eids = re.findall(r"\[evidence_id=([a-f0-9]+)", prompt)
        return json.dumps(
            [
                {
                    "candidate_id": "fs_001",
                    "verdict": "SUPPORTED",
                    "answer": "令牌确为真品。",
                    "evidence_id": eids[-1] if eids else "",
                    "supporting_quote": "城主打开石匣，确认令牌确为真品。",
                    "counter_search": {"performed": True, "checked_evidence_ids": eids},
                }
            ],
            ensure_ascii=False,
        )

    calls: list[str] = []

    async def fake(prompt: str) -> str:
        calls.append(prompt)
        return _resp(prompt)

    base = await run_recheck_v2(_fresh_items(), chapters=chapters, call_model=fake)
    base_calls = len(calls)
    base_verdicts = [(v.candidate_id, v.verdict) for v in base.verdicts]

    calls.clear()
    log: dict = {
        "schema": "recheck_request_log_v1",
        "run": {
            "work_id": "local-sim",
            "output_dir": "local-sim",
            "code_snapshot_id": "simcode",
            "contract_version": "recheck_v2",
            "model_config": {"model": "fake"},
        },
        "requests": [],
        "usage_by_prompt_sha256": {},
    }
    logged = await run_recheck_v2(
        _fresh_items(), chapters=chapters, call_model=fake, request_log=log
    )

    assert len(calls) == base_calls
    assert [(v.candidate_id, v.verdict) for v in logged.verdicts] == base_verdicts

    requests = log["requests"]
    assert requests
    for rec in requests:
        assert rec["prompt"]
        assert rec["prompt_sha256"]
        assert rec["candidate_ids"]
        assert rec["run"]["code_snapshot_id"] == "simcode"
        assert rec["recorded_at"]
        for e in rec["evidence_batches"]:
            assert e["evidence_id"]
            assert e["text_sha256"]
        for a in rec["attempts"]:
            assert a["seconds"] is not None
            assert a["response_sha256"]
            assert "error" in a

    from backend.services import recheck_v2 as rv2

    def renderer(cands):
        rendered = "".join(
            rv2.ITEM_TEMPLATE.format(
                candidate_id=c["candidate_id"],
                description=c.get("description", ""),
                question=c.get("question", "") or "（未提供）",
                answer_criteria=c.get("answer_criteria", "") or "（未提供）",
                first_seen=c.get("first_seen", 0),
                last_seen=c.get("last_seen", 0),
                unit_note="",
                segment_count=len(c.get("segments") or []),
                truncation_note="",
                segments="\n".join(
                    f'[evidence_id={seg["evidence_id"]} 第{seg["chapter"]}章]\n"""\n'
                    f'{seg["text"]}\n"""'
                    for seg in (c.get("segments") or [])
                ),
            )
            for c in cands
        )
        return rv2.PROMPT_TEMPLATE.format(
            count=len(cands),
            items=rendered,
            schema=rv2.render_recheck_schema(),
            candidate_ids=", ".join(c["candidate_id"] for c in cands),
        )

    for rec in requests:
        v = verify_request_record(rec, renderer)
        assert v["matched"] is True, v


@pytest.mark.asyncio
async def test_t1_foreign_evidence_id_or_missing_quote_rejected_with_specific_error():
    """Citing foreign candidate evidence_id is rejected with FOREIGN_ID without false resolve."""
    chapters = {
        1: "第一章：他把令牌藏进石匣。",
        2: "第二章：城主确认令牌为真品。",
    }
    item = ForeshadowItem(
        id="fs_001",
        description="石匣令牌",
        first_seen_chapter=1,
        first_seen_batch=0,
        last_seen_chapter=1,
        last_seen_batch=0,
        status="active",
        anchor_quote="他把令牌藏进石匣。",
        anchor_chapter=1,
        question="令牌是否为真品？",
        answer_criteria="确认令牌真伪",
    )

    async def model_foreign_eid(_prompt: str) -> str:
        return json.dumps(
            [
                {
                    "candidate_id": "fs_001",
                    "verdict": "SUPPORTED",
                    "answer": "令牌为真品。",
                    "evidence_id": "foreign_eid_999",
                    "supporting_quote": "城主确认令牌为真品。",
                    "counter_search": {"performed": True, "checked_evidence_ids": ["foreign_eid_999"]},
                }
            ],
            ensure_ascii=False,
        )

    out_for = await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=model_foreign_eid,
    )
    assert out_for.verdicts[0].verdict == "INVALID"
    assert out_for.verdicts[0].reason == "FOREIGN_ID"
    assert item.status == "active"


def test_b2_bundle_for_nominated_chapters_and_density_window():
    chapters = {
        1: "【第1章】他把一枚玄铁令牌藏进石匣。",
        50: "【第50章】众人在客栈里随口聊起江湖传闻。" + ("无关闲聊内容。" * 80) + "有人提到玄铁令牌现世，而且石匣已经被打开，真相大白。",
        88: "【第88章】城主终于亮出那枚信物，揭开了二十年前的血案真相。",
    }
    index = EvidenceIndex(chapters)
    b_legacy = index.bundle_for(
        anchor_chapter=1,
        anchor_quote="他把一枚玄铁令牌藏进石匣。",
        hint_texts=["玄铁令牌的下落是什么？"],
        radius=40,
    )
    assert 88 not in b_legacy.chapters

    b_fixed = index.bundle_for(
        anchor_chapter=1,
        anchor_quote="他把一枚玄铁令牌藏进石匣。",
        hint_texts=["玄铁令牌的下落是什么？", "石匣被打开"],
        nominated_chapters=[88],
        density_window=True,
        radius=40,
    )
    assert 88 in b_fixed.chapters
    assert "二十年前的血案真相" in b_fixed.text
    assert "真相大白" in b_fixed.text


def test_recheck_resolution_gates():
    from backend.services.recheck_resolution import (
        INSUFFICIENT,
        INVALID,
        REASON_NO_COUNTER_SEARCH,
        REASON_QUOTE_NOT_FOUND,
        SUPPORTED,
        locate_quote,
        parse_recheck,
    )

    text = "书页泛黄。王冠早已熔成金锭，此事千真万确。此后无人再提。"
    quote = "王冠早已熔成金锭，此事千真万确"
    span = (text.index(quote), text.index(quote) + len(quote))

    def _parse(over: dict):
        payload = {
            "candidate_id": "cd_1",
            "evidence_id": "ev_1",
            "verdict": "SUPPORTED",
            "supporting_quote": quote,
            "supporting_span": list(span),
            "answer": "王冠已被熔毁",
            "counter_search": {"performed": True, "checked_evidence_ids": ["ev_1"], "counter_quotes": []},
            "material_insufficient_reason": None,
        }
        payload.update(over)
        return parse_recheck(
            payload,
            allowed_candidate_ids={"cd_1"},
            allowed_evidence_ids={"ev_1"},
            locate=lambda q, eid: locate_quote(q, text, reported=None),
            evidence_text=lambda eid: text,
        )

    v_ok = _parse({})
    assert v_ok.verdict == SUPPORTED and v_ok.located_span == span

    v_bad_quote = _parse({"supporting_quote": "原文根本不存在的一段长引文内容"})
    assert v_bad_quote.verdict == INVALID and v_bad_quote.reason == REASON_QUOTE_NOT_FOUND

    v_no_counter = _parse({"counter_search": {"performed": False, "checked_evidence_ids": [], "counter_quotes": []}})
    assert v_no_counter.verdict == INSUFFICIENT and v_no_counter.reason == REASON_NO_COUNTER_SEARCH
