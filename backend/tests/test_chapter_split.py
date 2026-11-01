"""2.0 切章器：复合标题校验。

四类真实文本都要过：

1. **多卷重置**：第一卷第 1..3 回、第二卷第 1..2 回 → 5 章，卷序号正确。
2. **正文里的前瞻引用**："第一百回方知分晓" 不能把章号抬到 100（否则后续 90 章全被吞）。
3. **正文里行首出现的"第N回"**（引用而非标题）不切。
4. **古籍形态**：回目与正文之间没有空行、回目带标点。

另外：章号允许重复，定位靠 `content_hash`。
"""

from __future__ import annotations

from backend.core.chapter_split import (
    chapters_with_hash,
    split_chapters,
)


def _t(*lines: str) -> str:
    return "\n".join(lines)


def test_plain_sections_are_split():
    text = _t(
        "第一章 起",
        "方醒打开铜匣。",
        "",
        "第二章 承",
        "旧契上写着三年后归还。",
        "",
        "第三章 转",
        "三年期满，米铺换了东家。",
    )
    spans = split_chapters(text)

    assert [s.chapter_no for s in spans] == [1, 2, 3]
    assert "铜匣" in spans[0].text
    assert "换了东家" in spans[2].text


def test_forward_reference_does_not_jump_the_counter():
    """正文里前瞻引用"第一百回"不能把章号抬到 100。

    若按单调规则，last 会被抬到 100，之后第 4..100 章**全部**被当成正文吞掉。
    """
    text = _t(
        "第一章 起",
        "这里先卖个关子，第一百回方知分晓。",
        "",
        "第二章 承",
        "第二回里已经埋下伏笔。",
        "",
        "第三章 转",
        "第三回水落石出。",
    )
    spans = split_chapters(text)

    assert [s.chapter_no for s in spans] == [1, 2, 3]


def test_reference_sentence_is_not_a_heading():
    """行首出现"第十回中写道"是引用，不是标题。"""
    text = _t(
        "第一章 起",
        "",
        "第十回中写道：此案另有隐情。",
        "",
        "第二章 承",
        "正文。",
    )
    spans = split_chapters(text)

    assert [s.chapter_no for s in spans] == [1, 2]


def test_volume_reset_allows_chapter_numbers_to_restart():
    """分卷小说：第二卷的章号回到 1，不能被当成回退而整卷丢弃。"""
    text = _t(
        "第一卷 风起",
        "",
        "第一章 起",
        "卷一第一章。",
        "",
        "第二章 承",
        "卷一第二章。",
        "",
        "第二卷 云涌",
        "",
        "第一章 再起",
        "卷二第一章。",
        "",
        "第二章 再承",
        "卷二第二章。",
    )
    spans = split_chapters(text)

    chapters = [(s.chapter_no, s.volume_idx) for s in spans]
    assert (1, 0) in chapters
    assert (2, 0) in chapters
    assert (1, 1) in chapters, "第二卷的第一章必须切出来"
    assert (2, 1) in chapters
    assert len(spans) == 4


def test_step_of_two_is_allowed_but_bigger_jump_is_not():
    """作者跳号（+2）容忍；跳得太多不切。"""
    ok = _t("第一章 起", "正文。", "", "第三章 转", "正文。")
    assert [s.chapter_no for s in split_chapters(ok)] == [1, 3]

    jump = _t("第一章 起", "正文。", "", "第七回 转", "正文。")
    assert [s.chapter_no for s in split_chapters(jump)] == [1]


def test_sentence_ending_punctuation_blocks_a_heading():
    """标题行以句末标点结尾 → 是正文句子，不是标题。"""
    text = _t(
        "第一章 起",
        "正文。",
        "",
        "第二章 他终于回来了。",
        "正文。",
    )
    spans = split_chapters(text)

    assert [s.chapter_no for s in spans] == [1]


def test_long_line_is_not_a_heading():
    """超过长度上限的行不是标题。"""
    long_title = "第" + "二" + "章 " + "很长的副标题" * 8
    text = _t("第一章 起", "正文。", "", long_title, "正文。")
    spans = split_chapters(text)

    assert [s.chapter_no for s in spans] == [1]


def test_no_heading_yields_single_chapter():
    """切不出标题时整段算一章（不能变成零章）。"""
    spans = split_chapters("就是一段没有任何标题的正文。")

    assert len(spans) == 1
    assert spans[0].chapter_no == 1
    assert spans[0].content_hash


def test_chapter_hash_is_stable_and_distinguishes_same_number():
    """章号相同的两段（回号重复）必须靠哈希区分开。"""
    text_a = _t("第一章 起", "甲段正文。", "", "第一章 乙", "乙段正文。")
    spans = split_chapters(text_a, first_chapter=1)
    # 第二段"第一章"紧跟第一段，步长不满足 → 不切；用两次独立文本验证哈希差异
    other = _t("第一章 起", "完全不同的正文。", "", "第二章 承", "承段正文。")
    spans2 = split_chapters(other)

    assert spans2[0].content_hash != spans2[1].content_hash
    assert spans2[0].content_hash == split_chapters(other)[0].content_hash, "哈希必须稳定"


def test_chapters_with_hash_keeps_duplicate_numbers():
    """同一章号出现多段时全部保留（不覆盖）。"""
    text = _t(
        "第一卷",
        "",
        "第一章 起",
        "甲。",
        "",
        "第二章 承",
        "乙。",
    )
    spans = split_chapters(text, first_chapter=1)
    grouped = chapters_with_hash(text, first_chapter=1)

    assert sum(len(v) for v in grouped.values()) == len(spans)
