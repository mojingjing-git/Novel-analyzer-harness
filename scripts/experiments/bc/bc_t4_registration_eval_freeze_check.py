"""离线验证冻结与隔离机制（不调用模型、不扩试产）。

用现有曝光样本做三件事：
1. **冻结**：把登记条目的问题、条件、来源跨度、输入前缀哈希、规则版本固定下来；
2. **隔离检查**：核验可见范围覆盖实际送入的全部材料类别、内容哈希可复算、
   辅助材料可逐字溯源到前缀；
3. **指标口径**：验证分母按有效裁决分离、争议与不确定项不进分母。

这一步只验证机制可用，不产生质量结论。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.common.registration_eval_freeze import (  # noqa: E402
    build_freeze_record,
    check_isolation,
    save_freeze,
)
from backend.common.registration_eval_metrics import compute_metrics  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
TRIAL = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"

RULES_VERSION = "registration_eval_v1"
PREFIX_UNITS = 8


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _units(wid: str, arm: str = "candidate") -> dict[int, str]:
    from backend.core.text_units import load_blocks_as_files

    units, _ = load_blocks_as_files(TRIAL / "runs" / wid / arm / "blocks")
    return units


def main() -> None:
    wid = "t4_game_holdout"
    units = _units(wid)
    keys = sorted(units)
    prefix_units = keys[:PREFIX_UNITS]
    prefix_text = "\n".join(units[k] for k in prefix_units)
    later_text = "\n".join(units[k] for k in keys[PREFIX_UNITS:])

    ledger = json.loads(
        (TRIAL / "runs" / wid / "candidate" / "foreshadow_ledger.json").read_text(encoding="utf-8")
    )
    items = []
    for it in ledger.get("items") or []:
        items.append(
            {
                "item_id": str(it.get("id")),
                "question": str(it.get("question") or ""),
                "criteria": str(it.get("answer_criteria") or ""),
                "source_span": None,
                "visible_prefix_id": "p1",
                "registration_chapter": int(it.get("anchor_chapter") or 0),
                "anchor_quote": str(it.get("anchor_quote") or ""),
            }
        )

    # 可见材料：正文前缀 + 摘要 + 知识库 + 检索材料。
    # 辅助材料必须是**抽取式**——可逐字溯源到前缀；改写式内容无法证明"只从前缀生成"。
    aux_summary = prefix_text[:120]
    aux_knowledge = prefix_text[:80]
    aux_retrieval = prefix_text[:60]
    materials = [
        {
            "kind": "body_prefix",
            "used": True,
            "sha256": _sha(prefix_text),
            "content": prefix_text,
            "chars": len(prefix_text),
            "source_prefix_id": "p1",
        },
        {
            "kind": "summary",
            "used": True,
            "sha256": _sha(aux_summary),
            "content": aux_summary,
            "chars": len(aux_summary),
            "source_prefix_id": "p1",
            "generation": "extract_from_prefix",
        },
        {
            "kind": "knowledge",
            "used": True,
            "sha256": _sha(aux_knowledge),
            "content": aux_knowledge,
            "chars": len(aux_knowledge),
            "source_prefix_id": "p1",
            "generation": "extract_from_prefix",
        },
        {
            "kind": "retrieval",
            "used": True,
            "sha256": _sha(aux_retrieval),
            "content": aux_retrieval,
            "chars": len(aux_retrieval),
            "source_prefix_id": "p1",
            "generation": "extract_from_prefix",
            "index_scope": "prefix_only",
        },
    ]

    rec = build_freeze_record(
        work_id=wid,
        prefix_id="p1",
        prefix_unit_ids=prefix_units,
        prefix_sha256=_sha(prefix_text),
        materials=materials,
        items=items,
        rules_version=RULES_VERSION,
        generator_model="",
        prompt_template_version="",
        code_version="",
        budget={"max_calls": 0},
        run_id="offline-check",
        arm="",
        observations=[],
    )
    freeze_path = OUT_DIR / "t4-registration-eval-freeze.json"
    created = save_freeze(freeze_path, rec)

    iso = check_isolation(
        record=rec, materials=materials, prefix_text=prefix_text, later_text=later_text
    )

    # 负例：辅助材料里混进后文内容 → 必须挡住
    leaked = [dict(m) for m in materials]
    leaked[1] = dict(leaked[1])
    leaked[1]["content"] = later_text[:100]
    leaked[1]["sha256"] = _sha(leaked[1]["content"])
    iso_bad = check_isolation(
        record=rec, materials=leaked, prefix_text=prefix_text, later_text=later_text
    )

    # 指标口径演示：1 条无依据 + 1 条争议 → 1/1，另报争议 1 条
    demo = compute_metrics(
        system_items=[{"item_id": "a"}, {"item_id": "b"}],
        adjudicated_items=[
            {"item_id": "a", "label": "无依据", "span_located": False},
            {"item_id": "b", "label": "有依据", "span_located": True, "disputed": True},
        ],
        independent_qualified=[],
    )

    out = {
        "work_id": wid,
        "prefix_units": prefix_units,
        "later_units": keys[PREFIX_UNITS:],
        "freeze": {
            "path": str(freeze_path.relative_to(ROOT)),
            "created_this_run": created,
            "items_frozen": len(items),
            "material_kinds": rec["material_kinds"],
            "material_kinds_complete": rec["material_kinds_complete"],
            "materials_all_hashed": rec["materials_all_hashed"],
            "declared_unused_kinds": rec["declared_unused_kinds"],
        },
        "isolation_check": iso,
        "isolation_negative_case": {
            "ok": iso_bad["ok"],
            "problems": iso_bad["problems"],
            "note": "辅助材料混进后文内容必须被挡住",
        },
        "metrics_denominator_demo": {
            "no_ground_rate": demo["metrics"]["no_ground_rate"],
            "denominators": demo["denominators"],
            "disputed": demo["excluded"]["disputed_ids"],
            "note": "1 条无依据 + 1 条争议 → 1/1，争议单列",
        },
        "note": (
            "这一步只验证机制：冻结可落盘且拒绝原位覆盖、隔离检查能挡住"
            "类别缺失/哈希乱填/辅助材料混入后文、指标分母按有效裁决分离。"
            "不产生质量结论。"
        ),
    }
    p = OUT_DIR / "t4-registration-eval-freeze-check.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p)
    print(json.dumps(out, ensure_ascii=False, indent=2)[:2200])


if __name__ == "__main__":
    main()
