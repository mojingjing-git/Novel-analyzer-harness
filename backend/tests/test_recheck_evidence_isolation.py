"""复审五：同章多候选的证据隔离、状态收尾、截断语义。

这三条都是"组合场景才暴露"的缺陷：单候选样本永远碰不到，
但真书里同一章埋好几条伏笔是常态。
"""

from __future__ import annotations

import asyncio
import json
import re

from backend.core.evidence import EvidenceIndex
from backend.services.recheck_v2 import run_recheck_v2
from backend.utils.foreshadow_ledger import ForeshadowItem


def _item(item_id: str, quote: str, chapter: int = 1, description: str = "某条伏笔") -> ForeshadowItem:
    return ForeshadowItem(
        id=item_id,
        description=description,
        first_seen_chapter=chapter,
        first_seen_batch=0,
        last_seen_chapter=chapter,
        last_seen_batch=0,
        anchor_quote=quote,
        anchor_chapter=chapter,
    )


def _payload(
    candidate_id: str,
    quote: str,
    verdict: str = "SUPPORTED",
    answer: str = "已交代",
    prompt: str = "",
) -> str:
    """构造模型回包：evidence_id 从提示词里取，模拟真实模型照着回填。"""
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
    """同一章两处相距很远的锚点：两个候选各自拿到不重叠的窗口。"""
    filler = "无关内容。" * 300
    return {
        1: (
            "第一章\n方醒打开铜匣，铜匣底下那层是空的。"
            + filler
            + "旧契上写着三年后归还城南米铺，落款是癸卯年。"
            + filler
        )
    }


def test_same_chapter_candidates_do_not_share_evidence_ids():
    """同章两个等长窗口曾算出同一个 evidence_id，后写覆盖前写。

    后果不是"难看"，是判错：A 引用自己看到的原文被判 QUOTE_NOT_FOUND，
    而 A 引用只展示给 B 的文字反而通过并 AUTO_RESOLVED。
    """
    chapters = _two_anchor_chapter()
    item_a = _item("fsA", "铜匣底下那层是空的", description="铜匣底部空了")
    item_b = _item("fsB", "旧契上写着三年后归还城南米铺", description="旧契还款期限")

    async def call_model(prompt: str) -> str:
        if "fsA" in prompt:
            return _payload("fsA", "铜匣底下那层是空的", prompt=prompt)
        return _payload("fsB", "旧契上写着三年后归还城南米铺", prompt=prompt)

    outcome = asyncio.run(
        run_recheck_v2([item_a, item_b], chapters=chapters, call_model=call_model, max_calls=4)
    )
    assert [v.verdict for v in outcome.verdicts] == ["SUPPORTED", "SUPPORTED"]
    assert len({v.evidence_id for v in outcome.verdicts}) == 2, "两个候选的证据 id 不该相同"


def test_citing_other_candidates_text_is_rejected():
    """引用只展示给别的候选、不在本题证据里的文字，必须判无效。"""
    chapters = _two_anchor_chapter()
    item_a = _item("fsA", "铜匣底下那层是空的", description="铜匣底部空了")

    async def call_model(prompt: str) -> str:
        # 引用的句子在别的窗口里，不在 A 的证据里
        return _payload(
            "fsA", "旧契上写着三年后归还城南米铺，落款是癸卯年。", answer="三年后归还", prompt=prompt
        )

    outcome = asyncio.run(
        run_recheck_v2([item_a], chapters=chapters, call_model=call_model, max_calls=2)
    )
    assert outcome.verdicts, "至少应产出一条判定（可能是无效）"
    assert all(v.verdict != "SUPPORTED" for v in outcome.verdicts)


def test_status_fixup_does_not_nameerror_and_marks_partial():
    """一个有效候选 + 一个缺锚点条目：不能抛 NameError，状态必须是 PARTIAL。"""
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
    """恰好命中数等于窗口上限、且全部展示时，不该报截断。"""
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
