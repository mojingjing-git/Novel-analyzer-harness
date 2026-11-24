"""兼容转发层：权威实现位于 backend/common/error_taxonomy.py。

core 内引用面（llm_client / llm_stream / llm_failure_logger / pipeline 等）路径不变；
符号身份由转发保证。新代码请直接从 backend.common 引用。
"""

from ..common.error_taxonomy import *  # noqa: F401,F403
from ..common.error_taxonomy import (  # noqa: F401  （显式列出常用公开 API）
    RETRY_POLICY,
    ClassVerdict,
    ErrorClass,
    MODERATION_BODY_CODES,
    MODERATION_TEXT_PATTERNS,
    _MESSAGE_SCAN_CODES,  # noqa: F401  （test_c01 静态守护引用私有表）
    classify_exception,
    classify_message,
    is_moderation_code,
    is_moderation_message,
)
