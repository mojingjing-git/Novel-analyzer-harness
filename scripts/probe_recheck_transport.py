#!/usr/bin/env python3
"""传输层探针：验证「本地网关 + codex/gpt-6-luna」这条判定通道是否可用。

只做一件事：发 1~2 次**极小**请求，把 TTFT、总耗时、success、content 长度、错误原文
打出来。用来区分三种情况：

1. 通道正常（success=True、content 非空、秒级返回）→ 判定链路的问题在别处；
2. 请求被拒（400/401/404）→ 会打印 API 原始错误，不再被 `content or ""` 吞掉；
3. 请求挂住 → 单次 60s 硬超时，不会再出现"看着像卡死、其实是退避重试"。

用法：
    python -u scripts/probe_recheck_transport.py
    python -u scripts/probe_recheck_transport.py --model codex/gpt-6-luna --effort low
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 本机回环不能被代理接管。注意：**不能只 setdefault**——本机 NO_PROXY 里的
# ``[::1]`` 会让 httpx 把它当 URL 解析（port ':1]'），在 OpenAI 客户端**构造期**
# 直接抛 InvalidURL（ci.yml 里为同一原因显式重写过 NO_PROXY）。这里强制覆盖。
for _key in ("NO_PROXY", "no_proxy"):
    os.environ[_key] = "localhost,127.0.0.1"
os.environ.pop("ALL_PROXY", None)
os.environ.pop("all_proxy", None)

from backend.config.paths import runtime_path  # noqa: E402
from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.llm_client import LLMClient  # noqa: E402


async def one_call(llm: LLMClient, prompt: str, timeout: float) -> dict:
    """发一次请求，返回 {ttft, total, success, chars, error, sample}。"""
    first: dict[str, float] = {}

    async def on_progress(chunk) -> None:  # noqa: ANN001 - 协议对象，按需取字段
        if "t" not in first:
            first["t"] = time.perf_counter()

    started = time.perf_counter()
    try:
        res = await asyncio.wait_for(
            llm.chat_auto(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=256,
                on_progress=on_progress,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {
            "total": time.perf_counter() - started,
            "ttft": first.get("t", 0) - started if "t" in first else None,
            "success": False,
            "chars": 0,
            "error": f"超时（>{timeout}s）",
            "sample": "",
        }
    except Exception as exc:  # 传输层异常必须原样暴露
        return {
            "total": time.perf_counter() - started,
            "ttft": first.get("t", 0) - started if "t" in first else None,
            "success": False,
            "chars": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "sample": "",
        }

    content = getattr(res, "content", "") or ""
    return {
        "total": time.perf_counter() - started,
        "ttft": (first["t"] - started) if "t" in first else None,
        "success": bool(getattr(res, "success", True)),
        "chars": len(content),
        "error": str(getattr(res, "error", "") or ""),
        "sample": content.strip().replace("\n", " ")[:80],
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:3425/v1")
    ap.add_argument("--model", default="codex/gpt-6-luna")
    ap.add_argument("--effort", default="low", help="reasoning_effort；空串表示不改")
    ap.add_argument("--max-tokens", type=int, default=32768)
    ap.add_argument("--timeout", type=float, default=60.0, help="单次硬超时（秒）")
    ap.add_argument("--times", type=int, default=2)
    args = ap.parse_args()

    cfg = ConfigManager(runtime_path("config.json")).load()
    api = replace(
        cfg.api,
        base_url=args.base_url,
        model=args.model,
        max_tokens=args.max_tokens,
        reasoning_effort=args.effort,
        streaming_enabled=True,
    )
    print("=== 本次探针参数 ===")
    print(f"base_url      : {api.base_url}")
    print(f"model         : {api.model}")
    print(f"reasoning     : {api.reasoning_effort or '(不改)'}")
    print(f"max_tokens    : {api.max_tokens}")
    print(f"streaming     : {api.streaming_enabled}")
    print(f"单次硬超时    : {args.timeout}s")
    print(f"HTTP(S)_PROXY : {os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY') or '(无)'}")
    print()

    llm = LLMClient(api)
    results = []
    for i in range(1, args.times + 1):
        r = await one_call(llm, "用一句话回答：你是谁", args.timeout)
        results.append(r)
        ttft = f"{r['ttft']:.2f}s" if r["ttft"] is not None else "—"
        print(
            f"[{i}] success={r['success']} chars={r['chars']} "
            f"ttft={ttft} total={r['total']:.2f}s"
        )
        if r["error"]:
            print(f"     error: {r['error'][:300]}")
        if r["sample"]:
            print(f"     回包: {r['sample']}")

    ok = all(r["success"] and r["chars"] > 0 and r["total"] < args.timeout for r in results)
    print()
    print("=== 结论 ===")
    if ok:
        print("通道可用：两次调用都成功、非空、未超时 → 进段 B（单候选复检）")
        return 0
    print("通道不可用 → 先修传输层，不要进段 B")
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
