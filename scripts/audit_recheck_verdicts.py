"""逐条审计复检结论：SUPPORTED 到底真不真。

跑完一轮长书之后，产物里只有"SUPPORTED 19 / INSUFFICIENT 20"这种计数，
**判的是哪一条、拿哪句话判的**只能靠逐条明细文件才能回看。这支脚本把回看
变成可重复的流程：

1. **章号闭包**（0 成本）：答案里引用的"第 N 章"必须出现在该候选的证据段里。
   越界说明模型在拿证据范围之外的东西编答案。
2. **专名闭包**（0 成本）：答案里的多字词在证据里找不到 → 候选过判信号
   （实测抓到过一条：答案写"陷空岛"，而证据 2877 字里没有这个词）。
   裸 n-gram 噪声大，这里只统计不判定。
3. **盲读者复核**（每条 1 次调用，可选）：换一个模型，只给"事项 + 证据"，
   不给任何结论，问它原文里到底有没有交代。
   **它说"没有"不等于原文没有**——实测盲读者漏判过证据里明写着答案的样本，
   所以这里只当旁证，不当判据。
4. **乱序复核**（每条 1 次调用，可选）：把证据段顺序打乱再问，
   看结论是不是只依赖"哪一段排在前面"。

用法：
    python scripts/audit_recheck_verdicts.py --verdicts docs/data/verdicts-dfm-full.json \
        --book "runtime/workspace/《大奉打更人》" --out docs/data/audit-dfm-full.json
    # 加 --blind-model codex/gpt-6-luna 才会真的调模型
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_CH_REF = re.compile(r"第\s*([0-9零〇一二三四五六七八九十百千万两]{1,4})\s*章")
_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(text: str) -> int:
    text = text.strip()
    if text.isdigit():
        return int(text)
    if "十" in text:
        head, tail = text.split("十", 1)
        return (_CN.get(head, 1) if head else 1) * 10 + (_CN.get(tail, 0) if tail else 0)
    return _CN.get(text, 0)


def _drop_proxy_env() -> None:
    """本机网关直连：清掉代理变量，否则 httpx 会在 `[::1]` 上抛 Invalid port。"""
    for key in list(os.environ):
        if "proxy" in key.lower():
            os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"


def _load_book(book_dir: Path):
    """从书目录读回章级产物 → 候选条目 + 按回号索引的原文。"""
    from backend.core.anchor import entity_key_for, split_block_into_chapters
    from backend.models.analysis_result import AnalysisResult
    from backend.utils.foreshadow_ledger import ForeshadowItem
    from backend.utils.text_utils import deduplicate_foreshadows

    results = []
    for path in sorted((book_dir / "output").glob("chapter_*_result.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        result = AnalysisResult.from_dict(data)
        results.append((getattr(result, "chapter_number", 0) or 0, result))

    clues = []
    for ch, result in results:
        for fs in result.foreshadowing:
            clues.append((ch, fs.clue, fs.type, fs.confidence, fs.importance,
                          getattr(fs, "anchor_quote", "") or "",
                          getattr(fs, "anchor_chapter", 0),
                          getattr(fs, "motif_key", "") or "",
                          getattr(fs, "question", "") or "",
                          getattr(fs, "answer_criteria", "") or ""))

    items = {}
    for cat in deduplicate_foreshadows(clues):
        item = ForeshadowItem(
            id=cat["id"], description=cat["clue"],
            first_seen_chapter=cat["first_seen"], first_seen_batch=0,
            last_seen_chapter=cat["last_seen"], last_seen_batch=0,
            evidence_chapters=cat["evidence_chapters"], source_type="catalog_scan",
            anchor_quote=cat.get("anchor_quote", ""),
            anchor_chapter=cat.get("anchor_chapter", 0),
            entity_key=entity_key_for(cat.get("motif_key", ""), str(cat.get("id", ""))),
            question=cat.get("question", ""),
            answer_criteria=cat.get("answer_criteria", ""),
        )
        items[item.id] = item

    chapters: dict[int, str] = {}
    blocks_dir = book_dir / "blocks"
    for ordinal, path in enumerate(sorted(blocks_dir.glob("*.txt")), start=1):
        chapters.update(split_block_into_chapters(path.read_text(encoding="utf-8"), ordinal))
    return items, chapters, len(results)


BLIND = """你是审校员。下面是一条"待交代事项"和若干段原文（按故事顺序括注章号）。
请只依据原文判断：这件事在后文里是否已被**明确交代**。

规则：
- 只依据给出的原文，不要用常识、推断或原著记忆补全；
- 原文没有交代，就填 "found": false，answer 写 "无法确定"；
- 交代了才填 true，answer 用一句话写出答案，quote 逐字抄写支撑它的原文片段（不少于 6 字）。

待交代事项：{question}
判定为已回答的标准：{criteria}

原文：
{segments}

只输出 JSON：{{"found": true/false, "answer": "...", "quote": "..."}}"""

BLIND_REVERSED = """你是审校员。下面是一条"待交代事项"和若干段原文。
**注意：原文分段顺序已被打乱，不保证故事先后顺序**，请按内容判断。
只依据原文，没有明确交代就填 "found": false。

待交代事项：{question}
判定为已回答的标准：{criteria}

原文：
{segments}

