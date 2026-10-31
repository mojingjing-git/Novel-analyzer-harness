"""
分析引擎（asyncio 版）
协调LLM调用、结果解析和知识库更新的核心逻辑
（自旧项目 core/analyzer.py 移植：analyze_chapter 改 async，
内部 await llm_client.chat_with_retry；H16 Phase 3 业务接入 2026-08-26：
改用 llm_client.chat_auto()，按 self.config.api.streaming_enabled 自动选流式/非流式；
流式模式下 on_progress 回调 broadcast token_delta 给前端）
"""

import logging
from typing import Optional, Tuple

from ..config.settings import AppConfig
from ..models.analysis_result import AnalysisResult
from ..models.knowledge import KnowledgeBase
from .llm_client import LLMClient
from .stream_progress import make_stream_progress_callback
from .prompt_builder import PromptBuilder
from ..progress_hub import get_hub
from ..utils.json_utils import extract_json_from_text, safe_parse_json

logger = logging.getLogger(__name__)


def _usage_status_str(usage_status) -> str:
    """把用量状态归一成上报用的字符串（U6 / T04）。

    retry_info 与 WS 载荷只认 "KNOWN"/"UNKNOWN"：
    - "KNOWN"   拿到了 usage，tokens 里的数字是真实测量；
    - "UNKNOWN" 没拿到 usage，tokens 里的 (0, 0) 只是记账哨兵，**不得**当"零消耗"。
    缺省（None/空）按 UNKNOWN 处理——宁可标注未知，也不宣称零。
    """
    raw = getattr(usage_status, "value", usage_status)
    return str(raw or "UNKNOWN").upper()


