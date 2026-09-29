"""
LLM客户端封装（asyncio 版）
提供带重试机制和温度退火的 OpenAI 兼容 API 调用
（自旧项目 core/llm_client.py 移植：OpenAI→AsyncOpenAI，time.sleep→asyncio.sleep，
 行为契约保持不变：stop 检查在循环顶部、温度退火 max(0, temp-attempt*step)、
 退避 min(2^(retry+1), 60)、认证失败即停、验证失败注入格式修正 hint、failed_tokens 累加）
"""

import json
import re
import asyncio
import inspect
import logging
import threading
from typing import AsyncIterator, Tuple, Optional, List, Callable, Awaitable
from openai import AsyncOpenAI, APITimeoutError, APIError, AuthenticationError
import anthropic
from anthropic import AsyncAnthropic

from ..config.settings import APIConfig
from ..core.moderation import (
    mark_moderation, is_moderation_code, is_moderation_message, is_moderation_error,
)
from .llm_failure_logger import _get_failure_logger, redact as _redact
from .llm_stream import (
    StreamChunk, StreamResult,
    parse_anthropic_response, parse_anthropic_stream_event, parse_openai_stream_event,
)

logger = logging.getLogger(__name__)


def detect_provider(config: APIConfig) -> str:
    """判定请求协议格式：显式 provider 优先，否则按特征自动检测。

    auto 检测规则（按序）：
    1. base_url 含 "anthropic" → anthropic
    2. api_key 以 "sk-ant-" 开头 → anthropic
    3. model 以 "claude-" 开头 → anthropic
    其余 → openai
    """
    if config.provider and config.provider != "auto":
        return "openai" if config.provider == "openai" else "anthropic"
    base = (config.base_url or "").lower()
    key = config.api_key or ""
    model = config.model or ""
    if "anthropic" in base:
        return "anthropic"
    if key.startswith("sk-ant-"):
        return "anthropic"
    if model.lower().startswith("claude-"):
        return "anthropic"
    return "openai"


# FailureLogger 已拆至 llm_failure_logger.py（2026-08-31）：
# 类 + 进程级单例 + _get_failure_logger 访问器随迁，本文件经导入使用。


def moderation_hit_from_exception(e) -> bool:
    """从 API 异常嗅探内容审核拦截信号（纯函数，供单测）。

    P1 修复（2026-08-24 审计）：OpenAI SDK 的 APIStatusError.body 是 Any 类型，
    聚合网关可能返回字符串/数组错误体；原实现在 except 块内对非 dict body 调
    .get("code") 抛 AttributeError 且无人捕获，导致整批章节零重试直接报废。"""
    e_code = getattr(e, 'code', None)
    body = getattr(e, 'body', None) or {}
    body_msg = ""
    body_code = None
    if isinstance(body, dict):
        body_code = body.get("code")
        err_inner = body.get("error") or {}
        body_msg = str(err_inner.get("message", "")) if isinstance(err_inner, dict) else str(body)
    return (is_moderation_code(e_code) or is_moderation_code(body_code)
            or is_moderation_message(body_msg) or is_moderation_message(str(e)))


