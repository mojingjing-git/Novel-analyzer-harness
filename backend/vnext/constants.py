"""vnext 数据空间的标识、文件名与错误码。

**为什么是字面量而不是读配置**：格式族与版本是"这份文件属于哪个解释器"的判据，
必须由代码决定。任何来自配置、环境变量或文件内容的"版本覆盖"都会让"不猜字段、
不回退解析"的规则失效。
"""

from __future__ import annotations

from enum import Enum

# ---------------------------------------------------------------------------
# 格式标识
# ---------------------------------------------------------------------------

#: 格式族。与 ``schema_version`` 共同识别文件；缺一即拒收。
FORMAT_ID = "novel-analyzer-vnext"

#: 契约版本。不兼容变更时 +1，并且**不**为旧版本降级解析。
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 路径与文件名（PLAN v2 §1.1-2：独立工作区、独立队列、独立书目索引）
# ---------------------------------------------------------------------------

#: 独立默认工作区目录名。旧 ``workspace/`` 不在任何读链里。
WORKSPACE_DIR_NAME = "workspace_vnext"

#: 独立队列状态文件名。
QUEUE_STATE_FILE_NAME = "queue_state_vnext.json"

#: 独立书目索引文件名（替代旧 ``book_service`` 的目录扫描发现）。
BOOK_INDEX_FILE_NAME = "books_vnext.json"

#: 独立运行配置文件（首次启动创建默认值；不读取旧 ``config.json`` 里的路径）。
CONFIG_FILE_NAME = "config_vnext.json"

#: 每本项目的项目清单文件名，位于项目目录根部。
MANIFEST_FILE_NAME = "manifest.json"


# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------


class ErrorCode(str, Enum):
    """格式与契约相关的拒绝原因。

    只收"程序可以确定"的失败。语义层面的失败（模型判断、内容质量）不属于错误码，
    它们是候选裁决或任务状态。
    """

    #: 缺 ``format_id`` / ``schema_version``，或格式族不是 :data:`FORMAT_ID`。
    #: 这是旧产物（``chapter_*_result.json``、``knowledge.json`` 等）与"任意目录"
    #: 的统一出口，界面文案固定为"此目录不属于本版本，请从原文创建新项目"。
    UNSUPPORTED_ARTIFACT_FORMAT = "UNSUPPORTED_ARTIFACT_FORMAT"

    #: 格式族对得上，但版本号不在本实现支持范围内（只支持 :data:`SCHEMA_VERSION`）。
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"

    #: 格式标记正确但缺必需字段（不存在的字段不得用旧默认值补全）。
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"

    #: 字段存在但类型/取值域非法。
    INVALID_FIELD_VALUE = "INVALID_FIELD_VALUE"

    #: 文件存在、格式标记缺失或版本不符之外的第三种情况：内容不是合法 JSON。
    #: 与"格式不支持"分开，因为前者可能是一次写坏（可重写），后者是"走错门"。
    UNPARSEABLE_ARTIFACT = "UNPARSEABLE_ARTIFACT"

    #: 块签名与当前源快照不符（正文、章节集合或策略版本任一变化）。
    BLOCK_SIGNATURE_MISMATCH = "BLOCK_SIGNATURE_MISMATCH"

    #: 引用无法在当前规范文本中唯一定位（找不到或不唯一）。
    SOURCE_REF_UNRESOLVED = "SOURCE_REF_UNRESOLVED"

    #: 依赖指纹不符：结果本身完好，但它依赖的输入已变。
    STALE_INPUT = "STALE_INPUT"

    #: 同一本书的写入提交发生 revision 冲突（仅元数据竞争，重读后可重试）。
    REVISION_CONFLICT = "REVISION_CONFLICT"


class VnextFormatError(Exception):
    """格式/契约校验失败的统一异常。

    携带 :class:`ErrorCode` 与人类可读的中文原因，调用方据此决定是"拒绝该产物"
    还是"终止该任务"——两者都不能静默降级成默认值。
    """

    def __init__(self, code: ErrorCode, detail: str, *, path: str | None = None) -> None:
        self.code = code
        self.detail = detail
        self.path = path
        super().__init__(f"{code.value}: {detail}" + (f" ({path})" if path else ""))

    def to_dict(self) -> dict:
        """序列化成可经 API 返回的形状（中文文案由前端按 code 查表，不塞长句）。"""
        return {
            "code": self.code.value,
            "detail": self.detail,
            "path": self.path,
        }
