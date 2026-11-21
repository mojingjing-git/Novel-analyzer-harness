"""
知识库管理器
负责知识库的加载、保存、更新和压缩
（纯 dict/文件操作无 LLM 调用，同步保留，pipeline 中经 asyncio.to_thread 调用）
"""

import json
import logging
import re
import threading
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from datetime import datetime

from ..models.knowledge import KnowledgeBase
from ..models.analysis_result import AnalysisResult
from ..utils.json_utils import safe_save_json, safe_load_json
from ..config.constants import (
    MAX_COMPRESSED_ARCS, MAX_RECENT_SUMMARIES,
    MAX_FORESIGHT_NETWORK,
    MAX_WORLD_BUILDING,
    MAX_VERIFIED_FACTS, MAX_LONG_TERM_ARCS,
    MAX_THEMATIC_ELEMENTS,
    ROLLING_FILE_NAME,
)

logger = logging.getLogger(__name__)

# build_temp_knowledge 内存缓存（线程安全）
# key: (str(output_dir), chapter_limit)
# value: (frozenset of (filename, mtime_ns), list of AnalysisResult, KnowledgeBase)
_TEMP_KB_CACHE_MAX = 8
_temp_kb_cache: Dict[Tuple, Tuple] = {}
_temp_kb_lock = threading.Lock()


def _cache_put(key: Tuple, value: Tuple) -> None:
    """写入缓存条目，超限时淘汰最旧的（FIFO，按 dict 插入序）。须在 _temp_kb_lock 内调用。"""
    _temp_kb_cache[key] = value
    while len(_temp_kb_cache) > _TEMP_KB_CACHE_MAX:
        oldest = next(iter(_temp_kb_cache))
        if oldest == key:
            break  # 极端情况：只剩刚插入的，不淘汰自己
        _temp_kb_cache.pop(oldest)
        logger.debug(f"build_temp_knowledge 缓存淘汰旧条目 (当前 {len(_temp_kb_cache)})")


