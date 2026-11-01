"""把后端测试切成 N 份并行跑：不需要 pytest-xdist。

为什么不用 xdist：本仓库的 `pytest_isolation` 会在导入期 chdir 到临时目录并按
testpaths 建同名占位目录，xdist 每个 worker 都这么做一遍，主进程就看不到用例了
（实测 `-n 8` 直接 "no tests ran"）。改成分文件切组、各自起独立进程，
每条进程的隔离互不干扰。

用法：
    python scripts/pytest_split.py            # 默认 4 组
    python scripts/pytest_split.py -n 8       # 8 组
    python scripts/pytest_split.py -n 4 -q    # 静默（只打汇总）

退出码：全组通过为 0，否则 1。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collect_files() -> list[tuple[Path, int]]:
    """用 pytest 自己收集，避免手写 glob 漏掉 testpaths 之外的目录。"""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # pytest 9 的 `--collect-only -q` 每行是 "路径.py: 用例数"（老版本是 "路径.py::用例"），
    # 两种都要认，否则切组会静默拿到空列表。
    files: list[tuple[Path, int]] = []
    seen: set[str] = set()
    for line in out.stdout.splitlines():
        line = line.strip()
        # 只跳过 pytest 的汇总/报错行。曾经用 "error" in line 一刀切，
        # 结果把 test_error_taxonomy.py / test_error_types.py 这类**文件名带 error**
        # 的用例整份漏掉（80 条），切组跑完还报"全组通过"——比慢更糟。
        if not line or line.endswith("tests collected"):
            continue
        if line.startswith(("ERROR", "FAILED", "=")) or " errors during collection" in line:
            continue
        weight = 1
        if "::" in line:
            path = line.split("::", 1)[0].strip()
        elif ".py:" in line:
            head, tail = line.split(".py:", 1)
            path = head.strip() + ".py"
            m = re.search(r"(\d+)", tail)
            weight = int(m.group(1)) if m else 1
        else:
            continue
        if path and path not in seen and (ROOT / path).exists():
            seen.add(path)
            files.append((ROOT / path, max(1, weight)))
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--groups", type=int, default=8)
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    files = collect_files()
    if not files:
        print("没收集到用例文件", file=sys.stderr)
        return 2

    groups = max(1, args.groups)
    buckets: list[list[Path]] = [[] for _ in range(groups)]
    weights = [0] * groups
    # 按**用例数**从多到少贪心装桶。用文件体积估过一轮，效果很差：
    # 慢的往往是个别文件里的若干条（每次都等模型桩的退避），体积看不出来。
    for path, count in sorted(files, key=lambda x: -x[1]):
        idx = weights.index(min(weights))
        buckets[idx].append(path)
        weights[idx] += count
    if not args.quiet:
        print(
            f"用例文件 {len(files)} 个，切 {groups} 组，各组用例数：{weights}",
        )

    started = time.monotonic()
    procs = []
    for idx, bucket in enumerate(buckets):
        if not bucket:
            continue
        cmd = [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", *map(str, bucket)]
        if args.quiet:
            procs.append((idx, subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)))
        else:
            procs.append((idx, subprocess.Popen(cmd, cwd=ROOT)))

    failed = []
    for idx, proc in procs:
        code = proc.wait()
        if code != 0:
            failed.append((idx, code))

    elapsed = time.monotonic() - started
    print(f"\n用时 {elapsed:.1f}s，{len(procs)} 组，合计 {sum(weights)} 条用例")
    print("（这个数字必须与单进程全量一致；不一致说明有文件被漏掉了）")
    if failed:
        print(f"失败组：{failed}", file=sys.stderr)
        return 1
    print("全组通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
