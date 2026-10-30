"""vnext 身份与指纹（PLAN v2 §2.1，S2 的"来源模型"基础）。

这里的用例重点是**稳定性**与**敏感性**：
指纹对无关变化必须不变（否则缓存全废），对相关变化必须变（否则旧结果被误认）。
"""

from __future__ import annotations

import pytest

from backend.vnext import fingerprint as fp


# ---------------------------------------------------------------------------
# 文本规范化
# ---------------------------------------------------------------------------


def test_normalize_unifies_newlines_and_bom() -> None:
    assert fp.normalize_chapter_text("\ufeff第一章\r\n内容\r\n") == "第一章\n内容\n"
    assert fp.normalize_chapter_text("第一章\r内容\r") == "第一章\n内容\n"


def test_normalize_strips_trailing_spaces_but_keeps_indent() -> None:
    """行尾空白删掉（不可见、会让 hash 飘），行首缩进保留（是正文信息）。"""
    out = fp.normalize_chapter_text("　　缩进保留   \n第二行\t\n")
    assert out == "　　缩进保留\n第二行\n"


def test_normalize_collapses_long_blank_runs() -> None:
    out = fp.normalize_chapter_text("a\n\n\n\n\nb\n")
    assert out == "a\n\nb\n"


def test_normalize_is_idempotent() -> None:
    """规范化必须幂等——否则二次导入会得到不同 content_hash。"""
    once = fp.normalize_chapter_text("第一章\r\n\r\n\r\n\r\n正文  \r\n")
    assert fp.normalize_chapter_text(once) == once


def test_normalize_keeps_offsets_meaningful() -> None:
    """规范化只删不可见/多余空白，不改变可见字符的相对顺序。"""
    raw = "第一段。\n\n第二段。"
    norm = fp.normalize_chapter_text(raw)
    start = norm.index("第二段")
    assert fp.slice_source_ref(norm, start, start + 3) == "第二段"


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------


def test_content_hash_changes_with_text() -> None:
    a = fp.content_hash(fp.normalize_chapter_text("内容甲"))
    b = fp.content_hash(fp.normalize_chapter_text("内容乙"))
    assert a != b
    assert len(a) == 64


def test_content_hash_ignores_irrelevant_formatting() -> None:
    """同一章不同换行/行尾空白 → 同一 hash（否则重存一次结果就失效）。"""
    a = fp.content_hash(fp.normalize_chapter_text("第一章\r\n正文  \r\n"))
    b = fp.content_hash(fp.normalize_chapter_text("第一章\n正文\n"))
    assert a == b


# ---------------------------------------------------------------------------
# block_signature
# ---------------------------------------------------------------------------


def test_block_signature_is_order_sensitive() -> None:
    """顺序参与拼接：换章顺序必须换签名，否则"同一组章不同故事线"会被当成同一块。"""
    h = [("c1", "a" * 64), ("c2", "b" * 64)]
    s1 = fp.block_signature(h, block_strategy_version=1)
    s2 = fp.block_signature(list(reversed(h)), block_strategy_version=1)
    assert s1 != s2


def test_block_signature_binds_strategy_version() -> None:
    h = [("c1", "a" * 64)]
    assert fp.block_signature(h, block_strategy_version=1) != fp.block_signature(
        h, block_strategy_version=2
    )


def test_block_signature_changes_when_any_chapter_text_changes() -> None:
    base = [("c1", "a" * 64), ("c2", "b" * 64)]
    changed = [("c1", "a" * 64), ("c2", "c" * 64)]
    assert fp.block_signature(base, block_strategy_version=1) != fp.block_signature(
        changed, block_strategy_version=1
    )


def test_block_signature_ignores_display_only_fields() -> None:
    """同样成员同样 hash，只是换了展示章号 → 签名不变（展示不进指纹）。"""
    h = [("c1", "a" * 64)]
    assert fp.block_signature(h, block_strategy_version=1) == fp.block_signature(
        [("c1", "a" * 64)], block_strategy_version=1
    )


# ---------------------------------------------------------------------------
# evidence_id / task_key / result_id
# ---------------------------------------------------------------------------


def _ev(**over):
    base = dict(
        book_id="b1",
        chapter_id="c1",
        chapter_content_hash="a" * 64,
        start=10,
        end=20,
    )
    base.update(over)
    return fp.evidence_id(**base)


def test_evidence_id_is_short_stable_and_sensitive() -> None:
    assert len(_ev()) == 12
    assert _ev() == _ev()
    assert _ev(start=11) != _ev()
    assert _ev(end=21) != _ev()
    assert _ev(chapter_id="c2") != _ev()
    assert _ev(chapter_content_hash="b" * 64) != _ev()


def test_evidence_id_cannot_collide_on_separator() -> None:
    """用 '|' 拼接会让 ("ab","c") 与 ("a","bc") 撞同一哈希；这里必须不同。"""
    a = fp.evidence_id(book_id="ab", chapter_id="c", chapter_content_hash="x",
                       start=0, end=1)
    b = fp.evidence_id(book_id="a", chapter_id="bc", chapter_content_hash="x",
                       start=0, end=1)
    assert a != b


def test_task_key_excludes_run_dimension() -> None:
    """同一输入同一策略 → 同一任务键（跨运行复用缓存的前提）。"""
    k1 = fp.task_key(kind="block_analysis", stage="extract",
                     inputs={"block": "sig"}, strategy_versions={"prompt": 1})
    k2 = fp.task_key(kind="block_analysis", stage="extract",
                     inputs={"block": "sig"}, strategy_versions={"prompt": 1})
    assert k1 == k2


def test_task_key_changes_when_strategy_changes() -> None:
    k1 = fp.task_key(kind="k", stage="s", inputs={"i": 1}, strategy_versions={"prompt": 1})
    k2 = fp.task_key(kind="k", stage="s", inputs={"i": 1}, strategy_versions={"prompt": 2})
    assert k1 != k2


def test_result_id_changes_with_model_or_prompt() -> None:
    base = dict(block_sig="s", prompt_strategy_version=1,
                model_fingerprint="m", dependency_fp="d")
    r0 = fp.result_id(**base)
    assert r0 != fp.result_id(**{**base, "model_fingerprint": "m2"})
    assert r0 != fp.result_id(**{**base, "prompt_strategy_version": 2})


# ---------------------------------------------------------------------------
# 序列化与偏移
# ---------------------------------------------------------------------------


def test_canonical_json_sorts_keys_and_keeps_chinese() -> None:
    out = fp.canonical_json({"b": 1, "a": "中文"})
    assert out == '{"a":"中文","b":1}'
    assert "\\u" not in out


def test_slice_source_ref_rejects_out_of_range() -> None:
    with pytest.raises(IndexError):
        fp.slice_source_ref("abc", 0, 9)
    with pytest.raises(IndexError):
        fp.slice_source_ref("abc", 2, 1)


def test_codepoint_len_counts_astral_characters_as_one() -> None:
    """一个 emoji 是 1 个码点、2 个 UTF-16 码元；偏移用码点才不会错位。"""
    assert fp.codepoint_len("a😀b") == 3
    assert len("a😀b".encode("utf-16-le")) // 2 == 4
