"""书名推断规则：

用 suffix 白名单（校对版/完整版/全本）剥离书名版本后缀会漏，
永远漏（精校版/精修版/典藏版/完结版/最终版/校对后/无括号版 全部漏网）。
真实案例：用户两本书《从姑获鸟开始》（精校版）和《超级能源强国》（精校版）
推断出来的书名包含"精校版"，存到 workspace 后目录是
`workspace/《xxx》（精校版）/blocks/...`，用户看起来"推断失败"。

修法：优先用《...》截取（最稳，所有主流电子书都是这个格式）；
截不到时回退到扩展白名单（同时覆盖带/不带括号变体）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.services.splitter_service import infer_book_name


# ---------------------------------------------------------------------------
# 主路径：截取《...》
# ---------------------------------------------------------------------------

def test_user_real_files_extract_bookname():
    """用户两本真实文件 → 推断出干净书名《xxx》"""
    cases = [
        (r"F:\AI\04_小说与写作\修复后txt文件\《从姑获鸟开始》（精校版）.txt",
         "《从姑获鸟开始》"),
        (r"F:\AI\04_小说与写作\修复后txt文件\《超级能源强国》（精校版）.txt",
         "《超级能源强国》"),
    ]
    for path, expected in cases:
        src = Path(path)
        if not src.exists():
            continue  # 文件不存在时跳过，不影响其他 case
        actual = infer_book_name(src)
        assert actual == expected, f"书名推断错: {path} → {actual!r} (期望 {expected!r})"


@pytest.mark.parametrize("filename,expected", [
    # 用户报告的"失效"案例：精校版/精修版/典藏版/完结版/最终版
    ("《xxx》（精校版）.txt", "《xxx》"),
    ("《xxx》（精修版）.txt", "《xxx》"),
    ("《xxx》（典藏版）.txt", "《xxx》"),
    ("《xxx》（完结版）.txt", "《xxx》"),
    ("《xxx》（最终版）.txt", "《xxx》"),
    ("《xxx》（高清版）.txt", "《xxx》"),
    ("《xxx》（校对后）.txt", "《xxx》"),
    # 嵌套"xx版" 复合后缀
    ("《xxx》（TXT精校版）.txt", "《xxx》"),
    ("《xxx》（校对版全本）.txt", "《xxx》"),
    ("《xxx》（完）.txt", "《xxx》"),
    # 全角 vs 半角括号
    ("《xxx》(精校版).txt", "《xxx》"),
    ("《xxx》（精校版）.txt", "《xxx》"),
    # 无括号版本
    ("《xxx》精校版.txt", "《xxx》"),
    ("《xxx》.txt", "《xxx》"),
    # 半角书名号
    ("<xxx>（精校版）.txt", "<xxx>"),
    # 已支持的 case 必须保持可用
    ("《xxx》（校对版）.txt", "《xxx》"),
    ("《xxx》（完整版）.txt", "《xxx》"),
    ("《xxx》（全本）.txt", "《xxx》"),
    ("《xxx》.txt", "《xxx》"),
])
def test_quote_extraction_handles_all_known_variants(filename, expected):
    actual = infer_book_name(Path(filename))
    assert actual == expected, f"{filename!r} → {actual!r} (期望 {expected!r})"


# ---------------------------------------------------------------------------
# Fallback 路径：无《》时按后缀剥离
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename,expected", [
    # 无括号版本（早期电子书）
    ("xxx校对版.txt", "xxx"),
    ("xxx完整版.txt", "xxx"),
    ("xxx精校版.txt", "xxx"),
    ("xxx校对版全本.txt", "xxx"),
    ("xxx全本.txt", "xxx"),
    # 半角括号
    ("xxx(校对版).txt", "xxx"),
    # 无版本后缀
    ("xxx.txt", "xxx"),
    # 分析产物（保持兼容）
    ("xxx_分析报告.txt", "xxx"),
    ("xxx_分析.txt", "xxx"),
    ("xxx-分析报告.txt", "xxx"),
])
def test_fallback_strips_known_suffixes(filename, expected):
    actual = infer_book_name(Path(filename))
    assert actual == expected, f"{filename!r} → {actual!r} (期望 {expected!r})"


# ---------------------------------------------------------------------------
# P5b: 无《》时的分隔符变体（用户问「万一没有书名号怎么办」）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename,expected", [
    # 裸后缀（最简）
    ("斗破苍穹精校版.txt", "斗破苍穹"),
    ("斗破苍穹校对版.txt", "斗破苍穹"),
    ("斗破苍穹全本.txt", "斗破苍穹"),
    # 连字符
    ("斗破苍穹-精校版.txt", "斗破苍穹"),
    ("斗破苍穹-校对版.txt", "斗破苍穹"),
    # 下划线
    ("斗破苍穹_精校版.txt", "斗破苍穹"),
    ("斗破苍穹_校对版.txt", "斗破苍穹"),
    # 点
    ("斗破苍穹.精校版.txt", "斗破苍穹"),
    # 空格（带/不带括号）
    ("斗破苍穹 校对版.txt", "斗破苍穹"),
    ("凡人修仙传 （校对版）.txt", "凡人修仙传"),
    ("星辰变 (校对版).txt", "星辰变"),
    # 全套版本词
    ("xxx精修版.txt", "xxx"),
    ("xxx典藏版.txt", "xxx"),
    ("xxx完结版.txt", "xxx"),
    ("xxx最终版.txt", "xxx"),
    ("xxx高清版.txt", "xxx"),
    ("xxx校对后.txt", "xxx"),
    ("xxx校对版全本.txt", "xxx"),
    # 无版本后缀（最简）
    ("斗破苍穹.txt", "斗破苍穹"),
    ("盘龙.txt", "盘龙"),
    # 真实无《》书名（带括号）
    ("凡人修仙传（校对版）.txt", "凡人修仙传"),
    ("凡人修仙传（精校版）.txt", "凡人修仙传"),
    # 不应该剥离的（避免误伤）
    ("盘龙_第一卷.txt", "盘龙_第一卷"),  # "第X卷"不在版本词表
    ("凡人修仙传 第1部.txt", "凡人修仙传 第1部"),  # "第X部"不在版本词表
    ("2024-05-01 斗破苍穹.txt", "2024-05-01 斗破苍穹"),  # 日期前缀不被误伤
])
def test_no_quote_with_separator_variants(filename, expected):
    """无《》时，分隔符变体（-/ _/./空格）也正确剥离版本后缀"""
    actual = infer_book_name(Path(filename))
    assert actual == expected, f"{filename!r} → {actual!r} (期望 {expected!r})"


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------

def test_empty_bookname_quote_falls_back():
    """文件名是 `《》.txt`（空书名）→ 主路径截到空，回退到 stem"""
    actual = infer_book_name(Path("《》.txt"))
    # m.group(0) 是 "《》"（带书名号），空内容在 group(1)
    # 主路径会返回 "《》"，这是合理 fallback
    assert actual == "《》" or actual == ""


def test_no_quote_no_suffix_returns_as_is():
    """无《》也无版本后缀 → 原样返回"""
    actual = infer_book_name(Path("SomeRandomBookName.txt"))
    assert actual == "SomeRandomBookName"


def test_path_with_directories_still_works():
    """路径含目录时也能正确推断（只取 stem）"""
    actual = infer_book_name(Path(r"C:\books\《从姑获鸟开始》（精校版）.txt"))
    assert actual == "《从姑获鸟开始》"
