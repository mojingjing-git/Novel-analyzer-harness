"""2.0 的切章器：复合标题校验。

为什么不能用"章号单调不回退"：那种规则会被两种真实文本雪崩式误杀——

- **前瞻回次引用**：正文里出现"第一百回方知分晓"，章号瞬间被抬到 100，
  之后第 11~100 章**全部**被当成正文引用而不再切分；
- **分卷重置**：长篇按卷组织（第一卷第 1..100 章，第二卷第 1..80 章），
  进入第二卷时章号回到 1，整卷被吞。

所以判据是**四条同时成立**才算章节边界：

1. **形态**：整行 strip 后长度 ≤ 40；行尾不是句末标点；前面是空行或文件首行。
2. **步长局部连续**：章号 ∈ {last+1, last+2}（容忍作者跳号），跳更多不切。
3. **分卷感知**：遇到"第 X 卷/部"重置连续性计数，允许章号回到 1。
4. **不是引用句**：含"中写道/见第/如前所述"这类谓词的行不切。

**章号允许重复**——回号不唯一是事实（同一卷里"第一回"既可能是真标题，也可能只是
正文引用）。所以**定位靠内容哈希，不靠章号**：``ChapterSpan.content_hash`` 是
该章正文的 sha256；引用定位时用 ``(章号, 哈希)`` 寻址，章号只用于人读与排序。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: 块内章节标题：第/十二/一百零三章（回）。只用来切边界，不校验标题格式。
CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:第\s*[0-9零一二三四五六七八九十百千万两]+\s*[章回节]|Chapter\s+\d+)",
    re.MULTILINE,
)

#: 卷/部标记：分卷小说用它重置章号连续性。
VOLUME_HEADING_RE = re.compile(
    r"^\s*第\s*([0-9零一二三四五六七八九十百千万两]+)\s*[卷部]", re.MULTILINE
)

#: 标题行长度上限（去掉缩进）。古典回目与网络小说章节标题都远小于它。
MAX_HEADING_LEN = 40

#: 行尾不得出现的标点——正文句子会带这些，标题不会。
SENTENCE_ENDERS = frozenset("。，！？；：…、”’』」）》】")

#: 引用句特征：正文里提到"某回中写道""见第 X 回"时，整行会以"第X回"开头。
REFERENCE_HINTS = (
    "中写道", "中说道", "中记载", "中提到", "见第", "见前", "见后",
    "如前所述", "如上所述", "前文", "后文", "详见", "参见", "下文", "上文",
)

#: 章号解析用的中文数字表
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def cn_to_int(text: str) -> int | None:
    """中文数字 → 整数；解析不了返回 None（万以内）。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
            number = 0
        else:
            return None
    return total + section + number


