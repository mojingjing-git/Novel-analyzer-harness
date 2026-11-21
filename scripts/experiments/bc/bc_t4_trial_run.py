"""T4 试产运行器：在冻结的新作品上跑真实生产链路，两臂在同一产物层比较。

链路：真实 AnalysisPipeline（章节分析）→ 真实 FinalSummaryRunner（调和→目录→复检→落账→保存恢复）。
两臂共用同一份分析阶段产物（四个开关只作用于 final_summary/recheck 阶段，
分析产物不随开关变化），复检/复审按臂各跑一次，从而在同一产物层比较。

产物落在 workspace/bc-evaluation/t4-trial-20261106/runs/<work_id>/<arm>/ 下。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

ROOT = Path(__file__).resolve().parents[3]
TRIAL_ROOT = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
RUNS_ROOT = TRIAL_ROOT / "runs"
WORK_IDS = [
    "t4_ind_holdout",
    "t4_game_holdout",
    "t4_urban_holdout",
    "t4_hist_holdout",
]
ARMS = {
    # 生产现状：四个开关全关。这是验收表里的比较基线。
    "baseline": {
        "enable_c_on_demand_audit": False,
        "fix_b2_evidence_and_recheck": False,
        "fix_b4_flow_and_storage": False,
        "enable_b_trace": False,
    },
    # 候选：拟默认开启的组合（B 修复 + C 按需复审）。
    "candidate": {
        "enable_c_on_demand_audit": True,
        "fix_b2_evidence_and_recheck": True,
        "fix_b4_flow_and_storage": True,
        "enable_b_trace": True,
    },
}

# 三臂：baseline（生产现状全关）/ b_fixed（只开 B 修复，C 关）/ candidate（B+C 全开）。
# b_fixed 用于隔离 C 按需复审的净效应，baseline→b_fixed 给出 B 修复的净效应。
ARMS_ORDER = ["baseline", "b_fixed", "candidate"]
ARMS["b_fixed"] = {
    "enable_c_on_demand_audit": False,
    "fix_b2_evidence_and_recheck": True,
    "fix_b4_flow_and_storage": True,
    "enable_b_trace": True,
}
MAIN_MODEL = "codex/gpt-6-luna"


def _patched_config(arm: str):
    """按臂与冻结模型构造配置：主通道与复检通道同源同模。"""
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


async def run_analysis(work_id: str, cfg) -> dict:
    """真实章节分析：产出 output/chapter_N_result.json 与知识库。"""
    from backend.core.pipeline import AnalysisPipeline

    blocks_dir = TRIAL_ROOT / work_id / "blocks"
    work_dir = TRIAL_ROOT / "runs" / work_id / "_shared"
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir = work_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = replace(cfg, working_directory=str(work_dir))
    tokens = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    async def on_token_stats(payload: dict) -> None:
        tokens["input_tokens"] += int(payload.get("input_tokens") or 0)
        tokens["output_tokens"] += int(payload.get("output_tokens") or 0)
        tokens["calls"] += 1

    pipe = AnalysisPipeline(
        config=cfg,
        directory=blocks_dir,
        knowledge_file=work_dir / cfg.knowledge_file,
        start_chapter=1,
        end_chapter=12,
        on_token_stats=on_token_stats,
    )
    t0 = time.perf_counter()
    res = await pipe.run()
    elapsed = time.perf_counter() - t0
    return {
        "elapsed_sec": round(elapsed, 2),
        "tokens": tokens,
        "pipeline_result": {
            k: v for k, v in (res or {}).items() if not isinstance(v, (list, dict))
        },
        "output_dir": str(out_dir),
    }


async def run_arm(work_id: str, arm: str, cfg) -> dict:
    """真实总结链路：调和→目录→复检→落账→保存恢复。"""
    from backend.services.final_summary import FinalSummaryRunner

    shared = TRIAL_ROOT / "runs" / work_id / "_shared"
    arm_dir = TRIAL_ROOT / "runs" / work_id / arm
    arm_dir.mkdir(parents=True, exist_ok=True)
    out_dir = arm_dir / "output"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(shared / "output", out_dir)
    # blocks 必须与 output 同级：复检按 output_dir.parent/"blocks" 取原文
    shutil.copytree(TRIAL_ROOT / work_id / "blocks", arm_dir / "blocks")

    cfg = replace(cfg, working_directory=str(arm_dir))
    tokens: dict[str, dict] = {}

    def on_token_stats(payload: dict) -> None:
        cat = str(payload.get("category") or "unknown")
        slot = tokens.setdefault(cat, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
        slot["input_tokens"] += int(payload.get("input_tokens") or 0)
        slot["output_tokens"] += int(payload.get("output_tokens") or 0)
        slot["calls"] += 1

    runner = FinalSummaryRunner(
        config=cfg,
        output_dir=out_dir,
        start_chapter=1,
        end_chapter=12,
        batch_size=int(cfg.analysis.summary_batch_size),
        concurrency=2,
        ledger_path=arm_dir / "foreshadow_ledger.json",
        on_token_stats=on_token_stats,
    )
    t0 = time.perf_counter()
    err = None
    try:
        await runner.run()
    except Exception as exc:  # noqa: BLE001 — 试产要记录失败而不是中断全部作品
        err = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - t0

    totals = {
        "input_tokens": sum(v["input_tokens"] for v in tokens.values()),
        "output_tokens": sum(v["output_tokens"] for v in tokens.values()),
    }
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return {
        "arm": arm,
        "elapsed_sec": round(elapsed, 2),
        "error": err,
        "tokens_by_category": tokens,
        "tokens_total": totals,
        "recheck_summary": getattr(runner, "last_recheck_summary", None),
        "ledger_items": len(getattr(runner.ledger, "items", {}) or {}),
        "output_dir": str(out_dir),
    }


async def run_work(work_id: str, force: bool) -> dict:
    marker = TRIAL_ROOT / "runs" / work_id / "_shared" / ".analysis_done"
    analysis_meta_path = TRIAL_ROOT / "runs" / work_id / "_shared" / "analysis_meta.json"
    if marker.is_file() and not force:
        analysis_meta = json.loads(analysis_meta_path.read_text(encoding="utf-8"))
    else:
        cfg = _patched_config("baseline")
        analysis_meta = await run_analysis(work_id, cfg)
        analysis_meta_path.write_text(
            json.dumps(analysis_meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        marker.write_text("ok", encoding="utf-8")

    arms = {}
    for arm in ARMS_ORDER:
        # 已有该臂的完整产物就复用，避免重复付费
        done_marker = TRIAL_ROOT / "runs" / work_id / arm / ".arm_done"
        if done_marker.is_file() and not force:
            print(f"  [{work_id}/{arm}] skip (已存在)", flush=True)
            continue
        cfg = _patched_config(arm)
        arms[arm] = await run_arm(work_id, arm, cfg)
        done_marker.write_text("ok", encoding="utf-8")
        print(f"  [{work_id}/{arm}] done", flush=True)
    return {"work_id": work_id, "analysis": analysis_meta, "arms": arms}


async def rerun_work(work_id: str) -> dict:
    """补跑：单列的新运行。产物写到 runs/<work_id>_rerun/，不覆盖原产物。

    用于原运行缺失 token 统计（臂名迁移时丢失）的情况——按"补跑必须单列为新运行"
    的要求，新数据独立存放、独立报告，不与原运行合并。
    """
    src_shared = TRIAL_ROOT / "runs" / work_id / "_shared"
    work_root = TRIAL_ROOT / "runs" / f"{work_id}_rerun"
    shared = work_root / "_shared"
    if shared.exists():
        shutil.rmtree(shared)
    shutil.copytree(src_shared, shared)
    marker = shared / ".analysis_done"
    if marker.is_file():
        marker.unlink()

    cfg = _patched_config("baseline")
    analysis_meta = await run_analysis(work_id, cfg)
    (shared / "analysis_meta.json").write_text(
        json.dumps(analysis_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    marker.write_text("ok", encoding="utf-8")

    arms = {}
    for arm in ARMS_ORDER:
        arm_dir = work_root / arm
        arm_dir.mkdir(parents=True, exist_ok=True)
        out_dir = arm_dir / "output"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        shutil.copytree(shared / "output", out_dir)
        bdir = arm_dir / "blocks"
        if bdir.exists():
            shutil.rmtree(bdir)
        shutil.copytree(TRIAL_ROOT / work_id / "blocks", bdir)

        acfg = _patched_config(arm)
        tokens: dict[str, dict] = {}

        def on_token_stats(payload: dict, _t=tokens) -> None:
            cat = str(payload.get("category") or "unknown")
            slot = _t.setdefault(cat, {"input_tokens": 0, "output_tokens": 0, "calls": 0})
            slot["input_tokens"] += int(payload.get("input_tokens") or 0)
            slot["output_tokens"] += int(payload.get("output_tokens") or 0)
            slot["calls"] += 1

        from backend.services.final_summary import FinalSummaryRunner

        runner = FinalSummaryRunner(
            config=acfg,
            output_dir=out_dir,
            start_chapter=1,
            end_chapter=12,
            batch_size=int(acfg.analysis.summary_batch_size),
            concurrency=2,
            ledger_path=arm_dir / "foreshadow_ledger.json",
            on_token_stats=on_token_stats,
        )
        err = None
        t0 = time.perf_counter()
        try:
            await runner.run()
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - t0
        arms[arm] = {
            "arm": arm,
            "elapsed_sec": round(elapsed, 2),
            "error": err,
            "tokens_by_category": tokens,
            "tokens_total": {
                "input_tokens": sum(v["input_tokens"] for v in tokens.values()),
                "output_tokens": sum(v["output_tokens"] for v in tokens.values()),
                "total_tokens": sum(
                    v["input_tokens"] + v["output_tokens"] for v in tokens.values()
                ),
            },
            "recheck_summary": getattr(runner, "last_recheck_summary", None),
            "ledger_items": len(getattr(runner.ledger, "items", {}) or {}),
            "output_dir": str(out_dir),
        }
        print(f"  [{work_id}_rerun/{arm}] done", flush=True)

    return {
        "work_id": work_id,
        "rerun_of": work_id,
        "run_kind": "separate_rerun_missing_token_stats",
        "analysis": analysis_meta,
        "arms": arms,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--works", nargs="*", default=WORK_IDS)
    ap.add_argument("--force", action="store_true", help="忽略已完成标记重跑分析")
    ap.add_argument(
        "--rerun",
        action="store_true",
        help="补跑：写入 <work>_rerun/ 单列新运行，不覆盖原产物",
    )
    args = ap.parse_args()

    # 必须覆盖而非 setdefault：系统 NO_PROXY 含 "[::1]"，httpx 会把它当 URL 解析
    # 并在 ":1]" 上抛 Invalid port，导致连本地网关都建不出客户端。
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)

    out = {"schema": "t4_trial_run_v1", "model": MAIN_MODEL, "works": {}}
    out_path = TRIAL_ROOT / (
        "t4-trial-rerun.json" if args.rerun else "t4-trial-run.json"
    )
    if out_path.is_file():
        out = json.loads(out_path.read_text(encoding="utf-8"))
        out.setdefault("works", {})

    for wid in args.works:
        print(f"[{wid}] start", flush=True)
        if args.rerun:
            out["works"][wid] = await rerun_work(wid)
        else:
            out["works"][wid] = await run_work(wid, args.force)
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", out_path)


if __name__ == "__main__":
    asyncio.run(main())
