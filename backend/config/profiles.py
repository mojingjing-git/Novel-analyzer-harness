"""配置方案槽位管理（3 个内置通用上下文预设 + 3 个用户自定义槽）。

分层约束：仅依赖标准库、`backend.config.*`（constants/schema/settings）与
`backend.utils.json_utils`，不引入 core/services，保持导入零副作用。
"""

from __future__ import annotations

import copy
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from .constants import DEFAULT_VOLUME_COMPRESS_GROUP, ROLLING_MAX_MOMENTUM
from .settings import AppConfig, ConfigManager, validate_settings_payload
from ..utils.json_utils import safe_save_json

logger = logging.getLogger(__name__)

PROFILES_FILE_NAME = "config_profiles.json"

BUILTIN_SLOT_IDS: Tuple[str, ...] = ("builtin_128k", "builtin_256k", "builtin_512k")
USER_SLOT_IDS: Tuple[str, ...] = ("user_1", "user_2", "user_3")
ALL_SLOT_IDS: Tuple[str, ...] = BUILTIN_SLOT_IDS + USER_SLOT_IDS

SLOT_ID_ALIASES: Dict[str, str] = {
    "128k": "builtin_128k",
    "256k": "builtin_256k",
    "512k": "builtin_512k",
}

_USER_SLOT_DEFAULT_NAMES: Dict[str, str] = {
    "user_1": "自定义槽 1",
    "user_2": "自定义槽 2",
    "user_3": "自定义槽 3",
}

_MAX_NAME_LEN = 40
_MAX_DESC_LEN = 160

_COMMON_BUILTIN_ANALYSIS: Dict[str, Any] = {
    "block_size": 2,
    "rolling_update_interval": 10,
    "rolling_max_momentum": ROLLING_MAX_MOMENTUM,
    # 紧凑提示词预算（TIGHT_KB）
    "max_summaries_in_prompt": 4,
    "max_arcs_in_prompt": 8,
    "max_character_states": 12,
    "max_world_items": 12,
    "max_foreshadow_entries": 15,
    "max_relationships": 8,
    "max_themes": 5,
    "max_verified_facts_in_prompt": 8,
    "timeline_truncate": 120,
    # 卷摘要压缩组大小归位
    "volume_compress_group": DEFAULT_VOLUME_COMPRESS_GROUP,
    # Stage 2 V3 复检公共基座与残留归位
    "recheck_version": "v3",
    "recheck_v3_mode": "wide",
    "recheck_v3_max_post_windows": 4,
    "recheck_v3_max_pre_windows": 1,
    "recheck_v3_max_hops": 2,
    "recheck_v3_hop2_new_cap": 3,
    "recheck_v3_hop3_new_cap": 0,
    "recheck_v3_max_audit_windows": 0,
    "recheck_v3_nms_cluster_radius": 3,
    "recheck_v3_nms_max_per_cluster": 2,
    "recheck_v3_mid_single_hop": True,
    "recheck_v3_gray_u4": False,
    "recheck_max_calls": 2000,
    "recheck_timeout": 600,
}

