"""兼容转发层：权威实现位于 backend/common/moderation.py。

[MODERATION] 标记契约（mark_moderation / is_moderation_error / MODERATION_MARKER）
的历史引用路径保持不变（pipeline / analyzer / 前端沿用同一标记）。新代码请直接从
backend.common 引用。
"""

from ..common.moderation import *  # noqa: F401,F403
from ..common.moderation import (  # noqa: F401  （显式列出公开 API）
    MODERATION_CODES,
    MODERATION_MARKER,
    MODERATION_TEXT_PATTERNS,
    is_moderation_code,
    is_moderation_error,
    is_moderation_message,
    mark_moderation,
)
