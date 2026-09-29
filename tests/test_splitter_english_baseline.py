"""
切章器 baseline 跑测 —— 只跑不修
记录 7 本 Project Gutenberg 外文书用现有切章器切出什么。
v2 (2026-09-02) Phase 1 baseline

设计要点（与父任务派工说明一致）：
- 不修改 splitter_service.py，仅跑测
- 期望值是「宽松」版（不阻塞），多数会 fail 以暴露问题
- 已知限制（Dracula 等日记体）不报错，只记录
- 真实签名：split_text(content: str, options: SplitOptions) -> SplitResult
- SplitResult 字段：chapters / total_chapters / total_volumes / metadata / detected_pattern / pattern_name
  （没有 .volumes 属性；卷数用 .total_volumes；具体卷列表从 ChapterBlock.volume 提取）
- 文件读取用 splitter 内部的 _detect_and_read（与生产路径一致）
"""
import sys
from pathlib import Path

# 顶层 tests/ 与 backend/tests 共用项目根路径；显式注入便于单独运行本文件
# 与 backend/tests/test_splitter_mixed_formats.py 同样的做法
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from backend.services.splitter_service import (
    SplitResult,
    _detect_and_read,
    split_text,
)


FIXTURES = Path(__file__).parent / "fixtures" / "gutenberg"

# 期望值是粗略的（plan §3 验收测试矩阵的"宽松"版本）
EXPECTED = {
    1342:  {"min_chapters": 50,  "title_contains": "Pride",         "label": "Pride and Prejudice"},
    84:    {"min_chapters": 20,  "title_contains": "Frankenstein",  "label": "Frankenstein"},
    345:   {"min_chapters": 0,   "title_contains": "Dracula",       "known_limited": True, "label": "Dracula"},
    98:    {"min_chapters": 30,  "title_contains": "Tale",          "min_volumes": 2, "label": "A Tale of Two Cities"},
    2701:  {"min_chapters": 100, "title_contains": "Moby",          "label": "Moby-Dick"},
    64317: {"min_chapters": 5,   "title_contains": "Gatsby",        "label": "The Great Gatsby"},
    1661:  {"min_chapters": 8,   "title_contains": "Sherlock",      "label": "Sherlock Holmes"},
}


def _split(gutenberg_id: int) -> SplitResult:
    txt_path = FIXTURES / f"gutenberg_{gutenberg_id}.txt"
    if not txt_path.exists():
        pytest.skip(f"未下载: {txt_path}")
    content = _detect_and_read(txt_path)
    return split_text(content)


def _volume_set(result: SplitResult):
    """SplitResult 没有 .volumes 列表；卷信息在每个 ChapterBlock.volume 字段里。"""
    return {ch.volume for ch in result.chapters if ch.volume}


@pytest.mark.parametrize("gutenberg_id", list(EXPECTED.keys()))
def test_baseline_split(gutenberg_id):
    """每本 Gutenberg 外文书：跑现有切章器，记录章节数 + metadata。

    断言策略：宽松。多数用例预期 fail（暴露 v2 待修的 bug），但 Dracula
    等已知限制不 fail。pytest -v -s 输出会留下完整 baseline 现场。
    """
    result = _split(gutenberg_id)
    exp = EXPECTED[gutenberg_id]

    actual_chapters = len(result.chapters)
    actual_total = result.total_chapters
    actual_volumes = _volume_set(result)
    actual_total_volumes = result.total_volumes
    title = (result.metadata.get("title") or "").strip()
    author = (result.metadata.get("author") or "").strip()
    pattern_name = result.pattern_name
    detected = result.detected_pattern[:80] + "..." if len(result.detected_pattern) > 80 else result.detected_pattern

    print(f"\n=== Gutenberg #{gutenberg_id} {exp['label']!r} baseline ===")
    print(f"  章节数: {actual_chapters} (total_chapters={actual_total}, 期望 >= {exp['min_chapters']})")
    print(f"  卷数: {actual_total_volumes} (集合={sorted(actual_volumes)!r}, 期望 >= {exp.get('min_volumes', 0)})")
    print(f"  metadata.title:  {title!r}")
    print(f"  metadata.author: {author!r}")
    print(f"  pattern_name:    {pattern_name!r}")
    print(f"  detected_preview: {detected!r}")

    # 宽松断言（不阻塞，只记录）
    if actual_chapters < exp["min_chapters"]:
        if exp.get("known_limited"):
            print(f"  ⚠️ 已知限制（不报错）: 章节数 {actual_chapters} < {exp['min_chapters']}")
        else:
            pytest.fail(
                f"❌ Gutenberg #{gutenberg_id} {exp['label']!r}: "
                f"章节数 {actual_chapters} < 期望 {exp['min_chapters']} "
                f"(pattern_name={pattern_name!r})"
            )
    if exp["title_contains"].lower() not in title.lower():
        print(f"  ⚠️ metadata.title 不含 '{exp['title_contains']}' (实际: {title!r})")
    if "min_volumes" in exp and actual_total_volumes < exp["min_volumes"]:
        print(f"  ⚠️ 卷数 {actual_total_volumes} < 期望 {exp['min_volumes']} (集合={sorted(actual_volumes)!r})")
