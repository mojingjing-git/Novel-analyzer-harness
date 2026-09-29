"""
切分工具服务 —— 功能增强版

将旧项目 cuttttttt_pyqt6.py 的切分逻辑，与两个开源实现里最实用的部分合并重写：
  * zzk6780051/novel  (.github/scripts/fenli.py)
        - 多格式章节正则（中文/阿拉伯数字、卷章节回、英文 Chapter、括号包裹）
        - 按前 N 行对每个候选正则打分，自动选出本书最优正则
        - 从正文前若干行提取书名/作者等元数据
        - 基于内容 MD5 的相邻去重
  * oomol-lab/txt-to-epub-converter  (src/txt_to_epub)
        - 卷/章/节 层级结构识别（volume / chapter / section）
        - 章节最小 / 最大字数限制

本模块在保留「workspace/{书名}/blocks/{index}.txt 扁平数字命名」契约的前提下，
新增：卷层级分组、番外/序章/楔子/尾声等特章、中文数字转阿拉伯、广告/水印行清理、
最小字数跳过或合并短章、超大章自动拆分、相邻去重、书名作者推断、统计信息。

输出：
  blocks/{index:04d}.txt        每章正文（标题 + 换行 + 正文）
  blocks/chapters.json          结构化章节清单（含卷、章节号、字数）
  blocks/metadata.json          推断出的书名 / 作者
  blocks/split_report.txt       人类可读切分报告

版权来源 / Third-Party Notices
=============================
本文件在以下 MIT 协议开源项目的思路基础上扩展重写，贡献边界明确如下：

  1) zzk6780051/novel
     URL  : https://github.com/zzk6780051/novel
     License: MIT
     借鉴内容：
       - 多格式章节正则（中文/阿拉伯数字、卷/章/回、英文 Chapter、括号包裹）
       - 按前 N 行对候选正则打分、自动选出本书最优正则
       - 从正文前若干行提取书名/作者等元数据
       - 基于内容 MD5 的相邻去重（防盗章场景）

  2) oomol-lab/txt-to-epub-converter
     URL  : https://github.com/oomol-lab/txt-to-epub-converter
     License: MIT
     借鉴内容：
       - 卷 / 章 / 节 三层层级结构识别（volume / chapter / section）
       - 章节最小 / 最大字数限制

  本文件中 2024 年 3 月之后所有改进（特章正则、强弱广告清理、并发写入、
  原子写入、min_words 沉默失败保护、超大章按段落拆分、书名作者推断、
  P1-P5 + G6 等六轮修复）由本项目作者独立完成。

  MIT 协议原文：https://opensource.org/licenses/MIT
  本文件同时包含上述项目的衍生作品，按 MIT 协议保留版权声明即可闭源商用。

已知限制（2026-09-02 P1 国际化修复后追加）
=========================================
下列限制经 7 本 Project Gutenberg 外文书 baseline + 4 类边界 case 实测确认，
本次 P1 修复未完全消除，列在此供 caller 知情使用：

1. **Dracula 等日记体无显式 Chapter 标记**：
   - baseline 期望：章节数 ≥ 0（不保证 ≥ 2）
   - 实测可识别 `CHAPTER I.` ~ `CHAPTER XXVII.` 等 27 个 Roman 标记
   - 完全无 Chapter 标记的纯日记体（罕见）会兜底为 1 章"全文"

2. **Moby-Dick TOC 重复切分残留**：
   - T2.5b 已识别并跳过 CONTENTS 块，再通过 `_dedup_same_key` 兜底去重
   - 极少数情况（TOC 条目紧跟 preface/poem 让 body check 误判）可能残留 1-2 条
   - 实测 Moby-Dick 切出 139 章（基线 270 → 修复后 139），残留可控

3. **裸罗马短篇正则要求全大写**：
   - 当前正则：`^[IVXLCDM]+\\.\\s+[A-Z][A-Z\\s,'\\u2019\\-:]{2,80}$`
   - 小写标题（如 "i. a scandal in bohemia"）**不识别**
   - 解决：调用方预处理（Title Case）后再喂入

4. **pre-START 头 Title/Author 提取只对 Project Gutenberg 有效**：
   - `_extract_pg_header_meta` 依赖 `Title:` / `Author:` 显式行或
     `The Project Gutenberg eBook of X` 第 1 行
   - 其他来源（自制 txt / 盗版站下载）的 header 不会被识别，会回退到
     `_extract_metadata` 的"前 8 行短行"策略
"""

import json
import logging
import os
import re
import shutil
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional

from backend.utils.text_utils import detect_and_decode
from backend.utils.docx_extract import extract_docx_text
from backend.config.constants import ENCODING_CANDIDATES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 中文数字 → 阿拉伯数字
# ---------------------------------------------------------------------------
_CN_NUM = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000,
}


def cn_to_int(chars: str) -> Optional[int]:
    """把『一百二十三』『万』『两』等中文数字转换为 int；纯阿拉伯直接转。"""
    chars = chars.strip()
    if not chars:
        return None
    if chars.isdigit():
        return int(chars)
    # 过滤掉非数字汉字
    if not any(c in _CN_NUM for c in chars):
        return None
    total = 0      # 已结算部分（跨越 万/亿 边界后累计）
    section = 0    # 当前节内（万以下）累计
    number = 0     # 当前正在拼接的小于 十 的数
    for ch in chars:
        if ch in ("零", "〇"):
            continue
        elif ch in "一二三四五六七八九两":
            number = number * 10 + _CN_NUM[ch]
        elif ch == "十":
            number = 10 if number == 0 else number * 10
            section += number
            number = 0
        elif ch == "百":
            number = 1 if number == 0 else number
            section += number * 100
            number = 0
        elif ch == "千":
            number = 1 if number == 0 else number
            section += number * 1000
            number = 0
        elif ch == "万":
            # 结算进 total，重置 section——旧实现直接改写 section 导致
            # "一亿零二万三千" 被算成 1000000023000（亿级联乘重复累计）
            total += (section + number) * 10000
            section = 0
            number = 0
        elif ch == "亿":
            total += (section + number) * 100000000
            section = 0
            number = 0
    return total + section + number


