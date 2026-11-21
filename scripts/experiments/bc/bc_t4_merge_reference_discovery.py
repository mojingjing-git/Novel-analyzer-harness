"""合并参考发现并冻结原始标注（离线）。

输入：一个前缀的两份独立标注文件。
输出：合并结果 + 冻结的原始标注记录。

**不做资格裁决**——那是独立复核的事。合并只做去重、保留争议、
标记疑似重复，并保留双方原始条件与跨度。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.common.registration_reference_set import (  # noqa: E402
    freeze_reference_set,
    merge_annotations,
    save_reference_set,
    validate_annotation,
)

ROOT = Path(__file__).resolve().parents[3]
PROP = ROOT / "docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal"


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--annotators", nargs="+", required=True)
    args = ap.parse_args()

    docs = []
    for a in args.annotators:
        p = PROP / f"reference-{args.work}-{args.prefix}-{a}.json"
        docs.append(json.loads(p.read_text(encoding="utf-8")))

    validations = []
    for a, d in zip(args.annotators, docs):
        v = validate_annotation(d)
        validations.append({"annotator_id": a, "ok": v["ok"], "errors": v["errors"]})
    bad = [v for v in validations if not v["ok"]]
    if bad:
        raise SystemExit(f"标注未通过校验，中止：{bad}")

    merged = merge_annotations(docs[0], docs[1], work_id=args.work, prefix_id=args.prefix)
    if not merged["ok"]:
        raise SystemExit(f"合并失败：{merged['errors']}")
    frozen = freeze_reference_set(merged, annotator_ids=list(args.annotators))
    frozen["validation"] = validations
    frozen["annotation_ids"] = list(args.annotators)

    raw_path = PROP / f"reference-merged-{args.work}-{args.prefix}.json"
    created = save_reference_set(raw_path, frozen)

    # 汇总到总表，供裁决包生成器读取
    all_path = PROP / "merged-references.json"
    all_doc = {"merged": []}
    if all_path.is_file():
        try:
            all_doc = json.loads(all_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            all_doc = {"merged": []}
    entries = [
        e for e in (all_doc.get("merged") or [])
        if not (e.get("work_id") == args.work and e.get("prefix_id") == args.prefix)
    ]
    entries.append(
        {
            "work_id": args.work,
            "prefix_id": args.prefix,
            "items": frozen["items"],
            "summary": frozen["summary"],
            "freeze_sha256": frozen["freeze_sha256"],
        }
    )
    all_doc["merged"] = entries
    all_path.write_text(json.dumps(all_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(
        {
            "saved": str(raw_path.relative_to(ROOT)),
            "created_this_run": created,
            "item_count": frozen["item_count"],
            "summary": frozen["summary"],
            "freeze_sha256": frozen["freeze_sha256"],
            "items": [
                {
                    "ref_id": i["ref_id"],
                    "status": i["status"],
                    "label": i["label"],
                    "question": i["question"],
                    "possible_duplicate_with": i.get("possible_duplicate_with"),
                    "spans": len(i["acceptable_spans"]),
                }
                for i in frozen["items"]
            ],
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
