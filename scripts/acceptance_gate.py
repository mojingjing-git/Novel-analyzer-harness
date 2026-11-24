#!/usr/bin/env python
"""打捞结果验收：共享关闭条件、逐分支取证、答案忠实性及版本化评测。

--prepare-only 配合 --blocks 将旧考卷冻结到独立文件，不调用模型。
--exam 接受 run_salvage 自动导出的 acceptance_input 文件。
评测标签必须绑定 sample_id；旧标签仅作为待复核备注，不用于准确性指标。
核心门禁与提示词已提取至生产与评测共用模块 `backend.common.c_claim_audit_gate`。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = "localhost,127.0.0.1"

from backend.common.c_claim_audit_gate import (  # noqa: E402,F401
    ANSWER_COVERAGE_REJECT,
    BINDING_FIELDS,
    CLAIM_PROMPT,
    CLAIM_V2,
    CLAIM_VERSION,
    EXCLUSION_MARKERS,
    MIN_QUOTE_CHARS,
    NLI_PROMPT,
    _gate_decision,
    base_sample_fingerprint,
    build_nli_prompt,
    compute_gate_code_snapshot,
    criteria_logic,
    evaluate_metrics,
    evidence_fingerprint,
    extract_json_object,
    gate,
    label_status,
    mechanical_increment_check,
    ngram_dice,
    ngrams,
    norm,
    sample_fingerprint,
)
from backend.config.paths import runtime_path  # noqa: E402
from backend.config.settings import ConfigManager  # noqa: E402
from backend.core.llm_client import LLMClient  # noqa: E402


def freeze_item(item: dict, blocks: Path | None = None) -> dict:
    """冻结具体题目/证据/答案。已有标签不会被自动认证。"""
    out = dict(item)
    if blocks is not None:
        chapter = item.get("chapter")
        if type(chapter) is not int or chapter <= 0:
            out["context_error"] = "invalid_block_ordinal"
        else:
            path = blocks / f"{chapter:04d}.txt"
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                out["context_error"] = "block_unreadable"
            else:
                quote = item.get("quote") or ""
                if not quote or text.count(quote) != 1:
                    out["context_error"] = "quote_not_unique_in_block"
                elif len(text) > 24000:
                    out["context_error"] = "context_budget_exceeded"
                else:
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    out["evidence_segments"] = [
                        {
                            "evidence_id": f"block_{chapter}_{digest[:12]}",
                            "chapter": chapter,
                            "text": text,
                            "source_path": str(path.resolve()),
                            "content_hash": digest,
                        }
                    ]
                    out.pop("context_error", None)
    out["sample_id"] = sample_fingerprint(out)
    return out


def render_report(results: list[dict], metrics: dict) -> str:
    lines = [
        "# 自动验收器评测",
        "",
        f"样本 {metrics['total']} 条；标签与当前证据绑定 {metrics['labelled']} 条。",
        "标签绑定仅保证版本一致，标签是否正确仍需独立复核。无对应标签时指标为 N/A。",
        "",
        "```json",
        json.dumps(metrics, ensure_ascii=False, indent=2),
        "```",
        "",
        "部分回答/答案扩写为模型的分项判断，不代表人工确认或整条已解决。",
        "",
        "| 条目 | 标签状态 | 判定 | 分项结果 | 原因 |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        reason = ";".join(r.get("reasons", [])).replace("|", r"\|").replace("\n", " ")
        lines.append(
            f"| {r['fs_id']} | {r.get('label_status')} | {r['decision']} | "
            f"{r.get('assessment_status', 'unconfirmed')} | {reason} |"
        )
    return "\n".join(lines)


async def run_evaluate(args) -> int:
    exam_doc = json.loads(Path(args.exam).read_text(encoding="utf-8"))
    version = getattr(args, "claim_contract", None) or (
        CLAIM_VERSION if getattr(args, "claim_audit", False) else None
    )
    exam = [
        freeze_item(
            dict(it, evidence_contract_version=version) if version else it,
            Path(args.blocks) if args.blocks else None,
        )
        for it in exam_doc["items"]
    ]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prepared = out_dir / f"acceptance_prepared_{args.stamp}.json"
    prepared.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source_exam": str(Path(args.exam).resolve()),
                "note": "复核后将label_sample_id设为sample_id；不得自动认证历史标签",
                "items": exam,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if args.prepare_only:
        print(f"冻结考卷：{prepared}（未调用模型）")
        return 0
    cfg = ConfigManager(runtime_path("config.json")).load()
    api = cfg.api
    from dataclasses import replace as dc_replace

    api = dc_replace(
        api,
        model=cfg.analysis.recheck_model or api.model,
        base_url=cfg.analysis.recheck_base_url or api.base_url,
        api_key=cfg.analysis.recheck_api_key or api.api_key,
        reasoning_effort=cfg.analysis.recheck_reasoning_effort or api.reasoning_effort,
    )
    if args.model:
        api = dc_replace(api, model=args.model)
    if args.base_url:
        api = dc_replace(api, base_url=args.base_url)
    log = print
    log(f"NLI 通道：{api.model} @ {api.base_url}")
    llm = LLMClient(api)
    max_tokens = min(int(cfg.analysis.recheck_max_tokens), 4096)

    async def call_model(prompt: str) -> str:
        last: Exception | None = None
        for attempt in range(3):
            try:
                res = await llm.chat_auto(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                )
                if not getattr(res, "success", True):
                    raise RuntimeError(str(getattr(res, "error", "") or "未知错误"))
                return getattr(res, "content", "") or ""
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < 2:
                    await asyncio.sleep(2**attempt * 2)
        raise last  # type: ignore[misc]

    sem = asyncio.Semaphore(args.concurrency)

    async def one(item: dict) -> dict:
        async with sem:
            identity = {
                "fs_id": item["fs_id"],
                "sample_id": item["sample_id"],
                "label": item.get("label"),
                "label_status": label_status(item),
            }
            if item.get("context_error") or not item.get("evidence_segments"):
                return {
                    **identity,
                    "decision": "存疑",
                    "reasons": ["缺少可核验上下文；请先提供--blocks或原始证据段"],
                }
            try:
                raw = await call_model(build_nli_prompt(item))
                nli = extract_json_object(raw)
            except Exception as e:  # noqa: BLE001
                return {**identity, **gate(item, None, str(e)[:120])}
            nli_verdict = (nli or {}).get("verdict") if isinstance(nli, dict) else None
            return {
                **identity,
                "nli": nli,
                "nli_verdict": nli_verdict,
                "nli_reason": (nli or {}).get("reason", ""),
                **gate(item, nli),
            }

    results = list(await asyncio.gather(*(one(it) for it in exam)))

    metrics = evaluate_metrics(results)
    report = out_dir / f"acceptance_gate_eval_{args.stamp}.md"
    report.write_text(render_report(results, metrics), encoding="utf-8")
    detail = out_dir / f"acceptance_gate_eval_{args.stamp}.json"
    detail.write_text(
        json.dumps({"schema_version": 2, "results": results, "metrics": metrics}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"判定分布：{metrics['decisions']}；已绑定标签 {metrics['labelled']}/{metrics['total']}")
    log(f"报告：{report}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="打捞结果自动验收器 v1")
    ap.add_argument("--exam", required=True, help="考卷 json（评测模式）")
    ap.add_argument("--output-dir", default="runtime/workspace/《道诡异仙》/output")
    ap.add_argument("--blocks", help="原文块目录；旧考卷chapter必须对应文件序号")
    ap.add_argument("--prepare-only", action="store_true", help="仅冻结输入与证据，不调用模型")
    ap.add_argument("--claim-audit", action="store_true", help="试验性逐断言证据契约；改变输入指纹，需重新复核标签")
    ap.add_argument("--claim-contract", choices=[CLAIM_VERSION, CLAIM_V2], help="显式选择试验协议版本；优先于--claim-audit")
    ap.add_argument("--model", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--stamp", default="")
    args = ap.parse_args()
    if args.concurrency < 1:
        ap.error("concurrency必须大于0")
    args.stamp = args.stamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output_dir and not Path(args.output_dir).is_absolute():
        args.output_dir = str(ROOT / args.output_dir)
    return asyncio.run(run_evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
