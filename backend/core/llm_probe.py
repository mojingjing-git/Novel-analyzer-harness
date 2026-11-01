"""禁用思考参数探测（probe_thinking_params）
多模式检测某端点/模型默认是否输出思考链、哪个禁用参数有效。与 LLMClient
的调用/重试主链无状态耦合（自建 AsyncOpenAI 探针客户端，不走重试链/失败日志），
单测 patch 目标为 `backend.core.llm_probe.AsyncOpenAI`。
"""

import asyncio
import json
import logging
import re
import time
from typing import List, Tuple

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


def _get_reasoning_tokens(usage) -> int:
    """从 usage 安全提取 reasoning_tokens，兼容 dict / Pydantic model / None

    OpenAI SDK 的 CompletionUsage 与
    CompletionTokensDetails 都是 Pydantic v2 model，没有 .get() 方法。
    (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    在 Pydantic 形态下 AttributeError，导致 probe_thinking_params 所有「真在
    思考」的候选都炸（只有模型完全禁思考时 completion_tokens_details 为 None
    才碰巧走通）—— 表现为大部分候选都报「无效」。
    """
    if usage is None:
        return 0
    # 顶层 usage：可能是 dict 也可能是 Pydantic
    if isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
    else:
        details = getattr(usage, "completion_tokens_details", None)
    if details is None:
        return 0
    # 嵌套 details：同样两种形态
    if isinstance(details, dict):
        return details.get("reasoning_tokens", 0) or 0
    return getattr(details, "reasoning_tokens", 0) or 0


# 探测禁用思考参数用的检测器清单（前端 UI 也用这份，禁删字段）
# 7 个维度，任一命中即判定模型在思考：
#   - reasoning_content: Anthropic / GLM / DeepSeek v3.1+ 字段
#   - reasoning_details: MiniMax M3 + reasoning_split=true
#   - think_tags: <think>...</think> （DeepSeek R1 / M3 默认 / QwQ / Qwen3）
#   - thinking_tags: <thinking>...</thinking>
#   - reasoning_tag: <reasoning>...</reasoning>
#   - usage_reasoning_tokens: OpenAI o-series 隐藏 thinking（仅在 usage 计费字段）
#   - anthropic_thinking_blocks: Anthropic 协议 content list 形态
_THINKING_DETECTORS: List[Tuple[str, str]] = [
    ("reasoning_content", "message.reasoning_content 字段（GLM/DeepSeek v3.1+）"),
    ("reasoning_details", "message.reasoning_details 字段（M3+reasoning_split）"),
    ("think_tags", "content 含 <think>...</think>（DeepSeek R1 / M3 / QwQ / Qwen3）"),
    ("thinking_tags", "content 含 <thinking>...</thinking>"),
    ("reasoning_tag", "content 含 <reasoning>...</reasoning>"),
    ("usage_reasoning_tokens", "usage.completion_tokens_details.reasoning_tokens > 0（OpenAI o-series）"),
    ("anthropic_thinking_blocks", "content 是 list 且含 type=thinking block（Anthropic）"),
]