只输出 JSON：{{"found": true/false, "answer": "...", "quote": "..."}}"""


def _render(segs, reversed_: bool = False) -> str:
    if reversed_:
        segs = list(reversed(segs))
    return "\n\n".join(f"[第{ch}章]\n\"\"\"\n{txt}\n\"\"\"" for ch, txt in segs)


def _parse_json_reply(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {"found": None, "answer": (text or "")[:160], "quote": "", "parsed": False}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {"found": None, "answer": (text or "")[:160], "quote": "", "parsed": False}
    return {
        "found": bool(data.get("found")),
        "answer": str(data.get("answer", ""))[:240],
        "quote": str(data.get("quote", ""))[:160],
        "parsed": True,
    }


async def _audit(verdicts, items, chapters, blind_model: str, limit: int, concurrency: int):
    from backend.config.paths import runtime_path
    from backend.core.evidence import EvidenceIndex
    from backend.config.settings import ConfigManager
    from backend.core.llm_client import LLMClient

    index = EvidenceIndex(chapters)
    rows = [r for r in verdicts if r.get("verdict") == "SUPPORTED"]
    if limit:
        rows = rows[:limit]

    client = None
    if blind_model:
        cfg = ConfigManager(runtime_path("config.json")).load()
        cfg.api.base_url = "http://127.0.0.1:3425/v1"
        cfg.api.model = blind_model
        cfg.api.max_tokens = 4000
        cfg.api.timeout = 300
        client = LLMClient(cfg.api)

    sem = asyncio.Semaphore(max(1, concurrency))

    async def ask(prompt: str) -> str:
        async with sem:
            res = await client.chat_auto(
                messages=[{"role": "user", "content": prompt}], max_tokens=4000
            )
            return getattr(res, "content", "") or ""

    async def one(row: dict) -> dict:
        item = items.get(row["candidate_id"])
        out = dict(row)
        if item is None:
            out["audit"] = {"error": "账本里找不到这条候选"}
            return out
        bundle = index.bundle_for(
            anchor_chapter=item.anchor_chapter,
            anchor_quote=(item.anchor_quote or "").strip(),
            hint_texts=[getattr(item, "question", "") or "", getattr(item, "description", "") or ""],
        )
        segs = [(ch, bundle.text[s:e].strip()) for ch, s, e in bundle.spans]
        evidence = "\n".join(t for _c, t in segs)
        ev_chapters = sorted({ch for ch, _t in segs})

        refs = [_cn_to_int(x) for x in _CH_REF.findall(row.get("answer") or "")]
        out["audit"] = {
            "question": getattr(item, "question", "") or "",
            "answer_criteria": getattr(item, "answer_criteria", "") or "",
            "evidence_chapters": ev_chapters,
            "evidence_chars": len(evidence),
            "segments": len(segs),
            "chapter_refs": refs,
            "chapter_refs_outside_evidence": [r for r in refs if r not in ev_chapters],
            "absent_3grams": sorted(
                {g for run in _CJK_RUN.findall(row.get("answer") or "")
                 for g in (run[i:i + 3] for i in range(len(run) - 2))
                 if g not in evidence}
            )[:12],
        }
        if client is None:
            return out
        q = out["audit"]["question"]
        crit = out["audit"]["answer_criteria"]
        blind_prompt = BLIND.format(question=q, criteria=crit, segments=_render(segs))
        rev_prompt = BLIND_REVERSED.format(question=q, criteria=crit, segments=_render(segs, True))
        blind, rev = await asyncio.gather(ask(blind_prompt), ask(rev_prompt))
        out["audit"]["blind"] = _parse_json_reply(blind)
        out["audit"]["blind_reversed"] = _parse_json_reply(rev)
        return out

    return await asyncio.gather(*[one(r) for r in rows])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdicts", required=True, help="逐条判定明细 JSON")
    ap.add_argument("--book", default="", help="书目录（含 output/ 与 blocks/）")
    ap.add_argument("--out", default="", help="审计结果输出路径")
    ap.add_argument("--blind-model", default="", help="盲读者模型（留空 = 只做 0 成本检查）")
    ap.add_argument("--limit", type=int, default=0, help="只审前 N 条")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    _drop_proxy_env()
    verdicts = json.loads(Path(args.verdicts).read_text(encoding="utf-8"))
    rows = verdicts["verdicts"] if isinstance(verdicts, dict) else verdicts

    book_dir = Path(args.book) if args.book else None
    items: dict = {}
    chapters: dict = {}
    if book_dir is not None:
        items, chapters, n_results = _load_book(book_dir)
        print(f"读回 {n_results} 章产物，{len(items)} 条候选，{len(chapters)} 回原文")
    else:
        print("未给 --book：只做不依赖原文的检查")

    audited = asyncio.run(
        _audit(rows, items, chapters, args.blind_model, args.limit, args.concurrency)
    )

    outside = [a for a in audited if a.get("audit", {}).get("chapter_refs_outside_evidence")]
    skipped = [a for a in audited if "chapter_refs_outside_evidence" not in a.get("audit", {})]
    print(f"\n审计 {len(audited)} 条 SUPPORTED")
    print(f"  答案引用了证据之外的章号：{len(outside)} 条")
    if skipped:
        # 没给 --book 时账本里找不到条目，这些条**没有真正审**——不能算成"没问题"
        print(f"  未真正审计（账本里找不到条目）：{len(skipped)} 条")
    if args.blind_model:
        both = sum(
            1 for a in audited
            if (a["audit"].get("blind") or {}).get("found")
            and (a["audit"].get("blind_reversed") or {}).get("found")
        )
        denied = sum(
            1 for a in audited
            if (a["audit"].get("blind") or {}).get("found") is False
            and (a["audit"].get("blind_reversed") or {}).get("found") is False
        )
        print(f"  盲读者两轮都认为有交代：{both} 条")
        print(f"  盲读者两轮都认为没交代：{denied} 条")

    out_path = Path(args.out) if args.out else Path("docs/data") / f"audit-{time.strftime('%Y%m%d-%H%M')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "verdicts_file": str(args.verdicts),
        "blind_model": args.blind_model,
        "audited": len(audited),
        "rows": audited,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"审计结果写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
