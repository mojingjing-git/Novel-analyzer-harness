"""S-1：模型回包键名只允许有一个来源。

2026-10-31 的事故值得留个用例：Prompt 里手写 `supporting_evidence_id` 而解析器读
`evidence_id`，连续两轮真实调用（36 次、约 109k token）全部被判 `NO_EVIDENCE_ID` /
`NO_SUPPORTING_QUOTE`。那不是模型不行，是两边各写一份字段名。

这里做**双向核对**：
1. `render_recheck_schema()` 渲染出的 JSON，其键集合必须与 `RECHECK_MODEL_KEYS` 完全一致；
2. `parse_recheck` 源码里实际 `payload.get(...)` / `counter.get(...)` 读到的键，
   必须与 `RECHECK_MODEL_KEYS` 完全一致。

第 2 条是源码级断言：只要有人加了读取却忘了加常量（或反之），用例立刻红。
"""

from __future__ import annotations

import inspect
import json
import re

from backend.vnext import resolution


def _flatten(prefix: str, value: object, out: set[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            _flatten(f"{prefix}{key}.", nested, out)
    else:
        out.add(prefix.rstrip("."))


def _schema_keys() -> set[str]:
    payload = json.loads(resolution.render_recheck_schema())
    out: set[str] = set()

    def walk(prefix: str, node: dict) -> None:
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(f"{path}.", value)
            else:
                out.add(path)

    walk("", payload)
    return out


def _constant_keys() -> set[str]:
    return set(resolution.iter_model_keys())


def _read_keys() -> set[str]:
    """从 `parse_recheck` 源码里抽出它实际读取的键。

    `payload.get("x")` → `x`；`counter.get("y")` → `counter_search.y`
    （counter 是 `payload["counter_search"]` 的局部名）。
    """
    source = inspect.getsource(resolution.parse_recheck)
    found: set[str] = set()
    for name in re.findall(r"payload\.get\(\s*\"([^\"]+)\"", source):
        found.add(name)
    for name in re.findall(r"counter\.get\(\s*\"([^\"]+)\"", source):
        found.add(f"counter_search.{name}")
    # `payload.get("counter_search")` 取的是容器本身，不是叶子键；常量里它表示为嵌套映射
    containers = {
        key for key, value in resolution.RECHECK_MODEL_KEYS.items() if isinstance(value, dict)
    }
    return found - containers


def test_schema_render_matches_constant() -> None:
    assert _schema_keys() == _constant_keys()


def test_parser_reads_exactly_the_declared_keys() -> None:
    assert _read_keys() == _constant_keys()


def test_nested_counter_keys_are_declared() -> None:
    assert "counter_search.performed" in _constant_keys()
    assert "counter_search.checked_evidence_ids" in _constant_keys()
    assert "counter_search.counter_quotes" in _constant_keys()


def test_rendered_schema_is_valid_json_with_placeholders() -> None:
    rendered = resolution.render_recheck_schema()
    payload = json.loads(rendered)
    assert payload["candidate_id"] == "<candidate_id>"
    assert set(payload["counter_search"]) == {
        "performed",
        "checked_evidence_ids",
        "counter_quotes",
    }
