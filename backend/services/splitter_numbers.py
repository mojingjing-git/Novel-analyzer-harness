"""切分器的数字解析器库（与切分算法正交）。

三套解析器分别覆盖：中文数字（含 万/亿 节级联乘）、罗马数字（古典英文章节）、
英文数词（"twenty-one" 等）。只做"字符串 → int"，不感知章节语义；调用方
（splitter_service._extract_chapter_num）决定哪个解析器用于哪类标题。
"""

from typing import Optional


# ---------------------------------------------------------------------------
# 中文数字 → 阿拉伯数字
# ---------------------------------------------------------------------------
_CN_NUM = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000,
}


def cn_to_int(chars: str) -> Optional[int]:
    """把『一百二十三』『万』『两』等中文数字转换为 int；纯阿拉伯直接转。"""
    chars = chars.strip()
    if not chars:
        return None
    if chars.isdigit():
        return int(chars)
    # 过滤掉非数字汉字
    if not any(c in _CN_NUM for c in chars):
        return None
    total = 0      # 已结算部分（跨越 万/亿 边界后累计）
    section = 0    # 当前节内（万以下）累计
    number = 0     # 当前正在拼接的小于 十 的数
    for ch in chars:
        if ch in ("零", "〇"):
            continue
        elif ch in "一二三四五六七八九两":
            number = number * 10 + _CN_NUM[ch]
        elif ch == "十":
            number = 10 if number == 0 else number * 10
            section += number
            number = 0
        elif ch == "百":
            number = 1 if number == 0 else number
            section += number * 100
            number = 0
        elif ch == "千":
            number = 1 if number == 0 else number
            section += number * 1000
            number = 0
        elif ch == "万":
            # 结算进 total，重置 section：直接改写 section 会让
            # "一亿零二万三千" 被算成 1000000023000（亿级联乘重复累计）
            total += (section + number) * 10000
            section = 0
            number = 0
        elif ch == "亿":
            total += (section + number) * 100000000
            section = 0
            number = 0
    return total + section + number


_ROMAN_MAP = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _roman_to_int(s: str) -> Optional[int]:
    """把罗马数字转换为 int；非合法罗马数字返回 None。

    防御：
      - 超过 12 字符 → 拒绝（避免误命中长行）
      - 单字符 I 保留（I=1），其他单字符拒绝（避免代词 I 等误命中）
    """
    if not s or len(s) > 12:
        return None
    if len(s) < 2 and s != "I":
        return None
    total = 0
    prev = 0
    for c in reversed(s):
        if c not in _ROMAN_MAP:
            return None
        cur = _ROMAN_MAP[c]
        if cur < prev:
            total -= cur
        else:
            total += cur
        prev = cur
    return total if total > 0 else None


_WORD_TO_INT = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100,
}


def _word_to_int(word: str) -> Optional[int]:
    """英文数词 → int。处理 "twenty-one" / "twenty one" / "twenty" 等。"""
    if word in _WORD_TO_INT:
        return _WORD_TO_INT[word]
    for sep in ("-", " "):
        if sep in word:
            parts = word.split(sep)
            total = 0
            for p in parts:
                v = _WORD_TO_INT.get(p)
                if v is None:
                    return None
                total += v
            return total
    return None
