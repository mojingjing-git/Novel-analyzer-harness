"""伏笔复检 V3：双向蒸馏路由 + 块内精排 + 有界多跳侦探 + 四防对称外科终审。

四层无环包结构：
- Layer 1 ``prompts``：静态常量与中立通用 Prompt 模板；
- Layer 2 ``parsers`` / ``preprocessing``：结构化响应解析与跨题材文本预处理；
- Layer 3 ``evidence_pool``：超块分桶、共享证据池组装与证据链核验；
- Layer 4 ``runner``：主执行器 ``run_recheck_v3``。
"""

from __future__ import annotations

from .evidence_pool import apply_v3_verdict_to_item, verify_evidence_chain_v3
from .parsers import _salvage_json_array_rows, _select_primary_evidence
from .preprocessing import (
    build_adaptive_digest_chunks,
    build_unit_headers,
    detect_protagonist_stopwords,
    format_unit_segment,
    tokenize_entity_tokens,
)
from .prompts import (
    ALLOWED_V3_TIERS,
    COMPACT_CHAPTER_CHARS,
    DEFAULT_V3_BATCH_SIZE,
    DEFAULT_V3_COMPACT_BATCH_SIZE,
    DEFAULT_V3_COMPACT_POOL_MAX_CHARS,
    DEFAULT_V3_CONCURRENCY,
    DEFAULT_V3_MAX_CALLS,
    DEFAULT_V3_POOL_MAX_CHARS,
    U1_ALIAS_CONSOLIDATE_PROMPT,
    U1_ALIAS_PAIR_PROMPT,
    U1_COMPACT_ROUTE_Q_BATCH,
    U1_COMPACT_SPLIT_BATCH_SIZE,
    U1_COMPACT_SUPERCHUNK_TARGET_CHARS,
    U1_ROUTE_PROMPT,
    U1_ROUTE_Q_BATCH,
    U1_SPLIT_BATCH_SIZE,
    U1_SPLIT_PROMPT,
    U1_SUPERCHUNK_TARGET_CHARS,
    U3_DETECTIVE_PROMPT,
    U3_POOL_SINGLE_STAGE_PROMPT,
    U4_AUDITOR_PROMPT,
    U4_POOL_AUDITOR_PROMPT,
    U5_QUOTE_REPAIR_PROMPT,
    V3_PROMPT_HARD_CAP_CHARS,
    V3RecheckOptions,
    WIDE_CHAPTER_CHARS,
)
from .runner import run_recheck_v3

__all__ = [
    "ALLOWED_V3_TIERS",
    "COMPACT_CHAPTER_CHARS",
    "DEFAULT_V3_BATCH_SIZE",
    "DEFAULT_V3_COMPACT_BATCH_SIZE",
    "DEFAULT_V3_COMPACT_POOL_MAX_CHARS",
    "DEFAULT_V3_CONCURRENCY",
    "DEFAULT_V3_MAX_CALLS",
    "DEFAULT_V3_POOL_MAX_CHARS",
    "U1_ALIAS_CONSOLIDATE_PROMPT",
    "U1_ALIAS_PAIR_PROMPT",
    "U1_COMPACT_ROUTE_Q_BATCH",
    "U1_COMPACT_SPLIT_BATCH_SIZE",
    "U1_COMPACT_SUPERCHUNK_TARGET_CHARS",
    "U1_ROUTE_PROMPT",
    "U1_ROUTE_Q_BATCH",
    "U1_SPLIT_BATCH_SIZE",
    "U1_SPLIT_PROMPT",
    "U1_SUPERCHUNK_TARGET_CHARS",
    "U3_DETECTIVE_PROMPT",
    "U3_POOL_SINGLE_STAGE_PROMPT",
    "U4_AUDITOR_PROMPT",
    "U4_POOL_AUDITOR_PROMPT",
    "U5_QUOTE_REPAIR_PROMPT",
    "V3_PROMPT_HARD_CAP_CHARS",
    "V3RecheckOptions",
    "WIDE_CHAPTER_CHARS",
    "_salvage_json_array_rows",
    "_select_primary_evidence",
    "apply_v3_verdict_to_item",
    "build_adaptive_digest_chunks",
    "build_unit_headers",
    "detect_protagonist_stopwords",
    "format_unit_segment",
    "run_recheck_v3",
    "tokenize_entity_tokens",
    "verify_evidence_chain_v3",
]
