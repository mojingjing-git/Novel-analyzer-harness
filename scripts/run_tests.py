#!/usr/bin/env python
"""轻量统一测试入口：后端 pytest + 可选前端 vitest。

用法：
    python scripts/run_tests.py                 # 跑后端 pytest + 前端 vitest
    python scripts/run_tests.py --no-frontend   # 仅跑后端 pytest
    python scripts/run_tests.py -k vnext        # 透传参数给 pytest
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIFILE = ROOT / "pytest.ini"
BACKEND_TESTS = ROOT / "backend" / "tests"
FRONTEND_DIR = ROOT / "frontend"


def _run_frontend() -> int:
    npm = shutil.which("npm")
    if not npm:
        print("[run_tests] [warn] 找不到 npm，跳过前端测试", file=sys.stderr)
        return 0
    cmd = [npm, "run", "test"]
    print(f"[run_tests] 前端：{' '.join(cmd)} (cwd={FRONTEND_DIR})", flush=True)
    proc = subprocess.run(cmd, cwd=str(FRONTEND_DIR))
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="统一测试入口（pytest + vitest）")
    parser.add_argument("--no-frontend", action="store_true", help="跳过前端 vitest")
    args, unknown = parser.parse_known_args(argv)

    paths: list[str] = []
    extra: list[str] = []
    after_ddash = False
    for token in unknown:
        if token == "--":
            after_ddash = True
            continue
        if after_ddash or token.startswith("-"):
            extra.append(token)
        elif (ROOT / token).exists() or Path(token).exists():
            paths.append(str(Path(token).resolve()))
        else:
            extra.append(token)

    target_paths = paths if paths else [str(BACKEND_TESTS)]
    cmd = [sys.executable, "-m", "pytest", "-c", str(INIFILE), *target_paths, *extra]
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    print(f"[run_tests] 后端：{' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env)
    if proc.returncode != 0:
        return proc.returncode

    if not args.no_frontend and not paths:
        return _run_frontend()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
