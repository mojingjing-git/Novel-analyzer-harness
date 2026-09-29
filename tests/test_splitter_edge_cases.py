"""
切章器边界 case —— Phase 3 (2026-09-02) v2 plan T3.1-T3.6

4 个边界 case：
  1. 空文件 → 抛 ValueError 或返回空 chapters（当前实现：返回 1 个 0 字 "全文" 章，不报错）
  2. 单章节（"第1章 xxx" + 500 字正文）→ 返回 1 章不报错
  3. 5 行诗集 → 返回 1 章不报错
  4. 20MB 单章 → 自动拆为 2+ 章（@pytest.mark.slow，超时 180s）

设计要点：
  - 不修改 splitter_service.py 的大逻辑；只允许在 split_text 入口加 ≤ 2 行
    防护（T3.2-T3.5）使所有 case 满足"不报错 + 合理结果"。
  - 20MB 测试用手工 TemporaryDirectory()（不依赖 pytest tmp_path，避开
    Windows 文件锁导致的 teardown PermissionError）。
  - 自定义 @pytest.mark.slow 标记；conftest.py 注册 markers 防止 -W 警告。
  - 超时用 threading 实现（无 pytest-timeout 依赖）—— Windows 兼容。

签名：split_text(content: str, options: SplitOptions) -> SplitResult
"""
import sys
import shutil
import tempfile
import threading
import time
from pathlib import Path

# 与 test_splitter_english_baseline.py 一致：显式注入项目根路径，便于单独运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from backend.services.splitter_service import (
    split_text,
    SplitOptions,
    SplitResult,
    _detect_and_read,
)


