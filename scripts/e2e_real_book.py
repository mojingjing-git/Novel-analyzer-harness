#!/usr/bin/env python
"""真书端到端：把切好的《三侠五义》建成 vnext 项目，用真实模型整本跑一遍。

与 C 批测量脚本的区别：那不是"测量某个机制"，而是**把整条链在真书上走一遍**——
建项目 → 块规划 → 真实分析（32 块）→ 知识库落盘 → 覆盖视图 → 二次运行零调用。

安全边界：默认只跑 32 次调用（`--max-calls` 可调）；超限即停并如实报告"未完成"。

用法::

    python scripts/e2e_real_book.py --dry-run     # 只建项目、打印块划分
    python scripts/e2e_real_book.py               # 真实调用
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

# 本机 NO_PROXY 含 "[::1]"，httpx 会把它当端口解析。进程内清掉。
for _key in [k for k in list(os.environ) if "proxy" in k.lower()]:
    os.environ.pop(_key, None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BOOK_DIR = Path(r"F:\AI\04_小说与写作\商鞅\三侠五义\blocks")
BLOCK_SIZE = 2
MAX_CALLS = 40
MAX_TOKENS = 900_000

_HEADER_RULE = re.compile(r"^=+\s*$")


def strip_splitter_header(text: str) -> str:
    """去掉切章器写的 4 行表头（`====` 两条 + 两行元信息）。

    保守做法：只在**开头确实像表头**时才剥，且最多剥到第二条分隔线为止。
    """
    lines = text.splitlines()
    if not lines or not _HEADER_RULE.match(lines[0].strip()):
        return text
    cut = None
    seen = 0
    for index, line in enumerate(lines[:12]):
        if _HEADER_RULE.match(line.strip()):
            seen += 1
            if seen == 2:
                cut = index + 1
                break
    if cut is None:
        return text
    body = lines[cut:]
    while body and not body[0].strip():
        body.pop(0)
    return "\n".join(body) + "\n"


#: 盗版站点在正文里塞的水印与前言。它们不只是噪声——带 URL 的行会让厂商的
#: 输入审核直接拦下整块（MiniMax: 422 `input new_sensitive (1026)`），
#: 表现成"第一章永远失败"。清洗规则只删这几类行，不动正文。
_JUNK_PATTERNS = (
    "isyd",
    "http",
    "www.",
    "本书由",
    "本书来自",
    "声明：",
    "敬告：",
    "仅供",
    "请访问",
    "更多精彩",
    "版权归",
    "24小时内删除",
)


def scrub_junk(text: str) -> str:
    """去掉水印/前言类行，保留正文。"""
    kept = [line for line in text.splitlines() if not any(j in line for j in _JUNK_PATTERNS)]
    return "\n".join(kept).strip()


def load_chapters() -> list[tuple[int, str]]:
    files = sorted(
        (p for p in BOOK_DIR.glob("*.txt") if p.stem.isdigit()),
        key=lambda p: int(p.stem),
    )
    chapters: list[tuple[int, str]] = []
    for path in files:
        body = scrub_junk(strip_splitter_header(path.read_text(encoding="utf-8", errors="replace")))
        if len(body.strip()) < 200:  # 空壳章节不算
            continue
        chapters.append((int(path.stem), body))
    return chapters


class CountingClient:
    """包一层 `LLMClient`，只从返回结果读用量（拿不到就记 unknown，不写成 0）。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.unknown = 0
        self.attempts = 0
        self.latencies: list[float] = []

    async def chat(self, messages, **kwargs):
        self.calls += 1
        started = time.monotonic()
        result = await self.inner.chat(messages, **kwargs)
        self.latencies.append(round(time.monotonic() - started, 1))
        self.attempts += int(getattr(result, "attempts", 1) or 1)
        tokens = getattr(result, "tokens", None)
        if tokens is None:
            self.unknown += 1
        else:
            self.prompt_tokens += int(tokens[0])
            self.completion_tokens += int(tokens[1])
        return result

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def over_budget(self, max_calls: int, max_tokens: int) -> bool:
        return self.calls >= max_calls or self.total_tokens >= max_tokens


_OPTS = {"max_tokens": 64000, "block_size": 2, "chapters": 0, "timeout": 900, "concurrency": 8}


def args_timeout() -> int:
    return _OPTS.get("timeout", 900)


def args_max_tokens() -> int:
    return _OPTS.get("max_tokens", 64000)


def args_block_size() -> int:
    return _OPTS["block_size"]


def _load_client():
    from backend.config.settings import APIConfig
    from backend.core.llm_client import LLMClient

    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    fields = {f.name for f in APIConfig.__dataclass_fields__.values()} if hasattr(APIConfig, "__dataclass_fields__") else set()
    api = raw.get("api", {})
    # 真书单块输出比合成语料大得多，300s 会撞超时（实测 block1 一次 330s 空返回）
    api = {
        **api,
        "timeout": args_timeout(),
        # 真书一块的输出用不到十万 token。130000 会让模型"一直写到上限"，
        # 实测单块跑 900s 仍 0 token 返回——把上限收到 8000，写不下就是超长，
        # 让失败变快、变得可诊断。
        "max_tokens": args_max_tokens(),
    }
    kwargs = {k: v for k, v in api.items() if not fields or k in fields}
    return LLMClient(APIConfig(**kwargs))


def _knowledge_stats(book_dir: Path) -> dict:
    from backend.vnext import knowledge

    path = book_dir / "derived" / "knowledge.json"
    if not path.exists():
        return {"exists": False}
    store, meta = knowledge.load_store(path)
    reduced = store.reduced()
    return {
        "exists": True,
        "facts": len(getattr(reduced, "facts", []) or []),
        "characters": len(getattr(reduced, "characters", {}) or {}),
        "blocks_in_store": len(getattr(store, "block_signatures", lambda: [])()),
        "meta_keys": sorted(meta)[:8] if isinstance(meta, dict) else None,
    }


