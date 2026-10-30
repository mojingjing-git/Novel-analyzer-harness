"""vnext 数据空间路由（PLAN v2 §4 S2 接线入口）。

口径与 1.x 路由的区别：

* 没有"兼容旧目录"的分支。旧目录只出现在 ``rejected`` 列表里，并带原因。
* 书目标识是 ``book_id``，不是书名。
* 进度来自块签名重算，不是数文件。
* 格式类错误一律带 ``code`` 字段返回，前端据此显示"此目录不属于本版本"，
  而不是猜一条 HTTP 500。
"""

from __future__ import annotations

import asyncio

import logging

from fastapi import APIRouter, HTTPException

from backend.services import vnext_service
from backend.vnext.constants import ErrorCode, VnextFormatError
from backend.vnext.project import RevisionConflict

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
    # 缺必需字段是「请求不合法」(422)，不是「资源不存在」(404)。
    # 之前统一 404 会让「缺少 title」看起来像「项目没找到」。
    ErrorCode.MISSING_REQUIRED_FIELD: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.INVALID_FIELD_VALUE: 422,
    ErrorCode.SOURCE_REF_UNRESOLVED: 422,
}


def _revision_error(exc: RevisionConflict) -> HTTPException:
    """清单版本冲突（并发写）。`RevisionConflict` 刻意不继承 VnextFormatError——
    它是并发语义而非格式问题，所以必须单独接住，否则会漏成 500。"""
    return HTTPException(
        status_code=409,
        detail={
            "code": ErrorCode.REVISION_CONFLICT.value,
            "message": str(exc),
            "path": None,
        },
    )


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


@router.post("/projects", status_code=201)
async def create_project(payload: dict):
    """从原文创建 vnext 项目。

    请求体二选一：

    * ``{"title": "...", "text": "<整本原文>"}`` —— 后端用**同一套**切章器切分；
    * ``{"title": "...", "chapters": [{"title": "...", "content": "..."}, ...]}``

    两种都给时以 ``chapters`` 为准（调用方已经切好，不该再被切一遍）。
    """
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _http_error(
            VnextFormatError(ErrorCode.INVALID_FIELD_VALUE, "title 必须是非空字符串")
        )
    title = title.strip()

    raw_chapters = payload.get("chapters")
    chapters = None
    if raw_chapters is not None:
        if not isinstance(raw_chapters, list):
            raise _http_error(
                VnextFormatError(ErrorCode.INVALID_FIELD_VALUE, "chapters 必须是数组")
            )
        chapters = []
        for index, item in enumerate(raw_chapters, start=1):
            if not isinstance(item, dict):
                raise _http_error(
                    VnextFormatError(
                        ErrorCode.INVALID_FIELD_VALUE, f"chapters[{index}] 不是对象"
                    )
                )
            body = item.get("content")
            if not isinstance(body, str):
                raise _http_error(
                    VnextFormatError(
                        ErrorCode.INVALID_FIELD_VALUE,
                        f"chapters[{index}].content 必须是字符串",
                    )
                )
            head = str(item.get("title") or "").strip()
            chapters.append((index, f"{head}\n{body}" if head else body))

    try:
        return vnext_service.create_book(
            title,
            text=payload.get("text"),
            chapters=chapters,
            book_id=payload.get("book_id"),
            dir_name=payload.get("dir_name"),
        )
    except VnextFormatError as exc:
        raise _http_error(exc) from exc


@router.get("/books/{book_id}/blocks")
async def get_blocks(book_id: str):
    """块级进度（断点恢复的读入口）：哪些块已完成、哪些失效、哪些没跑。"""
    try:
        return vnext_service.list_blocks(book_id)
    except VnextFormatError as exc:
        raise _http_error(exc) from exc


@router.post("/queue/restore")
async def restore_queue():
    """启动后修复：遗留 running 回落 pending。幂等，可重复调用。"""
    try:
        state = vnext_service.restore_queue_after_restart()
    except VnextFormatError as exc:
        raise _http_error(exc) from exc
    return {"entries": len(state.entries)}


@router.post("/queue")
async def enqueue_book(payload: dict):
    """把项目放进队列（**不启动分析**）。

    vnext 本版没有分析流水线，入队只是把条目写进 ``queue_state_vnext.json``；
    响应带 ``analysis_started: false``，避免界面让人以为「点了就在跑」。
    """
    try:
        return await asyncio.to_thread(vnext_service.enqueue_book, payload)
    except VnextFormatError as exc:
        # 服务层抛的是领域错误，必须在这里翻成 HTTP 状态；
        # 漏掉这一步会变成 500「服务器错误」，界面只能显示"操作失败"。
        raise _http_error(exc) from exc


@router.delete("/queue/{book_id}")
async def dequeue_book(book_id: str):
    """把项目移出队列。移出不存在的条目返回 404，不静默成功。"""
    try:
        return await asyncio.to_thread(vnext_service.dequeue_book, book_id)
    except VnextFormatError as exc:
        raise _http_error(exc) from exc
