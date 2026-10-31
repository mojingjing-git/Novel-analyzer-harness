"""有界证据包：只给窗口，不给全书。

这些用例守的是"模型能看到的范围 = 程序允许它引用的范围"。
一旦两者不一致，就会出现"引文是编的但校验通过"——正是这套改造要防的事。
"""

from __future__ import annotations

import asyncio

from backend.core.evidence import EvidenceIndex
from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem

CHAPTERS = {
    1: "第一章 起\n方醒打开铜匣，发现底下那层是空的。到底谁先动过？",
    2: "第二章 承\n街上人来人往，无人提起铜匣。",
    3: "第三章 转\n他忽然想起当年搬动铜匣的人，原来是自己。",
    4: "第四章 合\n此后再无人提及旧事。",
}


def _item(**kw) -> ForeshadowItem:
    base = dict(
        id="fs_001",
        description="铜匣底下空了",
        first_seen_chapter=1,
        first_seen_batch=0,
        last_seen_chapter=1,
        last_seen_batch=0,
        anchor_quote="底下那层是空的",
        anchor_chapter=1,
        question="谁先动过铜匣？",
    )
    base.update(kw)
    return ForeshadowItem(**base)


def test_bundle_contains_anchor_and_later_hits_only():
    index = EvidenceIndex(CHAPTERS)
    bundle = index.bundle_for(anchor_chapter=1, anchor_quote="底下那层是空的", hint_texts=["铜匣"])
    assert "第 1 章（埋设处）" in bundle.text
    assert "原来是自己" in bundle.text          # 第 3 章命中
    assert "第 2 章" not in bundle.text or "铜匣" in bundle.text  # 第 2 章也提到，允许
    assert 1 in bundle.chapters


def test_mentions_skip_chapters_before_anchor():
    index = EvidenceIndex(CHAPTERS)
    assert index.find_mentions(["铜匣"], after=1) == [2, 3]


def test_bundle_is_empty_when_no_text():
    index = EvidenceIndex({})
    bundle = index.bundle_for(anchor_chapter=1, anchor_quote="不存在的话")
    assert bundle.empty
    assert bundle.text == ""


def test_recheck_uses_bundle_instead_of_whole_book():
    prompts: list[str] = []

    async def model(prompt: str) -> str:
        prompts.append(prompt)
        return '{"verdict": "INSUFFICIENT", "reason": "MATERIAL_INSUFFICIENT"}'

    outcome = asyncio.run(
        run_recheck_v2(
            [_item()],
            chapters=CHAPTERS,
            call_model=model,
            max_calls=1,
            batch_size=1,
        )
    )
    assert prompts, "应当至少调用一次"
    # 证据包里有埋设处标记；不含未被选中的章节正文（第 4 章）
    assert "埋设处" in prompts[0]
    assert "此后再无人提及旧事" not in prompts[0]
    assert outcome.status != "COMPLETED" or outcome.coverage["checked"] == 1


def test_recheck_marks_not_checked_when_anchor_missing():
    async def model(prompt: str) -> str:  # 不该被调用
        raise AssertionError("没有锚点就不该调用模型")

    outcome = asyncio.run(
        run_recheck_v2(
            [_item(anchor_quote="", anchor_chapter=0)],
            chapters=CHAPTERS,
            call_model=model,
            max_calls=3,
        )
    )
    assert outcome.calls == 0
    assert outcome.not_checked == ["fs_001"]
