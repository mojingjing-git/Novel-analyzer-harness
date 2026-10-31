"""采集契约：把"细节"和"问题"分开（2026-11-01）。

旧契约要求 `implication` **预测具体未来事件**，于是模型稳定产出剧情预测
（"未来将成就侠侣姻缘"）——那是没有答案的推演，复检问它"后文回答了吗"必然只能答"证据不足"。
新契约把一件伏笔拆成三问：出现了什么（clue）、留下什么待交代的事（question）、
后文出现什么才算回答（answer_criteria）。
"""

from __future__ import annotations

import json
from dataclasses import asdict

from backend.core.anchor import mark_leads
from backend.models.analysis_result import AnalysisResult, Foreshadowing
from backend.utils.foreshadow_ledger import SCHEMA_VERSION, ForeshadowItem, ForeshadowLedger
from backend.utils.text_utils import deduplicate_foreshadows


def test_from_dict_parses_question_and_criteria():
    payload = {
        "chapter_number": 12,
        "foreshadowing": [
            {
                "clue": "铜匣底下那层是空的",
                "question": "谁先动过匣底",
                "answer_criteria": "后文明确交代动匣底的人或动机",
                "anchor_quote": "铜匣底下那层是空的。",
            }
        ],
    }
    result = AnalysisResult.from_dict(payload)
    fs = result.foreshadowing[0]
    assert fs.question == "谁先动过匣底"
    assert fs.answer_criteria == "后文明确交代动匣底的人或动机"
    assert fs.lead is False


def test_missing_contract_marks_lead():
    only_detail = Foreshadowing(clue="丁家掌管芦花荡船运")
    partial = Foreshadowing(clue="婚事", question="这桩婚事最终如何？")
    assert mark_leads([only_detail, partial]) == 1
    assert only_detail.lead is True
    # 只要给出了一边，就不算"没有可关闭事项"——不能因为答案写得糙就当成线索
    assert partial.lead is False


def test_lead_is_not_a_word_form_filter():
    """刻意不按"有没有疑问词"筛：语义资格判不了，只判契约是否提供了可关闭事项。"""
    speculative = Foreshadowing(
        clue="丁母呼展爷为贤侄",
        question="这桩婚事最终是否成立",
        answer_criteria="明确交代婚事成立、取消或其他结果",
    )
    mark_leads([speculative])
    assert speculative.lead is False


def test_result_roundtrip_keeps_new_fields():
    result = AnalysisResult(
        chapter_number=12,
        foreshadowing=[
            Foreshadowing(clue="细节", question="问题", answer_criteria="判据")
        ],
    )
    again = AnalysisResult.from_dict(result.to_dict())
    assert again.foreshadowing[0].question == "问题"
    assert again.foreshadowing[0].answer_criteria == "判据"


def test_dedup_carries_question_and_criteria():
    catalog = deduplicate_foreshadows(
        [
            (
                12,
                "铜匣底下那层是空的",
                "异常",
                "中",
                "高",
                "铜匣底下那层是空的。",
                12,
                "铜匣",
                "谁先动过匣底",
                "后文明确交代动匣底的人",
            )
        ]
    )
    assert catalog[0]["question"] == "谁先动过匣底"
    assert catalog[0]["answer_criteria"] == "后文明确交代动匣底的人"


def test_ledger_v5_holds_contract_fields(tmp_path):
    assert SCHEMA_VERSION == 5
    item = ForeshadowItem(
        id="fs_001",
        description="铜匣",
        first_seen_chapter=12,
        first_seen_batch=1,
        last_seen_chapter=12,
        last_seen_batch=1,
        question="谁先动过匣底",
        answer_criteria="后文明确交代动匣底的人",
    )
    ledger = ForeshadowLedger()
    ledger.total_chapters = 100
    ledger.items = [item]
    path = tmp_path / "ledger.json"
    ledger.save(path)

    loaded = ForeshadowLedger.load(path)
    assert loaded.items[0].question == "谁先动过匣底"
    assert loaded.items[0].answer_criteria == "后文明确交代动匣底的人"
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 5


def test_ledger_v4_payload_still_loads(tmp_path):
    path = tmp_path / "v4.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "total_chapters": 100,
                "items": [
                    {
                        "id": "fs_001",
                        "description": "v4 条目",
                        "first_seen_chapter": 1,
                        "first_seen_batch": 0,
                        "last_seen_chapter": 2,
                        "last_seen_batch": 0,
                        "anchor_quote": "旧锚点",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = ForeshadowLedger.load(path)
    assert loaded.items[0].anchor_quote == "旧锚点"
    assert loaded.items[0].question == ""
    assert loaded.items[0].lead is False


def test_asdict_roundtrip_includes_lead():
    item = ForeshadowItem(
        id="fs_002",
        description="线索",
        first_seen_chapter=1,
        first_seen_batch=0,
        last_seen_chapter=1,
        last_seen_batch=0,
        lead=True,
    )
    assert ForeshadowItem.from_dict(asdict(item)).lead is True


async def test_recheck_skips_leads_without_calling_model():
    """线索不进复检队列——它本来就不是"待核实但没查"，而是"没有可核实的问题"。"""
    from backend.services.recheck_v2 import run_recheck_v2

    lead = ForeshadowItem(
        id="fs_lead",
        description="丁家掌管芦花荡船运",
        first_seen_chapter=1,
        first_seen_batch=0,
        last_seen_chapter=1,
        last_seen_batch=0,
        anchor_quote="丁家掌管芦花荡船运。",
        anchor_chapter=1,
        lead=True,
    )
    no_anchor = ForeshadowItem(
        id="fs_noanchor",
        description="没有锚点",
        first_seen_chapter=1,
        first_seen_batch=0,
        last_seen_chapter=1,
        last_seen_batch=0,
    )
    calls: list[str] = []

    async def call_model(prompt: str) -> str:  # pragma: no cover - 不该被调用
        calls.append(prompt)
        return "{}"

    outcome = await run_recheck_v2(
        [lead, no_anchor], book_text="丁家掌管芦花荡船运。", call_model=call_model, max_calls=1
    )

    assert outcome.leads_skipped == 1
    assert calls == []
    assert "fs_lead" not in outcome.not_checked
    assert "fs_noanchor" in outcome.not_checked
