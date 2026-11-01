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
from backend.core.anchor import entity_key_for, split_block_into_chapters  # noqa: E402
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
    ap.add_argument(
        "--recheck-model",
        default="",
        help="复检专用模型（留空 = 跟随 --model）；采集要覆盖面、判定要准确，可分开设",
    )
    ap.add_argument("--max-tokens", type=int, default=0)
    ap.add_argument(
        "--reasoning-effort",
        default="",
        help="推理档位（low/medium/high/xhigh/max）；留空 = 服务端默认。"
        "实测 low 与 max 的思考量差约 3 倍，长书批量跑通常用 low",
    )
    ap.add_argument("--out", default="docs/MEASURE-RECHECK-V2-PIPELINE.json")
    ap.add_argument(
        "--out-verdicts",
        default="",
        help="逐条判定明细落盘路径；留空则写 docs/data/verdicts-<时间戳>.json",
    )
    ap.add_argument(
        "--recheck-concurrency",
        type=int,
        default=0,
        help="复检在途批次数上限；0 表示跟随 --concurrency",
    )
    ap.add_argument(
        "--recheck-max-tokens",
        type=int,
        default=0,
        help="复检单次调用的输出上限；0 表示用配置里的 analysis.recheck_max_tokens",
    )
    ap.add_argument("--skip-recheck", action="store_true", help="只测采集，不跑复检（省调用）")
    ap.add_argument("--workdir", default=".tmp_measure_pipeline")
    ap.add_argument("--reuse", action="store_true", help="已有产物时只测量，不重跑分析")
    args = ap.parse_args()

    try:
        import pytest_isolation

        pytest_isolation.sanitize_proxy_env()
        # sanitize 只把 `[::1]` 规范化成 `::1`，但 httpx 解析 `::1` 仍会抛
        # `Invalid port ':1]'`；这些脚本都打本机网关，直接把代理变量清空最省事。
        import os

        for key in list(os.environ):
            if "proxy" in key.lower():
                os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "localhost,127.0.0.1"
        os.environ["no_proxy"] = "localhost,127.0.0.1"
    except Exception as exc:
        print(f"代理净化跳过：{exc}", file=sys.stderr)

    cfg = ConfigManager(ROOT / "config.json").load()
    if args.base_url:
        cfg.api.base_url = args.base_url
    if args.model:
        cfg.api.model = args.model
    if args.max_tokens:
        cfg.api.max_tokens = args.max_tokens
    if args.reasoning_effort:
        cfg.api.reasoning_effort = args.reasoning_effort
    cfg.analysis.concurrency = args.concurrency
    cfg.analysis.block_size = args.block_size
    cfg.analysis.anchor_quote_enabled = True
    print(
        f"模型 {cfg.api.base_url} / {cfg.api.model} / max_tokens={cfg.api.max_tokens}，"
        f"并发={cfg.analysis.concurrency}，块={cfg.analysis.block_size}，"
        f"思考档位={cfg.api.reasoning_effort or '默认'}"
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
        # 管线发的事件用 ``status``（PipelineStatus 枚举），没有 ``type`` 字段——
        # 早先按 ``type`` 过滤会一行都打不出来，并发也就无从取证。
        raw = payload.get("status") or payload.get("type") or ""
        name = getattr(raw, "name", None) or str(raw)
        if not name or name.startswith("_"):
            return
        if not any(tag in name for tag in ("START", "DONE", "ROLLING", "FAILED", "SKIPPED")):
            return
        # 带时间戳：并发有没有真的生效，看这些行的时间分布就知道
        # （串行的话 START 与 DONE 会一行隔一行地顺序出现）
        stamp = time.strftime("%H:%M:%S")
        print(
            f"  [{stamp}] {name} ch={payload.get('chapter')} "
            f"{payload.get('message') or ''}".rstrip(),
            flush=True,
        )

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

    # 按**回**索引的原文：生产路径（`final_summary._collect_chapters`）就是用
    # `split_block_into_chapters` 从块内标题反查章号，再有界取窗。两件事必须对齐：
    # 1. 只传整本 `book_text` 时，一个候选的证据包就是全书正文——批 6 条等于把同一本
    #    12 万字塞进 prompt 六遍，既烧钱又把"证据范围"放大到整本，判定自然偏乐观；
    # 2. 章号若按"块序号"算（而不是标题里写的回号），锚点几乎全落空：实测 48 条里
    #    44 条被记成 `dropped_anchor_missing`，复检等于什么都没查。
    chapters: dict[int, str] = {}
    for ordinal, path in enumerate(sorted((book_dir / "blocks").glob("*.txt")), start=1):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        chapters.update(split_block_into_chapters(text, ordinal))
    print(
        f"按标题切出 {len(chapters)} 章（回号 "
        f"{min(chapters) if chapters else 0}..{max(chapters) if chapters else 0}），"
        f"{sum(len(t) for t in chapters.values()):,} 字"
    )

    llm = pipeline._analyzer.llm_client if getattr(pipeline, "_analyzer", None) else None
    if llm is None:
        from backend.core.llm_client import LLMClient

        llm = LLMClient(cfg.api)

    # 复检可以用**另一个模型**：采集要覆盖面（别漏伏笔），判定要准确（判得准），
    # 两者对模型的偏好并不一致。留空就跟随 --model。
    recheck_llm = llm
    if args.recheck_model and args.recheck_model != cfg.api.model:
        from dataclasses import replace

        from backend.core.llm_client import LLMClient

        recheck_llm = LLMClient(replace(cfg.api, model=args.recheck_model))
        print(f"复检模型：{args.recheck_model}（采集模型：{cfg.api.model}）")

    recheck_max_tokens = args.recheck_max_tokens or int(cfg.analysis.recheck_max_tokens)

    async def call_model(prompt: str) -> str:
        res = await recheck_llm.chat_auto(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=recheck_max_tokens,
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
        recheck_concurrency = args.recheck_concurrency or cfg.analysis.concurrency
        print(f"复检并发={recheck_concurrency}，批大小={args.batch_size}，调用上限={args.max_calls}")
        t1 = time.time()
        outcome = await run_recheck_v2(
            items,
            chapters=chapters,
            call_model=call_model,
            max_calls=args.max_calls,
            batch_size=args.batch_size,
            concurrency=recheck_concurrency,
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
                "evidence_chapters": len(chapters),
                "acquisition": acq,
                "recheck": outcome.detail() if outcome else {"skipped": True},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"已写入 {out}")

    if outcome is not None:
        # 明细单独落一份：审计要看的是"每条候选的 verdict/answer/引文/位置"，
        # 而不是计数；混在大报告里读起来费劲，也容易被别的测量覆盖掉。
        stamp = time.strftime("%Y%m%d-%H%M")
        verdicts_path = (
            ROOT / args.out_verdicts
            if args.out_verdicts
            else ROOT / "docs" / "data" / f"verdicts-{stamp}.json"
        )
        verdicts_path.parent.mkdir(parents=True, exist_ok=True)
        verdicts_path.write_text(
            json.dumps(
                {
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "model": cfg.api.model,
                    "base_url": cfg.api.base_url,
                    "chapters": len(results),
                    "evidence_mode": "bounded",
                    "batch_size": args.batch_size,
                    "concurrency": args.recheck_concurrency or cfg.analysis.concurrency,
                    "max_calls": args.max_calls,
                    "status": outcome.status,
                    "status_reason": outcome.status_reason,
                    "coverage": dict(outcome.coverage),
                    "candidates_total": outcome.coverage.get("candidates_total", 0),
                    "verdicts": [v.to_dict() for v in outcome.verdicts],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"逐条判定写入 {verdicts_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
