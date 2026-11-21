"""T4 Step 4: 扩充 C 类真实管线候选——把试产范围从 12 章扩到 24 章。

任务书 §六 要求 C 类至少 80 条真实管线候选。12 章范围下，采集端每块上限
`max_foreshadow_entries=5`（3 块 × 5 = 15 条/部），四部作品合计只有 49 条真实候选，
不足门槛，且不得用合成探针充数。

做法：同一批冻结作品、同一条生产链路，把连续章节范围从 1-12 扩到 1-24，
产出更多**真实管线候选**用于 C 类独立核对。这不是"换容易作品"，
也不是"构造探针"——候选全部来自生产链路对原文的真实抽取。

产物：
  workspace/bc-evaluation/t4-trial-20261106/ext24/<work_id>/blocks/0001.txt..0024.txt
  workspace/bc-evaluation/t4-trial-20261106/ext24/runs/<work_id>/<arm>/...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

ROOT = Path(__file__).resolve().parents[3]
TRIAL_ROOT = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
EXT_ROOT = TRIAL_ROOT / "ext24"
CORPUS_DIR = Path(r"F:\AI\04_小说与写作\修复后txt文件")
WORK_IDS = [
    "t4_ind_holdout",
    "t4_game_holdout",
    "t4_urban_holdout",
    "t4_hist_holdout",
]
SOURCES: dict[str, str] = {}  # 从冻结声明读取，避免手写文件名出错


def _load_sources() -> dict[str, str]:
    """源文件名以冻结声明为准（声明里已带 sha256/编码，可追溯）。"""
    if SOURCES:
        return SOURCES
    decl = json.loads(
        (ROOT / "docs/research/bc-production-readiness-2026-11-06/t4-frozen-trial-declaration.json")
        .read_text(encoding="utf-8")
    )
    for w in decl["works"]:
        SOURCES[w["work_id"]] = w["filename"]
    return SOURCES
CHAPTERS = 24
MAIN_MODEL = "codex/gpt-6-luna"
ARMS = {
    "baseline": {
        "enable_c_on_demand_audit": False,
        "fix_b2_evidence_and_recheck": False,
        "fix_b4_flow_and_storage": False,
        "enable_b_trace": False,
    },
    "candidate": {
        "enable_c_on_demand_audit": True,
        "fix_b2_evidence_and_recheck": True,
        "fix_b4_flow_and_storage": True,
        "enable_b_trace": True,
    },
}
_NOISE = re.compile(r"https?://|www\.|最新章节|请记住本站|手机阅读|无弹窗", re.I)
_CH_RE = re.compile(r"(?m)^\s*第[零一二三四五六七八九十百千万0-9]+[章节回]")


def _patched_config(arm: str):
    from backend.config.settings import ConfigManager

    cfg = ConfigManager(ROOT / "config.json").load()
    api = replace(cfg.api, model=MAIN_MODEL)
    analysis = replace(cfg.analysis, recheck_model=MAIN_MODEL, recheck_base_url=cfg.api.base_url)
    analysis = replace(
        analysis,
        concurrency=2,
        summary_concurrency=2,
        recheck_concurrency=2,
        **ARMS[arm],
    )
    return replace(cfg, api=api, analysis=analysis)


def split_blocks(work_id: str) -> int:
    """按原文标题把前 CHAPTERS 章切成 blocks/<nnnn>.txt（沿用生产切块口径）。"""
    src = CORPUS_DIR / _load_sources()[work_id]
    raw = src.read_bytes()
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    marks = list(_CH_RE.finditer(text))
    out_dir = EXT_ROOT / work_id / "blocks"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for i, m in enumerate(marks[:CHAPTERS]):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.start() : end]
        body = "\n".join(ln for ln in body.splitlines() if not _NOISE.search(ln)).strip()
        if not body:
            continue
        n += 1
        (out_dir / f"{n:04d}.txt").write_text(body, encoding="utf-8")
    return n


async def run_work(work_id: str) -> dict:
    from backend.core.pipeline import AnalysisPipeline
    from backend.services.final_summary import FinalSummaryRunner

    work_dir = EXT_ROOT / "runs" / work_id / "_shared"
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir = work_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = replace(_patched_config("baseline"), working_directory=str(work_dir))
    pipe = AnalysisPipeline(
        config=cfg,
        directory=EXT_ROOT / work_id / "blocks",
        knowledge_file=work_dir / cfg.knowledge_file,
        start_chapter=1,
        end_chapter=CHAPTERS,
    )
    t0 = time.perf_counter()
    await pipe.run()
    analysis_elapsed = time.perf_counter() - t0

    arms = {}
    for arm in ("baseline", "candidate"):
        arm_dir = EXT_ROOT / "runs" / work_id / arm
        arm_dir.mkdir(parents=True, exist_ok=True)
        aout = arm_dir / "output"
        if aout.exists():
            shutil.rmtree(aout)
        shutil.copytree(out_dir, aout)
        bdir = arm_dir / "blocks"
        if bdir.exists():
            shutil.rmtree(bdir)
        shutil.copytree(EXT_ROOT / work_id / "blocks", bdir)

        acfg = replace(_patched_config(arm), working_directory=str(arm_dir))
        tokens: dict[str, dict] = {}

        def on_token_stats(payload: dict, _t=tokens) -> None:
            cat = str(payload.get("category") or "unknown")
            slot = _t.setdefault(cat, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
            slot["input_tokens"] += int(payload.get("input_tokens") or 0)
            slot["output_tokens"] += int(payload.get("output_tokens") or 0)
            slot["calls"] += 1

        runner = FinalSummaryRunner(
            config=acfg,
            output_dir=aout,
            start_chapter=1,
            end_chapter=CHAPTERS,
            batch_size=int(acfg.analysis.summary_batch_size),
            concurrency=2,
            ledger_path=arm_dir / "foreshadow_ledger.json",
            on_token_stats=on_token_stats,
        )
        err = None
        t1 = time.perf_counter()
        try:
            await runner.run()
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        arms[arm] = {
            "elapsed_sec": round(time.perf_counter() - t1, 2),
            "error": err,
            "tokens_by_category": tokens,
            "tokens_total": {
                "input_tokens": sum(v["input_tokens"] for v in tokens.values()),
                "output_tokens": sum(v["output_tokens"] for v in tokens.values()),
            },
        }
        print(f"  [{work_id}/{arm}] done", flush=True)

    return {
        "work_id": work_id,
        "chapters": CHAPTERS,
        "analysis_elapsed_sec": round(analysis_elapsed, 2),
        "arms": arms,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--works", nargs="*", default=WORK_IDS)
    args = ap.parse_args()
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"

    out_path = EXT_ROOT / "ext24-run.json"
    out = {"schema": "t4_ext24_run_v1", "chapters": CHAPTERS, "works": {}}
    if out_path.is_file():
        out = json.loads(out_path.read_text(encoding="utf-8"))

    for wid in args.works:
        marker = EXT_ROOT / "runs" / wid / "_shared" / ".analysis_done"
        blocks_ready = (EXT_ROOT / wid / "blocks" / f"{CHAPTERS:04d}.txt").is_file()
        if not blocks_ready:
            n = split_blocks(wid)
            print(f"[{wid}] split {n} blocks", flush=True)
        if marker.is_file():
            print(f"[{wid}] skip analysis (已存在)", flush=True)
            out["works"][wid] = out["works"].get(wid, {})
            continue
        print(f"[{wid}] start", flush=True)
        out["works"][wid] = await run_work(wid)
        marker.write_text("ok", encoding="utf-8")
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", out_path)


if __name__ == "__main__":
    asyncio.run(main())
