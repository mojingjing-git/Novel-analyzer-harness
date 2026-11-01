"""阶段 0-2 的离线断言：正文身份落到文件级后，锚点定位到底行不行。

**不调模型、不花钱**：只读 blocks 目录与已有账本，重放一遍定位逻辑。

三个判定（AGY 复审 P2-2 要求的确切分母，不能是"定位失败的就算不存在"）：

1. ``located``：引文在某个文件里**逐字**找到 → 给出文件序号；
2. ``absent``：全书逐字扫描确认**不存在** → 这条候选本来就没有证据；
3. ``unresolved``：全书存在引文字面、却没能定位到文件 → **必须为 0**，
   有它就说明定位逻辑有 bug。

顺带报告两个对照指标：
- ``hit_by_recorded_chapter``：按采集侧记的章号直接命中的比例（历史基线 22%）；
- ``multi_file_quotes``：同一句引文在多个文件里出现（取最早，需人工抽查是否误挂）。

用法::

    python scripts/check_anchor_identity.py --book workspace/《大奉打更人》
    python scripts/check_anchor_identity.py --book workspace/《大奉打更人》 --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.text_units import (  # noqa: E402
    build_display_labels,
    load_blocks_as_files,
    locate_in_units,
)


def _load_items(book_dir: Path) -> list:
    """账本里的伏笔条目（没有账本就从章节结果里重建）。"""
    from backend.utils.foreshadow_ledger import ForeshadowLedger

    for name in ("foreshadow_ledger.json", "foreshadow_ledger_v5.json"):
        path = book_dir / "output" / name
        if path.exists():
            ledger = ForeshadowLedger.load(path)
            return list(ledger.items)
    return []


def _load_items_from_results(book_dir: Path) -> list:
    """没有账本时：从章节结果里收集伏笔（复检的候选来源之一）。"""
    from backend.models.analysis_result import AnalysisResult

    items = []
    for path in sorted((book_dir / "output").glob("chapter_*_result.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for fs in data.get("foreshadowing", []) or []:
            items.append(fs)
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阶段 0-2：锚点身份离线断言")
    ap.add_argument("--book", required=True, help="书的工作目录（含 blocks/ 与 output/）")
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

    items = _load_items(book_dir) or _load_items_from_results(book_dir)
    with_anchor = []
    for it in items:
        quote = str(it.get("anchor_quote", "") if isinstance(it, dict)
                    else getattr(it, "anchor_quote", "") or "").strip()
        if not quote:
            continue
        chapter = int(it.get("anchor_chapter", 0) if isinstance(it, dict)
                      else getattr(it, "anchor_chapter", 0) or 0)
        with_anchor.append((quote, chapter))

    total = len(with_anchor)
    located = 0
    absent = 0
    unresolved = 0
    hit_by_recorded = 0
    multi = 0
    samples: list[dict] = []

    for quote, chapter in with_anchor:
        hit = locate_in_units(units, quote, prefer=chapter or None)
        if hit is not None:
            located += 1
            unit, offset = hit
            if chapter and unit == chapter:
                hit_by_recorded += 1
            occurrences = sum(1 for text in units.values() if quote in text)
            if occurrences > 1:
                multi += 1
                if len(samples) < 5:
                    samples.append({
                        "quote": quote[:40],
                        "unit": unit,
                        "label": labels.get(unit, ""),
                        "occurrences": occurrences,
                    })
            continue
        # 逐字找不到：再确认它是不是**根本不在书里**（用单元首尾拼接的全文复核）
        joined = "\n\n".join(units[k] for k in sorted(units))
        if quote not in joined:
            absent += 1
        else:
            unresolved += 1

    result = {
        "units": len(units),
        "chars": sum(len(t) for t in units.values()),
        "excluded_files": excluded,
        "candidates_total": total,
        "located": located,
        "absent": absent,
        "unresolved": unresolved,
        "hit_by_recorded_chapter": hit_by_recorded,
        "multi_file_quotes": multi,
        "samples": samples,
    }
    print()
    print(f"候选总数            {total}")
    print(f"逐字定位命中        {located}  ({located / total:.1%})" if total else "候选总数 0")
    print(f"确凿不存在          {absent}")
    print(f"存在但定位失败      {unresolved}   ← 必须为 0")
    print(f"[对照] 按记录章号命中 {hit_by_recorded}  ({hit_by_recorded / total:.1%})" if total else "")
    print(f"[对照] 多文件同句引文 {multi}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n已写出 {args.json}")

    return 0 if unresolved == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
