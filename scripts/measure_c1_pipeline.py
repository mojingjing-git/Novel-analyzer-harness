#!/usr/bin/env python
"""C1 实测：用真实模型把合成语料（23 章 / 6 块）整本跑一遍。

只回答两个问题，**不选方案**：

1. 管道能不能用真实模型跑通（历史是否累积、块序是否被遵守、覆盖是否如实）；
2. token 量级是多少（每块多少、整本多少），以及第二次运行是否零调用。

安全边界：最多 90 次调用 / 200000 token，超限即停并如实报告"未完成"。
默认只用合成语料，不把用户书库发出去。

用法::

    python scripts/measure_c1_pipeline.py            # 真实调用
    python scripts/measure_c1_pipeline.py --dry-run  # 只建项目、打印块划分
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# 本机 NO_PROXY 含 "[::1]"，httpx 会把它当端口解析。进程内清掉（子进程改父环境无效）。
for _key in [k for k in list(os.environ) if "proxy" in k.lower()]:
    os.environ.pop(_key, None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_CALLS = 90
MAX_TOKENS = 200_000
BLOCK_SIZE = 4  # 与 vnext_corpus.BLOCK_SIZE 一致：22 章可切 → 6 块


class CountingClient:
    """包一层 `LLMClient`，数调用次数与 token。

    统计**只从返回结果读**，不去猜：`tokens` 为 None 表示该次尝试没拿到用量，
    如实记进 `unknown`，绝不写成 (0, 0)。
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.unknown = 0
        self.attempts = 0
        self.per_call: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls += 1
        result = await self.inner.chat(messages, **kwargs)
        tokens = getattr(result, "tokens", None)
        attempts = int(getattr(result, "attempts", 1) or 1)
        self.attempts += attempts
        if tokens is None:
            self.unknown += 1
            self.per_call.append({"tokens": None, "attempts": attempts,
                                  "usage_status": str(getattr(result, "usage_status", None))})
        else:
            prompt_tokens, completion_tokens = tokens
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens
            self.per_call.append({"tokens": list(tokens), "attempts": attempts,
                                  "usage_status": str(getattr(result, "usage_status", None))})
        return result

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def over_budget(self) -> bool:
        return self.calls >= MAX_CALLS or self.total_tokens >= MAX_TOKENS


def _load_client():
    from backend.config.settings import APIConfig
    from backend.core.llm_client import LLMClient

    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    fields = {f.name for f in APIConfig.__dataclass_fields__.values()}
    api = {k: v for k, v in (raw.get("api") or {}).items() if k in fields}
    return LLMClient(APIConfig(**api))


def _build_book(data_root: Path, title: str):
    """把合成语料建成一个 vnext 项目（第 17 章缺失是语料本身的设定）。"""
    from backend.tests.fixtures.vnext_corpus import build_corpus
    from backend.vnext import project

    corpus_dir = Path(tempfile.mkdtemp(prefix="c1corpus-"))
    corpus = build_corpus(corpus_dir)

    # build_corpus 把章节文件写在 <root>/blocks/ 下（不是 root 直下）
    chapter_root = Path(corpus.root) / "blocks"
    chapters = []
    for ordinal in range(1, 24):
        path = chapter_root / f"ch_{ordinal:04d}.txt"
        if not path.exists():
            continue  # 第 17 章故意缺失
        chapters.append((ordinal, path.read_text(encoding="utf-8")))

    book_dir = project.create_project(data_root, title, chapters, book_id="bk_c1corpus001")
    shutil.rmtree(corpus_dir, ignore_errors=True)
    return book_dir, len(chapters)


async def amain(args) -> int:
    from backend.vnext import blocks, project, runner
    from backend.vnext.prompt_limits import PromptLimits

    data_root = Path(tempfile.mkdtemp(prefix="c1data-"))
    book_dir, chapter_count = _build_book(data_root, "C1 合成语料")

    manifest = project.load_manifest(book_dir)
    plans = blocks.plan_blocks(manifest, block_size=BLOCK_SIZE)
    print(f"项目: {book_dir.name}  章节 {chapter_count}  块 {len(plans)}")
    for index, plan in enumerate(plans, 1):
        print(f"  块 {index}: {plan.chapter_ids} 签名 {plan.block_signature[:12]}")

    if args.dry_run:
        print("dry-run：未调用模型")
        shutil.rmtree(data_root, ignore_errors=True)
        return 0

    client = CountingClient(_load_client())
    limits = PromptLimits()

    started = time.monotonic()
    outcome = await runner.run_analysis(
        book_dir, client=client, limits=limits, block_size=BLOCK_SIZE
    )
    elapsed = time.monotonic() - started

    print("\n=== 第一次运行 ===")
    print(f"committed_blocks : {outcome.committed_blocks}")
    print(f"stop_reason      : {outcome.stop_reason}")
    print(f"coverage         : cursor={outcome.coverage.commit_cursor} "
          f"prefix_complete={outcome.coverage.prefix_complete} gaps={outcome.coverage.gaps}")
    print(f"调用             : {client.calls}（attempts {client.attempts}）")
    print(f"token            : prompt={client.prompt_tokens} completion={client.completion_tokens} "
          f"合计={client.total_tokens}")
    print(f"未知用量         : {client.unknown}")
    print(f"耗时             : {elapsed:.1f}s")

    # 第二次运行必须零调用（缓存命中）
    again = CountingClient(_load_client())
    outcome2 = await runner.run_analysis(
        book_dir, client=again, limits=limits, block_size=BLOCK_SIZE
    )
    print("\n=== 第二次运行（应为零调用）===")
    print(f"调用             : {again.calls}")
    print(f"committed_blocks : {outcome2.committed_blocks}")
    print(f"coverage         : cursor={outcome2.coverage.commit_cursor} "
          f"prefix_complete={outcome2.coverage.prefix_complete}")

    budget_ok = client.calls < MAX_CALLS and client.total_tokens < MAX_TOKENS
    report = {
        "task": "C1",
        "chapters": chapter_count,
        "blocks": len(plans),
        "first_run": {
            "calls": client.calls,
            "attempts": client.attempts,
            "prompt_tokens": client.prompt_tokens,
            "completion_tokens": client.completion_tokens,
            "total_tokens": client.total_tokens,
            "unknown_usage_calls": client.unknown,
            "elapsed_s": round(elapsed, 1),
            "stop_reason": outcome.stop_reason,
            "committed_blocks": outcome.committed_blocks,
            "commit_cursor": outcome.coverage.commit_cursor,
            "prefix_complete": outcome.coverage.prefix_complete,
            "gaps": list(outcome.coverage.gaps),
        },
        "second_run": {"calls": again.calls,
                       "commit_cursor": outcome2.coverage.commit_cursor},
        "budget": {"max_calls": MAX_CALLS, "max_tokens": MAX_TOKENS, "within_budget": budget_ok},
        "per_call": client.per_call,
    }
    out = ROOT / "docs" / "MEASURE-C1-2026-10-31.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"\n报告已写入 {out.relative_to(ROOT)}")

    shutil.rmtree(data_root, ignore_errors=True)
    return 0 if budget_ok else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="不调用模型")
    return asyncio.run(amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
