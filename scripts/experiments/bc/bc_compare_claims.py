#!/usr/bin/env python
"""Bounded paired probe for frozen current/claims_v1 exams; never updates a ledger."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import logging
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from backend.common.claim_evidence import VERSION
from backend.config.settings import ConfigManager
from backend.core.llm_client import LLMClient
from backend.core.llm_failure_logger import redact
from scripts.acceptance_gate import build_nli_prompt, evaluate_metrics, extract_json_object, gate, label_status, sample_fingerprint


def validate_pairs(items: list[dict], max_calls: int, candidate: str = VERSION) -> None:
    if candidate not in {"claims_v1", "claims_v2"}:
        raise ValueError("unknown_candidate_contract")
    if not items or len(items) > max_calls or max_calls <= 0:
        raise ValueError("logical_call_budget_exceeded_or_empty_exam")
    groups = {}
    for item in items:
        if item.get("sample_id") != sample_fingerprint(item):
            raise ValueError("exam_fingerprint_mismatch")
        if label_status(item) != "bound":
            raise ValueError("reference_label_not_bound_before_run")
        if item.get("arm") not in {"current", candidate}:
            raise ValueError("invalid_arm")
        version = item.get("evidence_contract_version")
        if (item["arm"] == candidate and version != candidate) or (item["arm"] == "current" and "evidence_contract_version" in item):
            raise ValueError("arm_contract_mismatch")
        pair = groups.setdefault(item["fs_id"], {})
        if item["arm"] in pair:
            raise ValueError("duplicate_arm")
        pair[item["arm"]] = item
    for pair in groups.values():
        if set(pair) != {"current", candidate}:
            raise ValueError("missing_paired_arm")
        skip = {"arm", "evidence_contract_version", "sample_id", "label_sample_id"}
        if {k: v for k, v in pair["current"].items() if k not in skip} != {k: v for k, v in pair[candidate].items() if k not in skip}:
            raise ValueError("paired_inputs_differ")


async def run(args) -> dict:
    exam_bytes = args.exam.read_bytes()
    exam = json.loads(exam_bytes)
    items = exam["items"]
    candidate = getattr(args, "candidate", VERSION)
    validate_pairs(items, args.max_calls, candidate)
    if args.output.exists():
        raise ValueError("output_directory_already_exists")
    # Never log full runtime configuration, endpoints, or credentials.
    logging.disable(logging.CRITICAL)
    config = ConfigManager(ROOT / "config.json").load()
    api = replace(config.api, model=config.analysis.recheck_model or config.api.model,
                  base_url=config.analysis.recheck_base_url or config.api.base_url,
                  api_key=config.analysis.recheck_api_key or config.api.api_key,
                  reasoning_effort=config.analysis.recheck_reasoning_effort or "low",
                  temperature=0, thinking_mode={}, streaming_enabled=False,
                  timeout=75, max_retries=0, temperature_max_retries=1, backoff_max_retries=0)
    prompts = [build_nli_prompt(item) for item in items]
    files = ["scripts/experiments/bc/bc_compare_claims.py", "scripts/acceptance_gate.py", "backend/common/claim_evidence.py",
             "backend/common/claim_evidence_v2.py", "backend/common/closure.py"]
    metadata = {"model": api.model, "reasoning_effort": api.reasoning_effort, "temperature": 0,
                "max_logical_calls": args.max_calls, "planned_calls": len(items), "max_output_tokens": args.max_tokens,
                "concurrency": args.concurrency, "source_exam_sha256": hashlib.sha256(exam_bytes).hexdigest(),
                "candidate_contract": candidate,
                "reference_source": exam.get("reference_source"),
                "limits": "constructed cross-work probe; model-assisted labels; not natural error rate or whole-book recall",
                "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in files},
                "inputs": [{"sample_id": it["sample_id"], "arm": it["arm"], "prompt_sha256": hashlib.sha256(p.encode()).hexdigest()}
                           for it, p in zip(items, prompts)]}
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "run.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "frozen-exam.json").write_bytes(exam_bytes)
    for relative in files:
        snapshot = args.output / "code" / relative
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        source = (ROOT / relative).read_bytes()
        if hashlib.sha256(source).hexdigest() != metadata["code_sha256"][relative]:
            raise ValueError("source_changed_while_freezing_run")
        snapshot.write_bytes(source)
    results = []
    client = LLMClient(api)
    semaphore = asyncio.Semaphore(args.concurrency)

    def save():
        data = {"run": metadata, "results": results, "metrics": {
            arm: evaluate_metrics([r for r in results if r["arm"] == arm]) for arm in ("current", candidate)}}
        temp = args.output / "results.tmp"
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(args.output / "results.json")
        return data

    async def one(index):
        item, prompt = items[index], prompts[index]
        async with semaphore:
            row = {"fs_id": item["fs_id"], "sample_id": item["sample_id"], "work_id": item["work_id"],
                   "arm": item["arm"], "label": item["label"], "label_status": label_status(item), "prompt_chars": len(prompt)}
            start = time.perf_counter()
            try:
                response = await asyncio.wait_for(client.chat_auto(
                    messages=[{"role": "user", "content": prompt}], max_tokens=args.max_tokens), timeout=110)
                row.update(success=response.success, tokens=response.tokens, attempts=response.attempts)
                if response.success:
                    nli = extract_json_object(response.content)
                    row.update(raw_response=response.content, nli=nli, **gate(item, nli))
                else:
                    row.update(error=redact(str(response.error))[:300], **gate(item, None, "model_call_failed"))
            except Exception as exc:
                row.update(success=False, error=redact(str(exc))[:300], **gate(item, None, "model_call_failed"))
            row["seconds"] = round(time.perf_counter() - start, 3)
            results.append(row)
            save()
            print(f"{len(results)}/{len(items)} {row['arm']} {row['fs_id']} {row['decision']} success={row['success']}", flush=True)
            return row

    try:
        # Detect credentials/transport failures before launching the remaining paired calls.
        first = await one(0)
        if first["success"]:
            await asyncio.gather(*(one(i) for i in range(1, len(items))))
        return save()
    finally:
        for transport in (client.client, client.anthropic):
            if transport is not None:
                await transport.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exam", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=16)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--candidate", choices=["claims_v1", "claims_v2"], default=VERSION)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 4 or not 256 <= args.max_tokens <= 4096:
        parser.error("invalid concurrency or output token budget")
    result = asyncio.run(run(args))
    return 0 if len(result["results"]) == result["run"]["planned_calls"] and all(r["success"] for r in result["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