async def probe_thinking_params(base_url: str, api_key: str, model: str,
                                provider: str = "auto", max_tokens: int = 1024) -> dict:
    """探测当前端点实际认哪个禁用思考参数（OpenAI 兼容端点）。

    对每个候选参数发一次微请求（小 prompt，max_tokens=1024，30s 超时，
    不走重试链/失败日志）。**多模式检测**（任一命中即判定模型在思考）：

    1. ``reasoning_content`` 字段非空（Anthropic / GLM / DeepSeek v3.1+）
    2. ``reasoning_details`` 字段非空（M3 + reasoning_split=true）
    3. content 含 ``<think>...</think>`` 标签（DeepSeek R1 / M3 默认 / QwQ / Qwen3）
    4. content 含 ``<thinking>...</thinking>`` 标签
    5. content 含 ``<reasoning>...</reasoning>`` 标签
    6. ``usage.completion_tokens_details.reasoning_tokens > 0``（OpenAI o-series 隐藏 thinking）
    7. content 是 list 且含 ``type=thinking`` block（Anthropic 协议 content list 形态）

    判定标准：基线请求**任一**模式命中 → 模型默认开启思考；
    候选参数请求**所有**模式都未命中 → 该参数有效（关闭了思考）。

    anthropic 协议无需探测（官方参数即 thinking:disabled）。

    返回结构: {
      "results": [
        {
          "param", "thinking_mode",
          "reasoning_chars", "content_chars", "think_tag_chars", "reasoning_token_count",
          "worked", "error", "detection_breakdown": {7 个布尔}
        }...
      ],
      "best": {"thinking_mode": dict, "param": str} | None,
      "default_thinks": bool,
      "default_detection_breakdown": {7 个布尔},
      "note": str
    }
    """
    if provider == "anthropic" or (provider == "auto" and "anthropic" in (base_url or "").lower()):
        return {
            "results": [{
                "param": "thinking disabled（anthropic 官方）",
                "thinking_mode": {"thinking": {"type": "disabled"}},
                "reasoning_chars": 0, "content_chars": 0,
                "think_tag_chars": 0, "reasoning_token_count": 0,
                "worked": True, "error": "",
                "detection_breakdown": {k: False for k, _ in _THINKING_DETECTORS},
            }],
            "best": {"thinking_mode": {"thinking": {"type": "disabled"}}, "param": "thinking disabled（anthropic 官方）"},
            "default_thinks": True,
            "default_detection_breakdown": {k: False for k, _ in _THINKING_DETECTORS},
            "note": "anthropic 协议官方禁用思考参数即 thinking:disabled，无需探测",
        }

    client = AsyncOpenAI(base_url=base_url, api_key=api_key if api_key else "empty")
    prompt = "计算 123456789 * 987654321 的结果，只输出数字。"

    # 候选参数：覆盖 2025-2026 主流厂商的禁用 thinking 语法
    candidates = [
        {"param": "无参数（基线）", "thinking_mode": None, "extra": {}},
        # 三个基础关闭写法
        {"param": "thinking: disabled", "thinking_mode": {"thinking": {"type": "disabled"}},
         "extra": {"extra_body": {"thinking": {"type": "disabled"}}}},
        {"param": "reasoning_effort: none", "thinking_mode": {"reasoning_effort": "none"},
         "extra": {"extra_body": {"reasoning_effort": "none"}}},
        {"param": "enable_thinking: false", "thinking_mode": {"enable_thinking": False},
         "extra": {"extra_body": {"enable_thinking": False}}},
        # MiniMax M3 完整关闭（thinking + reasoning_split 联合）
        {"param": "thinking: disabled + reasoning_split",
         "thinking_mode": {"thinking": {"type": "disabled"}, "reasoning_split": True},
         "extra": {"extra_body": {"thinking": {"type": "disabled"}, "reasoning_split": True}}},
        # Qwen3 / GLM vLLM 走 chat_template_kwargs
        {"param": "chat_template_kwargs: enable_thinking=false",
         "thinking_mode": {"chat_template_kwargs": {"enable_thinking": False}},
         "extra": {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}},
        # OpenAI Responses 风格
        {"param": "reasoning: effort=none",
         "thinking_mode": {"reasoning": {"effort": "none"}},
         "extra": {"extra_body": {"reasoning": {"effort": "none"}}}},
    ]

    logger.info(f"开始探测禁用思考参数 - Model: {model}, 端点: {base_url}, "
                f"候选 {len(candidates)} 个（并发发送，各 30s 超时）")

    def _detect(msg, usage: dict) -> tuple:
        """返回 (any_detected, breakdown_dict, reasoning_chars, content_chars, think_tag_chars, reasoning_token_count)"""
        content = getattr(msg, "content", None) or ""
        content_str = content if isinstance(content, str) else ""  # list 形态（Anthropic）走 _anthropic_thinking_blocks
        content_chars = len(content_str.strip())

        reasoning = getattr(msg, "reasoning_content", None) or ""
        # 防御：MagicMock 默认返回的 reasoning_content 是 Mock 实例（len 会爆），强制 str
        reasoning_chars = len(str(reasoning).strip()) if reasoning else 0

        reasoning_details = getattr(msg, "reasoning_details", None)
        # 防御：MagicMock 会让 reasoning_details 返回 Mock 实例（非 None），视为无值
        if isinstance(reasoning_details, (dict, list, str)):
            has_reasoning_details = bool(reasoning_details)
        else:
            has_reasoning_details = False

        # think tag 字符数（用于诊断）
        think_tag_chars = 0
        for pat in (r"<think>.*?</think>", r"<thinking>.*?</thinking>", r"<reasoning>.*?</reasoning>"):
            for m in re.finditer(pat, content_str, re.DOTALL | re.IGNORECASE):
                think_tag_chars += len(m.group(0))

        reasoning_token_count = _get_reasoning_tokens(usage)

        # Anthropic 协议 content list 形态
        has_anthropic_thinking_blocks = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "thinking" for b in content
        )

        breakdown = {
            "reasoning_content": bool(reasoning_chars),
            "reasoning_details": has_reasoning_details,
            "think_tags": bool(re.search(r"<think>.*?</think>", content_str, re.DOTALL | re.IGNORECASE)),
            "thinking_tags": bool(re.search(r"<thinking>.*?</thinking>", content_str, re.DOTALL | re.IGNORECASE)),
            "reasoning_tag": bool(re.search(r"<reasoning>.*?</reasoning>", content_str, re.DOTALL | re.IGNORECASE)),
            "usage_reasoning_tokens": reasoning_token_count > 0,
            "anthropic_thinking_blocks": has_anthropic_thinking_blocks,
        }
        return any(breakdown.values()), breakdown, reasoning_chars, content_chars, think_tag_chars, reasoning_token_count

    async def probe_one(cand: dict) -> dict:
        empty_breakdown = {k: False for k, _ in _THINKING_DETECTORS}
        result = {
            "param": cand["param"],
            "thinking_mode": cand["thinking_mode"],
            "reasoning_chars": 0, "content_chars": 0,
            "think_tag_chars": 0, "reasoning_token_count": 0,
            "worked": False, "error": "",
            "detection_breakdown": dict(empty_breakdown),
        }
        t0 = time.time()
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.1,
                timeout=30,
                **cand["extra"],
            )
            msg = resp.choices[0].message
            usage = getattr(resp, "usage", None)
            # Pydantic / TypedDict 兼容：usage 可能是 dict 或对象
            usage_dict = usage if isinstance(usage, dict) else (vars(usage) if usage else {})
            detected, breakdown, r_chars, c_chars, tag_chars, tok_count = _detect(msg, usage_dict)
            result.update({
                "reasoning_chars": r_chars,
                "content_chars": c_chars,
                "think_tag_chars": tag_chars,
                "reasoning_token_count": tok_count,
                "detection_breakdown": breakdown,
            })
            if cand["thinking_mode"] is None:
                result["worked"] = None  # 基线不判定
            else:
                # 候选参数有效 = 所有检测模式都未命中
                if not detected:
                    result["worked"] = True
                elif c_chars == 0 and r_chars > 0:
                    # 旧版"MiniMax 拆字段坑"告警
                    result["error"] = "content 为空、思考全在 reasoning_content"
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        dt = time.time() - t0
        if cand["thinking_mode"] is None:
            logger.info(f"探测[{cand['param']}] 完成（{dt:.1f}s）: 思考 {result['reasoning_chars']} 字 / "
                        f"内容 {result['content_chars']} 字 / think_tag {result['think_tag_chars']} 字 / "
                        f"reasoning_tokens {result['reasoning_token_count']} / "
                        f"detected={any(result['detection_breakdown'].values())}")
        else:
            verdict = "✅ 有效" if result["worked"] else "❌ 无效"
            logger.info(f"探测[{cand['param']}] 完成（{dt:.1f}s）: 思考 {result['reasoning_chars']} 字 / "
                        f"内容 {result['content_chars']} 字 / think_tag {result['think_tag_chars']} 字 / "
                        f"reasoning_tokens {result['reasoning_token_count']} → {verdict}"
                        + (f"，原因: {result['error']}" if result['error'] else ""))
        return result

    # 所有候选并发发送
    results = list(await asyncio.gather(*(probe_one(c) for c in candidates)))

    baseline = results[0]
    default_thinks = any(baseline["detection_breakdown"].values())
    default_breakdown = baseline["detection_breakdown"]
    best = None
    note = ""
    if not default_thinks:
        note = "该模型默认不输出思考链，无需禁用思考"
    else:
        for r in results[1:]:
            if r["worked"]:
                best = {"thinking_mode": r["thinking_mode"], "param": r["param"]}
                break
        if best is None:
            # 分情况给 note：M2.x 系类是 known limitation
            # 这里没有 model → provider 映射，保守给通用提示
            note = ("未探测到有效的禁用思考参数。可能是：①M2.x 系类官方不支持（"
                    "MiniMax M2/M2.5/M2.7 等思考强制开启）；②需要尝试更多参数组合。"
                    "考虑临时切到同厂商其他模型（如 M3）或非思考模型。")

    if best:
        logger.info(f"探测完成: 默认思考={default_thinks}, 推荐参数=[{best['param']}] → "
                    f"{json.dumps(best['thinking_mode'], ensure_ascii=False)}")
    else:
        logger.warning(f"探测完成: 默认思考={default_thinks}, 未命中有效参数 - {note}")

    return {
        "results": results,
        "best": best,
        "default_thinks": default_thinks,
        "default_detection_breakdown": default_breakdown,
        "note": note,
    }
