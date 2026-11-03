#!/usr/bin/env python
"""伏笔打捞两段式管线（PLAN-foreshadow-rescue v3）。

Layer A（匹配，只读）：把账本未解问题清单对准管线已产出的蒸馏层
（每块结果的 cross_block.contextual_link / new_leads / updated_knowledge.world_building），
产出候选 {fs_id, source_block_id, refs, digest_quote}。
Layer B（验证，audit 型统一协议）：来源块原文 + resolution 指针章 ±5 进证据集，
逐字引文门禁 + 分支命中审计；VERIFIED 且关闭条件满足才落账。

默认影子账本（不写生产账本）；切正账需人工确认后 --apply。

用法（pilot）：
  python -u scripts/run_salvage.py \
    --ledger "workspace/《道诡异仙》/output/foreshadow_ledger.json" \
    --output-dir "workspace/《道诡异仙》/output" \
    --blocks "workspace/《道诡异仙》/blocks" \
    --mode pilot --sample 38 --seed 42 \
    --base-url http://127.0.0.1:3425/v1 --recheck-model minimax-cn/MiniMax-M3.1-Flash
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = "localhost,127.0.0.1"

from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.llm_client import LLMClient  # noqa: E402
from backend.services.recheck_v2 import (  # noqa: E402
    _evidence_id,
    _parse_audit_payload,
)
from backend.utils.foreshadow_ledger import ForeshadowLedger  # noqa: E402

#: Layer A 蒸馏流分块的字符预算（问题清单为稳定前缀另行计入）
STREAM_CHUNK_CHARS = 30_000
#: criteria 预登记 pass 每批条数
PREREG_BATCH = 50
#: Layer B 指针漂移缓冲
POINTER_RADIUS = 5

CHUNK_RE = re.compile(r"第\s*(\d+)\s*章|第\s*(\d+)\s*[-—~至到]\s*(\d+)\s*章")


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 装载


def load_distilled_stream(output_dir: Path) -> list[dict]:
    """蒸馏块列表：{block_id, chapter_range, text}——Layer A 的匹配面。"""
    chunks = []
    for p in sorted(output_dir.glob("chapter_*_result.json"),
                    key=lambda x: int(x.stem.split("_")[1])):
        d = json.loads(p.read_text(encoding="utf-8"))
        block_id = int(p.stem.split("_")[1])
        cb = d.get("cross_block") or {}
        uk = d.get("updated_knowledge") or {}
        start = int(d.get("chapter_number") or block_id)
        size = int(d.get("block_size") or 4)
        parts = [f"[蒸馏块 {block_id}，覆盖第{start}-{start + size - 1}章]"]
        if cb.get("contextual_link"):
            parts.append(f"跨线连接：{cb['contextual_link']}")
        for lead in cb.get("new_leads") or []:
            parts.append(f"新线索：{lead}")
        for fact in uk.get("world_building") or []:
            parts.append(f"世界观：{fact}")
        chunks.append({"block_id": block_id, "chapter_range": (start, start + size - 1),
                       "text": "\n".join(parts)})
    return chunks


def load_questions(items: list, skip_ids: set[str]) -> list[dict]:
    return [{"fs_id": it.id,
             "question": it.question or "",
             "criteria": it.answer_criteria or "",
             "description": it.description or ""}
            for it in items if it.id not in skip_ids]


# ---------------------------------------------------------------- LLM


def make_call_model(cfg, args):
    from dataclasses import replace as dc_replace

    # 通道：默认走 config 的 recheck 通道（独立于采集通道），CLI 显式覆盖优先
    api = cfg.api
    recheck_model = getattr(cfg.analysis, "recheck_model", "") or ""
    recheck_url = getattr(cfg.analysis, "recheck_base_url", "") or ""
    recheck_key = getattr(cfg.analysis, "recheck_api_key", "") or ""
    if recheck_model:
        api = dc_replace(api, model=recheck_model)
    if recheck_url:
        api = dc_replace(api, base_url=recheck_url)
    if recheck_key:
        api = dc_replace(api, api_key=recheck_key)
    effort = (args.reasoning_effort
              or getattr(cfg.analysis, "recheck_reasoning_effort", "")
              or "low")
    if args.recheck_model:
        api = dc_replace(api, model=args.recheck_model)
    if args.base_url:
        api = dc_replace(api, base_url=args.base_url)
    api = dc_replace(api, reasoning_effort=effort)
    log(f"判定通道：{api.model} @ {api.base_url}（effort={effort}）")
    llm = LLMClient(api)
    max_tokens = int(cfg.analysis.recheck_max_tokens)

    async def call_model(prompt: str) -> str:
        # 外层重试（预检 P2-3）：chat_auto 内部重试管 API 层；这里管网络/瞬时异常，
        # 38 条小样本每废 1 条候选都伤统计
        last: Exception | None = None
        for attempt in range(3):
            try:
                res = await llm.chat_auto(messages=[{"role": "user", "content": prompt}],
                                          max_tokens=max_tokens)
                # chat_auto 对 400/401 返回 success=False 不抛——必须 raise，否则整轮静默 0 判定
                if not getattr(res, "success", True):
                    raise RuntimeError(f"打捞调用失败：{getattr(res, 'error', '') or '未知错误'}")
                return getattr(res, "content", "") or ""
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt * 2)
        raise last  # type: ignore[misc]

    return call_model


def extract_json_array(text: str) -> list:
    """从回包里挖出 JSON 数组（容忍 ```json 围栏与前后杂text）。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    # 括号配平兜底
    start = text.find("[")
    if start < 0:
        return []
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start:i + 1])
                    return data if isinstance(data, list) else []
                except json.JSONDecodeError:
                    return []
    return []


# ---------------------------------------------------------------- Stage 1：criteria 预登记

PREREG_PROMPT = """你是判定标准分析员。下面每条伏笔有一条 answer_criteria（判定为"已回答"的标准）。
任务：把它切分为分支，并登记关闭条件。

规则（必须遵守）：
1. branch_id 固定为 c1、c2…（按原文顺序），text 必须逐字摘自 criteria 的对应片段。
2. **每个分支必须是完整命题**（谁/什么 + 做了什么 + 结果或状态），不能只抽主语、人物名、
   对象名或动作名。反例：criteria"丹阳子或瓶女之爹对瓶碎事件的发现与追查"——若拆成
   "丹阳子" / "瓶女之爹" / "对事件的发现与追查" 三个分支，只命中人物就会误关闭；
   正确做法是登记为**一个**分支"丹阳子或瓶女之爹对瓶碎事件的发现与追查"（或两个都带完整
   谓词的分支"丹阳子对瓶碎事件的发现与追查"/"瓶女之爹对瓶碎事件的发现与追查"）。
3. criteria 顶层含**明确的择一替代词"或"**且**各分支各自构成完整关闭条件** → closing =
   {{"type": "OR", "closable": [可独立关闭的 branch_id...]}}；
4. 多个"？"的多联问且无"或" → closing = {{"type": "AND"}}（全部分支满足才算回收）；
5. **"以及/和/与/加上"是并列合取，不是择一替代**——用它们连接的多个要求必须合在一个
   分支里（或拆成 AND），**禁止**拆成 OR 分支。实测反例：criteria"眼睛的归属、来历，
   以及计划目的"被拆成 OR 后只回答"目的"就误关闭了。引号内的"或"不算分隔符。
6. 分数/并列关系/条件从句等嵌套逻辑无法用扁平分支表达 → closing = {{"type": "UNCERTAIN"}}
   （该条转人工，不参与自动打捞）。**宁可 UNCERTAIN，也不要拆出会被误关闭的碎分支。**

共 {count} 条，fs_id 必须原样取自：{ids}。
输入条目：
{items}

输出：纯 JSON 数组，长度必须为 {count}：
[{{"fs_id": "...", "branches": [{{"branch_id": "c1", "text": "..."}}], "closing": {{"type": "AND"}}}}]
"""

#: 分支文本低于该长度视为"碎分支"（主语/名物词），按 UNCERTAIN 转人工
MIN_BRANCH_CHARS = 6


def _norm_text(s: str) -> str:
    """校验用的归一化：去空白与常见分隔标点（不影响语义比较）。"""
    return re.sub(r"[\s，,。；;：:、（）()\"'“”‘’]", "", s or "")


def _is_subsequence(needle: str, haystack: str) -> bool:
    """needle 的字符是否按序出现在 haystack 里（允许补全谓词，禁止编造内容）。"""
    it = iter(haystack)
    return all(ch in it for ch in needle)


def validate_prereg_row(row: dict, criteria: str) -> tuple[bool, str]:
    """预登记后验校验（fail-closed）：任一不满足即判 UNCERTAIN。

    - 分支非空，branch_id 唯一，text ≥ MIN_BRANCH_CHARS（防"丹阳子"式碎分支）；
    - 每个 branch text 的字符必须按序出现在 criteria 中（归一化子序列）——
      既拦编造内容，也允许"A/B 各自补全谓词"的合法展开；
    - closing：AND 需 ≥1 分支；OR 需 closable 非空且 ⊆ 已登记 branch_id；UNCERTAIN 直接放行。
    """
    branches = [b for b in (row.get("branches") or []) if isinstance(b, dict)]
    closing = row.get("closing") if isinstance(row.get("closing"), dict) else {}
    ctype = closing.get("type")
    if ctype == "UNCERTAIN":
        return True, "uncertain"
    if not branches:
        return False, "no_branches"
    norm_criteria = _norm_text(criteria)
    ids = [b.get("branch_id") for b in branches]
    if len(set(ids)) != len(ids):
        return False, "duplicate_branch_id"
    for b in branches:
        text = (b.get("text") or "").strip()
        norm_branch = _norm_text(text)
        if len(norm_branch) < MIN_BRANCH_CHARS:
            return False, f"fragment_branch:{text[:12]}"
        if not _is_subsequence(norm_branch, norm_criteria):
            return False, f"branch_not_in_criteria:{text[:12]}"
    if ctype == "AND":
        return (True, "and") if len(branches) >= 1 else (False, "and_no_branches")
    if ctype == "OR":
        closable = closing.get("closable") or []
        if not closable or any(c not in ids for c in closable):
            return False, "or_bad_closable"
        return True, "or"
    return False, f"bad_closing_type:{ctype}"


async def preregister_criteria(call_model, items: list[dict]) -> dict[str, dict]:
    """返回 {fs_id: {"branches": [...], "closing": {...}}}；UNCERTAIN 同样登记（由调用方分流）。"""
    reg: dict[str, dict] = {}
    for i in range(0, len(items), PREREG_BATCH):
        batch = items[i:i + PREREG_BATCH]
        ids = [b["fs_id"] for b in batch]
        item_text = "\n".join(
            f"- fs_id={b['fs_id']} | criteria: {b['criteria'] or '（空）'} | question: {b['question'][:60]}"
            for b in batch)
        prompt = PREREG_PROMPT.format(count=len(batch), ids=json.dumps(ids, ensure_ascii=False),
                                      items=item_text)
        rows = extract_json_array(await call_model(prompt))
        by_criteria = {b["fs_id"]: b["criteria"] for b in batch}
        got = 0
        rejected: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("fs_id") not in set(ids):
                continue
            fid_row = row["fs_id"]
            if not (isinstance(row.get("branches"), list) and isinstance(row.get("closing"), dict)):
                continue
            ok, why = validate_prereg_row(row, by_criteria.get(fid_row, ""))
            if not ok:
                # 后验校验不过 → UNCERTAIN 转人工（fail-closed：宁可人工，不可误关闭）
                rejected[fid_row] = why
                continue
            reg[fid_row] = {"branches": row["branches"], "closing": row["closing"]}
            got += 1
        for fid_row, why in rejected.items():
            reg[fid_row] = {"branches": [], "closing": {"type": "UNCERTAIN"},
                            "rejected": why}
        missing = [x for x in ids if x not in reg]
        # 缺失条目按 UNCERTAIN 兜底（预登记失败 ≠ 条目有问题，转人工最安全）
        for x in missing:
            reg[x] = {"branches": [], "closing": {"type": "UNCERTAIN"}, "rejected": "missing"}
        log(f"  预登记 {i // PREREG_BATCH + 1}/{(len(items) + PREREG_BATCH - 1) // PREREG_BATCH} 批："
            f"{got}/{len(batch)} 校验通过，{len(rejected)} 校验拒绝，{len(missing)} 缺失 → 均转人工")
        if rejected:
            sample = "; ".join(f"{k}:{v}" for k, v in list(rejected.items())[:5])
            log(f"    拒绝样例：{sample}")
    return reg


def closing_satisfied(closing: dict, branch_hits: list[dict], reg: dict) -> bool | None:
    """关闭条件是否满足。None = 无法判定（UNCERTAIN → 转人工）。

    AND 的 branch 全集取预登记的 reg["branches"]（PREREG 输出的 closing 里没有
    branches 字段——v2 曾因此恒 False，所有 AND 型 VERIFIED 全部 closing_unmet）。
    """
    ctype = closing.get("type")
    hit_ids = {b.get("branch_id") for b in branch_hits if isinstance(b, dict)}
    if ctype == "AND":
        need = {b.get("branch_id") for b in (reg.get("branches") or []) if isinstance(b, dict)}
        return bool(need) and need <= hit_ids
    if ctype == "OR":
        closable = set(closing.get("closable") or [])
        return bool(closable & hit_ids)
    return None  # UNCERTAIN：转人工


# ---------------------------------------------------------------- Stage 2：Layer A 匹配

LAYER_A_PROMPT = """你是伏笔线索匹配员。任务：在一段小说分析过程的"跨线连接笔记"里，找出**正面回答了问题清单中某条问题**的句子——即该句揭示/解释/兑现了问题的答案。

规则（必须遵守）：
1. fs_id 只能取自问题清单中存在的 id，禁止编造；
2. digest_quote 必须**逐字**摘自下方笔记原文（将被程序校验），不要改写；
3. "又提了一次/继续推进"不算回答；必须是解释、揭示、兑现悬念本身；
4. 指出该句属于哪个蒸馏块（source_block_id）以及它提到的章号及角色（setup=前情铺垫 / resolution=给出答案 / context=背景）；
5. 不确定就跳过——宁缺毋滥，漏一条的代价远小于错一条。

问题清单（{count} 条）：
{questions}

跨线连接笔记（本段来自蒸馏块 {block_ids}）：
{stream}

输出：纯 JSON 数组（没有匹配就输出 []）：
[{{"fs_id": "...", "source_block_id": 1005, "source_chapter_range": [1005, 1008],
   "refs": [{{"chapter": 981, "role": "setup"}}, {{"chapter": 1007, "role": "resolution"}}],
   "digest_quote": "笔记原文逐字摘录", "rationale": "该句如何回答该问题", "confidence": "high|medium|low"}}]
"""


async def layer_a_match(call_model, questions: list[dict], stream: list[dict],
                  *, max_chapter: int = 1046) -> tuple[list[dict], dict]:
    """返回 (候选列表, 统计)。chunk 切分按字符预算；问题清单每轮原样重复（稳定前缀）。"""
    q_text = "\n".join(f"- {q['fs_id']} | {q['question']} | 标准: {q['criteria']} | 背景: {q['description']}"
                       for q in questions)
    q_ids = {q["fs_id"] for q in questions}
    block_text = {c["block_id"]: c["text"] for c in stream}
    candidates: list[dict] = []
    stats = {"passes": 0, "raw_matches": 0, "rejected_bad_id": 0, "rejected_quote": 0,
             "rejected_block": 0}
    # 流切分
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for c in stream:
        cur.append(c)
        size += len(c["text"])
        if size >= STREAM_CHUNK_CHARS:
            chunks.append(cur)
            cur, size = [], 0
    if cur:
        chunks.append(cur)

    for ci, chunk in enumerate(chunks):
        stream_text = "\n\n".join(c["text"] for c in chunk)
        block_ids = [c["block_id"] for c in chunk]
        prompt = LAYER_A_PROMPT.format(count=len(questions), questions=q_text,
                                       block_ids=json.dumps(block_ids), stream=stream_text)
        rows = extract_json_array(await call_model(prompt))
        stats["passes"] += 1
        kept: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            stats["raw_matches"] += 1
            fid = row.get("fs_id")
            if fid not in q_ids:
                stats["rejected_bad_id"] += 1
                continue
            sb = row.get("source_block_id")
            if not isinstance(sb, int) or sb not in block_text:
                stats["rejected_block"] += 1
                continue
            quote = row.get("digest_quote") or ""
            # 块级归属校验（v3 收紧）：引文必须出自该块自己的蒸馏文本
            if not isinstance(quote, str) or len(quote) < 12 or quote not in block_text[sb]:
                stats["rejected_quote"] += 1
                continue
            refs = row.get("refs") if isinstance(row.get("refs"), list) else []
            refs = [r for r in refs if isinstance(r, dict)
                    and isinstance(r.get("chapter"), int) and 1 <= r["chapter"] <= max_chapter
                    and r.get("role") in ("setup", "resolution", "context")]
            row["refs"] = refs
            rng = next((c["chapter_range"] for c in chunk if c["block_id"] == sb), (sb, sb))
            row["source_chapter_range"] = list(rng)
            kept.append(row)
        # 每段候选上限（防模型一次回几百条全进 Layer B）+ 去重
        rank = {"high": 0, "medium": 1, "low": 2}
        kept.sort(key=lambda r: rank.get(r.get("confidence"), 3))
        dedup = 0
        for row in kept[:MAX_CANDIDATES_PER_PASS]:
            key = (row["fs_id"], row["source_block_id"])
            if key in {(c["fs_id"], c["source_block_id"]) for c in candidates}:
                dedup += 1
                continue
            candidates.append(row)
        stats["dedup"] = stats.get("dedup", 0) + dedup
        if len(kept) > MAX_CANDIDATES_PER_PASS:
            stats["capped"] = stats.get("capped", 0) + len(kept) - MAX_CANDIDATES_PER_PASS
        log(f"  Layer A {ci + 1}/{len(chunks)} 段：候选 {min(len(kept), MAX_CANDIDATES_PER_PASS)}（原始 {len(rows)}，"
            f"拒：bad_id {stats['rejected_bad_id']} / quote {stats['rejected_quote']} / block {stats['rejected_block']}；"
            f"去重 {dedup}，截断 {max(0, len(kept) - MAX_CANDIDATES_PER_PASS)}）")
    return candidates, stats


# ---------------------------------------------------------------- Stage 3：抽样


def stratified_sample(items: list, sample_n: int, seed: int) -> list[str]:
    """last_seen 分层（<300 / 300-700 / 700-900 / 900+）最大余数法抽样。"""
    import random
    layers: dict[str, list] = {"a": [], "b": [], "c": [], "d": []}
    for it in items:
        ch = int(it.last_seen_chapter or 0)
        layers["a" if ch < 300 else "b" if ch < 700 else "c" if ch < 900 else "d"].append(it.id)
    total = len(items)
    weights = {k: len(v) / total for k, v in layers.items()}
    quotas = {k: int(weights[k] * sample_n) for k in layers}
    remainder = sample_n - sum(quotas.values())
    by_frac = sorted(layers, key=lambda k: weights[k] * sample_n - quotas[k], reverse=True)
    for k in by_frac[:remainder]:
        quotas[k] += 1
    rng = random.Random(seed)
    picked: list[str] = []
    for k, ids in layers.items():
        rng.shuffle(ids)
        picked.extend(ids[:quotas[k]])
    log(f"  抽样 {len(picked)} 条（层配额 {quotas}）")
    return picked


# ---------------------------------------------------------------- Stage 4：Layer B 验证

#: 每个蒸馏流分段的候选上限（防模型一次回几百条全进 Layer B）
MAX_CANDIDATES_PER_PASS = 40
#: 指针窗半径（原文章符）——重定位命中时围绕短语截取
POINTER_BEFORE = 1500
POINTER_AFTER = 2500

SALVAGE_PROMPT = """你是伏笔回收的复核员。账本中下面这条伏笔处于"待确认"（此前复检判证据不足）。
现有一条打捞候选主张：**该伏笔的问题已被一段原文回收**。请对照给出的原文判断这个主张是否成立。

判定基准（必须遵守）：
1. 主张成立 = 该段原文**正面回答**了 question，且满足 answer_criteria 预登记的关闭条件。
   回答必须是解释/揭示/兑现悬念本身——"又提了一次""继续推进"不算。
2. **复述已知信息不算回答**：description、"登记时已知"内容、anchor_quote 里已有的信息，
   原文只是重复它们 → 无信息增量，判 QUESTIONED(off_topic)。必须给出**超出登记时已知**的
   新解释、新事实或新结果。
3. **排他性问题不因确认其一而关闭**：question 问"是谁/哪些/是否只有 X"时，原文确认 X 是
   主使/来源之一**不等于**排除了其他可能——除非原文明确排除（"不是…而是…""没有其他"），
   否则判 QUESTIONED(no_evidence)。
4. **时间与事件必须匹配**：引文所述事件必须就是 question 指向的那次事件/那个时段。
   用"第一次发作"解释"治疗后的再次依赖"、把 A 事件的解释挪给 B 事件，判
   QUESTIONED(off_topic)。同章后段的新答案可以成立，但事件必须对得上。
5. 只依据给出的原文；不足以判断时判 QUESTIONED(no_evidence)，不要用常理推测。
6. 必须做**反证检索**：在原文里找"该问题并未被回答"的迹象。`counter_search.performed` 标 true，
   `checked_evidence_ids` 必须是 JSON 数组；`counter_quotes` 没有就写 []；
   找到"问题并未被回答"的直接原句就写进 counter_quotes 并判 QUESTIONED(contradicted)。
7. 判 VERIFIED 时 `supporting_quote` 必须从原文**逐字照抄**不少于 6 个字，
   `evidence_id` 原样回填该段证据的值；找不到支持性原句 → 只能 QUESTIONED(no_evidence)。
8. `branch_hits` 必须按"分支预登记"逐个标注：{{"branch_id": "c1", "evidence_id": "..."}}；
   预登记 closing 为 AND 时全部 branches 都要有命中记录，OR 时至少覆盖一个 closable 分支。

判定枚举（只能用这两个词之一）：
- `VERIFIED`：主张成立（reason_code: supported）
- `QUESTIONED`：不成立或无法验证（reason_code 三选一：no_evidence / contradicted / off_topic）

该伏笔：
{item_text}

分支预登记（branch_id 由这里来，branch_hits 必须引用它们）：
{prereg}

以下 {segment_count} 段原文各自有 evidence_id（第 1 组是候选来源块原文，其余为 resolution
指针章的窗口）。supporting_quote / counter_quotes 必须逐字取自其中一段，evidence_id 原样回填：

{segments}

输出：纯 JSON 对象：
{{
  "candidate_id": "{candidate_id}",
  "verdict": "VERIFIED",
  "reason_code": "supported",
  "answer": "该问题被如何回答的一句话结论",
  "supporting_quote": "原文逐字引文（判 QUESTIONED 时可留空）",
  "evidence_id": "引文所在段的 evidence_id",
  "counter_search": {{"performed": true, "checked_evidence_ids": ["..."], "counter_quotes": []}},
  "branch_hits": [{{"branch_id": "c1", "evidence_id": "..."}}]
}}
"""


def build_evidence(candidate: dict, blocks_dir: Path) -> list[dict]:
    """证据段列表：来源块原文（按章拆段）+ resolution 指针窗。返回 [{chapter, text}]。

    指针重定位（预检 P1-4）：蒸馏句是 LLM 转述，回原文常非逐字——先用 digest_quote
    找重定位锚点，命中则围绕短语截窗；未命中退全章（单章 ≈3000 字，整章兜底不贵）。
    """
    segs: list[dict] = []
    seen: set[int] = set()
    start, end = candidate.get("source_chapter_range") or (candidate["source_block_id"],) * 2
    for ch in range(int(start), int(end) + 1):
        p = blocks_dir / f"{ch:04d}.txt"
        if p.exists():
            text = p.read_text(encoding="utf-8")
            segs.append({"chapter": ch, "text": text})
            seen.add(ch)
    relocate_phrase = (candidate.get("digest_quote") or "").strip()[:24]
    for ref in candidate.get("refs") or []:
        if ref.get("role") != "resolution":
            continue
        p_ch = int(ref["chapter"])
        if p_ch in seen:
            continue
        base = blocks_dir / f"{p_ch:04d}.txt"
        if not base.exists():
            continue
        text = base.read_text(encoding="utf-8")
        phrase = (ref.get("phrase") or "").strip() or relocate_phrase
        pos = text.find(phrase) if phrase else -1
        if pos >= 0:
            lo, hi = max(0, pos - POINTER_BEFORE), min(len(text), pos + POINTER_AFTER)
            seg_text = ("…" if lo > 0 else "") + text[lo:hi] + ("…" if hi < len(text) else "")
        else:
            seg_text = text  # 全章兜底
        segs.append({"chapter": p_ch, "text": seg_text})
        seen.add(p_ch)
    return segs


def _locate_in_segments(segments: list[dict]):
    """parse_recheck 的 locate 回调：单段内计次（1 次=命中，多次=ambiguous，0 次=未找到）。"""
    by_id = {s["evidence_id"]: s["text"] for s in segments}

    def locate(quote: str, evidence_id: str):
        text = by_id.get(evidence_id, "")
        n = text.count(quote) if (text and quote) else 0
        if n == 1:
            start = text.find(quote)
            return SimpleNamespace(found=True, ambiguous=False, start=start,
                                   end=start + len(quote))
        if n > 1:
            return SimpleNamespace(found=True, ambiguous=True, start=None, end=None)
        return SimpleNamespace(found=False, ambiguous=False, start=None, end=None)

    return locate


async def layer_b_verify(call_model, candidates: list[dict], prereg: dict,
                         items_by_id: dict, blocks_dir: Path, *, concurrency: int) -> tuple[list[dict], dict]:
    """Layer B。返回 (results, stats)。results 元素含 verdict/record/format_reject 标记。"""
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []
    stats = {"calls": 0, "verified": 0, "questioned": 0, "format_rejects": 0, "semantic_rejects": 0}

    async def one(cand: dict) -> None:
        async with sem:
            fid = cand["fs_id"]
            item = items_by_id[fid]
            reg = prereg.get(fid) or {"branches": [], "closing": {"type": "UNCERTAIN"}}
            segments = build_evidence(cand, blocks_dir)
            if not segments:
                results.append({"fs_id": fid, "outcome": "format_reject", "reason": "no_evidence_text",
                                "source_block_id": cand["source_block_id"]})
                stats["format_rejects"] += 1
                return
            for s in segments:
                s["evidence_id"] = _evidence_id(s["chapter"], s["text"])
            seg_text = "\n\n".join(
                f"--- evidence_id={s['evidence_id']}（第{s['chapter']}章）---\n{s['text']}"
                for s in segments)
            item_text = (f"fs_id={fid}\nquestion：{item.question}\n"
                         f"answer_criteria：{item.answer_criteria}\n"
                         f"description：{item.description}\n首次出现：第{item.first_seen_chapter}章")
            prereg_text = json.dumps(reg, ensure_ascii=False, indent=1)
            prompt = SALVAGE_PROMPT.format(item_text=item_text, prereg=prereg_text,
                                           segment_count=len(segments), segments=seg_text,
                                           candidate_id=fid)
            allowed_ids = {s["evidence_id"] for s in segments}
            locate = _locate_in_segments(segments)
            try:
                raw = await call_model(prompt)
            except Exception as e:
                results.append({"fs_id": fid, "outcome": "call_failed", "reason": str(e)[:200],
                                "source_block_id": cand["source_block_id"]})
                return
            stats["calls"] += 1
            rows = raw if isinstance(raw, list) else extract_json_object(raw)
            payload = None
            if isinstance(rows, list):
                payload = next((r for r in rows if isinstance(r, dict) and r.get("candidate_id") == fid), None)
            elif isinstance(rows, dict):
                payload = rows
            if not isinstance(payload, dict):
                results.append({"fs_id": fid, "outcome": "format_reject", "reason": "unparseable_response",
                                "source_block_id": cand["source_block_id"]})
                stats["format_rejects"] += 1
                return
            record = _parse_audit_payload(payload, candidate_id=fid,
                                          allowed_evidence_ids=allowed_ids, locate=locate)
            seg_ch = {s["evidence_id"]: s["chapter"] for s in segments}
            if record.verdict == "VERIFIED":
                hits = payload.get("branch_hits") if isinstance(payload.get("branch_hits"), list) else []
                if not hits:
                    # B7：VERIFIED 无 branch_hits = schema 违规 → format_reject
                    results.append({"fs_id": fid, "outcome": "format_reject",
                                    "reason": "branch_hits_missing", "record": record,
                                    "source_block_id": cand["source_block_id"]})
                    stats["format_rejects"] += 1
                    return
                sat = closing_satisfied(reg.get("closing") or {}, hits, reg)
                if sat is not True:
                    # 引文真但关闭条件未满足/UNCERTAIN → 不落 resolved，转人工（C4/前沿 P1-3）
                    results.append({"fs_id": fid, "outcome": "closing_unmet",
                                    "record": record, "branch_hits": hits,
                                    "closing": reg.get("closing"),
                                    "source_block_id": cand["source_block_id"]})
                    return
                # resolved_chapter 取引文所在段的章号（预检 P1-3：不是恒取首段）
                chapter = seg_ch.get(record.evidence_id, segments[0]["chapter"])
                results.append({"fs_id": fid, "outcome": "verified", "record": record,
                                "branch_hits": hits, "chapter": chapter,
                                "source_block_id": cand["source_block_id"]})
                stats["verified"] += 1
            else:
                # 反证必须逐条定位（contradicted 才成立）；未定位则降级为 no_evidence。
                # _parse_audit_payload 收到任意非空 counter_quotes 就返回 contradicted，
                # 不调用定位器——这里补上，并保存反证引文与位置供人工复核。
                reason_final = record.reason
                counter_spans: list[dict] = []
                if record.reason == "contradicted":
                    quotes = [q.strip() for q in
                              ((payload.get("counter_search") or {}).get("counter_quotes") or [])
                              if isinstance(q, str) and q.strip()]
                    all_located = bool(quotes)
                    for q in quotes:
                        hit = locate(q, record.evidence_id or next(iter(allowed_ids)))
                        ok = bool(hit.found and not hit.ambiguous)
                        counter_spans.append({"quote": q, "located": ok,
                                              "span": [hit.start, hit.end] if ok else None,
                                              "evidence_id": record.evidence_id})
                        all_located = all_located and ok
                    if not all_located:
                        reason_final = "no_evidence"
                        stats["contradiction_unlocated"] = stats.get("contradiction_unlocated", 0) + 1
                    else:
                        stats["contradicted"] = stats.get("contradicted", 0) + 1
                results.append({"fs_id": fid, "outcome": "questioned", "record": record,
                                "reason_final": reason_final, "counter_quotes": counter_spans,
                                "source_block_id": cand["source_block_id"]})
                stats["questioned"] += 1
                stats["semantic_rejects"] += 1

    await asyncio.gather(*(one(c) for c in candidates))
    return results, stats


def extract_json_object(text: str) -> dict | None:
    text = text.strip()
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


# ---------------------------------------------------------------- 落账（影子）


def apply_to_shadow(shadow: ForeshadowLedger, results: list[dict], items_by_id: dict,
                    da_ids: set[str] | None = None) -> dict:
    """显式落账适配器（audit → ledger）。返回动作计数（按唯一 fs_id 归并）。"""
    da_ids = da_ids or set()
    applied = closing_unmet = questioned = skipped = da_protected = duplicate_verified = 0
    applied_ids: set[str] = set()
    by_id = {i.id: i for i in shadow.items}
    for r in results:
        fid = r["fs_id"]
        item = by_id.get(fid)
        if item is None:
            skipped += 1
            continue
        outcome = r["outcome"]
        if outcome == "verified":
            if item.status != "active" or item.resolution_quality or fid in applied_ids:
                # 同一伏笔多候选：只认第一条通过的，其余计重复（不重复累加打捞数）
                if fid in applied_ids:
                    duplicate_verified += 1
                else:
                    skipped += 1
                continue
            if fid in da_ids:
                # DA 防线（C6）：DIFFERENT_ANSWER 条目只记不改，翻案必须显式人工
                _note(item, "打捞：候选成立，但该条此前判 DIFFERENT_ANSWER，按防线只记不改，待人工仲裁")
                da_protected += 1
                continue
            record = r["record"]
            item.status = "resolved"
            item.resolution = record.answer or item.resolution
            item.resolved_chapter = int(r.get("chapter") or item.resolved_chapter or 0)
            item.source_type = "auto_recheck"
            item.resolution_quality = "verified"
            item.needs_review = False
            _note(item, "打捞：蒸馏层候选经固定窗逐字引文复核成立")
            applied_ids.add(fid)
            applied += 1
        elif outcome == "closing_unmet":
            _note(item, f"打捞：引文成立但关闭条件未满足（hits={json.dumps(r.get('branch_hits'), ensure_ascii=False)[:120]}），转人工")
            closing_unmet += 1
        elif outcome == "questioned":
            # reason_final 已把未定位反证降级为 no_evidence
            reason = r.get("reason_final") or r["record"].reason
            tag = "明确矛盾（反证引文已定位）" if reason == "contradicted" else reason
            _note(item, f"打捞：候选未获原文支持（{tag}）")
            questioned += 1
        # call_failed / format_reject：不动账本
    return {"applied": applied, "closing_unmet": closing_unmet, "da_protected": da_protected,
            "questioned": questioned, "skipped": skipped,
            "duplicate_verified": duplicate_verified}


def _note(item, text: str) -> None:
    """notes 去重追加（C7：重跑双写防护）。"""
    notes = getattr(item, "notes", None)
    if not isinstance(notes, list):
        notes = []
        item.notes = notes
    if text not in notes:
        notes.append(text)


# ---------------------------------------------------------------- 主流程


async def main() -> int:
    ap = argparse.ArgumentParser(description="伏笔打捞两段式管线（影子账本）")
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--output-dir", required=True, help="块分析结果所在目录（蒸馏层来源）")
    ap.add_argument("--blocks", required=True)
    ap.add_argument("--mode", choices=["pilot", "full"], default="pilot")
    ap.add_argument("--sample", type=int, default=38)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--base-url", default="")
    ap.add_argument("--recheck-model", default="")
    ap.add_argument("--reasoning-effort", default="",
                    help="默认取 config 的 recheck_reasoning_effort")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-layer-b-calls", type=int, default=300,
                    help="Layer B 调用硬顶（full 模式防候选爆炸白花钱）")
    ap.add_argument("--retry-failed", default="",
                    help="复用某次明细的预登记/候选清单，只重跑其中 call_failed 的候选")
    args = ap.parse_args()
    # 切正账是人工流程（§2.4：diff 报告 → 人工确认 → 备份 → 原子替换），不提供命令行直写

    cfg = ConfigManager(ROOT / "config.json").load()
    ledger_path = Path(args.ledger)
    blocks_dir = Path(args.blocks)
    if not blocks_dir.is_absolute():
        blocks_dir = ROOT / blocks_dir
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir

    ledger = ForeshadowLedger.load(ledger_path)
    items_by_id = {i.id: i for i in ledger.items}
    # 资格集（C5）：active ∧ needs_review，三处口径唯一
    eligible = [i for i in ledger.items
                if i.status == "active" and i.needs_review and (i.question or "").strip()]
    if len(eligible) != 503 and (ledger_path.parent / "recheck_verdicts.json").exists():
        log(f"⚠️ 资格集 {len(eligible)} 条 ≠ 预期 503（可能与预期账本不同或口径漂移），继续但请留意分母")
    log(f"账本 {len(ledger.items)} 条；资格集 {len(eligible)} 条（active ∧ needs_review）")
    if not eligible:
        log("资格集为空，结束")
        return 0

    # DA 防线（C6）：从历史判定明细提取 DIFFERENT_ANSWER 集合
    da_ids: set[str] = set()
    verdicts_path = ledger_path.parent / "recheck_verdicts.json"
    if verdicts_path.exists():
        try:
            vd = json.loads(verdicts_path.read_text(encoding="utf-8"))
            rows = vd if isinstance(vd, list) else (vd.get("verdicts") or [])
            da_ids = {v["candidate_id"] for v in rows
                      if isinstance(v, dict) and v.get("verdict") == "DIFFERENT_ANSWER"}
            log(f"DA 防线：{len(da_ids)} 条 DIFFERENT_ANSWER 只记不改")
        except Exception as e:
            log(f"⚠️ 判定明细读取失败，DA 防线不可用：{e}")

    call_model = make_call_model(cfg, args)

    if args.retry_failed:
        # 定向重试：复用冻结的预登记与候选清单，只重跑上次 call_failed 的候选
        prev = json.loads(Path(args.retry_failed).read_text(encoding="utf-8"))
        prereg = prev.get("prereg") or {}
        candidates = prev.get("candidates") or []
        failed_keys = {(r["fs_id"], r.get("source_block_id"))
                       for r in prev.get("results") or [] if r.get("outcome") == "call_failed"}
        # 旧明细的失败记录可能没有 source_block_id —— 按 fs_id 兜底重试该伏笔的全部候选
        failed_fs = {r["fs_id"] for r in prev.get("results") or []
                     if r.get("outcome") == "call_failed"}
        scope = [c for c in candidates
                 if (c["fs_id"], c["source_block_id"]) in failed_keys or c["fs_id"] in failed_fs]
        uncertain = [k for k, v in prereg.items() if (v.get("closing") or {}).get("type") == "UNCERTAIN"]
        a_stats = {"reused_from": args.retry_failed, "reused_candidates": len(candidates)}
        log(f"== 定向重试：{len(scope)} 条 call_failed 候选（复用预登记 {len(prereg)} 条）==")
        if not scope:
            log("无可重试候选，结束")
            return 0
    else:
        # Stage 1：criteria 预登记
        t0 = time.time()
        log("== Stage 1：criteria 预登记 ==")
        questions = load_questions(eligible, skip_ids=set())
        prereg = await preregister_criteria(call_model, questions)
        uncertain = [k for k, v in prereg.items() if (v.get("closing") or {}).get("type") == "UNCERTAIN"]
        log(f"预登记完成（{time.time() - t0:.0f}s）：UNCERTAIN 转人工 {len(uncertain)} 条"
            f"（{len(uncertain) / max(len(prereg), 1):.0%}）")

        # Stage 2：Layer A 匹配（全部资格条目；UNCERTAIN 不参与 Layer B）
        t0 = time.time()
        log("== Stage 2：Layer A 蒸馏层匹配 ==")
        stream = load_distilled_stream(output_dir)
        max_chapter = max((int(p.stem.split("_")[1]) for p in output_dir.glob("chapter_*_result.json")),
                          default=1046)
        log(f"蒸馏块 {len(stream)} 个，匹配面 {sum(len(c['text']) for c in stream):,} 字")
        a_questions = [q for q in questions if (prereg.get(q["fs_id"]) or {}).get("closing", {}).get("type") != "UNCERTAIN"]
        candidates, a_stats = await layer_a_match(call_model, a_questions, stream, max_chapter=max_chapter)
        log(f"Layer A 完成（{time.time() - t0:.0f}s）：候选 {len(candidates)}")

        # Stage 3：范围选择（保留每条伏笔的**全部**候选，不按 fs_id 收敛——同一伏笔可多条证据）
        if args.mode == "pilot":
            picked_set = set(stratified_sample(eligible, args.sample, args.seed))
            scope = [c for c in candidates if c["fs_id"] in picked_set]
            smoke_ids = {"fs_001", "fs_002"} - picked_set
            smoke = [c for c in candidates if c["fs_id"] in smoke_ids]
            scope.extend(smoke)
            log(f"pilot 抽样 {len(picked_set)} 条：{len(scope) - len(smoke)} 条候选记录（"
                f"{len({c['fs_id'] for c in scope if c['fs_id'] in picked_set})} 条唯一伏笔）；冒烟 {len(smoke)} 条")
        else:
            scope = candidates[:args.max_layer_b_calls]
            if len(candidates) > len(scope):
                log(f"⚠️ 候选 {len(candidates)} 超过硬顶 {args.max_layer_b_calls}，截断（优先级=Layer A 顺序）")
    log(f"== Stage 3：Layer B 验证 {len(scope)} 条候选记录"
        f"（{len({c['fs_id'] for c in scope})} 条唯一伏笔）==")
    t0 = time.time()
    results, b_stats = await layer_b_verify(call_model, scope, prereg, items_by_id, blocks_dir,
                                            concurrency=args.concurrency)
    log(f"Layer B 完成（{time.time() - t0:.0f}s）：{json.dumps(b_stats, ensure_ascii=False)}")

    # Stage 4：落账（影子）+ 明细 + 报告
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shadow = ForeshadowLedger.load(ledger_path)  # 独立实例 = 影子
    actions = apply_to_shadow(shadow, results, items_by_id, da_ids=da_ids)
    shadow_path = ledger_path.parent / f"salvage_ledger_{ts}.json"
    shadow.save(shadow_path)

    # 三类计数分开报告（口径纪律：候选记录 ≠ 唯一伏笔 ≠ 可落账）
    cand_records = len(candidates)
    cand_unique = len({c["fs_id"] for c in candidates})
    verified_unique = len({r["fs_id"] for r in results if r["outcome"] == "verified"})
    summary = {
        "candidate_records": cand_records,
        "candidate_unique_fs": cand_unique,
        "candidate_unique_ratio": round(cand_unique / max(len(eligible), 1), 4),
        "verified_unique_fs": verified_unique,
        "applied_unique_fs": actions["applied"],
        "verified_unique_ratio": round(verified_unique / max(len(eligible), 1), 4),
    }
    detail = {
        "pipeline": "salvage", "run_id": ts, "mode": args.mode,
        "eligible_total": len(eligible), "uncertain_to_human": len(uncertain),
        "candidates_total": cand_records, "summary": summary,
        "layer_a_stats": a_stats,
        "layer_b_stats": b_stats, "actions": actions,
        "candidates": candidates,
        "results": [{k: v for k, v in r.items() if k != "record"} |
                    {"reason": r["record"].reason, "answer": r["record"].answer,
                     "quote": r["record"].quote}
                    for r in results if r.get("record")] +
                   [r for r in results if not r.get("record")],
        "prereg": prereg,
    }
    detail_path = ledger_path.parent / f"salvage_{ts}.json"
    detail_path.write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")

    from backend.utils.foreshadow_ledger import ForeshadowLedger as FL
    bars = FL.load(shadow_path).lifecycle_bars()
    # 七栏守恒断言（C7）：加总必须等于条目数，否则账本损坏立即失败
    assert sum(bars.values()) == len(shadow.items), f"七栏守恒失败：{bars} vs {len(shadow.items)}"
    log(f"动作：{json.dumps(actions, ensure_ascii=False)}")
    log(f"口径：候选记录 {cand_records} 条 / 唯一伏笔 {cand_unique} 条（{summary['candidate_unique_ratio']:.1%}）"
        f"；唯一通过 {verified_unique} 条（{summary['verified_unique_ratio']:.1%}）；实际落账 {actions['applied']} 条")
    log(f"影子账本七栏：{json.dumps(bars, ensure_ascii=False)}（守恒 ✓）")
    log(f"明细：{detail_path}")
    log(f"影子账本：{shadow_path}（生产账本未动；切正账见 §2.4 流程）")
    if b_stats["calls"] > 0 and b_stats["verified"] + b_stats["questioned"] == 0:
        log("❌ 有调用但零判定——检查通道与 schema")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
