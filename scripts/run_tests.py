#!/usr/bin/env python
"""全量测试入口：并行跑，把 750 秒压到 80 秒左右。

**为什么需要这个脚本**

1. **SSL context 缓存**（见 `pytest_ssl_cache.py`）：httpx 每次建客户端都要
   `ssl.create_default_context(cafile=certifi.where())`，本机实测 3.8 秒/次；
   而全量测试有 78 处 `LLMClient(...)` 构造 ⇒ 750 秒里绝大部分是它，
   不是测试逻辑慢。缓存后每个客户端 0.001 秒。

2. **并行**（`pytest-xdist`）：`-n auto`。但 `conftest.py` 顶层会把 cwd 切到
   会话隔离根，xdist worker 继承这个 cwd 后**找不到 `pytest.ini`**，
   `testpaths` 解析失效 ⇒ "no tests ran"。所以这里一律用**绝对路径** +
   显式 `-c` 指定 ini，绕开 cwd 依赖。

**用法**

    python scripts/run_tests.py                 # 全量并行（默认）
    python scripts/run_tests.py -n 2            # 指定并发数
    python scripts/run_tests.py --serial        # 串行（调试用，~250 秒）
    python scripts/run_tests.py backend/tests/test_recheck_v2.py   # 指定路径
    python scripts/run_tests.py -- -k recheck   # 透传 pytest 参数

**注意**：并行下各 worker 各自有一份隔离根，互不干扰（1588 例全通过）。
若某个用例依赖全局单例状态而并行不稳，用 `--serial` 复现。
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
INI = ROOT / "pytest.ini"
TEST_DIRS = [ROOT / "backend" / "tests", ROOT / "tests"]


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


def _expected_count(paths: list[str]) -> int:
    """收集一次，拿到本该跑的用例总数。

    **为什么必须核对**：并行/切组一旦因路径或 cwd 问题**静默漏掉一批文件**，
    跑完照样报"全绿"——比慢更糟（另一路实现曾因过滤条件把 `test_error_*`
    整份漏掉 80 条却仍报"全组通过"）。所以这里先数一遍，跑完对账。
    """
    cmd = [sys.executable, "-m", "pytest", "-o", "addopts=", "-c", str(INI),
           "--collect-only", "-q", "--no-header", "-p", "no:cacheprovider", *paths]
    out = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    total = 0
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith(("=", "ERROR", "FAILED")):
            continue
        m = re.match(r"^(\d+)\s+tests? collected", line)
        if m:
            total = int(m.group(1))
            break
        if "::" in line:
            total += 1
    return total


def _passed_count(log: str) -> int:
    m = re.search(r"(\d+)\s+passed", log)
    return int(m.group(1)) if m else 0


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，打印 ✅/❌ 会 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass

    parser = argparse.ArgumentParser(description="全量测试入口（并行 + SSL 缓存）")
    parser.add_argument("-n", "--workers", default="auto", help="并发进程数，默认 auto")
    parser.add_argument("--serial", action="store_true", help="串行跑（调试用）")
    parser.add_argument("--timeout", type=int, default=0, help="pytest-timeout 秒数，0=不设")
    parser.add_argument("--no-verify-count", action="store_true", help="跳过用例总数对账")
    parser.add_argument("paths", nargs="*", help="测试路径，默认 backend/tests + tests")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="透传给 pytest 的参数")
    args = parser.parse_args(argv)

    # 透传参数：-- 之后的内容原样给 pytest
    extra = [a for a in (args.pytest_args or []) if a != "--"]

    cmd = [sys.executable, "-m", "pytest", "-o", "addopts=", "-q"]
    cmd += ["-c", str(INI)]

    workers = 0 if args.serial else args.workers
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

    paths = [str(Path(p).resolve()) for p in args.paths] if args.paths else \
            [str(p) for p in TEST_DIRS if p.exists()]
    cmd += paths
    cmd += extra

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    expected = 0
    if not args.no_verify_count:
        expected = _expected_count(paths)
        print(f"[run_tests] 预期用例数：{expected}", flush=True)

    print(f"[run_tests] cwd={ROOT}")
    print(f"[run_tests] {' '.join(cmd)}", flush=True)

    # 输出落文件再 tail：Windows 上直接管道 pytest 会挂死（见 agent.md 的环境坑）
    log = ROOT / ".tmp_pytest_run.log"
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=str(ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT)
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in tail[-3:]:
        print(line)

    if expected:
        got = _passed_count(log.read_text(encoding="utf-8", errors="replace"))
        if proc.returncode == 0 and got != expected:
            print(f"[run_tests] ❌ 对账失败：预期 {expected} 条，实测通过 {got} 条"
                  "（有文件被静默漏跑？）", file=sys.stderr)
            return 1
        if proc.returncode == 0:
            print(f"[run_tests] ✅ 对账通过：{got}/{expected} 条")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
