"""
文本处理工具
提供编码检测、文本截断等功能
（自旧项目 utils/text_utils.py 原样移植）
"""

from pathlib import Path
from typing import Dict, List, Optional


# P2 修复（2026-08-24）：编码选择从「首个解码成功即返回」改为「采样罚分择优」。
# 繁体中文书（Big5）的字节流在 gb18030 下能无错解码，但产出乱码/PUA 字符，
# 整本书静默损坏。罚分权重：U+FFFD ×8（解码器替换符，强信号）/
# 控制字符 ×4 / PUA(U+E000-F8FF) ×3（gb18030 吞 Big5 的典型产物）。
_SAMPLE_BYTES = 256 * 1024
_CTRL_ALLOWED = set("\t\n\r")

# 单字节编码（latin-1 / cp1252 / ascii）能无错解码任何字节流，
# 它们的「无错」特性让它们在罚分系统里天然占优，会盖过真正的多字节编码
# （GBK 文本在 GBK 下的真实 penalty ~1e-5，单字节永远是 0）。
# P3 修复（2026-08-27）：给单字节编码加基础偏置 0.001，让 GBK 文本在
# GBK 解码下胜出（GBK penalty 0.00001 < 1e-3 < latin-1 penalty 0.001），
# 又远低于真正乱码（gb18030 解 Big5 文本 penalty ~0.3）。
_SINGLE_BYTE_ENCODINGS = frozenset({"latin-1", "cp1252", "ascii"})
_SINGLE_BYTE_BIAS = 0.001


def _penalty_sample(raw: bytes, encoding: str) -> Optional[float]:
    """采样解码罚分：越低越好。None = 该编码在采样上硬解失败。

    罚分权重：U+FFFD ×8（解码器替换符，强信号）/ 控制字符 ×4 /
    PUA(U+E000-F8FF) ×3（gb18030 吞 Big5 的典型产物）。
    P2 修复（2026-08-24)：取代「首个解码成功即返回」——Big5 字节流在 gb18030
    下无错解码但产出乱码，繁体书整本静默损坏。

    P3 修复（2026-08-27）：decode 用 errors='replace' 而非 strict。
    之前的 strict 模式在 256KB 采样窗口边界处会切坏 2-byte 编码（GBK/GB2312）
    的尾字节，触发 'incomplete multibyte sequence' → 整个合法编码被错误排除。
    真实案例：4.5MB GBK 小说在 256KB 边界正好切到 2-byte 字符的尾字节 0xA1，
    导致 gbk 解码失败，splitter 路径（无 gb18030 也无 latin-1 兜底）抛
    "无法使用任何编码读取文件"。改为 replace 后边界字符变 U+FFFD（罚分 8），
    不会让 GBK 文本在 GBK 解码下被排除，FFFD 罚分量级 ~1e-5/字符 远低于
    真正乱码（PUA 整片出现罚分 ~3e-5/字符）。"""
    sample = raw[:_SAMPLE_BYTES]
    try:
        text = sample.decode(encoding, errors='replace')
    except LookupError:
        return None
    if not text:
        return 0.0
    penalty = text.count("\ufffd") * 8
    penalty += sum(1 for ch in text if ord(ch) < 32 and ch not in _CTRL_ALLOWED) * 4
    penalty += sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF) * 3
    base = penalty / len(text)
    if encoding in _SINGLE_BYTE_ENCODINGS:
        base += _SINGLE_BYTE_BIAS
    return base


def _select_encoding(raw: bytes, encoding_priority: List[str]) -> Optional[str]:
    """BOM/NUL 预检 + 采样罚分择优。返回胜者编码名；全部失败返回 None。"""
    head = raw[:4096]
    if head.startswith(b'\xff\xfe\x00\x00') or head.startswith(b'\x00\x00\xfe\xff'):
        return 'utf-32'
    if head.startswith(b'\xff\xfe'):
        return 'utf-16'
    if head.startswith(b'\xfe\xff'):
        return 'utf-16-be'
    if head.startswith(b'\xef\xbb\xbf'):
        return 'utf-8-sig'
    if head and head.count(b'\x00') / len(head) > 0.05:
        # 无 BOM 但 NUL 密度高：几乎必然是 UTF-16（BE/LE 各试一次整文件解码）
        for enc in ('utf-16', 'utf-16-be'):
            try:
                raw.decode(enc)
                return enc
            except (UnicodeDecodeError, LookupError):
                continue

    best_enc, best_score = None, None
    for encoding in encoding_priority:
        score = _penalty_sample(raw, encoding)
        if score is None:
            continue
        if best_score is None or score < best_score:
            best_enc, best_score = encoding, score
    return best_enc


