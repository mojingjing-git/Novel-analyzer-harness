"""
分析引擎（asyncio 版）
协调LLM调用、结果解析和知识库更新的核心逻辑
（analyze_chapter 是 async，内部 await llm_client.chat_auto()，
按 self.config.api.streaming_enabled 自动选流式/非流式；
流式模式下 on_progress 回调 broadcast token_delta 给前端）
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field
import logging
import math
import re
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..common.error_types import ErrorScope, ErrorVerdict
from ..config.settings import AppConfig
from ..models.analysis_result import AnalysisResult
from ..models.knowledge import KnowledgeBase
from .anchor import locate_anchor, mark_leads, verify_result_anchors
from .llm_client import LLMClient
from .prompt_limits import PromptLimits
from .stream_progress import make_stream_progress_callback
from .prompt_builder import PromptBuilder
from ..progress_hub import get_hub
from ..utils.json_utils import (
    extract_json_from_text,
    parse_json_robust,
    safe_parse_json,
    salvage_json_array_objects,
)

logger = logging.getLogger(__name__)


def _usage_status_str(usage_status) -> str:
    """把用量状态归一成上报用的字符串。

    retry_info 与 WS 载荷只认 "KNOWN"/"UNKNOWN"：
    - "KNOWN"   拿到了 usage，tokens 里的数字是真实测量；
    - "UNKNOWN" 没拿到 usage，tokens 里的 (0, 0) 只是记账哨兵，**不得**当"零消耗"。
    缺省（None/空）按 UNKNOWN 处理——宁可标注未知，也不宣称零。
    """
    raw = getattr(usage_status, "value", usage_status)
    return str(raw or "UNKNOWN").upper()


def validate_parsed_analysis(data: dict) -> tuple:
    """对已解析的 JSON dict 做必须字段/类型校验。

    真值字符串（如 long_context_insights="本章无洞察"）也必须被拦住：
    它们绕过键存在性检查，在 AnalysisResult.from_dict 的 `or {}` 之后 .get()
    崩溃 → 该块被判解析失败，烧完 3 轮补跑预算后永久丢失。"""
    required_fields = ["core_events", "cross_block", "long_context_insights"]
    missing = [k for k in required_fields if k not in data]
    if missing:
        return False, f"JSON缺少必须字段: {missing}"
    cb = data.get("cross_block", {})
    if not isinstance(cb, dict) or not cb.get("summary"):
        return False, "cross_block.summary 为空或类型错误"
    ce = data.get("core_events", [])
    if not isinstance(ce, list):
        return False, f"core_events 应为列表，实际为 {type(ce).__name__}"
    # 嵌套对象字段必须是 dict（缺失键保持既有宽松语义，from_dict 有默认值兜底）
    for key in ("long_context_insights", "updated_knowledge"):
        v = data.get(key)
        if v is not None and not isinstance(v, dict):
            return False, f"{key} 应为对象，实际为 {type(v).__name__}"
    return True, ""


def validate_chapter_slot(slot: Any) -> Tuple[bool, str]:
    """对批量输出中单章槽位做字段与类型校验。"""
    if not isinstance(slot, dict):
        return False, "章节槽位应为对象"
    ce = slot.get("core_events")
    if not isinstance(ce, list):
        return False, "core_events 缺失或非列表"
    cb = slot.get("cross_block")
    if not isinstance(cb, dict) or not str(cb.get("summary") or "").strip():
        return False, "cross_block.summary 为空或类型错误"
    for key in ("updated_knowledge", "long_context_insights"):
        v = slot.get(key)
        if v is not None and not isinstance(v, dict):
            return False, f"{key} 应为对象，实际为 {type(v).__name__}"
    for list_key in ("character_arcs", "foreshadowing", "locations", "spatial_relationships", "plot_holes"):
        lv = slot.get(list_key)
        if lv is not None and not isinstance(lv, list):
            return False, f"{list_key} 应为列表，实际为 {type(lv).__name__}"
    return True, ""


def _parse_int_chapter(raw: Any) -> Optional[int]:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float) and raw.is_integer() and raw > 0:
        return int(raw)
    if isinstance(raw, str):
        m = re.search(r"(\d+)", raw.strip())
        if m:
            val = int(m.group(1))
            return val if val > 0 else None
    return None


def _slot_richness(slot: Mapping[str, Any]) -> int:
    cb = slot.get("cross_block")
    summary_len = len(str(cb.get("summary") or "")) if isinstance(cb, Mapping) else 0
    return (
        len(slot.get("core_events") or []) * 3
        + len(slot.get("foreshadowing") or []) * 3
        + len(slot.get("character_arcs") or []) * 2
        + summary_len
    )


def _anchor_confirmed_for_chapter(slot: Mapping[str, Any], text: str, min_anchor_chars: int) -> bool:
    if not text:
        return False
    fs_items = [f for f in (slot.get("foreshadowing") or []) if isinstance(f, Mapping)]
    quotes = [str(f.get("anchor_quote") or "").strip() for f in fs_items if str(f.get("anchor_quote") or "").strip()]
    for q in quotes:
        if locate_anchor(text, q, min_chars=min_anchor_chars) is not None:
            return True
    if not quotes:
        cb = slot.get("cross_block")
        summary = str(cb.get("summary") or "") if isinstance(cb, Mapping) else ""
        tokens = [t for t in re.findall(r"[\u4e00-\u9fff]{3,6}", summary) if t in text]
        return len(tokens) >= 1
    return False


def parse_batch_response(
    content: str,
    expected_chapters: Sequence[int],
    chapter_texts: Mapping[int, str],
    *,
    truncated: bool = False,
    anchor_enabled: bool = True,
    min_anchor_chars: int = 6,
) -> Tuple[Dict[int, AnalysisResult], List[int], bool, int]:
    """解析逐章批量 JSON 响应并按单章原文就地完成锚点校验与线索标记。

    Args:
        truncated: 模型因 ``length`` / ``max_tokens`` 截断返回时必须为 ``True``；
            截断态严禁对整包调用 ``json_repair``，仅抢救括号深度已完整闭合的前段章节。

    Returns:
        ``(valid_results_by_ch, missing_chapters, used_salvage, id_realigned_count)``
    """
    expected_list = [int(c) for c in expected_chapters]
    expected_set = set(expected_list)
    if not (content or "").strip() or not expected_list:
        return {}, list(expected_list), False, 0

    used_salvage = False
    id_realigned = 0
    raw_slots: List[dict] = []
    batch_lci: Dict[str, Any] = {}

    if truncated:
        raw_slots = salvage_json_array_objects(content, "chapters")
        used_salvage = bool(raw_slots)
    else:
        json_str = extract_json_from_text(content)
        parsed_top: Any = None
        if json_str is not None:
            parsed_top, _ = parse_json_robust(json_str)

        if isinstance(parsed_top, dict) and isinstance(parsed_top.get("chapters"), list):
            raw_slots = [x for x in parsed_top["chapters"] if isinstance(x, dict)]
            lci_raw = parsed_top.get("long_context_insights")
            if isinstance(lci_raw, dict):
                batch_lci = lci_raw
        elif (
            len(expected_list) == 1
            and isinstance(parsed_top, dict)
            and validate_chapter_slot(parsed_top)[0]
        ):
            slot_copy = dict(parsed_top)
            if slot_copy.get("chapter") is None and slot_copy.get("chapter_number") is None:
                slot_copy["chapter"] = expected_list[0]
            raw_slots = [slot_copy]
            lci_raw = parsed_top.get("long_context_insights")
            if isinstance(lci_raw, dict):
                batch_lci = lci_raw

        if not raw_slots:
            salvaged = salvage_json_array_objects(content, "chapters")
            if salvaged:
                raw_slots = salvaged
                used_salvage = True

    by_ch: Dict[int, dict] = {}
    unmatched_slots: List[dict] = []

    for slot in raw_slots:
        ok, _why = validate_chapter_slot(slot)
        if not ok:
            continue
        raw_id = slot.get("chapter") if "chapter" in slot else slot.get("chapter_number")
        ch_id = _parse_int_chapter(raw_id)
        if ch_id is not None and ch_id in expected_set:
            if ch_id not in by_ch or _slot_richness(slot) > _slot_richness(by_ch[ch_id]):
                by_ch[ch_id] = slot
        else:
            unmatched_slots.append(slot)

    # 顺序对称重对齐：单章批唯 1 未匹配槽无跨章混淆直接归位；多章批须经原文锚点/关键短语确认后采纳
    if unmatched_slots:
        remaining_expected = [c for c in expected_list if c not in by_ch]
        if len(expected_list) == 1 and len(unmatched_slots) == 1 and remaining_expected == expected_list:
            by_ch[expected_list[0]] = unmatched_slots[0]
            id_realigned += 1
        elif len(unmatched_slots) == len(remaining_expected) and remaining_expected:
            for slot, cand_ch in zip(unmatched_slots, remaining_expected):
                txt = chapter_texts.get(cand_ch, "")
                if _anchor_confirmed_for_chapter(slot, txt, min_anchor_chars):
                    by_ch[cand_ch] = slot
                    id_realigned += 1

    valid_results: Dict[int, AnalysisResult] = {}
    max_success_ch = max(by_ch.keys()) if by_ch else None

    for ch in sorted(by_ch.keys()):
        slot_data = dict(by_ch[ch])
        slot_data["chapter_number"] = ch
        if (
            ch == max_success_ch
            and batch_lci
            and not isinstance(slot_data.get("long_context_insights"), dict)
        ):
            slot_data["long_context_insights"] = copy.deepcopy(batch_lci)
        try:
            res = AnalysisResult.from_dict(slot_data)
        except Exception as exc:
            logger.warning("构建第%s章 AnalysisResult 失败，已剔除该槽位: %s", ch, exc)
            continue
        res.chapter_number = ch
        res.block_size = 1
        res.source_chapters = [ch]
        res.raw_response = ""

        ch_text = chapter_texts.get(ch, "")
        try:
            if anchor_enabled:
                res.anchor_stats = verify_result_anchors(
                    res, ch_text, ch, min_anchor_chars, expected_range=(ch, ch)
                )
            else:
                n_fs = len(getattr(res, "foreshadowing", []) or [])
                res.anchor_stats = {
                    "total": n_fs, "located": 0, "missing": n_fs, "by_kind": {}, "disabled": True,
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("第%s章伏笔锚点校验异常，已跳过: %s", ch, exc)

        try:
            mark_leads(res.foreshadowing)
        except Exception as exc:
            logger.warning("第%s章线索标记异常，已跳过: %s", ch, exc)

        valid_results[ch] = res

    missing = [c for c in expected_list if c not in valid_results]
    return valid_results, missing, used_salvage, id_realigned


@dataclass
class BatchAnalysisResult:
    """一次逐章批量分析调用的结构化结果。"""

    valid_results: Dict[int, AnalysisResult] = field(default_factory=dict)
    missing_chapters: List[int] = field(default_factory=list)
    tokens: Tuple[int, int] = (0, 0)
    retry_info: Dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    used_salvage: bool = False
    id_realigned: int = 0
    account_tripped: bool = False
    account_trip_reason: str = ""
    stopped: bool = False


class NovelAnalyzer:
    """小说分析引擎"""

    def __init__(self, config: AppConfig):
        """
        初始化分析引擎

        Args:
            config: 应用配置
        """
        self.config = config
        self.llm_client = LLMClient(config.api)
        # 9 项上限统一从 PromptLimits 走（此前 4 项硬编码在 prompt_builder 构造默认，
        # 配置写了也不生效）。max_verified_facts_in_prompt 与知识库裁剪的
        # max_verified_facts 是两层：一个管提示词窗口，一个管库里留多少。
        limits = PromptLimits(
            max_arcs=config.analysis.max_arcs_in_prompt,
            max_summaries=config.analysis.max_summaries_in_prompt,
            max_character_states=config.analysis.max_character_states,
            max_world_items=config.analysis.max_world_items,
            max_foreshadow_entries=config.analysis.max_foreshadow_entries,
            max_relationships=config.analysis.max_relationships,
            max_verified_facts=config.analysis.max_verified_facts_in_prompt,
            max_themes=config.analysis.max_themes,
            max_content_chars=config.analysis.max_content_chars,
        )
        logger.info(f"Prompt 限额：{limits.describe()}")
        self.prompt_builder = PromptBuilder(
            max_arc_length=config.analysis.max_arc_length,
            timeline_truncate=config.analysis.timeline_truncate,
            rolling_max_momentum=getattr(config.analysis, "rolling_max_momentum", 15),
            **dataclasses.asdict(limits),
        )
        self._is_stopped = False

    def stop(self):
        """停止分析"""
        self._is_stopped = True
        # 联动 LLMClient：让进行中的 chat_with_retry 重试链在下一检查点退出
        self.llm_client.request_stop()
        logger.info("分析已请求停止")

    @property
    def breaker_tripped(self) -> bool:
        """底层 LLM 客户端账户级熔断器是否已跳闸。"""
        return bool(getattr(self.llm_client, "breaker_tripped", False))

    @property
    def breaker_reason(self) -> str:
        """底层 LLM 客户端账户级熔断原因。"""
        reason = getattr(self.llm_client, "breaker_reason", "")
        return reason if isinstance(reason, str) else ""

    def get_token_stats(self) -> dict:
        """获取Token使用统计"""
        return self.llm_client.get_stats()

    async def analyze_batch(
        self,
        chapter_items: Sequence[Tuple[int, str]],
        knowledge_base: KnowledgeBase,
        book_id: str = "",
    ) -> BatchAnalysisResult:
        """按逐章独立槽位协议分析一批章节（1..N 章共用同一引擎逻辑）。"""
        expected_chapters = [int(ch) for ch, _ in chapter_items]
        if not expected_chapters:
            return BatchAnalysisResult(
                valid_results={},
                missing_chapters=[],
                tokens=(0, 0),
                retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN"},
            )
        if self._is_stopped:
            logger.info("分析已被停止")
            return BatchAnalysisResult(
                valid_results={},
                missing_chapters=list(expected_chapters),
                tokens=(0, 0),
                retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "KNOWN", "error": "用户请求停止"},
                stopped=True,
            )

        chapter_texts = {int(ch): (text or "") for ch, text in chapter_items}
        lead_ch = expected_chapters[0]
        range_label = (
            f"ch{expected_chapters[0]}-ch{expected_chapters[-1]}"
            if len(expected_chapters) > 1
            else f"ch{lead_ch}"
        )
        a_cfg = getattr(self.config, "analysis", None)
        anchor_enabled = bool(getattr(a_cfg, "anchor_quote_enabled", True))
        min_chars = int(getattr(a_cfg, "anchor_min_chars", 6) or 6)
        accept_threshold = (
            1 if len(expected_chapters) == 1 else math.ceil(len(expected_chapters) * 0.5)
        )

        best_lock = threading.Lock()
        best_state: Dict[str, Any] = {
            "valid": {},
            "missing": list(expected_chapters),
            "salvage": False,
            "realigned": 0,
        }

        try:
            messages = self.prompt_builder.build_batch_messages(chapter_items, knowledge_base)
            logger.info("开始批量分析 %s（共 %d 章）...", range_label, len(expected_chapters))

            def validate_json_response(response: str) -> Tuple[bool, str]:
                valid_map, missing_chs, used_salvage, id_realigned = parse_batch_response(
                    response,
                    expected_chapters,
                    chapter_texts,
                    truncated=False,
                    anchor_enabled=anchor_enabled,
                    min_anchor_chars=min_chars,
                )
                with best_lock:
                    if len(valid_map) > len(best_state["valid"]):
                        best_state["valid"] = valid_map
                        best_state["missing"] = missing_chs
                        best_state["salvage"] = used_salvage
                        best_state["realigned"] = id_realigned
                if len(valid_map) >= accept_threshold:
                    return True, ""
                return (
                    False,
                    f"仅成功解析 {len(valid_map)}/{len(expected_chapters)} 章（缺失 ID: {missing_chs}）",
                )

            def build_retry_messages(validation_error: str, current_messages: list) -> list:
                hint = (
                    f"\n\n[系统纠偏] 上次返回校验未通过（{validation_error}）。"
                    f"请严格输出包含 `{{\"chapters\": [...], \"long_context_insights\": {{...}}}}` 的纯 JSON 对象，"
                    f"为物理 ID 列表 {expected_chapters} 中的每一章产出独立对象并确保 `chapter` 字段填写对应整数 ID。"
                )
                retry_msgs = [dict(m) for m in current_messages]
                for msg in reversed(retry_msgs):
                    if msg.get("role") == "user":
                        msg["content"] = str(msg.get("content") or "") + hint
                        break
                return retry_msgs

            hub = get_hub()
            on_progress = make_stream_progress_callback(
                hub=hub,
                context="analysis",
                session_id=book_id or "unknown",
                unit_idx=lead_ch,
            )

            llm_result = await self.llm_client.chat_auto(
                messages,
                max_tokens=self.config.api.max_tokens,
                validate_response=validate_json_response,
                retry_messages_builder=build_retry_messages,
                on_progress=on_progress,
                stop_on_truncation=True,
                stop_on_timeout=(len(expected_chapters) > 1),
            )

            raw_fr = getattr(llm_result, "finish_reason", "")
            finish_reason = raw_fr if isinstance(raw_fr, str) else ""
            is_truncated = finish_reason.strip().lower() in ("length", "max_tokens")

            retry_info: Dict[str, Any] = {
                "retries": max(0, (llm_result.call_stats or {}).get("attempts", 0) - 1),
                "failed_tokens": (llm_result.call_stats or {}).get("failed_tokens", 0),
                "usage_status": _usage_status_str(llm_result.usage_status),
                "cached_tokens": int(getattr(llm_result, "cached_tokens", 0) or 0),
                "finish_reason": finish_reason,
            }
            if llm_result.error:
                retry_info["error"] = llm_result.error
            if llm_result.verdict is not None:
                retry_info["verdict"] = llm_result.verdict.to_dict()

            actual_tokens = llm_result.tokens if llm_result.tokens is not None else (0, 0)

            with best_lock:
                final_valid = dict(best_state["valid"])
                final_missing = list(best_state["missing"])
                used_salvage = bool(best_state["salvage"])
                id_realigned = int(best_state["realigned"])

            if is_truncated:
                salv_valid, salv_missing, salv_used, salv_realigned = parse_batch_response(
                    llm_result.content or "",
                    expected_chapters,
                    chapter_texts,
                    truncated=True,
                    anchor_enabled=anchor_enabled,
                    min_anchor_chars=min_chars,
                )
                if len(salv_valid) > len(final_valid) or (salv_valid and len(salv_valid) == len(final_valid)):
                    final_valid = salv_valid
                    final_missing = salv_missing
                    used_salvage = salv_used
                    id_realigned = salv_realigned
            elif not final_valid and llm_result.success and (llm_result.content or "").strip():
                parsed_valid, parsed_missing, parsed_salv, parsed_real = parse_batch_response(
                    llm_result.content,
                    expected_chapters,
                    chapter_texts,
                    truncated=False,
                    anchor_enabled=anchor_enabled,
                    min_anchor_chars=min_chars,
                )
                if parsed_valid:
                    final_valid = parsed_valid
                    final_missing = parsed_missing
                    used_salvage = parsed_salv
                    id_realigned = parsed_real

            for res in final_valid.values():
                res.raw_response = ""

            err_text = str(retry_info.get("error") or "")
            tripped_by_breaker = getattr(self, "breaker_tripped", False) is True
            verdict_dict = retry_info.get("verdict")
            structured = ErrorVerdict.from_dict(verdict_dict) if isinstance(verdict_dict, dict) else None
            tripped_by_verdict = (
                structured is not None
                and structured.is_permanent
                and structured.scope == ErrorScope.ACCOUNT_GLOBAL
            )
            account_tripped = bool(tripped_by_breaker or tripped_by_verdict or err_text.startswith("熔断："))
            account_trip_reason = ""
            if account_tripped:
                raw_reason = getattr(self, "breaker_reason", "")
                account_trip_reason = (
                    raw_reason.strip()
                    if isinstance(raw_reason, str) and raw_reason.strip()
                    else (err_text[:120] if err_text else "账户级永久错误（认证失败或余额不足）")
                )
            stopped = bool(self._is_stopped or err_text == "用户请求停止")

            if final_valid:
                logger.info(
                    "批量分析 %s 完成：成功 %d/%d 章%s%s",
                    range_label,
                    len(final_valid),
                    len(expected_chapters),
                    f"（缺失 {final_missing}）" if final_missing else "",
                    " [截断抢救]" if is_truncated else "",
                )
            elif not account_tripped and not stopped:
                if not retry_info.get("error"):
                    retry_info["error"] = (
                        "输出达到 max_tokens 上限（未抢救出完整章节）"
                        if is_truncated
                        else "JSON解析失败"
                    )
                logger.error("批量分析 %s 失败: %s", range_label, retry_info["error"])

            return BatchAnalysisResult(
                valid_results=final_valid,
                missing_chapters=final_missing,
                tokens=actual_tokens,
                retry_info=retry_info,
                truncated=is_truncated,
                used_salvage=used_salvage,
                id_realigned=id_realigned,
                account_tripped=account_tripped,
                account_trip_reason=account_trip_reason,
                stopped=stopped,
            )

        except Exception as e:
            logger.error("批量分析 %s 异常: %s", range_label, e, exc_info=True)
            with best_lock:
                final_valid = dict(best_state["valid"])
                final_missing = list(best_state["missing"])
            return BatchAnalysisResult(
                valid_results=final_valid,
                missing_chapters=final_missing,
                tokens=(0, 0),
                retry_info={"retries": 0, "failed_tokens": 0, "usage_status": "UNKNOWN", "error": str(e)},
                stopped=bool(self._is_stopped),
            )

    async def analyze_chapter(
        self,
        chapter_number: int,
        chapter_content: str,
        knowledge_base: KnowledgeBase,
        block_size: int = 1,
        book_id: str = "",  # 用于 token_delta 广播的 session_id
    ) -> Tuple[Optional[AnalysisResult], Tuple[int, int], dict]:
        """单章分析适配入口（内部委托 ``analyze_batch``）。"""
        batch_res = await self.analyze_batch(
            [(chapter_number, chapter_content)],
            knowledge_base,
            book_id=book_id,
        )
        result = batch_res.valid_results.get(chapter_number)
        return result, batch_res.tokens, batch_res.retry_info

    def _parse_response(self, response: str, chapter_number: int,
                         pre_parsed: Optional[dict] = None) -> Optional[AnalysisResult]:
        """
        解析LLM响应为AnalysisResult

        Args:
            response: LLM响应文本
            chapter_number: 章节号
            pre_parsed: 如果已经在 validate 阶段解析过，直接传入避免重复解析

        Returns:
            分析结果，失败返回None
        """
        if pre_parsed is not None:
            data = pre_parsed
        else:
            json_str = extract_json_from_text(response)
            if json_str is None:
                logger.error("无法从响应中提取JSON")
                logger.debug(f"原始响应前500字符: {response[:500]}")
                return None

            data = safe_parse_json(json_str)
            if data is None:
                logger.error("JSON解析失败")
                logger.debug(f"提取的JSON前500字符: {json_str[:500]}")
                return None

        try:
            # 转换为AnalysisResult
            result = AnalysisResult.from_dict(data)
            result.chapter_number = chapter_number
            return result

        except Exception as e:
            logger.error(f"构建AnalysisResult失败: {e}")
            return None
