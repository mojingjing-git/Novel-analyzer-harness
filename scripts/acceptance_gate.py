#!/usr/bin/env python
"""打捞结果自动验收器 v1（PLAN round2 后续：验收自动化第一步）。

三层判定：
1. 机械增量检测（确定性）：支持引文/答案与登记时已知信息（锚点原文、description）
   的字面重合——引文即已知原文或答案高相似 → "复述已知"，直接拒绝；
2. 回答回路（NLI，单次小调用）：只看 问题+判定标准+登记已知+答案，判断答案是否
   在已知信息之外构成有效回答（restating/off_topic/answer_ok/unclear）；
3. 裁决合成：机械拒绝 或 NLI=restating/off_topic → 拒绝；NLI=unclear 或增量存疑
   → 存疑；其余 → 接受。

评测模式（--evaluate）：对带独立复核标签的考卷跑判定，输出混淆矩阵——
拦截率（不支持被判拒绝）、保留率（支持被判接受）、存疑率。达标线见 PLAN。

用法：
  python -u scripts/acceptance_gate.py --exam acceptance_exam_20261104.json \
    --output-dir "workspace/《道诡异仙》/output" [--model ...] [--base-url ...]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = "localhost,127.0.0.1"

from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.llm_client import LLMClient  # noqa: E402

#: 答案的 2-gram 有多少比例来自已知信息——高于此值视为"无增量"（硬拒绝）
ANSWER_COVERAGE_REJECT = 0.85
#: 引文最短长度（低于此不参与子集判定）
MIN_QUOTE_CHARS = 12


def norm(s: str) -> str:
    """归一化：去空白与常见标点（语义比较用）。"""
    return re.sub(r"[\s，,。；;：:、（）()\"'“”‘’！!？?…—\-]", "", s or "")


def ngrams(s: str, n: int = 2) -> set[str]:
    if len(s) < n:
        return set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def ngram_dice(a: str, b: str, n: int = 2) -> float:
    """字符 n-gram Dice 系数（0~1）。"""
    ga, gb = ngrams(a, n), ngrams(b, n)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def criteria_logic(criteria: str) -> str:
    """判定标准的关系类型：OR（任一分支满足即可）/ AND（全部满足）/ UNKNOWN。

    OR 优先：出现"或/任一/之一"即视为择一；否则出现"以及/和/与/并"为合取。
    """
    c = criteria or ""
    if re.search(r"或|任一|之一", c):
        return "OR"
    if re.search(r"以及|和|与|并", c):
        return "AND"
    return "UNKNOWN"


#: 排他性表述（criteria 要求排除时，答案必须含其中之一）
EXCLUSION_MARKERS = ("不是", "没有其他", "只有", "排除", "并非", "唯一")


def mechanical_increment_check(item: dict) -> tuple[list[str], list[str]]:
    """机械检测。返回 (硬缺陷, 软疑点)。

    硬缺陷（直接拒绝）：
    - 引文互为已知原文子串 → 复述（fs_384 型）；
    - 答案的 2-gram 覆盖率（答案中来自 已知信息 的比例）≥0.85 → 无增量。
    软疑点（转存疑，不直接拒绝）：
    - 证据章 ≤ 锚点章 → 同段/更早内容，不构成"后文回收"；但同章后段的新答案
      也可能成立（复核标准），故不硬拒，交人工或 NLI 语义层确认。
    """
    hard: list[str] = []
    soft: list[str] = []
    quote_n = norm(item.get("quote") or "")
    answer_n = norm(item.get("answer") or "")
    anchor_n = norm(item.get("anchor_quote") or "")
    desc_n = norm(item.get("description") or "")
    known_n = anchor_n + desc_n
    if len(quote_n) >= MIN_QUOTE_CHARS and anchor_n:
        if (quote_n in anchor_n) or (anchor_n in quote_n):
            hard.append("quote_equals_anchor")
        elif known_n and quote_n in known_n:
            hard.append("quote_in_known")
    if len(answer_n) >= 10 and known_n:
        ga, gb = ngrams(answer_n), ngrams(known_n)
        coverage = len(ga & gb) / len(ga)
        if coverage >= ANSWER_COVERAGE_REJECT:
            hard.append("answer_in_known")
    chapter = item.get("chapter")
    anchor_ch = item.get("anchor_chapter")
    if isinstance(chapter, int) and isinstance(anchor_ch, int) and chapter > 0 and anchor_ch > 0:
        if chapter <= anchor_ch:
            soft.append("evidence_not_after_anchor")
    # 排他性：criteria 要求排除其他可能，但答案没有排除性表述 → 需人工核（fs_1000 型）
    if re.search(r"排[除他]|只[有是]|无其他|没有其他", item.get("criteria") or ""):
        if not any(m in (item.get("answer") or "") for m in EXCLUSION_MARKERS):
            soft.append("exclusivity_unverified")
    return hard, soft


NLI_PROMPT = """你是打捞结果验收员。判断一条候选答案是否构成对问题的有效回答。

