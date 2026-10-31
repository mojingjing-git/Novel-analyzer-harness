"""本地证据取材：把"全书"换成"几段原文窗口"。

为什么需要它：复检要判"这条伏笔被回答了吗"，答案可能在几百章之外，
但把整本正文塞进 prompt 一定会爆上下文（实测 16 章就 100k token）。
本地扫描不花 token —— "这个词在全书哪里出现过"是纯程序问题。

三条设计约束：
1. **只给窗口，不给摘要**：摘要是模型写的概括，拿它核对"引文是否逐字存在"永远核不出来。
2. **窗口必须带章号与截断标记**：模型要能说"这几段里看不出来"，也要能点名要更多。
3. **不猜**：关键词找不到就返回空，让调用方记"证据不足"，不要伪造一个位置。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

WINDOW_RADIUS = 400          # 每个窗口向前后各取多少字
MAX_WINDOWS = 3              # 一个候选最多给几段（再多模型也读不完）
MIN_KEYWORD_CHARS = 2

# 这些词太常见，检索它们等于全选
_STOPWORDS = frozenset(
    "什么 为何 为什么 是否 究竟 到底 如何 怎么 怎样 后来 后文 之后 现在 当时 这个 那个 一个 自己 他们 我们".split()
)


def _keywords_for(*texts: str, limit: int = 4) -> list[str]:
    """从候选自带的词里挑检索词：母题词优先，其余取较长的实词片段。"""
    words: list[str] = []
    for text in texts:
        for token in re.split(r"[\s，。、；：？！“”‘’（）()《》【】…—·]+", text or ""):
            token = token.strip()
            if len(token) >= MIN_KEYWORD_CHARS and token not in _STOPWORDS:
                words.append(token)
    # 去重保序；短的先排后面（长词更可能是专名）
    seen: list[str] = []
    for w in sorted(set(words), key=len, reverse=True):
        if w not in seen:
            seen.append(w)
    return seen[:limit]


@dataclass
class Bundle:
    """给模型看的证据包。"""

    text: str
    chapters: list[int] = field(default_factory=list)
    truncated: bool = False
    #: 证据文本内每段窗口的 (章号, 起, 止)——用来把模型给的引文位置反查回章号
    spans: list[tuple[int, int, int]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text


class EvidenceIndex:
    """按章号检索关键词并取原文窗口；建一次索引，全书复用。"""

    def __init__(self, chapters: Mapping[int, str]):
        self._chapters = {int(k): v or "" for k, v in chapters.items()}

    def __len__(self) -> int:
        return len(self._chapters)

    def find_mentions(self, keywords: Sequence[str], *, after: int, limit: int = 8) -> list[int]:
        """返回提到任一关键词的章号（只取 aset``after`` 之后的，按先后排）。"""
        if not keywords:
            return []
        hits: list[int] = []
        for ch in sorted(self._chapters):
            if ch <= after:
                continue
            text = self._chapters[ch]
            if any(k in text for k in keywords):
                hits.append(ch)
                if len(hits) >= limit:
                    break
        return hits

    def chapter_text(self, chapter: int) -> str:
        """整章原文。定位锚点必须用整章——窗口只够展示，不够搜索。"""
        return self._chapters.get(int(chapter), "")

    def window(self, chapter: int, needle: str = "", *, radius: int = WINDOW_RADIUS) -> str:
        """取该章下一段窗口：优先围绕关键词位置，找不到就从章首取。"""
        text = self._chapters.get(chapter, "")
        if not text:
            return ""
        pos = text.find(needle) if needle else -1
        start = max(0, (pos if pos >= 0 else 0) - radius)
        end = min(len(text), (pos if pos >= 0 else 0) + radius)
        snippet = text[start:end].strip()
        head = "…" if start > 0 else ""
        tail = "…" if end < len(text) else ""
        return f"{head}{snippet}{tail}"

    def bundle_for(
        self,
        *,
        anchor_chapter: int,
        anchor_quote: str,
        hint_texts: Iterable[str] = (),
        max_windows: int = MAX_WINDOWS,
        radius: int = WINDOW_RADIUS,
    ) -> Bundle:
        """埋设处 + 后续命中处，拼成有界证据包。"""
        keywords = _keywords_for(anchor_quote, *hint_texts)
        blocks: list[str] = []
        chapters: list[int] = []
        spans: list[tuple[int, int, int]] = []
        cursor = 0

        def _append(chapter: int, body: str, label: str) -> None:
            nonlocal cursor
            if not body:
                return
            chunk = f"{label}\n{body}"
            start = cursor
            blocks.append(chunk)
            chapters.append(chapter)
            spans.append((chapter, start, start + len(chunk)))
            cursor = start + len(chunk) + 2  # "\n\n" 分隔

        _append(anchor_chapter, self.window(anchor_chapter, anchor_quote, radius=radius),
                f"【第 {anchor_chapter} 章（埋设处）】")

        dropped = 0
        for ch in self.find_mentions(keywords, after=anchor_chapter, limit=max_windows):
            if len(chapters) >= max_windows + 1:
                break
            # 取窗要对准**真正命中的那个词**：用 keywords[0] 会在它没出现时退化成章首，
            # 于是"章节被选中、答案却没进包"——实测就是这样漏掉章末答案的。
            hit_word = next((w for w in keywords if w and w in self.chapter_text(ch)), "")
            if not hit_word:
                dropped += 1
                continue
            _append(ch, self.window(ch, hit_word, radius=radius), f"【第 {ch} 章】")

        text = "\n\n".join(blocks)
        return Bundle(
            text=text,
            chapters=chapters,
            # 有命中章被跳过、或命中数超过能展示的上限，都算截断
            truncated=bool(dropped) or len(chapters) > max_windows,
            spans=spans,
        )
