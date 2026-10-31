"""
最终总结服务（asyncio 版）
分批调用 LLM 对分析结果进行全书脉络总结，集成伏笔账本管理。
（从旧项目 workers/final_summary_worker.py 移植：QThread→async 类，
 Qt 信号→on_progress/on_token_stats 回调，ThreadPoolExecutor→asyncio.Semaphore+Task，
 threading.Lock→asyncio.Lock；prompt 与账本逻辑逐字保持不变）
"""

import json
import re
import asyncio
import logging
import time
import hashlib
import shutil
from dataclasses import replace
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Callable

from backend.config.settings import AppConfig
from backend.config.constants import (
    FORESHADOW_CATEGORY_DEFS,
    FORESHADOW_CATEGORY_FALLBACK,
    FORESHADOW_CATEGORY_SCHEMA_VERSION,
    FORESHADOW_TYPE_MAP_FILE,
    RECHECK_FULLTEXT_BUDGET_CHARS,
)
from backend.core.llm_client import LLMClient
from backend.core.style_analyzer import extract_style_profile
from backend.utils.json_utils import safe_load_json, safe_save_json, extract_json_from_text, safe_parse_json
from backend.core.anchor import entity_key_for
from backend.utils.text_utils import deduplicate_foreshadows
from backend.utils.foreshadow_ledger import ForeshadowLedger, ForeshadowItem

logger = logging.getLogger(__name__)


# ==================== Prompt 定义 ====================

# BATCH_PROMPT 的 system 部分（固定指令，跨批次可缓存）
BATCH_SYSTEM_PROMPT = """你是专业的网络小说结构分析师。请对该段内容进行综合分析，严格按以下7个部分输出（纯 Markdown，不要输出任何 JSON）：

## 1. 主线推进
- 这段推进了哪些具体剧情线（用具体事件说明）
- 每条剧情线在这段中的起点和终点

## 2. 角色发展
- 主要角色在这段中的变化（实力、性格、关系）
- 角色之间的关系变化（盟友/敌对/暧昧等）

## 3. 伏笔管理
- 新设伏笔：列出具体伏笔内容（必须是跨章节的悬念/谜团）
- 已回收伏笔：列出回收方式（自然揭示/角色发现/剧情触发）
- 仍活跃伏笔：列出当前状态

## 4. 势力格局
- 各势力的实力变化（增强/削弱/消亡）
- 势力之间的冲突与合作

## 5. 世界观扩展
- 新增的世界观元素（地点、规则、势力、技术等）
- 已有元素的深化或修正

## 6. 节奏与转折
- 这段内容的节奏曲线（标注每几章的节奏变化）
- 关键转折点及其意义

## 7. 逻辑检查
- 是否存在逻辑漏洞或前后矛盾
- 角色行为是否符合其性格设定

【伏笔的定义】
伏笔是指：
1. 作者为后续情节埋下的悬念或线索
2. 故事中尚未解答的疑问
3. 需要跨越多个章节才能看到结果的事件设定

不属于伏笔的内容：
- 单章内发生并立即解决的小冲突
- 角色的瞬时情绪
- 直接陈述的事实

如果某个事件或悬念在本章节中已经完整解决，它不应该作为新伏笔输出。

数据字段说明：
- events: 本章核心事件列表（用于分析"主线推进"和"势力格局"）
- arcs: 角色变化列表，格式为"角色名→变化描述"（用于分析"角色发展"）
- pacing: 本章节奏判断（用于分析"节奏与转折"）
- summary: 承上启下摘要（剧情流串联）
- foreshadow: 伏笔线索（已精简，伏笔管理请参考上方伏笔清单）
- holes: 已知逻辑漏洞
- timeline: 时间线"""

# BATCH_PROMPT 的 user 部分（每批次变化，无法缓存）
BATCH_USER_TEMPLATE = """以下是《{book_name}》第{start}章到第{end}章的结构化分析数据。

【截至本批已识别的伏笔清单】
以下是从前文分析中提取的伏笔，请在分析时关注这些伏笔在本批次中的状态变化（是否被回收、是否有新进展）：
{foreshadow_catalog}

【字数要求】撰写的分卷总结字数要求在{batch_min_words}字以上，请详细展开分析，不要省略。

数据：
{batch_data}"""


# 第二次调用专用：只做伏笔 reconciliation 判断（纯 JSON 输出）
# system 部分（固定指令，跨批次可缓存）
RECONCILIATION_SYSTEM_PROMPT = """你是伏笔状态审计员。你的任务：逐一判断每个【活跃伏笔】在本批次中是否被回收。

【回收判断标准】
- 回收 = 伏笔的核心悬念在本批次中得到了明确答案，或驱动它的事件已经完结
- 仅仅是"被提及"或"继续推进"不算回收，必须要有结论性的解决
- 一个伏笔绝不可能在"埋设的同一批次"就被回收

【输出要求 - 极其重要】
只输出一个 JSON 对象，不要输出任何 Markdown、解释或代码块标记。
所有活跃伏笔都必须出现在 foreshadow_reconciliation 数组中，不允许遗漏。
不要输出 new_foreshadows 字段，伏笔总表已由程序管理，你只需判断回收。

**省 token 规则**：未回收的伏笔省略 reason 字段，只保留 foreshadow_id 和 is_resolved_in_this_batch。
只有被回收的伏笔才需要 resolved_chapter、resolution_summary 和 confidence。

**confidence 规则**：判定回收时自评置信度——"高"=卷摘要中有明确直接的回收证据；"中"=推断性回收（证据间接但合理）；"低"=存疑回收（证据牵强或可能误判）。中/低置信度会被标记为需要人工复核。

JSON schema：
{{
  "foreshadow_reconciliation": [
    {{"foreshadow_id": "fs_xxx", "is_resolved_in_this_batch": true, "resolved_chapter": 18, "resolution_summary": "如何被回收", "confidence": "高"}},
    {{"foreshadow_id": "fs_yyy", "is_resolved_in_this_batch": false}}
  ]
}}

【示例】
活跃伏笔 fs_狸猫换太子真相，描述"刘妃、郭槐密谋以狸猫换走太子，陷害李妃"。
若本批次卷摘要中第18章"狄后讲述真相，仁宗认母，刘后惊死"，则：
{{"foreshadow_id": "fs_狸猫换太子真相", "is_resolved_in_this_batch": true, "resolved_chapter": 18, "resolution_summary": "狄后讲述真相，仁宗认母，刘后惊死，阴谋完全揭露", "confidence": "高"}}"""

# user 部分（每批次变化）
RECONCILIATION_USER_TEMPLATE = """【本批次卷摘要】（第{start}章-第{end}章）
{batch_summary}

【当前伏笔清单】（从原始分析数据提取，而非账本）
请对照以下伏笔的原始描述，判断每个伏笔在本批次中是否被回收。
{active_foreshadows_block}"""


# 最终报告生成专用 prompt
# system 部分（固定指令）
FINAL_SYSTEM_PROMPT = """你是专业的网络小说结构分析师。请产出完整的全书脉络报告，严格按以下8个部分输出：

## 1. 故事主线
用尽量详细的话语描述核心主线的变迁，包括：
- 故事的起点（开局设定）
- 主线的推进阶段（每个阶段的关键事件）
- 故事的终点（结局走向）
- 主线是否有断裂或偏移

## 2. 角色弧光
- 主角的完整成长轨迹（从起点到终点，中间的关键节点）
- 重要配角的关键转折点
- 角色之间的关系演变（从初始关系到最终关系）
- 是否有角色被浪费（出场多但作用小）

## 3. 伏笔网络（必须包含此节）
- 按时间线列出伏笔的"埋设→激活→回收"链
- 标注哪些伏笔回收得漂亮（意料之外，情理之中）
- 标注哪些伏笔虎头蛇尾（设了没回收，或回收太草率）
- 列出仍未回收的伏笔
- 所有伏笔结论必须带证据章节号

## 4. 世界观演变
- 从开头到结尾的变化
- 有没有换地图（场景迁移）
- 主要世界战力的变化（升级/降级/新势力出现）
- 整体技术水平的变化
- 整个世界的背景有没有发生迁移（如从校园到战场，从地球到星际）

## 5. 主题思想
- 贯穿全书的核心主题
- 主题如何通过情节和角色体现
- 主题是否有演变或深化
- 是否有主题偏离的情况

## 6. 节奏曲线
- 按章节区间标注节奏（紧张/舒缓/高潮/过渡）
- 标注全书的高潮点和低谷点
- 评价节奏是否合理（是否有拖沓或赶进度的问题）

## 7. 质量评价
- 写得好的段落及原因（对话精彩、情节紧凑、角色立体等）
- 有提升空间的段落及问题（节奏拖沓、角色行为不合理、伏笔遗忘等）
- 整体质量评分（1-10分）

## 8. 写作风格分析
- 基于输入的【写作风格特征】数据，分析本书的写作特点
- 涵盖：句式节奏、对话风格、修辞偏好、情感表达方式、叙事视角、信息管控策略
- 引用原文片段或统计数据支撑每个结论
- 如果输入中包含"写作风格特征"段落，必须基于该数据展开分析，不可忽略"""

# user 部分（每次调用变化）
FINAL_USER_TEMPLATE = """以下是《{book_name}》全书各段的分析摘要。

{style_profile}

{foreshadow_context}

{foreshadow_audit}

【字数要求】撰写的报告字数要求在{final_min_words}字以上，请详细展开分析，不要省略。

各段分析：
{volume_summaries}"""


# 全书伏笔复检专用 prompt
# system 部分（固定指令，跨批次可缓存）
GLOBAL_RECHECK_SYSTEM_PROMPT = """你是伏笔审计专家。你的任务：逐一检查每个伏笔，判断它在全书范围内是否已经被回收。

【回收判断标准】
- 回收 = 伏笔的核心悬念在全书中得到了明确答案，或驱动它的事件已经完结
- 关键线索：如果伏笔描述的问题在后续章节中有了明确交代，就是回收了
- 不要因为伏笔"影响深远"就认为它没回收——很多伏笔在解决后仍然影响后续剧情

【输出要求】
只输出一个 JSON 对象，不要输出任何 Markdown、解释或代码块标记。
所有活跃伏笔都必须出现在结果中，不允许遗漏。

**省 token 规则**：未回收的伏笔省略 reason 字段，只保留 foreshadow_id 和 is_resolved_in_this_batch。
只有被回收的伏笔才需要 resolved_chapter、resolution_summary 和 confidence。

**confidence 规则**：判定回收时自评置信度——"高"=摘要中有明确直接的回收证据；"中"=推断性回收（证据间接但合理）；"低"=存疑回收（证据牵强或可能误判）。中/低置信度会被标记为需要人工复核。

JSON schema：
{{
  "foreshadow_reconciliation": [
    {{"foreshadow_id": "fs_xxx", "is_resolved_in_this_batch": true, "resolved_chapter": 实际回收的章节号, "resolution_summary": "如何被回收（引用具体章节）", "confidence": "高"}},
    {{"foreshadow_id": "fs_yyy", "is_resolved_in_this_batch": false}}
  ]
}}"""

