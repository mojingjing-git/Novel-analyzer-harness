"""章节范围与列表规格解析工具（供 Web API、CLI 与调度层共用）。"""

from __future__ import annotations

import re
from typing import Collection, List, Optional, Sequence, Union

_SPLIT_RE = re.compile(r"[,，;\s]+")
_RANGE_RE = re.compile(r"^(\d+)\s*(?:-|\.\.)\s*(\d+)$")


def parse_chapter_spec(
    spec: Union[str, Sequence[int]],
    *,
    available_chapters: Optional[Collection[int]] = None,
    max_range_span: int = 10000,
) -> List[int]:
    """解析章节号规格为升序去重的正整数列表。

    支持：
    - 字符串：``"40"``、``"37-40, 55"``、``"37..40，55"``（支持逗号、中文逗号、分号、空白分隔）
    - 整数序列：``[37, 38, 39, 40, 55]``（严格拒绝 ``bool``）

    Raises:
        ValueError: 格式非法、范围倒置/过大、空列表或包含不存在的章节号。
    """
    if isinstance(spec, bool):
        raise ValueError("章节规格不能为布尔值")

    chapters: List[int] = []
    if isinstance(spec, str):
        raw = spec.strip()
        if not raw:
            raise ValueError("章节规格不能为空")
        tokens = [tok.strip() for tok in _SPLIT_RE.split(raw) if tok.strip()]
        if not tokens:
            raise ValueError("章节规格不能为空")
        for tok in tokens:
            m = _RANGE_RE.match(tok)
            if m:
                start = int(m.group(1))
                end = int(m.group(2))
                if start <= 0 or end <= 0:
                    raise ValueError(f"章节号必须为正整数: {tok}")
                if start > end:
                    raise ValueError(f"章节范围起止倒置: {tok}")
                span = end - start + 1
                if span > max_range_span:
                    raise ValueError(f"单段章节范围过大（{span} > {max_range_span}）: {tok}")
                chapters.extend(range(start, end + 1))
                continue
            if re.fullmatch(r"\d+", tok):
                num = int(tok)
                if num <= 0:
                    raise ValueError(f"章节号必须为正整数: {tok}")
                chapters.append(num)
                continue
            raise ValueError(f"无法识别的章节规格片段: {tok}")
    elif isinstance(spec, Sequence):
        for item in spec:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"章节列表元素必须为正整数: {item!r}")
            if item <= 0:
                raise ValueError(f"章节号必须为正整数: {item}")
            chapters.append(int(item))
    else:
        raise ValueError("章节规格必须为字符串或正整数列表")

    if not chapters:
        raise ValueError("未解析出任何有效章节")

    unique_sorted = sorted(set(chapters))
    if len(unique_sorted) > max_range_span:
        raise ValueError(f"章节总数超过上限 ({len(unique_sorted)} > {max_range_span})")

    if available_chapters is not None:
        avail_set = {int(c) for c in available_chapters}
        missing = [c for c in unique_sorted if c not in avail_set]
        if missing:
            preview = ", ".join(str(c) for c in missing[:10])
            suffix = f" 等共 {len(missing)} 章" if len(missing) > 10 else ""
            raise ValueError(f"以下章节不存在于 blocks/ 正文目录中: {preview}{suffix}")

    return unique_sorted
