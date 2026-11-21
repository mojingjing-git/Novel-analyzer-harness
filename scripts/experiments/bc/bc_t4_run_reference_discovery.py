"""参考发现执行器：独立会话 → 宿主组装标注 → 校验 → 落盘。

每次运行只做**一个**标注者（隔离会话），两份由调用方分别运行——
同一进程连跑两次会共享上下文，破坏隔离。

模型只给逐字引文；**跨度由宿主定位**（唯一命中才接受）。
标注的头部字段（作品、前缀、标注者、协议版本、看过范围、完成时间、冻结哈希）
全部由宿主组装，模型不参与——避免模型自称"看完"或自称"完成"。

用量记入归档；失败与重试同样计入。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.common.registration_reference_set import (  # noqa: E402
    ITEM_LABELS,
    PROTOCOL_VERSION,
    REVIEWER_TYPE_AI,
    validate_annotation,
)
from backend.common.registration_usage_archive import (  # noqa: E402
    STAGE_REFERENCE,
    UsageArchive,
)

ROOT = Path(__file__).resolve().parents[3]
PROP = ROOT / "docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal"
PLACEHOLDERS = (
    "work_id",
    "prefix_id",
    "visible_range_end",
    "unit_count",
    "source_units_with_stable_ids",
)


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def assemble(template: str, fields: dict) -> str:
    """逐个替换占位符；插入的原文不被二次模板展开。"""
    out = template
    for key in PLACEHOLDERS:
        out = out.replace("{" + key + "}", str(fields.get(key, "")))
    return out


def render_units(units: dict[int, str], checkpoint: int) -> str:
    parts = []
    for u in sorted(units):
        if u > int(checkpoint):
            break
        parts.append(f"[unit_id={u}]\n{units[u]}")
    return "\n".join(parts)


def locate_spans(units: dict[int, str], quote: str, checkpoint: int) -> list[dict]:
    """在可见单元里定位引文。**唯一命中**才接受。

    不指定单元时：在所有可见单元里找，唯一命中即可；
    多处命中 → 放弃该引文（交给人判断），不默取第一个。
    """
    q = (quote or "").strip()
    if not q:
        return []
    hits: list[tuple[int, int, int]] = []
    for u in sorted(units):
        if u > int(checkpoint):
            break
        text = units[u]
        start = 0
        while True:
            idx = text.find(q, start)
            if idx < 0:
                break
            hits.append((u, idx, idx + len(q)))
            start = idx + 1
    if len(hits) != 1:
        return []
    u, s, e = hits[0]
    return [{"unit_id": u, "start": s, "end": e}]


def parse_model_output(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        return {"parse_ok": False, "reason": "empty_response", "complete": None, "references": []}
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"parse_ok": False, "reason": f"invalid_json:{exc.msg}", "complete": None, "references": []}
    if not isinstance(obj, dict):
        return {"parse_ok": False, "reason": "not_an_object", "complete": None, "references": []}
    refs = obj.get("references")
    if not isinstance(refs, list):
        return {"parse_ok": False, "reason": "missing_references", "complete": obj.get("complete"), "references": []}
    return {"parse_ok": True, "complete": obj.get("complete"), "references": refs}


def assemble_annotation(
    *,
    work_id: str,
    prefix_id: str,
    annotator_id: str,
    checkpoint: int,
    units: dict[int, str],
    parsed: dict,
    freeze_sha256: str,
    completed_at: str,
) -> dict:
    """把模型输出组装成标注记录（头部由宿主写）。"""
    items = []
    for it in parsed.get("references") or []:
        if not isinstance(it, dict):
            continue
        quote = str(it.get("supporting_quote") or "").strip()
        alt = [str(a).strip() for a in (it.get("alternative_quotes") or []) if str(a).strip()]
        spans: list[dict] = []
        for q in [quote] + alt:
            spans.extend(locate_spans(units, q, checkpoint))
        # 去重（同一引文重复给出）
        uniq = []
        seen = set()
        for s in spans:
            key = (s["unit_id"], s["start"], s["end"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(s)
        label = str(it.get("label") or "").strip()
        if label not in ITEM_LABELS:
            label = "无法确定"
        items.append(
            {
                "question": str(it.get("question") or "").strip(),
                "answer_criteria": str(it.get("answer_criteria") or "").strip(),
                "supporting_quote": quote,
                "acceptable_spans": uniq,
                "label": label,
                "reason": str(it.get("reason") or "").strip(),
            }
        )
    visible_units = sorted(u for u in units if u <= int(checkpoint))
    return {
        "work_id": work_id,
        "prefix_id": prefix_id,
        "annotator_id": annotator_id,
        "protocol_version": PROTOCOL_VERSION,
        "scope_read": {
            "unit_ids": visible_units,
            "units_sha256": _sha(
                json.dumps(
                    [{"unit_id": u, "sha256": _sha(units[u])} for u in visible_units],
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
            "visible_range_end": int(checkpoint),
        },
        "completed": parsed.get("complete") is True,
        "completed_at": completed_at,
        "freeze_sha256": freeze_sha256,
        "items": items,
    }


async def main_async(args) -> int:
    from backend.core.llm_client import LLMClient
    from backend.config.settings import ConfigManager

    selection = json.loads((PROP / "pilot-selection-2026-11-07.json").read_text(encoding="utf-8"))
    work = next((w for w in selection["works"] if w["work_id"] == args.work), None)
    if work is None:
        print(f"找不到作品 {args.work}")
        return 2
    pref = next((p for p in work["prefixes"] if p["prefix_id"] == args.prefix), None)
    if pref is None:
        print(f"找不到前缀 {args.prefix}")
        return 2
    checkpoint = int(pref["checkpoint"])

    units: dict[int, str] = {}
    for p in sorted(Path(work["source_directory"]).glob("*.txt")):
        if p.stem.isdigit():
            units[int(p.stem)] = p.read_text(encoding="utf-8")

    sys_txt = (PROP / "reference-discovery-system.txt").read_text(encoding="utf-8")
    user_tpl = (PROP / "reference-discovery-user-template.txt").read_text(encoding="utf-8")
    source = render_units(units, checkpoint)
    user_txt = assemble(
        user_tpl,
        {
            "work_id": args.work,
            "prefix_id": args.prefix,
            "visible_range_end": checkpoint,
            "unit_count": len([u for u in units if u <= checkpoint]),
            "source_units_with_stable_ids": source,
        },
    )
    request_sha = _sha(sys_txt + "\n" + user_txt)

    # token 预检与名额预留：在配置加载与客户端准备就绪后，跨进程事务预留
    from scripts.experiments.bc.bc_t4_registration_pilot_runner import request_token_bound, run_guarded_llm_request

    cfg = ConfigManager(ROOT / "config.json").load()
    api = replace(
        cfg.api,
        model=args.model,
        sdk_max_retries=0,
        max_retries=0,
        temperature_max_retries=1,
        backoff_max_retries=0,
        streaming_enabled=False,
    )
    client = LLMClient(api)

    archive_path = PROP / "reference-discovery-usage.json"
    projected_in = request_token_bound(sys_txt, user_txt, model=args.model)

    run_res = await run_guarded_llm_request(
        client=client,
        archive_path=archive_path,
        work_id=args.work,
        prefix_id=args.prefix,
        arm="-",
        stage=STAGE_REFERENCE,
        messages=[{"role": "system", "content": sys_txt}, {"role": "user", "content": user_txt}],
        projected_input_tokens=projected_in,
        projected_output_tokens=args.max_output_tokens,
        request_sha=request_sha,
        max_runner_attempts=1,  # 默认单次试跑；由外层显式控制，网络重试逐次申请额度
        extra={"annotator_id": args.annotator},
    )

    if not run_res["ok"]:
        if run_res.get("reason") == "budget_blocked":
            print(f"发送前预算预检未通过，拒绝发送：{run_res.get('detail')}")
            return 3
        print(f"调用异常中止或失败（已保留未知用量预算占用）：{run_res.get('last_error')}")
        return 4

    res = run_res["result"]
    if not getattr(res, "success", True):
        print(f"调用失败：{getattr(res, 'error', '')}")
        return 4

    raw = getattr(res, "content", "") or ""
    parsed = parse_model_output(raw)
    if not parsed["parse_ok"]:
        print(f"输出解析失败：{parsed['reason']}")
        (PROP / f"reference-raw-{args.work}-{args.prefix}-{args.annotator}.txt").write_text(
            raw, encoding="utf-8"
        )
        return 5

    from datetime import datetime

    ann = assemble_annotation(
        work_id=args.work,
        prefix_id=args.prefix,
        annotator_id=args.annotator,
        checkpoint=checkpoint,
        units=units,
        parsed=parsed,
        freeze_sha256=request_sha,
        completed_at=datetime.now().isoformat(timespec="seconds"),
    )
    v = validate_annotation(ann)
    ann["validation"] = {"ok": v["ok"], "errors": v["errors"]}
    ann["model_complete"] = parsed.get("complete")
    ann["raw_response_sha256"] = _sha(raw)
    ann["raw_response"] = raw
    ann["reviewer_type_note"] = "本次参考发现由模型在隔离上下文中独立完成"

    out = PROP / f"reference-{args.work}-{args.prefix}-{args.annotator}.json"
    out.write_text(json.dumps(ann, ensure_ascii=False, indent=2), encoding="utf-8")

    archive = UsageArchive.load(archive_path)
    tokens = getattr(res, "tokens", None)
    in_tok = tokens[0] if (tokens and len(tokens) >= 2) else None
    out_tok = tokens[1] if (tokens and len(tokens) >= 2) else None

    print(json.dumps(
        {
            "saved": str(out.relative_to(ROOT)),
            "validation": ann["validation"],
            "item_count": len(ann["items"]),
            "items_with_spans": sum(1 for i in ann["items"] if i["acceptable_spans"]),
            "label_hist": {
                l: sum(1 for i in ann["items"] if i["label"] == l) for l in ITEM_LABELS
            },
            "tokens": {"input": in_tok, "output": out_tok},
            "usage_summary": archive.summary(),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--annotator", required=True, help="标注者标识，如 ref_1")
    ap.add_argument("--model", default="cn:deepseek-v4.1-flash")
    ap.add_argument("--max-output-tokens", type=int, default=2000)
    args = ap.parse_args()
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