# user 部分（每批次变化）
GLOBAL_RECHECK_USER_TEMPLATE = """以下是《{book_name}》的全书各卷摘要，以及一份仍标记为活跃的伏笔清单。

这些伏笔在逐批 reconciliation 中没有被标记为回收，但它们可能实际上在全书中已经被回收了——只是逐批判断时因为批次范围太小而遗漏。

【全书各卷摘要】
{volume_summaries}

【待检查的活跃伏笔清单】
{active_foreshadows_block}"""


# ==================== 辅助函数 ====================

# 常见的非书名文本（metadata 推断错误时会抓到这些）
# 2026-09-04：从 detect_book_name 闭包提为模块级，供 fix_metadata_titles.py 等外部脚本 import
_NON_TITLE_PREFIXES = (
    "内容简介", "作品简介", "小说简介", "简介", "作者", "目录", "楔子",
    "序章", "前言", "序言", "卷首语", "第一章", "第1章", "第 1 章",
    "正文", "章节",
)


def _clean_book_name(name: str) -> str:
    """剥离书名号 / 尾部括号备注（2026-09-04 从 detect_book_name 闭包提为模块级）"""
    name = name.strip()
    # 先去掉尾部括号备注，如 "（精校版全本）"
    name = re.sub(r"\s*[（(].*?[）)]\s*$", "", name)
    # 再去掉首尾的《》【】
    name = re.sub(r"^[《【]", "", name)
    name = re.sub(r"[》】]$", "", name)
    return name.strip()


def _looks_valid_book_name(title: str) -> bool:
    """判断 title 是否像合法书名（2026-09-04 加 2 段兜底过滤）"""
    if not title or len(title) < 2 or len(title) > 60:
        return False
    # 如果 title 主要由等号/横线/星号/下划线/空格/竖线组成，视为无效
    if re.fullmatch(r"[\s\=\-\*\_\.\|┅┄━─]+", title):
        return False
    # 以冒号结尾，或看起来像章节/简介/作者/目录开头，视为无效
    if title.rstrip().endswith(("：", ":")):
        return False
    t = title.strip()
    if any(t.startswith(p) or t == p for p in _NON_TITLE_PREFIXES):
        return False
    # 新增（修法兜底 #1，2026-09-04）：含中文句末标点视为正文句子
    # 真实案例：《轮回乐园》splitter 把 "夜晚时分，一座繁华中透露着浮躁的二线城市。" 当书名
    if any(p in t for p in ("。", "？", "！")):
        return False
    # 新增（修法兜底 #2，2026-09-04）：含 "作者" / "著" 视为 splitter 误把作者信息拼入
    # 真实案例：《进化的46亿重奏》title = "进化的四十六亿重奏 作者：相位行者"（含"作者"但不含句末标点）
    # _NON_TITLE_PREFIXES 只检查 t.startswith("作者")，这条是 t="进化的..."，不命中
    if "作者" in t or "著" in t:
        return False
    return True


def detect_book_name(output_dir: Path) -> Optional[str]:
    """
    检测书名（不带《》包裹，供报告模板使用）。
    1. 优先读取 blocks/metadata.json 中的 title 字段（切分时推断出的书名最准确）。
    2. 其次使用 blocks 所在目录名（书目目录名），去掉《》和括号备注。
    3. 不再扫描 blocks 内 .txt 文件名，避免 split_report.txt 等报告文件被误判为书名。

    2026-09-04：把 _clean / _looks_valid 闭包提到模块级（_clean_book_name /
    _looks_valid_book_name），并在 _looks_valid_book_name 加 2 段兜底过滤
    （含中文句末标点 / "作者"/"著" 视为正文句子，自动 fallback 到目录名）。
    """
    blocks_dir = output_dir.parent / "blocks"
    if not blocks_dir.exists():
        return None

    meta_path = blocks_dir / "metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            title = _clean_book_name(meta.get("title", ""))
            if _looks_valid_book_name(title):
                return title
        except Exception:
            pass

    # 回退：用 blocks 父目录名（书目目录名），同样清理书名号与括号
    fallback = _clean_book_name(output_dir.parent.name)
    return fallback if _looks_valid_book_name(fallback) else None


def extract_key_fields(data: dict) -> Optional[dict]:
    """从 chapter_N_result.json 提取关键字段（用于分卷总结）"""
    # 健壮性：畸形 result 文件（字段类型不符）不应拖垮整个总结任务，
    # 统一兜底为安全的 dict/list，解析失败时返回 None（该章被跳过）。
    if not isinstance(data, dict):
        return None
    cb = data.get('cross_block') or {}
    lci = data.get('long_context_insights') or {}
    uk = data.get('updated_knowledge') or {}
    if not isinstance(cb, dict):
        cb = {}
    if not isinstance(lci, dict):
        lci = {}
    if not isinstance(uk, dict):
        uk = {}

    summary = cb.get('summary', '')
    if not summary:
        return None

    # core_events：只取 event 描述，丢掉 characters/function（省 token）
    core_events = data.get('core_events') or []
    if not isinstance(core_events, list):
        core_events = []
    events = [str(e.get('event', '')) for e in core_events
              if isinstance(e, dict) and e.get('event')]

    # character_arcs：只取 name + change_delta，丢掉 surface_action/inner_motivation/driver
    character_arcs = data.get('character_arcs') or []
    if not isinstance(character_arcs, list):
        character_arcs = []
    arcs = [f"{a.get('name', '')}→{a.get('change_delta', '')}"
            for a in character_arcs
            if isinstance(a, dict) and a.get('name')]

    # foreshadowing：只取第 1 条 clue（伏笔 reconciliation 已在第二次调用单独处理，
    # 第一次调用只需给 LLM 一个伏笔印象，不需要全量列表）
    foreshadows = data.get('foreshadowing') or []
    if not isinstance(foreshadows, list):
        foreshadows = []
    first_clue = []
    if foreshadows and isinstance(foreshadows[0], dict):
        clue = foreshadows[0].get('clue', '')
        if clue:
            first_clue = [clue]

    plot_holes = data.get('plot_holes') or []
    if not isinstance(plot_holes, list):
        plot_holes = []

    return {
        'ch': data.get('chapter_number', 0),
        'summary': summary,
        'link': cb.get('contextual_link', ''),
        'foreshadow': first_clue,
        'holes': plot_holes,
        'timeline': uk.get('timeline', ''),
        'events': events,
        'arcs': arcs,
        'pacing': lci.get('pacing', ''),
    }


def extract_foreshadow_json(response: str) -> Optional[dict]:
    """
    从LLM响应中提取伏笔 reconciliation JSON。
    """
    if not response:
        return None

    data: Optional[dict] = None

    # 策略1: 直接解析（第二次调用期望纯 JSON）
    data = safe_parse_json(response.strip())

    # 策略2: 从 markdown 代码块 / 残缺文本中兜底提取
    if not data:
        json_str = extract_json_from_text(response)
        if json_str:
            data = safe_parse_json(json_str)

    if not data:
        logger.warning(f"伏笔 reconciliation JSON 解析失败，响应前200字: {response[:200]}")
        return None

    if not isinstance(data, dict):
        logger.warning(f"伏笔 reconciliation 返回了非字典类型: {type(data).__name__}")
        return None

    # 新格式：foreshadow_reconciliation
    if 'foreshadow_reconciliation' in data:
        recon = data.get('foreshadow_reconciliation', []) or []
        logger.info(f"伏笔 reconciliation 解析成功: {len(recon)} 条")
        return {
            'reconciliation': recon,
            'new_foreshadows': data.get('new_foreshadows', []) or []
        }

    # 兼容旧格式：foreshadow_evidence
    if 'foreshadow_evidence' in data:
        evidence = data['foreshadow_evidence'] or {}
        reconciliation = []
        for item in evidence.get('resolved_foreshadows', []) or []:
            reconciliation.append({
                'foreshadow_id': item.get('id', ''),
                'is_resolved_in_this_batch': True,
                'resolved_chapter': item.get('resolved_chapter', 0),
                'resolution_summary': item.get('resolution', '')
            })
        return {
            'reconciliation': reconciliation,
            'new_foreshadows': evidence.get('new_foreshadows', []) or []
        }

    return None


def validate_reconciliation_json(content: str) -> Tuple[bool, str]:
    """校验第二次调用返回的是合法 reconciliation JSON"""
    result = extract_foreshadow_json(content)
    if result is None:
        return False, "无法解析为 foreshadow_reconciliation JSON"
    recon = result.get('reconciliation')
    if not isinstance(recon, list):
        return False, "foreshadow_reconciliation 必须是数组"
    # 验证：LLM 被要求不输出 new_foreshadows，记录实际情况
    new_fs = result.get('new_foreshadows')
    if new_fs:
        logger.warning(f"LLM 意外输出了 new_foreshadows（{len(new_fs)}条），按 prompt 指令应省略")
    return True, ""


def build_reconciliation_retry_messages(validation_error: str, messages: List[dict]) -> List[dict]:
    """解析失败时追加格式修正提示后重试"""
    hint = (
        f"\n\n格式错误: {validation_error}\n"
        "请重新输出，且只输出一个 JSON 对象（不要 Markdown、不要代码块标记）。"
        "结构必须包含数组字段 foreshadow_reconciliation。"
    )
    new_messages = list(messages)
    if new_messages and new_messages[-1].get('role') == 'user':
        new_messages[-1] = {**new_messages[-1], 'content': new_messages[-1]['content'] + hint}
    else:
        new_messages.append({"role": "user", "content": hint.strip()})
    return new_messages


