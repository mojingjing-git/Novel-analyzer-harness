"""读取独立裁决结果：容忍代理输出中的中文引号、尾随逗号、单引号字符串。

代理写出的 JSON 常带两类偏差：把英文引号写成中文单引号（'...'）、
末尾多一个逗号。这里做保守修复后解析，并逐条做结构校验。
校验不通过的条目直接丢弃——不允许污染标签集。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REQUIRED = (
    "work_id",
    "arm",
    "candidate_id",
    "answer_correct",
    "failure_category",
)


def _repair(text: str) -> str:
    """保守修复：中文单引号 → 双引号；尾随逗号。"""
    # 只在字符串值内部出现的 '...' 视为引号包裹
    text = re.sub(r"(?<![\w\\])'([^'\n]*?)'(?=[,\s\]}])", r'"\1"', text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def _extract_objects(text: str) -> list[dict]:
    """从可能不完整的 JSON 数组里逐个提取对象，逐个校验。"""
    objs: list[dict] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                chunk = text[start : i + 1]
                try:
                    obj = json.loads(chunk)
                    if isinstance(obj, dict):
                        objs.append(obj)
                except json.JSONDecodeError:
                    pass
                start = -1
    return objs


def load_review_file(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            rows = [data]
        else:
            rows = []
    except json.JSONDecodeError:
        rows = _extract_objects(_repair(raw))
    return [r for r in rows if all(k in r for k in REQUIRED)]


def main() -> None:
    import sys

    tdir = Path(
        "workspace/bc-evaluation/t4-trial-20261106/t4-closeout-review-tasks"
    )
    total = 0
    for p in sorted(tdir.glob("*.closeout.review.json")):
        rows = load_review_file(p)
        total += len(rows)
        print(f"{p.name}: {len(rows)} valid rows")
        for r in rows:
            print(
                f"   {r.get('work_id')}/{r.get('arm')}/{r.get('candidate_id')}"
                f" -> {r.get('answer_correct')} / {r.get('failure_category')}"
            )
    print("total:", total)


if __name__ == "__main__":
    main()
