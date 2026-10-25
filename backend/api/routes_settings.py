"""
配置读写路由
GET /api/settings — 返回当前配置（config.json）
PUT /api/settings — 覆盖保存配置
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config.settings import AppConfig
from backend.config.presets import API_PRESETS
from backend.services.queue_service import get_service

logger = logging.getLogger(__name__)


class ModelPreviewRequest(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    provider: Optional[str] = None
    # 2026-09-30：探测目标模型（总结页对 summary_model 独立探测）；空回退主模型。
    # summary_model 复用同一 base_url/api_key，探测结果对其同样有效。
    model: Optional[str] = None

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/presets")
async def get_presets() -> dict:
    """返回 API 预设列表"""
    return {"presets": API_PRESETS}


@router.get("/models")
async def get_models() -> dict:
    """从 API 获取可用模型列表"""
    from backend.core.llm_client import LLMClient
    service = get_service()
    config = service.config_manager.load()
    try:
        models = await LLMClient.list_models(config.api.base_url, config.api.api_key, config.api.provider)
        return {"models": models}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/preview/models")
async def preview_models(req: ModelPreviewRequest) -> dict:
    """用表单当前（未保存）的 base_url/api_key 试查模型列表，不写回 config.json。
    空字段回退到已保存配置；POST body 避免 API Key 出现在 URL/日志中。"""
    from backend.core.llm_client import LLMClient
    service = get_service()
    config = service.config_manager.load()
    if req.base_url:
        config.api.base_url = req.base_url
    if req.api_key:
        config.api.api_key = req.api_key
    if req.provider:
        config.api.provider = req.provider
    try:
        models = await LLMClient.list_models(config.api.base_url, config.api.api_key, config.api.provider)
        return {"models": models}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/probe-thinking")
async def probe_thinking(req: ModelPreviewRequest) -> dict:
    """探测当前端点认哪个禁用思考参数（微请求，不写配置）。
    空字段回退到已保存配置。"""
    from backend.core.llm_probe import probe_thinking_params
    service = get_service()
    config = service.config_manager.load()
    if req.base_url:
        config.api.base_url = req.base_url
    if req.api_key:
        config.api.api_key = req.api_key
    if req.provider:
        config.api.provider = req.provider
    try:
        return await probe_thinking_params(
            config.api.base_url, config.api.api_key,
            req.model or config.api.model, config.api.provider)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("")
async def get_settings() -> dict:
    service = get_service()
    config = service.config_manager.load()
    return config.to_dict()


@router.put("")
async def put_settings(data: dict) -> dict:
    service = get_service()
    if service.is_running:
        raise HTTPException(status_code=409, detail="分析运行中，不能修改配置")
    try:
        config = AppConfig.from_dict(data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"配置格式无效: {e}")
    if not service.config_manager.save(config):
        # save() 拒绝通常意味着 config.json 解析失败待手工修复（_load_failed 置位），
        # 用 409 + 可操作文案替代笼统的 500，避免用户反复重试无效
        raise HTTPException(status_code=409,
                            detail="配置未能写入：config.json 可能解析失败待修复，"
                                   "请手工修复该文件后重试（详见日志）")
    return config.to_dict()