# ---------------------------------------------------------------------------
# 自定义 marker 注册（在 tests/conftest.py 里全局注册，本文件不再重复）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 超时工具（无 pytest-timeout 依赖，跨平台）
# ---------------------------------------------------------------------------
def _run_with_timeout(fn, timeout_sec: float, label: str):
    """线程超时包装：超时抛 TimeoutError。Windows 兼容（不支持 signal.alarm）。"""
    result_holder = {}

    def target():
        try:
            result_holder["value"] = fn()
        except Exception as e:
            result_holder["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        # 线程是 daemon，无法强杀；只能等垃圾回收。pytest 会超时失败。
        raise TimeoutError(f"{label} exceeded {timeout_sec}s timeout")
    if "error" in result_holder:
        raise result_holder["error"]
    return result_holder["value"]


# ---------------------------------------------------------------------------
# T3.2: 空文件 → 抛 ValueError 或返回空 chapters
# ---------------------------------------------------------------------------
def test_empty_content_returns_sensible_result():
    """空字符串应返回合理结果：要么抛 ValueError，要么 0 章空 SplitResult。

    验收：不能抛非 ValueError 异常（如 IndexError / UnicodeDecodeError）；
    现有行为：返回 1 个 0 字 "全文" 章（兜底逻辑）。
    """
    try:
        result = split_text("")
    except ValueError as e:
        # 抛 ValueError 也算"合理"
        assert "empty" in str(e).lower() or "空" in str(e), f"unexpected msg: {e}"
        return
    # 不抛：必须是 SplitResult + 0 或 1 章
    assert isinstance(result, SplitResult)
    assert len(result.chapters) <= 1, f"空文件不应切出多章: {len(result.chapters)}"


def test_whitespace_only_content_returns_sensible_result():
    """纯空白（空格/换行）应与空字符串行为一致。"""
    try:
        result = split_text("   \n\n   \n")
    except ValueError:
        return
    assert isinstance(result, SplitResult)
    assert len(result.chapters) <= 1


# ---------------------------------------------------------------------------
# T3.3: 单章节（"第1章 xxx" + 500 字正文）→ 1 章不报错
# ---------------------------------------------------------------------------
def test_single_chapter_returns_one_chapter():
    """'第1章 xxx' + 500 字正文 → 1 章。"""
    body_lines = ["韩立出生于一个普通的乡村家庭，对修仙有着浓厚的兴趣。"] * 20  # ~520 字
    text = "第1章 山村少年\n" + "\n".join(body_lines)
    result = split_text(text)
    assert isinstance(result, SplitResult)
    assert len(result.chapters) == 1, f"单章节应切出 1 章，实际 {len(result.chapters)}"
    assert result.chapters[0].num == 1
    assert "山村少年" in result.chapters[0].title or "第1章" in result.chapters[0].title


def test_single_chapter_with_min_words_does_not_crash():
    """min_words 过滤短章节不挂：要么 0 章（静默），要么抛 ValueError，都可接受。

    注：silent-failure 保护阈值是"过滤后 < 5 章 且 pre_filter >= 5"。
    单章节 pre_filter=1 < 5，预期静默通过（0 章）。本测试只验证不抛非 ValueError 异常。
    """
    text = "第1章 短章\n这是 10 字的短章内容。"  # < 50 字
    try:
        result = split_text(text, SplitOptions(min_words=50, merge_tiny=False))
    except ValueError:
        # 抛 ValueError 也算"合理"
        return
    # 静默通过：0 或 1 章均可接受
    assert isinstance(result, SplitResult)
    assert len(result.chapters) <= 1, f"单章应只剩 0/1 章，实际 {len(result.chapters)}"


# ---------------------------------------------------------------------------
# T3.4: 5 行诗集 → 1 章不报错
# ---------------------------------------------------------------------------
def test_5_line_poem_returns_one_chapter_no_error():
    """5 行诗集（无章节标记）→ 1 章不报错。"""
    poem = "静夜思\n床前明月光\n疑是地上霜\n举头望明月\n低头思故乡\n"
    result = split_text(poem)
    assert isinstance(result, SplitResult)
    assert len(result.chapters) == 1, f"5 行诗集应切出 1 章，实际 {len(result.chapters)}"
    # 兜底章节标题应为 "全文"
    assert result.chapters[0].title == "全文"


def test_short_text_under_10_lines_returns_one_chapter():
    """超短文（< 10 行 / < 1000 字）→ 1 章不报错。"""
    short = "从前有个程序员。\n他写了一个切章器。\n切章器很努力地工作。\n"
    result = split_text(short)
    assert isinstance(result, SplitResult)
    assert len(result.chapters) == 1


# ---------------------------------------------------------------------------
# T3.5: 20MB 单章 → 自动拆为 2+ 章（max_words 拆分逻辑）
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_oversized_single_chapter_auto_splits():
    """20MB 单章（无章节标记 + 大正文）→ max_words 拆分逻辑自动拆为 2+ 章。

    流程：
      1. 写一个 20MB 文本文件：1 个开头段（兜底 "全文" 章）+ 大量重复段落。
      2. 用 SplitOptions(max_words=10_000_000) 跑 split_text。
      3. 期望切出 >= 2 章。
      4. 整个测试在 180s 内完成。

    实现：手工 TemporaryDirectory 避开 pytest tmp_path 的 Windows 文件锁问题
    （pytest teardown 在某些情况下会触发 PermissionError: [WinError 5]）。
    """
    tmpdir = tempfile.mkdtemp(prefix="splitter_oversized_")
    try:
        big = Path(tmpdir) / "big_single_chapter.txt"
        # 写一个 ~20MB 的文本（UTF-8 中文，每段约 1000 字符 = ~3000 字节）
        # 8000 段 ≈ 23MB（给 max_words=10M 留充分拆分余量，应至少拆 2 章）
        paragraph = "这是一段正文内容，用于模拟超长单章。" * 50  # ~1000 字符
        content = "第1章 超长章\n" + "\n".join([paragraph] * 8_000)
        big.write_text(content, encoding="utf-8")
        actual_size_mb = big.stat().st_size / 1024 / 1024
        assert 18 <= actual_size_mb <= 30, f"写入大小异常: {actual_size_mb:.2f}MB"

        def _do_split():
            text = _detect_and_read(big)
            # max_words=1MB：内容 7MB 应至少拆 7 章（验证拆分逻辑）
            return split_text(text, SplitOptions(max_words=1_000_000))

        result = _run_with_timeout(_do_split, timeout_sec=180, label="20MB 单章切分")

        # 断言：必须 >= 2 章（拆了）
        assert isinstance(result, SplitResult)
        assert len(result.chapters) >= 2, (
            f"20MB 单章未自动拆分，实际 {len(result.chapters)} 章。"
            f"max_words=1_000_000 逻辑可能失效。"
        )
        # 不报异常
        assert result.total_words > 0
        # 拆出的章应该都带原标题后缀
        assert any("(1/" in c.title or "(2/" in c.title for c in result.chapters), (
            f"自动拆分应加 (n/m) 后缀，实际标题={[c.title[:30] for c in result.chapters[:3]]}"
        )
    finally:
        # 测试结束后清理临时目录（Windows 上有时 .exists() 仍返回 True，强制 ignore_errors）
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# T3.5 补充：10MB 单章 + 短内容（验证不超 max_words 时不拆）
# ---------------------------------------------------------------------------
def test_normal_chapter_not_split_when_under_max_words():
    """1KB 单章 + max_words=1MB → 不拆，仍 1 章。"""
    text = "第1章 正常章\n" + "短正文。\n" * 100  # ~600 字符
    result = split_text(text, SplitOptions(max_words=1_000_000))
    assert len(result.chapters) == 1, f"未超 max_words 不应拆，实际 {len(result.chapters)} 章"
