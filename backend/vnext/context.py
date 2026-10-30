"""复检上下文取材：把一处引用扩成"可读的、边界诚实的"上下文窗口。

为什么不能"前后各加 300 字"了事：证据经常靠**整句**才成立——"他并没有回答"单独看是
沉默，连上前一句"你是不是杀了人"才是拒绝。按固定字节切，会把句子切成两半，
于是模型读到半句话就下结论，而材料不足在数据里看不出来。

因此这里的规则是：**先扩到句子边界，再按目标补足**，并把三件事如实记下来：

* ``context_start/end``：实际取材范围（码点）；
* ``chars_used``：实际给了多少字；
* ``truncated`` 与 ``reason``：是否因**硬上限**被截断。

「到达章节真实边界」不算截断——那是材料就到这儿了。两者在结论上不同：
前者意味着"还有材料但没给"，必须让下游标记为证据不足。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 取材规则的版本。改"怎么扩"就 +1，它参与依赖指纹。
CONTEXT_STRATEGY_VERSION = 1

#: 目标前后文长度（码点）。不是硬上限——够到句子边界就停。
PRE_TARGET_CHARS = 300
POST_TARGET_CHARS = 300

#: 硬上限：无论句子多长，单次上下文不超过它。超过即视为"材料不完整"。
HARD_MAX_CHARS = 2000

#: 句读边界。换行也算——章节常用空行分段。
SENTENCE_DELIMITERS = "。！？；…\n\r"


@dataclass(frozen=True)
class ContextWindow:
    """一处引用的上下文及其完整性说明。"""

    start: int
    end: int
    text: str
    chars_used: int
    truncated: bool = False
    reason: str = ""

    @property
    def complete(self) -> bool:
        """材料是否完整。硬上限截断 = 不完整；到章节尽头 = 完整。"""
        return not self.truncated


def _sentence_start(text: str, index: int) -> int:
    """从 ``index`` 往前找到句子起点（上一个句读之后）。"""
    for cursor in range(index - 1, -1, -1):
        if text[cursor] in SENTENCE_DELIMITERS:
            return cursor + 1
    return 0


def _sentence_end(text: str, index: int) -> int:
    """从 ``index`` 往后找到句子终点（含该句读）。"""
    for cursor in range(index, len(text)):
        if text[cursor] in SENTENCE_DELIMITERS:
            return cursor + 1
    return len(text)


def build_context(
    chapter_text: str,
    *,
    span_start: int,
    span_end: int,
    pre_target: int = PRE_TARGET_CHARS,
    post_target: int = POST_TARGET_CHARS,
    hard_max: int = HARD_MAX_CHARS,
) -> ContextWindow:
    """为 ``[span_start, span_end)`` 构造上下文窗口。

    顺序固定：① 句子边界 → ② 补足目标 → ③ 硬上限收缩 → ④ **再对齐句界（只在放得下时）**。

    第 ④ 步是 2026-10-30 复审补的：只按字符数扩张会让窗口首尾停在句子中间，而那本身就是
    一种截断，却被标成 ``truncated=False``。给模型半句话比少给半句更糟——它会以为那就是
    原文的全部。因此最后一定检查边界是否落在句中，落在句中就如实标记 ``SENTENCE_SPLIT``。

    硬上限永远优先：宁可窗口边界在句中（并说明），也不能超出预算。
    """
    if not (0 <= span_start < span_end <= len(chapter_text)):
        raise IndexError(
            f"引用区间越界: [{span_start}, {span_end}) 正文长度 {len(chapter_text)}"
        )

    # ① 句子边界
    start = _sentence_start(chapter_text, span_start)
    end = _sentence_end(chapter_text, span_end)

    # ② 补足目标长度（不越过正文边界）
    start = max(0, min(start, span_start - pre_target))
    end = min(len(chapter_text), max(end, span_end + post_target))

    truncated = False
    reason = ""

    # ③ 硬上限收缩（引用本身优先）
    if span_end - span_start >= hard_max:
        # 引用就超预算：只能保住引用，上下文全让给它，并如实说明材料不完整
        return ContextWindow(
            start=span_start,
            end=span_end,
            text=chapter_text[span_start:span_end],
            chars_used=span_end - span_start,
            truncated=True,
            reason="QUOTE_EXCEEDS_HARD_MAX",
        )

    if end - start > hard_max:
        spare = hard_max - (span_end - span_start)
        before = max(0, spare // 2)
        start = max(0, span_start - before)
        end = min(len(chapter_text), start + hard_max)
        start = max(0, min(start, end - hard_max))
        truncated = True
        reason = "HARD_BUDGET"

    # ④ 再对齐句界——只在**不超预算**时向外扩，否则保持并标记边界落点
    if start > 0:
        anchored = _sentence_start(chapter_text, start)
        if anchored < start and (end - anchored) <= hard_max:
            start = anchored
    aligned_end = _sentence_end(chapter_text, end) if end > 0 else end
    if aligned_end > end and (aligned_end - start) <= hard_max:
        end = min(len(chapter_text), aligned_end)

    # 边界落在句中 = 这是一次真正的截断，必须说出来（预算够也不能假装完整）
    head_cut = start > 0 and chapter_text[start - 1] not in SENTENCE_DELIMITERS
    tail_cut = end < len(chapter_text) and chapter_text[end - 1] not in SENTENCE_DELIMITERS
    if head_cut or tail_cut:
        truncated = True
        if not reason:
            reason = "SENTENCE_SPLIT"

    if not (start <= span_start and end >= span_end):
        # 无论如何必须覆盖引用：这句在正常路径下不可达，留作断言式兜底
        start, end = span_start, span_end
        truncated = True
        reason = "QUOTE_EXCEEDS_HARD_MAX"

    text = chapter_text[start:end]
    return ContextWindow(
        start=start,
        end=end,
        text=text,
        chars_used=len(text),
        truncated=truncated,
        reason=reason,
    )


def context_covers(window: ContextWindow, *, span_start: int, span_end: int) -> bool:
    """窗口是否完整覆盖引用区间（否则这条证据不可核对）。"""
    return window.start <= span_start and span_end <= window.end
