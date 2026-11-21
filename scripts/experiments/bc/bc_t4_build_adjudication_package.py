"""生成独立复核用的**裁决包**（离线，不调用模型）。

裁决包只含：
- 该前缀的**可见**原文单元；
- 合并后的标注条目（含双方原始条件与跨度）；
- 复核协议与检查项；
- 期望的输出格式。

**不含**：前缀之后的原文、两臂输出、任何答案或回收标签。
复核方应在只提供本包的隔离上下文里工作，避免其上下文里已有的后文信息影响判断。

包内附前缀单元的内容哈希，与选片清单核对——不一致就中止。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.common.registration_reference_set import (  # noqa: E402
    ITEM_LABELS,
    LABEL_ALREADY_ANSWERED,
    LABEL_GROUNDED_UNRESOLVED,
    LABEL_UNDECIDED,
    REVIEWER_TYPE_AI,
)

ROOT = Path(__file__).resolve().parents[3]
PROP = ROOT / "docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal"
OUT_DIR = PROP / "adjudication-packages"

PROTOCOL = {
    "labels": list(ITEM_LABELS),
    "label_meaning": {
        LABEL_GROUNDED_UNRESOLVED: "前缀内已有依据提出，且尚未被回答",
        LABEL_ALREADY_ANSWERED: "前缀内已经给出答案",
        LABEL_UNDECIDED: "无法确定",
    },
    "checks": [
        "问题在该前缀内是否有依据：来源跨度能否在给定单元里定位",
        "问题在该前缀内是否已经被回答：已回答的不能算合格未解问题",
        "双方给出的回答条件是否一致：不同的按原文逐条判断哪些要求成立——"
        "等价的要求归并成一条，互补且各自必要的保留为多条；"
        "**不要机械取并集**，那会把双方要求叠加、重新制造过严的关闭条件；"
        "无法确定的保持争议，不要强行归并",
        "可接受跨度是否覆盖了该问题成立所需的全部位置",
        "单方条目是否合格：另一方没提到不等于它不成立，但也不能仅凭一方就确认",
        "疑似重复是否应合并：同引文但问题不同不合并，问题相同但措辞不同应合并",
    ],
    "rules": [
        "只依据本包提供的前缀原文判断，不使用对作品的记忆或后文信息",
        "无法确定的保留争议，不要强行判定",
        "每条都要写理由，理由供抽查",
        "不要填写 registered：那是盲审对照系统输出后才确定的字段",
    ],
    "output_schema": {
        "decisions": [
            {
                "ref_id": "<条目 id>",
                "qualified": True,
                "qualified_label": LABEL_GROUNDED_UNRESOLVED,
                "accepted_answer_criteria": "<可接受的回答条件>",
                "accepted_spans": [{"unit_id": 1, "start": 0, "end": 10}],
                "reason": "<简短理由>",
            }
        ],
        "note": (
            "qualified=true 只在标签为「有依据且未解」时可用；"
            "其余情况 qualified=false 并给出 qualified_label 与 reason。"
        ),
    },
    "reviewer_type": REVIEWER_TYPE_AI,
    "reviewer_type_note": "记录中如实标注为 AI 独立复核，不冒称人工标注",
}


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _units(source_dir: Path) -> dict[int, str]:
    out: dict[int, str] = {}
    for p in sorted(Path(source_dir).glob("*.txt")):
        if p.stem.isdigit():
            out[int(p.stem)] = p.read_text(encoding="utf-8")
    return out


def main() -> None:
    selection = json.loads((PROP / "pilot-selection-2026-11-07.json").read_text(encoding="utf-8"))
    merged_path = PROP / "merged-references.json"
    merged_doc = json.loads(merged_path.read_text(encoding="utf-8")) if merged_path.is_file() else {}
    merged_by_key = {
        (m.get("work_id"), m.get("prefix_id")): m.get("items") or []
        for m in (merged_doc.get("merged") or [])
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []
    for work in selection.get("works") or []:
        wid = work.get("work_id")
        units = _units(Path(work["source_directory"]))
        for pref in work.get("prefixes") or []:
            pid = pref.get("prefix_id")
            ck = int(pref.get("checkpoint") or 0)
            visible_ids = [u["unit_id"] for u in pref.get("units") or []]

            # 与选片清单核对：单元内容哈希必须一致
            mismatches = []
            for u in pref.get("units") or []:
                actual = _sha(units.get(int(u["unit_id"]), ""))
                if actual != u.get("sha256"):
                    mismatches.append(
                        {"unit_id": u.get("unit_id"), "declared": u.get("sha256"), "actual": actual}
                    )
            if mismatches:
                raise SystemExit(f"前缀单元哈希与选片清单不一致，中止：{wid}/{pid} {mismatches}")

            items = merged_by_key.get((wid, pid), [])
            if not items:
                # 参考发现还没跑（需付费调用）：先出前缀+协议的骨架，
                # 条目在参考发现完成后重建时填入。
                print(f"  提示：{wid}/{pid} 尚无合并标注条目，先出前缀与协议骨架")
            package = {
                "package_version": "registration_adjudication_package_v1",
                "work_id": wid,
                "prefix_id": pid,
                "checkpoint": ck,
                "units": [
                    {"unit_id": uid, "text": units[uid]} for uid in visible_ids if uid in units
                ],
                "units_sha256": _sha(
                    json.dumps(
                        [{"unit_id": uid, "sha256": _sha(units[uid])} for uid in visible_ids],
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                ),
                "items": items,
                "item_count": len(items),
                "protocol": PROTOCOL,
                "isolation": {
                    "contains_only": ["prefix_units", "merged_annotations", "protocol"],
                    "no_later_text": True,
                    "no_system_output": True,
                    "no_answer_or_resolved_label": True,
                    "note": "复核方应在只提供本包的隔离上下文中工作",
                },
            }
            # 规范哈希：复核方**直接引用这个字段**即可，
            # 不必自己重新序列化包——不同序列化约定算出的哈希对不上，
            # 那是约定问题，不该靠复核方猜。
            package["package_sha256_canonical"] = _sha(
                json.dumps(
                    package, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            )
            package["package_sha256_note"] = (
                "本字段即包的规范哈希（对除本字段外的内容按 sort_keys + 紧凑分隔符计算）。"
                "引用它，不要自己重新序列化整个文件。"
            )
            p = OUT_DIR / f"{wid}__{pid}.json"
            p.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
            manifest.append(
                {
                    "work_id": wid,
                    "prefix_id": pid,
                    "checkpoint": ck,
                    "package_path": str(p.relative_to(ROOT)),
                    "unit_count": len(package["units"]),
                    "item_count": len(items),
                    "items_present": bool(items),
                    "status": "ready_for_adjudication" if items else "skeleton_no_annotations_yet",
                    "status_note": (
                        "骨架：只有原文与协议，尚无参考标注条目。"
                        "**这不是“零条合格问题”**——参考发现尚未执行。"
                        if not items
                        else "含真实标注条目，可进入裁决"
                    ),
                    "units_sha256": package["units_sha256"],
                    "chars": sum(len(u["text"]) for u in package["units"]),
                }
            )
            print(f"{wid}/{pid}: units={len(package['units'])} items={len(items)} "
                  f"chars={manifest[-1]['chars']} status={manifest[-1]['status']}")

    mp = OUT_DIR / "manifest.json"
    mp.write_text(
        json.dumps({"packages": manifest, "model_calls": 0}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("saved:", mp)


if __name__ == "__main__":
    main()
