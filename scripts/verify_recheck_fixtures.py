"""复检的四组已知答案样本：检索有没有找到、裁决有没有判对。

为什么要单独做这个：真书 16 章里根本没有回收，跑出来全是"证据不足"，
而那既可能是"正确地说看不见"，也可能是"根本没去找"。要分开，只能构造
答案已知的样本。

四组：
  A 跨章正例   —— 第 2 章埋、第 9 章明确交代 → 应判 SUPPORTED，章号应为 9
  B 答案在章末 —— 同上，但答案放在第 9 章最末尾 → 验证取窗会不会跟过去
  C 反证       —— 第 9 章明确说"没人动过" → 不得判 SUPPORTED
  D 截断       —— 命中章多于窗口上限 → 纯离线，验证 truncated 与章数

用法：
    python scripts/verify_recheck_fixtures.py --base-url http://127.0.0.1:3425/v1 \
      --model wb2api/cn:hy3 --max-tokens 8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.evidence import MAX_WINDOWS, EvidenceIndex  # noqa: E402
from backend.core.llm_client import LLMClient  # noqa: E402
from backend.services.recheck_v2 import run_recheck_v2  # noqa: E402
from backend.utils.foreshadow_ledger import ForeshadowItem  # noqa: E402

FILLER = "街上人来人往，铺子里卖着各色杂货，谁也没在意天色。" * 40
SETUP = "方醒打开铜匣，铜匣底下那层是空的——到底谁先动过？"
QUESTION = "谁先动过铜匣底下的夹层？"
CRITERIA = "后文明确交代动过夹层的人是谁"


def chapters_for(answer: str, *, answer_at_end: bool) -> dict[int, str]:
    """造 9 章：第 2 章埋设，第 9 章给答案（或反证）。"""
    data = {i: f"第{i}章\n{FILLER}" for i in range(1, 10)}
    data[2] = f"第二章\n{SETUP}{FILLER}"
    if answer_at_end:
        data[9] = f"第九章\n{FILLER}{answer}"
    else:
        data[9] = f"第九章\n{answer}{FILLER}"
    return data


def make_item(item_id: str) -> ForeshadowItem:
    return ForeshadowItem(
        id=item_id,
        description="铜匣底部有夹层，且已被人动过",
        first_seen_chapter=2,
        first_seen_batch=0,
        last_seen_chapter=2,
        last_seen_batch=0,
        anchor_quote="铜匣底下那层是空的",
        anchor_chapter=2,
        question=QUESTION,
        answer_criteria=CRITERIA,
    )


async def run_case(name: str, chapters: dict[int, str], llm: LLMClient) -> dict:
    async def call_model(prompt: str) -> str:
        res = await llm.chat_auto(messages=[{"role": "user", "content": prompt}], max_tokens=8000)
        return getattr(res, "content", "") or ""

    item = make_item(f"fx_{name}")
    t0 = time.time()
    outcome = await run_recheck_v2([item], chapters=chapters, call_model=call_model, max_calls=3)
    verdicts = [
        {
            "verdict": v.verdict,
            "reason": v.reason,
            "answer": v.answer,
            "located_span": v.located_span,
            "evidence_id": v.evidence_id,
        }
        for v in outcome.verdicts
    ]
    return {
        "case": name,
        "calls": outcome.calls,
        "seconds": round(time.time() - t0, 1),
        "verdicts": verdicts,
        "status": outcome.status,
        "not_checked": list(outcome.not_checked),
        "resolved_chapter": getattr(item, "resolved_chapter", None),
        "item_status": getattr(item, "status", None),
        "coverage": outcome.coverage,
    }


def check_truncation() -> dict:
    """离线：命中章多于窗口上限时，必须只给上限内的章，且 truncated 为真。"""
    chapters = {1: f"第一章\n{SETUP}{FILLER}"}
    for ch in range(3, 3 + MAX_WINDOWS + 3):
        chapters[ch] = f"第{ch}章\n{FILLER}有人提起铜匣，说匣底动过。{FILLER}"
    index = EvidenceIndex(chapters)
    bundle = index.bundle_for(anchor_chapter=1, anchor_quote="铜匣底下那层是空的", hint_texts=["铜匣"])
    returned = [ch for ch, _s, _e in bundle.spans]
    return {
        "case": "D_truncation",
        "chapters_returned": returned,
        "max_windows": MAX_WINDOWS,
        "truncated": bundle.truncated,
        "omitted": bundle.omitted,
        "within_limit": len(returned) <= MAX_WINDOWS + 1,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--out", default="docs/MEASURE-FIXTURES-2026-11-01.json")
    args = ap.parse_args()

    try:
        import pytest_isolation

        pytest_isolation.sanitize_proxy_env()
    except Exception as exc:
        print(f"代理净化跳过：{exc}", file=sys.stderr)

    cfg = ConfigManager(ROOT / "config.json").load()
    if args.base_url:
        cfg.api.base_url = args.base_url
    if args.model:
        cfg.api.model = args.model
    cfg.api.max_tokens = args.max_tokens

    results: list[dict] = []

    truncation = check_truncation()
    results.append(truncation)
    print(f"D 截断（离线）：展示 {len(truncation['chapters_returned'])} 章，"
          f"truncated={truncation['truncated']}，在限额内={truncation['within_limit']}")

    llm = LLMClient(cfg.api)
    cases = {
        "A_cross_chapter": ("后来方醒才知道，动过匣底的人正是老三。", False),
        "B_answer_at_end": ("直到多年后方醒才查明白，动过匣底的人是老三。", True),
        "C_counter_evidence": ("这些年再没人提过匣底的事，也没人承认动过。", False),
    }
    for name, (answer, at_end) in cases.items():
        out = await run_case(name, chapters_for(answer, answer_at_end=at_end), llm)
        results.append(out)
        verdicts = ", ".join(v["verdict"] for v in out["verdicts"]) or "（无判定）"
        print(f"{name}: 判定={verdicts} 状态={out['status']} 回收章={out['resolved_chapter']} "
              f"调用={out['calls']} 用时={out['seconds']}s")

    path = ROOT / args.out
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {path}")

    # 自检：把预期写进退出码，避免"看起来跑通了"
    ok = True
    a = next(r for r in results if r["case"] == "A_cross_chapter")
    b = next(r for r in results if r["case"] == "B_answer_at_end")
    c = next(r for r in results if r["case"] == "C_counter_evidence")
    for label, case in (("A", a), ("B", b)):
        cov = case.get("coverage") or {}
        checked_ok = cov.get("checked") == 1 and not case.get("not_checked")
        passed = (
            any(v["verdict"] == "SUPPORTED" for v in case["verdicts"])
            and case["resolved_chapter"] == 9
            and checked_ok
        )
        print(f"  自检 {label}: {'通过' if passed else '未通过'}（核对 {cov.get('checked')} 条）")
        ok = ok and passed

    # C 不能"空通过"：必须真的核对过、有有效判定、且不是回收类判定
    cov_c = c.get("coverage") or {}
    c_verdicts = [v["verdict"] for v in c["verdicts"]]
    c_passed = (
        bool(c_verdicts)
        and cov_c.get("checked") == 1
        and not c.get("not_checked")
        and all(v in {"INSUFFICIENT", "DIFFERENT_ANSWER", "CONTRADICTED"} for v in c_verdicts)
    )
    print(f"  自检 C: {'通过' if c_passed else '未通过'}（判定={c_verdicts or '无'}，核对 {cov_c.get('checked')} 条）")
    ok = ok and c_passed

    # D：截断必须由"实际遗漏"支撑
    d_ok = truncation["truncated"] and truncation["within_limit"] and truncation.get("omitted", 0) > 0
    print(f"  自检 D: {'通过' if d_ok else '未通过'}（遗漏 {truncation.get('omitted')} 章）")
    ok = ok and d_ok
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
