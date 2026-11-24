#!/usr/bin/env python3
"""单候选复检探针：把「一次真实复检调用」的原始回包完整暴露出来。

段 A（`probe_recheck_transport.py`）只证明通道能通；这个脚本证明**判定链路**能通：
真的走 `run_recheck_v2`，但只放 1 条候选、1 次调用，然后把

- 渲染出的提示词长度、
- **模型原始回包（截断 2000 字）**、
- `_extract_payloads` 挖到几条、
- `parse_recheck` 认为无效时的原因、
- 最终 verdict 的 4 态

全部打出来。失败时不猜，直接看回包长什么样。

用法：
    python -u scripts/probe_recheck_one.py
    python -u scripts/probe_recheck_one.py --workdir .tmp_stage0_recheck --index 3
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 与段 A 相同：本机 NO_PROXY 的 ``[::1]`` 会让 httpx 在构造期抛 InvalidURL
for _key in ("NO_PROXY", "no_proxy"):
    os.environ[_key] = "localhost,127.0.0.1"
os.environ.pop("ALL_PROXY", None)
os.environ.pop("all_proxy", None)

from backend.config.paths import runtime_path  # noqa: E402
from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.llm_client import LLMClient  # noqa: E402
from backend.core.text_units import build_display_labels, load_blocks_as_files  # noqa: E402
from backend.services.recheck_v2 import _extract_payloads, run_recheck_v2  # noqa: E402


def _load_measure_module():
    """复用测量脚本的 load_results / build_items，避免两套账本重建逻辑漂移。"""
    path = ROOT / "scripts" / "measure_recheck_v2_pipeline.py"
    spec = importlib.util.spec_from_file_location("_measure_pipeline", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=".tmp_stage0_recheck")
    ap.add_argument("--base-url", default="http://127.0.0.1:3425/v1")
    ap.add_argument("--model", default="codex/gpt-6-luna")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--max-tokens", type=int, default=32768)
    ap.add_argument("--block-size", type=int, default=4)
    ap.add_argument("--index", type=int, default=0, help="取第几条带锚点候选（从 0 起）")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    book_dir = ROOT / args.workdir / "book"
    chapters, excluded = load_blocks_as_files(book_dir / "blocks")
    mod = _load_measure_module()
    results = mod.load_results(book_dir)
    items = mod.build_items(results)
    picked = [i for i in items if (i.anchor_quote or "").strip()]
    print(f"账本候选 {len(items)} 条，带锚点 {len(picked)} 条；单元 {len(chapters)} 个"
          f"（已排除非正文 {len(excluded)} 个：{excluded[:3]}）")
    if not picked:
        print("没有带锚点的候选，无法探针（账本没锚点 = V2 无从核对）", file=sys.stderr)
        return 3
    item = picked[min(args.index, len(picked) - 1)]
    print(f"选中候选 id={getattr(item, 'id', '?')} 引文=「{(item.anchor_quote or '')[:40]}」")
    print(f"  first_seen_chapter={getattr(item, 'first_seen_chapter', 0)} "
          f"anchor_chapter={getattr(item, 'anchor_chapter', 0)} "
          f"question={(getattr(item, 'question', '') or '')[:40]}")
    print()

    cfg = ConfigManager(runtime_path("config.json")).load()
    api = replace(
        cfg.api,
        base_url=args.base_url,
        model=args.model,
        max_tokens=args.max_tokens,
        reasoning_effort=args.effort,
        streaming_enabled=True,
    )
    llm = LLMClient(api)

    captured: dict[str, str] = {}

    async def call_model(prompt: str) -> str:
        captured["prompt_chars"] = str(len(prompt))
        res = await asyncio.wait_for(
            llm.chat_auto(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=args.max_tokens,
            ),
            timeout=args.timeout,
        )
        content = getattr(res, "content", "") or ""
        captured["raw"] = content
        captured["success"] = str(getattr(res, "success", True))
        captured["error"] = str(getattr(res, "error", "") or "")
        # 关键：失败必须显性化，否则 failed_batches 会永远是 0（复审 P1-1）
        if not getattr(res, "success", True):
            raise RuntimeError(f"LLM 调用失败：{getattr(res, 'error', '') or '未知错误'}")
        return content

    outcome = await run_recheck_v2(
        [item],
        chapters=chapters,
        call_model=call_model,
        max_calls=1,
        batch_size=1,
        concurrency=1,
        unit_noun="文件",
        display_labels=build_display_labels(chapters),
        block_size=args.block_size,
    )

    raw = captured.get("raw", "")
    print("=== 调用结果 ===")
    print(f"提示词长度 : {captured.get('prompt_chars', '?')} 字")
    print(f"success    : {captured.get('success', '?')}")
    if captured.get("error"):
        print(f"error      : {captured['error'][:300]}")
    print(f"回包长度   : {len(raw)} 字")
    print(f"_extract_payloads 命中 : {len(_extract_payloads(raw))} 条")
    print()
    print("=== 原始回包（前 2000 字） ===")
    print(raw[:2000] if raw else "(空)")
    print()
    print("=== 复检账 ===")
    print(f"calls={outcome.calls} failed_batches={outcome.failed_batches} "
          f"retried_batches={outcome.retried_batches}")
    print(f"status={outcome.status} reason={outcome.status_reason}")
    print(f"coverage={outcome.coverage}")
    print(f"verdicts={len(outcome.verdicts)}")
    for v in outcome.verdicts:
        print(f"  - {v.candidate_id}: verdict={v.verdict} retryable={v.retryable} "
              f"reason={(v.reason or '')[:120]}")

    ok = bool(outcome.verdicts)
    print()
    print("=== 结论 ===")
    print("判定链路可用：拿到 verdict → 进段 D（小批）" if ok
          else "判定链路没落账 → 按上面的原始回包修解析/提示词，不要进段 D")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