# ---------------------------------------------------------------------------
# 候选章节 / 卷 正则库（用于「自动检测」模式打分）
# ---------------------------------------------------------------------------
# 章节（章 / 回 / 节 / 特章 / 英文 / 数字编号）
CHAPTER_PATTERNS: List[tuple] = [
    ("第X章(中文)", r"^第[零一二三四五六七八九十百千两万千亿\d]+章"),
    ("第X章(阿拉伯)", r"^第\d+章"),
    ("第X回", r"^第[零一二三四五六七八九十百千两万千亿\d]+回"),
    ("第X节", r"^第[零一二三四五六七八九十百千两万千亿\d]+节"),
    ("卷X第X章", r"^[上下中下零一二三四五六七八九十百千两万千亿\d]+卷.*第[零一二三四五六七八九十百千两万千亿\d]+[章节回]"),
    ("第一卷第一章", r"^第[零一二三四五六七八九十百千两万千亿\d]+卷\s*第[零一二三四五六七八九十百千两万千亿\d]+[章节回]"),
    ("卷一第一章", r"^[卷部篇][零一二三四五六七八九十百千两万千亿\d]+卷?\s*第[零一二三四五六七八九十百千两万千亿\d]+[章节回]"),
    ("英文Chapter", r"^chapter\s+\d+"),
    # T2.1a（2026-09-02）：古典英文罗马数字章节（Austen、Dickens）
    # 匹配 `Chapter I.` `Chapter I.]` `CHAPTER II.` `CHAPTER XIII` 等。
    # 无 $ 锚点——允许后续接 `]` / `:` / 任意字符。
    ("古典罗马章节", r"^[Cc]hapter\s+[IVXLCDM]+[\.\s:]?"),
    # T2.1a（2026-09-02）：Sherlock 短篇无前缀 Roman（"I. A SCANDAL IN BOHEMIA"）
    # 要求后接全大写标题（区分正文内 "I went to..."）。\u2019 兼容 's 弯引号。
    ("裸罗马短篇", r"^[IVXLCDM]+\.\s+[A-Z][A-Z\s,'\u2019\-:]{2,80}$"),
    # T2.6（2026-09-02）：Gatsby 风格独行 Roman（仅在 T2.5b 跳过 TOC 后生效）
    # 匹配 "I" / "II" / "IX" / "X" 这种独占一行的 Roman。
    # 范围 I-XX 覆盖常见章节数；超过 XX 的不识别。
    ("裸罗马独行", r"^\s*(?:I{1,3}|IV|VI{0,3}|IX|X|XI{0,2}|XIV|XV|XIX|XX)\s*$"),
    # T2.5 收紧（2026-09-02）：原 r"^\d+[\.、]\s*\S" 会误命中 Gutenberg license 的
    # 1.A./1.B./1.E.1./1.F.6. 等条款，导致 Gatsby/Sherlock/Pride/Dracula/Tale 5/7 本
    # baseline 把 license 段当章节切。收紧：数字点后必须接非大写非点的内容（避免 1.A.）。
    # 两条覆盖「数字点+空格」与「数字点紧跟」两种常见章节标题写法。
    ("数字点编号", r"^\d{1,4}[\.、]\s+[^A-Z\.]{1,40}"),
    ("数字点紧跟", r"^\d{1,4}[\.、][^A-Z\.]{1,40}"),
    ("中文数字顿号", r"^[零一二三四五六七八九十百千]+、"),
    ("括号章", r"^[\【\[]第[零一二三四五六七八九十百千两万千亿\d]+[章节回]"),
    ("特章(序/楔/番外/尾声…)", r"^(序章|序言|引子|引言|前言|楔子|后记|后序|尾声|终章|终幕|番外|外传|附录|彩蛋|特别篇|完本感言)"),
]

# 卷 / 部 / 篇 标记
VOLUME_PATTERNS: List[tuple] = [
    ("第X卷", r"^第[零一二三四五六七八九十百千两万千亿\d]+[卷部篇]"),
    ("卷X", r"^[卷部篇][零一二三四五六七八九十百千两万千亿\d]+"),
    ("上下卷", r"^[上下中]+[卷部篇]"),
    ("英文卷", r"^(volume|book|part)\s+[\dIVXLC]+"),
    # T2.4（2026-09-02）：古典英文卷归属
    # 覆盖 "Book the First/Second/Third"（A Tale of Two Cities）与
    # "Book I/II/III"（已有正则也覆盖）。不带 $ 锚点以兼容"Book the First--Recalled to Life"
    # 这种「卷名+--副标题」的 Dickens 风格。后续的 _normalize_volume_label 会从行首提取
    # 规范标签，所以无需担心误命中正文里的 "Book the first time..."。
    ("英文卷全", r"^[Bb]ook\s+(?:the\s+)?(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|[IVXLCDM]+)[\.\s]?(?:--|\s|$)?"),
]

# 常见广告 / 水印行（清理开关开启时剔除）
# 分强弱两级：强标记几乎不可能出现在正文，命中即删；
# 弱标记（微信/下载/月票等）在正文中高频出现，需配合"短行无标点"才删（见 _is_ad_line）
_AD_STRONG = [
    "求月票", "求收藏", "求推荐", "求订阅", "求票", "月票推荐票", "求评价票",
    "求鲜花", "投喂", "催更", "上架感言", "作者感言", "完本感言", "书友群",
    "读者群", "qq群", "q群", "公众号", "作者新书", "笔趣阁", "八一中文",
    "本章说", "防盗章", "防复制", "无弹窗", "最新章节", "章节错误", "请报告",
    "手机阅读", "本书首发", "更新最快", "请收藏", "天才一秒记住", "一秒记住",
    "txt下载", "全文阅读", "顶点小说", "思路客",
]
_AD_WEAK = [
    "月票", "推荐票", "打赏", "赏金", "感言", "微信", "下载", "新书",
    "ps：", "ps:", "p.s", "enjoyment",
]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class ChapterBlock:
    index: int
    title: str
    content: str
    word_count: int
    volume: Optional[str] = None
    volume_index: int = 0
    num: Optional[int] = None


@dataclass
class SplitResult:
    chapters: List[ChapterBlock] = field(default_factory=list)
    total_chapters: int = 0
    total_words: int = 0
    total_volumes: int = 0
    dedup_count: int = 0
    detected_pattern: str = ""
    pattern_name: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SplitOptions:
    pattern: str = ""          # 自定义正则（mode=custom 时生效）
    mode: str = "auto"         # auto | custom
    use_volume: bool = True    # 是否识别卷/部/篇层级
    min_words: int = 0         # 小于该字数的章：跳过（merge_tiny=False）或合并（True）
    merge_tiny: bool = False    # 短章合并到下一章而非丢弃
    max_words: int = 0         # 大于该字数的章：自动拆分为多块（0=不拆分）
    remove_ads: bool = False   # 清理广告 / 水印行
    strip_whitespace: bool = True
    scan_lines: int = 800      # 自动检测时扫描的前 N 行


# ---------------------------------------------------------------------------
# 编码 / 读取
# ---------------------------------------------------------------------------
# 与其他入口（file_processor / routes_prompt 等）共用 ENCODING_CANDIDATES 常量，
# 包含 gb18030（GBK 超集，能解 GBK 边界字符）和 latin-1 兜底。
# P3 修复（2026-08-27）：之前的本地列表缺 gb18030，导致 4.5MB GBK 小说在
# 256KB 采样边界切到 2-byte 字符尾字节时，gbk 解码失败，splitter 路径
# 抛 "无法使用任何编码读取文件"。与 file_processor.py 行为对齐后，splitter
# 与 file_processor 选编码结果一致。
def _normalize_content(content: str) -> str:
    """统一的后处理：剥 BOM + 换行归一。TXT / DOCX 两条路径共用，确保形态严格一致。"""
    if content.startswith("\ufeff"):
        content = content[1:]
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _detect_and_read(file_path: Path) -> str:
    """读取源文件并归一化为「一行一个语义单元」的纯文本。

    这是全链路唯一的文本入口（调用方仅 preview_split / save_split 两处）。
    15 条章正则全部带 `^` 行首锚点并配套 `len(s) <= 80` 整行闸门，其成立前提
    是「一行一个语义单元」——TXT 天然满足，DOCX 需要显式重建，故在此唯一入口
    做格式分派，下游切章逻辑零改动。
    """
    suffix = file_path.suffix.lower()
    if suffix == ".docx":
        logger.info(f"检测到 DOCX 输入，走 Word 正文抽取: {file_path.name}")
        content = extract_docx_text(file_path)
    else:
        content = detect_and_decode(file_path, ENCODING_CANDIDATES)
    return _normalize_content(content)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