BUILTIN_PROFILES: Dict[str, Dict[str, Any]] = {
    "builtin_128k": {
        "slot_index": 1,
        "name": "128K 兼容保底",
        "badge": "128K",
        "recommended": False,
        "description": "适配 128K 上下文或中小模型，控制单批池容量以防超窗并保持稳健并发",
        "patch": {
            "api": {
                "max_tokens": 16384,
                "timeout": 300,
                "summary_timeout": 600,
            },
            "analysis": {
                **_COMMON_BUILTIN_ANALYSIS,
                "concurrency": 6,
                "summary_concurrency": 4,
                "recheck_concurrency": 4,
                "max_content_chars": 45000,
                "summary_batch_size": 40,
                "volume_compress_threshold": 80000,
                "volume_compress_ratio": 0.5,
                "batch_summary_min_words": 1200,
                "final_report_min_words": 3500,
                "style_sample_chapters": 5,
                "style_sample_chars": 1200,
                "style_max_tokens": 16384,
                "atlas_max_tokens": 16384,
                "atlas_max_characters": 50,
                "atlas_max_locations": 40,
                "recheck_max_tokens": 16384,
                "recheck_v3_batch_size": 10,
                "recheck_v3_pool_max_chars": 75000,
                "recheck_v3_prompt_hard_cap_chars": 90000,
                "recheck_v3_u1_superchunk_chars": 52000,
                "recheck_v3_u1_split_batch_size": 50,
                "recheck_v3_u1_route_q_batch": 80,
                "recheck_v3_chapter_chars": 4800,
                "recheck_v3_pre_lookback_units": 30,
            },
        },
    },
    "builtin_256k": {
        "slot_index": 2,
        "name": "256K 甜点推荐",
        "badge": "256K · 推荐",
        "recommended": True,
        "description": "面向 200K–256K 主流上下文模型，兼顾分析质量、复用率与 Token 成本的日常首选",
        "patch": {
            "api": {
                "max_tokens": 32768,
                "timeout": 300,
                "summary_timeout": 600,
            },
            "analysis": {
                **_COMMON_BUILTIN_ANALYSIS,
                "concurrency": 8,
                "summary_concurrency": 6,
                "recheck_concurrency": 6,
                "max_content_chars": 95000,
                "summary_batch_size": 45,
                "volume_compress_threshold": 180000,
                "volume_compress_ratio": 0.75,
                "batch_summary_min_words": 1500,
                "final_report_min_words": 4000,
                "style_sample_chapters": 6,
                "style_sample_chars": 1500,
                "style_max_tokens": 20000,
                "atlas_max_tokens": 32768,
                "atlas_max_characters": 60,
                "atlas_max_locations": 50,
                "recheck_max_tokens": 32768,
                "recheck_v3_batch_size": 20,
                "recheck_v3_pool_max_chars": 180000,
                "recheck_v3_prompt_hard_cap_chars": 205000,
                "recheck_v3_u1_superchunk_chars": 150000,
                "recheck_v3_u1_split_batch_size": 100,
                "recheck_v3_u1_route_q_batch": 150,
                "recheck_v3_chapter_chars": 6000,
                "recheck_v3_pre_lookback_units": 30,
            },
        },
    },
    "builtin_512k": {
        "slot_index": 3,
        "name": "512K 超长巨著",
        "badge": "512K",
        "recommended": False,
        "description": "面向 ≥512K 上下文模型与超长篇作品，放宽卷摘要压缩阈值并扩大共享证据池",
        "patch": {
            "api": {
                "max_tokens": 32768,
                "timeout": 300,
                "summary_timeout": 600,
            },
            "analysis": {
                **_COMMON_BUILTIN_ANALYSIS,
                "concurrency": 10,
                "summary_concurrency": 6,
                "recheck_concurrency": 6,
                "max_content_chars": 95000,
                "summary_batch_size": 50,
                "volume_compress_threshold": 350000,
                "volume_compress_ratio": 1.0,
                "batch_summary_min_words": 1800,
                "final_report_min_words": 4500,
                "style_sample_chapters": 6,
                "style_sample_chars": 1500,
                "style_max_tokens": 20000,
                "atlas_max_tokens": 32768,
                "atlas_max_characters": 60,
                "atlas_max_locations": 50,
                "recheck_max_tokens": 32768,
                "recheck_v3_batch_size": 32,
                "recheck_v3_pool_max_chars": 360000,
                "recheck_v3_prompt_hard_cap_chars": 410000,
                "recheck_v3_u1_superchunk_chars": 300000,
                "recheck_v3_u1_split_batch_size": 120,
                "recheck_v3_u1_route_q_batch": 180,
                "recheck_v3_chapter_chars": 6800,
                "recheck_v3_pre_lookback_units": 35,
            },
        },
    },
}

_PATH_LOCKS: Dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class ProfileError(Exception):
    """配置槽操作业务异常，供 HTTP 路由与 CLI 统一映射状态码/退出码。"""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def normalize_slot_id(raw: str) -> str:
    """规范化槽位 ID，将 `128k` / `256k` / `512k`（大小写不敏感）映射至内置槽位全名。"""
    key = (raw or "").strip()
    return SLOT_ID_ALIASES.get(key.lower(), key)


