"""兼容转发层：权威实现位于 backend/common/error_types.py。

core 内 7 个文件与 17 个测试文件的历史引用路径保持不变；符号身份（ErrorClass 的
``is`` 比较）由转发保证——两边拿到的是同一个类对象。新代码请直接从
backend.common 引用。
"""

from ..common.error_types import *  # noqa: F401,F403
from ..common.error_types import (  # noqa: F401  （显式列出公开 API）
    Confidence,
    ErrorClass,
    ErrorScope,
    ErrorVerdict,
    LLMResult,
    SourceChannel,
    UsageStatus,
)
