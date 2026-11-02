"""整本观测：64 章逐章分析 → 汇总伏笔 → 全书复检（带调用上限）。

跑的是**生产代码**（vnext `run_analysis` + 1.x `recheck_v2`），脚本只负责接线与统计。
输出一张分布表：检索到多少、判出多少、多少因预算/证据未核对。

用法：
    python scripts/measure_full_book.py --blocks "F:\\...\\三侠五义\\blocks" \
        --workdir .tmp_fullbook --max-tokens 64000 --concurrency 8 \
        --batch-size 6 --max-calls 60

注意两个环境事实（都踩过）：
- 某些模型（如 hy3）强制自适应思考，`max_tokens` 撞上限时**一个字的正文都拿不到**
  （cap 8000/16000 → 0 字；32000 → 206 字；64000 → 正常），所以默认给 64000。
- 独立脚本必须自己净化代理环境变量，否则 `NO_PROXY` 里的 `[::1]` 会让 httpx 抛
  `Invalid port ':1]'`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest_isolation  # noqa: E402

def _drop_proxy_env() -> None:
    """把代理环境变量彻底摘掉。

    `sanitize_proxy_env()` 只把 `[::1]` 规范化成 `::1`，但 httpx 解析 `::1` 仍会在
    `URLPattern` 里抛 `Invalid port ':1]'`；而这些脚本都是打本机网关的，直接清空最省事。
    """
    import os

    for key in list(os.environ):
        if "proxy" in key.lower():
            os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"

pytest_isolation.sanitize_proxy_env()
_drop_proxy_env()

from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.anchor import locate_anchor, split_block_into_chapters  # noqa: E402
from backend.core.llm_client import LLMClient  # noqa: E402
from backend.services.recheck_v2 import run_recheck_v2  # noqa: E402
from backend.utils.foreshadow_ledger import ForeshadowItem  # noqa: E402
from backend.utils.text_utils import deduplicate_foreshadows  # noqa: E402
from backend.vnext import project as vnext_project  # noqa: E402
from backend.vnext.blocks import plan_blocks  # noqa: E402
from backend.vnext.results import list_results  # noqa: E402
from backend.vnext.prompt_limits import PromptLimits  # noqa: E402
from backend.vnext.runner import run_analysis  # noqa: E402

#: 切章器写进每个块文件头部的四行元数据，不是正文——
#: 不剥掉会被当成章节标题，把 64 块误切出 113 章（实测），章号也会整体错位。
HEADER_PREFIXES = ("包含章节:", "章节数:", "书名:", "总章节")
HEADER_BOOK = re.compile(r"^《.*》第\s*\d+\s*部分")
HEADER_RULE = re.compile(r"^[=\-]{6,}$")

JUNK = re.compile(
    r"(https?://|www\.|本书由|爱上阅读|isyd\.net|盗版|免费阅读|请记住本站|章节报错)"
)


def scrub(text: str) -> str:
    """剥掉切章器表头 + 盗版水印行。

    两类都得删：表头会被误当标题（导致章号错位），水印/URL 会让厂商的输入审核
    直接拦掉整块。
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if HEADER_RULE.match(stripped) or HEADER_BOOK.match(stripped):
            continue
        if stripped.startswith(HEADER_PREFIXES):
            continue
        if JUNK.match(stripped):
            continue
        kept.append(line)
    return "\n".join(kept)


def load_book(blocks_dir: Path) -> dict[int, str]:
    """一个块文件 = 一个分析单元，章号取块序号。

    刻意**不再**在块内按"第N回"二次切分：实测 64 个块里标题格式并不统一，
    二次切分会切出 113 个章号且中间有空洞（1..120 带缺号），
    章号一旦不连续，"引文落在第几章"这类核对就没法对账。
    这里用块序号当章号，语义是"第 N 个部分"，与切章产物一致。
    """
    chapters: dict[int, str] = {}
    files = sorted(blocks_dir.glob("*.txt"), key=lambda f: int(f.stem) if f.stem.isdigit() else 0)
    for ordinal, path in enumerate(files, start=1):
        chapters[ordinal] = scrub(path.read_text(encoding="utf-8", errors="replace"))
    return chapters


