"""真书实测（走生产管路）：并发分析 → 锚点命中率 → 复检覆盖率。

与 `measure_recheck_v2.py` 的区别：**不自己串行调模型**，而是用 1.x 现成的
`AnalysisPipeline`（`config.analysis.concurrency` 并发、块级断点、滚动摘要都照旧），
跑完再对产物量两个数。串行版只适合隔离归因，跑整本纯属浪费。

用法：
    python scripts/measure_recheck_v2_pipeline.py --chapters 16 --concurrency 8 \
      --max-calls 12 --base-url http://127.0.0.1:3425/v1 --model wb2api/cn:hy3 --max-tokens 64000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.anchor import entity_key_for  # noqa: E402
from backend.core.pipeline import AnalysisPipeline  # noqa: E402
from backend.models.analysis_result import AnalysisResult  # noqa: E402
from backend.services.recheck_v2 import run_recheck_v2  # noqa: E402
from backend.utils.foreshadow_ledger import ForeshadowItem  # noqa: E402
from backend.utils.text_utils import deduplicate_foreshadows  # noqa: E402

_NOISE = re.compile(r"https?://|www\.|最新章节|请记住本站|手机阅读|无弹窗", re.I)


def prepare_workspace(src: Path, dst_root: Path, limit: int) -> tuple[Path, int]:
    """把真书前 N 章复制成 1.x 工作区形状：<root>/<book>/blocks/*.txt。"""
    book_dir = dst_root / "book"
    blocks = book_dir / "blocks"
    if dst_root.exists():
        shutil.rmtree(dst_root)
    blocks.mkdir(parents=True)
    (book_dir / "output").mkdir()

    count = 0
    copied_bytes = 0
    for path in sorted(src.glob("*.txt"))[:limit]:
        text = path.read_text(encoding="utf-8", errors="replace")
        clean = "\n".join(ln for ln in text.splitlines() if not _NOISE.search(ln)).strip()
        if not clean:
            continue
        count += 1
        out = blocks / f"{count:04d}.txt"
        out.write_text(clean, encoding="utf-8")
        copied_bytes += len(clean)
    return book_dir, count, copied_bytes


def load_results(book_dir: Path) -> list[tuple[int, AnalysisResult]]:
    """从产物里读回逐章结果（生产写的是 chapter_N_result.json）。"""
    found: list[tuple[int, AnalysisResult]] = []
    for path in sorted(book_dir.rglob("chapter_*_result.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        result = AnalysisResult.from_dict(data)
        found.append((getattr(result, "chapter_number", 0) or 0, result))
    found.sort(key=lambda x: x[0])
    return found


def build_items(results: list[tuple[int, AnalysisResult]]) -> list[ForeshadowItem]:
    clues = []
    for ch, result in results:
        for fs in result.foreshadowing:
            clues.append(
                (
                    ch,
                    fs.clue,
                    fs.type,
                    fs.confidence,
                    fs.importance,
                    fs.anchor_quote,
                    fs.anchor_chapter,
                    fs.motif_key,
                    getattr(fs, "question", "") or "",
                    getattr(fs, "answer_criteria", "") or "",
                )
            )
    items = []
    for cat in deduplicate_foreshadows(clues):
        items.append(
            ForeshadowItem(
                id=cat["id"],
                description=cat["clue"],
                first_seen_chapter=cat["first_seen"],
                first_seen_batch=0,
                last_seen_chapter=cat["last_seen"],
                last_seen_batch=0,
                evidence_chapters=cat["evidence_chapters"],
                source_type="catalog_scan",
                anchor_quote=cat.get("anchor_quote", ""),
                anchor_chapter=cat.get("anchor_chapter", 0),
                entity_key=entity_key_for(cat.get("motif_key", ""), str(cat.get("id", ""))),
                question=cat.get("question", ""),
                answer_criteria=cat.get("answer_criteria", ""),
            )
        )
    return items


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", default=r"F:\AI\04_小说与写作\商鞅\三侠五义\blocks")
    ap.add_argument("--chapters", type=int, default=16)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--block-size", type=int, default=1)
    ap.add_argument("--max-calls", type=int, default=12)
    ap.add_argument("--batch-size", type=int, default=5)
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--max-tokens", type=int, default=0)
    ap.add_argument("--out", default="docs/MEASURE-RECHECK-V2-PIPELINE.json")
    ap.add_argument("--skip-recheck", action="store_true", help="只测采集，不跑复检（省调用）")
    ap.add_argument("--workdir", default=".tmp_measure_pipeline")
    ap.add_argument("--reuse", action="store_true", help="已有产物时只测量，不重跑分析")
    args = ap.parse_args()

    try:
        import pytest_isolation

        pytest_isolation.sanitize_proxy_env()
    except Exception as exc:
        print(f"代理净化跳过：{exc}", file=sys.stderr)

    cfg = ConfigManager(ROOT / "config.json").load()
    if args.base_url:
        cfg.api.base_url = args.base_url
    if args.model:
        cfg.api.model = args.model
    if args.max_tokens:
        cfg.api.max_tokens = args.max_tokens
    cfg.analysis.concurrency = args.concurrency
    cfg.analysis.block_size = args.block_size
    cfg.analysis.anchor_quote_enabled = True
    print(
        f"模型 {cfg.api.base_url} / {cfg.api.model} / max_tokens={cfg.api.max_tokens}，"
        f"并发={cfg.analysis.concurrency}，块={cfg.analysis.block_size}"
    )

    if args.reuse:
        book_dir = ROOT / args.workdir / "book"
        if not book_dir.is_dir():
            print("--reuse 需要已有工作区", file=sys.stderr)
            return 2
        n = len(list((book_dir / "blocks").glob("*.txt")))
        chars = sum(len(f.read_text(encoding="utf-8")) for f in (book_dir / "blocks").glob("*.txt"))
    else:
        book_dir, n, chars = prepare_workspace(Path(args.blocks), ROOT / args.workdir, args.chapters)
    print(f"工作区：{book_dir}，{n} 章 / {chars} 字")

    # 关键：pipeline 用 config.working_directory 决定 output 落点，不设就会写到 repo 根
    cfg.working_directory = str(book_dir)

    async def on_progress(payload: dict) -> None:
        kind = payload.get("type", "")
        if kind in ("block_done", "block_start", "progress", "finished"):
            print(f"  [{kind}] {payload.get('message') or payload.get('block_id') or ''}")

    t0 = time.time()
    payload = {}
    pipeline = AnalysisPipeline(
        cfg,
        directory=book_dir / "blocks",
        knowledge_file=book_dir / "knowledge.json",
        on_progress=on_progress,
    )
    if not args.reuse:
        payload = await pipeline.run()
    else:
        print("--reuse：跳过分析，直接测量已有产物")
    elapsed = time.time() - t0
    print(f"分析完成：{json.dumps(payload, ensure_ascii=False)}，用时 {elapsed:.1f}s")

    results = load_results(book_dir)
    if not results:
        print("没有产物，无法测量", file=sys.stderr)
        return 3

    anchor = {"total": 0, "located": 0, "missing": 0}
    for _ch, result in results:
        stats = result.anchor_stats or {}
        for k in anchor:
            anchor[k] += int(stats.get(k, 0) or 0)
    hit = anchor["located"] / anchor["total"] if anchor["total"] else 0.0
    print(f"锚点命中率：{anchor['located']}/{anchor['total']} = {hit:.2%}（{len(results)} 章）")

    # 采集质量：三问与 lead 的分布
    fs_all = [fs for _ch, r in results for fs in r.foreshadowing]
    acq = {
        "foreshadows": len(fs_all),
        "with_question": sum(1 for f in fs_all if (f.question or "").strip()),
        "with_answer_criteria": sum(1 for f in fs_all if (f.answer_criteria or "").strip()),
        "lead": sum(1 for f in fs_all if getattr(f, "lead", False)),
        "with_anchor": sum(1 for f in fs_all if (f.anchor_quote or "").strip()),
    }
    print(
        "采集：{foreshadows} 条 / 带 question {with_question} / 带 answer_criteria "
        "{with_answer_criteria} / lead {lead} / 带锚点 {with_anchor}".format(**acq)
    )

    items = build_items(results)
    with_anchor = sum(1 for i in items if i.anchor_quote)
    print(f"账本候选 {len(items)} 条，带锚点 {with_anchor} 条")

    book_text = "\n\n".join(
        (book_dir / "blocks" / f"{i:04d}.txt").read_text(encoding="utf-8")
        for i in range(1, n + 1)
        if (book_dir / "blocks" / f"{i:04d}.txt").exists()
    )

    llm = pipeline._analyzer.llm_client if getattr(pipeline, "_analyzer", None) else None
    if llm is None:
        from backend.core.llm_client import LLMClient

        llm = LLMClient(cfg.api)

    async def call_model(prompt: str) -> str:
        res = await llm.chat_auto(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=int(cfg.analysis.recheck_max_tokens),
        )
        if hasattr(res, "content"):
            return res.content or ""
        if isinstance(res, tuple):
            return res[0] or ""
        return ""

    outcome = None
    if args.skip_recheck:
        print("跳过复检（--skip-recheck）")
    else:
        t1 = time.time()
        outcome = await run_recheck_v2(
            items,
            book_text=book_text,
            call_model=call_model,
            max_calls=args.max_calls,
            batch_size=args.batch_size,
        )
        print(
            f"复检完成：{json.dumps(outcome.summary(), ensure_ascii=False)}，"
            f"用时 {time.time() - t1:.1f}s"
        )

    out = ROOT / args.out
    out.write_text(
        json.dumps(
            {
                "chapters": len(results),
                "chars": chars,
                "concurrency": cfg.analysis.concurrency,
                "block_size": cfg.analysis.block_size,
                "analysis_seconds": round(elapsed, 1),
                "analysis_payload": payload,
                "anchor": {**anchor, "hit_rate": round(hit, 4)},
                "ledger_candidates": len(items),
                "candidates_with_anchor": with_anchor,
                "acquisition": acq,
                "recheck": outcome.summary() if outcome else {"skipped": True},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
