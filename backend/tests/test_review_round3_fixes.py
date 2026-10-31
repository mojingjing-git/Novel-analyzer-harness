"""复审四：章号来源、证据取窗、提示词三问、回收章号、覆盖可追溯。

这五条都属于"跑起来不报错、但结论会错"的缺陷，所以用例直接钉**位置与数值**，
不钉"函数被调用过"。
"""

from __future__ import annotations

import asyncio
import json

from backend.core.anchor import (
    build_chapter_spans,
    chapter_number_from_heading,
    split_block_into_chapters,
)
from backend.core.evidence import EvidenceIndex
from backend.services.recheck_v2 import (
    AUTO_RESOLVED,
    RecheckOutcome,
    _apply_state,
    run_recheck_v2,
)
from backend.utils.foreshadow_ledger import ForeshadowItem
from backend.vnext.resolution import CandidateVerdict


# ---------- 1. 章号来自标题，不来自块序号 ----------


def test_chapter_number_from_heading_variants():
    assert chapter_number_from_heading("第十三回 五鼠闹东京") == 13
    assert chapter_number_from_heading("  第 7 章 ") == 7
    assert chapter_number_from_heading("第一百零三章") == 103
    assert chapter_number_from_heading("Chapter 12") == 12
    assert chapter_number_from_heading("楔子") is None


def test_block_with_two_chapters_uses_real_numbers():
    block = "第十三回 甲\n正文A\n\n第十四回 乙\n正文B\n"
    # 块序号是 2，但块里真实是第 13、14 章
    spans = build_chapter_spans(block, first_chapter=2)
    assert [s[0] for s in spans] == [13, 14]
    chapters = split_block_into_chapters(block, first_chapter=2)
    assert set(chapters) == {13, 14}
    assert "正文B" in chapters[14]


# ---------- 2. 证据取窗对准真正命中的词 ----------


def test_bundle_window_centered_on_actual_hit():
    filler = "无关内容。" * 200
    index = EvidenceIndex(
        {
            1: "第一章\n铜匣底下那层是空的，到底谁先动过？" + filler,
            2: filler + "直到最后一句才提到：动过铜匣的是老三。",
        }
    )
    bundle = index.bundle_for(
        anchor_chapter=1, anchor_quote="铜匣底下那层是空的", hint_texts=["铜匣"]
    )
    # 命中词在章末：窗口必须跟过去，否则"章节选中、答案没进包"
    assert "动过铜匣的是老三" in bundle.text
    assert 2 in bundle.chapters


def test_bundle_skips_hit_chapter_without_keyword():
    filler = "无关内容。" * 200
    index = EvidenceIndex({1: "第一章\n铜匣底下那层是空的" + filler, 2: filler})
    # 第 2 章被 find_mentions 选中（关键词在章内），但取窗时找不到命中词 → 跳过并标截断
    bundle = index.bundle_for(anchor_chapter=1, anchor_quote="铜匣底下那层是空的", hint_texts=["铜匣"])
    assert bundle.spans, "至少要留下埋设处那段"
    assert all(ch != 2 for ch, _s, _e in bundle.spans)


# ---------- 3. 提示词必须带上问题与判据 ----------


def test_prompt_carries_question_and_answer_criteria():
    captured: list[str] = []

    async def call_model(prompt: str) -> str:
        captured.append(prompt)
        return json.dumps(
            {
                "candidate_id": "fs_001",
                "evidence_id": "x",
                "verdict": "INSUFFICIENT",
                "answer": "",
                "counter_search": {"performed": True, "checked_evidence_ids": ["x"]},
            },
            ensure_ascii=False,
        )

    item = ForeshadowItem(
        id="fs_001",
        description="铜匣底部有夹层",
        first_seen_chapter=1,
        first_seen_batch=0,
        last_seen_chapter=1,
        last_seen_batch=0,
        anchor_quote="铜匣底下那层是空的",
        anchor_chapter=1,
        question="谁先动过铜匣？",
        answer_criteria="后文明确交代取走夹层里东西的人",
    )
    asyncio.run(
        run_recheck_v2(
            [item],
            chapters={1: "第一章\n铜匣底下那层是空的，谁先动过？" * 5},
            call_model=call_model,
            max_calls=1,
        )
    )
    assert "待交代事项（question）：谁先动过铜匣？" in captured[0]
    assert "后文明确交代取走夹层里东西的人" in captured[0]


# ---------- 4. 回收章号取引文所在章 ----------


def test_resolved_chapter_comes_from_quote_location():
    item = ForeshadowItem(
        id="fs_002",
        description="铜匣夹层",
        first_seen_chapter=12,
        first_seen_batch=0,
        last_seen_chapter=12,
        last_seen_batch=0,
        anchor_quote="铜匣底下那层是空的",
        anchor_chapter=12,
    )
    verdict = CandidateVerdict(
        candidate_id="fs_002",
        evidence_id="ev_1",
        verdict="SUPPORTED",
        answer="动匣底的人是老三",
        located_span=(10, 30),
    )

    def chapter_of(evidence_id: str, offset: int | None) -> int | None:
        return 900 if offset == 10 else None

    _apply_state(item, AUTO_RESOLVED, [verdict], chapter_of=chapter_of)
    assert item.status == "resolved"
    # 埋设在第 12 章、证据在第 900 章：写回埋设章等于把"什么时候解决的"记错
    assert item.resolved_chapter == 900


# ---------- 5. 覆盖要能从输入追到分母 ----------


def test_coverage_tracks_input_total_and_drop_reasons():
    items = [
        ForeshadowItem(id="lead", description="背景", first_seen_chapter=1, first_seen_batch=0,
                       last_seen_chapter=1, last_seen_batch=0, lead=True),
        ForeshadowItem(id="no_anchor", description="无锚点", first_seen_chapter=1,
                       first_seen_batch=0, last_seen_chapter=1, last_seen_batch=0),
        ForeshadowItem(id="missing", description="锚点不在正文", first_seen_chapter=1,
                       first_seen_batch=0, last_seen_chapter=1, last_seen_batch=0,
                       anchor_quote="这句话正文里没有出现过", anchor_chapter=1),
    ]

    async def call_model(prompt: str) -> str:  # 不该被调用
        raise AssertionError("没有候选时不该调用模型")

    outcome = asyncio.run(
        run_recheck_v2(items, chapters={1: "第一章\n无关正文。"}, call_model=call_model, max_calls=3)
    )
    assert outcome.input_total == 3
    assert outcome.leads_skipped == 1
    assert outcome.dropped_no_anchor == 1
    assert outcome.dropped_anchor_missing == 1
    assert outcome.calls == 0
    summary = outcome.summary()
    assert summary["input_total"] == 3
    assert summary["dropped_no_anchor"] == 1
    assert summary["dropped_anchor_missing"] == 1


def test_outcome_defaults_are_explicit():
    outcome = RecheckOutcome()
    assert outcome.input_total == 0
    assert outcome.status == "NOT_RUN"