判定规则（只能输出以下四种之一）：
1. restating：答案内容与"登记时已知信息"相同或仅为转写——没有信息增量。
   注意：即使答案在原文中真实存在，若信息量不超出已知，也算 restating。
2. off_topic：答案回答的不是问题所问。以下情形一律 off_topic：
   a. **合取型**判定标准（下方"判定标准类型"标为 AND）要求多个条件全部满足，
      答案只满足其中一部分；
   b. 合取型标准下，答案自认部分覆盖（"虽未说明…""虽然…但已回答"）——部分覆盖不算覆盖；
      **注意：判定标准类型为 OR 时，回答任一完整分支即算满足，自认未答其它分支不算缺陷。**
   c. 问题要求确认"是谁/哪些/是否没有其他"时，答案只确认其一、**没有引用明确的排除性原文**
      （"不是…而是…""没有其他""只有"）——确认其一 ≠ 排除其他；
   d. 答案所述事件、人物或时段与问题指向的不是同一事件（例如用另一条时间线的现象、
      另一个角色的遭遇来解释本条问题）。
3. answer_ok：答案在已知信息之外，给出了针对问题的新事实/新解释/新结果，
   且满足判定标准（OR 型满足任一分支；AND 型须全部满足）。
4. unclear：信息不足以判断。

只依据给出的材料，不要用你对该小说的任何记忆。拿不准时选 unclear，不要选 answer_ok。

问题：{question}
判定标准：{criteria}
**判定标准类型：{logic}**（OR=任一分支满足即可；AND=必须全部满足；UNKNOWN=按语义判断）
登记时已知信息：{known}
候选答案：{answer}
答案所依据的原文引文：{quote}
证据章与锚点章：证据章第{chapter}章，锚点（登记）第{anchor_chapter}章