def validate_parsed_analysis(data: dict) -> tuple:
    """对已解析的 JSON dict 做必须字段/类型校验。

    P2 修复（2026-08-24）：真值字符串（如 long_context_insights="本章无洞察"）
    此前绕过键存在性检查，在 AnalysisResult.from_dict 的 `or {}` 之后 .get()
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
        self.prompt_builder = PromptBuilder(
            max_arc_length=config.analysis.max_arc_length,
            max_arcs=config.analysis.max_arcs_in_prompt,
            max_summaries=config.analysis.max_summaries_in_prompt,
            timeline_truncate=config.analysis.timeline_truncate,
            max_character_states=config.analysis.max_character_states,
            max_world_items=config.analysis.max_world_items,
            max_foreshadow_entries=config.analysis.max_foreshadow_entries,
        )
        self._is_stopped = False

    def stop(self):
        """停止分析"""
        self._is_stopped = True
        # 联动 LLMClient：让进行中的 chat_with_retry 重试链在下一检查点退出
        self.llm_client.request_stop()
        logger.info("分析已请求停止")

    def get_token_stats(self) -> dict:
        """获取Token使用统计"""
        return self.llm_client.get_stats()

    async def analyze_chapter(
        self,
        chapter_number: int,
        chapter_content: str,
        knowledge_base: KnowledgeBase,
        block_size: int = 1,
        book_id: str = "",  # H16 Phase 3：用于 token_delta 广播的 session_id
    ) -> Tuple[Optional[AnalysisResult], Tuple[int, int], dict]:
        """
        分析单个章节（并发安全版本）

        Args:
            chapter_number: 章节号
            chapter_content: 章节内容
            knowledge_base: 当前知识库（调用方传入，不在此方法内读写共享KB）
            block_size: 本块合并的章节数（写入 result 用于断点续跑时检测 block_size 变更）

        Returns:
            (分析结果, 本章消耗token数, retry_info)；
            retry_info = {retries, failed_tokens, usage_status, error?, verdict?}，
            usage_status == "UNKNOWN" 表示没拿到 usage，此时 token 数只是记账哨兵（U6）；
            失败返回 (None, 已消耗tokens, retry_info)
        """
        if self._is_stopped:
            logger.info("分析已被停止")
            # 未发起任何请求：这里的零消耗是真实测量，故 usage_status=KNOWN（U6）
            return None, (0, 0), {"retries": 0, "failed_tokens": 0,
                                  "usage_status": "KNOWN"}

        try:
            # 1. 使用传入的KB构建提示词
            messages = self.prompt_builder.build_messages(
                chapter_content, chapter_number, knowledge_base
            )

            logger.info(f"开始分析第{chapter_number}章...")

            # 定义JSON验证回调
            parsed_cache = [None]

            def validate_json_response(response: str) -> Tuple[bool, str]:
                """验证LLM响应是否能成功解析为JSON，并检查必须字段"""
                logger.debug(f"开始验证响应（长度={len(response)}字符）")
                json_str = extract_json_from_text(response)
                if json_str is None:
                    return False, "无法从响应中提取JSON"
                data = safe_parse_json(json_str)
                if data is None:
                    return False, "JSON解析失败"
                is_valid, check_err = validate_parsed_analysis(data)
                if not is_valid:
                    return False, check_err
                parsed_cache[0] = data
                logger.debug(f"响应验证通过（{len(data)}个顶层字段）")
                return True, ""

            # 定义重试时的格式修正提示构建器
            def build_retry_messages(validation_error: str, current_messages: list) -> list:
                """在user message末尾追加格式修正提示"""
                hint = f"\n\n[系统提示] 上次输出格式有误（{validation_error}），请严格按JSON格式输出，确保包含core_events、cross_block、long_context_insights等必须字段。"
                retry_msgs = [dict(m) for m in current_messages]
                for msg in reversed(retry_msgs):
                    if msg.get("role") == "user":
                        msg["content"] = msg["content"] + hint
                        break
                return retry_msgs

            # 2. 调用LLM（带重试和温度退火，包含JSON解析验证）
            # 重试归因改用单次调用统计：并发章节共享 LLMClient 时，
            # 客户端全局计数差分会把其它章节的尝试混入本块的 retry_info
            # H16 Phase 3 (2026-08-26)：改用 chat_auto() 按 streaming_enabled 自动选流式/非流式；
            # 流式模式下 on_progress 回调 broadcast token_delta 给前端。
            hub = get_hub()
            on_progress = make_stream_progress_callback(
                hub=hub,
                context="analysis",
                session_id=book_id or "unknown",
                unit_idx=chapter_number,
            )

            llm_result = await self.llm_client.chat_auto(
                messages,
                max_tokens=self.config.api.max_tokens,
                validate_response=validate_json_response,
                retry_messages_builder=build_retry_messages,
                on_progress=on_progress,
            )
            response = llm_result.content
            retry_info = {
                "retries": max(0, llm_result.call_stats.get("attempts", 0) - 1),
                "failed_tokens": llm_result.call_stats.get("failed_tokens", 0),
            }
            # U6（T04）：用量状态在此统一写入，成功 / 失败 / 解析失败三条出口同一口径。
            # 拿到 usage → "KNOWN"；没拿到 → "UNKNOWN"，此时下面的 (0, 0) 只是记账哨兵，
            # pipeline 会在对外载荷里带上本字段，不让下游把"未知"读成"零消耗"。
            retry_info["usage_status"] = _usage_status_str(llm_result.usage_status)

            if not llm_result.success:
                logger.error(f"第{chapter_number}章分析失败: {llm_result.error}")
                # 保留实际消耗的 tokens（API 已扣费，不能归零）；tokens 为 None 表示没拿到 usage
                actual_tokens = (llm_result.tokens if llm_result.tokens is not None
                                 else (0, 0))
                retry_info["error"] = llm_result.error  # 传播原始错误（含 [MODERATION] 标记，供 pipeline 判 skipped）
                # U4：把结构化判定透传给 pipeline——
                # pipeline 靠 verdict.is_permanent 识别"永久失败"块（补跑轮跳过，不重复烧钱）；
                # 文本兜底判定恒不永久，故只有结构化结论才能点亮该分支。
                # （usage_status 已在 retry_info 构造处统一写入，见上，U6）
                retry_info["verdict"] = (llm_result.verdict.to_dict()
                                        if llm_result.verdict is not None else None)
                return None, actual_tokens, retry_info

            in_tok, out_tok = (llm_result.tokens if llm_result.tokens is not None
                               else (0, 0))
            tokens = in_tok + out_tok
            logger.info(f"第{chapter_number}章API调用成功，使用{tokens} tokens (输入{in_tok}+输出{out_tok})")

            # 3. 解析响应（复用 validate 阶段的解析结果）
            result = self._parse_response(response, chapter_number, parsed_cache[0])
            if result is None:
                logger.error(f"第{chapter_number}章JSON解析失败，LLM响应前800字符:\n{response[:800]}")
                retry_info["error"] = "JSON解析失败"
                # U4：解析失败同属失败路径——verdict 为 None（LLM 调用本身成功），
                # usage_status 已在构造处统一透传（U6，tokens 是否可信由它决定）
                retry_info["verdict"] = (llm_result.verdict.to_dict()
                                        if llm_result.verdict is not None else None)
                return None, (in_tok, out_tok), retry_info

            result.raw_response = response
            result.block_size = block_size
            # 伏笔锚点就地校验：落盘前把"逐字引文"钉回原文坐标。
            # 定位不到就置空并计数——一条伏笔缺锚点不该让整章失败。
            try:
                from .anchor import verify_result_anchors

                a_cfg = getattr(self.config, "analysis", None)
                enabled = bool(getattr(a_cfg, "anchor_quote_enabled", True))
                min_chars = int(getattr(a_cfg, "anchor_min_chars", 6) or 6)
                first_chapter = chapter_number
                if enabled:
                    result.anchor_stats = verify_result_anchors(
                        result, chapter_content, first_chapter, min_chars
                    )
                else:
                    # 关掉锚点采集时必须**明确标出来**：这一章的伏笔没有证据，
                    # 复检阶段要按"无证据模式"对待，而不是假装证据只是没找到。
                    n = len(getattr(result, "foreshadowing", []) or [])
                    result.anchor_stats = {
                        "total": n, "located": 0, "missing": n, "by_kind": {}, "disabled": True,
                    }
                if result.anchor_stats["total"]:
                    logger.info(
                        "伏笔锚点: %s/%s 已定位（缺 %s）",
                        result.anchor_stats["located"],
                        result.anchor_stats["total"],
                        result.anchor_stats["missing"],
                    )
            except Exception as exc:  # noqa: BLE001 - 锚点校验失败不该影响分析
                logger.warning(f"伏笔锚点校验异常，已跳过: {exc}")

            # 4. 文件写入由 MemoryState.flush_to_disk() 统一处理
            logger.debug(f"第{chapter_number}章 result 已在内存中")

            logger.info(f"第{chapter_number}章分析完成")
            return result, (in_tok, out_tok), retry_info

        except Exception as e:
            logger.error(f"第{chapter_number}章分析异常: {e}", exc_info=True)
            # 尽量保留已消耗的 tokens；异常可能发生在已扣费之后，(0, 0) 只是记账哨兵，
            # 故 usage_status 必须标 UNKNOWN（U6：未知不得宣称为零）
            return None, (0, 0), {"retries": 0, "failed_tokens": 0,
                                  "usage_status": "UNKNOWN"}

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
