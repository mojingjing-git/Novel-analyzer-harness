"""T4 Step 1: Verify the 4 candidate holdout works are unexposed, then freeze the trial declaration."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.common.c_claim_audit_gate import compute_gate_code_snapshot
from backend.common.provenance_cache import PROVENANCE_SCHEMA_VERSION

OUT_DIR = ROOT / "docs/research/bc-production-readiness-2026-11-06"
TRIAL_ROOT = ROOT / "workspace/bc-evaluation/t4-trial-20261106"
CORPUS_DIR = Path(r"F:\AI\04_小说与写作\修复后txt文件")

# ---------------------------------------------------------------------------
# 1. Build the historical exposure universe from every registry we have
# ---------------------------------------------------------------------------
REGISTRIES = [
    ROOT / "docs/research/bc-improvement-2026-11-05/t0-exposure-registry.json",
    ROOT / "docs/research/bc-cross-work-2026-11-05/exposure-status.json",
    ROOT / "docs/research/bc-cross-work-2026-11-05/summary.json",
]


def _collect_titles() -> list[dict]:
    seen: list[dict] = []
    for reg_path in REGISTRIES:
        if not reg_path.is_file():
            continue
        doc = json.loads(reg_path.read_text(encoding="utf-8"))

        def walk(node, trail: str = ""):
            if isinstance(node, dict):
                t = node.get("title")
                if isinstance(t, str) and t.strip():
                    seen.append(
                        {
                            "title": t.strip(),
                            "source": reg_path.name,
                            "trail": trail,
                            "sha256": node.get("source_sha256"),
                        }
                    )
                for k, v in node.items():
                    if k in ("title",):
                        continue
                    walk(v, f"{trail}.{k}" if trail else k)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{trail}[{i}]")

        walk(doc)
    return seen


HISTORICAL = _collect_titles()

# ---------------------------------------------------------------------------
# 2. Candidate works for the frozen trial
# ---------------------------------------------------------------------------
CANDIDATES = [
    {
        "work_id": "t4_ind_holdout",
        "topic_group": "ind",
        "topic_label": "工业/科幻",
        "title": "从红月开始",
        "filename": "《从红月开始》（精校版全本）.txt",
    },
    {
        "work_id": "t4_game_holdout",
        "topic_group": "game",
        "topic_label": "游戏/电竞",
        "title": "世界树的游戏",
        "filename": "《世界树的游戏》（校对版全本）.txt",
    },
    {
        "work_id": "t4_urban_holdout",
        "topic_group": "urban",
        "topic_label": "都市日常",
        "title": "俗人回档",
        "filename": "《俗人回档》（校对版全本）.txt",
    },
    {
        "work_id": "t4_hist_holdout",
        "topic_group": "hist",
        "topic_label": "历史/朝堂",
        "title": "临安不夜侯",
        "filename": "《临安不夜侯》（校对版）.txt",
    },
]


def _norm_title(s: str) -> str:
    return re.sub(r"[\s《》（）()·\-—_]", "", s or "")


def _char_bigrams(s: str) -> set[str]:
    s = _norm_title(s)
    return {s[i : i + 2] for i in range(max(0, len(s) - 1))}


def check_unexposed(cand: dict) -> dict:
    """Check the candidate against the entire historical exposure universe:
    exact title match, normalized title match, and near-duplicate title similarity."""
    cand_norm = _norm_title(cand["title"])
    cand_grams = _char_bigrams(cand["title"])
    exact_hits, near_hits = [], []
    for h in HISTORICAL:
        h_norm = _norm_title(h["title"])
        if h_norm == cand_norm:
            exact_hits.append(h)
            continue
        h_grams = _char_bigrams(h["title"])
        if not h_grams or not cand_grams:
            continue
        sim = len(h_grams & cand_grams) / min(len(h_grams), len(cand_grams))
        if sim >= 0.5:
            near_hits.append({**h, "title_similarity": round(sim, 4)})
    return {
        "exact_title_match": exact_hits,
        "near_duplicate_title_match": sorted(near_hits, key=lambda x: -x["title_similarity"]),
        "unexposed": not exact_hits and not near_hits,
    }


def source_fingerprint(path: Path) -> dict:
    raw = path.read_bytes()
    text = None
    enc = None
    for e in ("utf-8", "gb18030", "utf-16"):
        try:
            text = raw.decode(e)
            enc = e
            break
        except Exception:
            continue
    return {
        "filename": path.name,
        "bytes": len(raw),
        "encoding": enc,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "decoded_chars": len(text or ""),
    }


def build() -> dict:
    checks = []
    for cand in CANDIDATES:
        src = CORPUS_DIR / cand["filename"]
        if not src.is_file():
            checks.append({**cand, "error": "source_file_missing", "unexposed": None})
            continue
        fp = source_fingerprint(src)
        blocks_dir = TRIAL_ROOT / cand["work_id"] / "blocks"
        blocks = []
        for ch in range(1, 13):
            bp = blocks_dir / f"{ch:04d}.txt"
            btext = bp.read_text(encoding="utf-8")
            blocks.append(
                {
                    "chapter": ch,
                    "file": f"{cand['work_id']}/blocks/{ch:04d}.txt",
                    "sha256": hashlib.sha256(btext.encode("utf-8")).hexdigest(),
                    "chars": len(btext),
                }
            )
        checks.append(
            {
                **cand,
                "source": fp,
                "frozen_blocks": blocks,
                **check_unexposed(cand),
            }
        )

    all_unexposed = all(c.get("unexposed") is True for c in checks)
    groups = sorted({c["topic_group"] for c in checks})

    declaration = {
        "schema": "t4_frozen_trial_declaration_v1",
        "frozen_at_utc": "2026-11-06T06:20:00+08:00",
        "purpose": "首次在从未参与调试的作品上验证 B（伏笔登记/回收）与 C（按需复审）的端到端效果与成本",
        "candidate_version": {
            "gate_code_snapshot_id": compute_gate_code_snapshot(),
            "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
            "scorer_version": "bc_scorer_t2_v1",
            "claim_contract": "claims_v2",
            "shared_gate_module": "backend/common/c_claim_audit_gate.py",
            "scorer_module": "backend/common/bc_scorer_and_labels.py",
            "provenance_module": "backend/common/provenance_cache.py",
        },
        "baseline_arm": {
            "name": "baseline",
            "description": "生产现状：四个开关全关。复检初次 SUPPORTED 直接落账，不触发 C 类升级复审；B 证据增强与目录保留均未启用",
            "switches": {
                "enable_c_on_demand_audit": False,
                "fix_b2_evidence_and_recheck": False,
                "fix_b4_flow_and_storage": False,
                "enable_b_trace": False,
            },
        },
        "b_fixed_arm": {
            "name": "b_fixed",
            "description": "中间臂：只开 B 修复（证据增强与目录保留），C 按需复审仍关。用于隔离 C 开关的净效应",
            "switches": {
                "enable_c_on_demand_audit": False,
                "fix_b2_evidence_and_recheck": True,
                "fix_b4_flow_and_storage": True,
                "enable_b_trace": True,
            },
        },
        "candidate_arm": {
            "name": "candidate",
            "description": "拟默认开启组合：复检初次 SUPPORTED 为暂定，必须经按需 claims_v2 复审才可落账",
            "switches": {
                "enable_c_on_demand_audit": True,
                "fix_b2_evidence_and_recheck": True,
                "fix_b4_flow_and_storage": True,
                "enable_b_trace": True,
            },
        },
        "arm_comparison_plan": {
            "B_improvement": "baseline → b_fixed（只变 B 开关），逐作品给登记与回答两个分母",
            "C_isolated_effect": "b_fixed → candidate（只变 C 开关）",
            "resource_and_adoption": "baseline → candidate（拟默认开启组合的真实增量）",
        },
        "frozen_scope": {
            "chapter_range": "第1-12章（连续章节试产）",
            "chapters_per_work": 12,
            "retrieval_range": "同作品全书文本（允许检索范围内），目录容量受生产默认 max_foreshadow_catalog_high/mid 限制",
            "prior_state_disclosure": (
                "本批作品以“第1章起、无前情账本”为起点试产；不含任何既往分析结果。"
                "因此结论仅限该 12 章区间内的伏笔登记与回收，不代表全书收益。"
            ),
            "same_layer_comparison": "两臂均通过真实生产链路（章节分析→调和→目录→复检→落账→保存恢复），在同一产物层比较",
        },
        "model_binding": {
            "main_channel_model": "codex/gpt-6-luna",
            "recheck_channel_model": "codex/gpt-6-luna",
            "recheck_channel_base_url": "http://127.0.0.1:3425/v1",
            "reasoning_effort": "low",
            "temperature": 0.1,
            "json_mode": "default",
            "streaming_enabled": True,
            "note": (
                "两臂共用同一模型与同一参数。选型原因：网关侧 zcode/GLM-5.3-Flash 与 "
                "wb2api/cn:hy3 在试产前探测均返回 502 exceed quota limit / 405 拦截，"
                "codex/gpt-6-luna 实测可用且与复检通道同源（recheck_conflict_reason 为空）。"
                "模型在试产开始前冻结，试产期间不再更换。"
            ),
        },
        "budget": {
            "logical_call_limit": 300,
            "request_attempt_limit": 400,
            "max_concurrency": 2,
            "includes": "模型辅助标注、审核、重试与测试调用",
            "context_limit_per_call": "章节分析 ≤ 32k tokens 输入；复检批次 ≤ 24k tokens 输入；claims_v2 复审 ≤ 24k tokens 输入",
            "output_limit_per_call": "章节分析 ≤ 4096 tokens；复检/复审 ≤ 4096 tokens",
            "realised_scope": "4 部作品 × 12 章 = 48 章；三臂（baseline/b_fixed/candidate）；两臂两两对比定位 B 与 C 各自的净效应",
        },
        "acceptance_thresholds": {
            "B_improvement": "预先选定指标相对遗漏数减少 ≥20%；≥3/4 作品不退化，≥2 部改善",
            "C_false_accept": "同一配对集上误接受不增加；同时列出误接受数量与负例分母",
            "correct_answer_retention": "相对基线下降 ≤2 个百分点；存疑不计保留",
            "decision_coverage": "同时报告接受/拒绝/存疑/格式失败/未触发误接受",
            "resource": "同范围总 token 增幅 ≤15%；端到端 P95 耗时增幅 ≤20%",
            "engineering": "关键反例、生产入口集成、保存恢复与全量后端/前端测试通过",
        },
        "works": checks,
        "topic_groups": groups,
        "all_candidates_unexposed": all_unexposed,
        "historical_exposure_universe_size": len(HISTORICAL),
        "exposure_event": {
            "event": "t4_trial_corpus_first_read",
            "recorded_at": "2026-11-06T06:05:00+08:00",
            "note": (
                "本批 4 部作品正文已被试产准备脚本读取（切分并落盘为 blocks/），"
                "故自本记录起按“已曝光但从未参与调试/调参”状态管理：可用于最终保留验收，"
                "但若用于调参则立即失去保留集身份。"
            ),
        },
        "exposure_disclosure": {
            "primary_trial_scope": {
                "chapters": "第1-12章",
                "exposure_state": "已曝光（切分与独立参考观察标注阶段读取过正文）",
                "used_for": "12 章主试产的三臂对比与独立验收",
                "independence_caveat": (
                    "参考观察由独立标注代理在仅见原文的条件下产出，"
                    "全程未读取任何管线产物；但作品正文已被标注者读取，"
                    "因此该批数据不能声称“从未被人读过”，只能声称“从未参与调试/调参”。"
                ),
            },
            "extended_scope": {
                "chapters": "第1-24章",
                "exposure_state": "已曝光（同一批作品延长章节范围）",
                "used_for": "扩充 C 类真实管线候选数量",
                "independence_caveat": (
                    "扩展范围与主试产同批作品，**不可**用于独立泛化证明；"
                    "其结果只作开发证据与候选池扩充，不进入独立验收结论。"
                ),
            },
            "p95_sampling_note": (
                "每臂样本量仅 3-4 次完整运行，P95 估计不稳定，"
                "按任务书要求标为证据不足，不用单次请求 P95 顶替。"
            ),
        },
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "t4-frozen-trial-declaration.json").write_text(
        json.dumps(declaration, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return declaration


if __name__ == "__main__":
    d = build()
    print(
        json.dumps(
            {
                "all_unexposed": d["all_candidates_unexposed"],
                "historical_universe_size": d["historical_exposure_universe_size"],
                "topic_groups": d["topic_groups"],
                "works": [
                    {
                        "work_id": w["work_id"],
                        "title": w["title"],
                        "group": w["topic_group"],
                        "unexposed": w["unexposed"],
                        "exact_matches": len(w["exact_title_match"]),
                        "near_duplicates": len(w["near_duplicate_title_match"]),
                        "source_sha256": w["source"]["source_sha256"][:16],
                        "encoding": w["source"]["encoding"],
                    }
                    for w in d["works"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
