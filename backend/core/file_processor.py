"""
文件处理器
负责扫描、读取和管理小说章节文件
（同步 IO 保留，pipeline 中通过 asyncio.to_thread 包装调用）
"""

import re
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

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


    def read_chapters_batch(self, chapters: Sequence[int]) -> List[Tuple[int, str]]:
        """按实存章号列表逐章读取原文，返回 [(chapter_num, content), ...]。"""
        items: List[Tuple[int, str]] = []
        for ch in chapters:
            cid = int(ch)
            content = self.read_chapter(cid)
            if content is None:
                logger.warning(f"read_chapters_batch: 第{cid}章读取失败或不存在，已跳过")
                continue
            items.append((cid, content))
        return items

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
