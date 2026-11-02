#!/usr/bin/env python
"""对生产账本执行 audit 存量清算（PLAN-foreshadow-adjudication 段 D）。

复核账本里 llm_judged 的"已回收"是否成立：VERIFIED→quality=verified；
QUESTIONED→quality=questioned + needs_review（status/source_type 不动）。

用法示例（先 50 条校准，再全量）：
  python -u scripts/run_recheck_audit.py \
    --ledger "workspace/《大奉打更人》/output/foreshadow_ledger.json" \
    --blocks .tmp_stage0_recheck/book/blocks \
    --base-url http://127.0.0.1:3425/v1 --recheck-model codex/gpt-6-luna \
    --reasoning-effort low --max-calls 130 --batch-size 6 --limit 50

  python -u scripts/run_recheck_audit.py ... （去掉 --limit 即全量）

产出（D-19：明细与被改账本同目录）：
  - recheck_audit.json：逐条明细 + 元数据 + 定位诊断
  - foreshadow_ledger.json 回写（先自动备份 .bak-audit）

环境：PYTHONIOENCODING=utf-8；NO_PROXY/no_proxy 需含 localhost,127.0.0.1（本脚本强制设置）。
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
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = "localhost,127.0.0.1"

from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.llm_client import LLMClient  # noqa: E402
from backend.core.text_units import build_display_labels, load_blocks_as_files  # noqa: E402
from backend.services.recheck_v2 import run_recheck_audit  # noqa: E402
from backend.utils.foreshadow_ledger import ForeshadowItem, ForeshadowLedger  # noqa: E402


def _eligible(it: ForeshadowItem) -> bool:
    """与 run_recheck_audit 的资格过滤一致（D-15），供 --limit 预切片。"""
    return (
        getattr(it, "status", "") == "resolved"
        and getattr(it, "source_type", "") == "llm_judged"
        and not (getattr(it, "resolution_quality", "") or "")
        and not getattr(it, "lead", False)
        and bool((getattr(it, "anchor_quote", "") or "").strip())
        and int(getattr(it, "anchor_chapter", 0) or 0) > 0
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", required=True, help="foreshadow_ledger.json 路径")
    ap.add_argument("--blocks", required=True, help="原文 blocks 目录（正文切分文件）")
    ap.add_argument("--base-url", default="", help="判定通道 base_url（留空跟随 config）")
    ap.add_argument("--recheck-model", default="", help="判定模型（留空跟随 config）")
    ap.add_argument("--reasoning-effort", default="low")
    ap.add_argument("--max-calls", type=int, default=130)
    ap.add_argument("--batch-size", type=int, default=6)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="只复核前 N 条合格条目（0=全量）")
    ap.add_argument("--dry-run", action="store_true", help="只统计资格与七栏，不发调用")
    args = ap.parse_args()

    cfg = ConfigManager(ROOT / "config.json").load()
    ledger_path = Path(args.ledger)
    ledger = ForeshadowLedger.load(ledger_path)
    bars_before = ledger.lifecycle_bars()
    eligible = [it for it in ledger.items if _eligible(it)]
    print(f"账本 {len(ledger.items)} 条；七栏（改前）：{json.dumps(bars_before, ensure_ascii=False)}")
    print(f"audit 合格条目：{len(eligible)} 条")
    if args.limit > 0:
        eligible = eligible[: args.limit]
        print(f"--limit={args.limit}：本次复核 {len(eligible)} 条")

    if args.dry_run:
        return 0
    if not eligible:
        print("没有可复核条目，结束")
        return 0

    chapters, excluded = load_blocks_as_files(Path(args.blocks))
    print(f"正文单元 {len(chapters)} 个（排除 {len(excluded)} 个非正文文件）")

    api = cfg.api
    if args.recheck_model:
        api = replace(api, model=args.recheck_model)
    if args.base_url:
        api = replace(api, base_url=args.base_url)
    api = replace(api, reasoning_effort=args.reasoning_effort)
    print(f"判定通道：{api.model} @ {api.base_url}（effort={args.reasoning_effort}）")
    llm = LLMClient(api)
    max_tokens = int(cfg.analysis.recheck_max_tokens)

    async def call_model(prompt: str) -> str:
        res = await llm.chat_auto(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )
        # 失败必须抛：chat_auto 对 400/401/404 返回 success=False 而不抛异常，
        # 只取 content 会让 failed_batches 恒为 0、整轮静默 0 判定
        if not getattr(res, "success", True):
            raise RuntimeError(f"audit 调用失败：{getattr(res, 'error', '') or '未知错误'}")
        return getattr(res, "content", "") or ""

    def _on_event(event: dict) -> None:
        if event.get("stage") == "build_candidates":
            print(f"  · 构建 {event.get('built')}/{event.get('total')}", flush=True)
            return
        calls = event.get("calls")
        if calls is None:
            return
        checked = event.get("checked")
        if checked is None:
            checked = (event.get("coverage") or {}).get("checked")
        print(f"  · calls={calls} checked={checked}", flush=True)

    t0 = time.time()
    outcome = await run_recheck_audit(
        eligible,
        chapters,
        call_model,
        max_calls=args.max_calls,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        unit_noun="文件",
        display_labels=build_display_labels(chapters),
        emit=_on_event,
    )
    print(f"audit 完成：{json.dumps(outcome.summary(), ensure_ascii=False)}，用时 {time.time() - t0:.1f}s")

    # 明细落盘：与被改账本同目录（D-19）
    detail = {
        "generated_at": datetime.now().isoformat(),
        "mode": "audit",
        "model": api.model,
        "base_url": api.base_url,
        "reasoning_effort": args.reasoning_effort,
        **outcome.detail(),
    }
    detail_path = ledger_path.parent / "recheck_audit.json"
    detail_path.write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"逐条明细已写入 {detail_path}")

    # 回写账本（先备份）
    backup = ledger_path.with_suffix(".json.bak-audit")
    shutil.copy2(ledger_path, backup)
    ledger.schema_version = 6
    ledger.save(ledger_path)
    print(f"账本已回写（备份：{backup.name}）")

    bars_after = ledger.lifecycle_bars()
    print(f"七栏（改后）：{json.dumps(bars_after, ensure_ascii=False)}")

    cov = outcome.coverage
    if outcome.calls > 0 and cov.get("checked", 0) == 0:
        print(f"❌ {outcome.calls} 次调用全部未返回可解析判定；failed_batches={outcome.failed_batches}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