def detect_and_decode(file_path: Path, encoding_priority: List[str]) -> str:
    """读取文件并以采样罚分择优解码（BOM/NUL 预检与 detect_encoding 共用一套）

    Raises:
        UnicodeDecodeError: 所有编码都失败时抛出
    """
    raw = Path(file_path).read_bytes()
    enc = _select_encoding(raw, encoding_priority)
    if enc is None:
        raise UnicodeDecodeError(
            "unknown", b"", 0, 1, f"无法使用任何编码读取文件: {file_path}")
    return raw.decode(enc)


def detect_encoding(file_path: Path, encoding_priority: List[str]) -> str:
    """检测文件编码（与 detect_and_decode 共用评分选择器）"""
    try:
        raw = Path(file_path).read_bytes()
    except Exception as e:
        raise IOError(f"读取文件失败: {e}")
    enc = _select_encoding(raw, encoding_priority)
    if enc is None:
        raise UnicodeDecodeError(
            "unknown", b"", 0, 1, f"无法使用任何编码读取文件: {file_path}")
    return enc


import difflib as _difflib
import re as _re


def is_text_duplicate(new_text: str, existing_set: set, threshold: float = 0.75) -> bool:
    """
    检查新文本是否与已有集合中的某条语义重复。
    三重判断：归一化相同 / 子串包含 / SequenceMatcher 相似度。

    Args:
        new_text: 待检查的文本
        existing_set: 已有文本集合
        threshold: SequenceMatcher 相似度阈值（默认 0.75）

    Returns:
        True 表示疑似重复
    """
    new_norm = _normalize_for_dedup(new_text)
    if not new_norm:
        return False
    for exist in existing_set:
        exist_norm = _normalize_for_dedup(exist)
        # 1. 归一化后完全相同
        if new_norm == exist_norm:
            return True
        # 2. 一个是另一个的子串（两者长度都 > 3）
        if len(new_norm) > 3 and len(exist_norm) > 3:
            if new_norm in exist_norm or exist_norm in new_norm:
                return True
        # 3. 序列相似度（考虑字符顺序）
        ratio = _difflib.SequenceMatcher(None, new_norm, exist_norm).ratio()
        if ratio >= threshold:
            return True
    return False


def _normalize_for_dedup(text: str) -> str:
    """去除虚词和标点，用于去重比较"""
    stopwords = set('的与和或在中是了被对为从到')
    return ''.join(
        c for c in text
        if c not in stopwords and not _re.match(r'[^\w\u4e00-\u9fff]', c)
    )


def _extract_keywords(text: str, min_len: int = 2, max_len: int = 4) -> set:
    """
    从中文文本中提取 2-4 字关键词（用于伏笔去重的粗筛）

    Args:
        text: 输入文本
        min_len: 关键词最小长度
        max_len: 关键词最大长度

    Returns:
        关键词集合
    """
    stopwords = {'的', '了', '在', '是', '我', '有', '和', '就', '不', '人',
                 '都', '一', '一个', '上', '也', '很', '到', '说', '要', '去',
                 '你', '会', '着', '没有', '看', '好', '自己', '这', '他', '她',
                 '它', '们', '那', '被', '把', '从', '对', '与', '或', '但',
                 '而', '了', '吗', '呢', '吧', '啊', '其', '中', '为', '以',
                 '及', '等', '此', '些', '于', '由', '该', '则', '如', '所'}

    # 提取连续中文字符片段
    segments = _re.findall(r'[\u4e00-\u9fff]+', text)
    keywords = set()
    for seg in segments:
        for length in range(min_len, min(max_len + 1, len(seg) + 1)):
            for i in range(len(seg) - length + 1):
                word = seg[i:i + length]
                if word not in stopwords:
                    keywords.add(word)
    return keywords


