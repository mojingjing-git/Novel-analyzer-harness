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

import math
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


def _segment(text: str) -> list[str]:
    """按标点/空白切段，只留含中文或字母的段。"""
    parts = re.split(r"[\s，。！？；：、, .!?;:\"“”\'（）()【】\[\]—…·]+", text or "")
    return [p for p in parts if p and re.search(r"[\u4e00-\u9fffA-Za-z]", p)]


def _keywords_for(*texts: str, max_terms: int | None = None) -> list[str]:
    """从锚点/问题/描述里提取**检索词条**。

    这里曾经直接返回整句（"铜匣底下那层是空的"），结果只有当某一章原封不动
    包含这整句时才算命中——等于检索没做。改成按标点切段后再取 2~4 字片段，
    长词优先，交给 ``find_mentions`` 按命中权重排序。
    """
    buckets: dict[int, list[str]] = {4: [], 3: [], 2: []}
    seen: set[str] = set()
    for text in texts:
        for seg in _segment(text):
            for size in (4, 3, 2):
                for i in range(0, max(1, len(seg) - size + 1)):
                    piece = seg[i : i + size]
                    if len(piece) < size or piece in _STOPWORDS or piece in seen:
                        continue
                    seen.add(piece)
                    buckets[size].append(piece)
    # 分层取：长词有区分度，但**不能把短词全挤掉**——"动过"这种 2 字片段
    # 往往是唯一能在"换了说法"的回收句里命中的线索（实测就是这样拿到第 9 章的）。
    terms: list[str] = []
    for size in (4, 3, 2):
        terms.extend(buckets[size])
    # 默认不截断：检索阶段要尽可能多的线索，真正该丢的是"到处都出现"的词，
    # 那由 find_mentions 按文档频率过滤（截断会把"动过"这类关键 2 字片段挤掉）。
    return terms if max_terms is None else terms[:max_terms]


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

    def segments(self) -> list[tuple[int, str]]:
        """(章号, 该段正文)。

        证据必须**分段**给模型：一条伏笔的埋设处在第 2 章、答案在第 9 章，
        若把整包当成一份证据，模型只能回填埋设处那个 evidence_id，于是
        "引文落在被点名证据里"这条门禁永远过不了（实测正是如此）。
        """
        return [(chapter, self.text[start:end].strip()) for chapter, start, end in self.spans]


class EvidenceIndex:
    """按章号检索关键词并取原文窗口；建一次索引，全书复用。"""

    def __init__(self, chapters: Mapping[int, str]):
        self._chapters = {int(k): v or "" for k, v in chapters.items()}

    def __len__(self) -> int:
        return len(self._chapters)

    def find_mentions(
        self, keywords: Sequence[str], *, after: int, limit: int = 8
    ) -> list[int]:
        """返回命中关键词的章号（只在 ``after`` 之后找），按命中权重降序。

        权重 = 命中词长度之和；同时过滤掉"超过三成章节都有"的常见词——
        否则"底下""的人"这类片段会把每一章都选成候选。
        """
        if not keywords:
            return []
        total = max(1, len(self._chapters))
        weights: dict[str, float] = {}
        for term in keywords:
            df = sum(1 for t in self._chapters.values() if term in t)
            if not df:
                continue  # 一次都没出现 = 用不上
            # 平滑加权：到处都出现的词区分度低但不该直接丢弃——小语料里"唯一线索词
            # 恰好每章都有"很常见，全丢会导致检索直接返回空（实测踩过）。
            weights[term] = len(term) * math.log((total + 1) / (df + 0.5))
        if not weights:
            return []
        scored: list[tuple[float, int]] = []
        for ch, text in self._chapters.items():
            if ch <= after:
                continue
            score = sum(w for term, w in weights.items() if term in text)
            if score:
                scored.append((score, ch))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [ch for _score, ch in scored[:limit]]

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
