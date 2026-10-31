#!/usr/bin/env python
"""C3 实测：同一个候选问题，重复采样 + 证据顺序打乱，看判定会不会翻。

要回答的问题只有一个：**这套"接口 B + 程序比对"的判定稳不稳**。
不稳的话，"自动判定已回收"就会变成随机事件，宁可退回"证据不足"。

做法（复审修订版，不用"隐藏候选"）：
- 语料真值里有 3 个伏笔（2 个已回收 + 1 个同章先埋后收），各构一个候选；
- 每个候选跑 **2 种证据顺序**（按故事序 / 逆序）× **3 次采样**；
- 用生产解析器 `resolution.parse_recheck` + 真实校验器（`quote in evidence_text`）判读，
  不自己另写一套判定，否则测的不是真链路。

安全边界：≤90 次调用 / 200000 token；超限即停并如实报告未完成。

用法::

    python scripts/measure_c3_sampling.py
    python scripts/measure_c3_sampling.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path


for _key in [k for k in list(os.environ) if "proxy" in k.lower()]:
    os.environ.pop(_key, None)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.vnext.resolution import RECHECK_MODEL_EXAMPLE  # noqa: E402

MAX_CALLS = 90
MAX_TOKENS = 200_000
SAMPLES = 3
ORDERS = ("story", "reverse")

SYSTEM = (
    "你是长篇小说伏笔核查员。判断给定伏笔是否已在提供的原文片段中被回收。\n"
    "只依据给出的片段作答：片段里没有的信息，不许当成已知。\n"
    "必须先用证据里的原话回答，再给判定；要给逐字引文，不要改写标点。\n"
    "只输出 JSON，不要解释。"
)

SCHEMA = "\n".join(
    [
        "输出 JSON（字段名由 backend.vnext.resolution.RECHECK_MODEL_EXAMPLE 渲染；"
        "不许在脚本里手写——2026-10-31 就是因为手写键名，两轮 36 次调用白烧）：",
        RECHECK_MODEL_EXAMPLE,
        "判定口径：",
        "- SUPPORTED：片段明确指出该伏笔已回收；必须给 evidence_id 与逐字 supporting_quote"
        "（supporting_span 可选，只用于多处出现时指路）；",
        "- DIFFERENT_ANSWER：回收了，但答案与埋设时的指向不同；",
        "- CONTRADICTED：片段明确否定或与该伏笔冲突；",
        "- INSUFFICIENT：片段里没有能定论的内容。",
    ]
)



class CountingClient:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0
        self.attempts = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.unknown = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        result = await self.inner.chat(messages, **kwargs)
        self.attempts += int(getattr(result, "attempts", 1) or 1)
        tokens = getattr(result, "tokens", None)
        if tokens is None:
            self.unknown += 1
        else:
            self.prompt_tokens += tokens[0]
            self.completion_tokens += tokens[1]
        return result

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _load_client():
    from backend.config.settings import APIConfig
    from backend.core.llm_client import LLMClient

    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    fields = {f.name for f in APIConfig.__dataclass_fields__.values()}
    api = {k: v for k, v in (raw.get("api") or {}).items() if k in fields}
    return LLMClient(APIConfig(**api))


def _corpus_texts() -> dict[int, str]:
    from backend.tests.fixtures.vnext_corpus import build_corpus

    corpus_dir = Path(tempfile.mkdtemp(prefix="c3corpus-"))
    corpus = build_corpus(corpus_dir)
    chapter_root = Path(corpus.root) / "blocks"
    texts: dict[int, str] = {}
    for ordinal in range(1, 25):
        path = chapter_root / f"ch_{ordinal:04d}.txt"
        if path.exists():
            texts[ordinal] = path.read_text(encoding="utf-8")
    return texts, corpus.truth


def _build_candidates(truth) -> list[dict]:
    """从语料真值构候选：埋设章号/引文 + 回收章号（有的话）。"""
    out = []
    for index, (name, item) in enumerate(truth.foreshadows.items(), start=1):
        out.append(
            {
                "candidate_id": f"cd_{index:04d}",
                "entity_id": f"fs_{name}",
                "question": "这条伏笔是否已在后文被回收？",
                "plant_chapter": item["plant_chapter"],
                "plant_quote": item["plant_quote"],
                "resolve_chapter": item.get("resolve_chapter"),
            }
        )
    return out


def _evidence(candidate, texts) -> list[dict]:
    """证据 = 埋设章的相关片段 + 回收章（若已知）的上下文窗口。

    每段带 `ev_k` id；`text` 是**原文片段**，校验器只认它里面的逐字子串。
    """
    from backend.vnext import context

    pieces = []
    plant_text = texts[candidate["plant_chapter"]]
    start = plant_text.find(candidate["plant_quote"])
    if start >= 0:
        window = context.build_context(
            plant_text, span_start=start, span_end=start + len(candidate["plant_quote"])
        )
        pieces.append(
            {
                "evidence_id": "ev_1",
                "chapter": candidate["plant_chapter"],
                "text": window.text,
                "window_start": window.start,
            }
        )
    resolve = candidate.get("resolve_chapter")
    if resolve and resolve in texts:
        resolve_text = texts[resolve]
        # 取该章全文的前 1200 字符作为"后文片段"（不预先告诉模型答案在哪）
        take = resolve_text[:1200]
        pieces.append(
            {
                "evidence_id": "ev_2",
                "chapter": resolve,
                "text": take,
                "window_start": 0,
            }
        )
    return pieces


def _messages(candidate, pieces, order) -> list[dict]:
    ordered = list(pieces) if order == "story" else list(reversed(pieces))
    body = []
    for piece in ordered:
        body.append(f"[{piece['evidence_id']}] 第 {piece['chapter']} 章片段：\n{piece['text']}")
    user = (
        f"候选 id：{candidate['candidate_id']}\n"
        f"伏笔：{candidate['plant_quote']}\n"
        f"问题：{candidate['question']}\n\n"
        "以下是原文片段（按给定顺序，不是全文）：\n\n" + "\n\n".join(body) + "\n\n" + SCHEMA
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def _extract_json(text: str) -> dict:
    """模型偶尔裹 ```json 或多余前言，抠出第一个平衡的大括号块。"""
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        return {}
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


async def amain(args) -> int:
    from backend.vnext import resolution

    texts, truth = _corpus_texts()
    candidates = _build_candidates(truth)
    print(f"候选 {len(candidates)} 个；顺序 {ORDERS} × 采样 {SAMPLES} 次")

    if args.dry_run:
        for candidate in candidates:
            pieces = _evidence(candidate, texts)
            print(f"  {candidate['candidate_id']} 埋设ch{candidate['plant_chapter']} "
                  f"回收ch{candidate['resolve_chapter']} 证据 {len(pieces)} 段")
        return 0

    client = CountingClient(_load_client())
    rows: list[dict] = []
    started = time.monotonic()

    for candidate in candidates:
        pieces = _evidence(candidate, texts)
        allowed_evidence = {piece["evidence_id"] for piece in pieces}
        by_id = {piece["evidence_id"]: piece["text"] for piece in pieces}

        # 生产解析器的校验器签名是 (quote, start, end) —— 拿不到 evidence_id。
        # 因此只能验"这个片段在**某个**证据里逐字对上"，无法验它属于模型点名的那个片段。
        # 这是契约缺陷（已记入 MEASURE 报告），不能靠脚本绕过。
        def verifier(quote: str, start: int, end: int, _texts=tuple(by_id.values())) -> bool:
            if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end):
                return False
            return any(0 <= end <= len(text) and text[start:end] == quote for text in _texts)

        for order in ORDERS:
            for sample in range(SAMPLES):
                if client.calls >= MAX_CALLS or client.total_tokens >= MAX_TOKENS:
                    print("预算上限，停止")
                    break
                messages = _messages(candidate, pieces, order)
                raw = await client.chat(messages)
                content = str(getattr(raw, "content", "") or "")
                payload = _extract_json(content)
                verdict = resolution.parse_recheck(
                    payload,
                    allowed_candidate_ids={candidate["candidate_id"]},
                    allowed_evidence_ids=allowed_evidence,
                    # D-19：位置由程序算，而且**按 evidence_id 取对应片段的正文**
                    # （旧校验器只能验"在某一段里对得上"，验不了"是点名那一段"）
                    evidence_text=lambda eid, _m=by_id: _m.get(eid, ""),
                    locate=lambda quote, eid, _m=by_id: resolution.locate_quote(
                        quote, _m.get(eid, "")
                    ),
                )
                rows.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "order": order,
                        "sample": sample + 1,
                        "model_verdict": payload.get("verdict"),
                        "parsed_verdict": verdict.verdict,
                        "reason": verdict.reason,
                        "evidence_id": payload.get("evidence_id"),
                        "supporting_quote": payload.get("supporting_quote"),
                        "located_span": list(verdict.located_span) if verdict.located_span else None,
                        "ambiguous": verdict.ambiguous,
                        "attempts": int(getattr(raw, "attempts", 1) or 1),
                    }
                )
                print(f"  {candidate['candidate_id']} {order:<8} #{sample+1} "
                      f"→ {verdict.verdict} {verdict.reason or ''}")

    elapsed = time.monotonic() - started

    # 翻转率：同一 (候选, 顺序) 组内是否出现不同判定
    groups: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        groups.setdefault((row["candidate_id"], row["order"]), set()).add(row["parsed_verdict"])
    unstable = [key for key, values in groups.items() if len(values) > 1]
    by_candidate: dict[str, dict] = {}
    for candidate in candidates:
        cid = candidate["candidate_id"]
        story = sorted(
            r["parsed_verdict"] for r in rows if r["candidate_id"] == cid and r["order"] == "story"
        )
        reverse = sorted(
            r["parsed_verdict"] for r in rows if r["candidate_id"] == cid and r["order"] == "reverse"
        )
        by_candidate[cid] = {"story": story, "reverse": reverse,
                             "order_sensitive": len(set(story) | set(reverse)) > 1}

    report = {
        "task": "C3",
        "design": {
            "candidates": len(candidates),
            "orders": list(ORDERS),
            "samples_per_group": SAMPLES,
            "note": "重复采样替代隐藏候选；证据顺序按故事序/逆序对比",
        },
        "calls": client.calls,
        "attempts": client.attempts,
        "prompt_tokens": client.prompt_tokens,
        "completion_tokens": client.completion_tokens,
        "unknown_usage_calls": client.unknown,
        "elapsed_s": round(elapsed, 1),
        "budget": {"max_calls": MAX_CALLS, "max_tokens": MAX_TOKENS,
                   "within_budget": client.calls < MAX_CALLS and client.total_tokens < MAX_TOKENS},
        "unstable_groups": [{"candidate_id": cid, "order": order, "verdicts": sorted(values)}
                            for (cid, order), values in groups.items() if len(values) > 1],
        "unstable_group_count": len(unstable),
        "group_count": len(groups),
        "by_candidate": by_candidate,
        "verdict_histogram": {
            verdict: sum(1 for row in rows if row["parsed_verdict"] == verdict)
            for verdict in sorted({row["parsed_verdict"] for row in rows})
        },
        "rows": rows,
    }
    out = ROOT / "docs" / "MEASURE-C3-2026-10-31.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")

    print(f"\n调用 {client.calls} / token {client.total_tokens} / 耗时 {elapsed:.1f}s")
    print(f"不稳定组 {len(unstable)}/{len(groups)}")
    print(f"判定分布 {report['verdict_histogram']}")
    print(f"报告已写入 {out.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    return asyncio.run(amain(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
