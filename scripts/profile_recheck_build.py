#!/usr/bin/env python3
"""定位 run_recheck_v2 在**候选构建阶段**的 CPU 热点（不调模型、不花钱）。

用法：
    python -u scripts/profile_recheck_build.py --limit 120

max_calls=0 → 一轮调用都不发，只走"建候选 + 定位"这段。用 cProfile 打前 25 个
累计耗时函数，热点一眼能看见。
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import importlib.util
import io
import os
import pstats
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = "localhost,127.0.0.1"

from backend.core.text_units import build_display_labels, load_blocks_as_files  # noqa: E402
from backend.services.recheck_v2 import run_recheck_v2  # noqa: E402


def _load_measure_module():
    path = ROOT / "scripts" / "measure_recheck_v2_pipeline.py"
    spec = importlib.util.spec_from_file_location("_measure_pipeline", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=".tmp_stage0_recheck")
    ap.add_argument("--limit", type=int, default=120, help="只跑前 N 条候选（0 = 全部）")
    ap.add_argument("--block-size", type=int, default=4)
    args = ap.parse_args()

    book_dir = ROOT / args.workdir / "book"
    t0 = time.time()
    chapters, excluded = load_blocks_as_files(book_dir / "blocks")
    mod = _load_measure_module()
    items = mod.build_items(mod.load_results(book_dir))
    picked = [i for i in items if (i.anchor_quote or "").strip()]
    if args.limit:
        picked = picked[: args.limit]
    print(f"单元 {len(chapters)}，候选 {len(picked)}，加载 {time.time() - t0:.1f}s")

    async def _never(_prompt: str) -> str:  # max_calls=0 时不会被调用
        raise AssertionError("不该发请求")

    prof = cProfile.Profile()
    prof.enable()
    t1 = time.time()
    await run_recheck_v2(
        picked,
        chapters=chapters,
        call_model=_never,
        max_calls=0,          # 只建候选，不发请求
        batch_size=6,
        concurrency=1,
        unit_noun="文件",
        display_labels=build_display_labels(chapters),
        block_size=args.block_size,
    )
    elapsed = time.time() - t1
    prof.disable()

    print(f"候选构建用时 {elapsed:.2f}s（{len(picked)} 条 → {elapsed / max(1, len(picked)) * 1000:.1f} ms/条）")
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(18)
    print(buf.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
