"""Prompt 上下文限额（转发层）。

实现已收敛到 `backend/core.prompt_limits`——9 个上限只允许一个权威定义，
两套默认值会漂移，漂移的代价是"同一个配置在一个地方生效、在另一个地方被忽略"。
此文件仅保留转发，兼容既有 import 路径（citations.py 同款先例）。
"""

from backend.core.prompt_limits import (  # noqa: F401 - 公开 API 原样转发
    DEFAULT_LIMITS,
    LIMIT_FIELDS,
    PromptLimits,
    from_dict,
    to_dict,
)