def chapter_number_from_heading(heading: str) -> int | None:
    """从标题文本里取章号（"第十三回" → 13）。"""
    m = re.search(r"第\s*([0-9零〇一二三四五六七八九十百千万两]+)\s*[章回节]", heading)
    if m:
        return cn_to_int(m.group(1))
    m = re.search(r"Chapter\s+(\d+)", heading, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


@dataclass(frozen=True)
class ChapterSpan:
    """一段正文（一个章节）的身份与位置。

    ``content_hash`` 是该章正文的 sha256 前 12 位——**定位靠它**，不靠章号。
    """

    chapter_no: int
    volume_idx: int
    start: int
    end: int
    text: str
    content_hash: str

    def as_dict(self) -> dict:
        return {
            "chapter_no": self.chapter_no,
            "volume_idx": self.volume_idx,
            "start": self.start,
            "end": self.end,
            "content_hash": self.content_hash,
            "chars": len(self.text),
        }


def _hash_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _line_bounds(text: str) -> list[tuple[int, int, str]]:
    """返回每行的 (start, end, stripped_text)。end 不含换行符。"""
    out: list[tuple[int, int, str]] = []
    pos = 0
    for raw in text.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        out.append((pos, pos + len(body), body))
        pos += len(raw)
    return out


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > MAX_HEADING_LEN:
        return False
    if stripped[-1] in SENTENCE_ENDERS:
        return False
    if any(hint in stripped for hint in REFERENCE_HINTS):
        return False
    return bool(CHAPTER_HEADING_RE.match(stripped) or VOLUME_HEADING_RE.match(stripped))


def split_chapters(
    text: str,
    *,
    first_chapter: int = 1,
    base_volume: int = 0,
    max_step: int = 2,
) -> list[ChapterSpan]:
    """把一段正文切成章节。

    - ``first_chapter``：本段第一段的章号起点（块级调用方传块起始序号）；
    - ``base_volume``：本段所属卷序号（分卷小说里由调用方累计）；
    - ``max_step``：允许的章号跳步（默认 +1/+2，容忍作者跳号）。

    切不出任何章节时返回**单段**（整段算一章）——与旧切章器行为一致，
    免得"没有标题"的文本变成零章。
    """
    if not text:
        return []
    lines = _line_bounds(text)
    bounds: list[tuple[int, int, int, int]] = []  # (start, end, chapter_no, volume_idx)

    last_chapter = first_chapter - 1
    volume_idx = base_volume
    saw_chapter = False
    prev_line_blank = True  # 文件首行也算"前面是空行"

    for start, end, body in lines:
        stripped = body.strip()
        is_blank = not stripped
        if not is_blank and prev_line_blank and _looks_like_heading(stripped):
            vol_match = VOLUME_HEADING_RE.match(stripped)
            chap_match = CHAPTER_HEADING_RE.match(stripped)
            if vol_match and not chap_match:
                # 纯卷标记：重置连续性。
                # 卷号只在**已经切过章之后**才递增——第一卷的标记对应 base_volume，
                # 否则第一卷会被记成第 1 卷。
                if saw_chapter:
                    volume_idx += 1
                last_chapter = first_chapter - 1
                bounds.append((start, end, 0, volume_idx))
            elif chap_match:
                parsed = chapter_number_from_heading(stripped)
                if parsed is not None and last_chapter < parsed <= last_chapter + max_step:
                    bounds.append((start, end, parsed, volume_idx))
                    last_chapter = parsed
                    saw_chapter = True
        prev_line_blank = is_blank

    if not bounds:
        whole = text
        return [ChapterSpan(
            chapter_no=first_chapter, volume_idx=base_volume, start=0, end=len(text),
            text=whole, content_hash=_hash_of(whole),
        )]

    spans: list[ChapterSpan] = []
    for idx, (start, end, chapter_no, volume) in enumerate(bounds):
        stop = bounds[idx + 1][0] if idx + 1 < len(bounds) else len(text)
        if chapter_no == 0:
            # 卷标记行本身不算一章，但它是分隔点：跳过它，不产出章
            continue
        body = text[start:stop]
        spans.append(ChapterSpan(
            chapter_no=chapter_no, volume_idx=volume, start=start, end=stop,
            text=body, content_hash=_hash_of(body.strip()),
        ))
    if not spans:
        whole = text
        return [ChapterSpan(
            chapter_no=first_chapter, volume_idx=base_volume, start=0, end=len(text),
            text=whole, content_hash=_hash_of(whole),
        )]
    return spans


def chapters_by_number(text: str, *, first_chapter: int = 1, base_volume: int = 0) -> dict[int, str]:
    """兼容旧调用形状：``{章号: 正文}``。

    章号重复时**后者覆盖前者**——所以调用方若要按章号寻址，必须带上
    ``content_hash``；只按章号寻址在回号重复的书上是不可靠的。
    """
    return {s.chapter_no: s.text for s in split_chapters(text, first_chapter=first_chapter,
                                                      base_volume=base_volume)}


def chapters_with_hash(text: str, *, first_chapter: int = 1, base_volume: int = 0) -> dict[int, list[ChapterSpan]]:
    """``{章号: [该号的所有段]}``——章号重复时全部保留。"""
    out: dict[int, list[ChapterSpan]] = {}
    for span in split_chapters(text, first_chapter=first_chapter, base_volume=base_volume):
        out.setdefault(span.chapter_no, []).append(span)
    return out
