"""
配置读写路由
GET /api/settings — 返回当前配置（config.json）
PUT /api/settings — 覆盖保存配置
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.config.settings import AppConfig, validate_settings_payload
from backend.config.presets import API_PRESETS
from backend.services.queue_service import get_service

logger = logging.getLogger(__name__)


class ModelPreviewRequest(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    provider: Optional[str] = None
    # 探测目标模型（总结页对 summary_model 独立探测）；空回退主模型。
    # summary_model 复用同一 base_url/api_key，探测结果对其同样有效。
    model: Optional[str] = None

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/presets")
async def get_presets() -> dict:
    """返回 API 预设列表"""
    return {"presets": API_PRESETS}


@router.get("/schema")
async def get_settings_schema() -> dict:
    """返回全量配置字段元信息、分组、默认值与校验约束"""
    from backend.config.schema import get_config_schema

    return get_config_schema()


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


@router.post("/probe-recheck")
async def probe_recheck(req: ModelPreviewRequest) -> dict:
    """探测**复检判定通道**（微请求，不写配置）。

    三级回退：请求字段 > analysis.recheck_*（复检专用通道）> 主 api.*。
    用于在保存前验证"判定模型 + 判定端点"组合可用，并拿到该端点认的
    思考控制参数——判定模型换家族时（网关专用名/第三方端点）尤其需要。"""
    from backend.core.llm_probe import probe_thinking_params
    service = get_service()
    config = service.config_manager.load()
    a = config.analysis

    def resolve(explicit: Optional[str], recheck_val, main_val) -> str:
        return (explicit or str(recheck_val or "") or str(main_val or "")).strip()

    base_url = resolve(req.base_url, a.recheck_base_url, config.api.base_url)
    api_key = resolve(req.api_key, a.recheck_api_key, config.api.api_key)
    provider = resolve(req.provider, a.recheck_provider, config.api.provider)
    model = resolve(req.model, a.recheck_model, config.api.model)
    try:
        return await probe_thinking_params(base_url, api_key, model, provider)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/model-spec")
async def get_model_spec(model: str = "") -> dict:
    """获取指定模型的 models.dev 规范（思考能力、上下文上限、Token 计费）。
    未匹配时返回 {"found": False, "spec": None}，不抛 404/500。"""
    from backend.core.model_catalog import lookup_model_spec, get_catalog_version
    if not model.strip():
        return {"found": False, "spec": None, "catalog_version": get_catalog_version()}
    spec = lookup_model_spec(model)
    return {
        "found": spec is not None,
        "spec": spec,
        "catalog_version": get_catalog_version(),
    }


@router.post("/models/sync")
async def sync_models() -> dict:
    """从 models.dev 在线同步最新模型库至用户数据目录 data/models_dev.json。"""
    from backend.core.model_catalog import sync_models_from_remote
    try:
        res = sync_models_from_remote()
        return res
    except Exception as e:
        return {"success": False, "error": str(e)}


_ALLOWED_SUMMARY_PATCH = {
    "api": {"summary_model", "summary_thinking_mode"},
    "analysis": {"summary_batch_size", "summary_concurrency"},
}


def _is_summary_only_patch(data: dict) -> bool:
    if not isinstance(data, dict) or not data:
        return False
    has_field = False
    for sec, sec_val in data.items():
        if sec not in _ALLOWED_SUMMARY_PATCH or not isinstance(sec_val, dict):
            return False
        allowed_keys = _ALLOWED_SUMMARY_PATCH[sec]
        for k in sec_val:
            if k not in allowed_keys:
                return False
            has_field = True
    return has_field


def _running_task_conflict(*, allow_during_analysis: bool = False) -> Optional[str]:
    from backend.services.summary_service import get_summary_service

    if not allow_during_analysis and get_service().is_running:
        return "分析运行中，不能修改配置"
    if get_summary_service().is_running:
        return "最终总结运行中，不能修改配置"
    return None


@router.get("")
async def get_settings() -> dict:
    service = get_service()
    config = service.config_manager.load()
    return config.to_dict()


@router.put("")
async def put_settings(data: dict) -> dict:
    service = get_service()
    conflict = _running_task_conflict(allow_during_analysis=False)
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
    errors = validate_settings_payload(data)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    try:
        config = AppConfig.from_dict(data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=[{"loc": ["body"], "msg": f"配置格式无效: {e}"}])
    if not service.config_manager.save(config):
        # save() 拒绝通常意味着 config.json 解析失败待手工修复（_load_failed 置位），
        # 用 409 + 可操作文案替代笼统的 500，避免用户反复重试无效
        raise HTTPException(status_code=409,
                            detail="配置未能写入：config.json 可能解析失败待修复，"
                                   "请手工修复该文件后重试（详见日志）")
    return config.to_dict()


@router.patch("")
async def patch_settings(data: dict) -> dict:
    service = get_service()
    conflict = _running_task_conflict(allow_during_analysis=_is_summary_only_patch(data))
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
    errors = validate_settings_payload(data)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    updated = service.config_manager.patch(data)
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="配置未能写入：config.json 可能解析失败待修复，请手工修复该文件后重试（详见日志）",
        )
    return updated.to_dict()
