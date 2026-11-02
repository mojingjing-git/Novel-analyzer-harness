#!/usr/bin/env python3
"""
scripts/sync_models_dev.py
从 models.dev 拉取或读取 api.json，抽取并净化推理控制（reasoning）、
上下文与输出上限（limits）、Token 定价（cost），生成轻量内置底包 backend/data/models_dev.json。
"""

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = "https://models.dev/api.json"
TARGET_FILE = Path(__file__).resolve().parent.parent / "backend" / "data" / "models_dev.json"


def normalize_key(name: str) -> str:
    """归一化模型标识：小写、去除厂商前缀、去除常见后缀（保留代际版本号）"""
    s = name.strip().lower()
    if "/" in s:
        s = s.split("/")[-1]
    if ":" in s:
        s = s.split(":")[0]
    # 去除常见的 preview/latest/chat/online/free 后缀，但严格保留代际号（如 -v3, -r1, -m3.1, -flash, -mini）
    s = re.sub(r"-(preview|latest|chat|online|free)(-\d{8})?$", "", s)
    s = re.sub(r"-\d{8}$", "", s)  # 去除 8 位纯日期后缀
    return s


def build_curated_specs(raw_data: dict) -> dict:
    """从 models.dev api.json 中清洗并抽取模型字典"""
    models_out = {}

    for provider_id, p_info in raw_data.items():
        if not isinstance(p_info, dict):
            continue
        models = p_info.get("models", {})
        if not isinstance(models, dict):
            continue

        for m_id, m_info in models.items():
            if not isinstance(m_info, dict):
                continue

            norm_id = normalize_key(m_id)
            if not norm_id:
                continue

            reasoning = bool(m_info.get("reasoning", False))
            reasoning_opts = m_info.get("reasoning_options", []) or []

            # 确定推荐模式
            recommended_mode = {}
            reasoning_type = "none"
            effort_values = []

            for opt in reasoning_opts:
                t = opt.get("type")
                if t == "effort":
                    reasoning_type = "effort"
                    effort_values = opt.get("values", [])
                    if "low" in effort_values:
                        recommended_mode = {"reasoning_effort": "low"}
                    elif effort_values:
                        recommended_mode = {"reasoning_effort": effort_values[0]}
                elif t == "budget_tokens" and reasoning_type != "effort":
                    reasoning_type = "budget_tokens"
                    min_tokens = opt.get("min", 1024)
                    recommended_mode = {"thinking": {"type": "enabled", "budget_tokens": min_tokens}}
                elif t == "toggle" and reasoning_type == "none":
                    reasoning_type = "toggle"
                    recommended_mode = {"enable_thinking": False}

            limit = m_info.get("limit") or {}
            cost = m_info.get("cost") or {}

            # 清洗 limit
            clean_limit = {}
            if "context" in limit and isinstance(limit["context"], (int, float)) and limit["context"] > 0:
                clean_limit["context"] = int(limit["context"])
            if "output" in limit and isinstance(limit["output"], (int, float)) and limit["output"] > 0:
                clean_limit["output"] = int(limit["output"])

            # 清洗 cost
            clean_cost = {}
            for c_key in ("input", "output", "reasoning", "cache_read", "cache_write"):
                if c_key in cost and isinstance(cost[c_key], (int, float)):
                    clean_cost[c_key] = float(cost[c_key])

            # 判断是否为常开思考模型（无 toggle，无 none 选项）
            is_always_on = (
                reasoning
                and reasoning_type == "effort"
                and "none" not in effort_values
                and not any(opt.get("type") == "toggle" for opt in reasoning_opts)
                and ("minimax" in (m_info.get("family", "") or "").lower() or "minimax" in m_id.lower())
            )

            entry = {
                "id": m_id,
                "name": m_info.get("name", m_id),
                "family": m_info.get("family", ""),
                "reasoning": reasoning,
                "reasoning_type": reasoning_type,
                "is_always_on": is_always_on,
                "effort_values": effort_values,
                "recommended_mode": recommended_mode,
                "limit": clean_limit,
                "cost": clean_cost,
                "provider": provider_id,
            }

            # 若已存在，优先保留有明确 reasoning_options / cost 的第一方官方供应商数据
            if norm_id in models_out:
                old = models_out[norm_id]
                # 如果新 entry 有推荐模式而旧 entry 没有，或者旧 entry 无 cost 而新 entry 有
                if (not old.get("recommended_mode") and recommended_mode) or (not old.get("cost") and clean_cost):
                    models_out[norm_id] = entry
            else:
                models_out[norm_id] = entry

    return {
        "version": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_models": len(models_out),
        "models": models_out,
    }


def main():
    parser = argparse.ArgumentParser(description="Sync models.dev catalog into backend/data/models_dev.json")
    parser.add_argument("--source", default=DEFAULT_URL, help="URL or local path to models.dev api.json")
    parser.add_argument("--out", default=str(TARGET_FILE), help="Output path for models_dev.json")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[*] Reading models.dev from: {args.source}")
    if args.source.startswith("http://") or args.source.startswith("https://"):
        req = urllib.request.Request(args.source, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    else:
        with open(args.source, "r", encoding="utf-8") as f:
            raw = json.load(f)

    print("[*] Processing and extracting specs...")
    curated = build_curated_specs(raw)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(curated, f, ensure_ascii=False, indent=2)

    print(f"[+] Successfully wrote {curated['total_models']} models to {out_path} ({out_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