def mask_profile_url(url: str) -> str:
    """剥离 URL 内嵌 userinfo 与 query/fragment，仅返回安全展示用的基址。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except Exception:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""
    host = parsed.hostname or ""
    if not host:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _normalize_origin(url: str) -> str:
    """提取用于判断端点是否跨源的规范化键。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw
    if not parsed.scheme or not parsed.netloc:
        return raw
    host = (parsed.hostname or "").lower()
    scheme = parsed.scheme.lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else (80 if scheme == "http" else None)
    return f"{scheme}://{host}:{port if port is not None else ''}"


def _current_env_api_key() -> str:
    return os.environ.get("LLM_API_KEY") or os.environ.get("MINIMAX_API_KEY") or ""


def _values_equal(a: Any, b: Any) -> bool:
    """递归比较配置值，严格区分 bool 与 int 并处理浮点精度误差。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=1e-9)
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_values_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_values_equal(x, y) for x, y in zip(a, b))
    return a == b


def _lock_for_path(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _count_patch_fields(patch: Dict[str, Any]) -> int:
    total = 0
    for sec in ("api", "analysis"):
        sec_obj = patch.get(sec)
        if isinstance(sec_obj, dict):
            total += len(sec_obj)
    return total


def _build_summary(patch: Dict[str, Any], *, is_user: bool) -> Dict[str, Any]:
    api_sec = patch.get("api") if isinstance(patch.get("api"), dict) else {}
    ana_sec = patch.get("analysis") if isinstance(patch.get("analysis"), dict) else {}
    summary: Dict[str, Any] = {
        "model": str(api_sec.get("model") or ""),
        "summary_model": str(api_sec.get("summary_model") or ""),
        "recheck_model": str(ana_sec.get("recheck_model") or ""),
        "block_size": int(ana_sec.get("block_size") or 0),
        "concurrency": int(ana_sec.get("concurrency") or 0),
        "summary_concurrency": int(ana_sec.get("summary_concurrency") or 0),
        "recheck_concurrency": int(ana_sec.get("recheck_concurrency") or 0),
        "summary_batch_size": int(ana_sec.get("summary_batch_size") or 0),
        "recheck_version": str(ana_sec.get("recheck_version") or ""),
        "recheck_v3_mode": str(ana_sec.get("recheck_v3_mode") or ""),
        "recheck_v3_pool_max_chars": int(ana_sec.get("recheck_v3_pool_max_chars") or 0),
        "recheck_v3_batch_size": int(ana_sec.get("recheck_v3_batch_size") or 0),
        "recheck_v3_max_hops": int(ana_sec.get("recheck_v3_max_hops") or 0),
        "volume_compress_threshold": int(ana_sec.get("volume_compress_threshold") or 0),
    }
    if is_user:
        summary["base_url"] = mask_profile_url(str(api_sec.get("base_url") or ""))
    return summary


def _patch_matches_config(patch: Dict[str, Any], cfg_dict: Dict[str, Any]) -> bool:
    for sec in ("api", "analysis"):
        sec_patch = patch.get(sec)
        if not isinstance(sec_patch, dict):
            continue
        sec_cfg = cfg_dict.get(sec)
        if not isinstance(sec_cfg, dict):
            return False
        for k, expected in sec_patch.items():
            if k not in sec_cfg or not _values_equal(sec_cfg[k], expected):
                return False
    return True


def _slots_have_credentials(slots_data: Dict[str, Any]) -> bool:
    for entry in slots_data.values():
        if not isinstance(entry, dict):
            continue
        patch = entry.get("patch")
        if not isinstance(patch, dict):
            continue
        api_sec = patch.get("api")
        ana_sec = patch.get("analysis")
        if isinstance(api_sec, dict) and str(api_sec.get("api_key") or "").strip():
            return True
        if isinstance(ana_sec, dict) and str(ana_sec.get("recheck_api_key") or "").strip():
            return True
    return False


def _sanitize_user_patch(raw_patch: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw_patch, dict):
        return None
    has_api = isinstance(raw_patch.get("api"), dict) and bool(raw_patch.get("api"))
    has_ana = isinstance(raw_patch.get("analysis"), dict) and bool(raw_patch.get("analysis"))
    if not (has_api or has_ana):
        return None
    # 使用 AppConfig.from_dict 过滤未知/废弃键并强转类型，再保留显式提供的字段
    norm = AppConfig.from_dict(raw_patch).to_dict()
    cleaned: Dict[str, Any] = {"api": {}, "analysis": {}}
    for sec in ("api", "analysis"):
        raw_sec = raw_patch.get(sec)
        if isinstance(raw_sec, dict):
            for k in raw_sec:
                if k in norm[sec]:
                    cleaned[sec][k] = copy.deepcopy(norm[sec][k])
    if not cleaned["api"] and not cleaned["analysis"]:
        return None
    return cleaned


class ProfileStore:
    """基于 runtime/config_profiles.json 的 6 槽配置持久化管理器。"""

    def __init__(self, profiles_path: Path) -> None:
        self.profiles_path = profiles_path
        self._lock = _lock_for_path(profiles_path)

    @classmethod
    def for_config_manager(cls, config_manager: ConfigManager) -> "ProfileStore":
        return cls(config_manager.config_path.parent / PROFILES_FILE_NAME)

    def _read_disk(self) -> Tuple[Dict[str, Any], bool]:
        empty_state = {"version": 1, "last_applied_slot_id": None, "slots": {}}
        if not self.profiles_path.exists():
            return empty_state, False
        try:
            with open(self.profiles_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("顶层必须为 JSON 对象")
            raw_slots = data.get("slots", {})
            if raw_slots is None:
                raw_slots = {}
            if not isinstance(raw_slots, dict):
                raise ValueError("slots 必须为 JSON 对象")
            last_id = data.get("last_applied_slot_id")
            if last_id is not None and (not isinstance(last_id, str) or last_id not in ALL_SLOT_IDS):
                last_id = None
            return {
                "version": int(data.get("version") or 1),
                "last_applied_slot_id": last_id,
                "slots": copy.deepcopy(raw_slots),
            }, False
        except Exception as exc:
            logger.error("配置槽文件加载失败（%s）: %s", self.profiles_path, exc)
            return empty_state, True

    def _write_disk(self, data: Dict[str, Any]) -> None:
        ok = safe_save_json(data, self.profiles_path, ensure_ascii=False)
        if not ok:
            raise ProfileError(500, "profile_save_failed", f"写入配置槽文件失败: {self.profiles_path}")
        if os.name != "nt" and _slots_have_credentials(data.get("slots") or {}):
            try:
                os.chmod(self.profiles_path, 0o600)
            except OSError:
                pass

    def _compare_dict_for_config(self, config_manager: ConfigManager, cfg: AppConfig) -> Dict[str, Any]:
        cfg_dict = cfg.to_dict()
        if getattr(config_manager, "_api_key_from_env", False):
            compare = copy.deepcopy(cfg_dict)
            compare["api"]["api_key"] = str(getattr(config_manager, "_disk_api_key", "") or "")
            return compare
        return cfg_dict

    def _build_state_unlocked(self, config_manager: ConfigManager, cfg: AppConfig) -> Dict[str, Any]:
        disk, store_corrupted = self._read_disk()
        raw_slots: Dict[str, Any] = disk.get("slots") or {}
        last_applied_slot_id: Optional[str] = disk.get("last_applied_slot_id")
        compare_dict = self._compare_dict_for_config(config_manager, cfg)

        candidates: List[Tuple[str, bool]] = []
        matches_by_id: Dict[str, bool] = {}
        populated_ids: set[str] = set()
        raw_entries: List[Dict[str, Any]] = []

        for idx, slot_id in enumerate(BUILTIN_SLOT_IDS, start=1):
            meta = BUILTIN_PROFILES[slot_id]
            patch = meta["patch"]
            matched = _patch_matches_config(patch, compare_dict)
            matches_by_id[slot_id] = matched
            populated_ids.add(slot_id)
            if matched:
                candidates.append((slot_id, False))
            raw_entries.append(
                {
                    "id": slot_id,
                    "kind": "builtin",
                    "slot_index": meta["slot_index"],
                    "name": meta["name"],
                    "description": meta["description"],
                    "badge": meta["badge"],
                    "recommended": bool(meta["recommended"]),
                    "populated": True,
                    "matches_current": matched,
                    "include_credentials": False,
                    "has_api_key": False,
                    "has_recheck_api_key": False,
                    "updated_at": None,
                    "summary": _build_summary(patch, is_user=False),
                    "managed_field_count": _count_patch_fields(patch),
                }
            )

        for idx, slot_id in enumerate(USER_SLOT_IDS, start=4):
            user_idx = idx - 3
            raw_entry = raw_slots.get(slot_id)
            patch = _sanitize_user_patch(raw_entry.get("patch")) if isinstance(raw_entry, dict) else None
            if isinstance(raw_entry, dict) and patch is not None:
                populated = True
                populated_ids.add(slot_id)
                matched = _patch_matches_config(patch, compare_dict)
                matches_by_id[slot_id] = matched
                if matched:
                    candidates.append((slot_id, True))
                name = str(raw_entry.get("name") or "").strip() or _USER_SLOT_DEFAULT_NAMES[slot_id]
                desc = str(raw_entry.get("description") or "").strip()
                include_creds = bool(raw_entry.get("include_credentials", False))
                api_sec = patch.get("api") or {}
                ana_sec = patch.get("analysis") or {}
                has_api_key = bool(str(api_sec.get("api_key") or "").strip())
                has_recheck_key = bool(str(ana_sec.get("recheck_api_key") or "").strip())
                updated_at = str(raw_entry.get("updated_at") or "").strip() or None
                summary = _build_summary(patch, is_user=True)
                field_count = _count_patch_fields(patch)
            else:
                populated = False
                matched = False
                matches_by_id[slot_id] = False
                name = _USER_SLOT_DEFAULT_NAMES[slot_id]
                desc = ""
                include_creds = False
                has_api_key = False
                has_recheck_key = False
                updated_at = None
                summary = None
                field_count = 0

            raw_entries.append(
                {
                    "id": slot_id,
                    "kind": "user",
                    "slot_index": idx,
                    "name": name,
                    "description": desc,
                    "badge": f"自定义 {user_idx}" if populated else f"空槽 {user_idx}",
                    "recommended": False,
                    "populated": populated,
                    "matches_current": matched,
                    "include_credentials": include_creds,
                    "has_api_key": has_api_key,
                    "has_recheck_api_key": has_recheck_key,
                    "updated_at": updated_at,
                    "summary": summary,
                    "managed_field_count": field_count,
                }
            )

        active_slot_id: Optional[str] = None
        if candidates:
            matching_ids = [sid for sid, _ in candidates]
            if last_applied_slot_id and last_applied_slot_id in matching_ids:
                active_slot_id = last_applied_slot_id
            else:
                user_matches = [sid for sid, is_u in candidates if is_u]
                active_slot_id = user_matches[0] if user_matches else matching_ids[0]

        for item in raw_entries:
            item["is_active"] = item["id"] == active_slot_id

        is_modified_from_last = bool(
            last_applied_slot_id
            and last_applied_slot_id in populated_ids
            and not matches_by_id.get(last_applied_slot_id, False)
        )

        return {
            "active_slot_id": active_slot_id,
            "last_applied_slot_id": last_applied_slot_id,
            "is_modified_from_last": is_modified_from_last,
            "store_corrupted": store_corrupted,
            "slots": raw_entries,
        }

    def list_profiles(self, config_manager: ConfigManager) -> Dict[str, Any]:
        with self._lock:
            cfg = config_manager.load()
            return self._build_state_unlocked(config_manager, cfg)

    def save_user_slot(
        self,
        config_manager: ConfigManager,
        slot_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        include_credentials: bool = False,
        raw_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        slot_id = normalize_slot_id(slot_id)
        if slot_id in BUILTIN_SLOT_IDS:
            raise ProfileError(400, "readonly_slot", f"内置默认槽位 {slot_id} 为只读预设，不可修改")
        if slot_id not in USER_SLOT_IDS:
            raise ProfileError(404, "slot_not_found", f"未知配置槽位: {slot_id}")

        clean_name = (name or "").strip() if name is not None else ""
        if not clean_name:
            clean_name = _USER_SLOT_DEFAULT_NAMES[slot_id]
        if len(clean_name) > _MAX_NAME_LEN:
            raise ProfileError(
                422,
                "validation_error",
                f"槽位名称长度不能超过 {_MAX_NAME_LEN} 个字符",
                details=[{"loc": ["body", "name"], "msg": f"长度不能超过 {_MAX_NAME_LEN} 个字符"}],
            )
        clean_desc = (description or "").strip() if description is not None else ""
        if len(clean_desc) > _MAX_DESC_LEN:
            raise ProfileError(
                422,
                "validation_error",
                f"槽位说明长度不能超过 {_MAX_DESC_LEN} 个字符",
                details=[{"loc": ["body", "description"], "msg": f"长度不能超过 {_MAX_DESC_LEN} 个字符"}],
            )

        with self._lock:
            disk, store_corrupted = self._read_disk()
            if store_corrupted:
                raise ProfileError(
                    409,
                    "store_corrupted",
                    "config_profiles.json 解析失败待修复，拒绝覆盖自定义槽数据",
                )

            cur_cfg = config_manager.load()
            env_key = _current_env_api_key()

            if raw_config is None:
                if getattr(config_manager, "_load_failed", False):
                    raise ProfileError(
                        409,
                        "config_corrupted",
                        "config.json 解析失败尚未修复，无法从当前磁盘配置抓取快照",
                    )
                norm_dict = cur_cfg.to_dict()
            else:
                if not isinstance(raw_config, dict):
                    raise ProfileError(
                        422,
                        "validation_error",
                        "config 必须为配置对象",
                        details=[{"loc": ["body", "config"], "msg": "config 必须为对象"}],
                    )
                has_api = isinstance(raw_config.get("api"), dict) and len(raw_config.get("api") or {}) > 0
                has_ana = isinstance(raw_config.get("analysis"), dict) and len(raw_config.get("analysis") or {}) > 0
                if not (has_api or has_ana):
                    raise ProfileError(
                        422,
                        "validation_error",
                        "config 必须包含非空的 api 或 analysis 配置节",
                        details=[{"loc": ["body", "config"], "msg": "config 必须包含非空的 api 或 analysis 配置节"}],
                    )
                errors = validate_settings_payload(raw_config)
                if errors:
                    raise ProfileError(422, "validation_error", "配置参数校验未通过", details=errors)
                norm_dict = AppConfig.from_dict(raw_config).to_dict()

            patch: Dict[str, Any] = {
                "api": copy.deepcopy(norm_dict["api"]),
                "analysis": copy.deepcopy(norm_dict["analysis"]),
            }

            warnings: List[str] = []
            if not include_credentials:
                patch["api"].pop("api_key", None)
                patch["analysis"].pop("recheck_api_key", None)
            else:
                cand_key = str(patch["api"].get("api_key") or "")
                if raw_config is None and getattr(config_manager, "_api_key_from_env", False):
                    patch["api"].pop("api_key", None)
                    warnings.append("当前主 API Key 来自环境变量，已跳过写入槽位文件")
                elif env_key and cand_key == env_key:
                    patch["api"].pop("api_key", None)
                    warnings.append("检测到主 API Key 与当前环境变量一致，已跳过落盘")

            now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
            slots = dict(disk.get("slots") or {})
            slots[slot_id] = {
                "name": clean_name,
                "description": clean_desc,
                "include_credentials": bool(include_credentials),
                "updated_at": now_iso,
                "patch": patch,
            }
            disk["slots"] = slots
            self._write_disk(disk)

            profiles = self._build_state_unlocked(config_manager, cur_cfg)
            saved_slot = next(s for s in profiles["slots"] if s["id"] == slot_id)
            return {
                "slot": saved_slot,
                "warnings": warnings,
                "profiles": profiles,
            }

    def clear_user_slot(self, config_manager: ConfigManager, slot_id: str) -> Dict[str, Any]:
        slot_id = normalize_slot_id(slot_id)
        if slot_id in BUILTIN_SLOT_IDS:
            raise ProfileError(400, "readonly_slot", f"内置默认槽位 {slot_id} 不可清空")
        if slot_id not in USER_SLOT_IDS:
            raise ProfileError(404, "slot_not_found", f"未知配置槽位: {slot_id}")

        with self._lock:
            disk, store_corrupted = self._read_disk()
            if store_corrupted:
                raise ProfileError(
                    409,
                    "store_corrupted",
                    "config_profiles.json 解析失败待修复，拒绝写盘覆盖",
                )
            slots = dict(disk.get("slots") or {})
            slots.pop(slot_id, None)
            disk["slots"] = slots
            if disk.get("last_applied_slot_id") == slot_id:
                disk["last_applied_slot_id"] = None
            self._write_disk(disk)

            cfg = config_manager.load()
            profiles = self._build_state_unlocked(config_manager, cfg)
            return {
                "cleared": slot_id,
                "profiles": profiles,
            }

    def apply_slot(self, config_manager: ConfigManager, slot_id: str) -> Dict[str, Any]:
        slot_id = normalize_slot_id(slot_id)
        if slot_id not in ALL_SLOT_IDS:
            raise ProfileError(404, "slot_not_found", f"未知配置槽位: {slot_id}")

        with self._lock:
            disk, store_corrupted = self._read_disk()
            cur_cfg = config_manager.load()
            if getattr(config_manager, "_load_failed", False):
                raise ProfileError(
                    409,
                    "config_corrupted",
                    "config.json 解析失败尚未修复，请先修复该文件后重试",
                )

            include_credentials = False
            if slot_id in BUILTIN_SLOT_IDS:
                raw_patch = BUILTIN_PROFILES[slot_id]["patch"]
            else:
                if store_corrupted:
                    raise ProfileError(
                        409,
                        "store_corrupted",
                        "config_profiles.json 解析失败待修复，无法应用自定义槽",
                    )
                entry = (disk.get("slots") or {}).get(slot_id)
                raw_patch = _sanitize_user_patch(entry.get("patch")) if isinstance(entry, dict) else None
                if not isinstance(entry, dict) or raw_patch is None:
                    raise ProfileError(400, "empty_slot", f"自定义槽位 {slot_id} 为空，请先存入配置")
                include_credentials = bool(entry.get("include_credentials", False))

            effective_patch = copy.deepcopy(raw_patch)
            warnings: List[str] = []

            if slot_id in USER_SLOT_IDS:
                api_p = effective_patch.setdefault("api", {})
                ana_p = effective_patch.setdefault("analysis", {})
                if not include_credentials:
                    api_p.pop("api_key", None)
                    ana_p.pop("recheck_api_key", None)

                    cur_recheck_base = str(cur_cfg.analysis.recheck_base_url or "").strip()
                    tgt_recheck_base = str(ana_p.get("recheck_base_url") or "").strip()
                    cur_recheck_norm = _normalize_origin(cur_recheck_base) or cur_recheck_base
                    tgt_recheck_norm = _normalize_origin(tgt_recheck_base) or tgt_recheck_base
                    if cur_recheck_norm != tgt_recheck_norm:
                        ana_p["recheck_api_key"] = ""
                        if str(cur_cfg.analysis.recheck_api_key or "").strip():
                            warnings.append("独立复检端点已变更，已自动清空旧的复检 API Key 以防跨端点发送")

                    cur_base = str(cur_cfg.api.base_url or "").strip()
                    tgt_base = str(api_p.get("base_url") or "").strip()
                    cur_base_norm = _normalize_origin(cur_base) or cur_base
                    tgt_base_norm = _normalize_origin(tgt_base) or tgt_base
                    if cur_base_norm != tgt_base_norm:
                        warnings.append("主 API Base URL 已变更且该槽未保存密钥，请确认当前 API Key 与新端点匹配")
                else:
                    if getattr(config_manager, "_api_key_from_env", False) and str(api_p.get("api_key") or "").strip():
                        warnings.append("当前环境变量 LLM_API_KEY / MINIMAX_API_KEY 生效中，运行时仍优先使用环境变量密钥")

            updated = config_manager.patch(effective_patch)
            if updated is None:
                raise ProfileError(
                    409,
                    "config_save_failed",
                    "配置未能写入：config.json 可能解析失败待修复，请手工修复后重试",
                )

            if _current_env_api_key():
                updated = config_manager.load()

            if not store_corrupted:
                disk["last_applied_slot_id"] = slot_id
                self._write_disk(disk)

            profiles = self._build_state_unlocked(config_manager, updated)
            return {
                "applied_slot_id": slot_id,
                "config": updated.to_dict(),
                "warnings": warnings,
                "profiles": profiles,
            }
