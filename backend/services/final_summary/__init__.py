"""最终总结子包门面导出（Layer 4）。

对外完整重导出 FinalSummaryRunner 与所有提示词/解析/名称校验/进度消息工具，
保持历史导入路径 ``from backend.services.final_summary import ...`` 不变。
"""

from .parsers import (
    _NON_TITLE_PREFIXES,
    _clean_book_name,
    _looks_valid_book_name,
    _recheck_progress_message,
    build_reconciliation_retry_messages,
    build_volume_summary_retry_messages,
    detect_book_name,
    extract_analysis_text,
    extract_foreshadow_json,
    extract_key_fields,
    recheck_channel,
    recheck_conflict_reason,
    recheck_overrides,
    split_lifecycle,
    validate_reconciliation_json,
    validate_volume_summary,
)
from .atlas_extractor import (
    build_deterministic_anchors,
    compute_atlas_fingerprint,
    parse_and_sanitize_char_atlas,
    parse_and_sanitize_world_atlas,
    sanitize_world_tree,
)
from .prompts import (
    ATLAS_CHAR_SYSTEM_PROMPT,
    ATLAS_WORLD_SYSTEM_PROMPT,
    BATCH_SYSTEM_PROMPT,
    BATCH_USER_TEMPLATE,
    FINAL_SYSTEM_PROMPT,
    FINAL_USER_TEMPLATE,
    RECONCILIATION_SYSTEM_PROMPT,
    RECONCILIATION_USER_TEMPLATE,
)
from .runner import FinalSummaryRunner

__all__ = [
    "ATLAS_CHAR_SYSTEM_PROMPT",
    "ATLAS_WORLD_SYSTEM_PROMPT",
    "BATCH_SYSTEM_PROMPT",
    "BATCH_USER_TEMPLATE",
    "FINAL_SYSTEM_PROMPT",
    "FINAL_USER_TEMPLATE",
    "FinalSummaryRunner",
    "RECONCILIATION_SYSTEM_PROMPT",
    "RECONCILIATION_USER_TEMPLATE",
    "_NON_TITLE_PREFIXES",
    "_clean_book_name",
    "_looks_valid_book_name",
    "_recheck_progress_message",
    "build_deterministic_anchors",
    "build_reconciliation_retry_messages",
    "build_volume_summary_retry_messages",
    "compute_atlas_fingerprint",
    "detect_book_name",
    "extract_analysis_text",
    "extract_foreshadow_json",
    "extract_key_fields",
    "parse_and_sanitize_char_atlas",
    "parse_and_sanitize_world_atlas",
    "recheck_channel",
    "recheck_conflict_reason",
    "recheck_overrides",
    "sanitize_world_tree",
    "split_lifecycle",
    "validate_reconciliation_json",
    "validate_volume_summary",
]
