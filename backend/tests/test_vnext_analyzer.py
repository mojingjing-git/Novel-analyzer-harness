"""B1a：单块分析。用 fake client，不触网。

守四件事：能出结果、解析失败要炸、没有引文的事实不许进库、内容变了身份就得变。
"""

from __future__ import annotations

import json

import pytest

from backend.vnext import blocks, project
from backend.vnext.analyzer import (
    PROMPT_STRATEGY_VERSION,
    analyze_block,
    build_messages,
    extracted_fingerprint,
    normalize_extracted,
    parse_block_output,
)
from backend.vnext.constants import VnextFormatError
from backend.vnext.knowledge import KnowledgeStore, build_snapshot
from backend.vnext.prompt_limits import PromptLimits
from backend.vnext.prompt_view import build_prompt_window
from backend.vnext.results import ResultStrategy, classify, list_results

CHAPTERS = [
    (1, "第一章\n方醒从棺中坐起，四下寂静。\n"),
    (2, "第二章\n他推开棺盖，雨声渐密。\n"),
]


class FakeClient:
    """按预设文本返回；记录调用次数与 messages。"""

    def __init__(self, content: str, tokens=(11, 7), usage_status="KNOWN"):
        self.content = content
        self.tokens = tokens
        self.usage_status = usage_status
        self.calls: list[list[dict]] = []

    async def chat(self, messages, temperature=0.1, max_tokens=None):  # noqa: ANN001
        self.calls.append(messages)

        class _R:
            pass

        r = _R()
        r.content = self.content
        r.tokens = self.tokens
        r.usage_status = self.usage_status
        return r


def _payload(quote_text="方醒从棺中坐起", start=3, end=None) -> str:
    end = end if end is not None else start + len(quote_text)
    return json.dumps(
        {
            "facts": [
                {
                    "fact_id": "f-1",
                    "text": "方醒醒了",
                    "quote": {"start": start, "end": end, "text": quote_text},
                }
            ],
            "characters": [
                {
                    "name": "方醒",
                    "state": "刚醒",
                    "quote": {"start": start, "end": end, "text": quote_text},
                }
            ],
        },
        ensure_ascii=False,
    )


@pytest.fixture()
def book(tmp_path):
    book_dir = project.create_project(tmp_path, "书", CHAPTERS, book_id="bk_an00000001")
    manifest = project.load_manifest(book_dir)
    plan = blocks.plan_blocks(manifest, block_size=2)[0]
    return book_dir, manifest, plan


async def _run(book, client, *, store=None, limits=None):
    book_dir, manifest, plan = book
    return await analyze_block(
        book_dir,
        plan,
        client=client,
        limits=limits or PromptLimits(),
        strategy=ResultStrategy(model_fingerprint="m1"),
        prior_result_ids=[],
        store=store,
        title=manifest.title,
    )


# ---------------------------------------------------------------------------
# 正向
# ---------------------------------------------------------------------------


async def test_analyze_publishes_verified_result(book) -> None:
    client = FakeClient(_payload())
    run = await _run(book, client)

    assert len(client.calls) == 1
    assert run.record.status == "SUCCEEDED"
    assert run.record.prompt_tokens == 11 and run.record.completion_tokens == 7
    assert run.record.usage_status == "KNOWN"
    # 引文真实 ⇒ 升级为已定位
    assert run.record.extracted["facts"][0]["verification_state"] == "EVIDENCE_LOCATED"
    # 上下文指纹非空且进依赖
    assert run.window.fingerprint()
    assert run.record.dependency_fingerprint

    book_dir, manifest, _ = book
    stored = list_results(book_dir)
    assert run.record.result_id in stored
    assert run.record.result_id in project.load_manifest(book_dir).active_results


async def test_result_is_classified_done_with_matching_strategy(book) -> None:
    await _run(book, FakeClient(_payload()))
    book_dir, manifest, plan = book
    manifest = project.load_manifest(book_dir)
    verdict = classify(
        manifest,
        [plan],
        list_results(book_dir),
        strategy=ResultStrategy(model_fingerprint="m1"),
        context_fingerprints={plan.block_signature: _window_fp(book_dir, plan)},
    )
    assert plan.block_signature in verdict.done


def _window_fp(book_dir, plan) -> str:
    manifest = project.load_manifest(book_dir)
    by_id = {c.chapter_id: c for c in manifest.chapters}
    first = min(by_id[cid].ordinal for cid in plan.chapter_ids)
    return build_prompt_window(
        KnowledgeStore(), exclusive_before=first, limits=PromptLimits()
    ).fingerprint()


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


async def test_malformed_output_raises_and_writes_nothing(book) -> None:
    client = FakeClient("我觉得这一章写得不错，但没有 JSON。")
    with pytest.raises(VnextFormatError) as exc:
        await _run(book, client)
    assert exc.value.code.value == "UNPARSEABLE_ARTIFACT"
    assert list_results(book[0]) == {}


async def test_facts_without_quote_are_dropped_not_faked(book) -> None:
    payload = json.dumps({"facts": [{"fact_id": "f-1", "text": "无引文"}], "characters": []}, ensure_ascii=False)
    run = await _run(book, FakeClient(payload))
    assert run.record.extracted["facts"] == []


async def test_fabricated_quote_stays_unverified(book) -> None:
    payload = json.dumps(
        {
            "facts": [
                {
                    "fact_id": "f-1",
                    "text": "编的",
                    "quote": {"start": 3, "end": 15, "text": "这句原文根本不存在"},
                }
            ],
            "characters": [],
        },
        ensure_ascii=False,
    )
    run = await _run(book, FakeClient(payload))
    assert run.record.extracted["facts"][0]["verification_state"] == "UNVERIFIED"


# ---------------------------------------------------------------------------
# 身份与内容
# ---------------------------------------------------------------------------


def test_content_change_changes_result_identity() -> None:
    """内容变了，身份必须变——否则重跑原地覆盖，下游看不见"结论换了"。"""
    a = {"facts": [{"fact_id": "f1", "text": "甲"}], "characters": []}
    b = {"facts": [{"fact_id": "f1", "text": "乙"}], "characters": []}
    assert extracted_fingerprint(a) != extracted_fingerprint(b)


def test_parse_accepts_code_fence_but_rejects_junk() -> None:
    fenced = "```json\n" + _payload() + "\n```"
    assert parse_block_output(fenced)["facts"]
    with pytest.raises(VnextFormatError):
        parse_block_output("[1,2,3]")


def test_window_truncation_is_reported_in_prompt() -> None:
    """截断必须写进 Prompt，否则模型把"没提到"当"不存在"。"""
    from backend.vnext.knowledge import CharacterStateRecord, FactRecord, SourceRef

    store = KnowledgeStore()
    store.add_block(
        block_signature="b0",
        facts=[
            FactRecord(fact_id=f"f{i}", text=f"事实{i}", source=SourceRef("ch_1", 1, 0, 3, "h"))
            for i in range(30)
        ],
        character_records=[CharacterStateRecord("甲", "在", SourceRef("ch_1", 1, 0, 3, "h"))],
    )
    window = build_prompt_window(
        store, exclusive_before=5, limits=PromptLimits(max_verified_facts=3)
    )
    messages = build_messages(title="书", block_label="第1章", chapters=[("ch_1", "正文")], window=window)
    user = messages[1]["content"]
    assert "省略" in user and "facts" in user
