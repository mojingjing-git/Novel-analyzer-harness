"""
配置管理模块
负责加载、保存和验证应用配置
（含 workspace_dir 与 max_* 知识库限制字段）
"""

import json
import logging
import math
import os
import re
import dataclasses
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

from ..utils.json_utils import safe_save_json

from .constants import (
    DEFAULT_BASE_URL, DEFAULT_API_KEY, DEFAULT_MODEL,
    DEFAULT_MAX_ARC_LENGTH,
    DEFAULT_CONCURRENCY, DEFAULT_BLOCK_SIZE,
    DEFAULT_MAX_ARCS_IN_PROMPT,
    DEFAULT_MAX_SUMMARIES_IN_PROMPT, DEFAULT_TIMELINE_TRUNCATE,
    DEFAULT_MAX_CHARACTER_STATES_IN_PROMPT,
    DEFAULT_MAX_WORLD_ITEMS_IN_PROMPT,
    DEFAULT_MAX_FORESHADOW_ENTRIES_IN_PROMPT,
    DEFAULT_BATCH_SUMMARY_MIN_WORDS,
    DEFAULT_FINAL_REPORT_MIN_WORDS,
    DEFAULT_VOLUME_COMPRESS_THRESHOLD, DEFAULT_VOLUME_COMPRESS_GROUP,
    MAX_OUTPUT_TOKENS, DEFAULT_TIMEOUT, DEFAULT_SUMMARY_TIMEOUT, MAX_RETRIES, SDK_MAX_RETRIES,
    DEFAULT_TEMPERATURE, DEFAULT_TEMPERATURE_STEP,
    TEMPERATURE_MAX_RETRIES, BACKOFF_MAX_RETRIES,
    ENCODING_CANDIDATES,
    CONFIG_FILE_NAME, KNOWLEDGE_FILE_NAME,
    DEFAULT_LANGUAGE,
    DEFAULT_CHECKPOINT_INTERVAL,
    ROLLING_EARLY_CHAPTERS,
    ROLLING_MAX_MILESTONES, ROLLING_MAX_MOMENTUM,
    MAX_COMPRESSED_ARCS, MAX_RECENT_SUMMARIES,
    MAX_PACING_TRACKER, MAX_FORESIGHT_NETWORK,
    MAX_WORLD_BUILDING, MAX_VERIFIED_FACTS,
    MAX_LONG_TERM_ARCS, MAX_THEMATIC_ELEMENTS,
    FORESHADOW_CATEGORY_DEFS,
    DEFAULT_FORESHADOW_MIN_IMPORTANCE,
    DEFAULT_FORESHADOW_MIN_CONFIDENCE,
    DEFAULT_MAX_FORESHADOW_CATALOG_HIGH,
    DEFAULT_MAX_FORESHADOW_CATALOG_MID,
)


def _coerce_fields(cls, data: dict) -> dict:
    """按 dataclass 字段声明类型清洗并强转配置值（含未知字段过滤）。

    PUT /api/settings 的入参不能以裸 dict 直透 dataclass 构造：
    字符串数值入库后 load() 的数值比较抛 TypeError 且位于 try 外，服务初始化失败
    → 全部 API 永久 500。规则：
    - int/float：接受数字或数字字符串；非法值丢弃 → 字段默认值
    - bool：接受 true/false/on/off/0/1/yes/no 及数字
    - List[str]/dict：类型不符整体丢弃 → 默认值
    - str：一律 str()
    """
    out = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        val = data[f.name]
        ftype = f.type
        try:
            if ftype is bool:
                if isinstance(val, bool):
                    out[f.name] = val
                elif isinstance(val, str):
                    v = val.strip().lower()
                    if v in ("true", "1", "yes", "on"):
                        out[f.name] = True
                    elif v in ("false", "0", "no", "off"):
                        out[f.name] = False
                elif isinstance(val, (int, float)):
                    out[f.name] = bool(val)
            elif ftype is int:
                if isinstance(val, bool):
                    out[f.name] = int(val)
                elif isinstance(val, int):
                    out[f.name] = val
                elif isinstance(val, float) and float(val).is_integer():
                    out[f.name] = int(val)
                elif isinstance(val, str):
                    s = val.strip()
                    if re.fullmatch(r"[+-]?\d+", s):
                        out[f.name] = int(s)
                    elif re.fullmatch(r"[+-]?\d+\.0*", s):
                        out[f.name] = int(float(s))
            elif ftype is float:
                if isinstance(val, bool):
                    out[f.name] = float(val)
                elif isinstance(val, (int, float)):
                    out[f.name] = float(val)
                elif isinstance(val, str):
                    s = val.strip()
                    if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)", s):
                        out[f.name] = float(s)
            elif ftype is str:
                out[f.name] = str(val)
            elif ftype is List[str]:
                if isinstance(val, list) and all(isinstance(x, str) for x in val):
                    out[f.name] = list(val)
            elif ftype is dict:
                if isinstance(val, dict):
                    out[f.name] = val
        except Exception:
            continue  # 任何强转意外都回落字段默认值
    return out


