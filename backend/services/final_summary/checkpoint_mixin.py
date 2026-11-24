"""Stage 1 / Stage 2 / Stage 3 断点指纹校验与读写 Mixin（Layer 2）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.utils.json_utils import safe_load_json, safe_save_json

from .parsers import recheck_channel, validate_volume_summary

logger = logging.getLogger(__name__)


class CheckpointMixin:
    """最终总结三阶段断点续跑（卷摘要+对账、伏笔复检、风格画像）。"""

    _PRESERVED_CHECKPOINT_FILES = frozenset({"style_profile.json", "recheck_v3_u1_cache.json"})

    @property
    def _checkpoint_dir(self) -> Path:
        return self.output_dir / "final_summary_checkpoint"

    def _resolve_blocks_dir(self) -> Optional[Path]:
        blocks_dir = self.output_dir.parent / "blocks"
        if not blocks_dir.exists() and self.config.working_directory:
            blocks_dir = Path(self.config.working_directory) / "blocks"
        return blocks_dir

    def _compute_results_fingerprint(self) -> str:
        """章节数据版本指纹（name:mtime_ns:size），用于断点失效判断。"""
        parts = []
        for f in sorted(self.output_dir.glob("chapter_*_result.json")):
            try:
                st = f.stat()
                parts.append(f"{f.name}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append(f"{f.name}:missing")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def _compute_blocks_fingerprint(self) -> str:
        """正文文件集合指纹（blocks/*.txt 按数字序号排序）。无正文目录或文件返回空串。"""
        blocks_dir = self._resolve_blocks_dir()
        if blocks_dir is None or not blocks_dir.is_dir():
            return ""
        txt_files = sorted(
            (f for f in blocks_dir.glob("*.txt") if re.match(r"^\d+\.txt$", f.name)),
            key=lambda f: int(f.stem),
        )
        if not txt_files:
            return ""
        parts = []
        for f in txt_files:
            try:
                st = f.stat()
                parts.append(f"{f.name}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append(f"{f.name}:missing")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def _compute_stage1_fingerprint(self, results_fp: str) -> str:
        """Stage 1 完整失效指纹：results_fp + 章节范围 + 批大小 + 伏笔过滤口径。"""
        a = self.config.analysis
        payload = {
            "results_fp": results_fp,
            "start_chapter": self.start_chapter,
            "end_chapter": self.end_chapter,
            "batch_size": self.batch_size,
            "foreshadow_min_importance": getattr(a, "foreshadow_min_importance", "中"),
            "foreshadow_min_confidence": getattr(a, "foreshadow_min_confidence", "中"),
            "foreshadow_kept_categories": sorted(getattr(a, "foreshadow_kept_categories", []) or []),
            "max_foreshadow_catalog_high": int(getattr(a, "max_foreshadow_catalog_high", 500)),
            "max_foreshadow_catalog_mid": int(getattr(a, "max_foreshadow_catalog_mid", 200)),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    def _compute_stage2_fingerprint(self, stage1_fp: str, blocks_fp: str, recheck_ver: str) -> str:
        """Stage 2 复检失效指纹：stage1_fp + blocks_fp + 引擎版本 + 复检通道 + block_size + V3 开关。"""
        a = self.config.analysis
        rc_model, rc_base = recheck_channel(self.config)
        is_v3 = recheck_ver == "v3"
        payload = {
            "stage1_fp": stage1_fp,
            "blocks_fp": blocks_fp,
            "recheck_version": recheck_ver,
            "recheck_model": rc_model,
            "recheck_base_url": rc_base,
            "block_size": int(getattr(a, "block_size", 4) or 4),
            "recheck_v3_gray_u4": bool(getattr(a, "recheck_v3_gray_u4", False)) if is_v3 else False,
            "recheck_v3_mid_single_hop": bool(getattr(a, "recheck_v3_mid_single_hop", False)) if is_v3 else False,
            "v3_engine_rev": "tier2_pool_v1" if is_v3 else "v2",
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    def _compute_style_fingerprint(self, blocks_fp: str) -> str:
        """Stage 3 风格提取失效指纹：blocks_fp + 总结模型 + 协议。"""
        api = self.config.api
        payload = {
            "blocks_fp": blocks_fp,
            "model": str(getattr(api, "summary_model", "") or getattr(api, "model", "") or ""),
            "provider": str(getattr(api, "provider", "auto") or "auto"),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _is_valid_style_profile(profile: Any) -> bool:
        """校验风格画像完整性：统计与 8 维语义均有效且不含降级失败占位文本。"""
        if not isinstance(profile, dict):
            return False
        stat = profile.get("statistical")
        sem = profile.get("semantic")
        if not isinstance(stat, dict) or not stat or not isinstance(sem, dict) or not sem:
            return False
        for v in sem.values():
            if not isinstance(v, str) or not v.strip() or "提取失败" in v or "未提取" in v:
                return False
        return True

    def _wipe_checkpoint_dir(self) -> None:
        """清理失效的 Stage 1/2 断点文件，保留独立指纹校验的 style_profile.json 与 recheck_v3_u1_cache.json。"""
        cp = self._checkpoint_dir
        if not cp.exists():
            return
        try:
            for child in list(cp.iterdir()):
                if child.name in self._PRESERVED_CHECKPOINT_FILES and child.is_file():
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink(missing_ok=True)
                    except OSError:
                        pass
            logger.warning(f"已清空失效的总结断点文件（保留独立缓存）: {cp}")
        except OSError as exc:
            logger.warning(f"清空总结断点目录失败: {exc}")

    def _load_stage1_baseline(self, stage1_fp: str) -> Optional[dict]:
        path = self._checkpoint_dir / "stage1_ledger_baseline.json"
        if not path.exists():
            return None
        try:
            data = safe_load_json(path)
        except OSError as exc:
            logger.warning(f"读取 stage1_ledger_baseline.json 失败: {exc}")
            return None
        if (
            not isinstance(data, dict)
            or data.get("stage1_fp") != stage1_fp
            or not isinstance(data.get("ledger"), dict)
        ):
            return None
        return data

    def _save_stage1_baseline(self, stage1_fp: str) -> None:
        cp = self._checkpoint_dir
        try:
            cp.mkdir(parents=True, exist_ok=True)
            safe_save_json(
                {"stage1_fp": stage1_fp, "ledger": self.ledger.to_dict()},
                cp / "stage1_ledger_baseline.json",
                ensure_ascii=False,
            )
        except OSError as exc:
            logger.warning(f"写入 stage1_ledger_baseline.json 失败: {exc}")

    def _load_stage2_checkpoint(self, stage2_fp: str) -> Optional[dict]:
        path = self._checkpoint_dir / "stage2_recheck_state.json"
        if not path.exists():
            return None
        try:
            data = safe_load_json(path)
        except OSError as exc:
            logger.warning(f"读取 stage2_recheck_state.json 失败: {exc}")
            return None
        if (
            not isinstance(data, dict)
            or data.get("stage2_fp") != stage2_fp
            or not isinstance(data.get("ledger"), dict)
            or not isinstance(data.get("recheck_summary"), dict)
        ):
            return None
        return data

    def _save_stage2_checkpoint(self, stage2_fp: str) -> None:
        cp = self._checkpoint_dir
        try:
            cp.mkdir(parents=True, exist_ok=True)
            safe_save_json(
                {
                    "stage2_fp": stage2_fp,
                    "ledger": self.ledger.to_dict(),
                    "recheck_summary": dict(getattr(self, "last_recheck_summary", None) or {}),
                },
                cp / "stage2_recheck_state.json",
                ensure_ascii=False,
            )
        except OSError as exc:
            logger.warning(f"写入 stage2_recheck_state.json 失败: {exc}")

    def _load_style_checkpoint(self, style_fp: str) -> Optional[dict]:
        path = self._checkpoint_dir / "style_profile.json"
        if not path.exists():
            return None
        try:
            data = safe_load_json(path)
        except OSError as exc:
            logger.warning(f"读取 style_profile.json 失败: {exc}")
            return None
        if not isinstance(data, dict) or data.get("style_fp") != style_fp:
            return None
        profile = data.get("profile")
        if not self._is_valid_style_profile(profile):
            return None
        return profile

    def _save_style_checkpoint(self, style_fp: str, profile: dict) -> None:
        cp = self._checkpoint_dir
        try:
            cp.mkdir(parents=True, exist_ok=True)
            safe_save_json(
                {"style_fp": style_fp, "profile": profile},
                cp / "style_profile.json",
                ensure_ascii=False,
            )
        except OSError as exc:
            logger.warning(f"写入 style_profile.json 失败: {exc}")

    async def _save_checkpoint_batch(self, idx: int, ch_start: int, ch_end: int,
                                     summary_text: Optional[str], recon_data: Optional[dict],
                                     stage1_fp: Optional[str] = None) -> None:
        """落盘单个批次的断点（卷摘要与 reconciliation 各自独立写，两步互不阻塞）。"""
        cp = self._checkpoint_dir
        try:
            await asyncio.to_thread(cp.mkdir, parents=True, exist_ok=True)
            vf = cp / f"volume_{idx}.md"
            rf = cp / f"recon_{idx}.json"
            async with self._checkpoint_lock:
                if summary_text is not None:
                    await asyncio.to_thread(vf.write_text, summary_text, encoding="utf-8")
                if recon_data is not None:
                    await asyncio.to_thread(safe_save_json, recon_data, rf)
                manifest_path = cp / "manifest.json"
                m = await asyncio.to_thread(safe_load_json, manifest_path)
                if not isinstance(m, dict):
                    m = {"batch_size": self.batch_size, "batches": {}}
                m["batch_size"] = self.batch_size
                m["results_fingerprint"] = await asyncio.to_thread(self._compute_results_fingerprint)
                if stage1_fp is not None:
                    m["stage1_fp"] = stage1_fp
                entry = m.setdefault("batches", {}).setdefault(str(idx), {})
                entry["start"] = ch_start
                entry["end"] = ch_end
                if vf.exists():
                    entry["summary"] = f"volume_{idx}.md"
                if rf.exists():
                    entry["recon"] = f"recon_{idx}.json"
                m["batches"][str(idx)] = entry
                await asyncio.to_thread(safe_save_json, m, manifest_path)
        except OSError as exc:
            logger.warning(f"写入批次 {idx} 总结断点失败: {exc}")

    async def _load_checkpoint(self, batch_tasks: List[tuple],
                               stage1_fp: Optional[str] = None) -> Tuple[Dict[int, str], Dict[int, dict]]:
        """从断点恢复已完成批次。"""
        cp = self._checkpoint_dir
        manifest_path = cp / "manifest.json"
        if not manifest_path.exists():
            return {}, {}
        try:
            m = await asyncio.to_thread(safe_load_json, manifest_path)
            current_fp = await asyncio.to_thread(self._compute_results_fingerprint)
        except OSError as exc:
            logger.warning(f"读取总结断点 manifest 失败: {exc}")
            return {}, {}
        fp_mismatch = (
            not isinstance(m, dict)
            or m.get("batch_size") != self.batch_size
            or m.get("results_fingerprint") != current_fp
            or (stage1_fp is not None and m.get("stage1_fp") is not None and m.get("stage1_fp") != stage1_fp)
        )
        if fp_mismatch:
            reason = ("manifest损坏" if not isinstance(m, dict)
                      else f"batch_size变化({m.get('batch_size')}->{self.batch_size})"
                      if m.get("batch_size") != self.batch_size
                      else "章节数据或配置口径已变化")
            logger.info(f"总结断点失效（{reason}），清空旧断点文件重新生成")
            await asyncio.to_thread(self._wipe_checkpoint_dir)
            return {}, {}
        batches = m.get("batches") or {}
        volume_results: Dict[int, str] = {}
        recon_results: Dict[int, dict] = {}
        for idx, ch_start, ch_end, _prompt in batch_tasks:
            b = batches.get(str(idx))
            if not isinstance(b, dict):
                continue
            if b.get("start") != ch_start or b.get("end") != ch_end:
                logger.debug(f"批次{idx} ch 范围与断点({b.get('start')}-{b.get('end')})不匹配，忽略")
                continue
            if b.get("summary"):
                vf = cp / b["summary"]
                if vf.exists():
                    try:
                        text = await asyncio.to_thread(vf.read_text, encoding="utf-8")
                        ok_vol, vol_err = validate_volume_summary(text)
                        if ok_vol:
                            volume_results[idx] = text
                        else:
                            logger.warning(f"忽略失效或拒答卷摘要断点 {vf.name}: {vol_err}")
                    except Exception:
                        pass
            if b.get("recon"):
                rf = cp / b["recon"]
                if rf.exists():
                    try:
                        rd = await asyncio.to_thread(safe_load_json, rf)
                        if isinstance(rd, dict):
                            recon_results[idx] = rd
                    except Exception:
                        pass
        return volume_results, recon_results
