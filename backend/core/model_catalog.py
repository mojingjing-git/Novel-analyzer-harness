"""
backend/core/model_catalog.py
基于 models.dev 规范的模型能力目录与归一化匹配引擎。

支持两层加载架构：
1. 用户更新目录 (data/models_dev.json) - 优先级高
2. 内置底包 (backend/data/models_dev.json) - 默认随发布包提供
3. 内联硬编码兜底字典 (_FALLBACK_CATALOG) - 极端容灾，保障 100% 离线可用

严禁非确定性模糊子串匹配，保留代际版本（-v3, -r1, -flash 等），防假阳性。
"""

import json
import logging
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 文件路径定义
_BASE_DIR = Path(__file__).resolve().parent.parent
BUNDLED_PATH = _BASE_DIR / "data" / "models_dev.json"
USER_DATA_DIR = Path("data")
USER_CUSTOM_PATH = USER_DATA_DIR / "models_dev.json"

# 极端容灾保底（当内置底包被意外误删或损坏时，核心主流模型仍可精准识别）
_FALLBACK_CATALOG: Dict[str, Any] = {
    "minimax-m3.1-flash": {
        "id": "minimax/MiniMax-M3.1-Flash",
        "name": "MiniMax M3.1 Flash",
        "family": "minimax",
        "reasoning": True,
        "reasoning_type": "effort",
        "is_always_on": True,
        "effort_values": ["low", "medium", "high", "xhigh", "max"],
        "recommended_mode": {"reasoning_effort": "low"},
        "limit": {"context": 1000000, "output": 512000},
        "cost": {"input": 0.15, "output": 0.6, "cache_read": 0.015},
    },
    "deepseek-r1": {
        "id": "deepseek/deepseek-r1",
        "name": "DeepSeek R1",
        "family": "deepseek-thinking",
        "reasoning": True,
        "reasoning_type": "toggle",
        "is_always_on": False,
        "effort_values": [],
        "recommended_mode": {"enable_thinking": False},
        "limit": {"context": 128000, "output": 8192},
        "cost": {"input": 0.55, "output": 2.19, "cache_read": 0.14},
    },
    "claude-3-7-sonnet": {
        "id": "anthropic/claude-3-7-sonnet",
        "name": "Claude 3.7 Sonnet",
        "family": "claude-sonnet",
        "reasoning": True,
        "reasoning_type": "budget_tokens",
        "is_always_on": False,
        "effort_values": [],
        "recommended_mode": {"thinking": {"type": "enabled", "budget_tokens": 1024}},
        "limit": {"context": 200000, "output": 64000},
        "cost": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    },
    "o1": {
        "id": "openai/o1",
        "name": "OpenAI o1",
        "family": "o",
        "reasoning": True,
        "reasoning_type": "effort",
        "is_always_on": False,
        "effort_values": ["low", "medium", "high"],
        "recommended_mode": {"reasoning_effort": "low"},
        "limit": {"context": 200000, "output": 100000},
        "cost": {"input": 15.0, "output": 60.0, "cache_read": 7.5},
    },
    "o3-mini": {
        "id": "openai/o3-mini",
        "name": "OpenAI o3-mini",
        "family": "o",
        "reasoning": True,
        "reasoning_type": "effort",
        "is_always_on": False,
        "effort_values": ["low", "medium", "high"],
        "recommended_mode": {"reasoning_effort": "low"},
        "limit": {"context": 200000, "output": 100000},
        "cost": {"input": 1.1, "output": 4.4, "cache_read": 0.55},
    },
}

# 显式白名单别名映射（严格防止非确定性模糊匹配）
_EXPLICIT_ALIASES: Dict[str, str] = {
    "m3.1-flash": "minimax-m3.1-flash",
    "m3.1": "minimax-m3.1-flash",
    "deepseek-reasoner": "deepseek-r1",
    "claude-3-7-sonnet-latest": "claude-3-7-sonnet",
    "claude-3.7-sonnet": "claude-3-7-sonnet",
    "gpt-4o-latest": "gpt-4o",
}

# 内存单例缓存
_CATALOG_CACHE: Optional[Dict[str, Any]] = None
_CATALOG_VERSION: str = "builtin"


def normalize_model_name(name: str) -> str:
    """
    高精度模型名归一化管道：
    1. 小写，剔除首尾空白
    2. 剔除厂商命名空间前缀（如 minimax/、openai/、anthropic/）
    3. 剔除后缀变体（:free, -preview, -latest, -chat, -online, 纯日期 8 位），严格保留架构代际号
    """
    if not name or not isinstance(name, str):
        return ""
    s = name.strip().lower()
    if "/" in s:
        s = s.split("/")[-1]
    if ":" in s:
        s = s.split(":")[0]
    # 剔除常见 preview/latest/chat/online/free 后缀与纯 8 位日期后缀
    s = re.sub(r"-(preview|latest|chat|online|free)(-\d{8})?$", "", s)
    s = re.sub(r"-\d{8}$", "", s)
    return s


