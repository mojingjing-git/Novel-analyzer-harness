"""倒排位图索引与逐段暴力扫的**等价性**契约。

B 优化（2-gram 位图倒排）必须保证：证据包一个字不差。
这里不比内部结构，只比**公共契约**——同一语料上，建索引与不建索引的
``find_mentions`` / ``bundle_for`` 输出完全一致；df 等价是它的推论。

filter-then-verify 为什么无漏报：词出现 ⇒ 它的**全部** 2-gram 都出现在该段，
所以"首/尾 2-gram 位图交集"是真命中集合的超集，交集内逐段 ``term in text``
验证后即精确（多滤不掉一个真命中，验证又兜住假阳性）。
"""

from __future__ import annotations

import pytest

from backend.core.evidence import EvidenceIndex


def _corpus() -> dict[int, str]:
    """30 段：共享短语 + 段内独有标记 + 只在部分段出现的组合词。"""
    units: dict[int, str] = {}
    for i in range(1, 31):
        units[i] = (
            f"第{i}段：江湖夜雨十年灯，故人相逢在镇口。"
            f"独有标记{i}号事件在此发生。"
        )
    return units


def test_find_mentions_identical_with_and_without_index():
    units = _corpus()
    indexed = EvidenceIndex(units)
    indexed.ensure_indexed()
    brute = EvidenceIndex(units)  # 不调 ensure_indexed → 走逐段暴力路径

    terms = [
        "江湖", "夜雨", "十年灯", "镇口", "独有标记",
        "标记3号", "标记30号", "不存在词", "第1段", "灯，故人",
        "一段超过四字的检索词组", "三字词",
    ]
    for after in (0, 5, 15, 29):
        a = indexed.find_mentions(terms, after=after, limit=8)
        b = brute.find_mentions(terms, after=after, limit=8)
        assert a == b, f"after={after}: {a} != {b}"


def test_bundle_identical_with_and_without_index():
    units = _corpus()
    indexed = EvidenceIndex(units)
    indexed.ensure_indexed()
    brute = EvidenceIndex(units)

    kwargs = dict(
        anchor_chapter=3,
        anchor_quote="独有标记3号事件在此发生",
        hint_texts=["标记3号事件后来如何了？", "镇口故人"],
    )
    b1 = indexed.bundle_for(**kwargs)
    b2 = brute.bundle_for(**kwargs)
    assert b1.text == b2.text
    assert b1.chapters == b2.chapters
    assert b1.spans == b2.spans
    assert b1.truncated == b2.truncated
    assert b1.omitted == b2.omitted
    assert b1.segments() == b2.segments()


def test_absent_terms_agree_and_empty_index_is_safe():
    units = _corpus()
    indexed = EvidenceIndex(units)
    indexed.ensure_indexed()
    brute = EvidenceIndex(units)
    for t in ("彻底不存在的词", " xyz "):
        assert indexed._hit_chapters(t) == brute._hit_chapters(t) == frozenset()


@pytest.mark.asyncio
async def test_single_sentence_corpus_matches():
    """单段小语料（边界）：建不建索引都必须与暴力一致。"""
    units = {1: "全文只有一句话，江湖夜雨十年灯。"}
    indexed = EvidenceIndex(units)
    indexed.ensure_indexed()
    brute = EvidenceIndex(units)
    for t in ("江湖", "十年灯", "没有这句"):
        assert indexed._hit_chapters(t) == brute._hit_chapters(t)
