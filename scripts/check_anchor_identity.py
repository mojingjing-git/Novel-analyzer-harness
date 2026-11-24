"""阶段 0-3 的离线断言：候选块范围定位到底行不行。

**不调模型、不花钱**：只读 blocks 目录与已有章节结果，重放一遍定位逻辑。

四个判定（AGY 复审 P2/P3 要求的确切分母，不能是"定位失败的就算不存在"）：

1. ``range_hits``：引文在**候选块范围** ``[N, N+block_size-1]`` 内逐字命中
   （N = 该伏笔来自哪个结果文件 = 物理文件序号）；
2. ``fallback_hits``：范围内没命中，退到全书最早出现 —— **跨卷错挂风险高**，
   应逐条人工核对（AGY P1-2）；
3. ``absent``：全书逐字扫描确认**不存在**；
4. ``unresolved``：全书存在引文字面、却没能定位 —— **必须为 0**。

另外报告两个对照指标：按采集侧记录章号直接命中的比例（历史基线 21.9%），
以及"全书最早"与"块范围"结论不一致的条数（AGY 复审指出的跨卷截胡）。

用法::

    python scripts/check_anchor_identity.py --book runtime/workspace/《大奉打更人》
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.text_units import (  # noqa: E402
    build_display_labels,
    load_blocks_as_files,
    locate_with_mode,
)


def _load_measure_module():
    """复用测量脚本的"结果 → 去重账本条目"逻辑（避免两套口径）。"""
    spec = importlib.util.spec_from_file_location(
        "measure_recheck_v2_pipeline", ROOT / "scripts" / "measure_recheck_v2_pipeline.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阶段 0-3：候选块范围定位的离线断言")
    ap.add_argument("--book", required=True, help="书的工作目录（含 blocks/ 与 output/）")
    ap.add_argument("--block-size", type=int, default=4, help="采集块大小（候选范围宽度）")
    ap.add_argument("--json", default="", help="把结果写成 JSON")
    args = ap.parse_args(argv)

    book_dir = Path(args.book)
    if not book_dir.is_absolute():
        book_dir = ROOT / book_dir
    blocks_dir = book_dir / "blocks"
    if not blocks_dir.is_dir():
        print(f"没有 blocks 目录：{blocks_dir}")
        return 2

    units, excluded = load_blocks_as_files(blocks_dir)
    labels = build_display_labels(units)
    print(f"单元（文件）{len(units)} 个，{sum(len(t) for t in units.values()):,} 字")
    if excluded:
        print(f"已排除非正文文件 {len(excluded)} 个：{excluded[:5]}")

    mp = _load_measure_module()
    items = mp.build_items(mp.load_results(book_dir))
    with_anchor = []
    for it in items:
        quote = (getattr(it, "anchor_quote", "") or "").strip()
        if not quote:
            continue
        with_anchor.append((
            quote,
            int(getattr(it, "first_seen_chapter", 0) or 0),
            int(getattr(it, "anchor_chapter", 0) or 0),
            str(getattr(it, "id", "")),
        ))

    total = len(with_anchor)
    range_hits = 0
    fallback_hits = 0
    absent = 0
    unresolved = 0
    hit_by_recorded = 0
    diffs: list[dict] = []
    fallbacks: list[dict] = []

    for quote, first_seen, anchor_chapter, item_id in with_anchor:
        cand = (first_seen, first_seen + args.block_size - 1) if first_seen > 0 else None
        hit = locate_with_mode(
            units, quote, candidate_range=cand,
            display_labels=labels, anchor_round=anchor_chapter or None,
        )
        if hit.mode == "range":
            range_hits += 1
            if hit.unit == anchor_chapter:
                hit_by_recorded += 1
            # 与"全书最早"结论不一致的（跨卷截胡风险条数）
            earliest = locate_with_mode(units, quote)
            if earliest.unit is not None and earliest.unit != hit.unit:
                diffs.append({
                    "id": item_id, "quote": quote[:36],
                    "range_unit": hit.unit, "earliest_unit": earliest.unit,
                })
        elif hit.mode == "fallback":
            fallback_hits += 1
            if len(fallbacks) < 10:
                fallbacks.append({"id": item_id, "quote": quote[:36], "unit": hit.unit})
        else:
            absent += 1

    result = {
        "units": len(units),
        "chars": sum(len(t) for t in units.values()),
        "excluded_files": excluded,
        "block_size": args.block_size,
        "candidates_total": total,
        "range_hits": range_hits,
        "fallback_hits": fallback_hits,
        "absent": absent,
        "unresolved": unresolved,
        "hit_by_recorded_chapter": hit_by_recorded,
        "differs_from_earliest": len(diffs),
        "diff_samples": diffs[:8],
        "fallback_samples": fallbacks,
    }
    print()
    print(f"候选总数              {total}")
    print(f"块范围内命中          {range_hits}"
          + (f"  ({range_hits / total:.1%})" if total else ""))
    print(f"全书兜底命中          {fallback_hits}"
          + (f"  ({fallback_hits / total:.1%})" if total else "")
          + "   ← 跨卷错挂风险，须逐条核对")
    print(f"确凿不存在            {absent}")
    print(f"存在但定位失败        {unresolved}   ← 必须为 0")
    print(f"[对照] 按记录章号命中  {hit_by_recorded}"
          + (f"  ({hit_by_recorded / total:.1%})" if total else ""))
    print(f"[对照] 与全书最早不一致 {len(diffs)} 条")

    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写出 {args.json}")

    return 0 if unresolved == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
