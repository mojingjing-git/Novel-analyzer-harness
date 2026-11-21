"""独立复核两臂模板方案的装配与差异（离线，不调用模型）。

不采信 manifest 里的声明，全部重算：
1. 文件哈希是否与 manifest 一致；
2. B 是否**逐字**继承 A 的规则（B 是 A 的严格超集）；
3. 同一任务的 user 消息在两臂是否逐字一致（差异只应出现在 system 消息）；
4. 替换模板占位符时，插入的原文不会被二次模板展开。

第 4 条是真实风险：原文里若出现花括号，用 str.format 会炸或误展开。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROP = ROOT / "docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal"
OUT = ROOT / "docs/research/bc-production-readiness-2026-11-06/registration-arm-proposal"

PLACEHOLDERS = (
    "work_id",
    "prefix_id",
    "visible_range_end",
    "registration_checkpoint",
    "source_units_with_stable_ids",
)


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def assemble(system_txt: str, user_template: str, fields: dict) -> dict:
    """装配一条请求。

    占位符**逐个替换**，插入的原文不再做模板展开——
    否则原文里的花括号会被当成占位符。
    """
    out = user_template
    for key in PLACEHOLDERS:
        out = out.replace("{" + key + "}", str(fields.get(key, "")))
    return {"system": system_txt, "user": out}


def main() -> None:
    manifest = json.loads((PROP / "manifest.json").read_text(encoding="utf-8"))
    files = {
        "arm-a-rule.txt": (PROP / "arm-a-rule.txt").read_text(encoding="utf-8"),
        "arm-b-rule.txt": (PROP / "arm-b-rule.txt").read_text(encoding="utf-8"),
        "common-system.txt": (PROP / "common-system.txt").read_text(encoding="utf-8"),
        "common-user-template.txt": (PROP / "common-user-template.txt").read_text(encoding="utf-8"),
    }
    actual = {name: _sha(txt) for name, txt in files.items()}
    declared = manifest.get("files_sha256") or {}
    hash_ok = {k: (actual.get(k) == declared.get(k)) for k in declared}

    a_rule = files["arm-a-rule.txt"]
    b_rule = files["arm-b-rule.txt"]
    # B 必须逐字包含 A 的全部规则内容（B 是 A 的严格超集）
    b_extends_a = a_rule.strip() in b_rule
    # B 的新增部分不得改写 A 的既有措辞
    b_extra = b_rule.replace(a_rule.strip(), "", 1).strip() if b_extends_a else ""

    # 同一任务的 user 消息两臂必须逐字一致
    src_with_braces = '第一章：他说"{name}"，又写下 {not_a_placeholder} 与 {x}。\n第二章：{unknown_placeholder}'
    fields = {
        "work_id": "w1",
        "prefix_id": "p1",
        "visible_range_end": 8,
        "registration_checkpoint": 2,
        "source_units_with_stable_ids": src_with_braces,
    }
    sys_a = files["common-system.txt"] + "\n\n" + a_rule
    sys_b = files["common-system.txt"] + "\n\n" + b_rule
    req_a = assemble(sys_a, files["common-user-template.txt"], fields)
    req_b = assemble(sys_b, files["common-user-template.txt"], fields)
    user_same = req_a["user"] == req_b["user"]
    # 插入的原文不得被二次展开：占位符原样出现在 user 消息里
    not_reformatted = "{not_a_placeholder}" in req_a["user"] and "{unknown_placeholder}" in req_a["user"]
    # 且不得残留任何未替换的模板占位符
    leftover = [
        p for p in PLACEHOLDERS if "{" + p + "}" in req_a["user"]
    ]

    # 装配确定性：同样输入装配两次必须逐字一致
    req_a2 = assemble(sys_a, files["common-user-template.txt"], fields)
    deterministic = req_a == req_a2

    report = {
        "rule_version": manifest.get("rule_version"),
        "hash_check": hash_ok,
        "all_hashes_match": all(hash_ok.values()),
        "candidate_extends_control_rule": b_extends_a,
        "b_extra_rule_chars": len(b_extra),
        "b_extra_rule_text": b_extra,
        "same_user_message": user_same,
        "system_differs_between_arms": req_a["system"] != req_b["system"],
        "inserted_source_not_reformatted": not_reformatted,
        "unreplaced_placeholders": leftover,
        "assembly_deterministic": deterministic,
        "model_calls": 0,
    }
    out = OUT / "verification.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", out)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