_ROMAN_MAP = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _roman_to_int(s: str) -> Optional[int]:
    """把罗马数字转换为 int；非合法罗马数字返回 None。

    防御：
      - 超过 12 字符 → 拒绝（避免误命中长行）
      - 单字符 I 保留（I=1），其他单字符拒绝（避免代词 I 等误命中）
    """
    if not s or len(s) > 12:
        return None
    if len(s) < 2 and s != "I":
        return None
    total = 0
    prev = 0
    for c in reversed(s):
        if c not in _ROMAN_MAP:
            return None
        cur = _ROMAN_MAP[c]
        if cur < prev:
            total -= cur
        else:
            total += cur
        prev = cur
    return total if total > 0 else None


_WORD_TO_INT = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100,
}


def _word_to_int(word: str) -> Optional[int]:
    """英文数词 → int。处理 "twenty-one" / "twenty one" / "twenty" 等。"""
    if word in _WORD_TO_INT:
        return _WORD_TO_INT[word]
    for sep in ("-", " "):
        if sep in word:
            parts = word.split(sep)
            total = 0
            for p in parts:
                v = _WORD_TO_INT.get(p)
                if v is None:
                    return None
                total += v
            return total
    return None


def _extract_chapter_num(title: str) -> Optional[int]:
    """从标题里提取章节号（中文/阿拉伯/罗马/英文数词），没有则返回 None。"""
    # 中文「第 X 章 / 回 / 节」
    m = re.search(r"第\s*([0-9零一二三四五六七八九十百千两万千亿]+)\s*[章回节]", title)
    if m:
        return cn_to_int(m.group(1))
    # 英文 Chapter <num>
    m = re.search(r"chapter\s+(\d+)", title, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # T2.1b（2026-09-02）：英文 Chapter <Roman>
    # v2（2026-09-02）：允许 Roman 后无 . 空白 :（Dracula 真实章节是 "CHAPTER I" 无尾标点）
    m = re.search(r"chapter\s+([IVXLCDM]+)[\.\s:]?", title, re.IGNORECASE)
    if m:
        val = _roman_to_int(m.group(1).upper())
        if val is not None:
            return val
    # T2.1b（2026-09-02）：英文 Chapter <英文数词>（含连字符/空格复合）
    m = re.search(r"chapter\s+([\w\- ]{1,30}?)\b[\s\.:]?", title, re.IGNORECASE)
    if m:
        word = m.group(1).strip().lower()
        val = _word_to_int(word)
        if val is not None:
            return val
    # T2.1b（2026-09-02）：裸 Roman 短篇（SHERLOCK/Gatsby 等）
    m = re.match(r"^\s*([IVXLCDM]+)[\.\s]\s*[A-Z]", title)
    if m:
        val = _roman_to_int(m.group(1).upper())
        if val is not None:
            return val
    # T2.1b（2026-09-02）：裸 Roman 独行（Gatsby 风格 "I" / "II"）
    m = re.match(r"^\s*([IVXLCDM]{1,5})\s*$", title)
    if m:
        val = _roman_to_int(m.group(1).upper())
        if val is not None:
            return val
    # 阿拉伯数字 1./2. 起始
    m = re.match(r"^\s*([0-9]+)[\.、]", title)
    if m:
        return int(m.group(1))
    return None


def _compile_volume_patterns() -> List[re.Pattern]:
    return [re.compile(p, re.MULTILINE) for _, p in VOLUME_PATTERNS]


def _normalize_volume_label(line: str) -> Optional[str]:
    """从『第一卷 风起青萍』『第一卷第一章』『卷二』『Part I』『Book the First』等行里提取规范卷标签。

    统一输出形如「第X卷 / 卷X / 上卷 / Part I / Book I」，保证『独立卷行』与
    『第X卷第Y章』合并写法被归到同一个卷标签下。

    T2.4（2026-09-02）：新增『Book the First/Second/Third』支持（Dickens 等）。
    把英文序数词 first/second/third... 转为 Roman numeral 以保持标签一致。
    """
    m = re.match(r"^(?:第)?\s*([零一二三四五六七八九十百千两万千亿\d]+)\s*([卷部篇])", line, re.IGNORECASE)
    if m:
        return f"第{m.group(1)}{m.group(2)}"
    m = re.match(r"^(卷|部|篇)\s*([零一二三四五六七八九十百千两万千亿\d]+)", line, re.IGNORECASE)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    m = re.match(r"^([上下中])\s*([卷部篇])", line, re.IGNORECASE)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    m = re.match(r"^(volume|book|part)\s+([\dIVXLC]+)", line, re.IGNORECASE)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    # T2.4：Book the First/Second/Third/...（允许后续接 `--Title` 或行尾）
    m = re.match(
        r"^[Bb]ook\s+(?:the\s+)?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth)",
        line, re.IGNORECASE,
    )
    if m:
        word_to_roman = {
            "first": "I", "second": "II", "third": "III", "fourth": "IV",
            "fifth": "V", "sixth": "VI", "seventh": "VII", "eighth": "VIII",
            "ninth": "IX", "tenth": "X", "eleventh": "XI", "twelfth": "XII",
        }
        roman = word_to_roman[m.group(1).lower()]
        return f"Book {roman}"
    return None


def _is_volume_start(line: str) -> Optional[str]:
    # 保守排除："中篇小说连载中/上篇简介" 等下载站前奏行不是卷标记，
    # 但会命中 "^[上下中]+[卷部篇]"（"中篇"），导致卷归属写错
    if "小说" in line or "连载" in line:
        return None
    for _, p in VOLUME_PATTERNS:
        if re.match(p, line, re.IGNORECASE):
            return _normalize_volume_label(line) or line.strip()
    return None


def _chapter_regex_for_mode(options: SplitOptions, lines: List[str]) -> tuple:
    """返回 (compiled_regex, pattern_name)。

    - pattern_name：扫描前 N 行打分选出的「主导格式」（仅用于展示）。
    - compiled_regex：合并【所有】候选章节正则的主正则，确保 章/回/节/序章/楔子/
      番外/尾声/英文 Chapter/数字编号 等任意形态都能被识别（功能最全）。
    """
    if options.mode == "custom" and options.pattern and options.pattern.strip():
        try:
            custom_rx = re.compile(options.pattern, re.MULTILINE)
        except re.error as e:
            # 正则语法错误，回退自动检测（之前行为：编译失败也回退）
            logger.warning(f"自定义正则编译失败 ({e})，回退自动检测")
        else:
            # P4 修复（2026-08-27）：编译成功但在前 N 行 0 命中，也回退 auto
            # 之前会沉默地把整本书当 1 章，split_report.txt 显示「总章节数 1」，
            # GUI 上看起来「切分成功」但用户拿到的是 1 个 2MB 的伪章节。
            # 真实案例：用户 mode=custom + 第X回 pattern（书用「章」不用「回」），
            # 或者 mode=custom + min_words=10000（其他章节都不到 10000 字被过滤）。
            # 复用 _chapter_regex_for_mode 的 scan_lines 做命中统计：
            sample = lines[: options.scan_lines]
            hits = sum(
                1 for ln in sample
                if ln.strip() and len(ln.strip()) <= 60 and custom_rx.match(ln.strip())
            )
            if hits > 0:
                return custom_rx, "自定义正则"
            # 0 命中：可能 pattern 错了（用户写了「第X回」但书是「第X章」），
            # 也可能书前 N 行恰好没章节标题（罕见）。无论哪种，回退 auto 都比
            # 把整本书当 1 章更友好。
            logger.warning(
                f"自定义正则在前 {options.scan_lines} 行 0 命中 (pattern={options.pattern!r})，"
                f"回退自动检测")
            # fall through to auto detection

    # 自动检测：对每个候选正则在前 N 行打分，选出主导格式用于展示
    scores: Dict[str, int] = {}
    for name, pat in CHAPTER_PATTERNS:
        try:
            rx = re.compile(pat, re.MULTILINE | re.IGNORECASE)
        except re.error:
            continue
        score = 0
        for line in lines[: options.scan_lines]:
            s = line.strip()
            if not s or len(s) > 60:
                continue
            if rx.match(s):
                score += 1
        scores[name] = score

    best_name = max(scores, key=lambda k: scores[k]) if scores else ""
    pattern_name = best_name if best_name and scores[best_name] > 0 else "兜底第X章"

    # 特章正则（序章/楔子/番外/尾声…）：无论主导格式如何都保留识别能力
    special_patterns = [pat for name, pat in CHAPTER_PATTERNS if name.startswith("特章")]
    special_alt = "|".join(special_patterns)

    # P2-11 + G6 收口（2026-08-24）：主导格式足够强时收窄为
    # 「主导 + 特章 + 达标次级」。次级入选双门槛：绝对得分 ≥ 2（排掉孤例噪声）
    # 且得分 ≥ 主导的 15%（排掉数量远超主导的高频噪声列表——那正是 P2-11 要防的
    # 过度切分场景）。合法卷章混排与主导同量级，必然入选。
    if best_name and scores[best_name] >= 5 and not best_name.startswith("特章"):
        _pat_by_name = {name: pat for name, pat in CHAPTER_PATTERNS}
        best_score = scores[best_name]
        combined = f"(?:{_pat_by_name[best_name]})"
        if special_alt:
            combined += f"|(?:{special_alt})"
        included_secondary = 0
        threshold = max(2, int(best_score * 0.15))
        for name, pat in CHAPTER_PATTERNS:
            if name == best_name or name.startswith("特章"):
                continue
            sc = scores.get(name, 0)
            if sc >= threshold:
                combined += f"|(?:{pat})"
                included_secondary += 1
        if included_secondary:
            logger.info(f"次级章节格式并入切分: {included_secondary} 种 "
                        f"(阈值≥{threshold})")
        return re.compile(combined, re.MULTILINE | re.IGNORECASE), pattern_name

    # 兜底：主导格式得分不足（前 N 行样本太少/无主导格式）时回退全量合并大网
    combined = "(" + ")|(".join(p for _, p in CHAPTER_PATTERNS) + ")"
    master = re.compile(combined, re.MULTILINE | re.IGNORECASE)
    return master, pattern_name


def _extract_metadata(
    lines: List[str],
    filename: str,
    external_title: str = "",  # 新增：来自文件名推断的权威书名（save_to_workspace 传入）
) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    # 外部权威书名优先：来自 save_to_workspace 的 book_name 参数
    # （已通过 infer_book_name 从《...》文件名推断；与正文乱猜相比更准确）
    if external_title:
        return {"title": external_title.strip(), "author": ""}
    for i, raw in enumerate(lines[:60]):
        line = raw.strip()
        if not line or len(line) > 120:
            continue
        # 书名：含《》的行，或前 8 行内且没有章节关键字的短行
        if not meta.get("title"):
            m = re.search(r"[《]([^》]{1,40})[》]", line)
            if m:
                meta["title"] = m.group(1)
            elif i < 8 and not any(k in line for k in ("第", "章", "回", "卷", "Chapter")):
                meta["title"] = line
        # 作者
        if not meta.get("author"):
            m = re.search(r"(?:作者|著)\s*[:：]?\s*(.+)", line)
            if m:
                meta["author"] = m.group(1).strip()
    if not meta.get("title"):
        meta["title"] = filename
    return meta


# T2.7（2026-09-02）：从 Gutenberg 头（pre-START）提取 Title/Author
# baseline 暴露 Bug-7：所有 7 本 title 退步到 "[Illustration:" / "MOBY-DICK;" / "A TALE OF TWO CITIES"，
# 根因是 T2.5 截断后留下的头部不干净（Pride 的出版页/Dracula 的版权页等），_extract_metadata 的
# "前 8 行内短行" 策略误命中了这些行。修法：单独扫 pre-START 头，识别 PG 显式的 `Title:`/`Author:` 行
# 或第 1 行的 `The Project Gutenberg eBook of X`。
def _extract_pg_header_meta(content: str) -> Dict[str, str]:
    """从 Gutenberg 头（pre-START 段）提取 Title/Author。

    PG header 典型格式：
        The Project Gutenberg eBook of Pride and Prejudice
        This eBook is for the use of anyone ...
        ...
        Title: Pride and Prejudice
        Author: Jane Austen
        ...
        *** START OF ...

    优先识别 `Title: X` / `Author: X` 显式行；找不到再回退到第 1 行的
    `The Project Gutenberg eBook of X`。
    """
    meta: Dict[str, str] = {}
    start_idx = content.find("*** START OF")
    pre_start = content[:start_idx] if start_idx != -1 else content[:2000]

    # 1. 显式 Title: 行
    m = re.search(r"^Title:\s*(.+)$", pre_start, re.MULTILINE | re.IGNORECASE)
    if m:
        meta["title"] = m.group(1).strip()
    else:
        # 2. 兜底：第 1 行的 "The Project Gutenberg eBook of X"
        m = re.search(r"^The Project Gutenberg eBook of (.+?)\s*$",
                      pre_start, re.MULTILINE | re.IGNORECASE)
        if m:
            meta["title"] = m.group(1).strip()

    # 1. 显式 Author: 行
    m = re.search(r"^Author:\s*(.+)$", pre_start, re.MULTILINE | re.IGNORECASE)
    if m:
        meta["author"] = m.group(1).strip()

    return meta


# ---------------------------------------------------------------------------
# Gutenberg 头截断（T2.5 + T2.5b，2026-09-02）
# ---------------------------------------------------------------------------
def _strip_gutenberg_header(content: str) -> str:
    """Gutenberg 头截断 + TOC 块跳过。

    T2.5（2026-09-02）：找到 '*** START OF PROJECT GUTENBERG' 标记后，截掉到
    第一个真章节行之间的版权页/Table of Contents/序言/题献等内容。

    T2.5b（2026-09-02）：baseline 发现 T2.5 在 2 本（Moby-Dick / Dracula）上
    误把 TOC 块里的 "CHAPTER 1." 当成"第一个真章节"截断，导致 TOC 和正文双重
    匹配（Moby-Dick 270 章 = 135 TOC + 135 正文）。新增：在 START 后 100 行内
    识别 CONTENTS / Table of Contents 标记，整块跳过后再找真章节。

    处理流程：
      1. 找 '*** START OF' 标记
      2. 在 START 后 100 行内找 CONTENTS / Table of Contents 标记
         - 找到则将搜索起点跳到该行末尾（TOC 块 + 起始标记一并跳过）
      3. 从搜索起点起 50 行内找第一个真章节行
         - 真章节候选：Chapter N/Chapter Roman/Book the X/Chinese Chapter/
           Sherlock "I. TITLE"/Gatsby bare Roman/特章
         - 找到且 < 50 行 → 从该行开始截
      4. 兜底：仅跳过 CONTENTS 标记（或 START 标记本身）

    不会动尾（license / donation links），尾由 数字点编号 正则收紧配合处理。

    Gutenberg 文件典型结构：
      - 头（版权页）：Title / Author / Release Date / Language / Produced by
      - 分割符：*** START OF (THE|THIS) PROJECT GUTENBERG EBOOK ... ***
      - 正文：第一章开始（含可能夹一段 TOC / 序言 / 题献 / 出版页）
      - 分割符：*** END OF (THE|THIS) PROJECT GUTENBERG EBOOK ... ***
    """
    start_idx = content.find("*** START OF")
    if start_idx == -1:
        return content

    after_start = content[start_idx:]
    # 跳过 START 标记所在行
    nl = after_start.find("\n")
    after_start_no_start = after_start[nl + 1:] if nl != -1 else ""

    # T2.5b：在 after_start_no_start 前 100 行内找 CONTENTS 标记
    # 触发条件：独立的 "CONTENTS" / "Table of Contents" 行（允许前导空白）。
    contents_m = re.search(
        r"^[\s]*(?:CONTENTS|Table of Contents)\s*$",
        after_start_no_start,
        re.MULTILINE | re.IGNORECASE,
    )
    if contents_m and after_start_no_start[: contents_m.start()].count("\n") <= 100:
        # 找到 CONTENTS 标记 → 跳到 CONTENTS 行末尾
        search_text = after_start_no_start[contents_m.end():]
    else:
        # 没找到 CONTENTS → 直接从 START 后开始搜
        search_text = after_start_no_start

    # T2.5b（v2）：找第一个有 body 紧随的章节/卷行（区别于 TOC 条目）
    real_offset = _find_first_real_chapter_or_volume(search_text)
    if real_offset is not None:
        return search_text[real_offset:]

    # 兜底：找不到真章节 → 跳过 CONTENTS/START 标记，保留之后内容
    return search_text


def _find_first_real_chapter_or_volume(text: str) -> Optional[int]:
    """在 text 中找第一个有 body 紧随的章节/卷行（区别于 TOC 条目）。

    判定逻辑（v2, 2026-09-02）：
      - 候选章节/卷行之后的 12 行内（跳过空行）：
        - 出现 2 个连续的非空章节/卷标记 → 还在 TOC，跳过
        - 出现非章节/卷 且非标题续行的"段落"内容 → 真章节/卷，OK
      - 章节标题续行（如 Moby-Dick 的 2 行标题、Tale 的"Book the First--Recalled to Life"
        之后的"The Period"）由 _is_title_continuation 识别，跳过但继续扫描。

    候选正则：T2.5 原有 + T2.5b 补充 Sherlock/Gatsby 风格。

    返回该行相对 text 起点的字节偏移；找不到返回 None。
    """
    chapter_pats = [
        re.compile(r"^[Cc]hapter\s+[IVXLCDM\d]", re.IGNORECASE),
        re.compile(r"^[Bb]ook\s+(?:the\s+)?[A-ZIVX\d]", re.IGNORECASE),
        re.compile(r"^第[零一二三四五六七八九十百千两万千亿\d]+[章回节]"),
        re.compile(r"^第\d+章"),
        re.compile(r"^序章|^序言|^引子|^楔子|^正文"),
        re.compile(r"^[IVXLCDM]+\.\s+[A-Z][A-Z\s,'\u2019\-:]{2,80}$"),
        re.compile(r"^\s*(?:I{1,3}|IV|VI{0,3}|IX|X|XI{0,2}|XIV|XV|XIX|XX)\s*$"),
    ]
    lines = text.split("\n")
    # 最多扫 2000 行（保护：超长非 Gutenberg 文件不卡死）
    max_scan = min(len(lines), 2000)
    for i in range(max_scan):
        s = lines[i].strip()
        if not s:
            continue
        is_candidate = any(rx.match(s) for rx in chapter_pats)
        if not is_candidate:
            continue
        # 检查后续 12 行
        body_found = False
        chapter_after_count = 0
        for j in range(i + 1, min(i + 13, len(lines))):
            ss = lines[j].strip()
            if not ss:
                continue
            is_cand_after = any(rx.match(ss) for rx in chapter_pats)
            if not is_cand_after:
                if not _is_title_continuation(ss):
                    # 真正的段落内容
                    body_found = True
                    break
                # 标题续行，继续
                continue
            # 章节/卷标记
            chapter_after_count += 1
            if chapter_after_count >= 2:
                # 2 个连续章节/卷 → 还在 TOC
                break
        if body_found:
            return sum(len(l) + 1 for l in lines[:i])
    return None


def _is_title_continuation(s: str) -> bool:
    """判断一行是否是章节标题的续行。

    启发式（v2, 2026-09-02）：标题续行通常 ≤ 5 词、仅末尾 1 个句点、无段落标点。
    段落通常 ≥ 6 词、含分号/冒号/破折号/括号，或有多个句点（多句）。

    测试用例：
      - "Pictures of Whaling Scenes."（4 词, 1 句点）→ True
      - "Stone; in Stars."（3 词, 1 句点, 含 `;`）→ True（标题续行也可能含 `;`）
      - "Loomings."（1 词）→ True
      - "The Period"（2 词）→ True
      - "JONATHAN HARKER'S JOURNAL"（3 词）→ True（Dracula 副标题）
      - "It was the best of times..."（12 词）→ False
      - "(_Kept in shorthand._)"（3 词）→ True
    """
    if len(s) > 80:
        return False
    word_count = len(s.split())
    if word_count >= 6:
        return False
    # 段落有多个句点（多句）
    if s.count(".") > 1:
        return False
    return True


def _dedup_same_key(blocks: List[ChapterBlock]) -> tuple:
    """同号去重：同 (volume, num) 出现多次时，仅删除字数 < 阈值的疑似 TOC 条目。

    用途：T2.5b 未完全跳过 TOC 时，TOC 条目与正文同号章节并存。
    正文章节通常 10000+ 字，TOC 条目通常 0-500 字。

    保守策略（避免误删真实短章）：
      - 仅当某条 word_count < 1000 且同 key 存在 word_count >= 5000 的章节时
        才把它视为 TOC 残留删除。
      - 所有同 key 章节字数都 < 1000 → 一律保留（可能真的都是短章）。
      - 所有同 key 章节字数都 >= 1000 → 一律保留（可能真的都是正文章节）。

    返回 (new_blocks, removed_count)。
    """
    if not blocks:
        return blocks, 0
    groups: Dict[tuple, List[int]] = {}
    for i, blk in enumerate(blocks):
        key = (blk.volume, blk.num)
        groups.setdefault(key, []).append(i)
    to_remove: set = set()
    TOC_THRESHOLD = 1000
    REAL_THRESHOLD = 5000
    for key, indices in groups.items():
        if len(indices) <= 1:
            continue
        # 是否存在"真章节"（>= REAL_THRESHOLD 字）
        real_indices = [i for i in indices if blocks[i].word_count >= REAL_THRESHOLD]
        if not real_indices:
            continue  # 全是短章（< 5000 字），不删
        # 删除 TOC 残留（< TOC_THRESHOLD 字）且同 key 有真章节
        for i in indices:
            if blocks[i].word_count < TOC_THRESHOLD:
                to_remove.add(i)
    new_blocks = [b for i, b in enumerate(blocks) if i not in to_remove]
    return new_blocks, len(to_remove)


# ---------------------------------------------------------------------------
# 核心切分
# ---------------------------------------------------------------------------
def split_text(
    content: str,
    options: Optional[SplitOptions] = None,
    external_title: str = "",  # 新增：来自 save_split / save_to_workspace 透传的权威书名
) -> SplitResult:
    if options is None:
        options = SplitOptions()
    # T2.7（2026-09-02）：先从 pre-START 头提取 Title/Author（必须在 T2.5 截断前做，
    # 否则 Title: / Author: 行被切掉）。后续 _extract_metadata 仅作 fallback。
    pg_meta = _extract_pg_header_meta(content)
    # T2.5 + T2.5b（2026-09-02）：Gutenberg 头截断 + TOC 跳过
    # 解决 baseline 暴露的 Bug-1（license 段被当章节切）+ Bug-2（Moby-Dick TOC 重复）。
    content = _strip_gutenberg_header(content)
    lines = content.split("\n")

    chapter_rx, pattern_name = _chapter_regex_for_mode(options, lines)
    volume_rxs = _compile_volume_patterns() if options.use_volume else []

    # 第一遍：定位所有「章/卷」起始行
    raw_blocks: List[Dict[str, Any]] = []
    current_volume: Optional[str] = None
    current_volume_index = 0

    for idx, raw in enumerate(lines):
        s = raw.strip()
        # 卷标记
        if volume_rxs:
            vol = _is_volume_start(s)
            if vol:
                # 仅当该行不像章节时才作为卷标记（卷行不会以『章/回/节』结尾）
                if not re.search(r"[章回节]$", s):
                    current_volume = vol
                    current_volume_index += 1
                    continue
        # 章节标记（含『第X卷第Y章』合并写法，本行同时带卷号）
        if chapter_rx.match(s) and 0 < len(s) <= 80:
            # 合并写法：本行开头即卷号，更新当前卷（与独立卷行共用同一卷标签）
            vol_label = _normalize_volume_label(s)
            if vol_label and vol_label != current_volume:
                current_volume = vol_label
                current_volume_index += 1
            title = s
            raw_blocks.append({
                "title": title,
                "start": idx + 1,            # 下一行开始
                "volume": current_volume,
                "volume_index": current_volume_index,
            })

    if not raw_blocks:
        # 无法识别：整体作为单章
        text = content.strip() if options.strip_whitespace else content
        block = ChapterBlock(1, "全文", text, len(text))
        return SplitResult(
            chapters=[block], total_chapters=1, total_words=len(text),
            metadata=_extract_metadata(lines, "", external_title),
            detected_pattern=chapter_rx.pattern, pattern_name=pattern_name,
        )

    # 第二遍：切出每块正文
    blocks: List[ChapterBlock] = []
    for i, b in enumerate(raw_blocks):
        end = raw_blocks[i + 1]["start"] - 1 if i + 1 < len(raw_blocks) else len(lines)
        body_lines = lines[b["start"]:end]
        if options.remove_ads:
            body_lines = [ln for ln in body_lines if not _is_ad_line(ln)]
        body = "\n".join(body_lines)
        if options.strip_whitespace:
            body = body.strip()
        title = b["title"]
        num = _extract_chapter_num(title)
        block = ChapterBlock(
            index=0,
            title=title,
            content=(title + "\n" + body) if body else title,
            word_count=len(body),
            volume=b["volume"],
            volume_index=b["volume_index"],
            num=num,
        )
        blocks.append(block)

    # 正文开头（第一个章节之前的内容）拼到首章
    if raw_blocks and raw_blocks[0]["start"] > 0:
        pre = "\n".join(lines[: raw_blocks[0]["start"]])
        if options.remove_ads:
            pre_lines = [ln for ln in pre.split("\n") if not _is_ad_line(ln)]
            pre = "\n".join(pre_lines)
        pre = pre.strip() if options.strip_whitespace else pre
        if pre:
            first = blocks[0]
            first.content = pre + "\n\n" + first.content
            first.word_count = len(first.content) - len(first.title)

    # 相邻去重（常见于防盗章 / 重复章）
    deduped: List[ChapterBlock] = []
    prev_hash: Optional[str] = None
    dedup_count = 0
    for blk in blocks:
        h = hashlib.md5(blk.content.encode("utf-8")).hexdigest()
        if h == prev_hash:
            dedup_count += 1
            continue
        prev_hash = h
        deduped.append(blk)
    blocks = deduped

    # T2.5b（v2 增强，2026-09-02）：同号去重
    # baseline 暴露：当 T2.5b 未完全跳过 TOC（如 Dracula/Gatsby/Moby-Dick 的最后 1-2 个
    # TOC 条目紧跟 preface/poem/notes，body check 误判为"真章节"）时，主扫描会同时命中
    # TOC 和正文的同号章节（CONTENTS 1-27 / CHAPTER 1-135 / I-IX）。内容 MD5 不同导致
    # 相邻去重无效。
    # 修法：同 (volume, num) 出现多次时，保留字数最多的那一个（正文章节通常 10000+ 字，
    # TOC 条目通常 0-500 字）。
    blocks, same_key_dedup = _dedup_same_key(blocks)
    dedup_count += same_key_dedup

    # 最小字数：跳过 or 合并短章
    if options.min_words > 0:
        pre_filter_count = len(blocks)
        if options.merge_tiny:
            blocks = _merge_tiny(blocks, options.min_words)
        else:
            blocks = [b for b in blocks if b.word_count >= options.min_words or b.num is None]
        # P4 修复（2026-08-27）：min_words 过滤后剩余章节过少时直接报错，
        # 避免「切分成功」但只切出 1 章的沉默失败。
        # 真实案例：《从姑获鸟开始》6.3MB 切出 768 章，仅第 1 章 (20568 字) 超过
        # min_words=10000 阈值，其余 767 章 < 10000 字全被过滤 → 剩 1 章。
        # 阈值定 5 章：低于 5 章基本不可能是用户期望的"长篇切分"。
        if len(blocks) < 5 and pre_filter_count >= 5:
            raise ValueError(
                f"min_words={options.min_words} 过高：切出 {pre_filter_count} 个章节，"
                f"过滤后仅剩 {len(blocks)} 个（阈值过滤了 {pre_filter_count - len(blocks)} 个）。"
                f"网文章节通常 2000-5000 字，建议把 min_words 调到 200-500，"
                f"或设为 0 不过滤。")

    # 超大章自动拆分
    if options.max_words > 0:
        blocks = _split_oversized(blocks, options.max_words)

    # 重新编号
    for i, b in enumerate(blocks, start=1):
        b.index = i

    # 统计
    counts = [b.word_count for b in blocks] or [0]
    counts_sorted = sorted(counts)
    stats = {
        "avg": sum(counts) // len(counts),
        "min": min(counts),
        "max": max(counts),
        "median": counts_sorted[len(counts_sorted) // 2],
    }
    volumes = sorted({b.volume for b in blocks if b.volume})
    # T2.7（2026-09-02）：pre-START 提取的 Title/Author 优先（覆盖 lines 提取）。
    # baseline 显示 _extract_metadata 的"前 8 行短行"策略对 PG header 退化成
    # `[Illustration:` / `CHAPTER I. ...` 等。pg_meta（来自 Title: / Author: 显式行）
    # 准确度更高，必须覆盖而非 fallback。
    metadata = _extract_metadata(lines, "", external_title)
    for k, v in pg_meta.items():
        if v:
            metadata[k] = v

    return SplitResult(
        chapters=blocks,
        total_chapters=len(blocks),
        total_words=sum(counts),
        total_volumes=len(volumes),
        dedup_count=dedup_count,
        detected_pattern=chapter_rx.pattern,
        pattern_name=pattern_name,
        metadata=metadata,
        stats=stats,
    )


def _is_ad_line(line: str) -> bool:
    s = line.strip().lower()
    if not s:
        return False
    if len(s) > 200:
        return False
    # 强广告标记：命中即删（公众号/书友群/笔趣阁 等几乎不可能出现在正文）
    if any(k in s for k in _AD_STRONG):
        return True
    # 弱标记（微信/下载/月票/推荐票 等会出现在正文里）：
    # 仅当"短行 + 无句子标点"才视为广告，避免误删"他打开微信看到消息"这类正文行
    if any(k in s for k in _AD_WEAK):
        return len(s) <= 40 and not any(p in s for p in "，。！？；：,;!?")
    return False


def _merge_tiny(blocks: List[ChapterBlock], min_words: int) -> List[ChapterBlock]:
    """把短于 min_words 的章合并进下一章（末尾则并入上一章）。"""
    merged: List[ChapterBlock] = []
    for blk in blocks:
        if blk.word_count < min_words and blk.num is not None:
            if merged:
                prev = merged[-1]
                prev.content += "\n\n" + blk.content
                prev.word_count = len(prev.content) - len(prev.title)
            else:
                merged.append(blk)
        else:
            merged.append(blk)
    # 末尾短章并入上一章
    if len(merged) >= 2 and merged[-1].word_count < min_words:
        last = merged.pop()
        merged[-1].content += "\n\n" + last.content
        merged[-1].word_count = len(merged[-1].content) - len(merged[-1].title)
    return merged


def _split_oversized(blocks: List[ChapterBlock], max_words: int) -> List[ChapterBlock]:
    """把超过 max_words 的章按段落拆成多块，标题加 (n/m) 后缀。

    标题固定保留在第 1 块；若某个段落本身超过 max_words，则按字符硬切。
    """
    out: List[ChapterBlock] = []
    for blk in blocks:
        if blk.word_count <= max_words:
            out.append(blk)
            continue
        parts = blk.content.split("\n", 1)
        title_line = parts[0]
        body = parts[1] if len(parts) > 1 else ""
        paragraphs = body.split("\n") if body else [""]

        # 超长单段落按字符硬切
        norm_paras: List[str] = []
        for p in paragraphs:
            if len(p) > max_words:
                for i in range(0, len(p), max_words):
                    norm_paras.append(p[i:i + max_words])
            else:
                norm_paras.append(p)

        # 按 max_words 累积成块
        chunks: List[List[str]] = [[]]
        size = 0
        for p in norm_paras:
            if size + len(p) + 1 > max_words and chunks[-1]:
                chunks.append([])
                size = 0
            chunks[-1].append(p)
            size += len(p) + 1

        # 末块过小则并入上一块，避免无意义的碎片续章
        if len(chunks) > 1:
            last_len = sum(len(p) for p in chunks[-1])
            if last_len < max(200, max_words // 10):
                chunks[-2].extend(chunks[-1])
                chunks.pop()

        total = len(chunks)
        for i, ch in enumerate(chunks, start=1):
            suffix = f" ({i}/{total})" if total > 1 else ""
            text = (title_line + suffix + "\n" + "\n".join(ch)) if ch else (title_line + suffix)
            out.append(ChapterBlock(
                index=0,
                title=blk.title + suffix,
                content=text,
                word_count=len(text),
                volume=blk.volume,
                volume_index=blk.volume_index,
                num=blk.num,
            ))
    return out


# ---------------------------------------------------------------------------
# 对外接口（保持旧签名 + 新增 options）
# ---------------------------------------------------------------------------
def preview_split(
    file_path: Path,
    options: Optional[SplitOptions] = None,
    preview_count: int = 50,
) -> Dict[str, Any]:
    if options is None:
        options = SplitOptions()
    content = _detect_and_read(file_path)
    result = split_text(content, options)
    preview_blocks = result.chapters[:preview_count]
    return {
        "chapters": [
            {
                "index": b.index,
                "title": b.title,
                "word_count": b.word_count,
                "volume": b.volume,
                "num": b.num,
            }
            for b in preview_blocks
        ],
        "total_chapters": result.total_chapters,
        "total_words": result.total_words,
        "total_volumes": result.total_volumes,
        "dedup_count": result.dedup_count,
        "detected_pattern": result.detected_pattern,
        "pattern_name": result.pattern_name,
        "metadata": result.metadata,
        "stats": result.stats,
        "preview_count": len(preview_blocks),
    }


def save_split(
    file_path: Path,
    output_dir: Path,
    options: Optional[SplitOptions] = None,
    external_title: str = "",  # 新增
) -> Dict[str, Any]:
    if options is None:
        options = SplitOptions()
    content = _detect_and_read(file_path)
    result = split_text(content, options, external_title=external_title)
    output_dir.mkdir(parents=True, exist_ok=True)

    # P1 修复（2026-08-24）：先全部暂存到 _split_staging，全部成功后才清理旧章并交换。
    # 原实现先删旧 txt 再并行写新文件，中途任一写入失败会留下
    # 「旧章已删、新章残缺、chapters.json 还指向已删除文件」的不一致状态。
    chapters = result.chapters
    if chapters:
        staging_dir = output_dir / "_split_staging"
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        staging_dir.mkdir(parents=True)
        try:
            max_workers = min(32, (os.cpu_count() or 4) + 4)

            def _stage(b):
                (staging_dir / f"{b.index:04d}.txt").write_text(b.content, encoding="utf-8")

            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                list(ex.map(_stage, chapters))
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise  # 旧 blocks 一个都没动过

        # 暂存全部成功后才进入破坏性阶段：清理旧章（含网络盘延迟优化注释保留）
        for stale in output_dir.glob("*.txt"):
            stem = stale.name[:-4]
            if stem.isdigit():
                try:
                    stale.unlink()
                except OSError:
                    pass
        for staged in sorted(staging_dir.iterdir()):
            os.replace(staged, output_dir / staged.name)
        shutil.rmtree(staging_dir, ignore_errors=True)

    # 结构化章节清单
    chapters_json = [
        {
            "index": b.index,
            "title": b.title,
            "word_count": b.word_count,
            "volume": b.volume,
            "num": b.num,
        }
        for b in result.chapters
    ]
    (output_dir / "chapters.json").write_text(
        json.dumps(chapters_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "metadata.json").write_text(
        json.dumps(result.metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 人类可读报告
    lines_report = [
        f"书名: {result.metadata.get('title', '')}",
        f"作者: {result.metadata.get('author', '')}",
        f"检测正则: {result.pattern_name}  ({result.detected_pattern})",
        f"总章节数: {result.total_chapters}",
        f"总卷数: {result.total_volumes}",
        f"总字数: {result.total_words}",
        f"去重章节数: {result.dedup_count}",
        f"字数统计: 平均 {result.stats.get('avg')} / 最小 {result.stats.get('min')} / 最大 {result.stats.get('max')} / 中位 {result.stats.get('median')}",
        "章节列表:",
    ]
    for b in result.chapters:
        vol = f"[{b.volume}] " if b.volume else ""
        lines_report.append(f"{b.index:04d}. {vol}{b.title} ({b.word_count} 字)")
    (output_dir / "split_report.txt").write_text(
        "\n".join(lines_report), encoding="utf-8"
    )

    logger.info(f"切分完成: {output_dir} ({result.total_chapters} 章)")
    return {
        "saved": True,
        "output_dir": output_dir.as_posix(),
        "total_chapters": result.total_chapters,
        "total_words": result.total_words,
        "total_volumes": result.total_volumes,
        "dedup_count": result.dedup_count,
    }


def infer_book_name(file_path: Path) -> str:
    """从文件名推断书名。

    P5 + P5b 修复（2026-08-27）：之前用 suffix 白名单（校对版/完整版/全本/...）剥离，
    永远漏（精校版/精修版/典藏版/完结版/最终版/校对后/无括号版 全部漏网）。
    实际小说文件名约定俗成都是 `《书名》（xx版）.txt` 格式——**书名就是
    第一个《到第一个》之间的内容**，版本后缀都在》之后。
    修法：优先截取《...》作为书名；截不到时回退到通用 regex 剥离版本后缀
    （覆盖分隔符变体：连字符/下划线/点/空格 + 可选括号）。

    真实案例（用户 2 本书）:
    - 《从姑获鸟开始》（精校版）.txt  → 之前: 含"精校版"; 现在: 《从姑获鸟开始》
    - 《超级能源强国》（精校版）.txt  → 之前: 含"精校版"; 现在: 《超级能源强国》

    无《》书名 fallback 案例:
    - 斗破苍穹.txt                 → 斗破苍穹
    - 斗破苍穹精校版.txt           → 斗破苍穹
    - 斗破苍穹-精校版.txt          → 斗破苍穹
    - 斗破苍穹_精校版.txt          → 斗破苍穹
    - 凡人修仙传 （校对版）.txt    → 凡人修仙传
    """
    name = file_path.stem
    # P5c（2026-08-27）：任意深度的最内层《...》或<...>内容
    # 之前用 `[《<]([^》>]{1,80})[》>]` 在双层书名号 `《《xxx》》` 上输出 `《《xxx》`
    # （少一个外层 `》`），导致书目录名跟"逻辑书名"对不上，分析/总结阶段按 item.name 找不到。
    # 真实案例: 《《大王饶命》》（精校版）.txt → 之前: 《《大王饶命》（少外层》）;
    #          现在: 《大王饶命》（最内层正确内容）。
    # 用 `《+([^《》]+)》+` 自动找最内层，支持《》/双层《《》》/三层《《《》》》。
    # 用 <+...>+ 同步支持半角书名号（P5 已支持）。
    for open_q, close_q in [("《", "》"), ("<", ">")]:
        m = re.search(rf"{re.escape(open_q)}+([^《》<>]{{1,80}}){re.escape(close_q)}+", name)
        if m:
            return f"{open_q}{m.group(1)}{close_q}"

    # 通用剥离：处理「书名 + 可选分隔符(空格/-/_/.) + 可选括号 + 版本词 + 可选括号」
    # 版本词白名单见下方。分隔符和括号都可选，覆盖：
    #   "斗破苍穹精校版" / "斗破苍穹-精校版" / "斗破苍穹_精校版"
    #   "斗破苍穹 校对版" / "凡人修仙传 （校对版）" / "星辰变(校对版).txt"
    _VERSION_SUFFIX_RE = re.compile(
        r"[\s\-_.]*"          # 可选分隔符
        r"[\(（]?"            # 可选左括号
        r"(?:"                # 版本词（非捕获组）
        r"校对版全本|校对版|完整版|精校版|精修版|"
        r"典藏版|完结版|最终版|高清版|校对后|全本|完"
        r")"
        r"[\)）]?"            # 可选右括号
        r"$"
    )
    name = _VERSION_SUFFIX_RE.sub("", name).strip()

    # 兼容：硬编码分析产物后缀（带分隔符）
    for suffix in ["_分析报告", "_分析", "-分析报告"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name.strip()


def save_to_workspace(
    file_path: Path,
    workspace_dir: Path,
    book_name: str,
    options: Optional[SplitOptions] = None,
) -> Dict[str, Any]:
    """切分并保存到工作区 workspace/{书名}/blocks/"""
    book_dir = workspace_dir / book_name
    blocks_dir = book_dir / "blocks"
    # 修法 A 关键：把权威书名（已在调用方 routes_splitter.py:130 / 192 推断过）透传
    result = save_split(file_path, blocks_dir, options, external_title=book_name)
    result["book_name"] = book_name
    result["workspace_dir"] = book_dir.as_posix()
    return result
