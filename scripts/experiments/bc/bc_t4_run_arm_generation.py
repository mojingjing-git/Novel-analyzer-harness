"""两臂生成运行器（严格受控单次传输，原子预留与结算）。

运行 A/B 两臂的小说伏笔登记生成：
- A 臂：相关摘句规则（common-system + arm-a-rule）
- B 臂：信息齐备且最早一句规则（common-system + arm-b-rule）

两臂的 user 消息完全相同，模型、采样参数 (temperature=0.1)、定位器严格一致。
输出经宿主在原文中唯一标定 [start, end) 跨度后落盘。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend.common.registration_usage_archive import (  # noqa: E402
    STAGE_GENERATION,
    UsageArchive,
)
from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.llm_client import LLMClient  # noqa: E402
from scripts.experiments.bc.bc_t4_registration_pilot_runner import (  # noqa: E402
    _sha,
    convert_to_host_schema,
    parse_generator_output,
    render_source_units,
    request_token_bound,
    run_guarded_llm_request,
)

PROP = ROOT / "docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal"

CHECKPOINTS: Mapping[str, int] = {
    "p1": 1,
    "p2": 2,
    "p3": 3,
}

PLACEHOLDERS = (
    "work_id",
    "prefix_id",
    "visible_range_end",
    "registration_checkpoint",
    "source_units_with_stable_ids",
)


def assemble(user_template: str, fields: dict) -> str:
    """逐个替换占位符，不在插入的正文上二次展开花括号。"""
    out = user_template
    for key in PLACEHOLDERS:
        out = out.replace("{" + key + "}", str(fields.get(key, "")))
    return out


def load_units(work_id: str) -> dict[int, str]:
    """读取已曝光作品的物理单元。"""
    blocks_dir = (
        ROOT
        / "workspace/bc-evaluation/t4-trial-20261106/runs"
        / work_id
        / "candidate/blocks"
    )
    if not blocks_dir.exists():
        raise FileNotFoundError(f"单元目录不存在：{blocks_dir}")
    units = {}
    for p in sorted(blocks_dir.glob("*.txt")):
        if p.stem.isdigit():
            units[int(p.stem)] = p.read_text(encoding="utf-8")
    if not units:
        raise ValueError(f"未找到单元文本：{blocks_dir}")
    return units


async def run_single_arm_generation(
    *,
    work_id: str,
    prefix_id: str,
    arm: str,
    model: str = "cn:deepseek-v4.1-flash",
    temperature: float = 0.1,
    max_output_tokens: int = 2000,
) -> dict:
    arm = arm.upper()
    if arm not in ("A", "B"):
        raise ValueError(f"未知臂：{arm}（仅支持 A 或 B）")
    if prefix_id not in CHECKPOINTS:
        raise ValueError(f"未知前缀：{prefix_id}")

    checkpoint = CHECKPOINTS[prefix_id]
    units = load_units(work_id)

    # 0. 硬前置检查：当前前缀的合格参考集必须已冻结且 pending_count == 0
    elig_path = PROP / f"reference-eligible-{work_id}-{prefix_id}.json"
    if not elig_path.is_file():
        raise FileNotFoundError(f"合格参考集尚未生成，禁止启动两臂生成：{elig_path}")
    elig_doc = json.loads(elig_path.read_text(encoding="utf-8"))
    if not elig_doc.get("frozen") or int(elig_doc.get("pending_count", 1)) != 0:
        raise ValueError(
            f"合格参考集未完成冻结（frozen={elig_doc.get('frozen')}, "
            f"pending_count={elig_doc.get('pending_count')}），禁止启动两臂生成：{elig_path}"
        )

    # 1. 装配 system 消息
    common_sys = (PROP / "common-system.txt").read_text(encoding="utf-8").strip()
    rule_file = PROP / f"arm-{arm.lower()}-rule.txt"
    if not rule_file.exists():
        raise FileNotFoundError(f"规则文件缺失：{rule_file}")
    arm_rule = rule_file.read_text(encoding="utf-8").strip()
    sys_txt = common_sys + "\n\n" + arm_rule

    # 2. 装配 user 消息（两臂必须完全一致）
    user_tpl = (PROP / "common-user-template.txt").read_text(encoding="utf-8")
    source = render_source_units(units, checkpoint)
    fields = {
        "work_id": work_id,
        "prefix_id": prefix_id,
        "visible_range_end": checkpoint,
        "registration_checkpoint": checkpoint,
        "source_units_with_stable_ids": source,
    }
    user_txt = assemble(user_tpl, fields)
    request_sha = _sha(sys_txt + "\n" + user_txt)

    # 3. 准备客户端与预算守卫
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    cfg = ConfigManager(ROOT / "config.json").load()
    api = replace(
        cfg.api,
        model=model,
        temperature=temperature,
        sdk_max_retries=0,
        max_retries=0,
        temperature_max_retries=1,
        backoff_max_retries=0,
        streaming_enabled=False,
    )
    client = LLMClient(api)
    archive_path = PROP / "reference-discovery-usage.json"
    projected_in = request_token_bound(sys_txt, user_txt, model=model)

    run_res = await run_guarded_llm_request(
        client=client,
        archive_path=archive_path,
        work_id=work_id,
        prefix_id=prefix_id,
        arm=arm,
        stage=STAGE_GENERATION,
        messages=[
            {"role": "system", "content": sys_txt},
            {"role": "user", "content": user_txt},
        ],
        projected_input_tokens=projected_in,
        projected_output_tokens=max_output_tokens,
        request_sha=request_sha,
        per_request_input_limit=20000,
        per_request_output_limit=2000,
        max_runner_attempts=1,
        extra={"rule_arm": arm},
    )

    if not run_res["ok"]:
        return {
            "ok": False,
            "work_id": work_id,
            "prefix_id": prefix_id,
            "arm": arm,
            "reason": run_res.get("reason"),
            "detail": run_res.get("detail"),
            "error": run_res.get("last_error"),
        }

    res = run_res["result"]
    raw = getattr(res, "content", "") or ""

    # 4. 解析与宿主定位
    parsed = parse_generator_output(raw)
    host_items = convert_to_host_schema(
        parsed,
        units=units,
        checkpoint=checkpoint,
        prefix_id=prefix_id,
        registration_checkpoint=checkpoint,
    )

    parse_ok = bool(parsed.get("parse_ok"))
    is_complete = parsed.get("complete") is True
    call_ok = parse_ok and is_complete
    fail_reason = None
    if not parse_ok:
        fail_reason = parsed.get("reason") or "parse_failed"
    elif not is_complete:
        fail_reason = "incomplete_output"

    record = {
        "schema": "registration_generation_v1",
        "work_id": work_id,
        "prefix_id": prefix_id,
        "arm": arm,
        "checkpoint": checkpoint,
        "visible_range_end": checkpoint,
        "registration_checkpoint": checkpoint,
        "model": model,
        "sampling": {"temperature": temperature, "reasoning_effort": ""},
        "request_sha256": request_sha,
        "raw_response": raw,
        "parsed": parsed,
        "host_items": host_items,
        "item_count": len(host_items),
        "valid_item_count": sum(1 for h in host_items if h.get("ok")),
        "parse_ok": parse_ok,
        "complete": is_complete,
        "status": "completed" if call_ok else "failed_or_incomplete",
        "reason": fail_reason,
        "completed_at": datetime.now().isoformat(),
    }

    out_path = PROP / f"generation-{work_id}-{prefix_id}-arm_{arm.lower()}.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    archive = UsageArchive.load(archive_path)
    return {
        "ok": call_ok,
        "reason": fail_reason,
        "saved": str(out_path.relative_to(ROOT)),
        "work_id": work_id,
        "prefix_id": prefix_id,
        "arm": arm,
        "item_count": len(host_items),
        "valid_item_count": sum(1 for h in host_items if h.get("ok")),
        "parse_ok": parse_ok,
        "complete": is_complete,
        "tokens": {
            "input": getattr(res, "tokens", [0, 0])[0],
            "output": getattr(res, "tokens", [0, 0])[1],
        },
        "usage_summary": archive.summary(),
    }


async def run_all_tasks(
    *,
    model: str = "cn:deepseek-v4.1-flash",
    temperature: float = 0.1,
    max_output_tokens: int = 2000,
    skip_existing: bool = True,
) -> list[dict]:
    # 启动前全量检查 6 个前缀合格参考集均已冻结且 pending_count == 0
    for w in ("t4_hist_holdout", "t4_game_holdout"):
        for p in ("p1", "p2", "p3"):
            ep = PROP / f"reference-eligible-{w}-{p}.json"
            if not ep.is_file():
                raise FileNotFoundError(f"合格参考集缺失：{ep}")
            ed = json.loads(ep.read_text(encoding="utf-8"))
            if not ed.get("frozen") or int(ed.get("pending_count", 1)) != 0:
                raise ValueError(f"合格参考集未完成冻结：{ep}")

    # 按照交替 A/B 顺序执行 12 次调用，防范先验次序偏差
    sequence = [
        ("t4_hist_holdout", "p1", "A"),
        ("t4_hist_holdout", "p1", "B"),
        ("t4_hist_holdout", "p2", "B"),
        ("t4_hist_holdout", "p2", "A"),
        ("t4_hist_holdout", "p3", "A"),
        ("t4_hist_holdout", "p3", "B"),
        ("t4_game_holdout", "p1", "B"),
        ("t4_game_holdout", "p1", "A"),
        ("t4_game_holdout", "p2", "A"),
        ("t4_game_holdout", "p2", "B"),
        ("t4_game_holdout", "p3", "B"),
        ("t4_game_holdout", "p3", "A"),
    ]
    results = []
    for idx, (w, p, arm) in enumerate(sequence, start=1):
        out_path = PROP / f"generation-{w}-{p}-arm_{arm.lower()}.json"
        if skip_existing and out_path.is_file():
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if existing.get("complete") is True and existing.get("parse_ok", True) is True:
                print(f"[{idx}/12] 跳过已完成任务: work={w}, prefix={p}, arm={arm}")
                results.append(
                    {
                        "ok": True,
                        "skipped": True,
                        "saved": str(out_path.relative_to(ROOT)),
                        "work_id": w,
                        "prefix_id": p,
                        "arm": arm,
                        "item_count": existing.get("item_count"),
                        "valid_item_count": existing.get("valid_item_count"),
                        "complete": True,
                    }
                )
                continue
        print(f"[{idx}/12] 启动两臂生成: work={w}, prefix={p}, arm={arm}, model={model}...")
        res = await run_single_arm_generation(
            work_id=w,
            prefix_id=p,
            arm=arm,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        results.append(res)
        if not res.get("ok"):
            print(f"任务在第 {idx} 次调用中止：{res}")
            break
        print(f"  -> 完成: items={res.get('item_count')}, valid={res.get('valid_item_count')}, tokens={res.get('tokens')}")
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work")
    ap.add_argument("--prefix")
    ap.add_argument("--arm", choices=["A", "B", "a", "b"])
    ap.add_argument("--run-all", action="store_true", help="按交替顺序顺序运行全部 12 个前缀两臂任务")
    ap.add_argument("--model", default="cn:deepseek-v4.1-flash")
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--max-output-tokens", type=int, default=2000)
    args = ap.parse_args()

    if args.run_all:
        results = asyncio.run(
            run_all_tasks(
                model=args.model,
                temperature=args.temperature,
                max_output_tokens=args.max_output_tokens,
            )
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if any(not r.get("ok") for r in results):
            sys.exit(1)
        return

    if not (args.work and args.prefix and args.arm):
        ap.error("请指定 --work、--prefix 与 --arm，或使用 --run-all")

    res = asyncio.run(
        run_single_arm_generation(
            work_id=args.work,
            prefix_id=args.prefix,
            arm=args.arm,
            model=args.model,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
        )
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