def deduplicate_foreshadows(
    clues: list,
    keyword_overlap_min: int = 2,
    similarity_threshold: float = 0.6,
) -> list:
    """
    伏笔线索去重：从所有章节的 foreshadowing clue 中提取唯一伏笔。

    两阶段去重策略：
    1. 关键词交集粗筛：两条 clue 的关键词交集 >= keyword_overlap_min -> 候选重复对
    2. SequenceMatcher 精筛：候选对相似度 >= similarity_threshold -> 确认重复

    Args:
        clues: [(chapter, clue_text) 或 (chapter, clue_text, type, confidence) 或
                (chapter, clue_text, type, confidence, importance), ...]
               支持 2/4/5 元组多种格式，向后兼容。importance 字段用于透传到结果。
        keyword_overlap_min: 关键词交集最小数量（粗筛阈值）
        similarity_threshold: SequenceMatcher 相似度阈值（精筛阈值）

    Returns:
        去重后的伏笔列表，每条：
        {
            "id": "fs_001",           # 顺序编号
            "clue": str,              # 代表条目（首次出现的 clue）
            "first_seen": int,        # 首次出现章节
            "last_seen": int,         # 最后出现章节
            "evidence_chapters": [int], # 所有出现章节
            "merged_count": int,      # 合并了多少条原始线索
            "type": str,              # 伏笔类型（代表条目的 type，可能为空）
            "confidence": str,        # 置信度（代表条目的 confidence，可能为空）
            "importance": str,        # 重要度（合并条目中的最高重要度，5 元组时存在）
        }
    """
    if not clues:
        return []

    # 预处理：每条 clue 提取关键词 + 归一化文本，兼容 2/4/5 元组
    entries = []
    for item in clues:
        if len(item) >= 5:
            chapter, clue_text, ftype, conf, imp = (
                item[0], item[1], item[2], item[3], item[4]
            )
        elif len(item) >= 4:
            chapter, clue_text, ftype, conf = item[0], item[1], item[2], item[3]
            imp = ""
        elif len(item) >= 2:
            chapter, clue_text = item[0], item[1]
            ftype, conf, imp = "", "", ""
        else:
            continue
        if not clue_text or not str(clue_text).strip():
            continue
        entries.append({
            'chapter': chapter,
            'clue': str(clue_text).strip(),
            'keywords': _extract_keywords(str(clue_text)),
            'normalized': _normalize_for_dedup(str(clue_text)),
            'type': str(ftype) if ftype else "",
            'confidence': str(conf) if conf else "",
            'importance': str(imp) if imp else "",
        })

    if not entries:
        return []

    # 分组：每条 entry 归入某个"伏笔组"。
    # PERF-2：关键词倒排索引替代"与全部已有组逐一比较"（原实现 O(N²)，
    # 长书数千条线索时需做百万级关键词交集 + SequenceMatcher 精筛）；
    # 现在只与共享 >=1 个关键词的组比较，语义与旧逻辑等价（粗筛阈值不变）。
    groups = []  # 每组 = [entry_indices...]
    keyword_index: Dict[str, set] = {}  # 关键词 -> 含该关键词的组号集合

    for i, entry in enumerate(entries):
        matched_group = None

        # 候选组：与本条目共享任意关键词的组
        candidates = set()
        for kw in entry['keywords']:
            candidates |= keyword_index.get(kw, set())

        for g_idx in sorted(candidates):
            # 取组内第一条作为代表
            rep = entries[groups[g_idx][0]]

            # 粗筛：关键词交集
            overlap = entry['keywords'] & rep['keywords']
            if len(overlap) < keyword_overlap_min:
                continue

            # 精筛：SequenceMatcher
            ratio = _difflib.SequenceMatcher(
                None, entry['normalized'], rep['normalized']
            ).ratio()
            if ratio >= similarity_threshold:
                matched_group = g_idx
                break

        if matched_group is not None:
            groups[matched_group].append(i)
            g = matched_group
        else:
            groups.append([i])
            g = len(groups) - 1

        # 更新倒排索引
        for kw in entry['keywords']:
            keyword_index.setdefault(kw, set()).add(g)

    # 构建去重结果
    catalog = []
    for g_idx, group in enumerate(groups):
        members = [entries[i] for i in group]
        # 取首次出现的 clue 作为代表
        members.sort(key=lambda e: e['chapter'])
        rep = members[0]
        all_chapters = sorted(set(e['chapter'] for e in members))

        # 取最高置信度（高>中>低）
        conf_order = {"高": 3, "中": 2, "低": 1}
        best_conf = max(members, key=lambda e: conf_order.get(e['confidence'], 0))
        # 取最高重要度（高>中>低；5 元组输入时存在）
        imp_order = {"高": 3, "中": 2, "低": 1}
        best_imp = max(members, key=lambda e: imp_order.get(e['importance'], 0))
        # 取最常见的 type
        type_counts: Dict[str, int] = {}
        for m in members:
            if m['type']:
                type_counts[m['type']] = type_counts.get(m['type'], 0) + 1
        best_type = max(type_counts, key=lambda k: type_counts[k], default=rep['type'])

        catalog.append({
            'id': f"fs_{g_idx + 1:03d}",
            'clue': rep['clue'],
            'first_seen': all_chapters[0],
            'last_seen': all_chapters[-1],
            'evidence_chapters': all_chapters,
            'merged_count': len(members),
            'type': best_type,
            'confidence': best_conf['confidence'],
            'importance': best_imp['importance'],
        })

    return catalog


