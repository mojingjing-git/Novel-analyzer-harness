"""
聚合数据路由
POST /api/aggregate/run — 执行聚合
GET /api/aggregate/{book_id}/files — 列出聚合文件
GET /api/aggregate/{book_id}/file/{name} — 读取聚合文件内容
POST /api/aggregate/{book_id}/excel — 导出Excel
"""

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.book_service import get_output_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/aggregate", tags=["aggregate"])


class AggregateRequest(BaseModel):
    book_id: str
    include_raw: bool = False


@router.post("/run")
async def run_aggregate(req: AggregateRequest) -> dict:
    """执行聚合"""
    output_dir = get_output_dir(req.book_id)
    if output_dir is None or not output_dir.exists():
        raise HTTPException(status_code=404, detail=f"书目不存在或无输出: {req.book_id}")

    aggregated_dir = output_dir / "aggregated"
    try:
        files = await _run_async_aggregate(output_dir, aggregated_dir, req.include_raw)
        return {"ok": True, "files": files}
    except Exception as e:
        logger.error(f"聚合失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _run_async_aggregate(output_dir: Path, aggregated_dir: Path, include_raw: bool) -> dict:
    from ..utils.aggregate_utils import aggregate_novel_analysis

    result = await asyncio.to_thread(
        aggregate_novel_analysis,
        str(output_dir), str(aggregated_dir), include_raw,
        lambda: False,
    )
    if isinstance(result, dict):
        return {k: str(v) for k, v in result.items()}
    return {}


@router.get("/{book_id}/files")
async def list_aggregate_files(book_id: str) -> dict:
    """列出聚合文件"""
    output_dir = get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    aggregated_dir = output_dir / "aggregated"
    if not aggregated_dir.exists():
        return {"files": []}

    files = []
    for f in sorted(aggregated_dir.glob("*.json")):
        if f.name.startswith("_"):
            continue
        files.append({
            "name": f.name,
            "size_kb": round(f.stat().st_size / 1024, 1),
        })
    return {"files": files}


@router.get("/{book_id}/file/{name}")
async def read_aggregate_file(book_id: str, name: str) -> dict:
    """读取聚合文件内容"""
    output_dir = get_output_dir(book_id)
    if output_dir is None:
        raise HTTPException(status_code=404, detail=f"书目不存在: {book_id}")

    # 防路径穿越：必须仍落在 output_dir/aggregated 之内，否则拒绝（如 ../../config.json 读取含 API Key 配置）
    base = (output_dir / "aggregated").resolve()
    file_path = (base / name).resolve()
    if file_path != base and not file_path.is_relative_to(base):
        raise HTTPException(status_code=400, detail="非法文件名（可能越界访问）")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {name}")

    try:
        content = file_path.read_text(encoding="utf-8")
        try:
            data = json.loads(content)
            return {"content": data, "is_json": True}
        except json.JSONDecodeError:
            return {"content": content, "is_json": False}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{book_id}/excel")
async def export_aggregate_excel(book_id: str) -> dict:
    """导出聚合数据到Excel（导出前按需自动刷新聚合缓存，无需手动前置点击）"""
    from ..utils.aggregate_utils import ensure_aggregated_cache, get_agg_lock
    from ..utils.excel_export import export_aggregated_to_excel

    output_dir = get_output_dir(book_id)
    if output_dir is None or not output_dir.exists():
        raise HTTPException(status_code=404, detail=f"书目不存在或无输出: {book_id}")

    try:
        aggregated_dir = await asyncio.to_thread(ensure_aggregated_cache, output_dir)

        def _locked_export():
            with get_agg_lock(output_dir):
                return export_aggregated_to_excel(aggregated_dir)

        path = await asyncio.to_thread(_locked_export)
        return {"ok": True, "path": str(path)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