def load_model_catalog(force_reload: bool = False) -> Dict[str, Any]:
    """
    双层加载并缓存模型字典：
    1. 优先读取用户自定义更新路径 data/models_dev.json
    2. 回退读取安装包内置 backend/data/models_dev.json
    3. 极端容灾回退 _FALLBACK_CATALOG
    """
    global _CATALOG_CACHE, _CATALOG_VERSION
    if _CATALOG_CACHE is not None and not force_reload:
        return _CATALOG_CACHE

    target_path = None
    if USER_CUSTOM_PATH.is_file():
        target_path = USER_CUSTOM_PATH
    elif BUNDLED_PATH.is_file():
        target_path = BUNDLED_PATH

    loaded_models: Dict[str, Any] = {}
    if target_path:
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                loaded_models = data.get("models", {})
                _CATALOG_VERSION = data.get("version", target_path.stem)
                logger.info("已加载模型能力目录: %s (共 %d 款)", target_path, len(loaded_models))
        except Exception as e:
            logger.warning("读取模型能力目录失败 %s: %s，回退内置兜底", target_path, e)

    if not loaded_models:
        loaded_models = _FALLBACK_CATALOG
        _CATALOG_VERSION = "fallback"

    _CATALOG_CACHE = loaded_models
    return _CATALOG_CACHE


def lookup_model_spec(model_name: str) -> Optional[Dict[str, Any]]:
    """
    根据用户输入的模型名称查找能力规格。
    查表逻辑：
    1. 归一化输入名；
    2. 检查显式别名映射；
    3. 命中主表即返回字典拷贝；未命中返回 None。
    """
    norm = normalize_model_name(model_name)
    if not norm:
        return None

    catalog = load_model_catalog()

    # 1. 检查别名表
    aliased = _EXPLICIT_ALIASES.get(norm, norm)

    # 2. 精确主表查询
    spec = catalog.get(aliased) or catalog.get(norm)
    if spec:
        return dict(spec)

    # 3. 针对 claude-3-7 等常见点号写法（claude-3.7 -> claude-3-7）做点号转横杠二次对账
    alt_norm = norm.replace(".", "-")
    if alt_norm != norm:
        spec = catalog.get(alt_norm)
        if spec:
            return dict(spec)

    return None


def get_catalog_version() -> str:
    """返回当前模型库的数据版本"""
    global _CATALOG_VERSION
    if _CATALOG_CACHE is None:
        load_model_catalog()
    return _CATALOG_VERSION


def calc_model_cost(
    model_name: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
) -> Optional[Dict[str, Any]]:
    """按 models.dev 挂牌价计算一组用量的成本（USD）。

    口径（复审 R-1）：OpenAI 协议的 prompt_tokens **已包含** cached_tokens，
    所以输入费只对未命中部分按 input 价计，命中部分按 cache_read 价计——
    直接 in×input + cached×cache_read 会把缓存部分收两次费。
    查不到牌价返回 None，由调用方决定未定价如何呈现。
    """
    spec = lookup_model_spec(model_name)
    if not spec or not isinstance(spec.get("cost"), dict):
        return None
    price = spec["cost"]
    in_tok = max(int(input_tokens or 0), 0)
    out_tok = max(int(output_tokens or 0), 0)
    cached = min(max(int(cached_tokens or 0), 0), in_tok)
    uncached = in_tok - cached
    usd = (
        uncached * float(price.get("input", 0) or 0)
        + cached * float(price.get("cache_read", 0) or 0)
        + out_tok * float(price.get("output", 0) or 0)
    ) / 1_000_000
    return {"model": model_name, "usd": usd, "price": dict(price)}


def sync_models_from_remote(url: str = "https://models.dev/api.json", timeout: int = 15) -> Dict[str, Any]:
    """
    在线同步最新的 models.dev 数据并保存到用户数据目录 data/models_dev.json。
    成功后热更新内存缓存。
    """
    global _CATALOG_CACHE, _CATALOG_VERSION

    # 延迟引用抽取函数，避免与 scripts 循环
    from scripts.sync_models_dev import build_curated_specs

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw_data = json.loads(resp.read().decode("utf-8"))

    curated = build_curated_specs(raw_data)
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with open(USER_CUSTOM_PATH, "w", encoding="utf-8") as f:
        json.dump(curated, f, ensure_ascii=False, indent=2)

    _CATALOG_CACHE = curated.get("models", {})
    _CATALOG_VERSION = curated.get("version", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    logger.info("在线更新模型库成功: 共 %d 款模型，版本 %s", len(_CATALOG_CACHE), _CATALOG_VERSION)
    return {
        "success": True,
        "version": _CATALOG_VERSION,
        "total_models": len(_CATALOG_CACHE),
    }