# 2026-08-21 修复：对称剥离前后缀修饰符（替换 substring + 后缀白名单）
# 之前 substring 规则只识别后缀（"宁安县城" 中 extra="城" 从尾部切），
# 无法识别前缀修饰（"新大唐公司" 中 shorter="大唐公司" 在中间，extra="司" 也从尾部切）。
# 改为：两端都剥掉已知修饰符后比核心。前缀/后缀对称处理。
_PREFIX_MODS = (
    "新", "老", "旧", "原",         # 短前缀
    "原址", "旧址",                    # 复合前缀
)
_SUFFIX_MODS = (
    "主角", "身边", "附近", "一带", "境内", "内部",
    "已废弃",
    "新址", "原址", "旧址",   # 2026-08-21：与前缀对称，否则"宁安县原址/旧址/新址"漏判
)


def _strip_location_mods(name: str) -> str:
    """从 name 两端循环剥离软修饰符（角色后缀/状态/方位等）

    循环剥离直到稳定，因为前缀可能连续出现（如"旧原址X" → "原址X" → "X"）。
    每次 strip 后 len(s) 严格减小，最多 O(|name|) 次，防死循环。
    """
    s = name
    while True:
        stripped = False
        # 前缀（一旦匹配即跳出本轮循环）
        for p in _PREFIX_MODS:
            if s.startswith(p) and len(s) > len(p) + 1:
                s = s[len(p):]
                stripped = True
                break
        if stripped:
            continue
        # 后缀
        for x in _SUFFIX_MODS:
            if s.endswith(x) and len(s) > len(x) + 1:
                s = s[:-len(x)]
                stripped = True
                break
        if not stripped:
            break
    return s


def is_same_location(n1: str, n2: str) -> bool:
    """判断两个地名是否指向同一地点。

    规则：
    1. 归一化后任一为空或长度 < 2 → False（避免单字噪声）
    2. 归一化后完全相等 → True
    3. 两端剥离已知前后缀修饰符后核心相同 → True（"新大唐公司" vs "大唐公司" → True）

    注：归一化阶段（_normalize_for_dedup）已剥离停用词和标点（含括号）。
    此处只处理语义修饰词。
    """
    a = _normalize_for_dedup(n1)
    b = _normalize_for_dedup(n2)
    if not a or not b or len(a) < 2 or len(b) < 2:
        return False
    if a == b:
        return True
    return _strip_location_mods(a) == _strip_location_mods(b)
