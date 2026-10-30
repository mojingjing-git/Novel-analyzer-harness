#!/usr/bin/env python
"""S9 观测：用真实模型测"提示词各段花多少 token"，并对比两种历史组织方式。

PLAN v2 §4 S9-2 要求"使用模型实际 usage，无法精确计算时注明估算器与误差，不用字符比例
充当注意力指标"。因此这里**不猜**：每次调用后读模型返回的 usage。

两种组织方式（PLAN §4 S9-3 的 A/B 的最小版本）：

* ``A_duplicated``：既有做法——"完整故事历史"（滚动主线）与"前情摘要"（逐章）同时给；
* ``B_dedup``：去掉重复，只留按来源排序的逐章摘要。

用途：给"要不要去重"提供真实费用证据，而不是凭直觉改 Prompt。

用法::

    python scripts/measure_prompt_cost.py                 # 默认 1 章，2 次调用
    python scripts/measure_prompt_cost.py --chapters 3    # 更多章（更贵）
    python scripts/measure_prompt_cost.py --dry-run       # 只打印 prompt 长度，不调用

安全边界：默认最多 4 次调用、总 token 上限 200000；超限直接停并如实报告"未完成"。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# 本机环境里的 NO_PROXY 含 "[::1]"（带方括号），httpx 会把 ":1]" 当端口解析并抛
# InvalidURL。这是环境缺陷不是代码缺陷，在**进程内**清掉——子进程改父环境无效。
for _key in [k for k in list(os.environ) if "proxy" in k.lower()]:
    os.environ.pop(_key, None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAX_CALLS = 4
MAX_TOKENS = 200_000

SYSTEM = (
    "你是长篇小说分析助手。逐块分析正文，输出 JSON。"
    "不要复述原文，不要编造未出现的信息。"
)


def _load_config() -> dict:
    path = ROOT / "config.json"
    if not path.is_file():
        raise SystemExit("找不到 config.json（需要其中的 API 配置）")
    return json.loads(path.read_text(encoding="utf-8"))


def _sample_chapters(count: int) -> list[str]:
    """构造确定性的样本正文（不读用户书库，避免把真实作品发出去）。"""
    chapters = []
    for index in range(1, count + 1):
        paragraphs = [
            f"　　第{index}章清晨，方醒推开木门，院里积着薄雪。",
            f"　　他把昨夜的账册摊开，逐条核对第{index}笔出入。"
            f"数字对不上，差了三钱银子，伙计说是灯油钱。",
            f"　　午后方醒去了城东的米铺，与掌柜谈定了下月的粮价。"
            f"回来的路上，他听见有人在议论北边的战事。",
        ]
        chapters.append("\n".join(paragraphs))
    return chapters


def _history_block(chapters: list[str], *, duplicated: bool) -> str:
    """两种历史组织方式。``duplicated`` 复刻旧做法（主线 + 逐章各来一遍）。"""
    lines = [f"[第{i}章摘要] {text.strip()[:60]}…" for i, text in enumerate(chapters, start=1)]
    if not duplicated:
        return "[前情摘要]\n" + "\n".join(lines)
    rolling = "[完整故事历史]\n" + f"主线：主角在雪夜清账，逐步卷入粮价与战事。（共 {len(chapters)} 章）"
    return rolling + "\n[前情摘要]\n" + "\n".join(lines)


def _build_prompt(chapters: list[str], *, duplicated: bool) -> list[dict]:
    body = "\n".join(chapters)
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": (
                f"{_history_block(chapters, duplicated=duplicated)}\n\n"
                f"[本块正文]\n{body}\n\n"
                "请输出 JSON：{\"core_events\": [], \"character_arcs\": []}"
            ),
        },
    ]


def _char_count(messages: list[dict]) -> int:
    return sum(len(m["content"]) for m in messages)


async def _measure(client, messages: list[dict]) -> dict:
    result = await client.chat(messages)
    usage = getattr(result, "tokens", None)
    return {
        "ok": bool(getattr(result, "success", False)),
        "error": getattr(result, "error", "") or "",
        "prompt_tokens": usage[0] if usage else None,
        "completion_tokens": usage[1] if usage else None,
        "usage_status": getattr(getattr(result, "usage_status", None), "value", None),
        "chars": _char_count(messages),
    }


async def main_async(args: argparse.Namespace) -> int:
    chapters = _sample_chapters(args.chapters)
    variants = {"A_duplicated": True, "B_dedup": False}
    rows: list[dict] = []
    total_tokens = 0
    calls = 0

    client = None
    if not args.dry_run:
        from backend.core.llm_client import LLMClient

        from backend.config.settings import APIConfig

        raw = _load_config()
        # config.json 是扁平字典，APIConfig 是 dataclass：按字段名显式挑选，
        # 不整包传（多出来的键会让 dataclass 直接报 TypeError）。
        api_fields = {f.name for f in APIConfig.__dataclass_fields__.values()}
        api_kwargs = {k: v for k, v in (raw.get("api") or {}).items() if k in api_fields}
        client = LLMClient(APIConfig(**api_kwargs))

    for name, duplicated in variants.items():
        messages = _build_prompt(chapters, duplicated=duplicated)
        chars = _char_count(messages)
        if args.dry_run:
            print(f"{name}: {chars} 字符（dry-run，未调用）")
            rows.append({"variant": name, "chars": chars})
            continue
        if calls >= MAX_CALLS or total_tokens >= MAX_TOKENS:
            rows.append({"variant": name, "skipped": "预算上限，未调用"})
            continue
        calls += 1
        row = await _measure(client, messages)
        row["variant"] = name
        total_tokens += (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)
        rows.append(row)
        print(f"{name}: prompt={row['prompt_tokens']} completion={row['completion_tokens']} "
              f"ok={row['ok']} {row['error'][:60]}")

    out = {
        "chapters": args.chapters,
        "calls": calls,
        "total_tokens": total_tokens,
        "rows": rows,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="测提示词各段的真实 token 花费")
    parser.add_argument("--chapters", type=int, default=1, help="样本章节数（默认 1）")
    parser.add_argument("--dry-run", action="store_true", help="只打印长度，不调用模型")
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