async def main_async(args) -> int:
    cfg = ConfigManager(ROOT / "config.json").load()
    cfg.api.base_url = args.base_url
    cfg.api.model = args.model
    cfg.api.max_tokens = args.max_tokens
    cfg.api.timeout = max(int(cfg.api.timeout or 300), 900)
    client = LLMClient(cfg.api)

    chapters = load_book(Path(args.blocks))
    total_chars = sum(len(t) for t in chapters.values())
    print(f"章数 {len(chapters)}，字数 {total_chars:,}", flush=True)

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    book_id = "fullbook"
    # create_project 的项目目录 = data_root / book_id，别自己再套一层
    book_dir = workdir / book_id

    if not (book_dir / "manifest.json").exists():
        # chapters 是 (章号, 正文) 序列，章号给真实值才能让锚点与提示词对齐
        vnext_project.create_project(
            workdir,
            "整本观测",
            [(n, t) for n, t in sorted(chapters.items())],
            book_id=book_id,
        )
    manifest = vnext_project.load_manifest(book_dir)
    per_block = args.block_size
    plans = plan_blocks(manifest, block_size=per_block)

    started = time.monotonic()
    outcome = await run_analysis(
        book_dir,
        client=client,
        limits=PromptLimits(),
        plans=plans,
        skip_permanent=True,
        tolerate_failures=True,
        concurrency=args.concurrency,
    )
    analyze_seconds = time.monotonic() - started
    print(
        f"分析：块 {len(plans)}，committed {outcome.committed_blocks}，"
        f"skip {outcome.skipped_blocks}，stop={outcome.stop_reason}，"
        f"用时 {analyze_seconds:.0f}s，prompt {outcome.prompt_tokens} / completion {outcome.completion_tokens}",
        flush=True,
    )

    # 汇总伏笔（与 final_summary 的 catalog 同构：真实章号 + 锚点）
    results = list_results(book_dir)
    clues: list = []
    for rec in results.values():
        if not getattr(rec, "extracted", None):
            continue
        for raw in rec.extracted.get("foreshadowing", []) or []:
            quote = (raw.get("anchor_quote") or "").strip()
            if not quote:
                continue
            real = next(
                (ch for ch, text in chapters.items() if locate_anchor(text, quote)), None
            )
            if real is None:
                continue
            clues.append(
                (
                    real,
                    raw.get("clue", ""),
                    raw.get("type", ""),
                    raw.get("confidence", "中"),
                    raw.get("importance", "中"),
                    quote,
                    real,
                    raw.get("motif_key", ""),
                    raw.get("question", ""),
                    raw.get("answer_criteria", ""),
                )
            )
    cats = deduplicate_foreshadows(clues)
    items = [
        ForeshadowItem(
            id=c["id"],
            description=c["clue"],
            first_seen_chapter=c["first_seen"],
            first_seen_batch=0,
            last_seen_chapter=c["last_seen"],
            last_seen_batch=0,
            evidence_chapters=c["evidence_chapters"],
            source_type="catalog_scan",
            anchor_quote=c.get("anchor_quote", ""),
            anchor_chapter=c.get("anchor_chapter", 0),
            question=c.get("question", ""),
            answer_criteria=c.get("answer_criteria", ""),
            lead=c.get("lead", False),
        )
        for c in cats
    ]

    async def call_model(prompt: str) -> str:
        res = await client.chat_auto(
            messages=[{"role": "user", "content": prompt}], max_tokens=args.max_tokens
        )
        return getattr(res, "content", "") or ""

    recheck_started = time.monotonic()
    re_outcome = await run_recheck_v2(
        items,
        chapters=chapters,
        call_model=call_model,
        max_calls=args.max_calls,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
    )
    recheck_seconds = time.monotonic() - recheck_started

    counts = re_outcome.coverage
    by_verdict: dict[str, int] = {}
    for v in re_outcome.verdicts:
        by_verdict[v.verdict] = by_verdict.get(v.verdict, 0) + 1
    by_state: dict[str, int] = {}
    for state in re_outcome.applied.values():
        by_state[state] = by_state.get(state, 0) + 1

    report = {
        "chapters": len(chapters),
        "chars": total_chars,
        "blocks": len(plans),
        "analyze_seconds": round(analyze_seconds, 1),
        "analyze_prompt_tokens": outcome.prompt_tokens,
        "analyze_completion_tokens": outcome.completion_tokens,
        "stop_reason": outcome.stop_reason,
        "committed_blocks": outcome.committed_blocks,
        "skipped_blocks": outcome.skipped_blocks,

        "clues_total": len(clues),
        "catalog_total": len(cats),
        "recheck_seconds": round(recheck_seconds, 1),
        "recheck_calls": re_outcome.calls,
        "recheck_batches_retried": re_outcome.retried_batches,
        "recheck_batches_failed": re_outcome.failed_batches,
        "coverage": counts,
        "verdicts": by_verdict,
        "states": by_state,
        "leads_skipped": re_outcome.leads_skipped,
        "dropped_no_anchor": re_outcome.dropped_no_anchor,
        "dropped_anchor_missing": re_outcome.dropped_anchor_missing,
        "status": re_outcome.status,
        "status_reason": re_outcome.status_reason,
    }
    out = Path(args.out)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 整本观测 ===")
    for key in (
        "chapters", "blocks", "analyze_seconds", "committed_blocks",
        "clues_total", "catalog_total", "recheck_calls", "recheck_seconds",
        "verdicts", "states", "coverage", "status", "status_reason",
    ):
        print(f"{key}: {report[key]}")
    print(f"\n报告写入 {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", required=True)
    ap.add_argument("--workdir", default=".tmp_fullbook")
    ap.add_argument("--out", default="docs/data/MEASURE-FULLBOOK.json")
    ap.add_argument("--base-url", default="http://127.0.0.1:3425/v1")
    ap.add_argument("--model", default="wb2api/cn:hy3")
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--block-size", type=int, default=2)
    ap.add_argument("--batch-size", type=int, default=6)
    ap.add_argument("--max-calls", type=int, default=60)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
