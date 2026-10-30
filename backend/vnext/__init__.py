"""vnext 数据空间（PLAN v2 §1.1 / §2）。

本包只放**新格式**的常量、验证器与来源模型。旧格式（``chapter_*_result.json``、
``knowledge.json``、``rolling_summary.json``、伏笔账本 v3、聚合产物、总结 checkpoint）
在这里没有读取入口，也没有转换入口——这是设计目标，不是尚未实现。

约定：本包不得反向 import 旧的数据层模块（``backend.models`` / ``backend.utils.aggregate_utils``
等）。新契约需要的新形状在这里独立定义，避免"旧字段顺手复用"变成隐式兼容层。
"""

from .constants import (
    FORMAT_ID,
    SCHEMA_VERSION,
    WORKSPACE_DIR_NAME,
    QUEUE_STATE_FILE_NAME,
    BOOK_INDEX_FILE_NAME,
    CONFIG_FILE_NAME,
    MANIFEST_FILE_NAME,
    VnextFormatError,
    ErrorCode,
)

__all__ = [
    "FORMAT_ID",
    "SCHEMA_VERSION",
    "WORKSPACE_DIR_NAME",
    "QUEUE_STATE_FILE_NAME",
    "BOOK_INDEX_FILE_NAME",
    "CONFIG_FILE_NAME",
    "MANIFEST_FILE_NAME",
    "VnextFormatError",
    "ErrorCode",
]
