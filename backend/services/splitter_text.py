"""切分器的文本预处理（与切分算法正交）。

三组职责：广告/水印行识别（强弱两级清单）、源文件读取与归一
（TXT 编码检测 / DOCX 正文抽取唯一入口）、Project Gutenberg 头处理
（元数据提取、START 截断、TOC 跳过、标题续行识别）。
"""

import re
import logging
from pathlib import Path
from typing import Dict, Optional

from backend.utils.text_utils import detect_and_decode
from backend.utils.docx_extract import extract_docx_text
from backend.config.constants import ENCODING_CANDIDATES

logger = logging.getLogger(__name__)


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


# 与其他入口（file_processor / routes_prompt 等）共用 ENCODING_CANDIDATES 常量，
# 包含 gb18030（GBK 超集，能解 GBK 边界字符）和 latin-1 兜底。
# gb18030 必须在候选列表内：4.5MB GBK 小说在 256KB 采样边界切到 2-byte
# 字符尾字节时，gbk 解码失败，splitter 路径会抛 "无法使用任何编码读取文件"。
# 与 file_processor.py 行为对齐后，splitter 与 file_processor 选编码结果一致。
def _normalize_content(content: str) -> str:
    """统一的后处理：剥 BOM + 换行归一。TXT / DOCX 两条路径共用，确保形态严格一致。"""
    if content.startswith("\ufeff"):
        content = content[1:]
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _detect_and_read(file_path: Path) -> str:
    """读取源文件并归一化为「一行一个语义单元」的纯文本。

    这是全链路唯一的文本入口（调用方仅 preview_split / save_split 两处）。
    16 条章正则全部带 `^` 行首锚点并配套 `len(s) <= 80` 整行闸门，其成立前提
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


# 从 Gutenberg 头（pre-START）提取 Title/Author
# 头截断后留下的头部可能不干净（Pride 的出版页/Dracula 的版权页等），
# _extract_metadata 的 "前 8 行内短行" 策略会误命中成 "[Illustration:" /
# "MOBY-DICK;" / "A TALE OF TWO CITIES"。这里单独扫 pre-START 头，识别 PG 显式的
# `Title:`/`Author:` 行或第 1 行的 `The Project Gutenberg eBook of X`。
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
# Gutenberg 头截断
# ---------------------------------------------------------------------------
def _strip_gutenberg_header(content: str) -> str:
    """Gutenberg 头截断 + TOC 块跳过。

    找到 '*** START OF PROJECT GUTENBERG' 标记后，截掉到第一个真章节行之间的
    版权页/Table of Contents/序言/题献等内容。

    注意：不能把 TOC 块里的 "CHAPTER 1." 当成"第一个真章节"截断，否则会
    TOC 和正文双重匹配（Moby-Dick 270 章 = 135 TOC + 135 正文）。
    因此在 START 后 100 行内识别 CONTENTS / Table of Contents 标记，
    整块跳过后再找真章节。

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

    # 在 after_start_no_start 前 100 行内找 CONTENTS 标记
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

    # 找第一个有 body 紧随的章节/卷行（区别于 TOC 条目）
    real_offset = _find_first_real_chapter_or_volume(search_text)
    if real_offset is not None:
        return search_text[real_offset:]

    # 兜底：找不到真章节 → 跳过 CONTENTS/START 标记，保留之后内容
    return search_text


def _find_first_real_chapter_or_volume(text: str) -> Optional[int]:
    """在 text 中找第一个有 body 紧随的章节/卷行（区别于 TOC 条目）。

    判定逻辑：
      - 候选章节/卷行之后的 12 行内（跳过空行）：
        - 出现 2 个连续的非空章节/卷标记 → 还在 TOC，跳过
        - 出现非章节/卷 且非标题续行的"段落"内容 → 真章节/卷，OK
      - 章节标题续行（如 Moby-Dick 的 2 行标题、Tale 的"Book the First--Recalled to Life"
        之后的"The Period"）由 _is_title_continuation 识别，跳过但继续扫描。

    候选正则：基础章节正则 + Sherlock/Gatsby 风格补充。

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

    启发式：标题续行通常 ≤ 5 词、仅末尾 1 个句点、无段落标点。
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
