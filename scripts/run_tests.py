#!/usr/bin/env python
"""统一测试入口：按档位（--tier 0-4）或改动相关性（--related）选测试。

档位（累积式，T0 ⊂ T1 ⊂ T2 ⊂ T3 ⊂ T4；文件→档位规则见 pytest_tiers.py）：

    --tier 0   冒烟（~5s）：backend 全模块导入 + 核心纯函数
    --tier 1   快速单测（~20s）：T0 + 默认档单测（backend/tests 未列名文件）
    --tier 2   标准门禁（~30s）：T1 + 中速文件 + 顶层 tests/（含 Gutenberg 语料基线）
    --tier 3   pytest 全量（~110s）：T2 + 慢速重试链路（test_streaming / test_llm_mock）
    --tier 4   全仓验证（~2min）：T3 + 前端 vitest（33 个测试文件）

**为什么需要这个脚本**

1. **SSL context 缓存**（见 `pytest_ssl_cache.py`）：httpx 每次建客户端都要
   `ssl.create_default_context(cafile=certifi.where())`，本机实测 3.8 秒/次；
   而全量测试有 78 处 `LLMClient(...)` 构造 ⇒ 750 秒里绝大部分是它，
   不是测试逻辑慢。缓存后每个客户端 0.001 秒。

2. **并行**（`pytest-xdist`）：`-n auto`。但 `conftest.py` 顶层会把 cwd 切到
   会话隔离根，xdist worker 继承这个 cwd 后**找不到 `pytest.ini`**，
   `testpaths` 解析失效 ⇒ "no tests ran"。所以这里一律用**绝对路径** +
   显式 `-c` 指定 ini，绕开 cwd 依赖。

3. **对账**：跑前先 collect 数一遍预期用例数、跑后核对 passed 数——并行/过滤
   一旦静默漏掉整份文件，照样报"全绿"，比慢更糟。`--tier` / `--related` 的
   过滤同样参与对账（预期数按过滤后的收集结果算）。

**用法**

    python scripts/run_tests.py                     # 全仓验证 = T3 + 前端（推荐收口用）
    python scripts/run_tests.py --tier 1            # 按档位跑（Agent 迭代期自检）
    python scripts/run_tests.py --related           # 只跑与本次改动相关的文件
    python scripts/run_tests.py --related --tier 2  # 相关集 ∩ 档位过滤
    python scripts/run_tests.py --list-tiers        # 查看各档文件/用例分布
    python scripts/run_tests.py --tier 0 -n 2       # 指定并发数
    python scripts/run_tests.py --no-frontend       # T4 跳过前端 vitest
    python scripts/run_tests.py -- -k recheck       # 透传 pytest 参数

**注意**：并行下各 worker 各自有一份隔离根，互不干扰。若某个用例依赖全局
单例状态而并行不稳，用 `--serial` 复现。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIFILE = ROOT / "pytest.ini"
TEST_DIRS = [ROOT / "backend" / "tests", ROOT / "tests"]
FRONTEND_DIR = ROOT / "frontend"
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/（pytest_related）
sys.path.insert(0, str(ROOT))                             # 仓库根（pytest_tiers）

import pytest_related  # noqa: E402  (同目录脚本，用于 --related 的选择逻辑)
import pytest_tiers  # noqa: E402

FRONTEND_TIMEOUT = 600  # vitest 冷启动 + 33 个文件，10 分钟上限


def _has_xdist() -> bool:
    try:
        import xdist  # noqa: F401
    except ImportError:
        return False
    return True


def _has_timeout_plugin() -> bool:
    try:
        import pytest_timeout  # noqa: F401
    except ImportError:
        return False
    return True


def _collect_file_counts(paths: list[str], extra: list[str]) -> dict[str, int]:
    """collect 一遍，返回 测试文件 → 用例数。路径按仓库相对 posix 形式给出。"""
    cmd = [sys.executable, "-m", "pytest", "-o", "addopts=", "-c", str(INIFILE),
           "--collect-only", "-q", "--no-header", "-p", "no:cacheprovider", *paths, *extra]
    out = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    counts: dict[str, int] = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(("=", "ERROR", "FAILED")):
            continue
        m = re.match(r"^(\d+)\s+tests? collected", line)
        if m:
            counts["_total"] = int(m.group(1))
            break
        if "::" in line:
            path = line.split("::", 1)[0]
            counts[path] = counts.get(path, 0) + 1
        elif ".py:" in line:
            head, tail = line.split(".py:", 1)
            path = head.strip() + ".py"
            m2 = re.search(r"(\d+)", tail)
            counts[path] = int(m2.group(1)) if m2 else 1
    return counts


def _expected_count(paths: list[str], extra: list[str]) -> int:
    """本该跑的用例总数：collect 一遍，跑完对账，防"静默漏跑仍报全绿"。"""
    counts = _collect_file_counts(paths, extra)
    if "_total" in counts:
        return counts["_total"]
    # 逐条式输出（老版 pytest / 空过滤）：文件计数相加
    total = sum(v for k, v in counts.items() if not k.startswith("_"))
    if not total and counts.get("_total") is None:
        for k, v in counts.items():
            if "::" in k:
                total += v
    return total


def _passed_count(log: str) -> int:
    m = re.search(r"(\d+)\s+passed", log)
    return int(m.group(1)) if m else 0


def _run_frontend() -> int:
    """前端 vitest（T4 的前端半场）。返回退出码。"""
    npm = shutil.which("npm")
    if not npm:
        print("[run_tests] [warn] 找不到 npm，跳过前端测试", file=sys.stderr)
        return 0
    cmd = [npm, "run", "test"]
    print(f"[run_tests] 前端：{' '.join(cmd)}（cwd={FRONTEND_DIR}）", flush=True)
    try:
        proc = subprocess.run(cmd, cwd=str(FRONTEND_DIR), timeout=FRONTEND_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"[run_tests] ❌ 前端测试超过 {FRONTEND_TIMEOUT}s 未结束", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print("[run_tests] ❌ 前端 vitest 失败", file=sys.stderr)
    else:
        print("[run_tests] ✅ 前端 vitest 通过")
    return proc.returncode


def _print_tiers(paths: list[str]) -> None:
    """打印各档的文件数与用例数分布（帮助选档）。--tier N 为累积过滤，一并给累计值。"""
    counts = _collect_file_counts(paths, [])
    if "_total" in counts:
        counts.pop("_total")
    by_tier: dict[int, tuple[int, int]] = {t: (0, 0) for t in range(pytest_tiers.TIERS)}
    for path, n in counts.items():
        tier = pytest_tiers.assign_tier(path.replace("\\", "/"))
        f, c = by_tier[tier]
        by_tier[tier] = (f + 1, c + n)
    desc = {
        0: "冒烟：backend 全模块导入 + 核心纯函数",
        1: "快速单测：T0 + 默认档（新文件自动落入）",
        2: "标准门禁：T1 + 中速文件 + 顶层语料基线",
        3: "pytest 全量：T2 + 慢速重试链路",
        4: "全仓：T3 + 前端 vitest（脚本编排，非 pytest 过滤）",
    }
    print(f"{'tier':<6}{'files':>7}{'cases':>8}{'cum.cases':>11}   description")
    cum_files = cum_cases = 0
    for t in range(pytest_tiers.TIERS):
        f, c = by_tier[t]
        cum_files += f
        cum_cases += c
        print(f"T{t:<5}{f:>7}{c:>8}{cum_cases:>11}   {desc[t]}")


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，打印 ✅/❌ 会 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass

    parser = argparse.ArgumentParser(
        description="统一测试入口（档位 / 相关性 / 并行 + SSL 缓存 + 对账）",
        epilog="档位规则见 pytest_tiers.py；-- 之后的参数原样透传给 pytest")
    parser.add_argument("--tier", type=int, default=None,
                        help="只跑档位 <= N 的测试（0 冒烟 / 1 快单测 / 2 门禁 / 3 pytest 全量 / 4 全仓+前端）")
    parser.add_argument("--related", action="store_true",
                        help="按本次 git 改动选择相关测试文件（与 --tier 可组合）")
    parser.add_argument("--base", default="HEAD", help="--related 的比较基准（默认 HEAD）")
    parser.add_argument("--list-tiers", action="store_true", help="打印各档文件/用例分布后退出")
    parser.add_argument("--no-frontend", action="store_true", help="T4 跳过前端 vitest")
    parser.add_argument("-n", "--workers", default="auto", help="并发进程数，默认 auto")
    parser.add_argument("--serial", action="store_true", help="串行跑（调试用）")
    parser.add_argument("--timeout", type=int, default=0, help="pytest-timeout 秒数，0=不设")
    parser.add_argument("--no-verify-count", action="store_true", help="跳过用例总数对账")
    parser.add_argument("paths", nargs="*", help="测试路径（与 --related 互斥，--related 优先）")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="透传给 pytest 的参数")
    args = parser.parse_args(argv)

    # 透传参数：-- 之后的内容原样给 pytest
    extra = [a for a in (args.pytest_args or []) if a != "--"]

    tier = args.tier
    if tier is not None and not 0 <= tier < pytest_tiers.TIERS:
        parser.error(f"--tier 必须在 0-{pytest_tiers.TIERS - 1}")

    # T4 是脚本编排（pytest 全量 + 前端），pytest 侧不过滤
    pytest_tier = None if tier in (None, 4) else tier

    paths = [str(Path(p).resolve()) for p in args.paths] if args.paths else \
            [str(p) for p in TEST_DIRS if p.exists()]
    if args.related:
        changed = pytest_related.changed_files(args.base)
        picked, notes = pytest_related.select(changed)
        for note in notes:
            print(f"[related] # {note}")
        if not picked:
            print("[related] 改动未命中任何测试文件（纯文档/配置改动？）")
            if tier in (None, 4):
                picked = [str(p) for p in TEST_DIRS if p.exists()]  # 收口语义退回全量
            else:
                return 0
        rel = [str(p) for p in picked]
        if args.paths:
            print("[related] [warn] 同时给了路径与 --related，以 --related 为准", file=sys.stderr)
        print(f"[related] 选中 {len(rel)} 个测试文件", flush=True)
        paths = [str(Path(p).resolve()) for p in rel]

    if args.list_tiers:
        _print_tiers(paths)
        return 0

    if pytest_tier is not None:
        extra = ["--tier", str(pytest_tier), *extra]

    cmd = [sys.executable, "-m", "pytest", "-o", "addopts=", "-q"]
    cmd += ["-c", str(INIFILE)]

    workers = 0 if args.serial else args.workers
    # 小档（冒烟/快单测）用例少，xdist 起 N 个 worker 各自做一遍隔离 + 全量收集，
    # 启动开销反而超过收益（实测 T0：串行 12s vs -n auto 42s）——默认串行。
    if pytest_tier is not None and pytest_tier <= 1 and args.workers == "auto":
        workers = 0
    if workers:
        if not _has_xdist():
            print("[warn] pytest-xdist 未安装，退回串行"
                  "（pip install pytest-xdist）", file=sys.stderr)
        else:
            cmd += ["-n", str(workers)]

    if args.timeout:
        if _has_timeout_plugin():
            cmd += ["--timeout", str(args.timeout)]
        else:
            print("[warn] pytest-timeout 未安装，忽略 --timeout"
                  "（pip install pytest-timeout）", file=sys.stderr)

    cmd += paths
    cmd += extra

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    expected = 0
    if not args.no_verify_count:
        expected = _expected_count(paths, ["--tier", str(pytest_tier)] if pytest_tier is not None else [])
        print(f"[run_tests] 预期用例数：{expected}", flush=True)

    print(f"[run_tests] cwd={ROOT}")
    print(f"[run_tests] {' '.join(cmd)}", flush=True)

    # 输出落文件再 tail：Windows 上直接管道 pytest 会挂死（见 agent.md 的环境坑）。
    # 文件名带 PID：两个实例并发时日志互不污染（否则对账会读到对方的汇总行）。
    log = ROOT / f".tmp_pytest_run_{os.getpid()}.log"
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT)
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in tail[-3:]:
        print(line)

    rc = proc.returncode
    if expected:
        got = _passed_count(log.read_text(encoding="utf-8", errors="replace"))
        if rc == 0 and got != expected:
            print(f"[run_tests] ❌ 对账失败：预期 {expected} 条，实测通过 {got} 条"
                  "（有文件被静默漏跑？）", file=sys.stderr)
            rc = 1
        if rc == 0:
            print(f"[run_tests] ✅ 对账通过：{got}/{expected} 条")

    # T4：pytest 全量之后接前端 vitest
    if tier == 4 and rc == 0 and not args.no_frontend:
        rc = _run_frontend()
    elif tier == 4 and args.no_frontend:
        print("[run_tests] --no-frontend：跳过前端 vitest")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
