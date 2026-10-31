#!/usr/bin/env python
"""C2 实测：用标注集（30 条）评"事实核查"环节的召回与误报。

标注口径（`docs/annotations-C2-2026-10-31.json`）：
- `SUPPORTED`：正文逐字支持；
- `NOT_STATED`：正文未提及但不矛盾；
- `CONTRADICTED`：正文与之冲突。

与生产契约的映射（必须写明，否则数字无法解释）：
- 标注的 `NOT_STATED` ↔ 生产的 `INSUFFICIENT`（"原文没写"就是"证据不足"）；
- 生产的 `DIFFERENT_ANSWER` 在本任务里没有对应标注类别，出现即计入"其它"。

指标定义（只用这 30 条，**不给置信区间**）：
- 召回：标注支持且被判 SUPPORTED / 标注支持总数；
- 误报（把没支持的说成支持）：标注非支持且被判 SUPPORTED / 标注非支持总数；
- 引用定位率：判 SUPPORTED 且引文通过逐字校验的比例。

安全边界：≤30 次调用 / 200000 token。

用法::

    python scripts/measure_c2_annotations.py
    python scripts/measure_c2_annotations.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path


for _key in [k for k in list(os.environ) if "proxy" in k.lower()]:
    os.environ.pop(_key, None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.vnext.resolution import render_recheck_schema  # noqa: E402
sys.path.insert(0, str(ROOT / "scripts"))

MAX_CALLS = 30
MAX_TOKENS = 200_000
ANNOTATIONS = ROOT / "docs" / "annotations-C2-2026-10-31.json"

SYSTEM = (
    "你是长篇小说事实核查员。判断给定陈述是否被提供的原文片段支持。\n"
    "只依据片段本身的字面内容，不做语用推理，不补充常识。\n"
    "支持时必须给逐字引文，不许改写标点。只输出 JSON。"
)

SCHEMA = "\n".join(
    [
        "输出 JSON（字段名由 backend.vnext.resolution.render_recheck_schema() 渲染；"
        "不许在脚本里手写——2026-10-31 就是因为手写键名，两轮 36 次调用白烧）：",
        render_recheck_schema(),
        "判定口径：",
        "- SUPPORTED：片段明确指出该伏笔已回收；必须给 evidence_id 与逐字 supporting_quote"
        "（supporting_span 可选，只用于多处出现时指路）；",
        "- DIFFERENT_ANSWER：回收了，但答案与埋设时的指向不同；",
        "- CONTRADICTED：片段明确否定或与该伏笔冲突；",
        "- INSUFFICIENT：片段里没有能定论的内容。",
    ]
)



def _texts() -> dict[int, str]:
    import measure_c3_sampling as c3

    texts, _truth = c3._corpus_texts()
    return texts


def _messages(item: dict, text: str, context_chars: int = 900) -> tuple[list[dict], str]:
    start = text.find(item["quote"])
    if start < 0:
        window_text, base = text[:context_chars], 0
    else:
        base = max(0, start - context_chars // 3)
        window_text = text[base : base + context_chars]
    body = f"[ev_1] 第 {item['chapter']} 章片段：\n{window_text}"
    user = (
        f"候选 id：{item['id']}\n"
        f"待核查陈述：{item['fact']}\n\n"
        f"{body}\n\n"
        "请判断该陈述是否被上面的片段支持。\n\n" + SCHEMA
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], window_text


async def amain(args) -> int:
    import measure_c3_sampling as c3
    from backend.vnext import resolution

    data = json.loads(ANNOTATIONS.read_text(encoding="utf-8"))
    items = data["items"]
    texts = _texts()
    print(f"标注 {len(items)} 条，来自 {ANNOTATIONS.name}")

    if args.dry_run:
        for item in items[:3]:
            print(" ", item["id"], item["expected"], item["fact"][:30])
        return 0

    client = c3.CountingClient(c3._load_client())
    rows = []
    started = time.monotonic()

    for item in items:
        if client.calls >= MAX_CALLS or client.total_tokens >= MAX_TOKENS:
            print("预算上限，停止")
            break
        text = texts.get(int(item["chapter"]), "")
        messages, fragment = _messages(item, text)
        raw = await client.chat(messages)
        payload = c3._extract_json(str(getattr(raw, "content", "") or ""))
        verdict = resolution.parse_recheck(
            payload,
            allowed_candidate_ids={item["id"]},
            allowed_evidence_ids={"ev_1"},
            # 偏移是相对**片段**的（模型看到的 ev_1 正文），不是相对整条消息
            quote_within_range=lambda quote, s, e: 0 <= s < e <= len(fragment)
            and fragment[s:e] == quote,
        )
        expected = item["expected"]
        expected_norm = resolution.INSUFFICIENT if expected == "NOT_STATED" else expected
        rows.append(
            {
                "id": item["id"],
                "chapter": item["chapter"],
                "expected": expected,
                "model_verdict": payload.get("verdict"),
                "parsed_verdict": verdict.verdict,
                "reason": verdict.reason,
                "match": verdict.verdict == expected_norm,
            }
        )
        print(f"  {item['id']} 期望 {expected:<12} 得到 {verdict.verdict:<14} {verdict.reason or ''}")

    elapsed = time.monotonic() - started
    positives = [r for r in rows if r["expected"] == "SUPPORTED"]
    negatives = [r for r in rows if r["expected"] != "SUPPORTED"]
    recall = (sum(1 for r in positives if r["parsed_verdict"] == "SUPPORTED") / len(positives)
              if positives else None)
    false_positive = (sum(1 for r in negatives if r["parsed_verdict"] == "SUPPORTED") / len(negatives)
                      if negatives else None)
    located = [r for r in rows if r["parsed_verdict"] == "SUPPORTED"]
    located_ok = [r for r in located if not r["reason"]]

    report = {
        "task": "C2",
        "annotations_file": ANNOTATIONS.name,
        "items": len(rows),
        "mapping": {"NOT_STATED": "INSUFFICIENT",
                    "DIFFERENT_ANSWER": "标注集里没有对应类别，出现即计入其它"},
        "calls": client.calls,
        "attempts": client.attempts,
        "prompt_tokens": client.prompt_tokens,
        "completion_tokens": client.completion_tokens,
        "total_tokens": client.total_tokens,
        "unknown_usage_calls": client.unknown,
        "elapsed_s": round(elapsed, 1),
        "budget": {"max_calls": MAX_CALLS, "max_tokens": MAX_TOKENS,
                   "within_budget": client.calls < MAX_CALLS and client.total_tokens < MAX_TOKENS},
        "metrics": {
            "recall_on_supported": recall,
            "false_positive_rate_on_non_supported": false_positive,
            "quote_located_rate_among_supported": (len(located_ok) / len(located)) if located else None,
            "exact_match_rate": sum(1 for r in rows if r["match"]) / len(rows) if rows else None,
            "note": "30 条样本，不给置信区间；标注者是模型，人工复核范围见 docs/MEASURE-C2-2026-10-31.md",
        },
        "confusion": {
            f"{r['expected']}->{r['parsed_verdict']}": sum(
                1 for x in rows if x["expected"] == r["expected"] and x["parsed_verdict"] == r["parsed_verdict"]
            )
            for r in rows
        },
        "rows": rows,
    }
    out = ROOT / "docs" / "MEASURE-C2-2026-10-31.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"\n调用 {client.calls} / token {client.total_tokens} / {elapsed:.1f}s")
    print(f"召回 {recall}  误报 {false_positive}  精确匹配 {report['metrics']['exact_match_rate']}")
    print(f"报告已写入 {out.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    return asyncio.run(amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
