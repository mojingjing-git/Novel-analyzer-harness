"""
工作区管理路由
GET  /api/workspace/novels         — 列出 workspace 中的小说
GET  /api/workspace/archives       — 列出已有归档
POST /api/workspace/archive        — 归档指定小说
POST /api/workspace/archive_all    — 归档全部
POST /api/workspace/delete_archive — 删除归档
POST /api/workspace/restore_blocks — 从归档仅还原 blocks/ 切分章节至工作区并加入队列
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services import workspace_service
from backend.services.queue_service import get_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


def _require_analysis_idle(action: str = "归档"):
    """归档或还原操作会改动工作区目录及队列状态，分析运行时拒绝执行。"""
    if get_service().is_running:
        raise HTTPException(status_code=409, detail=f"分析正在进行中，请先停止或等待其完成后再{action}")


class ArchiveRequest(BaseModel):
    novel_name: str
    mode: str = "rename"  # skip / overwrite / rename


class DeleteArchiveRequest(BaseModel):
    archive_name: str


class RestoreBlocksRequest(BaseModel):
    archive_name: str
    target_name: str = ""


@router.get("/novels")
async def list_novels():
    """列出 workspace 中的小说目录"""
    return {"novels": workspace_service.list_workspace_novels()}


@router.get("/archives")
async def list_archives():
    """列出 分析结果/ 中的归档"""
    return {"archives": workspace_service.list_archives()}


@router.post("/archive")
async def archive(req: ArchiveRequest):
    """归档指定小说"""
    _require_analysis_idle()
    if req.mode not in ("skip", "overwrite", "rename"):
        raise HTTPException(status_code=422, detail="mode 须为 skip/overwrite/rename")
    result = workspace_service.archive_novel(req.novel_name, mode=req.mode)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "归档失败"))
    return result


@router.post("/archive_all")
async def archive_all():
    """归档全部小说"""
    _require_analysis_idle()
    return workspace_service.archive_all()


@router.post("/delete_archive")
async def delete_archive(req: DeleteArchiveRequest):
    """删除指定归档"""
    result = workspace_service.delete_archive(req.archive_name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "删除失败"))
    return result


@router.post("/restore_blocks")
async def restore_blocks(req: RestoreBlocksRequest):
    """将指定归档中的 blocks/ 切分章节还原回工作区，并加入待分析队列"""
    _require_analysis_idle("还原切分")
    result = await asyncio.to_thread(
        workspace_service.restore_archive_blocks,
        req.archive_name,
        req.target_name,
    )
    if not result.get("ok"):
        status = 409 if result.get("conflict") else 400
        raise HTTPException(status_code=status, detail=result.get("error", "还原切分失败"))
    return result