输出：纯 JSON 对象 {{"verdict": "answer_ok", "reason": "一句话依据"}}
"""


def build_nli_prompt(item: dict) -> str:
    known = f"description：{item.get('description') or '（无）'}；锚点原文（第{item.get('anchor_chapter')}章）：{item.get('anchor_quote') or '（无）'}"
    return NLI_PROMPT.format(question=item.get("question") or "", criteria=item.get("criteria") or "",
                             logic=criteria_logic(item.get("criteria") or ""),
                             known=known, answer=item.get("answer") or "", quote=item.get("quote") or "",
                             chapter=item.get("chapter") or "?", anchor_chapter=item.get("anchor_chapter") or "?")


def extract_json_object(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def gate(item: dict, nli: dict | None, nli_error: str | None = None) -> dict:
    """合成裁决：接受 / 拒绝 / 存疑。硬缺陷直接拒绝；软疑点与 NLI 不清 → 存疑。"""
    hard, soft = mechanical_increment_check(item)
    if hard:
        return {"decision": "拒绝", "reasons": [f"机械增量缺陷：{','.join(hard)}"]}
    verdict = (nli or {}).get("verdict") if isinstance(nli, dict) else None
    if nli_error:
        return {"decision": "存疑", "reasons": [f"NLI 调用失败：{nli_error[:80]}"]}
    if verdict in ("restating", "off_topic"):
        return {"decision": "拒绝",
                "reasons": [f"NLI={verdict}：{(nli or {}).get('reason', '')[:80]}"]}
    if verdict == "unclear":
        return {"decision": "存疑", "reasons": ["NLI=unclear"]}
    if verdict == "answer_ok":
        if soft:
            return {"decision": "存疑",
                    "reasons": [f"机械软疑点：{','.join(soft)}（证据不晚于锚点章，需人工确认同章后段是否成立）"]}
        return {"decision": "接受",
                "reasons": [f"NLI=answer_ok：{(nli or {}).get('reason', '')[:60]}"]}
    return {"decision": "存疑", "reasons": ["无充分接受依据"]}


async def run_evaluate(args) -> int:
    cfg = ConfigManager(ROOT / "config.json").load()
    api = cfg.api
    from dataclasses import replace as dc_replace
    if args.model:
        api = dc_replace(api, model=args.model)
    if args.base_url:
        api = dc_replace(api, base_url=args.base_url)
    log = print
    log(f"NLI 通道：{api.model} @ {api.base_url}")
    llm = LLMClient(api)
    max_tokens = min(int(cfg.analysis.recheck_max_tokens), 1024)

    async def call_model(prompt: str) -> str:
        last: Exception | None = None
        for attempt in range(3):
            try:
                res = await llm.chat_auto(messages=[{"role": "user", "content": prompt}],
                                          max_tokens=max_tokens)
                if not getattr(res, "success", True):
                    raise RuntimeError(str(getattr(res, "error", "") or "未知错误"))
                return getattr(res, "content", "") or ""
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt * 2)
        raise last  # type: ignore[misc]

    exam = json.loads(Path(args.exam).read_text(encoding="utf-8"))["items"]
    sem = asyncio.Semaphore(args.concurrency)

    async def one(item: dict) -> dict:
        async with sem:
            try:
                raw = await call_model(build_nli_prompt(item))
                nli = extract_json_object(raw)
            except Exception as e:  # noqa: BLE001
                return {"fs_id": item["fs_id"], "label": item["label"],
                        **gate(item, None, str(e)[:120])}
            nli_verdict = (nli or {}).get("verdict") if isinstance(nli, dict) else None
            return {"fs_id": item["fs_id"], "label": item["label"], "nli_verdict": nli_verdict,
                    "nli_reason": (nli or {}).get("reason", ""), **gate(item, nli)}

    results = list(await asyncio.gather(*(one(it) for it in exam)))

    # 混淆矩阵
    def cnt(label, decision):
        return sum(1 for r in results if r["label"] == label and r["decision"] == decision)

    sup, rej, disp = ("支持", "不支持", "待裁决")
    keep = cnt(sup, "接受")
    block = cnt(rej, "拒绝")
    disp_doubt = cnt(disp, "存疑")
    agree = keep + block
    total_sr = sum(1 for r in results if r["label"] in (sup, rej))
    lines = [
        "# 自动验收器 v1 评测（考卷 = 独立复核 55 条，标签待人工确认）", "",
        f"- NLI 通道：{api.model} @ {api.base_url}",
        f"- 考卷：{len(results)} 条（支持 31 / 不支持 12 / 待裁决 12）", "",
        "## 混淆矩阵", "",
        "| 标签 \\ 判定 | 接受 | 拒绝 | 存疑 |", "|---|---|---|---|",
        f"| 支持（应接受） | {cnt(sup, '接受')} | {cnt(sup, '拒绝')} | {cnt(sup, '存疑')} |",
        f"| 不支持（应拒绝） | {cnt(rej, '接受')} | {cnt(rej, '拒绝')} | {cnt(rej, '存疑')} |",
        f"| 待裁决（期望存疑） | {cnt(disp, '接受')} | {cnt(disp, '拒绝')} | {cnt(disp, '存疑')} |", "",
        "## 核心指标", "",
        f"- **误报拦截率 = {block}/12 = {block/12:.0%}**（不支持被拒）",
        f"- **支持保留率 = {keep}/31 = {keep/31:.0%}**（支持被接）",
        f"- 一致率（支持+不支持，{total_sr} 条）= {agree}/{total_sr} = {agree/max(total_sr,1):.0%}",
        f"- 待裁决→存疑 = {disp_doubt}/12（应高：机器不该替人裁决）",
        f"- 危险错误（不支持被判接受）= {cnt(rej, '接受')}", "",
        "## 逐条", "",
        "| fs_id | 标签 | 判定 | NLI | 理由 |", "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r['fs_id']} | {r['label']} | {r['decision']} | "
                     f"{r.get('nli_verdict', 'ERROR')} | {(r.get('nli_reason') or ';'.join(r.get('reasons', [])))[:60]} |")
    report = Path(args.output_dir) / f"acceptance_gate_eval_{args.stamp}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    detail = Path(args.output_dir) / f"acceptance_gate_eval_{args.stamp}.json"
    detail.write_text(json.dumps({"results": results, "metrics": {
        "block_rate": block / 12, "keep_rate": keep / 31,
        "agreement": agree / max(total_sr, 1), "dangerous": cnt(rej, "接受"),
    }}, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"拦截率 {block}/12 | 保留率 {keep}/31 | 一致率 {agree}/{total_sr} | 危险错误 {cnt(rej, '接受')}")
    log(f"报告：{report}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="打捞结果自动验收器 v1")
    ap.add_argument("--exam", help="考卷 json（评测模式）")
    ap.add_argument("--output-dir", default="workspace/《道诡异仙》/output")
    ap.add_argument("--model", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--stamp", default="")
    args = ap.parse_args()
    args.stamp = args.stamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output_dir and not Path(args.output_dir).is_absolute():
        args.output_dir = str(ROOT / args.output_dir)
    return asyncio.run(run_evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