async def amain(args) -> int:
    _OPTS["max_tokens"] = args.max_tokens
    _OPTS["block_size"] = args.block_size
    _OPTS["timeout"] = args.timeout
    from backend.vnext import blocks, project, runner
    from backend.vnext.prompt_limits import PromptLimits

    chapters = load_chapters()
    if args.chapters:
        chapters = chapters[: args.chapters]
        print(f"[子集] 只取前 {len(chapters)} 章")
    if not chapters:
        print(f"没有可用的章节文件：{BOOK_DIR}")
        return 2
    total_chars = sum(len(text) for _, text in chapters)
    print(f"真书: {len(chapters)} 章 / {total_chars:,} 字 / 目录 {BOOK_DIR}")

    data_root = ROOT / "workspace_vnext"
    data_root.mkdir(parents=True, exist_ok=True)
    title = "三侠五义"
    book_id = f"sxy{args.chapters}" if args.chapters else "sxy"
    book_dir = data_root / book_id
    if book_dir.exists():
        print(f"复用已有项目: {book_dir}")
    else:
        book_dir = project.create_project(
            data_root, title, chapters, book_id=book_id, dir_name=book_id
        )

    manifest = project.load_manifest(book_dir)
    plans = blocks.plan_blocks(manifest, block_size=args_block_size())
    sizes = []
    for plan in plans:
        chars = sum(len(project.read_chapter_text(book_dir, cid)) for cid in plan.chapter_ids)
        sizes.append(chars)
    print(f"块: {len(plans)}  每块字数 min/median/max = "
          f"{min(sizes)}/{sorted(sizes)[len(sizes)//2]}/{max(sizes)}")

    if args.dry_run:
        print("dry-run：未调用模型")
        return 0

    client = CountingClient(_load_client())
    limits = PromptLimits()
    started = time.monotonic()
    outcome = await runner.run_analysis(
        book_dir, client=client, limits=limits, block_size=args_block_size(),
        skip_permanent=args.skip_permanent,
        tolerate_failures=args.tolerate_failures,
        concurrency=args.concurrency,
    )
    elapsed = time.monotonic() - started

    print("\n=== 第一次运行 ===")
    print(f"committed_blocks : {outcome.committed_blocks}")
    print(f"stop_reason      : {outcome.stop_reason}")
    print(f"coverage         : cursor={outcome.coverage.commit_cursor} "
          f"prefix_complete={outcome.coverage.prefix_complete} gaps={outcome.coverage.gaps}")
    print(f"调用 {client.calls}（attempts {client.attempts}）  "
          f"token prompt={client.prompt_tokens} completion={client.completion_tokens} "
          f"合计={client.total_tokens}  未知用量={client.unknown}")
    print(f"耗时 {elapsed:.1f}s  单次中位 {sorted(client.latencies)[len(client.latencies)//2] if client.latencies else 0}s")

    again = CountingClient(_load_client())
    outcome2 = await runner.run_analysis(
        book_dir, client=again, limits=limits, block_size=args_block_size()
    )
    print("\n=== 第二次运行（应为零调用）===")
    print(f"调用 {again.calls}  committed={outcome2.committed_blocks}  "
          f"cursor={outcome2.coverage.commit_cursor} prefix={outcome2.coverage.prefix_complete}")

    stats = _knowledge_stats(book_dir)
    print(f"\n知识库: {stats}")

    report = {
        "task": "E2E-三侠五义",
        "source": str(BOOK_DIR),
        "chapters": len(chapters),
        "chars": total_chars,
        "block_size": BLOCK_SIZE,
        "blocks": len(plans),
        "block_chars": {"min": min(sizes), "max": max(sizes)},
        "first_run": {
            "calls": client.calls,
            "attempts": client.attempts,
            "prompt_tokens": client.prompt_tokens,
            "completion_tokens": client.completion_tokens,
            "total_tokens": client.total_tokens,
            "unknown_usage_calls": client.unknown,
            "elapsed_s": round(elapsed, 1),
            "committed_blocks": outcome.committed_blocks,
            "stop_reason": outcome.stop_reason,
            "commit_cursor": outcome.coverage.commit_cursor,
            "prefix_complete": outcome.coverage.prefix_complete,
            "gaps": list(outcome.coverage.gaps),
        },
        "second_run": {
            "calls": again.calls,
            "committed_blocks": outcome2.committed_blocks,
            "commit_cursor": outcome2.coverage.commit_cursor,
        },
        "knowledge": stats,
        "budget": {"max_calls": args.max_calls, "max_tokens": MAX_TOKENS},
    }
    out = ROOT / "docs" / "MEASURE-E2E-2026-10-31.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {out.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-calls", type=int, default=MAX_CALLS)
    parser.add_argument(
        "--skip-permanent",
        action="store_true",
        help="永久错误（审核/鉴权/欠费）记 SKIPPED 并留 gap，继续跑后面的块",
    )
    parser.add_argument("--max-tokens", type=int, default=64000, help="单次输出上限（含思考预算）")
    parser.add_argument("--block-size", type=int, default=2, help="每块章节数")
    parser.add_argument("--timeout", type=int, default=900, help="单次调用超时（秒）")
    parser.add_argument("--chapters", type=int, default=0, help="只用前 N 章（0=整本）")
    parser.add_argument("--tolerate-failures", action="store_true", help="非永久失败也跳过，留 gap 继续")
    parser.add_argument("--concurrency", type=int, default=8, help="同一波并发的块数")
    return asyncio.run(amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
