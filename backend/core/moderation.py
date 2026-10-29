"""
内容审核拦截识别（provider 无关）

2026-10-25 起判定逻辑迁移至 error_taxonomy.py（厂商错误码分类体系，见
docs/PLAN-provider-error-taxonomy-2026-09-30.md T1.2）：
- 码表/文本表由 taxonomy 持有并扩容（旧 10 值码 + 文本模式全量吸收）
- 本模块保留 [MODERATION] 标记契约（mark_moderation / is_moderation_error /
  MODERATION_MARKER 原样，pipeline/analyzer/前端零改动），判定函数转发 taxonomy
"""

import re

from .error_taxonomy import (
    MODERATION_BODY_CODES,
    MODERATION_TEXT_PATTERNS,
    is_moderation_code as _is_moderation_code,
    is_moderation_message as _is_moderation_message,
)

MODERATION_MARKER = "[MODERATION]"

# 兼容导出：旧码表位置（现为 taxonomy 结构化码表中审核类目的键集，已扩容）
MODERATION_CODES = set(MODERATION_BODY_CODES)

# 兼容导出：旧文本模式列表（现含标记/new_sensitive 等新增项）
MODERATION_TEXT_PATTERNS = list(MODERATION_TEXT_PATTERNS)

_MOD_TEXT_RE = re.compile("|".join(MODERATION_TEXT_PATTERNS), re.IGNORECASE)


def is_moderation_code(code) -> bool:
    """结构化错误码判定（code 可为 str/int/None）"""
    return _is_moderation_code(code)


def is_moderation_message(message) -> bool:
    """自由文本关键词判定"""
    return _is_moderation_message(message)


def mark_moderation(error: str) -> str:
    """给错误字符串加 [MODERATION] 标记（幂等）"""
    error = error or ""
    if is_moderation_error(error):
        return error
    return f"{MODERATION_MARKER} {error}"


def is_moderation_error(error: str) -> bool:
    """检查错误字符串是否带审核标记"""
    return bool(error) and error.startswith(MODERATION_MARKER)