@dataclass
class APIConfig:
    """API配置"""
    base_url: str = DEFAULT_BASE_URL
    api_key: str = DEFAULT_API_KEY
    model: str = DEFAULT_MODEL
    max_tokens: int = MAX_OUTPUT_TOKENS
    timeout: int = DEFAULT_TIMEOUT
    summary_timeout: int = DEFAULT_SUMMARY_TIMEOUT  # 最终总结阶段 API 超时（秒）。比 timeout 长，因为最终报告 prompt 输入更大（11 万字符级）；final_summary 服务创建 LLMClient 时会覆盖 timeout 字段使用此值
    max_retries: int = MAX_RETRIES
    sdk_max_retries: int = SDK_MAX_RETRIES
    json_mode: str = "default"  # default/qwen/deepseek/glm47
    temperature: float = DEFAULT_TEMPERATURE
    temperature_step: float = DEFAULT_TEMPERATURE_STEP
    temperature_max_retries: int = TEMPERATURE_MAX_RETRIES
    backoff_max_retries: int = BACKOFF_MAX_RETRIES
    thinking_mode: dict = field(default_factory=dict)  # {}=自动过滤, {"thinking":{"type":"disabled"}}=mimo/GLM, {"enable_thinking":false}=DeepSeek/Qwen
    #: 推理档位（low/medium/high/xhigh/max）。与 ``thinking_mode`` 是两件事：
    #: 后者决定"要不要思考"，前者决定"想多深"。网关支持的模型会按档位给不同的
    #: 思考预算——实测 low 与 max 的思考量差约 3 倍，所以长书批量跑通常用 low。
    #: 空 = 不传，由服务端默认决定。
    reasoning_effort: str = ""
    summary_model: str = ""      # 最终总结专用模型；空=跟随 model（复用同一 base_url/api_key，重型任务可切 M3 等）
    summary_thinking_mode: dict = field(default_factory=dict)  # 最终总结专用思考控制；空=跟随 thinking_mode
    provider: str = "auto"       # 协议格式：auto=自动检测 / openai=OpenAI兼容 / anthropic=Anthropic /v1/messages
    streaming_enabled: bool = True  # true=流式 chat_stream_with_retry / false=回退 chat_with_retry


