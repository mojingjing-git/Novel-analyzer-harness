"""最终总结纯解析、校验与通道解析函数（Layer 1，无包内横向依赖）。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

from backend.utils.json_utils import extract_json_from_text, safe_parse_json

logger = logging.getLogger(__name__)


def recheck_channel(config) -> tuple[str, str]:
    """复检实际使用的 ``(model, base_url)``：未单独配置就跟随主通道。

    明细归档必须记**复检自己**的通道，否则事后审计会把采集通道当成判定通道。
    """
    analysis = getattr(config, "analysis", None)
    api = getattr(config, "api", None)
    model = str(getattr(analysis, "recheck_model", "") or "").strip()
    base_url = str(getattr(analysis, "recheck_base_url", "") or "").strip()
    return (
        model or str(getattr(api, "model", "") or ""),
        base_url or str(getattr(api, "base_url", "") or ""),
    )


def recheck_conflict_reason(config) -> str:
    """判定模型与判定通道**同源**校验；同源返回空串。"""
    model, base_url = recheck_channel(config)
    if "minimaxi.com" in base_url and "/" in model:
        return (
            f"配置冲突：判定通道 {base_url} 是官方端点，"
            f"不接受判定模型 {model}，已阻断复检"
        )
    return ""


def recheck_overrides(config) -> dict:
    """复检通道 overrides：``recheck_*`` 字段非空才写键，空 = 跟随主配置。"""
    a = getattr(config, "analysis", None)
    overrides: dict = {}

    def put(key: str, value) -> None:
        text = str(value or "").strip()
        if text:
            overrides[key] = text

    put("model", getattr(a, "recheck_model", ""))
    put("base_url", getattr(a, "recheck_base_url", ""))
    put("api_key", getattr(a, "recheck_api_key", ""))
    put("provider", getattr(a, "recheck_provider", ""))
    put("json_mode", getattr(a, "recheck_json_mode", ""))
    timeout = int(getattr(a, "recheck_timeout", 0) or 0)
    if timeout > 0:
        overrides["timeout"] = timeout
    effort = str(getattr(a, "recheck_reasoning_effort", "") or "").strip()
    if effort:
        overrides["reasoning_effort"] = effort
        overrides["thinking_mode"] = {"reasoning_effort": effort}
    return overrides


def split_lifecycle(items: list[dict]) -> dict[str, list[dict]]:
    """按生命周期分组供报告呈现。"""
    groups: dict[str, list[dict]] = {
        "active": [],
        "pending": [],
        "resolved": [],
        "dormant": [],
        "frozen": [],
    }
    for item in items:
        status = item.get("status")
        needs_review = bool(item.get("needs_review"))
        if status == "active":
            (groups["pending" if needs_review else "active"]).append(item)
        elif status == "resolved":
            groups["resolved"].append(item)
        elif status == "dormant":
            (groups["frozen" if needs_review else "dormant"]).append(item)
    return groups


def _recheck_progress_message(summary: dict) -> str:
    """复检进度文案：兼容 ``run_recheck_v2`` 与 ``run_recheck_v3`` 的各阶段 emit 载荷。"""
    stage = summary.get("stage")
    stage_total = summary.get("stage_total")
    remaining = summary.get("remaining")
    unit = str(summary.get("unit") or ("批" if stage in ("u1_alias", "u1_route") else "条"))
    rem_suffix = (
        f"（剩余 {remaining} {unit}）"
        if isinstance(stage_total, int) and stage_total > 0 and isinstance(remaining, int)
        else ""
    )
    if stage == "build_candidates":
        return f"伏笔复检：构建证据 {summary.get('built', 0)}/{summary.get('total', 0)} 条{rem_suffix}"
    if stage == "u1_index":
        return f"伏笔复检 V3：构建全书双向路由索引（已调用 {summary.get('calls', 0)} 次）"
    if stage == "u1_split":
        return f"伏笔复检 V3：核心问题拆解（已调用 {summary.get('calls', 0)} 次）{rem_suffix}"
    if stage == "u1_alias":
        return f"伏笔复检 V3：构建别名词典（已调用 {summary.get('calls', 0)} 次）{rem_suffix}"
    if stage == "u1_route":
        return f"伏笔复检 V3：全书双向路由（已调用 {summary.get('calls', 0)} 次）{rem_suffix}"
    if stage == "u3_hop":
        return f"伏笔复检 V3：第 {summary.get('hop', 1)}/3 跳深挖核查（已调用 {summary.get('calls', 0)} 次）{rem_suffix}"
    if stage == "u4_audit":
        return f"伏笔复检 V3：四防对称外科终审（已调用 {summary.get('calls', 0)} 次）{rem_suffix}"
    if stage == "u5_repair":
        return f"伏笔复检 V3：引文精确修复（已调用 {summary.get('calls', 0)} 次）{rem_suffix}"
    checked = summary.get("checked")
    if checked is None:
        checked = (summary.get("coverage") or {}).get("checked", 0)
    return f"伏笔复检：已核对 {checked} 条{rem_suffix}"


# 常见的非书名文本（metadata 推断错误时会抓到这些）
# 供 backend/scripts/fix_metadata_titles.py 等外部脚本 import
_NON_TITLE_PREFIXES = (
    "内容简介", "作品简介", "小说简介", "简介", "作者", "目录", "楔子",
    "序章", "前言", "序言", "卷首语", "第一章", "第1章", "第 1 章",
    "正文", "章节",
)


def _clean_book_name(name: str) -> str:
    """剥离书名号 / 尾部括号备注"""
    name = name.strip()
    name = re.sub(r"\s*[（(].*?[）)]\s*$", "", name)
    name = re.sub(r"^[《【]", "", name)
    name = re.sub(r"[》】]$", "", name)
    return name.strip()


def _looks_valid_book_name(title: str) -> bool:
    """判断 title 是否像合法书名"""
    if not title or len(title) < 2 or len(title) > 60:
        return False
    if re.fullmatch(r"[\s\=\-\*\_\.\|┅┄━─]+", title):
        return False
    if title.rstrip().endswith(("：", ":")):
        return False
    t = title.strip()
    if any(t.startswith(p) or t == p for p in _NON_TITLE_PREFIXES):
        return False
    if any(p in t for p in ("。", "？", "！")):
        return False
    if "作者" in t or "著" in t:
        return False
    return True


def detect_book_name(output_dir: Path) -> Optional[str]:
    """
    检测书名（不带《》包裹，供报告模板使用）。
    1. 优先读取 blocks/metadata.json 中的 title 字段。
    2. 其次使用 blocks 所在目录名（书目目录名），去掉《》和括号备注。
    """
    blocks_dir = output_dir.parent / "blocks"
    if not blocks_dir.exists():
        return None

    meta_path = blocks_dir / "metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            title = _clean_book_name(meta.get("title", ""))
            if _looks_valid_book_name(title):
                return title
        except Exception:
            pass

    fallback = _clean_book_name(output_dir.parent.name)
    return fallback if _looks_valid_book_name(fallback) else None


def extract_key_fields(data: dict) -> Optional[dict]:
    """从 chapter_N_result.json 提取关键字段（用于分卷总结）"""
    if not isinstance(data, dict):
        return None
    cb = data.get('cross_block') or {}
    lci = data.get('long_context_insights') or {}
    uk = data.get('updated_knowledge') or {}
    if not isinstance(cb, dict):
        cb = {}
    if not isinstance(lci, dict):
        lci = {}
    if not isinstance(uk, dict):
        uk = {}

    summary = cb.get('summary', '')
    if not summary:
        return None

    core_events = data.get('core_events') or []
    if not isinstance(core_events, list):
        core_events = []
    events = [str(e.get('event', '')) for e in core_events
              if isinstance(e, dict) and e.get('event')]

    character_arcs = data.get('character_arcs') or []
    if not isinstance(character_arcs, list):
        character_arcs = []
    arcs = [f"{a.get('name', '')}→{a.get('change_delta', '')}"
            for a in character_arcs
            if isinstance(a, dict) and a.get('name')]

    foreshadows = data.get('foreshadowing') or []
    if not isinstance(foreshadows, list):
        foreshadows = []
    first_clue = []
    if foreshadows and isinstance(foreshadows[0], dict):
        clue = foreshadows[0].get('clue', '')
        if clue:
            first_clue = [clue]

    plot_holes = data.get('plot_holes') or []
    if not isinstance(plot_holes, list):
        plot_holes = []

    return {
        'ch': data.get('chapter_number', 0),
        'summary': summary,
        'link': cb.get('contextual_link', ''),
        'foreshadow': first_clue,
        'holes': plot_holes,
        'timeline': uk.get('timeline', ''),
        'events': events,
        'arcs': arcs,
        'pacing': lci.get('pacing', ''),
    }


def extract_foreshadow_json(response: str) -> Optional[dict]:
    """从LLM响应中提取伏笔 reconciliation JSON。"""
    if not response:
        return None

    data: Optional[dict] = safe_parse_json(response.strip())
    if not data:
        json_str = extract_json_from_text(response)
        if json_str:
            data = safe_parse_json(json_str)

    if not data:
        logger.warning(f"伏笔 reconciliation JSON 解析失败，响应前200字: {response[:200]}")
        return None

    if not isinstance(data, dict):
        logger.warning(f"伏笔 reconciliation 返回了非字典类型: {type(data).__name__}")
        return None

    if 'foreshadow_reconciliation' in data:
        recon = data.get('foreshadow_reconciliation', []) or []
        logger.info(f"伏笔 reconciliation 解析成功: {len(recon)} 条")
        return {
            'reconciliation': recon,
            'new_foreshadows': data.get('new_foreshadows', []) or []
        }

    if 'foreshadow_evidence' in data:
        evidence = data['foreshadow_evidence'] or {}
        reconciliation = []
        for item in evidence.get('resolved_foreshadows', []) or []:
            reconciliation.append({
                'foreshadow_id': item.get('id', ''),
                'is_resolved_in_this_batch': True,
                'resolved_chapter': item.get('resolved_chapter', 0),
                'resolution_summary': item.get('resolution', '')
            })
        return {
            'reconciliation': reconciliation,
            'new_foreshadows': evidence.get('new_foreshadows', []) or []
        }

    return None


def validate_reconciliation_json(content: str) -> Tuple[bool, str]:
    """校验第二次调用返回的是合法 reconciliation JSON"""
    result = extract_foreshadow_json(content)
    if result is None:
        return False, "无法解析为 foreshadow_reconciliation JSON"
    recon = result.get('reconciliation')
    if not isinstance(recon, list):
        return False, "foreshadow_reconciliation 必须是数组"
    new_fs = result.get('new_foreshadows')
    if new_fs:
        logger.warning(f"LLM 意外输出了 new_foreshadows（{len(new_fs)}条），按 prompt 指令应省略")
    return True, ""


def build_reconciliation_retry_messages(validation_error: str, messages: List[dict]) -> List[dict]:
    """解析失败时追加格式修正提示后重试"""
    hint = (
        f"\n\n格式错误: {validation_error}\n"
        "请重新输出，且只输出一个 JSON 对象（不要 Markdown、不要代码块标记）。"
        "结构必须包含数组字段 foreshadow_reconciliation。"
    )
    new_messages = list(messages)
    if new_messages and new_messages[-1].get('role') == 'user':
        new_messages[-1] = {**new_messages[-1], 'content': new_messages[-1]['content'] + hint}
    else:
        new_messages.append({"role": "user", "content": hint.strip()})
    return new_messages


def extract_analysis_text(response: str) -> str:
    """提取第一次调用的 Markdown 卷摘要（剥离末尾可能的 JSON）"""
    if not response:
        return ""

    for marker in ('```json', '```JSON', '``` Json'):
        pos = response.find(marker)
        if pos != -1:
            return response[:pos].rstrip()

    for key in ('"foreshadow_reconciliation"', '"foreshadow_evidence"'):
        pos = response.find(key)
        if pos > 0:
            brace_pos = response.rfind('{', 0, pos)
            if brace_pos > 0:
                return response[:brace_pos].rstrip()

    return response.strip()


_VOLUME_REFUSAL_MAX_CHARS = 1200
_VOLUME_REFUSAL_PATTERNS = (
    re.compile(r"你只提供了[^\n。]{0,40}(?:分析要求|字段说明|提示词|指令)"),
    re.compile(r"(?:没有|未|尚未)提供[^\n。]{0,35}(?:具体|完整)?(?:小说)?(?:章节正文|小说正文|章节内容|正文内容|分析数据)"),
    re.compile(r"无法(?:进行|完成|生成|开展)[^\n。]{0,20}(?:单章分析|分卷摘要|分卷分析|卷摘要)"),
    re.compile(r"请(?:先)?(?:补充|提供|给出|发送)[^\n。]{0,35}(?:以下任一种输入|单章完整正文|小说正文|章节正文|结构化摘要)"),
    re.compile(r"(?:缺少|缺失|未收到)[^\n。]{0,30}(?:小说正文|章节正文|分析输入)"),
)


def validate_volume_summary(content: str) -> Tuple[bool, str]:
    """校验第一次调用的分卷摘要非空，且非模型误判缺正文输出的拒答文本。"""
    clean = extract_analysis_text(content).strip()
    if not clean:
        return False, "卷摘要内容为空"
    if len(clean) < _VOLUME_REFUSAL_MAX_CHARS:
        for pat in _VOLUME_REFUSAL_PATTERNS:
            if pat.search(clean):
                return False, "检测到模型拒答或误判缺少正文输入"
    return True, ""


def build_volume_summary_retry_messages(validation_error: str, messages: List[dict]) -> List[dict]:
    """分卷摘要校验未通过时追加明确输入定位提示后重试。"""
    hint = (
        f"\n\n校验未通过：{validation_error}\n"
        "请直接基于上方用户消息中已提供的各章结构化分析 JSON 数据汇总撰写本卷 Markdown 摘要，"
        "无需等待小说原文，不得索要章节正文或输出拒答说明。"
    )
    new_messages = list(messages)
    if new_messages and new_messages[-1].get("role") == "user":
        new_messages[-1] = {**new_messages[-1], "content": new_messages[-1]["content"] + hint}
    else:
        new_messages.append({"role": "user", "content": hint.strip()})
    return new_messages
