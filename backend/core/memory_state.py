"""
内存状态容器（asyncio 版）
持有所有 AnalysisResult 和增量维护的 KnowledgeBase，避免重复磁盘IO
（threading.Lock → asyncio.Lock，涉锁方法改为 async；
flush/restore 逻辑与磁盘格式零改动，block_size 续跑校验保留）
"""

import asyncio
import json
import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set

from ..models.analysis_result import AnalysisResult
from ..models.knowledge import KnowledgeBase
from ..config.constants import (
    MAX_COMPRESSED_ARCS, MAX_RECENT_SUMMARIES,
    MAX_FORESIGHT_NETWORK, MAX_WORLD_BUILDING, MAX_VERIFIED_FACTS,
    MAX_LONG_TERM_ARCS, MAX_THEMATIC_ELEMENTS, ROLLING_FILE_NAME,
)
from ..utils.json_utils import safe_save_json, safe_load_json

logger = logging.getLogger(__name__)


class MemoryState:
    """内存状态容器，持有所有 AnalysisResult 并增量维护 KnowledgeBase"""

    def __init__(self, checkpoint_interval: int = 5, kb_limits: Optional[dict] = None):
        self.results: Dict[int, AnalysisResult] = {}
        self.kb: KnowledgeBase = KnowledgeBase()
        self.rolling: dict = {}
        self._failed_chapters: Dict[int, str] = {}
        self._skipped_chapters: Dict[int, str] = {}  # 内容审核拦截跳过的块（不进失败集、不补跑）
        self._flushed_chapters: Set[int] = set()
        self._checkpoint_interval: int = checkpoint_interval
        self._kb_lock = asyncio.Lock()
        self._rolling_lock = asyncio.Lock()  # 保护 state.rolling 的并发读写
        self._seen_facts: Set[str] = set()
        self._seen_world: Set[str] = set()
        self._seen_themes: Set[str] = set()
        self._seen_arcs: Set[str] = set()
        # get_kb_snapshot 子集缓存（仅缓存"已构建的最大 limit"，保证不含未来章节）
        self._snapshot_cache_limit: Optional[int] = None
        self._snapshot_cache_kb: Optional[KnowledgeBase] = None
        self._snapshot_lock = threading.Lock()  # 快照缓存并发保护（事件循环内短临界区）
        # 增量 KB 已合并的最大章号：并发完成乱序时，仅章号 >= 此值的合并更新
        # timeline/story_timeline/recent_summaries（防低章号结果倒灌覆盖"当前进度"）
        self._kb_last_chapter: int = 0
        # 知识库限制（来自配置，默认回退到常量）
        self._kb_limits = kb_limits or {}

    async def add_result(self, result: AnalysisResult) -> None:
        """存储结果并增量更新 KB（协程安全）

        get_kb_snapshot 在线程池中执行，工作线程会并发深拷贝 results/kb。
        插入与合并全程持 threading 锁与读侧互斥——merge 本身毫秒级，即便工作
        线程正持锁做 to_dict，事件循环最多等它做完（有界短暂），远好于偶发
        RuntimeError 导致静默丢块。锁序恒为 threading→asyncio，无死锁环。"""
        with self._snapshot_lock:
            self.results[result.chapter_number] = result
            # 成功落盘的结果清除该章失败标记：重跑成功的章节不应再被永久报告为失败
            self._failed_chapters.pop(result.chapter_number, None)
            # 同理清除陈旧 skipped 标记：否则下次 flush 会把已成功章节的
            # 审核拦截标记重新写回磁盘（与上方 failed 清理对称）
            self._skipped_chapters.pop(result.chapter_number, None)
            async with self._kb_lock:
                self._merge_one(result)
                # 子集缓存失效策略：仅当新结果的章号落在已缓存
                # limit 区间内才失效（该结果无法被"按章号区间增量扩展"覆盖）；
                # 区间外的新结果（正常顺序分析）可直接增量扩展，避免每块全量重建 KB。
                if (self._snapshot_cache_limit is not None
                        and result.chapter_number <= self._snapshot_cache_limit):
                    self._snapshot_cache_limit = None

    def add_failed(self, chapter_number: int, error: str) -> None:
        """记录失败章节"""
        self._failed_chapters[chapter_number] = error

    def add_skipped(self, chapter_number: int, reason: str) -> None:
        """记录被内容审核拦截跳过的章节（不进失败集，补跑不重试）"""
        self._skipped_chapters[chapter_number] = reason

    def failed_chapters_in(self, valid_block_ids: set) -> List[int]:
        """当前分析范围内的失败章号（restore 会加载历史场次的失败标记，
        最终报告必须按本次 valid_block_ids 过滤，否则重跑小区间会误报旧场失败）。"""
        return [c for c in self._failed_chapters if c in valid_block_ids]

    def get_kb_snapshot(self, chapter_limit: Optional[int] = None) -> KnowledgeBase:
        """
        获取 KB 快照（调用方拥有独立副本）。
        chapter_limit 为 None 或 >= 最大章号时返回当前 KB 深拷贝，
        否则返回"仅包含章号 <= chapter_limit 结果"的子集 KB。

        正确性约束：子集缓存只允许在请求 limit 等于或大于
        缓存 limit 时复用/扩展；绝不能用覆盖更大章号的缓存回答更小的 limit，
        否则并发分析中低章号块会"读到未来章节知识"。

        整函数持 _snapshot_lock——本方法经 to_thread 在工作线程执行，
        必须与 add_result 的写侧互斥，否则 to_dict 深遍历期间 results/kb 被
        并发修改会抛 "dictionary changed size during iteration"。"""
        with self._snapshot_lock:
            keys = list(self.results.keys())
            if chapter_limit is None or not keys or chapter_limit >= max(keys):
                return self._apply_rolling_to_snapshot(KnowledgeBase.from_dict(self.kb.to_dict()))

            cached_limit = self._snapshot_cache_limit

            # 命中：请求与缓存 limit 精确相等
            if cached_limit is not None and chapter_limit == cached_limit:
                return self._apply_rolling_to_snapshot(
                    KnowledgeBase.from_dict(self._snapshot_cache_kb.to_dict()))

            # 命中：请求 limit 大于缓存 limit → 增量扩展（O(新增章节数)，非全量重建）
            if cached_limit is not None and chapter_limit > cached_limit:
                new_results = sorted(
                    (r for ch, r in self.results.items() if cached_limit < ch <= chapter_limit),
                    key=lambda r: r.chapter_number,
                )
                kb = KnowledgeBase.from_dict(self._snapshot_cache_kb.to_dict())
                if new_results:
                    self._extend_kb(kb, new_results)
                self._snapshot_cache_limit = chapter_limit
                self._snapshot_cache_kb = kb
                return self._apply_rolling_to_snapshot(KnowledgeBase.from_dict(kb.to_dict()))

            # 未命中：请求 limit 小于缓存 limit（或首次构建）→ 从子集重建，
            # 保证不包含任何 > chapter_limit 的未来章节
            subset = sorted(
                (r for ch, r in self.results.items() if ch <= chapter_limit),
                key=lambda r: r.chapter_number,
            )
            kb = self._build_kb_from_results(subset)
            if cached_limit is None or chapter_limit > cached_limit:
                self._snapshot_cache_limit = chapter_limit
                self._snapshot_cache_kb = kb
            return self._apply_rolling_to_snapshot(KnowledgeBase.from_dict(kb.to_dict()))

    def _apply_rolling_to_snapshot(self, kb: KnowledgeBase) -> KnowledgeBase:
        """把滚动总结数据注入快照副本（只改传入副本，绝不写回 self.kb / 缓存对象）。

        rolling_structured 由 knowledge_base.build_temp_knowledge 注入导出/预览路径，
        分析主链路 get_kb_snapshot 的快照里恒为空 {} —— 后台 rolling 任务产出的
        主线概要对 prompt 零生效。
        说明：dict.get 在 GIL 下原子，最坏读到略旧的一版 rolling，可接受；
        不取 _rolling_lock 是因为那是 asyncio.Lock，本方法为同步上下文。
        """
        rs = self.rolling.get("rolling_structured") if isinstance(self.rolling, dict) else None
        if isinstance(rs, dict):
            kb.rolling_structured = rs
        return kb

    async def flush_to_disk(self, output_dir: Path) -> int:
        """
        将未落盘的结果写入 output_dir。
        返回本次新落盘的章节数。
        （磁盘格式与旧项目完全一致：chapter_N_result.json / rolling_summary.json / chapter_N_failed.json）
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        newly_flushed = 0

        # 快照 self.results 避免迭代期间并发修改导致 RuntimeError
        snapshot = list(self.results.items())

        for ch, result in snapshot:
            if ch in self._flushed_chapters:
                continue
            data = result.to_dict()
            data.pop("raw_response", None)
            filepath = output_dir / f"chapter_{ch}_result.json"
            try:
                await asyncio.to_thread(self._write_json_file, filepath, data)
                self._flushed_chapters.add(ch)
                newly_flushed += 1
                # 成功落盘后移除该章陈旧的失败标记文件，避免已成功章节仍被报告为失败
                failed_path = output_dir / f"chapter_{ch}_failed.json"
                if failed_path.exists():
                    try:
                        await asyncio.to_thread(failed_path.unlink)
                    except OSError:
                        pass
                # 同理清理该章陈旧的 skipped 标记
                skipped_path = output_dir / f"chapter_{ch}_skipped.json"
                if skipped_path.exists():
                    try:
                        await asyncio.to_thread(skipped_path.unlink)
                    except OSError:
                        pass
            except Exception as e:
                logger.warning(f"落盘章节 {ch} 失败: {e}")

        # 写入滚动摘要
        if self.rolling:
            async with self._rolling_lock:
                rolling_snapshot = dict(self.rolling)
            rolling_path = output_dir / ROLLING_FILE_NAME
            await asyncio.to_thread(safe_save_json, rolling_snapshot, rolling_path)

        # 写入失败章节标记（快照迭代：循环体内 await 让出，流式 worker 可能并发 add_failed
        # 插入新键导致 "dictionary changed size during iteration" 中断整个任务）
        for ch, error in list(self._failed_chapters.items()):
            failed_path = output_dir / f"chapter_{ch}_failed.json"
            try:
                await asyncio.to_thread(
                    safe_save_json, {"chapter_number": ch, "error": error}, failed_path
                )
            except Exception as e:
                logger.warning(f"落盘失败标记 {ch} 失败: {e}")

        # 写入 skipped 标记（审核拦截块）：持久化后重启续跑不再对同一批块
        # 重新支付完整 LLM 重试链费用
        for ch, reason in list(self._skipped_chapters.items()):
            skipped_path = output_dir / f"chapter_{ch}_skipped.json"
            try:
                await asyncio.to_thread(
                    safe_save_json,
                    {"chapter_number": ch, "reason": reason}, skipped_path
                )
            except Exception as e:
                logger.warning(f"落盘 skipped 标记 {ch} 失败: {e}")

        return newly_flushed

    @staticmethod
    def _write_json_file(filepath: Path, data: dict) -> None:
        """原子写单个章节结果 JSON（先写临时文件再 rename 替换，供 to_thread 调用）。

        用已导入的 safe_save_json（临时文件 + os.replace 原子替换），与 rolling/failed
        标记的写法保持一致，写入中途崩溃最多丢失临时文件、不会污染正式结果文件。
        （open('w') 先截断再写的话，进程在写入中途被杀（崩溃/强关/断电）会
        留下半截/空的 chapter_N_result.json；恢复时 json.load 抛异常，该章被误判为损坏。）"""
        safe_save_json(data, filepath, ensure_ascii=False)

    async def restore_from_disk(self, output_dir: Path, block_size: Optional[int] = None) -> int:
        """从 output_dir 恢复结果到内存（阻塞 IO 走线程池，避免事件循环卡顿）"""
        return await asyncio.to_thread(self._restore_from_disk_sync, output_dir, block_size)

    def _restore_from_disk_sync(self, output_dir: Path, block_size: Optional[int]) -> int:
        """同步恢复实现（原 restore_from_disk 主体：纯文件 IO，无 await）"""
        import glob as glob_mod
        pattern = str(output_dir / "chapter_*_result.json")
        files = sorted(glob_mod.glob(pattern))

        restored = 0
        for filepath_str in files:
            filepath = Path(filepath_str)
            if block_size is not None and not self._validate_block_size(filepath, block_size):
                continue

            m = re.match(r'chapter_(\d+)_result\.json', filepath.name)
            if not m:
                continue
            ch_num = int(m.group(1))

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                result = AnalysisResult.from_dict(data)
                self.results[ch_num] = result
                self._flushed_chapters.add(ch_num)
                restored += 1
            except Exception as e:
                logger.warning(f"恢复 {filepath.name} 失败: {e}")

        # 恢复后重建 KB
        # 注意：本函数在 pipeline 启动期经 to_thread 调用，此时尚无并发 worker，
        # 不存在与 add_result/merge 的竞争，故不取 asyncio 锁（asyncio.Lock 不支持
        # 线程内同步使用；若未来并发调用需改为线程安全方案）
        if restored > 0:
            sorted_results = [self.results[ch] for ch in sorted(self.results.keys())]
            self.kb = KnowledgeBase()
            self._seen_facts.clear()
            self._seen_world.clear()
            self._seen_themes.clear()
            self._seen_arcs.clear()
            for r in sorted_results:
                self._merge_one(r)

        # 恢复滚动摘要
        rolling_path = output_dir / ROLLING_FILE_NAME
        if rolling_path.exists():
            try:
                rd = safe_load_json(rolling_path)
                if isinstance(rd, dict):
                    self.rolling = rd
            except Exception as e:
                logger.warning(f"恢复 rolling_summary 失败: {e}")

        # 恢复失败标记：已成功恢复结果的章节不应再被标记为失败（清除陈旧失败标记）
        restored_chapters = set(self.results.keys())
        failed_pattern = str(output_dir / "chapter_*_failed.json")
        for fp in sorted(glob_mod.glob(failed_pattern)):
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and "chapter_number" in data:
                    ch_num = data["chapter_number"]
                    if ch_num in restored_chapters:
                        continue  # 该章已有成功结果，跳过陈旧失败标记
                    self._failed_chapters[ch_num] = data.get("error", "")
            except Exception:
                pass

        # 恢复 skipped 标记：已有成功结果的章节跳过陈旧标记
        skipped_pattern = str(output_dir / "chapter_*_skipped.json")
        for fp in sorted(glob_mod.glob(skipped_pattern)):
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and "chapter_number" in data:
                    ch_num = data["chapter_number"]
                    if ch_num in restored_chapters:
                        continue
                    self._skipped_chapters[ch_num] = data.get("reason", "")
            except Exception:
                pass

        return restored

    def should_checkpoint(self, batch_idx: int) -> bool:
        """判断是否应触发 checkpoint"""
        if self._checkpoint_interval <= 0:
            return False
        return (batch_idx + 1) % self._checkpoint_interval == 0

    def _merge_one(self, result: AnalysisResult) -> None:
        """增量合并一个结果到 self.kb（须在 _kb_lock 内调用）"""
        # 乱序保护：只有按章号推进（>= 当前最大）的合并才更新"进度类"字段，
        # 低章号迟到结果只合并非进度内容（事实/角色/世界等），
        # 避免并发完成顺序不定时 timeline/摘要被旧章倒灌
        in_order = result.chapter_number >= self._kb_last_chapter
        self._merge_result_into(
            result, self.kb,
            self._seen_facts, self._seen_world, self._seen_themes, self._seen_arcs,
            update_timeline=in_order,
        )
        if in_order:
            self._kb_last_chapter = result.chapter_number
        self._trim_kb(self.kb)

    @staticmethod
    def _merge_result_into(
        result: AnalysisResult,
        kb: KnowledgeBase,
        seen_facts: Set[str],
        seen_world: Set[str],
        seen_themes: Set[str],
        seen_arcs: Set[str],
        update_timeline: bool = True,
    ) -> None:
        """将单个 result 合并到指定 kb（纯函数，无副作用）。

        update_timeline=False：乱序迟到的低章号结果，跳过 timeline/story_timeline/
        recent_summaries 等"进度类"字段（合并方已保证传入列表有序时保持 True）。
        """
        if update_timeline and result.cross_block.summary:
            kb.recent_summaries.append(result.cross_block.summary)

        timeline = result.updated_knowledge.timeline
        if timeline and update_timeline:
            if isinstance(timeline, list):
                timeline = ", ".join(str(t) for t in timeline)
            else:
                timeline = str(timeline)
            kb.compressed_arcs.append(timeline)

        for event in result.core_events:
            if event.event and event.event not in seen_facts:
                seen_facts.add(event.event)
                kb.verified_facts.append(event.event)

        for a in result.long_context_insights.long_term_arcs:
            if a not in seen_arcs:
                seen_arcs.add(a)
                kb.long_term_arcs.append(a)

        for w in result.updated_knowledge.world_building:
            if w not in seen_world:
                seen_world.add(w)
                kb.world_building.append(w)

        for arc in result.character_arcs:
            if arc.name and arc.surface_action:
                kb.character_states[arc.name] = arc.surface_action

        arcs_in_ch = [a for a in result.character_arcs if a.name]
        for i, a1 in enumerate(arcs_in_ch):
            for a2 in arcs_in_ch[i + 1:]:
                first, second = (a1, a2) if a1.name <= a2.name else (a2, a1)
                pair_key = f"{first.name}-{second.name}"
                desc = f"{first.change_delta}；{second.change_delta}"
                kb.character_relationships[pair_key] = desc

        for t in result.long_context_insights.thematic_elements:
            if t not in seen_themes:
                seen_themes.add(t)
                kb.thematic_elements.append(t)

        if result.long_context_insights.foreshadowing_network:
            kb.foreshadowing_network.append(
                f"第{result.chapter_number}章: {result.long_context_insights.foreshadowing_network}"
            )

        if timeline and update_timeline:
            kb.story_timeline = timeline

    def _trim_kb(self, kb: KnowledgeBase) -> None:
        """裁剪 KB 各字段到上限（优先使用配置值，回退到常量）"""
        L = self._kb_limits
        kb.recent_summaries = kb.recent_summaries[-L.get('max_recent_summaries', MAX_RECENT_SUMMARIES):]
        kb.compressed_arcs = kb.compressed_arcs[-L.get('max_compressed_arcs', MAX_COMPRESSED_ARCS):]
        kb.verified_facts = kb.verified_facts[-L.get('max_verified_facts', MAX_VERIFIED_FACTS):]
        kb.long_term_arcs = kb.long_term_arcs[-L.get('max_long_term_arcs', MAX_LONG_TERM_ARCS):]
        kb.world_building = kb.world_building[-L.get('max_world_building', MAX_WORLD_BUILDING):]
        kb.thematic_elements = kb.thematic_elements[-L.get('max_thematic_elements', MAX_THEMATIC_ELEMENTS):]
        kb.foreshadowing_network = kb.foreshadowing_network[-L.get('max_foreshadow_network', MAX_FORESIGHT_NETWORK):]

    def _extend_kb(self, kb: KnowledgeBase, new_results: List[AnalysisResult]) -> None:
        """将新结果增量合并进已有 KB（快照扩展用；须在 _snapshot_lock 内调用）。

        去重集合从 KB 现存字段重建（verified_facts 等本身就是去重后的列表），
        与 _merge_result_into 的语义保持一致。
        """
        seen_facts = set(kb.verified_facts)
        seen_world = set(kb.world_building)
        seen_themes = set(kb.thematic_elements)
        seen_arcs = set(kb.long_term_arcs)
        for r in new_results:
            self._merge_result_into(r, kb, seen_facts, seen_world, seen_themes, seen_arcs)
        self._trim_kb(kb)

    def _build_kb_from_results(self, results: List[AnalysisResult]) -> KnowledgeBase:
        """从结果列表构建全新 KB（用于 get_kb_snapshot 子集场景）"""
        kb = KnowledgeBase()
        seen_facts: Set[str] = set()
        seen_world: Set[str] = set()
        seen_themes: Set[str] = set()
        seen_arcs: Set[str] = set()

        results = sorted(results, key=lambda r: r.chapter_number)

        for r in results:
            self._merge_result_into(r, kb, seen_facts, seen_world, seen_themes, seen_arcs)

        self._trim_kb(kb)
        return kb

    @staticmethod
    def _validate_block_size(filepath: Path, expected_block_size: int) -> bool:
        """校验文件中的 block_size 是否匹配预期"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                head = f.read(500)
            m = re.search(r'"block_size"\s*:\s*(\d+)', head)
            if m is None:
                # 头部窗口未命中（新 schema 字段可能将 block_size 推后），回退完整解析
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, dict) or 'block_size' not in data:
                    return True  # 旧格式无此字段，视为兼容
                return int(data['block_size']) == expected_block_size
            return int(m.group(1)) == expected_block_size
        except Exception:
            return False