@dataclass
class AnalysisConfig:
    """分析配置"""
    max_arc_length: int = DEFAULT_MAX_ARC_LENGTH
    concurrency: int = DEFAULT_CONCURRENCY
    block_size: int = DEFAULT_BLOCK_SIZE
    encoding_priority: List[str] = field(default_factory=lambda: ENCODING_CANDIDATES.copy())
    max_arcs_in_prompt: int = DEFAULT_MAX_ARCS_IN_PROMPT
    max_summaries_in_prompt: int = DEFAULT_MAX_SUMMARIES_IN_PROMPT
    timeline_truncate: int = DEFAULT_TIMELINE_TRUNCATE
    max_character_states: int = DEFAULT_MAX_CHARACTER_STATES_IN_PROMPT
    max_world_items: int = DEFAULT_MAX_WORLD_ITEMS_IN_PROMPT
    max_foreshadow_entries: int = DEFAULT_MAX_FORESHADOW_ENTRIES_IN_PROMPT
    # 提示词窗口上限（此前 4 项硬编码在 prompt_builder，配置写了也不生效）。
    # 注意与知识库裁剪字段 max_verified_facts 分层：那个管"库里留多少"，
    # 这里的 max_verified_facts_in_prompt 管"构建提示词时取前多少条"。
    max_relationships: int = 15
    max_themes: int = 8
    max_content_chars: int = 30000
    max_verified_facts_in_prompt: int = 15
    # 伏笔分类（治本核心）：50 类功能分类（人物/情节/冲突/关系/设定/主题/题材），
    # 通过 LLM 归一化把 LLM 自由产出的 type 字符串映射到这 50 类之一
    # 默认全启用（用户可取消勾选，但应保留"其他"作为兜底）
    foreshadow_kept_categories: List[str] = field(default_factory=lambda: [name for name, _, _ in FORESHADOW_CATEGORY_DEFS])
    # 最低保留重要度（"高"/"中"/"低"）：filter 阶段丢弃低于该等级的所有伏笔
    foreshadow_min_importance: str = DEFAULT_FORESHADOW_MIN_IMPORTANCE
    # 最低保留置信度（"高"/"中"/"低"）：importance 已收一道，confidence 兜底过滤
    foreshadow_min_confidence: str = DEFAULT_FORESHADOW_MIN_CONFIDENCE
    # 分层上限：importance=高 最多保留多少（-1=无上限）
    max_foreshadow_catalog_high: int = DEFAULT_MAX_FORESHADOW_CATALOG_HIGH
    # 分层上限：importance=中 最多保留多少
    max_foreshadow_catalog_mid: int = DEFAULT_MAX_FORESHADOW_CATALOG_MID
    batch_summary_min_words: int = DEFAULT_BATCH_SUMMARY_MIN_WORDS
    final_report_min_words: int = DEFAULT_FINAL_REPORT_MIN_WORDS
    # 最终报告卷摘要压缩：拼接总字符超过阈值时做轻量分层压缩（首尾各 1 组保留全文，中间组截断到 1/2）
    volume_compress_threshold: int = DEFAULT_VOLUME_COMPRESS_THRESHOLD
    volume_compress_group: int = DEFAULT_VOLUME_COMPRESS_GROUP
    rolling_early_chapters: int = ROLLING_EARLY_CHAPTERS
    rolling_max_milestones: int = ROLLING_MAX_MILESTONES
    rolling_max_momentum: int = ROLLING_MAX_MOMENTUM
    checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL  # 每 N 批自动落盘，0=只在结束时落盘
    auto_archive: bool = False  # 队列分析完成后自动归档整个小说目录到 分析结果/
    # 队列完成后自动总结
    auto_summary: bool = False        # 队列全部书分析完成后，逐本自动执行最终总结
    summary_concurrency: int = 0      # 最终总结并发数（0=跟随主并发 analysis.concurrency，>0 显式覆盖）
    summary_batch_size: int = 30      # 最终总结每批章节数
    # 伏笔复检（V3 / V2）：锚点采集与复检预算
    anchor_quote_enabled: bool = True        # 逐章采集 anchor_quote（V2 需要；V3 可兼容空锚点）
    anchor_min_chars: int = 6                # 锚点最短字数；太短的长句子里必然命中
    recheck_max_calls: int = 1200            # 一次复检最多调用次数（超出标 BUDGET_SKIPPED，可续跑）
    #: 单次复检调用的输出上限。判定回包是 JSON 数组（6 候选约 2k token），32768 足够；
    #: 旧值 60000 会顶到部分上游的输出上限而被拒。
    recheck_max_tokens: int = 32768
    #: 复检并发：0 = 严格跟随 ``analysis.concurrency``；>0 显式覆盖
    recheck_concurrency: int = 0
    #: 复检引擎版本："v3"=多跳深挖与四防对称终审（默认）；"v2"=单跳严格核对
    recheck_version: str = "v3"
    #: V2 复检的批大小（recheck_version="v2" 时生效）
    recheck_v2_batch_size: int = 6
    #: V3 灰区终审开关：False=默认跳过 U4 直接单阶段裁决+U5（省约 33% token）；True=灰区追加 U4 严审
    recheck_v3_gray_u4: bool = False
    #: V3 支线单跳省钱开关：True 时中重要度伏笔缩窗至 3 个并跳过 Hop-2/3，高重要度保留 5 窗 3 跳
    recheck_v3_mid_single_hop: bool = False
    #: 复检专用模型：留空 = 跟随 ``api.model``。分开设是有实测理由的——
    #: 采集要的是"别漏"（覆盖面），判定要的是"判得准"（语义），
    #: 两者对模型的偏好并不一致：实测同一批真实语料，采集用 MiniMax-M3.1
    #: 出的伏笔条数是另一模型的近两倍，而判定质量明显更弱的模型会把
    #: 同一批证据判成"证据不足"。
    recheck_model: str = ""
    #: 复检专用通道：留空 = 跟随 ``api.base_url``。判定模型与通道必须同源——
    #: 网关专用模型名（如 ``codex/gpt-6-luna``）打到官方厂商端点会得到
    #: ``400 unknown model``，而流式路径把它变成空回包，整轮只剩 ``checked=0``。
    recheck_base_url: str = ""
    #: 复检专用 Key：留空 = 跟随 ``api.api_key``。换第三方端点时必配；
    #: 明文落 config.json，与主 ``api_key`` 同现状。
    recheck_api_key: str = ""
    #: 复检专用协议格式：空 = 完全跟随主配置（含主配置显式指定的 anthropic）；
    #: ``auto`` = 按复检通道特征重新检测；``openai`` / ``anthropic`` = 显式指定。
    recheck_provider: str = ""
    #: 复检专用请求超时（秒）：0 = 跟随 ``api.timeout``。判定是单次长 prompt
    #: 请求，独立设值可避免与采集链路互相牵制。
    recheck_timeout: int = 0
    #: 复检专用 JSON 模式：空 = 跟随 ``api.json_mode``。判定模型换家族时需要
    #: 对应的 response_format 提示（default/qwen/deepseek/glm47）。
    recheck_json_mode: str = ""
    #: 复检专用思考档位：空 = 跟随主配置的思考设置（thinking_mode +
    #: reasoning_effort 原样继承）；low/medium/high 显式覆盖档位。
    #: 判定是"读证据下结论"，low 可显著压低耗时与费用。
    recheck_reasoning_effort: str = ""
    # 内容审核拦截处理：识别为审核拦截的章节重试1次后跳过并标记（不进失败集、不补跑）
    skip_moderation_blocked: bool = True
    # 知识库限制
    max_compressed_arcs: int = MAX_COMPRESSED_ARCS
    max_recent_summaries: int = MAX_RECENT_SUMMARIES
    max_pacing_tracker: int = MAX_PACING_TRACKER
    max_foreshadow_network: int = MAX_FORESIGHT_NETWORK
    max_world_building: int = MAX_WORLD_BUILDING
    max_verified_facts: int = MAX_VERIFIED_FACTS
    max_long_term_arcs: int = MAX_LONG_TERM_ARCS
    max_thematic_elements: int = MAX_THEMATIC_ELEMENTS


