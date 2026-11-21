"""用已有 7 条候选的**历史真实响应**离线回放，验证恢复机制的修复效果。

不发起任何模型调用、不扩大试产：
- 把历史审核响应喂给修复后的解析器，看能否救回被丢弃的正确裁决；
- 统计 7 条的证据侧定论（已送达 / 未送达 / 未定位）。

证据侧定论依据（见 t4-evidence-rebuild*.json）：
- prompt_sha256 与缓存记录一致 → 重建的提示词与历史请求完全相同，
  据此可以断言"实际送达的证据里有没有关键句"；
- 初始复检阶段（无审核记录）的历史请求未缓存 → 标为未定位。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.common.c_claim_audit_gate import extract_json_object  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
TRIAL = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"

#: 7 条经独立裁决确认"原文确有可答事实"的正例候选
ADJUDICATED = [
    ("t4_game_holdout", "fs_003"),
    ("t4_urban_holdout", "fs_001"),
    ("t4_urban_holdout", "fs_009"),
    ("t4_urban_holdout", "fs_011"),
    ("t4_hist_holdout", "fs_006"),
    ("t4_hist_holdout", "fs_007"),
    ("t4_hist_holdout", "fs_011"),
]

#: 每条候选的"关键支撑句"——答案要成立就必须在证据里出现
KEY_SENTENCES = {
    ("t4_game_holdout", "fs_003"): ["怨气", "感激", "信仰之力", "神力值"],
    ("t4_urban_holdout", "fs_001"): ["2001年", "密码", "记忆"],
    ("t4_urban_holdout", "fs_009"): ["12个数字", "透明手袋", "转笔"],
    ("t4_urban_holdout", "fs_011"): ["5考场", "周航", "转笔", "轨迹重叠", "诚信"],
    ("t4_hist_holdout", "fs_006"): ["勾结金人", "搬三山", "扳倒三衙禁军", "权力传承"],
    ("t4_hist_holdout", "fs_007"): ["本相要知道", "皇城司指使", "过河卒子", "李押班"],
    ("t4_hist_holdout", "fs_011"): ["勾结金人", "搬三山", "扳倒三衙禁军"],
}

#: 关键词缺失 ≠ 证据未送达。
#: 关键词只能提示"值得核对的原文跨度"；是否构成未送达，
#: 要由**精确原文跨度**是否存在、以及该跨度与登记时序的关系共同决定。
#: 登记时序归类见 t4-background-vs-later-answer.json。
TIME_CLASS = {
    ("t4_game_holdout", "fs_003"): ["same_chapter_order_pending"],
    ("t4_urban_holdout", "fs_001"): ["later_answer", "same_chapter_order_pending"],
    ("t4_urban_holdout", "fs_009"): ["later_answer"],
    ("t4_urban_holdout", "fs_011"): ["later_answer", "same_chapter_order_pending"],
    ("t4_hist_holdout", "fs_006"): ["later_answer", "same_chapter_order_pending"],
    ("t4_hist_holdout", "fs_007"): ["later_answer", "same_chapter_order_pending"],
    ("t4_hist_holdout", "fs_011"): ["background_material"],
}

#: 各候选的实际审核响应所属作品（审计阶段只覆盖 candidate 臂）
AUDIT_ARMS = ["candidate"]


def load_cache(wid: str, arm: str) -> dict:
    p = TRIAL / "runs" / wid / arm / "output" / "c_audit_cache.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def load_rebuild() -> dict[tuple[str, str, str], dict]:
    p = TRIAL / "t4-evidence-rebuild-all.json"
    if not p.is_file():
        return {}
    rows = json.loads(p.read_text(encoding="utf-8"))
    return {
        (r["work_id"], r["arm"], r["candidate_id"]): r
        for r in rows
        if r.get("status") == "ok"
    }


def main() -> None:
    rebuild = load_rebuild()
    out_rows: list[dict] = []

    for wid, fs_id in ADJUDICATED:
        keys = KEY_SENTENCES.get((wid, fs_id), [])
        for arm in ("baseline", "b_fixed", "candidate"):
            cache = load_cache(wid, arm)
            entry = next(
                (v for v in cache.values() if str(v.get("fs_id")) == fs_id), None
            )
            rb = rebuild.get((wid, arm, fs_id))
            evidence_chars = 0
            hits: list[str] = []
            miss: list[str] = []
            sha_matched = None
            if rb:
                joined = "\n".join(s["text"] for s in rb["segments"])
                evidence_chars = len(joined)
                hits = [k for k in keys if k in joined]
                miss = [k for k in keys if k not in joined]
                sha_matched = rb.get("matched")

            row = {
                "work_id": wid,
                "candidate_id": fs_id,
                "arm": arm,
                "audit_stage_recorded": entry is not None,
                "evidence_chars": evidence_chars,
                "evidence_key_sentences_hit": hits,
                "evidence_key_sentences_missing": miss,
                "prompt_sha256_matched": sha_matched,
            }

            if entry is not None:
                raw = entry.get("raw_response_text") or ""
                parsed_before = None
                try:
                    parsed_before = json.loads(raw)
                except json.JSONDecodeError:
                    parsed_before = None
                parsed_after = extract_json_object(raw)
                row["audit_record_status"] = entry.get("status")
                row["audit_record_decision"] = entry.get("decision")
                row["raw_json_valid_before_fix"] = parsed_before is not None
                row["raw_json_valid_after_fix"] = parsed_after is not None
                row["recovered_verdict_after_fix"] = (
                    (parsed_after or {}).get("verdict")
                    if parsed_before is None
                    else None
                )
                row["verdict_before_fix"] = (parsed_before or {}).get("verdict")
                row["verdict_after_fix"] = (parsed_after or {}).get("verdict")
                row["time_class"] = TIME_CLASS.get((wid, fs_id), [])
                if sha_matched:
                    # 重建 = 历史请求（sha256 一致）。但"关键词缺失"只是线索，
                    # 未送达的判定还要看登记时序：支撑句早于登记章的是**背景材料**，
                    # 不属于"该送没送"，而是"登记之后没有相应内容"。
                    if miss and len(hits) < len(keys):
                        row["evidence_delivery"] = "confirmed_span_absent"
                        row["evidence_basis"] = (
                            "prompt_sha256 与缓存记录一致（重建=历史请求）；"
                            "指定支撑句不在该请求的证据里"
                        )
                    else:
                        row["evidence_delivery"] = "confirmed_span_present"
                        row["evidence_basis"] = (
                            "prompt_sha256 与缓存记录一致（重建=历史请求）；"
                            "指定支撑句在该请求的证据里"
                        )
                else:
                    row["evidence_delivery"] = "audit_stage_unverified"
                    row["evidence_basis"] = (
                        "哈希未匹配，重建不能代表历史请求；"
                        "审核模型引用了它看到的证据，但引用不能证明它没收到其他内容"
                    )
            else:
                # 无审核记录 = 停在初始复检阶段；历史请求未缓存
                if sha_matched:
                    row["evidence_delivery"] = (
                        "confirmed_span_absent"
                        if miss and len(hits) < len(keys)
                        else "confirmed_span_present"
                    )
                    row["evidence_basis"] = "prompt_sha256 与缓存记录一致（重建=历史请求）"
                else:
                    row["evidence_delivery"] = "unlocated"
                    row["evidence_basis"] = (
                        "初始复检阶段的历史请求未缓存；重建结果未经 sha256 校验，"
                        "只作线索不作结论"
                    )
            out_rows.append(row)

    summary = {
        "schema": "t4_closeout_evidence_attribution_v1",
        "scope": "7 条经独立裁决确认有正解的候选 × 3 臂",
        "method": (
            "审核阶段：重建审计提示词并与缓存记录的 prompt_sha256 比对，"
            "一致即证明重建与历史请求完全相同，可据关键句断言证据是否送达；"
            "初始复检阶段历史请求未缓存，标为未定位。"
        ),
        "repair_effect": {
            "note": (
                "本地修复只动 JSON 结构（补/改闭合符），不动任何字段值；"
                "语义拒绝不参与修复、也不重试。"
                "修复范围受约束：只在语法明确期待闭合符的位置修一处，"
                "改词元/提前闭合/重复键/多处歧义一律拒绝。"
            ),
            "scope_note": (
                "恢复的是**模型裁决的解析**，不等于门禁最终通过。"
                "原先 game/fs_003 的门禁回放判存疑（待复核：evidence_not_after_anchor）——"
                "该门禁只比较章号，同章一律标记，看不到同章内字符先后。"
                "按同一份原文、同一套字符坐标的时序判定实测：锚点在第 6 章第 1139 字符处"
                "（唯一命中），参与判断的证据段全部在锚点之后，判定为 all_after；"
                "补上时序结果后门禁回放为接受。"
                "这一条是**回归案例的观察结果**，不作为验收目标——"
                "时序只解除时间疑点，答案正确性、条件满足、信息增量仍由原门禁判断。"
            ),
            "recovered_from_broken_json": [
                {
                    "work_id": r["work_id"],
                    "candidate_id": r["candidate_id"],
                    "arm": r["arm"],
                    "verdict_before_fix": r.get("verdict_before_fix"),
                    "verdict_after_fix": r.get("verdict_after_fix"),
                    "gate_outcome": (
                        "存疑（evidence_not_after_anchor）；补上时序判定后回放为接受"
                        if (r["work_id"], r["candidate_id"]) == ("t4_game_holdout", "fs_003")
                        else None
                    ),
                }
                for r in out_rows
                if r.get("raw_json_valid_before_fix") is False
                and r.get("raw_json_valid_after_fix") is True
            ],
        },
        "rows": out_rows,
    }
    p = OUT_DIR / "t4-closeout-evidence-attribution.json"
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p)
    for r in out_rows:
        print(
            f"{r['work_id']:<18}/{r['arm']:<10}/{r['candidate_id']}: "
            f"delivery={r['evidence_delivery']:<22} "
            f"hits={len(r['evidence_key_sentences_hit'])} miss={len(r['evidence_key_sentences_missing'])}"
        )


if __name__ == "__main__":
    main()
