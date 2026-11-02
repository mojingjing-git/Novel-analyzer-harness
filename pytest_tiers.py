"""测试分档原语（5 档，累积式：T0 ⊂ T1 ⊂ T2 ⊂ T3 ⊂ T4）

与 :mod:`pytest_isolation` 同级独立成模块的原因相同：仓库根与 ``tests/`` 下各有一个
``conftest.py``，``import conftest`` 会随收集范围解析到不同模块对象；分档规则必须
只有一份，测试与脚本都 ``import pytest_tiers`` 取同一份状态。

档位语义（验证强度递增，供 Agent 按需启用）：

===== ====== =============================================================
档    耗时   内容
===== ====== =============================================================
T0    ~5s    冒烟：backend 全模块导入 + 核心纯函数（容错 / 错误分类 / 账本）
T1    ~20s   T0 + 默认档单测（backend/tests 下未列名的文件，实测均 <1s）
T2    ~30s   T1 + 中速文件（0.9s–2.5s）+ 顶层 tests/（含 Gutenberg 语料基线）
T3    ~110s  T2 + 慢速文件（真实重试/退避链路）＝ pytest 全量
T4    ~2min  T3 + 前端 vitest ＝ 全仓验证（由 scripts/run_tests.py 编排）
===== ====== =============================================================

**文件 → 档位**按 :data:`SLOW_FILES` → :data:`MEDIUM_FILES` → 顶层 ``tests/`` →
:data:`SMOKE_FILES` 的顺序匹配，未命中一律落默认档 T1。新测试文件自动落 T1；
实测超过 1 秒的文件必须移进 :data:`MEDIUM_FILES`（约定见 agent.md），否则 T1 会
被悄悄拖慢。清单有效性由 ``backend/tests/test_tier_assignment.py`` 守护。
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

TIERS = 5

# T3：慢速文件（真实时间流逝的重试 / 退避 / 超时链路，2026-11 实测 >20s）。
SLOW_FILES = (
    "backend/tests/test_streaming.py",   # 53s：流式 + 两级重试链
    "backend/tests/test_llm_mock.py",    # 21s：LLM mock 全链路
)

# T2：中速文件（2026-11 实测 0.9s–2.5s：文件 IO / 生命周期矩阵 / mock 流水线批处理）。
MEDIUM_FILES = (
    "backend/tests/test_splitter_silent_failure.py",
    "backend/tests/test_runtime_isolation.py",
    "backend/tests/test_service_lifecycle_matrix.py",
    "backend/tests/test_vnext_runner_analysis.py",
    "backend/tests/test_pipeline_p2_batch.py",
    "backend/tests/test_vnext_results_queue.py",
)

# T0：冒烟清单（核心纯函数，改一行也值得先跑一遍的那批）。
# 只收 <0.3s 且承载核心契约的文件；-1s 量级的进 MEDIUM_FILES。
SMOKE_FILES = (
    "backend/tests/test_smoke_imports.py",          # backend 全模块导入（本文件配套）
    "backend/tests/test_tier_assignment.py",        # 本文件清单的守护用例
    "backend/tests/test_models.py",
    "backend/tests/test_json_repair.py",
    "backend/tests/test_json_parse_boundaries.py",
    "backend/tests/test_error_types.py",
    "backend/tests/test_error_taxonomy.py",
    "backend/tests/test_redact.py",
    "backend/tests/test_text_units_identity.py",
    "backend/tests/test_book_name_rules.py",
    "backend/tests/test_char_name_suffix_source.py",
    "backend/tests/test_foreshadow_type_normalize.py",
    "backend/tests/test_foreshadow_ledger.py",
    "backend/tests/test_foreshadow_composite_score.py",
    "backend/tests/test_retry_decision_u2.py",
    "backend/tests/test_text_encoding.py",
    "backend/tests/test_moderation.py",
    "backend/tests/test_moderation_threshold.py",
    "backend/tests/test_queue_item_validation.py",
    "backend/tests/test_vnext_constants_alignment.py",
    "backend/tests/test_vnext_schema_keys.py",
    "backend/tests/test_kb_fingerprint.py",
)

# 顶层 tests/（切分器边界用例 + Gutenberg 语料基线）整体落 T2。
TOP_LEVEL_TESTS_DIR = "tests/"

DEFAULT_TIER = 1


def assign_tier(rel_path: str) -> int:
    """按仓库相对路径（``/`` 分隔）返回所属档位。顺序匹配，先命中先赢。"""
    p = rel_path.replace("\\", "/")
    for table, tier in ((SLOW_FILES, 3), (MEDIUM_FILES, 2), (SMOKE_FILES, 0)):
        if p in table:
            return tier
    if p.startswith(TOP_LEVEL_TESTS_DIR):
        return 2
    return DEFAULT_TIER


def match_files(pattern: str) -> tuple[str, ...]:
    """返回清单里匹配 glob 模式的条目（守护测试用它核对清单拼写）。"""
    return tuple(f for f in (*SLOW_FILES, *MEDIUM_FILES, *SMOKE_FILES)
                 if fnmatch.fnmatch(f, pattern))


def _rel(item_path: Path, repo_root: Path) -> str:
    try:
        return item_path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return item_path.as_posix()


def pytest_addoption(parser) -> None:
    group = parser.getgroup("tiers")
    group.addoption(
        "--tier", type=int, default=None, choices=range(TIERS),
        metavar=f"0-{TIERS - 1}",
        help="只跑档位 <= N 的测试（累积式：T0 ⊂ T1 ⊂ T2 ⊂ T3；规则见 pytest_tiers.py）",
    )


def pytest_collection_modifyitems(config, items) -> None:
    """给每条用例打 ``tier{n}`` marker；命令行给了 --tier 时把超档用例 deselect。"""
    repo_root = Path(__file__).resolve().parent
    max_tier = config.getoption("--tier")
    keep, drop = [], []
    for item in items:
        tier = assign_tier(_rel(item.path, repo_root))
        item.add_marker(f"tier{tier}")
        if max_tier is None or tier <= max_tier:
            keep.append(item)
        else:
            drop.append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
