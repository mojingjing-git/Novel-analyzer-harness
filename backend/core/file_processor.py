"""
文件处理器
负责扫描、读取和管理小说章节文件
（自旧项目 core/file_processor.py 原样移植；同步 IO 保留，
pipeline 中通过 asyncio.to_thread 包装调用）
"""

import re
import logging
import threading
from pathlib import Path
from typing import List, Dict, Optional

from ..utils.text_utils import detect_encoding
from ..config.constants import ENCODING_CANDIDATES

logger = logging.getLogger(__name__)


class FileProcessor:
    """小说文件处理器"""

    def __init__(self, directory: Path, encoding_priority: Optional[List[str]] = None):
        """
        初始化文件处理器

        Args:
            directory: 章节文件所在目录
            encoding_priority: 编码优先级列表
        """
        self.directory = directory
        self.encoding_priority = encoding_priority or ENCODING_CANDIDATES
        self._file_list: List[tuple] = []  # [(chapter_num, file_path), ...]
        self._file_map: Dict[int, Path] = {}  # chapter_num -> path（O(1) 查找索引）
        self._content_cache: Dict[int, str] = {}
        self._cache_lock = threading.Lock()
        self._detected_encoding: Optional[str] = None  # 目录级编码缓存

    def scan_chapters(self) -> List[int]:
        """
        扫描目录中的章节文件（仅识别纯数字文件名，如1.txt, 2.txt）

        Returns:
            排序后的章节号列表
        """
        self._file_list = []
        self._file_map = {}

        if not self.directory.exists():
            logger.warning(f"目录不存在: {self.directory}")
            return []

        for file_path in self.directory.glob("*.txt"):
            chapter_num = self._extract_chapter_number(file_path)
            if chapter_num is not None:
                self._file_list.append((chapter_num, file_path))
                self._file_map[chapter_num] = file_path

        # 按章节号排序
        self._file_list.sort(key=lambda x: x[0])

        chapter_nums = [chap_num for chap_num, _ in self._file_list]
        logger.info(f"扫描到{len(chapter_nums)}个章节文件")

        return chapter_nums

    def read_chapter(self, chapter_num: int) -> Optional[str]:
        """
        读取指定章节的内容

        Args:
            chapter_num: 章节号

        Returns:
            章节内容，失败返回None
        """
        # 检查缓存
        with self._cache_lock:
            if chapter_num in self._content_cache:
                return self._content_cache[chapter_num]

        # 查找文件
        file_path = self._find_chapter_file(chapter_num)
        if not file_path:
            logger.warning(f"未找到第{chapter_num}章的文件")
            return None

        try:
            # 优先使用目录级编码缓存（同目录文件编码基本相同）
            if self._detected_encoding:
                try:
                    with open(file_path, 'r', encoding=self._detected_encoding) as f:
                        content = f.read()
                    with self._cache_lock:
                        self._content_cache[chapter_num] = content
                    return content
                except UnicodeDecodeError:
                    self._detected_encoding = None  # 缓存失效，回退全量探测

            # 首次：探测编码、读取内容、缓存编码
            detected = detect_encoding(file_path, self.encoding_priority)
            with open(file_path, 'r', encoding=detected) as f:
                content = f.read()
            self._detected_encoding = detected
            with self._cache_lock:
                self._content_cache[chapter_num] = content
            logger.info(f"检测到文件编码: {detected}（后续文件将跳过探测）")
            logger.debug(f"成功读取第{chapter_num}章 ({len(content)}字符)")
            return content

        except Exception as e:
            logger.error(f"读取第{chapter_num}章失败: {e}")
            return None


    def read_block(self, chapters: List[int]) -> Optional[str]:
        """
        合并多章为一个文本块（用于块化分析）。

        2026-08-07 优化（P0-4）：签名由 (start_chapter, block_size) 改为接收
        **实际存在的章号列表**。原实现按 range(start, start+block_size) 连号读取，
        与 pipeline 按"实际章号"切块不一致——目录断号时（如只有 1/3/5.txt）
        中间的章会被静默跳过、永远不分析。

        Args:
            chapters: 本块的实际章号列表（有序）

        Returns:
            合并后的文本，失败返回None
        """
        if not chapters:
            return None
        parts = []
        multi = len(chapters) > 1
        for i, ch in enumerate(chapters):
            content = self.read_chapter(ch)
            if content is None:
                if i == 0:
                    return None
                logger.warning(f"read_block: 第{ch}章不存在，块内后续章跳过")
                break
            if multi:
                parts.append(f"--- 第{ch}章 ---\n{content}")
            else:
                parts.append(content)

        return "\n\n".join(parts)

    def clear_cache(self):
        """清空内容缓存"""
        with self._cache_lock:
            self._content_cache.clear()
        logger.info("内容缓存已清空")

    def _extract_chapter_number(self, file_path: Path) -> Optional[int]:
        """
        从文件名提取章节号（仅支持纯数字格式）

        Args:
            file_path: 文件路径

        Returns:
            章节号，无法提取返回None
        """
        # 匹配纯数字格式 "123.txt"
        match = re.match(r'^(\d+)\.txt$', file_path.name)
        if match:
            return int(match.group(1))

        return None

    def _find_chapter_file(self, chapter_num: int) -> Optional[Path]:
        """查找指定章节的文件（O(1) 字典索引；scan_chapters 后可用）"""
        if self._file_map:
            return self._file_map.get(chapter_num)
        # 降级：未扫描时线性查找（兼容旧行为）
        for chap_num, file_path in self._file_list:
            if chap_num == chapter_num:
                return file_path
        return None