@dataclass
class GUIConfig:
    """GUI配置（当前仅保留 i18n 预留字段）"""
    language: str = DEFAULT_LANGUAGE


@dataclass
class AppConfig:
    """应用总配置"""
    api: APIConfig = field(default_factory=APIConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    gui: GUIConfig = field(default_factory=GUIConfig)

    # 运行时路径
    working_directory: Optional[str] = None
    knowledge_file: str = KNOWLEDGE_FILE_NAME
    workspace_dir: str = "workspace"  # 工作区目录名（相对于项目根）

    def to_dict(self) -> dict:
        """转换为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'AppConfig':
        """从字典创建配置"""
        api_data = dict(data.get('api')) if isinstance(data.get('api'), dict) else {}
        analysis_data = dict(data.get('analysis')) if isinstance(data.get('analysis'), dict) else {}
        gui_data = dict(data.get('gui')) if isinstance(data.get('gui'), dict) else {}

        # 兼容旧配置：temperature_sequence 已废弃（从未被重试逻辑读取——
        # 温度退火实际使用 temperature/temperature_step 算术递减），忽略并提示
        if 'temperature_sequence' in api_data:
            api_data.pop('temperature_sequence', None)
            logger.warning("config.json 中 api.temperature_sequence 已废弃并被忽略，"
                           "请从配置中删除该字段（温度退火由 temperature/temperature_step 控制）。")

        # 兼容旧配置：移除已退役的字段与实验开关
        for _retired_key in (
            'compress_every',
            'rolling_summary_target_chars',
            'rolling_early_target_chars',
            'rolling_recent_target_chars',
            'recheck_v2_enabled',
            'foreshadow_recheck_batch_size',
            'enable_b_trace',
            'fix_b4_flow_and_storage',
            'fix_b2_evidence_and_recheck',
            'enable_c_on_demand_audit',
            'enable_anchor_b_plus_rule',
        ):
            analysis_data.pop(_retired_key, None)

        if 'recheck_version' in analysis_data:
            ver = str(analysis_data.get('recheck_version') or '').strip().lower()
            analysis_data['recheck_version'] = ver if ver in ('v3', 'v2') else 'v3'

        # 兼容旧配置：thinking_mode 必须是 dict，字符串则清空回默认值
        tm = api_data.get('thinking_mode')
        if tm is not None and not isinstance(tm, dict):
            api_data.pop('thinking_mode', None)

        # 兼容旧配置/非法值：provider 仅接受 auto/openai/anthropic
        if str(api_data.get('provider', 'auto')).lower() not in ('auto', 'openai', 'anthropic'):
            api_data['provider'] = 'auto'

        # 兼容旧配置：model 为空字符串/缺省时回落到默认模型（DEFAULT_MODEL，当前为空
        # = 未配置）。空模型名会在 API 预检查阶段被拦出，要求用户在设置页填写。
        if not str(api_data.get('model', '')).strip():
            api_data.pop('model', None)

        return cls(
            api=APIConfig(**_coerce_fields(APIConfig, api_data)),
            analysis=AnalysisConfig(**_coerce_fields(AnalysisConfig, analysis_data)),
            gui=GUIConfig(**_coerce_fields(GUIConfig, gui_data)),
            working_directory=data.get('working_directory'),
            knowledge_file=data.get('knowledge_file', KNOWLEDGE_FILE_NAME),
            workspace_dir=data.get('workspace_dir', 'workspace'),
        )


def validate_settings_payload(data: Any) -> list[dict]:
    """对 PUT/PATCH /api/settings 的载荷做严格类型与数值范围校验，返回 FastAPI 风格错误列表。"""
    if not isinstance(data, dict):
        return [{"loc": ["body"], "msg": "配置请求体必须为对象"}]

    errors: list[dict] = []
    parsed_nums: dict[tuple[str, str], float] = {}

    def _add_err(loc: list[str], msg: str) -> None:
        errors.append({"loc": loc, "msg": msg})

    def _parse_int(val: Any) -> Optional[int]:
        if isinstance(val, bool):
            return None
        if isinstance(val, int):
            return val
        if isinstance(val, float) and math.isfinite(val) and float(val).is_integer():
            return int(val)
        if isinstance(val, str):
            s = val.strip()
            if re.fullmatch(r"[+-]?\d+", s):
                return int(s)
            if re.fullmatch(r"[+-]?\d+\.0*", s):
                return int(float(s))
        return None

    def _parse_float(val: Any) -> Optional[float]:
        if isinstance(val, bool):
            return None
        if isinstance(val, (int, float)):
            fval = float(val)
            return fval if math.isfinite(fval) else None
        if isinstance(val, str):
            s = val.strip()
            if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)", s):
                fval = float(s)
                return fval if math.isfinite(fval) else None
        return None

    def _is_valid_bool(val: Any) -> bool:
        if isinstance(val, (bool, int, float)):
            return True if isinstance(val, bool) else (math.isfinite(val) if isinstance(val, float) else True)
        if isinstance(val, str):
            return val.strip().lower() in ("true", "false", "1", "0", "yes", "no", "on", "off")
        return False

    section_defs = (("api", APIConfig), ("analysis", AnalysisConfig), ("gui", GUIConfig))
    for sec, dc_cls in section_defs:
        if sec not in data or data[sec] is None:
            continue
        sec_data = data[sec]
        if not isinstance(sec_data, dict):
            _add_err(["body", sec], "必须为对象")
            continue
        for f in dataclasses.fields(dc_cls):
            if f.name not in sec_data:
                continue
            val = sec_data[f.name]
            ftype = f.type
            if ftype is bool:
                if not _is_valid_bool(val):
                    _add_err(["body", sec, f.name], "必须为布尔值")
            elif ftype is int:
                iv = _parse_int(val)
                if iv is None:
                    _add_err(["body", sec, f.name], "必须为整数")
                else:
                    parsed_nums[(sec, f.name)] = float(iv)
            elif ftype is float:
                fv = _parse_float(val)
                if fv is None:
                    _add_err(["body", sec, f.name], "必须为有效数字")
                else:
                    parsed_nums[(sec, f.name)] = fv
            elif ftype is str:
                if not isinstance(val, str):
                    _add_err(["body", sec, f.name], "必须为字符串")
            elif ftype is dict:
                if not isinstance(val, dict):
                    _add_err(["body", sec, f.name], "必须为对象")
            elif ftype is List[str]:
                if not (isinstance(val, list) and all(isinstance(x, str) for x in val)):
                    _add_err(["body", sec, f.name], "必须为字符串数组")

    api_data = data.get("api")
    if isinstance(api_data, dict):
        if "base_url" in api_data and isinstance(api_data["base_url"], str):
            url = api_data["base_url"].strip()
            if url and not url.startswith(("http://", "https://")):
                _add_err(["body", "api", "base_url"], "必须以 http:// 或 https:// 开头")
        for pos_key in ("max_tokens", "timeout", "summary_timeout"):
            if ("api", pos_key) in parsed_nums and parsed_nums[("api", pos_key)] <= 0:
                _add_err(["body", "api", pos_key], "必须大于 0")
        if ("api", "temperature") in parsed_nums:
            t = parsed_nums[("api", "temperature")]
            if t < 0.0 or t > 2.0:
                _add_err(["body", "api", "temperature"], "必须在 0 到 2 之间")
        if ("api", "temperature_step") in parsed_nums and parsed_nums[("api", "temperature_step")] < 0.0:
            _add_err(["body", "api", "temperature_step"], "不能小于 0")
        if ("api", "temperature_max_retries") in parsed_nums and parsed_nums[("api", "temperature_max_retries")] < 1:
            _add_err(["body", "api", "temperature_max_retries"], "必须 >= 1")
        if ("api", "backoff_max_retries") in parsed_nums and parsed_nums[("api", "backoff_max_retries")] < 0:
            _add_err(["body", "api", "backoff_max_retries"], "不能小于 0")

    analysis_data = data.get("analysis")
    if isinstance(analysis_data, dict):
        for gte1_key in (
            "concurrency", "block_size",
            "summary_batch_size", "recheck_max_calls", "recheck_v2_batch_size",
        ):
            if ("analysis", gte1_key) in parsed_nums and parsed_nums[("analysis", gte1_key)] < 1:
                _add_err(["body", "analysis", gte1_key], "必须 >= 1")
        for gte0_key in ("summary_concurrency", "recheck_concurrency", "recheck_timeout", "checkpoint_interval"):
            if ("analysis", gte0_key) in parsed_nums and parsed_nums[("analysis", gte0_key)] < 0:
                _add_err(["body", "analysis", gte0_key], "不能小于 0")
        if "recheck_version" in analysis_data and isinstance(analysis_data["recheck_version"], str):
            rv = analysis_data["recheck_version"].strip().lower()
            if rv and rv not in ("v3", "v2"):
                _add_err(["body", "analysis", "recheck_version"], "只支持 v3 或 v2")

    return errors


class ConfigManager:
    """配置管理器"""

    def __init__(self, config_path: Optional[Path] = None):
        if config_path is None:
            from .paths import runtime_path
            config_path = runtime_path(CONFIG_FILE_NAME)
        self.config_path = config_path
        self.config = AppConfig()
        # config.json 解析失败标志：置位期间 save() 拒绝写入，
        # 防止内存默认配置（空 api_key）原子覆盖掉仍可手工修复的原文件
        self._load_failed = False
        # 标记当前 api_key 是否来自环境变量：True 时 save() 不回写磁盘（防明文落盘）
        self._api_key_from_env = False
        self._loaded_once = False

    def load(self) -> AppConfig:
        """从文件加载配置"""
        if not self.config_path.exists():
            self._loaded_once = True
            return self.config

        try:
            # utf-8-sig：兼容手改/工具保存带入的 UTF-8 BOM（utf-8 会直接抛 JSONDecodeError）
            with open(self.config_path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            self.config = AppConfig.from_dict(data)
            # 成功解析即解除拒绝状态：修复后的文件在下次 load()/重启后可正常保存
            self._load_failed = False
            self._loaded_once = True
        except Exception as e:
            # 配置损坏必须显式告警：静默回退默认配置会导致 API Key/模型全部丢失，
            # 用户只会看到"认证失败"等误导性错误（PowerShell 写文件引入 BOM 是常见诱因）
            self._load_failed = True
            logger.error(f"配置加载失败（{self.config_path}）: {e}，已回退默认配置。"
                         f"请检查 config.json 是否为合法 JSON（注意 UTF-8 BOM/尾逗号），"
                         f"修复后重启即可恢复。")
            return self.config

        # 安全：API Key 优先从环境变量读取，避免明文硬编码落在 config.json。
        # 设置 LLM_API_KEY 或 MINIMAX_API_KEY 即可覆盖；未设置且 config 中也为空时给出明确警告。
        env_key = os.environ.get("LLM_API_KEY") or os.environ.get("MINIMAX_API_KEY")
        self._api_key_from_env = bool(env_key)
        if env_key:
            self.config.api.api_key = env_key
        elif not self.config.api.api_key:
            logger.warning("未配置 API Key：请设置环境变量 LLM_API_KEY（推荐），"
                           "或在 config.json 的 api.api_key 中填入（注意该文件不应提交含密钥的版本）。")

        # 模型未配置提示（与 API Key 提示并列，避免带着空模型名开始分析）
        if not str(self.config.api.model or '').strip():
            logger.warning("未配置模型（api.model 为空）：请在设置页填写你要使用的模型名，"
                           "否则 API 预检查与分析都会失败。")

        # max_tokens 超常见厂商输出上限（64K）时提示——多数 OpenAI 兼容
        # 端点输出上限 4K-64K，配置 130000 会在切换厂商时让所有调用直接报错。
        # 65537 以上才提示：65535/65536（64K-1/64K）本就是多数厂商上限，属正常配置。
        if self.config.api.max_tokens and self.config.api.max_tokens > 65537:
            logger.warning(f"api.max_tokens={self.config.api.max_tokens} 超过多数厂商输出上限(64K)，"
                           f"切换到输出上限较小的模型时可能报错，建议调低到 65536 或 32768。")

        return self.config

    def save(self, config: Optional[AppConfig] = None) -> bool:
        """保存配置到文件"""
        if self._load_failed:
            logger.error("config.json 解析失败尚未修复，拒绝保存以免默认配置覆盖真实数据"
                         "（含 API Key）。请手工修复或删除 config.json 后重试。")
            return False

        if config is not None:
            self.config = config

        try:
            data = self.config.to_dict()
            if self._api_key_from_env:
                # env 来源的 key 不回写磁盘：load() 时 env 仍会覆盖，行为一致，
                # 但避免明文 key 因一次 PUT /api/settings 落盘
                data["api"]["api_key"] = ""
            # 配置必须原子写：直接 open('w') 截断写，在并发保存或写一半崩溃时会破坏 config.json，
            # 下次启动 load() 抛 JSONDecodeError 导致 API Key/模型全部丢失（回退默认配置）。
            # 因此用 safe_save_json（临时文件 + os.replace 原子替换），与队列状态/章节结果一致。
            return safe_save_json(data, self.config_path, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return False

    def patch(self, partial: dict) -> Optional[AppConfig]:
        """局部增量更新配置并原子持久化。损坏配置或保存失败返回 None。"""
        if not self._loaded_once and not self._load_failed:
            self.load()
        if self._load_failed:
            return None

        was_env_key = self._api_key_from_env
        prev_key = self.config.api.api_key

        merged = self.config.to_dict()
        if isinstance(partial, dict):
            for sec in ("api", "analysis", "gui"):
                if isinstance(partial.get(sec), dict):
                    merged[sec] = {**(merged.get(sec) or {}), **partial[sec]}
            for top_key in ("working_directory", "knowledge_file", "workspace_dir"):
                if top_key in partial:
                    merged[top_key] = partial[top_key]

        new_cfg = AppConfig.from_dict(merged)

        api_patch = partial.get("api") if isinstance(partial, dict) else None
        if was_env_key:
            if isinstance(api_patch, dict) and "api_key" in api_patch and api_patch["api_key"] != prev_key:
                self._api_key_from_env = False
            else:
                self._api_key_from_env = True
                new_cfg.api.api_key = prev_key

        if not self.save(new_cfg):
            return None
        return self.config

    def update(self, **kwargs):
        """更新配置项"""
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
