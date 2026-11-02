"""守护 pytest_tiers.py 的分档清单

分档规则是"显式清单 + 默认档"：清单里的文件改名/删除后规则会变成静默死条目，
新文件默认落 T1。这里锁住三件事：

1. 清单条目指向的文件都真实存在；
2. ``assign_tier`` 的档位语义（慢/中/冒烟/顶层/默认）不被无意改动；
3. T0 冒烟档始终包含全模块导入用例（test_smoke_imports.py）。
"""
from __future__ import annotations

from pathlib import Path

import pytest_tiers
from pytest_tiers import (
    DEFAULT_TIER,
    MEDIUM_FILES,
    SLOW_FILES,
    SMOKE_FILES,
    assign_tier,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_tier_lists_point_to_existing_files() -> None:
    for rel in (*SLOW_FILES, *MEDIUM_FILES, *SMOKE_FILES):
        assert (REPO_ROOT / rel).is_file(), f"分档清单条目不存在: {rel}"


def test_tier_lists_are_disjoint() -> None:
    slow, medium, smoke = set(SLOW_FILES), set(MEDIUM_FILES), set(SMOKE_FILES)
    assert not slow & medium, f"SLOW/MEDIUM 重叠: {slow & medium}"
    assert not slow & smoke, f"SLOW/SMOKE 重叠: {slow & smoke}"
    assert not medium & smoke, f"MEDIUM/SMOKE 重叠: {medium & smoke}"


def test_assign_tier_semantics() -> None:
    for rel in SLOW_FILES:
        assert assign_tier(rel) == 3, rel
    for rel in MEDIUM_FILES:
        assert assign_tier(rel) == 2, rel
    for rel in SMOKE_FILES:
        assert assign_tier(rel) == 0, rel
    # 顶层 tests/（切分器边界 + Gutenberg 语料基线）整体落 T2
    assert assign_tier("tests/test_splitter_edge_cases.py") == 2
    assert assign_tier("tests/test_splitter_english_baseline.py") == 2
    # 未列名文件默认 T1
    assert assign_tier("backend/tests/test_anything_new.py") == DEFAULT_TIER == 1
    # 返回值域 0-4
    assert all(0 <= assign_tier(p) <= 4 for p in (*SLOW_FILES, *MEDIUM_FILES, *SMOKE_FILES))


def test_smoke_imports_stays_in_tier0() -> None:
    assert "backend/tests/test_smoke_imports.py" in SMOKE_FILES