class LLMClient:
    """OpenAI兼容API客户端（异步）"""

    def __init__(self, config: APIConfig):
        self.config = config
        self.provider = detect_provider(config)
        if self.provider == "anthropic":
            self.client = None
            self.anthropic = AsyncAnthropic(
                base_url=config.base_url or None,
                api_key=config.api_key if config.api_key else "empty",
            )
        else:
            self.anthropic = None
            self.client = AsyncOpenAI(
                base_url=config.base_url,
                api_key=config.api_key if config.api_key else "empty"
            )
        self._stats_lock = threading.Lock()
        self._request_count = 0
        self._attempts = 0
        self._total_tokens = 0
        self._failed_tokens = 0
        self._cached_tokens = 0
        self._stop_requested = False  # 外部可设置，让进行中的重试链尽快退出
        self._stop_event = asyncio.Event()  # 触发后立即取消进行中的 API 请求

    def request_stop(self):
        """请求停止：让正在进行的 chat_with_retry 重试链在下一检查点退出，
        并立即取消当前进行中的 API 请求（不等超时）"""
        self._stop_requested = True
        self._stop_event.set()

    @staticmethod
    def _strip_thinking(content: str) -> str:
        """移除思考链标签（兼容各模型：<think>, <思考>, <reasoning> 等）"""
        if not content:
            return content
        # 处理 <think>...</think> 及各种变体（大小写不敏感，支持嵌套空白）
        content = re.sub(r'<think[^>]*>.*?</think>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<思考>.*?</思考>', '', content, flags=re.DOTALL)
        content = re.sub(r'<reasoning>.*?</reasoning>', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<thought>.*?</thought>', '', content, flags=re.DOTALL | re.IGNORECASE)
        return content.strip()

    @staticmethod
    async def list_models(base_url: str, api_key: str, provider: str = "auto") -> List[str]:
        """获取API可用的模型列表（支持 openai/anthropic 两种格式）"""
        if provider == "anthropic" or (provider == "auto" and "anthropic" in (base_url or "").lower()):
            # Anthropic 格式：GET /models + x-api-key 头
            if not (base_url or "").strip():
                logger.error("获取模型列表失败: anthropic 端点 base_url 为空")
                return []
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30) as hclient:
                    resp = await hclient.get(
                        f"{base_url.rstrip('/')}/models",
                        headers={
                            "x-api-key": api_key or "",
                            "anthropic-version": "2023-06-01",
                        },
                    )
                    if resp.status_code != 200:
                        logger.error(f"获取模型列表失败: HTTP {resp.status_code}")
                        return []
                    data = resp.json()
                    ids = [str(m.get("id", "")) for m in data.get("data", []) if m.get("id")]
                    return sorted(ids)
            except Exception as e:
                logger.error(f"获取模型列表失败: {e}")
                return []
        try:
            client = AsyncOpenAI(base_url=base_url, api_key=api_key if api_key else "empty")
            result = await client.models.list()
            # 标准端点：返回分页对象，模型列表在 .data 里（每个元素有 .id）
            raw = getattr(result, "data", None)
            if raw is None:
                # 部分非标准端点直接返回 list
                raw = result
            ids: List[str] = []
            for m in raw:
                # 兼容 Model 对象、dict、或直接就是 id 字符串 / (字段,值) tuple 的情况
                if isinstance(m, str):
                    ids.append(m)
                elif isinstance(m, tuple) and len(m) == 2 and m[0] == "data":
                    # pydantic BaseModel 默认的 __iter__ 会把字段迭代成 (名, 值)，
                    # 说明 result 没被解析成标准分页对象，此时从 .data 取值已在上面处理，
                    # 这里跳过这种顶层 tuple，避免误用。
                    continue
                elif hasattr(m, "id"):
                    ids.append(str(m.id))
                elif isinstance(m, dict) and "id" in m:
                    ids.append(str(m["id"]))
            return sorted(ids)
        except Exception as e:
            logger.error(f"获取模型列表失败: {e}")
            return []

    def _build_anthropic_payload(self, messages: List[dict], temperature: float, max_tokens: int) -> dict:
        """Anthropic /v1/messages 载荷构造：system 提取为顶层参数，相邻同角色消息合并。"""
        system_parts: List[str] = []
        msgs: List[dict] = []
        for m in messages:
            role = str(m.get("role", "user"))
            content = m.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if role == "system":
                system_parts.append(content)
            elif role in ("user", "assistant"):
                msgs.append({"role": role, "content": content})
            else:
                msgs.append({"role": "user", "content": content})
        # Anthropic 要求 user/assistant 交替；合并相邻同角色消息
        merged: List[dict] = []
        for m in msgs:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] += "\n\n" + m["content"]
            else:
                merged.append(dict(m))
        payload: dict = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": merged,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        # 思考控制：Anthropic 认 thinking: {"type": "enabled"/"disabled", "budget_tokens": N}。
        # 只放行 thinking 键——残留的 enable_thinking 等 OpenAI 系字段会被 SDK 原样透传导致 API 400
        if self.config.thinking_mode:
            anthro_extra = {k: v for k, v in self.config.thinking_mode.items() if k == "thinking"}
            if anthro_extra:
                payload.update(anthro_extra)
        return payload


    async def chat(
        self,
        messages: List[dict],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None
    ) -> Tuple[bool, str, str, Tuple[int, int]]:
        if max_tokens is None:
            max_tokens = self.config.max_tokens

        # 结构化输出：仅对远程 OpenAI 兼容 API 生效（Anthropic 无 response_format，
        # prompt 已要求纯 JSON，跳过），本地模型(localhost)跳过
        extra_kwargs = {}
        if (self.provider != "anthropic" and self.config.json_mode != "default"
                and "localhost" not in self.config.base_url and "127.0.0.1" not in self.config.base_url):
            extra_kwargs["response_format"] = {"type": "json_object"}

        # 思考模式控制：OpenAI 兼容用 extra_body；Anthropic 在载荷构造时注入
        if self.config.thinking_mode and self.provider != "anthropic":
            extra_kwargs["extra_body"] = dict(self.config.thinking_mode)

        try:
            # 三分竞争：API 请求完成 / 用户请求停止 / 硬超时（任选其一先到）
            if self.provider == "anthropic":
                anthropic_client = self.anthropic
                assert anthropic_client is not None  # provider=anthropic 时 __init__ 已构造
                api_task = asyncio.create_task(
                    anthropic_client.messages.create(
                        **self._build_anthropic_payload(messages, temperature, max_tokens)
                    )
                )
            else:
                openai_client = self.client
                assert openai_client is not None  # provider=openai 时 __init__ 已构造
                api_task = asyncio.create_task(
                    openai_client.chat.completions.create(
                        model=self.config.model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=self.config.timeout,
                        **extra_kwargs
                    )
                )
            stop_wait = asyncio.create_task(self._stop_event.wait())
            hard_timeout = asyncio.create_task(asyncio.sleep(self.config.timeout + 30))  # 硬超时：比 SDK timeout 多 30s 余量

            try:
                done, _pending = await asyncio.wait(
                    {api_task, stop_wait, hard_timeout},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if api_task in done:
                    response = api_task.result()
                elif stop_wait in done:
                    # 用户请求停止：立即取消进行中的请求，不等超时
                    logger.info("用户请求停止，取消进行中的 API 请求")
                    return False, "", "用户请求停止", (0, 0)
                else:
                    # 硬超时：取消后走下方 asyncio.TimeoutError 分支
                    raise asyncio.TimeoutError
            finally:
                # 统一清理：正常返回/异常/外部 Task.cancel 都不泄漏子任务
                # （外部 cancel 时 asyncio.wait 抛 CancelledError，此处确保底层请求被真正取消）
                for t in (api_task, stop_wait, hard_timeout):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(api_task, stop_wait, hard_timeout, return_exceptions=True)

            with self._stats_lock:
                self._request_count += 1

            if self.provider == "anthropic":
                content, prompt_tokens, completion_tokens, cached_tokens = parse_anthropic_response(response)
                # total 口径对齐 OpenAI usage.total_tokens（OpenAI 的 total 含缓存命中部分）
                total_tokens = prompt_tokens + completion_tokens + cached_tokens
                if not content:
                    error_msg = "API返回空内容（可能触发内容过滤）"
                    # stop_reason=refusal（Anthropic 安全拒绝）或含审核关键词 → 按拦截处理
                    stop_reason = getattr(response, "stop_reason", None) or ""
                    if stop_reason == "refusal" or is_moderation_message(str(stop_reason)):
                        error_msg = mark_moderation(error_msg)
                        logger.warning("检测到内容审核拦截（空内容/refusal），将重试1次后跳过该章节")
                    logger.warning(error_msg)
                    return False, "", error_msg, (0, 0)
                with self._stats_lock:
                    self._total_tokens += total_tokens
                    self._cached_tokens += cached_tokens
                original_len = len(content)
                stripped = self._strip_thinking(content)
                if stripped:
                    content = stripped
                    if original_len > len(content):
                        logger.debug(f"已移除思考链: {original_len} -> {len(content)} 字符")
                cache_hint = f"，缓存命中{cached_tokens}" if cached_tokens else ""
                logger.info(f"API请求成功，使用{total_tokens} tokens (输入{prompt_tokens}+输出{completion_tokens}{cache_hint})")
                return True, content, "", (prompt_tokens, completion_tokens)

            prompt_tokens = 0
            completion_tokens = 0
            cached_tokens = 0
            if response.usage is not None:
                prompt_tokens = response.usage.prompt_tokens
                completion_tokens = response.usage.completion_tokens
                with self._stats_lock:
                    self._total_tokens += response.usage.total_tokens
                # 读取 KV cache 命中数（厂商扩展字段，无则 0）
                # OpenAI: usage.prompt_tokens_details.cached_tokens
                # DeepSeek: usage.prompt_cache_hit_tokens
                # 智谱/Qwen: 部分版本在 prompt_tokens_details
                ptd = getattr(response.usage, "prompt_tokens_details", None)
                if ptd is not None and getattr(ptd, "cached_tokens", None):
                    cached_tokens = ptd.cached_tokens
                cached_tokens = cached_tokens or getattr(response.usage, "prompt_cache_hit_tokens", 0) or 0
                with self._stats_lock:
                    self._cached_tokens += cached_tokens

            if not response.choices:
                error_msg = "API返回空choices列表（可能触发内容过滤）"
                # 空 choices 通常是审核过滤（content_filter），按拦截处理
                error_msg = mark_moderation(error_msg)
                logger.warning("检测到内容审核拦截（空choices），将重试1次后跳过该章节")
                return False, "", error_msg, (0, 0)

            message = response.choices[0].message
            content = message.content or ""
            # mimo-v2.5 等推理模型：思考过程在 reasoning_content，直接丢弃
            reasoning = getattr(message, 'reasoning_content', None)
            if reasoning:
                logger.debug(f"丢弃推理模型 reasoning_content（{len(reasoning)}字符）")

            # 移除思考链标签（本地模型如Qwen3/Gemma4默认输出思考过程）
            original_len = len(content) if content else 0
            stripped = self._strip_thinking(content)
            # 如果 strip 后变空但原始内容非空（思考链被截断），保留原始内容
            if stripped:
                content = stripped
                if original_len > len(content):
                    logger.debug(f"已移除思考链: {original_len} -> {len(content)} 字符")
            elif content and original_len > 0:
                logger.debug(f"思考链 strip 后为空，保留原始内容（{original_len}字符）")
            cache_hint = f"，缓存命中{cached_tokens}" if cached_tokens else ""
            logger.info(f"API请求成功，使用{response.usage.total_tokens if response.usage else 0} tokens (输入{prompt_tokens}+输出{completion_tokens}{cache_hint})")

            return True, content, "", (prompt_tokens, completion_tokens)

        except asyncio.TimeoutError:
            error_msg = f"请求硬超时 (timeout={self.config.timeout + 30}s): API 无响应"
            logger.warning(error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="HardTimeout",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return False, "", error_msg, (0, 0)

        except AuthenticationError as e:
            error_msg = f"认证失败，请检查API Key: {_redact(str(e))}"
            logger.error(error_msg)

            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="AuthenticationError",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return False, "", error_msg, (0, 0)

        except APITimeoutError as e:
            error_msg = f"请求超时 (timeout={self.config.timeout}s): {_redact(str(e))}"
            logger.warning(error_msg)

            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="TimeoutError",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return False, "", error_msg, (0, 0)

        except APIError as e:
            status_code = getattr(e, 'status_code', None)
            error_msg = f"API错误 (HTTP {status_code}): {_redact(str(e))}"

            # 内容审核拦截识别：结构化错误码 / 响应体 message / 异常文本
            if moderation_hit_from_exception(e):
                error_msg = mark_moderation(error_msg)
                logger.warning("检测到内容审核拦截，将重试1次后跳过该章节")

            if status_code == 429:
                # 提取 Retry-After（秒），供 chat_with_retry 做长退避（429 专属）
                retry_after = ""
                resp = getattr(e, 'response', None)
                if resp is not None and getattr(resp, 'headers', None):
                    ra = resp.headers.get('retry-after')
                    if ra:
                        retry_after = str(ra).strip()
                if retry_after:
                    error_msg += f" [Retry-After:{retry_after}]"
                logger.warning(f"触发速率限制(429)，尊重 Retry-After={retry_after or '无'} 长退避重试")

            logger.error(error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type=f"APIError_HTTP{status_code}",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return False, "", error_msg, (0, 0)

        except anthropic.AuthenticationError as e:
            error_msg = f"认证失败，请检查API Key: {_redact(str(e))}"
            logger.error(error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="AuthenticationError",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return False, "", error_msg, (0, 0)

        except anthropic.APITimeoutError as e:
            error_msg = f"请求超时 (timeout={self.config.timeout}s): {_redact(str(e))}"
            logger.warning(error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="TimeoutError",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return False, "", error_msg, (0, 0)

        except anthropic.APIStatusError as e:
            status_code = getattr(e, 'status_code', None)
            error_msg = f"API错误 (HTTP {status_code}): {_redact(str(e))}"
            # 内容审核拦截识别（Anthropic usage policy 类消息）
            body = getattr(e, 'response', None)
            body_text = ""
            if body is not None and hasattr(body, 'text'):
                body_text = body.text
            if is_moderation_message(body_text) or is_moderation_message(str(e)):
                error_msg = mark_moderation(error_msg)
                logger.warning("检测到内容审核拦截，将重试1次后跳过该章节")
            if status_code == 429:
                resp = getattr(e, 'response', None)
                if resp is not None and getattr(resp, 'headers', None):
                    ra = resp.headers.get('retry-after')
                    if ra:
                        error_msg += f" [Retry-After:{str(ra).strip()}]"
                logger.warning(f"触发速率限制(429)，尊重 Retry-After 长退避重试")
            logger.error(error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type=f"APIError_HTTP{status_code}",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return False, "", error_msg, (0, 0)

        except anthropic.APIConnectionError as e:
            error_msg = f"连接失败: {_redact(str(e))}"
            logger.error(error_msg)
            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type="ConnectionError",
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return False, "", error_msg, (0, 0)

        except Exception as e:
            error_msg = f"未知错误: {type(e).__name__}: {_redact(str(e))}"
            logger.error(error_msg, exc_info=True)

            # 内容审核拦截识别（智谱/小米等自由文本消息走这里）
            if is_moderation_message(error_msg):
                error_msg = mark_moderation(error_msg)
                logger.warning("检测到内容审核拦截，将重试1次后跳过该章节")

            _get_failure_logger().record_failure(
                attempt_num=0,
                max_retries=self.config.max_retries,
                temperature=temperature,
                error_type=type(e).__name__,
                error_message=error_msg,
                messages_length=len(str(messages))
            )
            return False, "", error_msg, (0, 0)

    async def chat_stream(
        self,
        messages: List[dict],
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
    ) -> "AsyncIterator[StreamChunk]":
        """流式 LLM 调用：每收到一个 chunk 立即 yield（不阻塞等完整响应）。

        与 chat() 区别：
        - chat() 等完整响应，10.5 分钟硬超时才能发现连接被切；chat_stream 边生成边吐
        - usage 字段：OpenAI 在最后一个 chunk（choices 为空），Anthropic 在 message_delta 事件
        - stop 语义：每 chunk 检查 self._stop_requested，true 时调 stream.close() 优雅退出
        - stream_options 降级：部分聚合网关不认 include_usage 会 400，降级为不带该参数

        调用方责任（chat_stream_with_retry 已封装）：
        - 累积 full_content / full_reasoning
        - 维护 accumulated_output_tokens
        - 调 self._strip_thinking 剥离 <think> 标签
        - 调 validate_response 验证（兼容同步/异步）
        """
        if max_tokens is None:
            max_tokens = self.config.max_tokens

        extra_kwargs = {}
        if (self.provider != "anthropic"
                and self.config.json_mode != "default"
                and "localhost" not in self.config.base_url
                and "127.0.0.1" not in self.config.base_url):
            extra_kwargs["response_format"] = {"type": "json_object"}

        if self.config.thinking_mode and self.provider != "anthropic":
            extra_kwargs["extra_body"] = dict(self.config.thinking_mode)

        try:
            if self.provider == "anthropic":
                # Anthropic 流式：async with + current_message_snapshot 收尾
                async with self.anthropic.messages.stream(
                    **self._build_anthropic_payload(messages, temperature, max_tokens)
                ) as stream:
                    async for event in stream:
                        if self._stop_requested:
                            await stream.close()
                            return
                        chunk = parse_anthropic_stream_event(event)
                        if chunk:
                            yield chunk
                    # 收尾：补一个 usage chunk
                    snap = stream.current_message_snapshot
                    if snap and getattr(snap, "usage", None):
                        u = snap.usage
                        yield StreamChunk(
                            type="usage",
                            usage_prompt_tokens=getattr(u, "input_tokens", 0) or 0,
                            usage_completion_tokens=getattr(u, "output_tokens", 0) or 0,
                            usage_cached_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                            is_final=True,
                        )
            else:
                # OpenAI 流式：先尝试带 stream_options，不支持则降级
                stream = None
                try:
                    stream = await self.client.chat.completions.create(
                        model=self.config.model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        timeout=self.config.timeout,
                        stream=True,
                        stream_options={"include_usage": True},
                        **extra_kwargs,
                    )
                except (TypeError, APIError) as e:
                    if "stream_options" in str(e):
                        logger.warning(f"该端点不支持 stream_options，降级不带该参数: {e}")
                        stream = await self.client.chat.completions.create(
                            model=self.config.model,
                            messages=messages,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            timeout=self.config.timeout,
                            stream=True,
                            **extra_kwargs,
                        )
                    else:
                        raise

                async for event in stream:
                    if self._stop_requested:
                        if hasattr(stream, "close"):
                            await stream.close()
                        return
                    chunk = parse_openai_stream_event(event)
                    if chunk:
                        yield chunk
        except asyncio.CancelledError:
            # 调用方主动 cancel
            raise
        except Exception as e:
            # 整个调用级别错误（连接失败、API 4xx/5xx 等）作为 error chunk yield
            error_msg = f"流式调用异常: {type(e).__name__}: {_redact(str(e))}"
            logger.warning(error_msg)
            yield StreamChunk(type="error", error=error_msg)
    @staticmethod
    async def _call_validate_response(validate_fn, content: str, timeout: float = 30.0):
        """兼容同步/异步 validate_response；30s 超时（与 chat_with_retry 风格一致）

        H16 (2026-08-26)：现有 validator 都是同步函数（llm_client.py:881 签名），
        老代码用 asyncio.to_thread 包裹（925/1059 行）。新流式版保持同步/异步双兼容，
        避免破坏现有 4 个 validator。
        """
        try:
            if inspect.iscoroutinefunction(validate_fn):
                return await asyncio.wait_for(validate_fn(content), timeout=timeout)
            return await asyncio.wait_for(
                asyncio.to_thread(validate_fn, content),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return False, "验证超时（30秒）"
        except Exception as e:
            return False, f"验证异常: {e}"

    async def chat_stream_with_retry(
        self,
        messages: List[dict],
        max_tokens: Optional[int] = None,
        validate_response: Optional[Callable] = None,
        retry_messages_builder: Optional[Callable] = None,
        on_progress: Optional[Callable[[StreamChunk], Awaitable[None]]] = None,
    ) -> StreamResult:
        """流式重试包装：完整对齐 chat_with_retry 行为契约
        （温度退火 → 指数退避；429 Retry-After；审核短路；认证即停；FailureLogger；统计累加）。

        H16 (2026-08-26) 关键设计：
        - 同步/异步 validator 双兼容（_call_validate_response 包裹）
        - stop 语义：每 attempt 顶部检查 _stop_requested；chunk 循环内 chat_stream 自身也检查
        - 累计值：on_progress 传 accumulated_output_tokens
        - 空响应按失败处理（moderation 嗅探）
        - partial content 保留：流式中断时 final_content 留作 9 级 JSON 容错链尝试
        """
        last_error = ""  # 关键：每次调用重置为 ""，不是 "未知错误"
        total_attempts = 0
        moderation_hits = 0
        call_failed_tokens = 0
        messages_length = len(str(messages))
        last_consumed_tokens: Tuple[int, int] = (0, 0)
        accumulated_output_tokens = 0  # P0-4 累计值
        final_content = ""  # 保留 partial content
        final_reasoning = ""
        last_usage: Optional[StreamChunk] = None

        logger.info("=" * 60)
        logger.info(f"开始流式API调用 - Model: {self.config.model}")
        logger.info(f"配置: temperature={self.config.temperature}, temperature_step={self.config.temperature_step}, "
                    f"temperature_max_retries={self.config.temperature_max_retries}, "
                    f"backoff_max_retries={self.config.backoff_max_retries}")
        if validate_response:
            logger.info(f"启用响应验证回调")
        logger.info("=" * 60)

        def _build_result(success, content, error, prompt_tok, comp_tok,
                          cached_tokens=0, reasoning_chars=0):
            return StreamResult(
                success=success, content=content, error=error,
                prompt_tokens=prompt_tok, completion_tokens=comp_tok,
                cached_tokens=cached_tokens, reasoning_chars=reasoning_chars,
                call_stats={"attempts": total_attempts, "failed_tokens": call_failed_tokens},
            )

        def _record_validation_failure(validation_error, temp):
            nonlocal last_error, call_failed_tokens
            last_error = f"响应验证失败: {validation_error}"
            if last_consumed_tokens[0] > 0 or last_consumed_tokens[1] > 0:
                with self._stats_lock:
                    self._failed_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
                call_failed_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
            _get_failure_logger().record_failure(
                attempt_num=total_attempts,
                max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                temperature=temp,
                error_type="ValidationFailed",
                error_message=last_error,
                wait_time=0,
                messages_length=messages_length,
            )

        async def _run_stream_attempt(temp):
            """单次流式 attempt：返回 (status, content, reasoning, usage_chunk, err)
            status ∈ {"ok", "error", "cancelled"}"""
            nonlocal accumulated_output_tokens, last_consumed_tokens
            nonlocal final_content, final_reasoning, last_usage
            attempt_content = ""
            attempt_reasoning = ""
            attempt_error = ""
            try:
                async for chunk in self.chat_stream(messages, temperature=temp, max_tokens=max_tokens):
                    if chunk.type == "content":
                        attempt_content += chunk.text
                        accumulated_output_tokens += len(chunk.text) // 4
                    elif chunk.type == "reasoning":
                        attempt_reasoning += chunk.reasoning_text
                    elif chunk.type == "usage":
                        last_usage = chunk
                        last_consumed_tokens = (chunk.usage_prompt_tokens, chunk.usage_completion_tokens)
                    elif chunk.type == "error":
                        attempt_error = chunk.error
                        break
                    # P0-4: on_progress 传累计值
                    if on_progress:
                        await on_progress(StreamChunk(
                            type="content",
                            estimated_total_tokens=accumulated_output_tokens,
                            text=chunk.text if chunk.type == "content" else "",
                        ))
            except asyncio.CancelledError:
                return ("cancelled", attempt_content, attempt_reasoning, last_usage, "用户请求停止")
            finally:
                # P1-a (2026-08-26)：同步 attempt_content → final_content
                # 失败路径（认证/审核/退避耗尽）依赖 final_content 作为残缺 JSON 兜底
                # 保留最新 attempt 的 partial（含 reasoning）以便重试失败后增量补全
                final_content = attempt_content
                final_reasoning = attempt_reasoning

            if attempt_error:
                return ("error", attempt_content, attempt_reasoning, last_usage, attempt_error)
            return ("ok", attempt_content, attempt_reasoning, last_usage, "")

        # === 温度退火重试层 ===
        for attempt in range(self.config.temperature_max_retries):
            if self._stop_requested:
                logger.info("⛔ 检测到停止请求，终止重试链")
                return _build_result(False, final_content, "用户请求停止",
                                     last_consumed_tokens[0], last_consumed_tokens[1])
            total_attempts += 1
            with self._stats_lock:
                self._attempts += 1

            temp = max(0.0, self.config.temperature - attempt * self.config.temperature_step)
            logger.info(f"\n[温度退火] 尝试 {total_attempts}，temperature={temp} "
                        f"(第{attempt + 1}/{self.config.temperature_max_retries}次)")

            status, content, reasoning, usage, err = await _run_stream_attempt(temp)

            if status == "cancelled":
                return _build_result(False, content, err, 0, 0, reasoning_chars=len(reasoning))

            if status == "error":
                last_error = err
                logger.warning(f"❌ 尝试失败: {err}")
                if last_consumed_tokens[0] > 0 or last_consumed_tokens[1] > 0:
                    with self._stats_lock:
                        self._failed_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
                    call_failed_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]

                if is_moderation_error(err):
                    if moderation_hits >= 2:
                        _get_failure_logger().record_failure(
                            attempt_num=total_attempts,
                            max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                            temperature=temp, error_type="ModerationBlocked",
                            error_message=err, messages_length=messages_length,
                        )
                        logger.warning("⛔ 内容审核拦截（连续3次），跳过该章节")
                        return _build_result(False, final_content, err,
                                             last_consumed_tokens[0], last_consumed_tokens[1])
                    moderation_hits += 1

                _get_failure_logger().record_failure(
                    attempt_num=total_attempts,
                    max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                    temperature=temp, error_type="RetryNeeded",
                    error_message=err, wait_time=0, messages_length=messages_length,
                )

                if "认证失败" in err or "401" in err:
                    logger.error("⛔ 认证错误，停止重试")
                    return _build_result(False, final_content, err,
                                         last_consumed_tokens[0], last_consumed_tokens[1])

                if attempt < self.config.temperature_max_retries - 1:
                    if self._is_429(err):
                        wait_time = min(self._retry_after_seconds(err) or 30 * (attempt + 1), 120)
                    else:
                        wait_time = 3 * (attempt + 1)
                    logger.info(f"⏳ 等待{wait_time}秒后进行下一次温度尝试...")
                    await self._sleep(wait_time)
                continue

            # status == "ok"：剥离思考链 + 累计成功 token + 验证
            stripped = self._strip_thinking(content)
            if stripped:
                content = stripped

            if last_consumed_tokens[0] > 0 or last_consumed_tokens[1] > 0:
                with self._stats_lock:
                    self._total_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
                    if usage:
                        self._cached_tokens += usage.usage_cached_tokens

            # 空响应检测（moderation 嗅探）— P0-小-2
            if not content.strip():
                error_msg = "API返回空内容（可能触发内容过滤）"
                if usage and (is_moderation_message(str(usage)) or is_moderation_error(error_msg)):
                    error_msg = mark_moderation(error_msg)
                logger.warning(error_msg)
                last_error = error_msg
                _get_failure_logger().record_failure(
                    attempt_num=total_attempts,
                    max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                    temperature=temp, error_type="EmptyResponse",
                    error_message=error_msg, messages_length=messages_length,
                )
                if attempt < self.config.temperature_max_retries - 1:
                    await self._sleep(3 * (attempt + 1))
                continue

            if validate_response:
                is_valid, validation_error = await self._call_validate_response(validate_response, content, timeout=30)
                if not is_valid:
                    logger.warning(f"❌ 响应验证失败: {validation_error}")
                    if retry_messages_builder:
                        messages = retry_messages_builder(validation_error, messages)
                    _record_validation_failure(validation_error, temp)
                    if attempt < self.config.temperature_max_retries - 1:
                        await self._sleep(3 * (attempt + 1))
                    continue

            # 成功
            logger.info(f"✅ API请求成功（第{total_attempts}次尝试）")
            logger.info(f"使用Tokens: {last_consumed_tokens}")
            logger.info("=" * 60)
            return _build_result(
                True, content, "",
                last_consumed_tokens[0], last_consumed_tokens[1],
                cached_tokens=usage.usage_cached_tokens if usage else 0,
                reasoning_chars=len(reasoning),
            )

        # === 指数退避重试层（429 时额外多 3 轮）===
        final_temp = max(0.0, self.config.temperature -
                         (self.config.temperature_max_retries - 1) * self.config.temperature_step)
        backoff_rounds = max(self.config.backoff_max_retries, 3) \
            if self._is_429(last_error) else self.config.backoff_max_retries

        for retry_num in range(backoff_rounds):
            if self._stop_requested:
                logger.info("⛔ 检测到停止请求，终止重试链")
                return _build_result(False, final_content, "用户请求停止",
                                     last_consumed_tokens[0], last_consumed_tokens[1])
            if is_moderation_error(last_error) and moderation_hits >= 2:
                _get_failure_logger().record_failure(
                    attempt_num=total_attempts,
                    max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                    temperature=final_temp, error_type="ModerationBlocked",
                    error_message=last_error, messages_length=messages_length,
                )
                logger.warning("⛔ 内容审核拦截（重试1次后仍拦截），跳过该章节")
                return _build_result(False, final_content, last_error,
                                     last_consumed_tokens[0], last_consumed_tokens[1])

            total_attempts += 1
            with self._stats_lock:
                self._attempts += 1

            if self._is_429(last_error):
                wait_time = min(self._retry_after_seconds(last_error) or 30 * (retry_num + 1), 120)
            else:
                wait_time = min(2 ** (retry_num + 1), 60)
            logger.info(f"\n[指数退避] 尝试 {total_attempts}，等待{wait_time}秒，"
                        f"temperature={final_temp} (第{retry_num + 1}/{backoff_rounds}次)")
            await self._sleep(wait_time)

            status, content, reasoning, usage, err = await _run_stream_attempt(final_temp)

            if status == "cancelled":
                return _build_result(False, content, err, 0, 0, reasoning_chars=len(reasoning))

            if status == "error":
                last_error = err
                logger.warning(f"❌ 重试失败: {err}")
                if last_consumed_tokens[0] > 0 or last_consumed_tokens[1] > 0:
                    with self._stats_lock:
                        self._failed_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
                    call_failed_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
                _get_failure_logger().record_failure(
                    attempt_num=total_attempts,
                    max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                    temperature=final_temp, error_type="ExponentialBackoff",
                    error_message=err, wait_time=wait_time, messages_length=messages_length,
                )
                if "认证失败" in err or "401" in err:
                    logger.error("⛔ 认证错误，停止重试")
                    return _build_result(False, final_content, err,
                                         last_consumed_tokens[0], last_consumed_tokens[1])
                continue

            # status == "ok"
            stripped = self._strip_thinking(content)
            if stripped:
                content = stripped

            if last_consumed_tokens[0] > 0 or last_consumed_tokens[1] > 0:
                with self._stats_lock:
                    self._total_tokens += last_consumed_tokens[0] + last_consumed_tokens[1]
                    if usage:
                        self._cached_tokens += usage.usage_cached_tokens

            if not content.strip():
                error_msg = "API返回空内容（可能触发内容过滤）"
                if usage and (is_moderation_message(str(usage)) or is_moderation_error(error_msg)):
                    error_msg = mark_moderation(error_msg)
                logger.warning(error_msg)
                last_error = error_msg
                continue

            if validate_response:
                is_valid, validation_error = await self._call_validate_response(validate_response, content, timeout=30)
                if not is_valid:
                    logger.warning(f"❌ 响应验证失败: {validation_error}")
                    if retry_messages_builder:
                        messages = retry_messages_builder(validation_error, messages)
                    _record_validation_failure(validation_error, final_temp)
                    continue

            # 成功
            logger.info(f"✅ API请求成功（第{total_attempts}次尝试）")
            logger.info(f"使用Tokens: {last_consumed_tokens}")
            logger.info("=" * 60)
            return _build_result(
                True, content, "",
                last_consumed_tokens[0], last_consumed_tokens[1],
                cached_tokens=usage.usage_cached_tokens if usage else 0,
                reasoning_chars=len(reasoning),
            )

        final_msg = (f"所有重试均失败（温度退火{self.config.temperature_max_retries}次 + "
                     f"指数退避{backoff_rounds}次 = 共{total_attempts}次）。最后错误: {last_error}")
        logger.error("\n" + "=" * 60)
        logger.error(f"⛔ API调用最终失败")
        logger.error(f"总尝试次数: {total_attempts}")
        logger.error(f"最后错误: {last_error}")
        summary = _get_failure_logger().get_summary()
        logger.error(f"失败统计: {summary}")
        logger.error("=" * 60)
        return _build_result(False, final_content, final_msg,
                             last_consumed_tokens[0], last_consumed_tokens[1])

    async def chat_auto(
        self,
        messages: List[dict],
        max_tokens: Optional[int] = None,
        validate_response: Optional[Callable] = None,
        retry_messages_builder: Optional[Callable] = None,
        on_progress: Optional[Callable[[StreamChunk], Awaitable[None]]] = None,
    ) -> Tuple[bool, str, str, Tuple[int, int], dict]:
        """根据 self.config.streaming_enabled 自动选 chat_with_retry / chat_stream_with_retry

        H16 Phase 3 (2026-08-26) 业务接入统一入口：
        - streaming_enabled=True（默认）→ chat_stream_with_retry（流式，每 chunk 回调 on_progress）
        - streaming_enabled=False → chat_with_retry（旧非流式 API，on_progress 忽略）

        返回值与 chat_with_retry 一致（兼容现有 4 个调用方）：
        (success, content, error, (prompt_tokens, completion_tokens), call_stats)
        """
        if self.config.streaming_enabled:
            result = await self.chat_stream_with_retry(
                messages, max_tokens=max_tokens,
                validate_response=validate_response,
                retry_messages_builder=retry_messages_builder,
                on_progress=on_progress,
            )
            return (
                result.success,
                result.content,
                result.error,
                (result.prompt_tokens, result.completion_tokens),
                result.call_stats,
            )
        # 非流式：on_progress 被忽略（保持接口一致）
        return await self.chat_with_retry(
            messages, max_tokens=max_tokens,
            validate_response=validate_response,
            retry_messages_builder=retry_messages_builder,
        )

    @staticmethod
    def _is_429(error: str) -> bool:
        """错误消息是否为 429 限流"""
        return "429" in (error or "")

    @staticmethod
    def _retry_after_seconds(error: str) -> Optional[float]:
        """从错误消息提取 Retry-After 秒数（chat() 已在 429 分支注入 [Retry-After:N]）"""
        m = re.search(r'Retry-After:(\d+)', error or "")
        if m:
            try:
                return max(1.0, float(m.group(1)))
            except ValueError:
                return None
        return None

    async def _sleep(self, seconds: float):
        """重试等待的薄封装（测试注入点，行为同 asyncio.sleep）"""
        await asyncio.sleep(seconds)

    async def chat_with_retry(
        self,
        messages: List[dict],
        max_tokens: Optional[int] = None,
        validate_response: Optional[Callable[[str], Tuple[bool, str]]] = None,
        retry_messages_builder: Optional[Callable[[str, List[dict]], List[dict]]] = None
    ) -> Tuple[bool, str, str, Tuple[int, int], dict]:
        last_error = ""
        total_attempts = 0
        moderation_hits = 0  # 内容审核拦截连续命中次数（连续3次才短路，见下方短路判断）
        # 本次调用的失败 token 累计（并发安全：不再依赖客户端全局计数做差分归因）
        call_failed_tokens = 0
        messages_length = len(str(messages))
        # 跟踪最后一次 API 成功调用消耗的 tokens（即使 JSON 验证失败，API 已扣费）
        last_consumed_tokens: Tuple[int, int] = (0, 0)

        logger.info("=" * 60)
        logger.info(f"开始API调用 - Model: {self.config.model}")
        logger.info(f"配置: temperature={self.config.temperature}, temperature_step={self.config.temperature_step}, "
                   f"temperature_max_retries={self.config.temperature_max_retries}, "
                   f"backoff_max_retries={self.config.backoff_max_retries}")
        if validate_response:
            logger.info(f"启用响应验证回调")
        logger.info("=" * 60)

        # 温度退火重试
        for attempt in range(self.config.temperature_max_retries):
            if self._stop_requested:
                logger.info("⛔ 检测到停止请求，终止重试链")
                return False, "", "用户请求停止", last_consumed_tokens, {"attempts": total_attempts, "failed_tokens": call_failed_tokens}
            total_attempts += 1
            with self._stats_lock:
                self._attempts += 1

            temp = max(0.0, self.config.temperature - attempt * self.config.temperature_step)

            logger.info(f"\n[温度退火] 尝试 {total_attempts}，"
                       f"temperature={temp} (第{attempt + 1}/{self.config.temperature_max_retries}次)")

            success, content, error, tokens = await self.chat(
                messages, temperature=temp, max_tokens=max_tokens
            )

            if success:
                if validate_response:
                    # 在线程中运行验证，避免 json5/ast.literal_eval 等同步解析器阻塞事件循环
                    try:
                        is_valid, validation_error = await asyncio.wait_for(
                            asyncio.to_thread(validate_response, content),
                            timeout=30
                        )
                    except asyncio.TimeoutError:
                        logger.warning("❌ 响应验证超时（30秒），视为验证失败")
                        is_valid, validation_error = False, "验证超时（30秒）"
                    except Exception as e:
                        logger.warning(f"❌ 响应验证异常: {e}")
                        is_valid, validation_error = False, f"验证异常: {e}"
                    if not is_valid:
                        logger.warning(f"❌ 响应验证失败: {validation_error}")
                        logger.info(f"[DEBUG] LLM响应(全文): {repr(content)}")
                        # 累加失败 tokens（API 已消耗，但响应无效）
                        if isinstance(tokens, tuple) and len(tokens) == 2:
                            with self._stats_lock:
                                self._failed_tokens += tokens[0] + tokens[1]
                            call_failed_tokens += tokens[0] + tokens[1]
                            last_consumed_tokens = tokens  # 保留以供最终返回
                        last_error = f"响应验证失败: {validation_error}"
                        # 构建带格式修正提示的messages用于重试
                        if retry_messages_builder:
                            messages = retry_messages_builder(validation_error, messages)
                        _get_failure_logger().record_failure(
                            attempt_num=total_attempts,
                            max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                            temperature=temp,
                            error_type="ValidationFailed",
                            error_message=last_error,
                            wait_time=0,
                            messages_length=messages_length
                        )
                        if attempt < self.config.temperature_max_retries - 1:
                            wait_time = 3 * (attempt + 1)
                            logger.info(f"⏳ 等待{wait_time}秒后进行下一次温度尝试...")
                            await self._sleep(wait_time)
                        continue

                logger.info(f"✅ API请求成功（第{total_attempts}次尝试）")
                logger.info(f"使用Tokens: {tokens}")
                logger.info("=" * 60)
                return True, content, "", tokens, {"attempts": total_attempts, "failed_tokens": call_failed_tokens}

            last_error = error
            logger.warning(f"❌ 尝试失败: {error}")

            # 内容审核拦截：连续 3 次命中才短路（P2 2026-08-24：宽松关键词会把
            # 瞬时网关 502 误判为拦截，2 次即弃太激进——多给一轮温度尝试；
            # 真·内容审核多付 1 次调用换取误判恢复，值得）
            if is_moderation_error(error):
                if moderation_hits >= 2:
                    _get_failure_logger().record_failure(
                        attempt_num=total_attempts,
                        max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                        temperature=temp,
                        error_type="ModerationBlocked",
                        error_message=error,
                        messages_length=messages_length,
                    )
                    logger.warning("⛔ 内容审核拦截（连续3次），跳过该章节，不再重试")
                    return False, "", error, last_consumed_tokens, {"attempts": total_attempts, "failed_tokens": call_failed_tokens}
                moderation_hits += 1

            # 即使 API 调用失败，如果有成功的 tokens（如超时但部分计费），保留
            if isinstance(tokens, tuple) and len(tokens) == 2 and (tokens[0] > 0 or tokens[1] > 0):
                last_consumed_tokens = tokens

            _get_failure_logger().record_failure(
                attempt_num=total_attempts,
                max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                temperature=temp,
                error_type="RetryNeeded",
                error_message=error,
                wait_time=0,
                messages_length=messages_length
            )

            if "认证失败" in error or "401" in error:
                logger.error("⛔ 认证错误，停止重试")
                return False, "", error, last_consumed_tokens, {"attempts": total_attempts, "failed_tokens": call_failed_tokens}

            if attempt < self.config.temperature_max_retries - 1:
                if self._is_429(error):
                    # 429：尊重 Retry-After，退避拉长（限流是暂时的，值得等）
                    wait_time = min(self._retry_after_seconds(error) or 30 * (attempt + 1), 120)
                else:
                    wait_time = 3 * (attempt + 1)
                logger.info(f"⏳ 等待{wait_time}秒后进行下一次温度尝试...")
                await self._sleep(wait_time)

        # 指数退避重试（429 限流时额外多试几轮：限流通常是暂时的，快速放弃会丢块）
        final_temp = max(0.0, self.config.temperature - (self.config.temperature_max_retries - 1) * self.config.temperature_step)
        backoff_rounds = max(self.config.backoff_max_retries, 3) if self._is_429(last_error) else self.config.backoff_max_retries
        for retry_num in range(backoff_rounds):
            if self._stop_requested:
                logger.info("⛔ 检测到停止请求，终止重试链")
                return False, "", "用户请求停止", last_consumed_tokens, {"attempts": total_attempts, "failed_tokens": call_failed_tokens}
            # 温度循环已连续 3 次命中审核拦截（含 temperature_max_retries<3 的配置）→ 直接放弃，
            # 不进入指数退避，避免对"永久拒绝"白等 30 分钟
            if is_moderation_error(last_error) and moderation_hits >= 2:
                _get_failure_logger().record_failure(
                    attempt_num=total_attempts,
                    max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                    temperature=final_temp,
                    error_type="ModerationBlocked",
                    error_message=last_error,
                    messages_length=messages_length,
                )
                logger.warning("⛔ 内容审核拦截（重试1次后仍拦截），跳过该章节，不再重试")
                return False, "", last_error, last_consumed_tokens, {"attempts": total_attempts, "failed_tokens": call_failed_tokens}
            total_attempts += 1
            with self._stats_lock:
                self._attempts += 1

            if self._is_429(last_error):
                wait_time = min(self._retry_after_seconds(last_error) or 30 * (retry_num + 1), 120)
            else:
                wait_time = min(2 ** (retry_num + 1), 60)

            logger.info(f"\n[指数退避] 尝试 {total_attempts}，"
                       f"等待{wait_time}秒，temperature={final_temp} "
                       f"(第{retry_num + 1}/{backoff_rounds}次)")
            await self._sleep(wait_time)

            success, content, error, tokens = await self.chat(
                messages,
                temperature=final_temp,
                max_tokens=max_tokens
            )

            if success:
                if validate_response:
                    # 在线程中运行验证，避免 json5/ast.literal_eval 等同步解析器阻塞事件循环
                    try:
                        is_valid, validation_error = await asyncio.wait_for(
                            asyncio.to_thread(validate_response, content),
                            timeout=30
                        )
                    except asyncio.TimeoutError:
                        logger.warning("❌ 响应验证超时（30秒），视为验证失败")
                        is_valid, validation_error = False, "验证超时（30秒）"
                    except Exception as e:
                        logger.warning(f"❌ 响应验证异常: {e}")
                        is_valid, validation_error = False, f"验证异常: {e}"
                    if not is_valid:
                        logger.warning(f"❌ 响应验证失败: {validation_error}")
                        logger.info(f"[DEBUG] LLM响应(全文): {repr(content)}")
                        # 累加失败 tokens（API 已消耗，但响应无效）
                        if isinstance(tokens, tuple) and len(tokens) == 2:
                            with self._stats_lock:
                                self._failed_tokens += tokens[0] + tokens[1]
                            call_failed_tokens += tokens[0] + tokens[1]
                            last_consumed_tokens = tokens
                        last_error = f"响应验证失败: {validation_error}"
                        # 构建带格式修正提示的messages用于重试
                        if retry_messages_builder:
                            messages = retry_messages_builder(validation_error, messages)
                        _get_failure_logger().record_failure(
                            attempt_num=total_attempts,
                            max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                            temperature=final_temp,
                            error_type="ValidationFailed",
                            error_message=last_error,
                            wait_time=wait_time,
                            messages_length=messages_length
                        )
                        continue

                logger.info(f"✅ API请求成功（第{total_attempts}次尝试）")
                logger.info(f"使用Tokens: {tokens}")
                logger.info("=" * 60)
                return True, content, "", tokens, {"attempts": total_attempts, "failed_tokens": call_failed_tokens}

            last_error = error
            logger.warning(f"❌ 重试失败: {error}")

            # 保留 API 调用消耗的 tokens
            if isinstance(tokens, tuple) and len(tokens) == 2 and (tokens[0] > 0 or tokens[1] > 0):
                last_consumed_tokens = tokens

            _get_failure_logger().record_failure(
                attempt_num=total_attempts,
                max_retries=self.config.temperature_max_retries + self.config.backoff_max_retries,
                temperature=final_temp,
                error_type="ExponentialBackoff",
                error_message=error,
                wait_time=wait_time,
                messages_length=messages_length
            )

            if "认证失败" in error or "401" in error:
                logger.error("⛔ 认证错误，停止重试")
                return False, "", error, last_consumed_tokens, {"attempts": total_attempts, "failed_tokens": call_failed_tokens}

        final_msg = f"所有重试均失败（温度退火{self.config.temperature_max_retries}次 + " \
                   f"指数退避{self.config.backoff_max_retries}次 = 共{total_attempts}次）。" \
                   f"最后错误: {last_error}"
        logger.error("\n" + "=" * 60)
        logger.error(f"⛔ API调用最终失败")
        logger.error(f"总尝试次数: {total_attempts}")
        logger.error(f"最后错误: {last_error}")

        summary = _get_failure_logger().get_summary()
        logger.error(f"失败统计: {summary}")
        logger.error("=" * 60)

        return False, "", final_msg, last_consumed_tokens, {"attempts": total_attempts, "failed_tokens": call_failed_tokens}

    def get_stats(self) -> dict:
        return {
            "total_requests": self._request_count,
            "total_attempts": self._attempts,
            "total_tokens": self._total_tokens,
            "failed_tokens": self._failed_tokens,
            "cached_tokens": self._cached_tokens,
            "failure_log_summary": _get_failure_logger().get_summary()
        }
