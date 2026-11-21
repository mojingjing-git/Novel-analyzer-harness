#!/usr/bin/env python
"""Freeze corpus selection without displaying text; explicitly export blind annotation packets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from backend.common.evaluation_corpus import export_packets, freeze_plan, verify_manifest


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    freeze = subs.add_parser("freeze")
    freeze.add_argument("--plan", required=True)
    freeze.add_argument("--bundle", type=Path, required=True)
    export = subs.add_parser("export")
    export.add_argument("--bundle", type=Path, required=True)
    export.add_argument("--work", nargs="+", required=True)
    export.add_argument("--purpose", choices=["validation", "development", "final_evaluation"], required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--run-spec", type=Path)
    status = subs.add_parser("status")
    status.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            # A fresh directory makes accidental re-freezing/erasing history impossible.
            if args.bundle.exists():
                raise ValueError("bundle_already_exists")
            plan = read_json(args.plan)
            manifest = freeze_plan(plan, ROOT)
            args.bundle.mkdir(parents=True, exist_ok=False)
            for name, data in [("plan.json", plan), ("manifest.json", manifest)]:
                with (args.bundle / name).open("x", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            print(json.dumps({"manifest_id": manifest["manifest_id"], "works": len(manifest["works"]),
                              "samples": sum(len(w["samples"]) for w in manifest["works"]),
                              "novel_text_exported": False}, ensure_ascii=False))
        elif args.command == "export":
            print(json.dumps(export_packets(read_json(args.bundle / "manifest.json"), args.work,
                  args.bundle, args.output, purpose=args.purpose,
                  run_spec=read_json(args.run_spec) if args.run_spec else None, protocol_root=ROOT), ensure_ascii=False))
        else:
            manifest = read_json(args.bundle / "manifest.json")
            verify_manifest(manifest)
            events = [read_json(p) for p in (args.bundle / "exposures").glob("*.json")]
            for w in manifest["works"]:
                exposures = [e["purpose"] for e in events if w["work_id"] in e["work_ids"]]
                print(json.dumps({"work_id": w["work_id"], "title": w["title"], "split": w["split"],
                                  "exports": exposures, "exposure_state": "opened" if exposures else "metadata_only"},
                                 ensure_ascii=False))
    except (ValueError, OSError, KeyError) as exc:
        # No config/model calls exist here; print error type, not potential source text.
        parser.exit(2, f"corpus operation failed: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
