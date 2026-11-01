"""splitter 沉默失败保护：

两个独立 bug 都会让"切分成功"但整本书被切成 1 章：

1. mode=custom + 不匹配 pattern（用户写了「第X回」但书用「章」/写错 pattern）
   期望：回退到自动检测 + log warning，而不是沉默把整本书当 1 章。

2. mode=custom/auto + min_words 阈值过高（网文章节大多 2000-5000 字，
   用户设 10000 几乎全部被过滤）
   期望：过滤后 < 5 章时抛 ValueError 让用户知道阈值过高，而不是沉默剩 1 章。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.services.splitter_service import (
    _detect_and_read,
    split_text,
    SplitOptions,
)


# ---------------------------------------------------------------------------
# Bug 1: custom pattern 0 命中回退
# ---------------------------------------------------------------------------

def test_custom_zero_match_falls_back_to_auto(tmp_path=None):
    """自定义 pattern 在书前 N 行 0 命中 → 回退自动检测，不沉默出 1 章。

    真实场景：用户切到「自定义正则」模式但 pattern 写错了（比如「第X回」
    但书用「章」），splitter 编译成功 + 0 命中 = 整本书当 1 章。
    """
    src = Path(r"F:\AI\04_小说与写作\修复后txt文件\《从姑获鸟开始》（精校版）.txt")
    if not src.exists():
        pytest.skip(f"用户真实文件不存在: {src}")

    content = _detect_and_read(src)

    # 错的 pattern：「第X回」（书用「章」不用「回」）
    result = split_text(content, SplitOptions(
        mode="custom",
        pattern=r"第[一二三四五六七八九十百千零\d]+回",
    ))
    # 不应只剩 1 章，应回退到 auto
    assert len(result.chapters) >= 5, (
        f"custom pattern 0 命中应回退 auto，实际只切出 {len(result.chapters)} 章"
    )


def test_custom_empty_pattern_falls_back_to_auto():
    """custom 模式但 pattern 为空 → 回退自动检测（不报错，正常切分）"""
    src = Path(r"F:\AI\04_小说与写作\修复后txt文件\《从姑获鸟开始》（精校版）.txt")
    if not src.exists():
        pytest.skip(f"用户真实文件不存在: {src}")

    content = _detect_and_read(src)
    result = split_text(content, SplitOptions(mode="custom", pattern=""))
    assert len(result.chapters) >= 5


def test_custom_correct_pattern_uses_custom():
    """custom 模式 + 正确 pattern → 用 custom，不回退（768 章）"""
    src = Path(r"F:\AI\04_小说与写作\修复后txt文件\《从姑获鸟开始》（精校版）.txt")
    if not src.exists():
        pytest.skip(f"用户真实文件不存在: {src}")

    content = _detect_and_read(src)
    result = split_text(content, SplitOptions(
        mode="custom",
        pattern=r"第[一二三四五六七八九十百千零\d]+章",
    ))
    # 这本书的真正「章」数 ~ 768
    assert 700 <= len(result.chapters) <= 850, (
        f"custom 正确 pattern 期望 ~768 章，实际 {len(result.chapters)}"
    )
    assert result.pattern_name == "自定义正则"


# ---------------------------------------------------------------------------
# Bug 2: min_words 过高抛错
# ---------------------------------------------------------------------------

def test_min_words_too_high_raises_with_helpful_message(tmp_path):
    """min_words 过高 + 多个 num 有值的真章节 → 过滤后 < 5 章 → 抛 ValueError。

    用合成数据保证场景：5+ 个 num 有值的真章节，内容都 < min_words。
    真实案例：用户用「第X章」pattern 切 5+ 章，然后 min_words=10000 全过滤 → 剩 0 章，
    或者设更高阈值后偶有几个超长章节也按 num 编号被过滤。
    """
    # 合成：5 章真章节，每章 ~500 字（远低于 min_words=10000）
    # 还要加一些 num=None 的子项确保 pre_filter_count >= 5
    text = (
        "——序章——\n"
        "序章内容。" + ("x" * 200) + "\n\n"
        "第1章 测试章节一\n" + ("这是测试内容。" + "x" * 50 + "\n") * 10 + "\n"
        "第2章 测试章节二\n" + ("这是测试内容。" + "x" * 50 + "\n") * 10 + "\n"
        "第3章 测试章节三\n" + ("这是测试内容。" + "x" * 50 + "\n") * 10 + "\n"
        "第4章 测试章节四\n" + ("这是测试内容。" + "x" * 50 + "\n") * 10 + "\n"
        "第5章 测试章节五\n" + ("这是测试内容。" + "x" * 50 + "\n") * 10 + "\n"
        "第6章 测试章节六\n" + ("这是测试内容。" + "x" * 50 + "\n") * 10 + "\n"
        "第7章 测试章节七\n" + ("这是测试内容。" + "x" * 50 + "\n") * 10 + "\n"
        "第8章 测试章节八\n" + ("这是测试内容。" + "x" * 50 + "\n") * 10 + "\n"
        "第9章 测试章节九\n" + ("这是测试内容。" + "x" * 50 + "\n") * 10 + "\n"
        "第10章 测试章节十\n" + ("这是测试内容。" + "x" * 50 + "\n") * 10 + "\n"
    )
    p = tmp_path / "small_book.txt"
    p.write_text(text, encoding="utf-8")

    content = _detect_and_read(p)
    # 验证 auto 模式能切出 10+ 章
    pre = split_text(content, SplitOptions())
    assert len(pre.chapters) >= 10, f"auto 模式应切出 10+ 章，实际 {len(pre.chapters)}"

    # 现在设高 min_words：所有 10 章都被过滤
    with pytest.raises(ValueError) as exc_info:
        split_text(content, SplitOptions(min_words=10000))
    msg = str(exc_info.value)
    assert "min_words=10000" in msg
    assert "过高" in msg
    assert "建议" in msg


def test_min_words_reasonable_still_works():
    """min_words=500（合理值）→ 正常切分不抛错。"""
    src = Path(r"F:\AI\04_小说与写作\修复后txt文件\《从姑获鸟开始》（精校版）.txt")
    if not src.exists():
        pytest.skip(f"用户真实文件不存在: {src}")

    content = _detect_and_read(src)
    result = split_text(content, SplitOptions(min_words=500))
    # 网文章节大多 2000+ 字，min_words=500 不应大量过滤
    assert len(result.chapters) >= 100


def test_min_words_with_small_corpus_not_affected():
    """小语料（< 5 章时即使过滤也不抛错，因为本来就没几章）。"""
    # 构造一个只有 3 章的小文档
    text = (
        "第1章 测试章节一\n"
        "这是第一章内容。" + ("x" * 100) + "\n\n"
        "第2章 测试章节二\n"
        "这是第二章内容。" + ("x" * 100) + "\n\n"
        "第3章 测试章节三\n"
        "这是第三章内容。" + ("x" * 100) + "\n\n"
    )
    p = Path(r"F:\AI\01_项目\小说分析器\tmp_small_corpus.txt")
    p.write_text(text, encoding="utf-8")
    try:
        content = _detect_and_read(p)
        # 3 章时设高 min_words 不应抛错（pre_filter_count < 5）
        result = split_text(content, SplitOptions(min_words=10000))
        # 3 章全被过滤，剩 0 章
        assert len(result.chapters) == 0
    finally:
        import os
        if p.exists():
            os.unlink(p)


# ---------------------------------------------------------------------------
# Bug 3: 合成测试 — 真实复现用户的「整本书切成 1 章」
# ---------------------------------------------------------------------------

def test_user_repro_whole_book_collapsed_to_one_chapter_fixed(tmp_path):
    """用户实际场景合成版：mode=custom + 错 pattern + min_words=10000。

    旧代码下：custom pattern 0 命中 → 整本书 1 章 (word_count=20568 唯一过 10000)。
    新代码下：custom 0 命中回退 auto → 切出 10+ 章；min_words 抛错而非沉默剩 1 章。
    """
    # 10 个真章节（num 有值），每章 ~500 字
    text = ""
    for i in range(1, 11):
        text += f"第{i}章 测试章节\n" + ("这是测试内容。" + "x" * 50 + "\n") * 10 + "\n"
    # 加一个超长章节（> 10000 字），模拟《从姑获鸟开始》第 1 章 20568 字
    text += "第100章 超长章节\n" + ("这是超长测试内容。" + "x" * 50 + "\n") * 300 + "\n"
    p = tmp_path / "user_repro.txt"
    p.write_text(text, encoding="utf-8")

    content = _detect_and_read(p)

    # 1) 错 pattern（旧代码会出 1 章）
    result1 = split_text(content, SplitOptions(
        mode="custom",
        pattern=r"第[一二三四五六七八九十百千零\d]+回",  # 书用「章」不用「回」
    ))
    assert len(result1.chapters) >= 5, (
        f"custom 0 命中应回退 auto，实际只切出 {len(result1.chapters)} 章"
    )

    # 2) min_words=10000 + 错 pattern（双 bug 叠加场景）
    with pytest.raises(ValueError) as exc_info:
        split_text(content, SplitOptions(
            mode="custom",
            pattern=r"第[一二三四五六七八九十百千零\d]+回",
            min_words=10000,
        ))
    msg = str(exc_info.value)
    assert "min_words=10000" in msg
    assert "过高" in msg