def extract_analysis_text(response: str) -> str:
    """提取第一次调用的 Markdown 卷摘要（剥离末尾可能的 JSON）"""
    if not response:
        return ""

    for marker in ('```json', '```JSON', '``` Json'):
        pos = response.find(marker)
        if pos != -1:
            return response[:pos].rstrip()

    for key in ('"foreshadow_reconciliation"', '"foreshadow_evidence"'):
        pos = response.find(key)
        if pos > 0:
            brace_pos = response.rfind('{', 0, pos)
            if brace_pos > 0:
                return response[:brace_pos].rstrip()

    return response.strip()


# ==================== Runner 类 ====================

class FinalSummaryRunner:
    """最终总结执行器（asyncio）

    回调（均为普通同步函数，在事件循环内调用）：
    - on_progress(dict)：进度消息，type 对齐旧 Qt 信号
      status/phase/batch_done/batch_failed/ledger_updated/complete
    - on_token_stats(dict)：{category, input_tokens, output_tokens}
    """

    def __init__(
        self,
        config: AppConfig,
        output_dir: Path,
        start_chapter: int,
        end_chapter: int,
        batch_size: int,
        concurrency: int = 1,
        ledger_path: Optional[Path] = None,
        on_progress: Optional[Callable[[dict], None]] = None,
        on_token_stats: Optional[Callable[[dict], None]] = None,
    ):
        self.config = config
        self.output_dir = Path(output_dir)
        self.start_chapter = start_chapter
        self.end_chapter = end_chapter
        self.batch_size = max(1, batch_size if batch_size is not None else 1)
        self.concurrency = max(1, concurrency if concurrency is not None else self.config.analysis.concurrency)
        self._lock = asyncio.Lock()
        self._checkpoint_lock = asyncio.Lock()  # 断点 checkpoint 落盘/读写的互斥保护
        self._stop_requested = False
        self._on_progress = on_progress
        self._on_token_stats = on_token_stats

        # 全局 LLM 并发上限 Sem：阶段 1/2/3/4 全部共用，硬上限 = self.concurrency
        # 这样无论是 4 / 9 / 20 都不会超 API 限流；阶段 3 的独立 LLMClient 也通过包装层共享此 Sem
        self._llm_sem = asyncio.Semaphore(self.concurrency)
        # in-flight 监控：跑完总结打印峰值，便于验证未超配置上限
        self._inflight_count = 0
        self._peak_inflight = 0

        # 分类 token 累计
        self._tokens_summary = (0, 0)        # 卷摘要 (input, output)
        self._tokens_final = (0, 0)          # 最终报告
        self._tokens_reconciliation = (0, 0) # 伏笔 reconciliation（批次内）
        self._tokens_recheck = (0, 0)        # 全书伏笔复检
        self._tokens_style = (0, 0)          # 风格提取
        self._style_task: Optional[asyncio.Task] = None  # 阶段1并行启动的风格提取任务（stop 时需 cancel）

        # 总结专用模型/思考覆盖：summary_model 空则跟随全局 model；
        # summary_thinking_mode 空则跟随全局 thinking_mode（兼容 mimo/GLM/DeepSeek 等各厂商参数）
        _api_cfg = replace(
            self.config.api,
            model=self.config.api.summary_model or self.config.api.model,
            json_mode="default",
            timeout=self.config.api.summary_timeout,
            thinking_mode=self.config.api.summary_thinking_mode or self.config.api.thinking_mode,
        )
        self._llm = LLMClient(_api_cfg)

        self.ledger_path = ledger_path or (self.output_dir / "foreshadow_ledger.json")
        self.ledger = ForeshadowLedger.load(self.ledger_path)
        self.ledger.batch_size_snapshot = batch_size
        self.book_name = detect_book_name(self.output_dir) or "未知小说"
        logger.info(f"检测到书名: {self.book_name}")

    def stop(self):
        self._stop_requested = True
        self._llm.request_stop()
        # 风格链路在 style_analyzer.call_llm_semantic 里自建独立 LLMClient，
        # request_stop() 传不过去；只能 cancel 承载它的任务（P1 2026-08-24）
        t = self._style_task
        if t is not None and not t.done():
            t.cancel()

    def _emit_progress(self, payload: dict) -> None:
        if self._on_progress:
            try:
                self._on_progress(payload)
            except Exception as e:
                logger.error(f"progress 回调异常: {e}")

    def _load_results(self) -> List[dict]:
        """加载并提取所有章节结果的关键字段（同步 IO，经 to_thread 调用）"""
        results = []
        for f in self.output_dir.glob("chapter_*_result.json"):
            m = re.match(r'chapter_(\d+)_result\.json', f.name)
            if not m:
                continue
            ch = int(m.group(1))
            if ch < self.start_chapter or ch > self.end_chapter:
                continue
            data = safe_load_json(f)
            if data is None:
                continue
            extracted = extract_key_fields(data)
            if extracted:
                # 额外保留原始 foreshadowing 数据（用于构建伏笔总表）
                extracted["foreshadowing_raw"] = data.get("foreshadowing", [])
                results.append(extracted)
        results.sort(key=lambda x: x['ch'])
        return results

    def _format_batch(self, batch: List[dict]) -> str:
        return json.dumps(batch, ensure_ascii=False, indent=1)

    async def _acquire_llm_slot(self) -> None:
        """获取全局 LLM 并发槽（硬上限 = self.concurrency），同时记录 in-flight 峰值"""
        await self._llm_sem.acquire()
        self._inflight_count += 1
        if self._inflight_count > self._peak_inflight:
            self._peak_inflight = self._inflight_count

    def _release_llm_slot(self) -> None:
        """释放 LLM 并发槽"""
        self._inflight_count -= 1
        self._llm_sem.release()

    async def _call_llm_summary(self, prompt: str, batch_idx: int = 0) -> Optional[str]:
        """第一次调用：产出 Markdown 卷摘要（H16 Phase 3 改 chat_auto + on_progress broadcast）"""
        messages = [
            {"role": "system", "content": BATCH_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        await self._acquire_llm_slot()
        try:
            on_progress = self._make_on_progress("summary_phase_1", batch_idx)
            llm_result = await self._llm.chat_auto(
                messages, max_tokens=self.config.api.max_tokens, validate_response=None,
                on_progress=on_progress,
            )
        finally:
            self._release_llm_slot()
        self._record_tokens("summary", llm_result.tokens)
        if llm_result.success:
            return llm_result.content
        logger.error(f"卷摘要 LLM 调用失败: {llm_result.error}")
        return None

    async def _call_llm_final(self, prompt: str, batch_idx: int = 0) -> Optional[str]:
        """最终报告调用（H16 Phase 3 改 chat_auto + on_progress broadcast）"""
        messages = [
            {"role": "system", "content": FINAL_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        await self._acquire_llm_slot()
        try:
            on_progress = self._make_on_progress("summary_phase_4", batch_idx)
            llm_result = await self._llm.chat_auto(
                messages, max_tokens=self.config.api.max_tokens, validate_response=None,
                on_progress=on_progress,
            )
        finally:
            self._release_llm_slot()
        self._record_tokens("final", llm_result.tokens)
        if llm_result.success:
            return llm_result.content
        logger.error(f"最终报告 LLM 调用失败: {llm_result.error}")
        return None

    async def _call_llm_reconciliation(self, prompt: str, batch_idx: int = 0) -> Optional[dict]:
        """第二次调用（H16 Phase 3 改 chat_auto + on_progress broadcast）"""
        messages = [
            {"role": "system", "content": RECONCILIATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        await self._acquire_llm_slot()
        try:
            on_progress = self._make_on_progress("summary_phase_1", batch_idx)
            llm_result = await self._llm.chat_auto(
                messages, max_tokens=self.config.api.max_tokens,
                validate_response=validate_reconciliation_json,
                retry_messages_builder=build_reconciliation_retry_messages,
                on_progress=on_progress,
            )
        finally:
            self._release_llm_slot()
        self._record_tokens("reconciliation", llm_result.tokens)
        if not llm_result.success:
            logger.warning(f"伏笔 reconciliation 调用失败: {llm_result.error}")
            return None
        return extract_foreshadow_json(llm_result.content)

    async def _call_llm_global_recheck(self, prompt: str, batch_idx: int = 0) -> Optional[dict]:
        """全书复检调用（H16 Phase 3 改 chat_auto + on_progress broadcast）"""
        messages = [
            {"role": "system", "content": GLOBAL_RECHECK_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        await self._acquire_llm_slot()
        try:
            on_progress = self._make_on_progress("summary_phase_2", batch_idx)
            llm_result = await self._llm.chat_auto(
                messages, max_tokens=self.config.api.max_tokens,
                validate_response=validate_reconciliation_json,
                retry_messages_builder=build_reconciliation_retry_messages,
                on_progress=on_progress,
            )
        finally:
            self._release_llm_slot()
        self._record_tokens("recheck", llm_result.tokens)
        if not llm_result.success:
            logger.warning(f"全书复检调用失败: {llm_result.error}")
            return None
        return extract_foreshadow_json(llm_result.content)

    def _make_on_progress(self, context: str, unit_idx: int):
        """薄适配器：共用流式进度工厂，计时仍在拿到并发槽之后创建

        保留本方法是为了不改动四个调用点的调用形态（计时创建位置 = 并发槽之后）。
        """
        from ..progress_hub import get_hub
        from ..core.stream_progress import make_stream_progress_callback

        return make_stream_progress_callback(
            hub=get_hub(),
            context=context,
            session_id=getattr(self, "book_id", "") or "unknown",
            unit_idx=unit_idx,
        )

    def _record_tokens(self, category: str, tokens) -> None:
        """累积并发出分类 token 统计（事件循环内调用，无需缓冲）"""
        if not tokens or not isinstance(tokens, tuple) or len(tokens) < 2:
            return
        in_tok = int(tokens[0] or 0)
        out_tok = int(tokens[1] or 0)
        if category == "summary":
            self._tokens_summary = (self._tokens_summary[0] + in_tok,
                                    self._tokens_summary[1] + out_tok)
        elif category == "final":
            self._tokens_final = (self._tokens_final[0] + in_tok,
                                  self._tokens_final[1] + out_tok)
        elif category == "reconciliation":
            self._tokens_reconciliation = (self._tokens_reconciliation[0] + in_tok,
                                           self._tokens_reconciliation[1] + out_tok)
        elif category == "recheck":
            self._tokens_recheck = (self._tokens_recheck[0] + in_tok,
                                    self._tokens_recheck[1] + out_tok)
        elif category == "style":
            self._tokens_style = (self._tokens_style[0] + in_tok,
                                  self._tokens_style[1] + out_tok)
        if self._on_token_stats:
            try:
                self._on_token_stats({
                    "category": category,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                })
            except Exception as e:
                logger.error(f"token_stats 回调异常: {e}")

    def get_token_stats(self) -> Dict[str, Dict[str, int]]:
        """分类 token 统计快照"""
        return {
            category: {"input_tokens": pair[0], "output_tokens": pair[1]}
            for category, pair in [
                ("summary", self._tokens_summary),
                ("final", self._tokens_final),
                ("reconciliation", self._tokens_reconciliation),
                ("recheck", self._tokens_recheck),
                ("style", self._tokens_style),
            ]
        }

    # type→category 归一化映射文件路径（per-book 持久化）
    def _foreshadow_type_map_path(self) -> Path:
        return self.output_dir / FORESHADOW_TYPE_MAP_FILE

    def _load_foreshadow_type_map(self) -> Optional[Dict[str, str]]:
        """加载 per-book type→category 映射（旧数据被动兜底，无 LLM 调用）。
        返回 None 表示文件不存在或 schema 不匹配。"""
        path = self._foreshadow_type_map_path()
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # schema 版本检查：常量改了定义时自动失效
            if data.get("schema_version") != FORESHADOW_CATEGORY_SCHEMA_VERSION:
                logger.info(f"伏笔 type 映射 schema 版本不匹配 "
                            f"({data.get('schema_version')} -> {FORESHADOW_CATEGORY_SCHEMA_VERSION})，映射失效按原始 type 兜底")
                return None
            valid_names = {name for name, _, _ in FORESHADOW_CATEGORY_DEFS}
            mapping = data.get("mapping", {})
            # 丢弃任何不在 50 类中的键（防御：旧版本残留或被手动改过）
            return {k: v for k, v in mapping.items() if v in valid_names}
        except Exception as e:
            logger.warning(f"加载伏笔 type 映射失败: {e}，按原始 type 兜底")
            return None

    async def _build_foreshadow_catalog(self, results: List[dict]) -> list:
        """
        从逐章分析结果的 foreshadowing 字段构建伏笔总表。

        流程：
        1. 收集所有 unique type + 5 元组线索 (ch, clue, type, conf, importance)
        2. 解析 category：type 已是 50 类之一则直接用；否则查 per-book 映射文件兜底
           （旧书在 prompt 约束落地前产出的自由式 type，靠映射文件归一；新书源头已约束）
        3. 过滤：importance >= min_importance AND confidence >= min_confidence AND category ∈ kept_categories
        4. 去重（text_utils.deduplicate_foreshadows，6 元组）
        5. 综合排序：importance * 100 + confidence * 10 + evidence_count
        6. 分层截断：importance=高 取到 max_high 上限，中 取到 max_mid 上限
        """
        a = self.config.analysis
        all_clues = []
        unique_types = set()
        for r in results:
            ch = r.get("ch", 0)
            foreshadows = r.get("foreshadowing_raw", [])
            if not foreshadows:
                continue
            for fs in foreshadows:
                if not isinstance(fs, dict):
                    continue
                clue = fs.get("clue", "")
                if not clue or len(clue.strip()) < 15:
                    continue
                ftype = fs.get("type", "") or ""
                conf = fs.get("confidence", "中") or "中"
                imp = fs.get("importance", "中") or "中"
                # 第 6..8 位携带锚点：没有锚点，复检阶段就只能听模型自述
                all_clues.append((
                    ch, clue, ftype, conf, imp,
                    fs.get("anchor_quote", "") or "",
                    int(fs.get("anchor_chapter") or 0),
                    fs.get("motif_key", "") or "",
                    fs.get("question", "") or "",
                    fs.get("answer_criteria", "") or "",
                ))
                if ftype:
                    unique_types.add(ftype)

        logger.info(f"伏笔总表原始数据：{len(all_clues)} 条线索，{len(unique_types)} 种 type")

        # 被动兜底：type 不在 50 类内的旧数据查 per-book 映射（无 LLM 调用）
        valid_names = {name for name, _, _ in FORESHADOW_CATEGORY_DEFS}
        type_to_category = self._load_foreshadow_type_map() or {}

        # 应用 category：给每条线索补 category
        categorized = []
        skipped_importance = 0
        skipped_confidence = 0
        skipped_category = 0
        for ch, clue, ftype, conf, imp in all_clues:
            # importance 阈值
            if not self._imp_at_least(imp, a.foreshadow_min_importance):
                skipped_importance += 1
                continue
            # confidence 阈值
            if not self._imp_at_least(conf, a.foreshadow_min_confidence):
                skipped_confidence += 1
                continue
            # category 解析：type 合法直接用；否则查映射；仍无则 fallback
            if ftype in valid_names:
                category = ftype
            else:
                category = type_to_category.get(ftype, FORESHADOW_CATEGORY_FALLBACK)
            if category not in set(a.foreshadow_kept_categories):
                skipped_category += 1
                continue
            categorized.append((ch, clue, ftype, conf, imp, category))

        logger.info(
            f"伏笔过滤：原 {len(all_clues)} 条 → "
            f"过滤后 {len(categorized)} 条（importance 丢 {skipped_importance}, "
            f"confidence 丢 {skipped_confidence}, category 丢 {skipped_category}）"
        )

        # 去重（5 元组 + importance 透传）
        catalog = deduplicate_foreshadows(categorized)

        # 综合排序：importance * 100 + confidence * 10 + evidence_count(≤50) + span(≤49)
        # 组合因子：evidence_count 捕捉"被反复检测"，span 捕捉"跨章时间范围"
        # 两者各贡献一半权重，避免单一因子的结构性歧视
        imp_order = {"高": 3, "中": 2, "低": 1}
        conf_order = {"高": 3, "中": 2, "低": 1}
        for c in catalog:
            evidence_count = len(c.get("evidence_chapters", []))
            span = c.get("last_seen", 0) - c.get("first_seen", 0)
            score = (
                imp_order.get(c.get("importance", "中"), 0) * 100
                + conf_order.get(c.get("confidence", "中"), 0) * 10
                + min(evidence_count, 50)
                + min(span, 49)
            )
            c["_sort_key"] = score
            c["composite_score"] = score  # 保留到 catalog 条目，供前端展示
            # 稳定实体键：优先用模型给的母题词，否则退回代表条目 id。
            # 一旦写进账本就不再改（改号=把同一件事拆成两条）。
            c["entity_key"] = entity_key_for(c.get("motif_key", ""), str(c.get("id", "")))
        catalog.sort(key=lambda c: c["_sort_key"], reverse=True)
        for c in catalog:
            c.pop("_sort_key", None)  # 临时排序键仍删除；composite_score 保留

        # 分层截断：importance=高 全部保留到 max_high，中 截到 max_mid
        high_items = [c for c in catalog if c.get("importance") == "高"]
        mid_items = [c for c in catalog if c.get("importance") == "中"]
        other_items = [c for c in catalog if c.get("importance") not in ("高", "中")]
        # 其他（如空字符串）按 confidence 排序
        other_items.sort(
            key=lambda c: conf_order.get(c.get("confidence", "中"), 0),
            reverse=True,
        )

        truncated = []
        max_high = a.max_foreshadow_catalog_high
        max_mid = a.max_foreshadow_catalog_mid
        if max_high == -1 or len(high_items) <= max_high:
            truncated.extend(high_items)
            high_kept = len(high_items)
        else:
            truncated.extend(high_items[:max_high])
            high_kept = max_high
            logger.info(f"高 importance 截断: {len(high_items)} -> {max_high}")
        if len(mid_items) <= max_mid:
            truncated.extend(mid_items)
            mid_kept = len(mid_items)
        else:
            truncated.extend(mid_items[:max_mid])
            mid_kept = max_mid
            logger.info(f"中 importance 截断: {len(mid_items)} -> {max_mid}")
        truncated.extend(other_items)

        logger.info(
            f"伏笔总表构建完成：{len(all_clues)} 条原始 -> {len(categorized)} 条过滤 -> "
            f"{len(truncated)} 条入总表（高 {high_kept}/{len(high_items)}, "
            f"中 {mid_kept}/{len(mid_items)}, "
            f"其他 {len(other_items)}）"
        )
        return truncated

    @staticmethod
    def _imp_at_least(value: str, threshold: str) -> bool:
        """判断 value 是否 >= threshold（高>=中>=低）。"""
        order = {"高": 3, "中": 2, "低": 1}
        return order.get(value, 0) >= order.get(threshold, 0)

    def _format_catalog_for_prompt(self, catalog: list, max_chapter: int = 99999) -> str:
        """
        格式化截至某章节的伏笔表，用于注入 BATCH_PROMPT / RECONCILIATION_PROMPT。
        格式：[fs_001] type|clue (ch1,15,837)
        """
        if not catalog:
            return "（暂无伏笔记录）\n"

        lines = []
        for item in catalog:
            if item["first_seen"] > max_chapter:
                continue
            chapters_str = ",".join(str(c) for c in item["evidence_chapters"][:8])
            merged = f" (合并{item['merged_count']}条)" if item["merged_count"] > 1 else ""
            ftype = item.get("type", "")
            type_prefix = f"[{ftype}] " if ftype else ""
            lines.append(f"[{item['id']}] {type_prefix}{item['clue']} (ch{chapters_str}){merged}")

        if not lines:
            return "（暂无伏笔记录）\n"

        return "\n".join(lines) + "\n"


    def _build_active_foreshadows_block(self, ch_start: int, ch_end: int, foreshadow_catalog: list = None) -> str:
        """构建【当前伏笔清单】文本块，使用伏笔总表的原始 clue"""
        if foreshadow_catalog:
            # 从伏笔总表取截至本批的条目；P1-7：仅保留账本中仍为 active 的条目——
            # 已回收伏笔不再进入后续批次的 reconciliation，避免反复重审（token 浪费）
            # 与"翻案"式状态抖动。
            ledger_map = {i.id: i for i in self.ledger.items}
            items = [
                c for c in foreshadow_catalog
                if c["first_seen"] <= ch_end
                and (c["id"] not in ledger_map or ledger_map[c["id"]].status == 'active')
            ]
        else:
            # 降级：用账本活跃列表
            active_items = [i for i in self.ledger.items if i.status == 'active']
            items = [{"id": i.id, "clue": i.description, "evidence_chapters": i.evidence_chapters,
                      "first_seen": i.first_seen_chapter, "last_seen": i.last_seen_chapter}
                     for i in active_items]

        if not items:
            return "\n【当前伏笔清单】\n（暂无伏笔，foreshadow_reconciliation 输出空数组 [] 即可）\n"

        lines = [
            f"\n【当前伏笔清单 - 共{len(items)}个，必须逐一判断是否在第{ch_start}-{ch_end}章中被回收】",
            "（注意：仅当伏笔核心悬念在本批次中得到结论性解决时才标记回收；仅被提及/继续推进不算回收）\n"
        ]
        for i, item in enumerate(items, 1):
            eid = item["id"]
            clue = item["clue"]
            chapters_str = ",".join(str(c) for c in item.get("evidence_chapters", [])[:5])
            first = item.get("first_seen", 0)
            last = item.get("last_seen", 0)
            lines.append(f'{i}. id="{eid}"  描述: {clue}')
            lines.append(f"   首次出现: 第{first}章  最近出现: 第{last}章  证据: [{chapters_str}]")

        return "\n".join(lines)

    def _build_foreshadow_context(self, foreshadow_catalog: list = None) -> str:
        """构建供 FINAL_PROMPT 使用的伏笔上下文——伏笔总表 clue + 账本状态合并"""
        if not self.ledger.items and not foreshadow_catalog:
            return ""

        # 建立账本 id -> item 的映射
        ledger_map = {i.id: i for i in self.ledger.items}

        # 合并：伏笔总表的 clue + 账本的状态
        all_items = []
        catalog_ids = set()
        if foreshadow_catalog:
            for cat in foreshadow_catalog:
                lid = cat["id"]
                catalog_ids.add(lid)
                ledger_item = ledger_map.get(lid)
                all_items.append({
                    "id": lid,
                    "clue": cat["clue"],
                    "first_seen": cat["first_seen"],
                    "last_seen": cat["last_seen"],
                    "evidence_chapters": cat["evidence_chapters"],
                    "status": ledger_item.status if ledger_item else "active",
                    "resolution": ledger_item.resolution if ledger_item else None,
                    "resolved_chapter": ledger_item.resolved_chapter if ledger_item else None,
                })

        # 补充账本中有但总表中没有的条目（兜底）
        for lid, item in ledger_map.items():
            if lid not in catalog_ids:
                all_items.append({
                    "id": lid,
                    "clue": item.description,
                    "first_seen": item.first_seen_chapter,
                    "last_seen": item.last_seen_chapter,
                    "evidence_chapters": item.evidence_chapters,
                    "status": item.status,
                    "resolution": item.resolution,
                    "resolved_chapter": item.resolved_chapter,
                })

        if not all_items:
            return ""

        active = [i for i in all_items if i["status"] == "active"]
        resolved = [i for i in all_items if i["status"] == "resolved"]
        dormant = [i for i in all_items if i["status"] == "dormant"]

        parts = []
        parts.append(f"## 伏笔总表（共{len(all_items)}条：活跃{len(active)} / 已回收{len(resolved)} / 休眠{len(dormant)}）\n")

        if resolved:
            parts.append("### 已回收伏笔")
            for item in resolved:
                chapters_str = ",".join(str(c) for c in item["evidence_chapters"][:5])
                parts.append(f'- [{item["id"]}] {item["clue"]} (ch{chapters_str})')
                res = item.get("resolution") or "未记录"
                rch = item.get("resolved_chapter") or "?"
                parts.append(f'  - 回收: ch{rch}, {res}')
            parts.append("")

        if active:
            parts.append("### 仍活跃伏笔（请逐一分析是否在全书中已被回收）")
            for item in active:
                chapters_str = ",".join(str(c) for c in item["evidence_chapters"][:5])
                parts.append(f'- [{item["id"]}] {item["clue"]} (ch{chapters_str})')
            parts.append("")

        if dormant:
            parts.append("### 休眠伏笔（长期未出现，可能是作者遗忘）")
            for item in dormant:
                parts.append(f'- [{item["id"]}] {item["clue"]} (ch{item["first_seen"]}-{item["last_seen"]})')
            parts.append("")

        parts.append("请在报告中特别关注：")
        parts.append('1. 标记为"已回收"的伏笔，分析其回收质量')
        parts.append('2. 标记为"休眠"的伏笔，这些可能是作者遗忘的伏笔')
        parts.append('3. 标记为"活跃"的伏笔——**请逐一核对**，判断它们在全书中是否实际上已经被回收了')
        parts.append("4. 所有伏笔结论必须带证据章节号")

        return "\n".join(parts)

    # 最终报告上下文预算保护（QUA-1）
    # 阈值与组大小改为读取 self.config.analysis.volume_compress_threshold / volume_compress_group（用户在设置中可调）

    def _compress_volume_summaries(self, volume_summaries: List[str]) -> str:
        """
        卷摘要拼接总字符超阈值时做轻量分层压缩（同步、零额外 LLM 调用）：
        - 首尾各 1 组保留全文（故事开端与结局信息最密）；
        - 中间组每条截断至 1/2 并标注省略，保留整体脉络。
        保证任意长度的小说都能进入最终报告调用，且上下文不超出模型有效注意力。
        """
        threshold = self.config.analysis.volume_compress_threshold
        group_size = self.config.analysis.volume_compress_group
        joined = "\n\n".join(volume_summaries)
        if len(joined) <= threshold or len(volume_summaries) <= 1:
            return joined
        logger.info(f"最终报告卷摘要超阈值（{len(joined)} 字符 > {threshold}），"
                    f"执行分层压缩（{len(volume_summaries)} 卷，组大小 {group_size}）")
        groups = [volume_summaries[i:i + group_size]
                  for i in range(0, len(volume_summaries), group_size)]
        parts = []
        n = len(groups)
        for gi, g in enumerate(groups):
            if gi == 0 or gi == n - 1:
                parts.append("\n\n".join(g))
            else:
                trimmed = [vs[:len(vs) // 2] + "\n（此处已压缩，省略后半）" for vs in g]
                parts.append("\n\n".join(trimmed))
        return "\n\n".join(parts)

    # ==================== 卷摘要断点续跑（2026-08-07） ====================
    # checkpoint 目录：{output_dir}/final_summary_checkpoint/
    #   volume_{idx}.md    批次卷摘要（一完成即写，不等 reconciliation）
    #   recon_{idx}.json   批次 reconciliation 结果（完成后写；失败不写→下次补）
    #   manifest.json      批次元数据（batch_size + start/end 校验用）
    # 恢复规则：batch_size 或批次 ch 范围与当前不一致时视为无效，忽略旧断点。

    @property
    def _checkpoint_dir(self) -> Path:
        return self.output_dir / "final_summary_checkpoint"

    def _compute_results_fingerprint(self) -> str:
        """章节数据版本指纹（name:mtime_ns:size），用于断点失效判断。

        与 location_normalizer 的 chapter_mtimes_hash 同思路：章节结果一旦被
        重新分析/手工修改，旧断点的卷摘要即视为过期。"""
        parts = []
        for f in sorted(self.output_dir.glob("chapter_*_result.json")):
            try:
                st = f.stat()
                parts.append(f"{f.name}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append(f"{f.name}:missing")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def _wipe_checkpoint_dir(self) -> None:
        """整体清空失效的断点目录（含孤儿 volume_*.md / recon_*.json）"""
        cp = self._checkpoint_dir
        if not cp.exists():
            return
        shutil.rmtree(cp, ignore_errors=True)
        logger.warning(f"已清空失效的总结断点目录: {cp}")

    async def _save_checkpoint_batch(self, idx: int, ch_start: int, ch_end: int,
                                     summary_text: Optional[str], recon_data: Optional[dict]) -> None:
        """落盘单个批次的断点（卷摘要与 reconciliation 各自独立写，两步互不阻塞）。

        卷摘要先写、reconciliation 后写：即使 reconciliation 阶段停止/失败，
        已完成的卷摘要也已落盘，下次重跑只需补 reconciliation（快速），
        不必重跑最耗时的卷摘要 LLM 调用。
        """
        cp = self._checkpoint_dir
        await asyncio.to_thread(cp.mkdir, parents=True, exist_ok=True)
        vf = cp / f"volume_{idx}.md"
        rf = cp / f"recon_{idx}.json"
        async with self._checkpoint_lock:
            # 本次新生成的内容无条件覆盖残留文件（原 not vf.exists() 守卫曾让
            # batch_size 变化后新摘要写不进盘，manifest 却登记旧分卷方案的旧内容，
            # 过期卷摘要静默毒化最终报告——P1 2026-08-24）。
            # 恢复场景安全：已恢复批次不会重新生成，不会走到这里覆写。
            if summary_text is not None:
                await asyncio.to_thread(vf.write_text, summary_text, encoding="utf-8")
            # reconciliation 失败返回 None 时不落盘 → 下次重跑补 reconciliation
            if recon_data is not None:
                await asyncio.to_thread(safe_save_json, recon_data, rf)
            # 更新 manifest（原子写；并发批次经 _checkpoint_lock 串行）
            manifest_path = cp / "manifest.json"
            m = await asyncio.to_thread(safe_load_json, manifest_path)
            if not isinstance(m, dict):
                m = {"batch_size": self.batch_size, "batches": {}}
            m["batch_size"] = self.batch_size
            m["results_fingerprint"] = await asyncio.to_thread(self._compute_results_fingerprint)
            entry = m.setdefault("batches", {}).setdefault(str(idx), {})
            entry["start"] = ch_start
            entry["end"] = ch_end
            if vf.exists():
                entry["summary"] = f"volume_{idx}.md"
            if rf.exists():
                entry["recon"] = f"recon_{idx}.json"
            m["batches"][str(idx)] = entry
            await asyncio.to_thread(safe_save_json, m, manifest_path)

    async def _load_checkpoint(self, batch_tasks: List[tuple]) -> Tuple[Dict[int, str], Dict[int, dict]]:
        """从断点恢复已完成批次。

        Returns:
            (volume_results: {idx: 卷摘要文本}, recon_results: {idx: reconciliation dict})
        恢复规则：batch_size / results_fingerprint / manifest 完整性任一不符时视为无效，
        清空旧断点目录后本次重新生成。
        """
        cp = self._checkpoint_dir
        manifest_path = cp / "manifest.json"
        if not manifest_path.exists():
            return {}, {}
        m = await asyncio.to_thread(safe_load_json, manifest_path)
        current_fp = await asyncio.to_thread(self._compute_results_fingerprint)
        if (not isinstance(m, dict)
                or m.get("batch_size") != self.batch_size
                or m.get("results_fingerprint") != current_fp):
            reason = ("manifest损坏" if not isinstance(m, dict)
                      else f"batch_size变化({m.get('batch_size')}->{self.batch_size})"
                      if m.get("batch_size") != self.batch_size
                      else "章节数据已变化")
            logger.info(f"总结断点失效（{reason}），清空旧断点目录重新生成")
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
                        if text.strip():
                            volume_results[idx] = text
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

    async def _cancel_style_task(self, style_task) -> None:
        """停止路径专用：cancel 并回收风格提取任务，杜绝孤儿任务继续烧 token"""
        if style_task is None:
            return
        if not style_task.done():
            style_task.cancel()
        try:
            await asyncio.gather(style_task, return_exceptions=True)
        except Exception:
            pass

    async def _run_style_extraction(self) -> Optional[dict]:
        """调用 style_analyzer 提取风格特征"""
        # 优先书目录下的 blocks（web 版按书目录组织），降级 working_directory
        blocks_dir = self.output_dir.parent / "blocks"
        if not blocks_dir.exists() and self.config.working_directory:
            blocks_dir = Path(self.config.working_directory) / "blocks"
        if not blocks_dir.exists():
            logger.warning(f"风格分析：blocks 目录不存在: {blocks_dir}")
            return None

        api_config = {
            'base_url': self.config.api.base_url,
            'api_key': self.config.api.api_key,
            'model': self.config.api.summary_model or self.config.api.model,
            'thinking_mode': self.config.api.summary_thinking_mode or self.config.api.thinking_mode,
            'timeout': self.config.api.summary_timeout,
            'provider': self.config.api.provider,
        }

        try:
            # 全局 Sem 包裹：让风格分析的 LLM 调用也参与 self.concurrency 上限
            # （style 用独立 LLMClient，连接池隔离，但 API 配额共享，必须走同一信号量）
            await self._acquire_llm_slot()
            try:
                return await extract_style_profile(
                    blocks_dir, self.book_name, api_config,
                    token_sink=lambda t: self._record_tokens("style", t))
            finally:
                self._release_llm_slot()
        except Exception as e:
            logger.warning(f"风格分析失败: {e}")
            return None

    def _format_style_for_prompt(self, style_profile: dict) -> str:
        """将 style_profile 格式化为自然语言段落，注入最终报告 prompt"""
        agg = style_profile.get("statistical", {})
        sem = style_profile.get("semantic", {})

        parts = ["## 写作风格特征\n"]

        # 统计部分
        parts.append("### 定量指标")
        parts.append(f"- 平均句长 {agg.get('avg_sentence_length', 0):.1f} 字，"
                     f"短句(≤10字) {agg.get('short_sentence_ratio', 0):.1%}，"
                     f"长句(≥50字) {agg.get('long_sentence_ratio', 0):.1%}")
        parts.append(f"- 对话占比 {agg.get('dialogue_ratio', 0):.1%}，"
                     f"词汇丰富度(TTR) {agg.get('ttr', 0):.2f}")
        parts.append(f"- 感官词密度 {agg.get('sensory_density', 0):.1f}/千字，"
                     f"比喻词频率 {agg.get('metaphor_freq', 0):.1f}/千字")
        parts.append(f"- 文言词占比 {agg.get('literary_ratio', 0):.2f}%，"
                     f"口语词占比 {agg.get('colloquial_ratio', 0):.2f}%")

        # 语义部分
        parts.append("\n### 定性特征")
        for key, label in [
            ('signature_expressions', '标志性表达'),
            ('narrative_rhythm', '叙事节奏'),
            ('dialogue_style', '对话风格'),
            ('rhetorical_preferences', '修辞偏好'),
            ('emotional_expression', '情感表达'),
            ('narrative_voice', '叙事视角'),
            ('information_control', '信息管控'),
            ('narrator_and_genre', '叙述者与类型'),
        ]:
            value = sem.get(key, "")
            if value and '未提取' not in value and '提取失败' not in value:
                parts.append(f"- **{label}**：{value}")

        return "\n".join(parts)

    def _apply_reconciliation(self, batch_idx: int, ch_start: int, ch_end: int, foreshadow_data: Optional[dict]) -> None:
        """应用 LLM 的 reconciliation 结果到账本。

        P0-2：休眠判定不在此处执行——批次并发完成导致 last_seen_batch 非单调，
        即时判定结果随完成顺序漂移；统一由 run() 在全部批次完成后按 batch_idx
        升序、以各批真实 ch_end 为"当前进度"执行 reconcile_and_update。
        """
        if foreshadow_data is None:
            logger.warning(f"批次{batch_idx}：reconciliation 缺失，伏笔状态保持不变")
            return
        reconciliation = foreshadow_data.get('reconciliation', []) or []
        for item in reconciliation:
            if not isinstance(item, dict):
                continue
            foreshadow_id = item.get('foreshadow_id', '')
            if not foreshadow_id:
                continue
            existing = self.ledger._find_item(foreshadow_id)
            is_resolved = bool(item.get('is_resolved_in_this_batch', False))
            if is_resolved:
                if existing:
                    existing.status = 'resolved'
                    existing.resolution = item.get('resolution_summary') or item.get('reason') or ''
                    existing.resolved_chapter = item.get('resolved_chapter') or ch_end
                    existing.resolved_batch = batch_idx
                    existing.source_type = 'llm_judged'
                    existing.last_seen_chapter = ch_end
                    existing.last_seen_batch = batch_idx
                    # 低置信回收（confidence 非"高"）→ 标人工复核（防 fs_001 式错位直接入库）
                    confidence = item.get('confidence', '高')
                    if confidence != '高' or not existing.resolution:
                        existing.needs_review = True
                        existing.notes.append(f"回收置信度={confidence}，建议人工复核（批次{batch_idx}）")
                    logger.info(f"LLM标记伏笔回收: {foreshadow_id} (第{existing.resolved_chapter}章)")
            # BUG-A 修复（2026-08-13）：未回收分支不再推进 last_seen_chapter/batch。
            # 旧实现把"本批未标记回收"当成"本批出现过"，无条件把 last_seen 推到批末 ch_end，
            # 导致休眠判定的章距条件永远 ≈0，休眠机制形同虚设（《奥术神座》0 休眠实锤）。
            # last_seen 语义 = 伏笔最后出现的真实章节，只由 catalog 证据章维护；
            # "本批未回收"只说明伏笔存活，不说明它出现了。

    def _plan_recheck_batches(self, recheck_items: list, full_text: str,
                              volume_summaries: List[str],
                              volume_ranges: List[Tuple[int, int]] = None) -> List[Tuple[list, str]]:
        """规划复检上下文：返回 [(伏笔子集, 该子集使用的卷摘要文本)]。
        未超预算 → [(全部, 全文)]（2026-08-07 决策的原行为，KV cache 友好）；
        超预算 → 按 first_seen 升序切 ≤3 个连续组，每组砍掉"ch_end < 组内最早埋设章"的卷
        （召回无损：回收必发生在埋设之后）；砍后仍超预算的组丢弃并记 error，不调用 LLM。"""
        budget = RECHECK_FULLTEXT_BUDGET_CHARS
        if len(full_text) <= budget:
            return [(recheck_items, full_text)]
        if not volume_ranges:
            logger.error(f"复检卷摘要超预算（{len(full_text)}>{budget}）且无卷章范围信息，跳过全书复检")
            return []
        sorted_items = sorted(recheck_items, key=lambda c: c.get("first_seen", 0))
        n = len(sorted_items)
        per = (n + 2) // 3  # ≤3 组，保住组间卷摘要前缀一致（KV cache）
        plans: List[Tuple[list, str]] = []
        for gstart in range(0, n, per):
            group = sorted_items[gstart:gstart + per]
            min_first = min(c.get("first_seen", 0) for c in group)
            kept = [i for i, (_cs, ce) in enumerate(volume_ranges) if ce >= min_first]
            group_text = "\n\n".join(volume_summaries[i] for i in kept)
            if len(group_text) > budget:
                logger.error(f"复检组（{len(group)}个伏笔，最早埋设第{min_first}章）砍卷后仍超预算"
                             f"（{len(group_text)}>{budget}），跳过该组")
                self._emit_progress({"type": "status",
                                     "message": f"⚠️ 复检：{len(group)}个伏笔因上下文超限跳过"})
                continue
            dropped = len(volume_ranges) - len(kept)
            if dropped:
                logger.info(f"复检上下文超限降级：组内最早埋设第{min_first}章，砍掉埋设前 {dropped} 卷（召回无损）")
                self._emit_progress({"type": "status",
                                     "message": f"复检卷摘要超限，按埋设章砍掉 {dropped} 卷（不影响召回）"})
            plans.append((group, group_text))
        return plans

    def _collect_book_text(self) -> str:
        """全文：复检的证据必须是**原文**。

        旧实现拿"卷摘要拼接"当全文——那是模型写的概括，拿它去核对"引文是否逐字存在"
        永远核不出问题，等于没有证据。这里读 blocks 下的章节正文。
        """
        blocks_dir = self.output_dir.parent / "blocks"
        if not blocks_dir.is_dir() and self.config.working_directory:
            blocks_dir = Path(self.config.working_directory) / "blocks"
        if not blocks_dir.is_dir():
            logger.warning("复检 V2：blocks 目录不存在，无法取得原文，本轮将全部记为未核对")
            return ""
        parts: List[str] = []
        for path in sorted(blocks_dir.glob("*.txt"), key=lambda x: x.name):
            try:
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError as exc:  # 单章读失败不该让整轮复检崩掉
                logger.warning(f"复检 V2：读取 {path.name} 失败：{exc}")
        return "\n\n".join(parts)

    async def _run_recheck_v2(self, results: List[dict]) -> None:
        """阶段 2（V2）：带证据的复检。"""
        from backend.services.recheck_v2 import run_recheck_v2

        book_text = self._collect_book_text()
        if not book_text:
            self.last_recheck_summary = {
                "status": "NOT_RUN",
                "status_reason": "取不到原文，未执行复检",
                "calls": 0,
            }
            logger.warning("复检 V2：未执行（没有原文）")
            return

        max_calls = max(1, int(getattr(self.config.analysis, "recheck_max_calls", 300) or 300))
        max_tokens = int(getattr(self.config.analysis, "recheck_max_tokens", 60000) or 60000)
        batch_size = max(1, int(getattr(self.config.analysis, "foreshadow_recheck_batch_size", 40) or 40))

        async def _call_model(prompt: str) -> str:
            res = await self.client.chat_auto(
                prompt=prompt, max_tokens=max_tokens, purpose="foreshadow_recheck_v2"
            )
            return getattr(res, "content", "") or ""

        self._emit_progress({"type": "status", "message": "正在核对伏笔证据..."})
        outcome = await run_recheck_v2(
            self.ledger.items,
            book_text=book_text,
            call_model=_call_model,
            max_calls=max_calls,
            batch_size=batch_size,
            emit=lambda summary: self._emit_progress(
                {"type": "status", "message": f"伏笔复检：已核对 {summary['coverage'].get('checked', 0)} 条"}
            ),
        )
        self.ledger.save(self.ledger_path)
        self.last_recheck_summary = outcome.summary()
        logger.info(
            f"复检 V2 完成：{outcome.status} 调用 {outcome.calls} 次，"
            f"覆盖 {outcome.coverage.get('checked', 0)}/{outcome.coverage.get('candidates_total', 0)}"
        )

    async def _run_global_foreshadow_recheck(self, volume_summaries: List[str],
                                             foreshadow_catalog: list = None,
                                             volume_ranges: List[Tuple[int, int]] = None) -> None:
        """全书伏笔复检：对仍 active 的伏笔做一次全书范围的集中复检"""
        active_ids = {i.id for i in self.ledger.items if i.status == 'active'}
        if not active_ids:
            logger.info("无活跃伏笔，跳过全书复检")
            return

        # 从伏笔总表取活跃伏笔的原始 clue（比账本 description 更准确）
        if foreshadow_catalog:
            recheck_items = [c for c in foreshadow_catalog if c["id"] in active_ids]
        else:
            # 降级：用账本
            recheck_items = [{"id": i.id, "clue": i.description,
                              "first_seen": i.first_seen_chapter, "last_seen": i.last_seen_chapter,
                              "evidence_chapters": i.evidence_chapters}
                             for i in self.ledger.items if i.status == 'active']

        if not recheck_items:
            logger.info("无活跃伏笔，跳过全书复检")
            return

        logger.info(f"开始全书伏笔复检：{len(recheck_items)}个活跃伏笔")
        full_text = "\n\n".join(volume_summaries)
        recheck_batch_size = max(1, int(self.config.analysis.foreshadow_recheck_batch_size or 40))
        total_rechecked = 0

        # 复检上下文策略（2026-08-07 用户决策）：默认维持原逻辑——每个子批携带
        # 全量卷摘要，不做任何裁剪（KV cache 厂商只有第一份全文付全价）。
        # 2026-08-17 补充：仅当全量卷摘要超 RECHECK_FULLTEXT_BUDGET_CHARS 时，
        # 由 _plan_recheck_batches 按埋设章砍"埋设前的卷"（召回无损）或整组跳过，
        # 防止超模型上下文走完整重试链烧钱。

        # 准备所有复检子批次
        recheck_batches = []
        for plan_items, plan_text in self._plan_recheck_batches(
                recheck_items, full_text, volume_summaries, volume_ranges):
            for batch_start in range(0, len(plan_items), recheck_batch_size):
                batch_items = plan_items[batch_start:batch_start + recheck_batch_size]
                lines = []
                for i, item in enumerate(batch_items, 1):
                    chapters_str = ",".join(str(c) for c in item.get("evidence_chapters", [])[:5])
                    lines.append(f'{i}. id="{item["id"]}"  描述: {item["clue"]}')
                    lines.append(f"   首次出现: 第{item.get('first_seen', 0)}章  最近出现: 第{item.get('last_seen', 0)}章  证据: [{chapters_str}]")
                recheck_prompt = GLOBAL_RECHECK_USER_TEMPLATE.format(
                    book_name=self.book_name, volume_summaries=plan_text,
                    active_foreshadows_block='\n'.join(lines)
                )
                recheck_batches.append((batch_items, recheck_prompt))

        # H17 P3 V2 修复（2026-08-26）：phase 事件后移到 recheck_batches 构造后，
        # 补 total_batches 字段。修复前 phase 事件在 batches 之前就发了，
        # 前端 LaneView 兼容层 applySummaryProgress 看到 totalBatches=0 直接跳过
        # recheck 合成，910001-910999 id 范围是死代码 → 阶段 2/4 全书伏笔复检
        # （可能跑很久）时 LaneView 全空。
        self._emit_progress({
            "type": "phase", "phase": "recheck", "phase_label": "全书伏笔复检",
            "total_batches": len(recheck_batches),
            "message": f"[阶段2/4] 全书伏笔复检（{len(recheck_items)}个活跃伏笔）..."
        })

        if not recheck_batches:
            logger.warning("复检计划为空（全部超预算跳过），结束全书复检")
            return

        semaphore = asyncio.Semaphore(min(self.concurrency, len(recheck_batches)))

        async def process_recheck_batch(prompt: str, batch_idx: int) -> int:
            async with semaphore:
                if self._stop_requested:
                    return 0
                foreshadow_data = await self._call_llm_global_recheck(prompt, batch_idx=batch_idx)
            if foreshadow_data is None:
                return 0
            local_count = 0
            for item in foreshadow_data.get('reconciliation', []) or []:
                if not isinstance(item, dict):
                    continue
                foreshadow_id = item.get('foreshadow_id', '')
                if not foreshadow_id:
                    continue
                async with self._lock:
                    existing = self.ledger._find_item(foreshadow_id)
                    is_resolved = bool(item.get('is_resolved_in_this_batch', False))
                    if is_resolved and existing and existing.status == 'active':
                        existing.status = 'resolved'
                        existing.resolution = item.get('resolution_summary') or item.get('reason') or ''
                        existing.resolved_chapter = item.get('resolved_chapter') or 0
                        existing.source_type = 'llm_judged'
                        # 低置信回收 → 标人工复核
                        confidence = item.get('confidence', '高')
                        if confidence != '高' or not existing.resolution:
                            existing.needs_review = True
                            existing.notes.append(f"复检回收置信度={confidence}，建议人工复核")
                        logger.info(f"全书复检回收: {foreshadow_id} (第{existing.resolved_chapter}章)")
                        local_count += 1
            return local_count

        tasks = [asyncio.create_task(process_recheck_batch(prompt, batch_idx=i))
                 for i, (_, prompt) in enumerate(recheck_batches)]
        for coro in asyncio.as_completed(tasks):
            try:
                batch_resolved = await coro
                total_rechecked += batch_resolved
                # 阶段2 续跑（2026-08-09）：每完成一批立即落盘 ledger。
                # 崩溃/停止后重启时 recheck_items 只取 active 伏笔，
                # 已回收的自动排除 → 只对剩余伏笔重新复检，不再重复付费。
                if batch_resolved > 0:
                    async with self._lock:
                        self.ledger.save(self.ledger_path)
            except Exception as e:
                logger.error(f"复检批次异常: {e}")

        logger.info(f"全书伏笔复检完成，新回收 {total_rechecked} 个伏笔")
        if total_rechecked > 0:
            self._emit_progress({"type": "ledger_updated", "counts": self.ledger.get_item_count()})

    async def run(self) -> Optional[str]:
        """执行完整总结流程。返回最终报告文本；用户停止返回 None；失败抛 RuntimeError"""
        t_start = time.time()

        # Phase 0 归一化已独立为独立功能，不再作为总结的强制前置条件
        # 归一化结果仅用于地图可视化 & Phase 0b 空间归一化，不影响最终总结生成
        output_subdir = self.output_dir / "output"
        if not output_subdir.is_dir():
            output_subdir = self.output_dir

        self._emit_progress({"type": "status", "message": "正在加载章节数据..."})
        results = await asyncio.to_thread(self._load_results)
        if not results:
            raise RuntimeError("未找到有效的章节分析结果")
        total_chapters = len(results)
        total_batches = (total_chapters + self.batch_size - 1) // self.batch_size
        # 账本总章数用真实末章号（BUG-C：旧实现存的是块数 len(results)，
        # 块大小>1 时 total_chapters 与"章"的语义错位，休眠/审计分母失真）
        self.ledger.total_chapters = results[-1]['ch'] if results else 0
        self.ledger.batch_size_snapshot = self.batch_size
        logger.info(f"加载{total_chapters}块，分为{total_batches}批，每批{self.batch_size}章")

        # 构建伏笔总表（async：含 type→category LLM 归一化调用）
        foreshadow_catalog = await self._build_foreshadow_catalog(results)

        # 同步伏笔总表到账本（账本为空时初始化，让 reconciliation 有条目可更新）
        existing_ids = {i.id for i in self.ledger.items}
        added = 0
        for cat in foreshadow_catalog:
            if cat["id"] not in existing_ids:
                self.ledger.items.append(ForeshadowItem(
                    id=cat["id"], description=cat["clue"],
                    first_seen_chapter=cat["first_seen"], first_seen_batch=0,
                    last_seen_chapter=cat["last_seen"], last_seen_batch=0,
                    evidence_chapters=cat["evidence_chapters"],
                    confidence=0.8, source_type='catalog_scan',
                    composite_score=cat.get("composite_score", 0),
                    importance=cat.get("importance", "中"),
                    anchor_quote=cat.get("anchor_quote", "") or "",
                    anchor_chapter=int(cat.get("anchor_chapter") or 0),
                    entity_key=cat.get("entity_key", "") or "",
                    question=cat.get("question", "") or "",
                    answer_criteria=cat.get("answer_criteria", "") or "",
                    lead=not (cat.get("question") or "").strip()
                    and not (cat.get("answer_criteria") or "").strip(),
                ))
                added += 1
        logger.info(f"伏笔总表同步到账本: {added} 条新增，{len(existing_ids)} 条已存在")

        batch_tasks = []
        for i in range(0, total_chapters, self.batch_size):
            batch = results[i:i + self.batch_size]
            batch_idx = len(batch_tasks) + 1
            ch_start = batch[0]['ch']
            ch_end = batch[-1]['ch']
            batch_data = self._format_batch(batch)
            catalog_text = self._format_catalog_for_prompt(foreshadow_catalog, max_chapter=ch_end)
            summary_prompt = BATCH_USER_TEMPLATE.format(
                book_name=self.book_name, start=ch_start, end=ch_end,
                batch_data=batch_data, foreshadow_catalog=catalog_text,
                batch_min_words=self.config.analysis.batch_summary_min_words
            )
            batch_tasks.append((batch_idx, ch_start, ch_end, summary_prompt))

        self._emit_progress({
            "type": "phase", "phase": "batch", "phase_label": "分卷分析+伏笔调和",
            "total_batches": total_batches,
            "message": f"[阶段1/4] 分卷分析+伏笔调和（{len(batch_tasks)}批，并发{self.concurrency}）..."
        })

        logger.info(f"开始并发卷摘要：{len(batch_tasks)}批，并发数={self.concurrency}")

        # 断点续跑：恢复已完成批次的卷摘要/reconciliation，跳过对应 LLM 调用
        # （卷摘要是全流程最耗时的阶段，中途停止后重跑无需重复付费）
        volume_results, recon_results = await self._load_checkpoint(batch_tasks)
        restored = len(volume_results)
        if restored:
            logger.info(f"断点续跑：恢复 {restored}/{len(batch_tasks)} 批卷摘要，跳过对应 LLM 调用")
            self._emit_progress({"type": "status",
                                 "message": f"已从断点恢复 {restored} 批卷摘要，继续剩余批次..."})
        else:
            self._emit_progress({"type": "status", "message": f"正在并发分析{len(batch_tasks)}批（并发{self.concurrency}）..."})

        async def process_batch_collect(idx: int, ch_start: int, ch_end: int, prompt: str):
            """并发采集：卷摘要 + reconciliation（断点恢复的批次跳过对应 LLM 调用）

            2026-08-23：去掉外层局部 Sem，改为全局 self._llm_sem（由 _call_llm_* 内部 acquire/release）。
            这样 summary 与 reconciliation 两次 LLM 调用各自独立占槽，
            - checkpoint 落盘 IO 不再抱死 Sem 槽
            - 任意时刻总在飞 LLM 数 ≤ self.concurrency（与阶段 2/3/4 共享同一上限）
            """
            if self._stop_requested:
                return None

            # 卷摘要：优先用断点恢复值，否则调用 LLM 并立即落盘（不等 reconciliation，
            # 防止 reconciliation 阶段停止导致已完成的卷摘要白做）
            if idx in volume_results:
                volume_summary = volume_results[idx]
                elapsed = 0.0
                restored_batch = True
            else:
                summary_response = await self._call_llm_summary(prompt, batch_idx=idx)
                if self._stop_requested:
                    return None
                if summary_response is None:
                    # 失败批次不入 volume_results（仅记失败状态），避免占位文本污染最终报告；
                    # 否则 not volume_results 永不成立、『所有批次均失败』的 RuntimeError 变死代码。
                    return {"status": "failed", "idx": idx, "ch_start": ch_start, "ch_end": ch_end}
                volume_summary = extract_analysis_text(summary_response)
                volume_results[idx] = volume_summary
                elapsed = time.time() - t_start
                restored_batch = False
                await self._save_checkpoint_batch(idx, ch_start, ch_end, volume_summary, None)

            # 伏笔 reconciliation：断点已存结果则跳过 LLM，否则调用并落盘
            if idx in recon_results:
                foreshadow_data = recon_results[idx]
            else:
                active_block = self._build_active_foreshadows_block(ch_start, ch_end, foreshadow_catalog)
                reconciliation_prompt = RECONCILIATION_USER_TEMPLATE.format(
                    book_name=self.book_name, start=ch_start, end=ch_end,
                    batch_summary=volume_summary,
                    active_foreshadows_block=active_block
                )
                foreshadow_data = await self._call_llm_reconciliation(reconciliation_prompt, batch_idx=idx)
                if foreshadow_data is not None:
                    await self._save_checkpoint_batch(idx, ch_start, ch_end, None, foreshadow_data)
            return {
                "status": "done", "idx": idx, "ch_start": ch_start, "ch_end": ch_end,
                "volume_summary": volume_summary, "elapsed": elapsed,
                "foreshadow_data": foreshadow_data, "restored": restored_batch,
            }

        tasks = [asyncio.create_task(process_batch_collect(idx, start, end, prompt))
                 for idx, start, end, prompt in batch_tasks]

        # 阶段 3 风格提取：与阶段 1 同步启动（数据无依赖：仅需 blocks_dir）
        # 与阶段 1/2/4 共用 self._llm_sem，硬上限 = self.concurrency（用户配置 4/9/20 均不超）
        # 风格提取通常 30-60s，期间会与阶段 1 抢同一信号量槽位
        style_task = asyncio.create_task(self._run_style_extraction())
        self._style_task = style_task

        # P2 收口（2026-08-24）：消费循环到风格结果回收之间任何异常（"所有批次
        # 均失败"的 RuntimeError、复检抛错等）都不再遗留孤儿 style_task 继续
        # 烧 token。正常路径 finally 时任务已被 await（done=True），cancel 为
        # no-op；下方两条停止路径的显式回收保留（幂等双保险）。
        try:
            for coro in asyncio.as_completed(tasks):
                try:
                    result = await coro
                    if result is None:
                        continue
                    if result["status"] == "done":
                        idx = result["idx"]
                        self._emit_progress({
                            "type": "batch_done", "batch": idx, "total_batches": total_batches,
                            "elapsed": result["elapsed"],
                            "message": f"[卷{idx}/{total_batches}] {'已从断点恢复' if result.get('restored') else '完成'}"
                        })
                        async with self._lock:
                            self._apply_reconciliation(
                                idx, result["ch_start"], result["ch_end"], result["foreshadow_data"])
                    elif result["status"] == "failed":
                        idx = result["idx"]
                        self._emit_progress({
                            "type": "batch_failed", "batch": idx,
                            "message": f"[卷{idx}] LLM调用失败，跳过"
                        })
                except Exception as e:
                    logger.error(f"卷异常: {e}")

            if self._stop_requested:
                await self._cancel_style_task(style_task)
                self._emit_progress({"type": "status", "message": "已停止"})
                return None
            if not volume_results:
                raise RuntimeError("所有批次均失败，无法生成总结")

            # 按 batch_idx 排序，恢复顺序
            volume_summaries = []
            volume_ranges: List[Tuple[int, int]] = []
            for idx, ch_start, ch_end, _ in batch_tasks:
                text = volume_results.get(idx, "")
                if text:
                    volume_summaries.append(f"=== 第{ch_start}-{ch_end}章 ===\n{text}")
                    volume_ranges.append((ch_start, ch_end))

            # 全书伏笔复检：开关只切算法，账本/报告/接口形状不变
            if getattr(self.config.analysis, "recheck_v2_enabled", False):
                await self._run_recheck_v2(results)
            else:
                await self._run_global_foreshadow_recheck(volume_summaries, foreshadow_catalog, volume_ranges)
            if self._stop_requested:
                # 复检可能已回收伏笔，保存账本保留进度后退出
                self.ledger.save(self.ledger_path)
                await self._cancel_style_task(style_task)
                self._emit_progress({"type": "status", "message": "已停止"})
                return None

            # 休眠判定统一收尾（BUG-D 修复：必须在复检之后执行——复检只审 active 伏笔，
            # 若先休眠，本可被复检回收的伏笔会被冻结成 dormant 绕过复检，造成
            # "已回收但标签未更新"（《奥术神座》报告第一类 ~60 条）。）
            # 按批次升序、以各批真实 ch_end 为"当前进度"执行 reconcile，保证休眠名单确定。
            if not self._stop_requested:
                for idx, _ch_start, ch_end, _prompt in sorted(batch_tasks, key=lambda t: t[0]):
                    self.ledger.reconcile_and_update(idx, ch_end)

            self.ledger.save(self.ledger_path)
            self._emit_progress({"type": "ledger_updated", "counts": self.ledger.get_item_count()})

            # 风格提取：实际工作已在阶段 1 启动时同步 create_task（与阶段 1 并行），
            # 此处只 await 收结果。emit 时机不变（复检完成后），保持 UI 阶段标签顺序。
            if not style_task.done():
                self._emit_progress({
                    "type": "phase", "phase": "style", "phase_label": "风格分析",
                    "message": "[阶段3/4] 正在提取写作风格特征..."
                })
            style_profile = await style_task
        finally:
            await self._cancel_style_task(style_task)
            self._style_task = None
        style_text = self._format_style_for_prompt(style_profile) if style_profile else "（风格分析未完成）"
        if self._stop_requested:
            self._emit_progress({"type": "status", "message": "已停止"})
            return None

        # 生成最终报告
        self._emit_progress({
            "type": "phase", "phase": "report", "phase_label": "生成全书脉络报告",
            "message": "[阶段4/4] 正在生成全书脉络报告..."
        })
        foreshadow_context = self._build_foreshadow_context(foreshadow_catalog)
        audit_text = self.ledger.audit_text_for_report()
        final_prompt = FINAL_USER_TEMPLATE.format(
            book_name=self.book_name,
            volume_summaries=self._compress_volume_summaries(volume_summaries),
            foreshadow_context=foreshadow_context,
            foreshadow_audit=audit_text,
            style_profile=style_text,
            final_min_words=self.config.analysis.final_report_min_words
        )
        final_report = await self._call_llm_final(final_prompt, batch_idx=0)
        if self._stop_requested:
            # 用户停止：返回 None（状态为已停止），不抛失败
            self._emit_progress({"type": "status", "message": "已停止"})
            return None
        if final_report is None:
            raise RuntimeError("最终汇总LLM调用失败")

        # 注意：style_profile 已通过 FINAL_USER_TEMPLATE 的 {style_profile} 注入最终报告 prompt，
        # 此处不再无条件追加，否则风格部分会在报告里出现两次。

        audit_path = self.output_dir / "foreshadow_audit.md"
        await asyncio.to_thread(audit_path.write_text, audit_text, encoding='utf-8')

        elapsed = time.time() - t_start
        # 2026-08-23：4 阶段 + 风格共用 self._llm_sem，记录 peak in-flight 便于验证未超 API 上限
        logger.info(f"final_summary 完成：peak in-flight = {self._peak_inflight} / concurrency = {self.concurrency}")
        if self._peak_inflight > self.concurrency:
            logger.error(f"BUG：peak in-flight {self._peak_inflight} 超 concurrency {self.concurrency}！")
        self._emit_progress({"type": "complete", "elapsed": elapsed,
                             "message": f"全书脉络报告生成完成，耗时{elapsed:.0f}秒"})
        return final_report
