"""定向开发样本的配对试验。只读成品；结果另存，不更新账本。"""
import ast
import asyncio
from dataclasses import replace
import hashlib
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from backend.config.settings import ConfigManager
from backend.core.llm_client import LLMClient
from backend.core.llm_failure_logger import redact
from scripts import acceptance_gate as current

OUT = ROOT / "docs/research/bc-evaluation-2026-11-05"
RAW_ARCHIVE = ROOT / "workspace/bc-evaluation/_research_raw_archive/bc-evaluation-2026-11-05"


def old_functions():
    source = subprocess.check_output(["git", "show", "HEAD:scripts/acceptance_gate.py"], cwd=ROOT).decode("utf-8")
    functions = {"norm", "ngrams", "ngram_dice", "criteria_logic", "mechanical_increment_check",
                 "build_nli_prompt", "extract_json_object", "gate"}
    constants = {"ANSWER_COVERAGE_REJECT", "MIN_QUOTE_CHARS", "EXCLUSION_MARKERS", "NLI_PROMPT"}
    nodes = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets):
            nodes.append(node)
    ns = {"re": re, "json": json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "baseline_acceptance", "exec"), ns)
    return ns, hashlib.sha256(source.encode()).hexdigest()


async def main():
    logging.disable(logging.CRITICAL)
    cfg = ConfigManager(ROOT / "config.json").load()
    api = replace(cfg.api, model=cfg.analysis.recheck_model or cfg.api.model,
                  base_url=cfg.analysis.recheck_base_url or cfg.api.base_url,
                  api_key=cfg.analysis.recheck_api_key or cfg.api.api_key,
                  reasoning_effort=cfg.analysis.recheck_reasoning_effort or "low",
                  thinking_mode={}, temperature=0, streaming_enabled=False,
                  timeout=75, max_retries=0, temperature_max_retries=1, backoff_max_retries=0)
    client = LLMClient(api)
    baseline, source_hash = old_functions()
    study = json.loads((RAW_ARCHIVE / "pilot12.json").read_text(encoding="utf-8"))
    rows = study["items"]
    results = []
    semaphore = asyncio.Semaphore(2)
    header = {"model": api.model, "reasoning_effort": api.reasoning_effort, "temperature": 0,
              "reference_source": study["reference_source"], "design": study["design"],
              "baseline_source_hash": source_hash,
              "new_source_hash": hashlib.sha256((ROOT / "scripts/acceptance_gate.py").read_bytes()).hexdigest()}

    def save():
        (OUT / "paired_results.json").write_text(json.dumps({**header, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    async def one(item, arm):
        async with semaphore:
            build = baseline["build_nli_prompt"] if arm == "baseline" else current.build_nli_prompt
            decide = baseline["gate"] if arm == "baseline" else current.gate
            prompt = build(item)
            t0 = time.perf_counter()
            row = {"fs_id": item["fs_id"], "sample_id": item["sample_id"], "arm": arm,
                   "label": item["label"], "label_status": current.label_status(item),
                   "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(), "prompt_chars": len(prompt)}
            try:
                res = await asyncio.wait_for(client.chat_auto(messages=[{"role": "user", "content": prompt}], max_tokens=2048), timeout=110)
                row.update(success=res.success, tokens=res.tokens, attempts=res.attempts)
                if res.success:
                    parsed = current.extract_json_object(res.content)
                    row.update(nli=parsed, raw_response=res.content, **decide(item, parsed))
                else:
                    row.update(error=redact(str(res.error))[:300], **decide(item, None, "model_call_failed"))
            except Exception as exc:
                row.update(success=False, error=redact(str(exc))[:300], decision="存疑", reasons=["调用失败"])
            row["seconds"] = round(time.perf_counter()-t0, 3)
            results.append(row)
            save()
            print(f"{len(results)}/24 {arm} {row['fs_id']} {row['decision']} success={row['success']}", flush=True)
            return row

    first = await one(rows[0], "baseline")
    if not first["success"]:
        print("首个请求失败，停止试验，未继续发起其余请求。", flush=True)
        return
    work = [(it, arm) for index, it in enumerate(rows)
            for arm in (("baseline", "current") if index % 2 == 0 else ("current", "baseline"))
            if not (index == 0 and arm == "baseline")]
    await asyncio.gather(*(one(it, arm) for it, arm in work))
    for arm in ("baseline", "current"):
        rs = [x for x in results if x["arm"] == arm]
        metrics = current.evaluate_metrics(rs)
        print(arm, json.dumps(metrics, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
