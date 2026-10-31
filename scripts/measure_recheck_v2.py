"""真书实测：锚点命中率 与 复检覆盖率。

只量两个数：
1. 逐章分析产生的伏笔里，有多少条真的带上了可定位的原文锚点（stage 0/1 的收益）；
2. 复检跑起来之后，覆盖了多少条、判定分布如何（stage 2 的收益与代价）。

不做的事：不改工作区、不写账本文件、不动队列。全程只读原文 + 真模型调用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.anchor import entity_key_for  # noqa: E402
from backend.core.analyzer import NovelAnalyzer  # noqa: E402
from backend.models.knowledge import KnowledgeBase  # noqa: E402
from backend.services.recheck_v2 import run_recheck_v2  # noqa: E402
from backend.utils.foreshadow_ledger import ForeshadowItem  # noqa: E402
from backend.utils.text_utils import deduplicate_foreshadows  # noqa: E402

# 盗版站的推广行会让厂商输入审核直接拦块，先剥掉
_NOISE = re.compile(r"https?://|www\.|最新章节|请记住本站|手机阅读|无弹窗", re.I)


def load_chapters(blocks_dir: Path, limit: int) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for path in sorted(blocks_dir.glob("*.txt"))[:limit]:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if not _NOISE.search(ln)]
        clean = "\n".join(lines).strip()
        if clean:
            out.append((len(out) + 1, clean))
    return out


def build_items(results: list):
    """把逐章结果压成账本条目：走的是生产同一套去重与实体键规则。"""
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
                )
            )
    catalog = deduplicate_foreshadows(clues)
    items = []
    for cat in catalog:
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
            )
        )
    return items


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", default=r"F:\AI\04_小说与写作\商鞅\三侠五义\blocks")
    ap.add_argument("--chapters", type=int, default=3)
    ap.add_argument("--max-calls", type=int, default=6)
    ap.add_argument("--batch-size", type=int, default=5)
    ap.add_argument("--out", default="docs/MEASURE-RECHECK-V2-2026-10-31.json")
    ap.add_argument("--base-url", default="", help="覆盖 api.base_url（例如本地网关）")
    ap.add_argument("--model", default="", help="覆盖 api.model")
    ap.add_argument("--max-tokens", type=int, default=0, help="覆盖 api.max_tokens")
    args = ap.parse_args()

    # 本机 NO_PROXY 里带 [::1]（方括号），httpx 会把 :1] 当端口 → InvalidURL。
    # 测试里由 pytest_isolation 处理，脚本必须自己做同一件事。
    try:
        import pytest_isolation

        pytest_isolation.sanitize_proxy_env()
    except Exception as exc:  # 净化失败不该让测量跑不起来，但要留痕
        print(f"代理环境净化跳过：{exc}", file=sys.stderr)

    cfg = ConfigManager(ROOT / "config.json").load()
    if args.base_url:
        cfg.api.base_url = args.base_url
    if args.model:
        cfg.api.model = args.model
    if args.max_tokens:
        cfg.api.max_tokens = args.max_tokens
    cfg.analysis.anchor_quote_enabled = True
    print(f"模型：{cfg.api.base_url} / {cfg.api.model} / max_tokens={cfg.api.max_tokens}")
    analyzer = NovelAnalyzer(cfg)

    chapters = load_chapters(Path(args.blocks), args.chapters)
    if not chapters:
        print("没有可用的章节文件", file=sys.stderr)
        return 2
    print(f"章节：{len(chapters)} 篇，共 {sum(len(t) for _c, t in chapters)} 字")

    kb = KnowledgeBase()
    results = []
    anchor_totals = {"total": 0, "located": 0, "missing": 0}
    for ch, text in chapters:
        result, tokens, retry = await analyzer.analyze_chapter(ch, text, kb)
        if result is None:
            print(f"  第{ch}章 失败：{retry.get('error', '')[:120]}")
            continue
        stats = result.anchor_stats or {}
        for k in anchor_totals:
            anchor_totals[k] += int(stats.get(k, 0) or 0)
        print(
            f"  第{ch}章 伏笔 {len(result.foreshadowing)} 条，"
            f"锚点 located={stats.get('located', 0)} missing={stats.get('missing', 0)}，"
            f"tokens={tokens}"
        )
        results.append((ch, result))

    if not results:
        print("所有章节都失败，无法测量（不是代码问题，是模型调用没成功）", file=sys.stderr)
        return 3

    total_fs = sum(len(r.foreshadowing) for _c, r in results)
    hit_rate = (anchor_totals["located"] / anchor_totals["total"]) if anchor_totals["total"] else 0.0
    print(f"锚点命中率：{anchor_totals['located']}/{anchor_totals['total']} = {hit_rate:.2%}")

    items = build_items(results)
    with_anchor = sum(1 for i in items if i.anchor_quote)
    print(f"账本候选：{len(items)} 条，其中带锚点 {with_anchor} 条")

    book_text = "\n\n".join(t for _c, t in chapters)

    async def call_model(prompt: str) -> str:
        res = await analyzer.llm_client.chat_auto(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=int(cfg.analysis.recheck_max_tokens),
        )
        if hasattr(res, "content"):
            return res.content or ""
        if isinstance(res, tuple):
            return res[0] or ""
        return ""

    outcome = await run_recheck_v2(
        items,
        book_text=book_text,
        call_model=call_model,
        max_calls=args.max_calls,
        batch_size=args.batch_size,
    )
    summary = outcome.summary()
    print("复检结果：", json.dumps(summary, ensure_ascii=False))

    payload = {
        "chapters": len(results),
        "foreshadow_items_raw": total_fs,
        "anchor": {**anchor_totals, "hit_rate": round(hit_rate, 4)},
        "ledger_candidates": len(items),
        "candidates_with_anchor": with_anchor,
        "recheck": summary,
    }
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
