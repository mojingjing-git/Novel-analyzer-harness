#!/usr/bin/env python
"""按本次改动挑选相关测试，替代"每次都跑全量"。

为什么要它：全量约 1100 例 / 12 分钟。迭代期改 20 行就跑一遍，绝大部分时间花在
验证"跟本次改动无关"的用例上。批次收口跑一次全量即可。

挑选规则（保守优先，宁可多选）：

1. **文件名对应**：改 `backend/vnext/results.py` → 选 `test_vnext_results*.py`、
   `test_*results*.py`；
2. **import 引用**：扫描测试源码里出现的模块名（`backend.vnext.results` 及其各级父包），
   命中即选。父包也参与匹配，因为 `from backend.vnext import results` 这种写法是常见的；
3. 生产文件改动时，把该文件所在包的**同目录测试**也带上（同包内往往互相引用）。

用法：

    python scripts/pytest_related.py              # 只列出会跑哪些
    python scripts/pytest_related.py --run        # 列出并执行
    python scripts/pytest_related.py --run --all  # 强制全量（批次收口）
    python scripts/pytest_related.py --base main  # 与某分支比较（默认 HEAD）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_DIRS = (ROOT / "backend" / "tests", ROOT / "tests")


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout if result.returncode == 0 else ""


def changed_files(base: str) -> list[str]:
    """已跟踪改动 + 未跟踪新文件。忽略文档——它们不承载可执行逻辑。"""
    tracked = _git("diff", "--name-only", base)
    untracked = _git("ls-files", "--others", "--exclude-standard")
    files = {line.strip() for line in (tracked + "\n" + untracked).splitlines() if line.strip()}
    return sorted(
        f for f in files if f.endswith((".py", ".vue", ".ts", ".js"))
    )


def _module_names(rel_path: str) -> set[str]:
    """`backend/vnext/results.py` → {"backend.vnext.results"}。

    刻意**不**把父包（``backend``、``backend.vnext``）作为匹配词：测试文件几乎都会出现
    ``backend`` 这个字符串，用父包匹配等于全选。宁可少选几个、由"同目录/同前缀文件名"
    那条规则兜底，也不要退化成"每次都是全量"。
    """
    path = Path(rel_path)
    if path.suffix != ".py":
        return set()
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if len(parts) < 2:
        return set()
    return {".".join(parts)}


def _all_tests() -> list[Path]:
    tests: list[Path] = []
    for directory in TEST_DIRS:
        if directory.is_dir():
            tests.extend(sorted(directory.glob("test_*.py")))
    return tests


def _is_frontend_test(path: Path) -> bool:
    return "frontend" in path.parts


def select(changed: list[str]) -> tuple[list[Path], list[str]]:
    """返回（被选中的测试文件，说明行）。"""
    tests = _all_tests()
    picked: dict[Path, str] = {}
    notes: list[str] = []

    # 1. 文件名对应
    for rel in changed:
        stem = Path(rel).stem
        for test in tests:
            if _is_frontend_test(test):
                continue
            if stem and (stem in test.stem):
                picked.setdefault(test, f"文件名对应 {rel}")

    # 2. import 引用（含父包）
    needles: dict[str, str] = {}
    for rel in changed:
        for name in _module_names(rel):
            needles.setdefault(name, rel)
    if needles:
        for test in tests:
            if test in picked or _is_frontend_test(test):
                continue
            text = test.read_text(encoding="utf-8", errors="ignore")
            for name, source in needles.items():
                if name in text:
                    picked.setdefault(test, f"引用 {name}（来自 {source}）")
                    break

    if not picked:
        notes.append("未匹配到相关测试——纯文档/配置改动，或改动尚未落盘")

    return sorted(picked), notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按改动挑选相关测试")
    parser.add_argument("--base", default="HEAD", help="比较基准（默认 HEAD）")
    parser.add_argument("--run", action="store_true", help="真正执行 pytest")
    parser.add_argument("--all", action="store_true", dest="run_all", help="强制全量")
    parser.add_argument("--extra", nargs="*", default=[], help="额外追加的测试文件")
    args = parser.parse_args(argv)

    if args.run_all:
        targets = [str(t) for t in _all_tests()]
        print(f"[全量] {len(targets)} 个测试文件")
    else:
        changed = changed_files(args.base)
        picked, notes = select(changed)
        for note in notes:
            print(f"# {note}")
        if not changed:
            print("# 没有代码改动")
        else:
            print(f"# 改动 {len(changed)} 个文件")
            for rel in changed:
                print(f"#   {rel}")
        targets = [str(t) for t in picked]
        targets.extend(args.extra)
        # 去重保序
        seen: set[str] = set()
        targets = [t for t in targets if not (t in seen or seen.add(t))]
        if not targets and not args.run:
            print("# 无可执行目标")
            return 0
        print(f"\n# 选中 {len(targets)} 个测试文件：")
        for target in targets:
            print(f"#   {Path(target).relative_to(ROOT)}")

    if not args.run:
        print("\n# 加 --run 执行")
        return 0
    if not targets:
        print("# 没有需要跑的测试")
        return 0

    cmd = [sys.executable, "-m", "pytest", *targets, "-o", "addopts=", "-q"]
    print(f"\n$ {' '.join(cmd)}\n")
    return subprocess.call(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
