"""vnext 数据空间路由（PLAN v2 §4 S2 接线入口）。

口径与 1.x 路由的区别：

* 没有"兼容旧目录"的分支。旧目录只出现在 ``rejected`` 列表里，并带原因。
* 书目标识是 ``book_id``，不是书名。
* 进度来自块签名重算，不是数文件。
* 格式类错误一律带 ``code`` 字段返回，前端据此显示"此目录不属于本版本"，
  而不是猜一条 HTTP 500。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from backend.services import vnext_service
from backend.vnext.constants import ErrorCode, VnextFormatError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vnext", tags=["vnext"])


#: 错误码 → HTTP 状态。约定：格式/版本问题=409（冲突，需要用户换目录），
#: 字段非法=422，找不到=404。不要把所有格式错误都归到 500。
_HTTP_BY_CODE = {
    ErrorCode.UNSUPPORTED_ARTIFACT_FORMAT: 409,
    ErrorCode.UNSUPPORTED_SCHEMA_VERSION: 409,
    ErrorCode.UNPARSEABLE_ARTIFACT: 409,
    ErrorCode.BLOCK_SIGNATURE_MISMATCH: 409,
    ErrorCode.REVISION_CONFLICT: 409,
    ErrorCode.STALE_INPUT: 409,
    ErrorCode.MISSING_REQUIRED_FIELD: 404,
    ErrorCode.INVALID_FIELD_VALUE: 422,
    ErrorCode.SOURCE_REF_UNRESOLVED: 422,
}


def _http_error(exc: VnextFormatError) -> HTTPException:
    status = _HTTP_BY_CODE.get(exc.code, 400)
    return HTTPException(
        status_code=status,
        detail={
            "code": exc.code.value,
            "message": exc.detail,
            "path": exc.path,
        },
    )


@router.get("/books")
async def list_books():
    """列出 vnext 书目；被拒绝的目录也在返回里，带原因。"""
    return vnext_service.list_books()


@router.get("/books/{book_id}")
async def get_book(book_id: str):
    try:
        return vnext_service.open_book(book_id)
    except VnextFormatError as exc:
        raise _http_error(exc) from exc


@router.get("/queue")
async def get_queue():
    try:
        state = vnext_service.load_queue()
    except VnextFormatError as exc:
        raise _http_error(exc) from exc
    return {
        "entries": [
            {
                "book_id": e.book_id,
                "name": e.name,
                "status": e.status,
                "completed_blocks": e.completed_blocks,
                "total_blocks": e.total_blocks,
                "stale_blocks": e.stale_blocks,
                "error_message": e.error_message,
            }
            for e in state.entries
        ]
    }


@router.post("/queue/restore")
async def restore_queue():
    """启动后修复：遗留 running 回落 pending。幂等，可重复调用。"""
    try:
        state = vnext_service.restore_queue_after_restart()
    except VnextFormatError as exc:
        raise _http_error(exc) from exc
    return {"entries": len(state.entries)}
