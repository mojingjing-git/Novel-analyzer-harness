"""S0 自检：合成语料夹具必须覆盖 PLAN v2 §S0-5 列出的全部边界。"""

from __future__ import annotations

from backend.tests.fixtures.vnext_corpus import (
    AMBIGUOUS_QUOTE,
    BLOCK_SIZE,
    FORESHADOWS,
    RETRY_CHAPTERS,
    TOTAL_CHAPTERS,
    build_corpus,
)


def test_corpus_is_complete_and_deterministic(tmp_path):
    first = build_corpus(tmp_path / "a")
    second = build_corpus(tmp_path / "b")

    assert len(first.chapter_ids) == TOTAL_CHAPTERS - len(first.missing_chapters)
    for chapter_id in first.chapter_ids:
        a = (first.root / "blocks" / f"{chapter_id}.txt").read_bytes()
        b = (second.root / "blocks" / f"{chapter_id}.txt").read_bytes()
        assert a == b, f"{chapter_id} 不是确定性生成"


def test_missing_chapter_creates_gap_but_numbering_continues(tmp_path):
    corpus = build_corpus(tmp_path)
    assert corpus.missing_chapters == [17]
    numbers = [int(c.split("_")[1]) for c in corpus.chapter_ids]
    assert 17 not in numbers
    assert 16 in numbers and 18 in numbers


def test_block_ranges_skip_the_gap(tmp_path):
    corpus = build_corpus(tmp_path)
    for start, end in corpus.block_ranges:
        assert end - start + 1 <= BLOCK_SIZE
        assert not (start <= 17 <= end), f"块 {(start, end)} 跨过了缺失章"
    # 缺口两侧必须落在不同块里
    assert any(start > 17 for start, _ in corpus.block_ranges)


def test_same_chapter_plant_and_resolve_present(tmp_path):
    corpus = build_corpus(tmp_path)
    pair = FORESHADOWS["same_chapter_pair"]
    assert pair["same_chapter"] is True
    text = corpus.chapter_text(pair["plant_chapter"])
    assert pair["plant_quote"] in text
    assert pair["resolve_quote"] in text
    # 同章内顺序必须"先埋后收"，否则该用例无法检验位置比较
    assert text.index(pair["plant_quote"]) < text.index(pair["resolve_quote"])


def test_ambiguous_quote_exists_but_is_not_a_resolution(tmp_path):
    corpus = build_corpus(tmp_path)
    text = corpus.chapter_text(19)
    assert AMBIGUOUS_QUOTE in text
    # 它词面相关（含"铜匣"）却语义无关：自动裁决必须拒绝
    bronze = FORESHADOWS["bronze_case"]
    assert bronze["plant_chapter"] != 19
    assert bronze["resolve_chapter"] != 19


def test_interacting_and_co_present_chapters_are_distinct(tmp_path):
    corpus = build_corpus(tmp_path)
    interacting = corpus.chapter_text(14)
    co_present = corpus.chapter_text(15)

    # 有互动：同章出现对话引号
    assert "「" in interacting and "」" in interacting
    # 无互动：同章出现两处地点分述，且没有对话
    assert "「" not in co_present
    assert "城东" in co_present and "城西" in co_present


def test_retry_chapters_are_ordinary_chapters(tmp_path):
    corpus = build_corpus(tmp_path)
    numbers = [int(c.split("_")[1]) for c in corpus.chapter_ids]
    for chapter in RETRY_CHAPTERS:
        assert chapter in numbers
        assert corpus.chapter_text(chapter).strip()
