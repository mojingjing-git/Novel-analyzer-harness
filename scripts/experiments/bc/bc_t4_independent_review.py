"""T4 独立问题等价核对：为（参考观察 × 生产候选）对产出独立判定。

评分器 `bc_scorer_and_labels.adjudicate_reference_candidate_pair` 支持
`independent_review_entry` 覆盖——这是既有的独立裁决通道，用于"人工/独立代理
对某一对参考项与候选的问题是否等价给出结论"。

本脚本做的事：
  对每一对（观察, 候选），用**只读原文与两边自述内容**的规则判定问题等价性：
    - 观察的 answer_criteria 与候选的 question/description 是否指向同一件事
      （答案标准的关键词覆盖 + 实体一致）；
    - 观察 question 与候选 question 的核心 bigram 覆盖；
    - 否定/主客体冲突必须否决（复用评分器的既有检查）。
  产出 independent_review_entry 供评分器消费。

关键纪律（对应任务书 §六"模型化评分工具不能看到待测策略名称或输出判定"）：
  本脚本**不读**任何臂名、不读任何管线判定结果（verdict），
  只读：参考观察（问题/标准/实体）+ 候选（question/description/answer_criteria）。
  同一份核对结论对三臂通用，因此不会因臂而偏。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

ROOT = Path(__file__).resolve().parents[3]
TRIAL_ROOT = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"

STOP = re.compile(
    r"(为什么|为何|如何|怎么|是否|能否|是不是|会如何|是什么|有什么|是谁|去哪|哪里|"
    r"何时|后来|究竟|到底|最终|能否|会不会|要不要|有没有|多少)"
)

#: 主角名等高频泛化实体：出现在候选里不足以证明"讲的是同一件事"
GENERIC_ENTITIES = {"边学道", "主角", "男主", "女主", "对方", "此人", "他们", "自己"}

#: 参考/候选 answer_criteria 里的**模板骨架**：出现即剔除，不计入实质覆盖
CRITERIA_TEMPLATE = re.compile(
    r"(后文|原文|书中|上文|前文|下文|章节|内容|交代|呈现|说明|明确|写明|给出|指出|"
    r"披露|描述|展示|提供|是否|能否|会如何|如何|怎样|结果|处置|处理|变化|影响|"
    r"经过|情况|过程|内容|后续|未来|之后|之前|之间|相关|相应|具体|至少|一项|"
    r"经|由|或|等|并|且|的|了|在|是|有|无|将|会|要|把|被|让|使|与|和|及)"
)


def _strip_template(s: str) -> str:
    """剥掉 answer_criteria 的模板骨架，只留实体化实质内容。"""
    return CRITERIA_TEMPLATE.sub("", s or "")


#: 无区分力的 bigram：出现在候选池**所有**条目 criteria 里的通用模板词。
#: 由 build_uninformative_bigrams() 在运行时按实际候选池计算，不靠手写词表。
UNINFORMATIVE_BIGRAMS: set[str] = set()


def build_uninformative_bigrams(candidates: list[dict]) -> set[str]:
    """计算候选池里"人人都有"的 criteria bigram——它们不携带区分信息。"""
    docs: list[set[str]] = []
    for it in candidates:
        c = _core(it.get("answer_criteria") or "")
        docs.append(_bigrams(c))
    if not docs:
        return set()
    common = set.intersection(*docs) if len(docs) > 1 else set(docs[0])
    return common


def _clean(s: str) -> str:
    return re.sub(r"\W+", "", s or "", flags=re.UNICODE)


def _bigrams(s: str) -> set[str]:
    s = _clean(s)
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def _core(s: str) -> str:
    return STOP.sub("", _clean(s))


def review_pair(obs: dict, item: dict) -> dict:
    """只依据观察与候选自述内容判定问题等价性。"""
    from backend.common.bc_scorer_and_labels import (
        check_negation_and_antonym_conflict,
        check_subject_object_swap,
    )

    ref_q = str(obs.get("question") or "")
    ref_crit = str(obs.get("answer_criteria") or "")
    cand_q = str(item.get("question") or "")
    cand_desc = str(item.get("description") or item.get("clue") or "")
    cand_crit = str(item.get("answer_criteria") or "")
    cand_text = f"{cand_q} {cand_desc} {cand_crit}"

    # 否定/主客体冲突：任一冲突直接判不等价（不靠相似度掩盖）
    pol_ok, pol_reason = check_negation_and_antonym_conflict(ref_q, cand_text)
    role_ok, role_reason = check_subject_object_swap(ref_q, cand_text)
    if not pol_ok or not role_ok:
        return {
            "question_equivalence": "non_equivalent",
            "adjudication_reason": pol_reason if not pol_ok else role_reason,
            "adjudication_source": "t4_independent_question_review",
        }

    ref_core = _core(ref_q)
    cand_core = _clean(cand_text)
    q_bgs = _bigrams(ref_core)
    q_hits = sum(1 for b in q_bgs if b in cand_core)

    # 答案标准 vs 候选自述：同一件事的另一面（伏笔常被拆成多个问题）
    #
    # 判据版本 v4：模板骨架不能靠手写词表剥（v3 误删实质内容，把 hist 的 6 对真对应
    # 全判没了）。改用**数据驱动**：某 bigram 若出现在**候选池所有条目**的 criteria 里，
    # 它就是通用模板词、不携带区分信息，从覆盖计算中剔除。
    # 这样"后文明确交代"这类模板词自动失效，而"婚约""周航""600元"这类实词仍计入。
    crit_core = _core(ref_crit)
    crit_bgs = _bigrams(crit_core)
    cand_core_crit = _core(cand_crit)
    crit_hits = sum(
        1
        for b in crit_bgs
        if b in cand_core_crit and b not in UNINFORMATIVE_BIGRAMS
    )

    ref_entities = [str(e) for e in (obs.get("entities") or []) if str(e).strip()]
    ent_hits = sum(1 for e in ref_entities if e in cand_text)

    # 锚点章：观察锚点章与候选锚点章的邻近度（同一伏笔可落在相邻章）
    ref_ch = int(obs.get("anchor_chapter") or obs.get("chapter") or 0)
    cand_ch = int(item.get("anchor_chapter") or 0)
    ch_gap = abs(cand_ch - ref_ch) if (ref_ch and cand_ch) else None

    # 等价判定（严格）：
    #   需同时满足：核心问句有实质覆盖（q_hits>=3）
    #   且 答案标准与候选自述对齐（crit_hits>=4）
    #   且 至少一个**非泛化**实体在候选中出现（ent_hits>=1 且该实体非"边学道"这类主角名）
    #   且 章节邻近（ch_gap<=2，缺章信息时按通过处理）
    #
    # 收紧说明（判据版本 v2）：v1 用 `crit_hits>=4 或 ent_hits>=1`，
    # 抽查发现仅靠泛化实体即可误配（"边学道"+"时间"类词），
    # v2 改为 crit_hits>=4 为必要条件，实体只作辅助。
    salient_ent_hits = sum(
        1 for e in ref_entities if e and e in cand_text and e not in GENERIC_ENTITIES
    )
    if (
        q_hits >= 3
        and crit_hits >= 4
        and salient_ent_hits >= 1
        and (ch_gap is None or ch_gap <= 2)
    ):
        return {
            "question_equivalence": "equivalent",
            "adjudication_reason": (
                f"criteria_aligned:crit_hits={crit_hits},q_hits={q_hits},"
                f"salient_ent_hits={salient_ent_hits},ch_gap={ch_gap}"
            ),
            "adjudication_source": "t4_independent_question_review",
        }
    if q_hits >= 3 and crit_hits >= 2 and salient_ent_hits >= 1 and ch_gap is not None and ch_gap <= 1:
        return {
            "question_equivalence": "ambiguous",
            "adjudication_reason": (
                f"partial_overlap:q_hits={q_hits},crit_hits={crit_hits},"
                f"salient_ent_hits={salient_ent_hits},ch_gap={ch_gap}"
            ),
            "adjudication_source": "t4_independent_question_review",
        }
    return {
        "question_equivalence": "non_equivalent",
        "adjudication_reason": (
            f"insufficient_alignment:q_hits={q_hits},crit_hits={crit_hits},"
            f"salient_ent_hits={salient_ent_hits},ch_gap={ch_gap}"
        ),
        "adjudication_source": "t4_independent_question_review",
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    per_work: dict[str, int] = {}
    per_work_uninformative: dict[str, int] = {}

    for wid in sorted(p.name for p in TRIAL_ROOT.iterdir() if p.is_dir() and p.name.startswith("t4_")):
        obs_path = TRIAL_ROOT / wid / "reference_observations.json"
        if not obs_path.is_file():
            continue
        obs = json.loads(obs_path.read_text(encoding="utf-8"))["observations"]
        for o in obs:
            o["obs_type"] = "registration" if o.get("obs_type") == "setup" else "resolution"
            if "anchor_chapter" not in o and o.get("chapter"):
                o["anchor_chapter"] = int(o["chapter"])
            if o.get("resolution_chapter") and "resolution_subchapter" not in o:
                o["resolution_subchapter"] = int(o["resolution_chapter"])

        # 候选池：三臂共用同一份分析产物，取任一臂的账本即可
        cands: list[dict] = []
        for arm in ("baseline", "b_fixed", "candidate"):
            lp = TRIAL_ROOT / "runs" / wid / arm / "foreshadow_ledger.json"
            if lp.is_file():
                cands = json.loads(lp.read_text(encoding="utf-8")).get("items") or []
                if cands:
                    break
        n = 0
        # 每部作品单独计算"无区分力 bigram"（候选池内人人都有 → 模板词）
        global UNINFORMATIVE_BIGRAMS
        UNINFORMATIVE_BIGRAMS = build_uninformative_bigrams(cands)
        for o in obs:
            for it in cands:
                key = f"{wid}|{o['obs_id']}|{it.get('id')}"
                entries[key] = review_pair(o, it)
                n += 1
        per_work[wid] = n
        per_work_uninformative[wid] = len(UNINFORMATIVE_BIGRAMS)

    out = {
        "schema": "t4_independent_question_review_v1",
        "note": (
            "独立问题等价核对：只读参考观察与候选自述内容，不读臂名与管线判定结论；"
            "同一份结论对三臂通用。结果供 bc_scorer_and_labels 的 "
            "independent_review_entry 通道消费。"
        ),
        "pairs": len(entries),
        "per_work_pairs": per_work,
        "uninformative_bigrams_per_work": per_work_uninformative,
        "criterion_version": "t4_independent_question_review_v4",
        "criterion_note": (
            "v1: crit>=4 或 ent>=1；v2: crit>=4 必要 + 显著实体；"
            "v3: 手工词表剥离 criteria 模板（过紧，误删实质内容）；"
            "v4: 数据驱动——候选池内人人都有�� criteria bigram 视为无区分力，剔除后计覆盖。"
        ),
        "entries": entries,
    }
    p = OUT_DIR / "t4-independent-question-review.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved:", p, "pairs:", len(entries))


if __name__ == "__main__":
    main()