class KnowledgeBaseManager:
    """知识库持久化管理器"""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.backup_dir = file_path.parent / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> KnowledgeBase:
        """加载知识库"""
        if not self.file_path.exists():
            logger.info("知识库文件不存在，创建新的知识库")
            return KnowledgeBase()

        data = safe_load_json(self.file_path)
        if data is None:
            # 尝试从备份恢复
            backup = self._get_latest_backup()
            if backup:
                logger.info(f"尝试从备份恢复: {backup}")
                backup_data = safe_load_json(backup)
                if backup_data:
                    return KnowledgeBase.from_dict(backup_data)

            logger.warning("无法恢复，创建新的知识库")
            return KnowledgeBase()

        kb = KnowledgeBase.from_dict(data)
        logger.info(f"成功加载知识库，已分析章节数: {len(kb.compressed_arcs)}")
        return kb

    def _create_backup(self):
        """备份当前知识库文件到 backups/ 目录"""
        if not self.file_path.exists():
            return
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"knowledge_{timestamp}.json"
            backup_path = self.backup_dir / backup_name
            import shutil
            shutil.copy2(self.file_path, backup_path)
            # 保留最近10个备份
            backups = sorted(self.backup_dir.glob("knowledge_*.json"), reverse=True)
            for old in backups[10:]:
                try:
                    old.unlink()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"备份知识库失败: {e}")

    def _get_latest_backup(self) -> Optional[Path]:
        """获取最新的备份文件路径"""
        backups = sorted(self.backup_dir.glob("knowledge_*.json"))
        return backups[-1] if backups else None

    def save(self, kb: KnowledgeBase) -> bool:
        """保存知识库"""
        success = safe_save_json(kb.to_dict(), self.file_path)

        if success:
            self._create_backup()
            logger.debug("知识库保存成功")

        return success

    @staticmethod
    def _apply_rolling_data(kb: KnowledgeBase, output_dir: Path) -> None:
        """加载并应用 rolling summary 数据到 KB（从磁盘读取）"""
        rolling_data = KnowledgeBaseManager._load_rolling_summary(output_dir)
        rs = rolling_data.get("rolling_structured")
        if isinstance(rs, dict) and rs:
            kb.rolling_structured = rs

    @staticmethod
    def build_temp_knowledge(output_dir: Path, chapter_limit: int = None) -> KnowledgeBase:
        """
        扫描output目录下所有 chapter_*_result.json，脚本合并成临时KB（纯dict操作，无LLM调用）。
        带内存缓存：相同输入直接返回缓存KB，有新文件时只解析新增部分增量合并。
        支持跨 chapter_limit 近邻增量。

        Args:
            output_dir: 输出目录
            chapter_limit: 可选，只加载章号 ≤ 此值的章节（补跑/并行时防止读到"未来"章）
        """
        global _temp_kb_cache, _temp_kb_lock

        cache_key = (str(output_dir), chapter_limit)

        result_files = sorted(output_dir.glob("chapter_*_result.json"))
        if not result_files:
            return KnowledgeBase()

        # 按 chapter_limit 过滤
        if chapter_limit is not None:
            filtered = []
            for f in result_files:
                m = re.match(r'chapter_(\d+)_result\.json', f.name)
                if m and int(m.group(1)) <= chapter_limit:
                    filtered.append(f)
            result_files = filtered

        if not result_files:
            return KnowledgeBase()

        # 构建文件指纹 (filename, mtime_ns)
        current_fingerprint = frozenset((f.name, f.stat().st_mtime_ns) for f in result_files)

        with _temp_kb_lock:
            # 1. 精确匹配
            if cache_key in _temp_kb_cache:
                cached_fp, cached_results, cached_kb = _temp_kb_cache[cache_key]
                if current_fingerprint == cached_fp:
                    logger.debug(f"build_temp_knowledge 缓存命中 ({len(cached_results)}章)")
                    return cached_kb

                # 同 key：用 (文件名, mtime) 检测新增或内容已变文件。
                # 旧逻辑只按文件名比对，重跑时文件名不变但 mtime 变化 → 漏判 → 返回陈旧缓存。
                cached_fps = {name: mtime for name, mtime in cached_fp}
                changed_files = [
                    f for f in result_files
                    if f.name not in cached_fps or cached_fps[f.name] != f.stat().st_mtime_ns
                ]
                if not changed_files:
                    return cached_kb

                # 区分「纯新增文件」与「内容已变文件」：
                # 内容已变的旧文件若继续增量合并会残留陈旧条目，故存在已变文件时整体重建。
                newly_added = [f for f in changed_files if f.name not in cached_fps]
                if len(newly_added) == len(changed_files):
                    # 全是新文件 → 安全增量合并
                    new_results = KnowledgeBaseManager._parse_result_files(changed_files)
                    if not new_results:
                        return cached_kb
                    all_results = cached_results + new_results
                    all_results.sort(key=lambda r: r.chapter_number)
                    kb = KnowledgeBaseManager._merge_incremental(
                        KnowledgeBase.from_dict(cached_kb.to_dict()), new_results)
                    _cache_put(cache_key, (current_fingerprint, all_results, kb))
                    KnowledgeBaseManager._apply_rolling_data(kb, output_dir)
                    logger.debug(f"build_temp_knowledge 增量更新: +{len(new_results)}章, 共{len(all_results)}章")
                    return kb
                else:
                    # 存在内容变化的旧文件（如重跑）→ 整体重建保证 KB 新鲜
                    all_results = KnowledgeBaseManager._parse_result_files(result_files)
                    if not all_results:
                        return cached_kb
                    all_results.sort(key=lambda r: r.chapter_number)
                    kb = KnowledgeBaseManager._merge_analysis_results(all_results)
                    _cache_put(cache_key, (current_fingerprint, all_results, kb))
                    KnowledgeBaseManager._apply_rolling_data(kb, output_dir)
                    logger.debug(f"build_temp_knowledge 内容变化重建: {len(all_results)}章")
                    return kb

            # 2. 近邻增量：找缓存中 chapter_limit 最大且 ≤ 当前值的条目
            str_dir = str(output_dir)
            best_key, best_limit = None, -1
            for key in _temp_kb_cache:
                if key[0] == str_dir and key[1] is not None:
                    if chapter_limit is None or key[1] <= chapter_limit:
                        if key[1] > best_limit:
                            best_key, best_limit = key, key[1]

            near_neighbor_valid = False
            if best_key is not None:
                cached_fp, cached_results, cached_kb = _temp_kb_cache[best_key]
                # ≤best_limit 基线区间的文件可能已被重写
                # （重跑/手工修正）。近邻增量只解析 >best_limit 新文件，若不校验
                # 基线指纹，陈旧 KB 会经 _cache_put 固化到新 key 且永不自愈，
                # 并可经 merge_results 进入最终 knowledge.json。
                cur_base = {}
                for f in result_files:
                    m = re.match(r'chapter_(\d+)_result\.json', f.name)
                    if m and int(m.group(1)) <= best_limit:
                        cur_base[f.name] = f.stat().st_mtime_ns
                base_map = dict(cached_fp)
                near_neighbor_valid = (
                    set(base_map.keys()) == set(cur_base.keys())
                    and all(base_map[n] == cur_base[n] for n in base_map)
                )

            if best_key is not None and near_neighbor_valid:
                cached_fp, cached_results, cached_kb = _temp_kb_cache[best_key]
                # 只读章号 > best_limit 的新文件
                new_files = []
                for f in result_files:
                    m = re.match(r'chapter_(\d+)_result\.json', f.name)
                    if m and int(m.group(1)) > best_limit:
                        new_files.append(f)

                new_results = KnowledgeBaseManager._parse_result_files(new_files) if new_files else []
                if new_results:
                    all_results = cached_results + new_results
                    all_results.sort(key=lambda r: r.chapter_number)
                    kb = KnowledgeBaseManager._merge_incremental(
                        KnowledgeBase.from_dict(cached_kb.to_dict()), new_results)
                    _cache_put(cache_key, (current_fingerprint, all_results, kb))
                else:
                    kb = KnowledgeBase.from_dict(cached_kb.to_dict())
                    _cache_put(cache_key, (current_fingerprint, cached_results, kb))
                KnowledgeBaseManager._apply_rolling_data(kb, output_dir)
                logger.debug(f"build_temp_knowledge 近邻增量(best_limit={best_limit}): +{len(new_results)}章")
                return kb
            # 基线失效或无可用近邻 → 落入下方全量重建（陈旧数据不固化）

            # 3. 全量构建（首次）
            results = KnowledgeBaseManager._parse_result_files(result_files)
            if not results:
                return KnowledgeBase()

            kb = KnowledgeBaseManager._merge_analysis_results(results)
            _cache_put(cache_key, (current_fingerprint, results, kb))
            KnowledgeBaseManager._apply_rolling_data(kb, output_dir)
            logger.debug(f"build_temp_knowledge 首次构建: {len(results)}章")
            return kb

    @staticmethod
    def _parse_result_files(files: list) -> List[AnalysisResult]:
        """解析一批 result JSON 文件为 AnalysisResult 列表"""
        results = []
        for f in files:
            try:
                with open(f, 'r', encoding='utf-8') as fp:
                    data = json.load(fp)
                results.append(AnalysisResult.from_dict(data))
            except Exception as e:
                logger.warning(f"读取 {f} 失败: {e}")
        return results

    @staticmethod
    def _load_rolling_summary(output_dir: Path) -> dict:
        """从 output_dir/rolling_summary.json 加载 rolling 摘要数据（返回完整 dict，含分层字段）"""
        rolling_file = output_dir / ROLLING_FILE_NAME
        if not rolling_file.exists():
            return {}
        try:
            rd = safe_load_json(rolling_file)
            if isinstance(rd, dict):
                return rd
        except Exception as e:
            logger.warning(f"加载 rolling_summary 失败: {e}")
        return {}

    @staticmethod
    def merge_results(output_dir: Path, preserve_archive: bool = False) -> KnowledgeBase:
        """
        脚本合并所有 chapter_*_result.json 生成最终KB（无LLM调用）。
        等同于 build_temp_knowledge 但会裁剪无限增长字段。
        注意：对缓存KB的深拷贝操作，避免 _trim_kb 污染缓存。
        """
        kb = KnowledgeBaseManager.build_temp_knowledge(output_dir)
        # 深拷贝，避免 _trim_kb 原地修改缓存中的KB对象
        kb_copy = KnowledgeBase.from_dict(kb.to_dict())
        KnowledgeBaseManager._trim_kb(kb_copy, preserve_archive=preserve_archive)
        return kb_copy

    @staticmethod
    def _trim_kb(kb: KnowledgeBase, preserve_archive: bool = False):
        """裁剪无限增长字段，防止 knowledge.json 膨胀"""
        if preserve_archive:
            prev_archive = getattr(kb, "_archive", None)
            prev_archive = prev_archive if isinstance(prev_archive, dict) else {}
            full_fields = {}
            for fname in (
                "world_building",
                "verified_facts",
                "long_term_arcs",
                "thematic_elements",
                "foreshadowing_network",
            ):
                cur_list = list(getattr(kb, fname, []) or [])
                old_list = list(prev_archive.get(fname, []) or [])
                if old_list:
                    seen = set(old_list)
                    merged = list(old_list)
                    for item in cur_list:
                        if item not in seen:
                            merged.append(item)
                            seen.add(item)
                    full_fields[fname] = merged
                else:
                    full_fields[fname] = cur_list
            setattr(kb, "_archive", full_fields)
        kb.world_building = kb.world_building[-MAX_WORLD_BUILDING:]
        kb.verified_facts = kb.verified_facts[-MAX_VERIFIED_FACTS:]
        kb.long_term_arcs = kb.long_term_arcs[-MAX_LONG_TERM_ARCS:]
        kb.thematic_elements = kb.thematic_elements[-MAX_THEMATIC_ELEMENTS:]
        kb.foreshadowing_network = kb.foreshadowing_network[-MAX_FORESIGHT_NETWORK:]
        if preserve_archive:
            archive = getattr(kb, "_archive", {})
            setattr(
                kb,
                "_trim_stats",
                {
                    k: {
                        "total": len(v),
                        "kept": len(getattr(kb, k, [])),
                        "dropped": max(0, len(v) - len(getattr(kb, k, []))),
                    }
                    for k, v in archive.items()
                },
            )

    @staticmethod
    def _merge_analysis_results(results: List[AnalysisResult]) -> KnowledgeBase:
        """将多个章节的AnalysisResult合并为KnowledgeBase（纯脚本操作）"""
        kb = KnowledgeBase()

        results.sort(key=lambda r: r.chapter_number)

        # recent_summaries: 按章号排序取最后5条
        for r in results:
            if r.cross_block.summary:
                kb.recent_summaries.append(r.cross_block.summary)
        if len(kb.recent_summaries) > MAX_RECENT_SUMMARIES:
            kb.recent_summaries = kb.recent_summaries[-MAX_RECENT_SUMMARIES:]

        # compressed_arcs: 按章号排序append，上限50条
        for r in results:
            timeline = r.updated_knowledge.timeline
            if timeline:
                if isinstance(timeline, list):
                    timeline = ", ".join(str(t) for t in timeline)
                else:
                    timeline = str(timeline)
                kb.compressed_arcs.append(timeline)
        if len(kb.compressed_arcs) > MAX_COMPRESSED_ARCS:
            kb.compressed_arcs = kb.compressed_arcs[-MAX_COMPRESSED_ARCS:]

        # verified_facts: 从 core_events.event 派生
        seen_facts = set()
        for r in results:
            for event in r.core_events:
                if event.event and event.event not in seen_facts:
                    seen_facts.add(event.event)
                    kb.verified_facts.append(event.event)

        # long_term_arcs: 精确去重append（新schema不再输出此字段，保留兼容旧数据）
        seen_arcs = set()
        for r in results:
            for a in r.long_context_insights.long_term_arcs:
                if a not in seen_arcs:
                    seen_arcs.add(a)
                    kb.long_term_arcs.append(a)

        # world_building: 去重append（从 updated_knowledge 取）
        seen_world = set()
        for r in results:
            for w in r.updated_knowledge.world_building:
                if w not in seen_world:
                    seen_world.add(w)
                    kb.world_building.append(w)

        # character_states: 从 character_arcs 派生，每角色取最后出现的 surface_action
        for r in results:
            for arc in r.character_arcs:
                if arc.name and arc.surface_action:
                    kb.character_states[arc.name] = arc.surface_action

        # character_relationships: 从同章角色配对推导（配对 key 按名字排序归一化）
        for r in results:
            arcs_in_ch = [a for a in r.character_arcs if a.name]
            for i, a1 in enumerate(arcs_in_ch):
                for a2 in arcs_in_ch[i+1:]:
                    first, second = (a1, a2) if a1.name <= a2.name else (a2, a1)
                    pair_key = f"{first.name}-{second.name}"
                    desc = f"{first.change_delta}；{second.change_delta}"
                    kb.character_relationships[pair_key] = desc

        # thematic_elements: 精确去重append
        seen_themes = set()
        for r in results:
            for t in r.long_context_insights.thematic_elements:
                if t not in seen_themes:
                    seen_themes.add(t)
                    kb.thematic_elements.append(t)

        # foreshadowing_network: 按章号排序append
        for r in results:
            if r.long_context_insights.foreshadowing_network:
                kb.foreshadowing_network.append(
                    f"第{r.chapter_number}章: {r.long_context_insights.foreshadowing_network}"
                )

        # story_timeline: 取最后写入章节的timeline
        if results:
            last = results[-1]
            timeline = last.updated_knowledge.timeline
            if timeline:
                if isinstance(timeline, list):
                    timeline = ", ".join(str(t) for t in timeline)
                else:
                    timeline = str(timeline)
                kb.story_timeline = timeline

        logger.info(f"脚本合并完成: {len(results)}个章节结果 → 1个KnowledgeBase")
        return kb

    @staticmethod
    def _merge_incremental(kb: KnowledgeBase, new_results: List[AnalysisResult]) -> KnowledgeBase:
        """增量更新已有KB：只处理 new_results，不重算旧数据。调用方须保证 kb 不被共享（传深拷贝或独占引用）。"""
        new_results.sort(key=lambda r: r.chapter_number)

        # 从已有列表重建去重集合（列表已裁剪，大小有上限）
        seen_facts = set(kb.verified_facts)
        seen_world = set(kb.world_building)
        seen_themes = set(kb.thematic_elements)
        seen_arcs = set(kb.long_term_arcs)

        timeline = None
        for r in new_results:
            if r.cross_block.summary:
                kb.recent_summaries.append(r.cross_block.summary)

            timeline = r.updated_knowledge.timeline
            if timeline:
                if isinstance(timeline, list):
                    timeline = ", ".join(str(t) for t in timeline)
                else:
                    timeline = str(timeline)
                kb.compressed_arcs.append(timeline)

            for event in r.core_events:
                if event.event and event.event not in seen_facts:
                    seen_facts.add(event.event)
                    kb.verified_facts.append(event.event)

            for a in r.long_context_insights.long_term_arcs:
                if a not in seen_arcs:
                    seen_arcs.add(a)
                    kb.long_term_arcs.append(a)

            for w in r.updated_knowledge.world_building:
                if w not in seen_world:
                    seen_world.add(w)
                    kb.world_building.append(w)

            for arc in r.character_arcs:
                if arc.name and arc.surface_action:
                    kb.character_states[arc.name] = arc.surface_action

            arcs_in_ch = [a for a in r.character_arcs if a.name]
            for i, a1 in enumerate(arcs_in_ch):
                for a2 in arcs_in_ch[i+1:]:
                    first, second = (a1, a2) if a1.name <= a2.name else (a2, a1)
                    pair_key = f"{first.name}-{second.name}"
                    desc = f"{first.change_delta}；{second.change_delta}"
                    kb.character_relationships[pair_key] = desc

            for t in r.long_context_insights.thematic_elements:
                if t not in seen_themes:
                    seen_themes.add(t)
                    kb.thematic_elements.append(t)

            if r.long_context_insights.foreshadowing_network:
                kb.foreshadowing_network.append(
                    f"第{r.chapter_number}章: {r.long_context_insights.foreshadowing_network}"
                )

            if timeline:
                kb.story_timeline = timeline

        # 裁剪
        kb.recent_summaries = kb.recent_summaries[-MAX_RECENT_SUMMARIES:]
        kb.compressed_arcs = kb.compressed_arcs[-MAX_COMPRESSED_ARCS:]
        kb.verified_facts = kb.verified_facts[-MAX_VERIFIED_FACTS:]
        kb.long_term_arcs = kb.long_term_arcs[-MAX_LONG_TERM_ARCS:]
        kb.world_building = kb.world_building[-MAX_WORLD_BUILDING:]
        kb.thematic_elements = kb.thematic_elements[-MAX_THEMATIC_ELEMENTS:]
        kb.foreshadowing_network = kb.foreshadowing_network[-MAX_FORESIGHT_NETWORK:]

        logger.info(f"增量合并完成: +{len(new_results)}章")
        return kb
