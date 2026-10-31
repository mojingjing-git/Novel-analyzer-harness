"""
配置管理模块
负责加载、保存和验证应用配置
（自旧项目 config/settings.py 移植，新增 workspace_dir + max_* 知识库限制字段）
"""

import json
import logging
import os
import re
import dataclasses
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional

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
    MAX_OUTPUT_TOKENS, DEFAULT_TIMEOUT, DEFAULT_SUMMARY_TIMEOUT, MAX_RETRIES,
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
    DEFAULT_FORESHADOW_RECHECK_BATCH_SIZE,
)


def _coerce_fields(cls, data: dict) -> dict:
    """按 dataclass 字段声明类型清洗并强转配置值（含未知字段过滤）。

    P1 修复（2026-08-24 审计）：PUT /api/settings 曾以裸 dict 直透 dataclass 构造，
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
    json_mode: str = "default"  # default/qwen/deepseek/glm47
    temperature: float = DEFAULT_TEMPERATURE
    temperature_step: float = DEFAULT_TEMPERATURE_STEP
    temperature_max_retries: int = TEMPERATURE_MAX_RETRIES
    backoff_max_retries: int = BACKOFF_MAX_RETRIES
    thinking_mode: dict = field(default_factory=dict)  # {}=自动过滤, {"thinking":{"type":"disabled"}}=mimo/GLM, {"enable_thinking":false}=DeepSeek/Qwen
    summary_model: str = ""      # 最终总结专用模型；空=跟随 model（复用同一 base_url/api_key，重型任务可切 M3 等）
    summary_thinking_mode: dict = field(default_factory=dict)  # 最终总结专用思考控制；空=跟随 thinking_mode
    provider: str = "auto"       # 协议格式：auto=自动检测 / openai=OpenAI兼容 / anthropic=Anthropic /v1/messages
    streaming_enabled: bool = True  # H16：true=流式 chat_stream_with_retry / false=回退 chat_with_retry（旧 API）


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
    # 最终报告卷摘要压缩（QUA-1）：拼接总字符超过阈值时做轻量分层压缩（首尾各 1 组保留全文，中间组截断到 1/2）
    volume_compress_threshold: int = DEFAULT_VOLUME_COMPRESS_THRESHOLD
    volume_compress_group: int = DEFAULT_VOLUME_COMPRESS_GROUP
    rolling_early_chapters: int = ROLLING_EARLY_CHAPTERS
    rolling_max_milestones: int = ROLLING_MAX_MILESTONES
    rolling_max_momentum: int = ROLLING_MAX_MOMENTUM
    checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL  # 每 N 批自动落盘，0=只在结束时落盘
    auto_archive: bool = False  # 队列分析完成后自动归档整个小说目录到 分析结果/
    # 队列完成后自动总结（2026-08-02 spec）
    auto_summary: bool = False        # 队列全部书分析完成后，逐本自动执行最终总结
    summary_concurrency: int = 2      # 最终总结并发数（默认 2，避免高并发触发 API 超时）
    summary_batch_size: int = 30      # 最终总结每批章节数
    foreshadow_recheck_batch_size: int = DEFAULT_FORESHADOW_RECHECK_BATCH_SIZE  # 全书伏笔复检批大小；调大减少调用次数（省 cache 命中价前缀），单批过大可能稀释注意力
    # 伏笔复检 V2（2026-10-31）：锚点采集与复检预算
    anchor_quote_enabled: bool = True        # 逐章采集 anchor_quote（关闭则复检退化为无证据模式）
    anchor_min_chars: int = 6                # 锚点最短字数；太短的长句子里必然命中
    recheck_max_calls: int = 300             # 一次复检最多调用次数（超出标 BUDGET_SKIPPED，可续跑）
    recheck_max_tokens: int = 60000          # 单次复检调用的 max_tokens；给足量才能拿到完整 JSON
    recheck_v2_enabled: bool = False         # 阶段 2 换成带证据的复检（V2）；默认关，真书验证后再打开
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
        api_data = data.get('api', {})
        analysis_data = data.get('analysis', {})
        gui_data = data.get('gui', {})

        # 兼容旧配置：temperature_sequence 已废弃（P1-9，从未被重试逻辑读取——
        # 温度退火实际使用 temperature/temperature_step 算术递减），忽略并提示
        if 'temperature_sequence' in api_data:
            api_data.pop('temperature_sequence', None)
            logger.warning("config.json 中 api.temperature_sequence 已废弃并被忽略，"
                           "请从配置中删除该字段（温度退火由 temperature/temperature_step 控制）。")

        # 兼容旧配置：移除已废弃的字段
        analysis_data.pop('compress_every', None)
        analysis_data.pop('rolling_summary_target_chars', None)
        analysis_data.pop('rolling_early_target_chars', None)
        analysis_data.pop('rolling_recent_target_chars', None)

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


class ConfigManager:
    """配置管理器"""

    def __init__(self, config_path: Optional[Path] = None):
        if config_path is None:
            config_path = Path(CONFIG_FILE_NAME)
        self.config_path = config_path
        self.config = AppConfig()
        # config.json 解析失败标志：置位期间 save() 拒绝写入，
        # 防止内存默认配置（空 api_key）原子覆盖掉仍可手工修复的原文件（P1 2026-08-24）
        self._load_failed = False
        # 标记当前 api_key 是否来自环境变量：True 时 save() 不回写磁盘（防明文落盘）
        self._api_key_from_env = False

    def load(self) -> AppConfig:
        """从文件加载配置"""
        if not self.config_path.exists():
            return self.config

        try:
            # utf-8-sig：兼容手改/工具保存带入的 UTF-8 BOM（utf-8 会直接抛 JSONDecodeError）
            with open(self.config_path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            self.config = AppConfig.from_dict(data)
            # 成功解析即解除拒绝状态：修复后的文件在下次 load()/重启后可正常保存
            self._load_failed = False
        except Exception as e:
            # 配置损坏必须显式告警：静默回退默认配置会导致 API Key/模型全部丢失，
            # 用户只会看到"认证失败"等误导性错误（曾踩：PowerShell 写文件引入 BOM）
            self._load_failed = True
            logger.error(f"配置加载失败（{self.config_path}）: {e}，已回退默认配置。"
                         f"请检查 config.json 是否为合法 JSON（注意 UTF-8 BOM/尾逗号），"
                         f"修复后重启即可恢复。")
            return self.config

        # 安全：API Key 优先从环境变量读取，避免明文硬编码落在 config.json（曾明文提交密钥）。
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

        # P1-9：max_tokens 超常见厂商输出上限（64K）时提示——多数 OpenAI 兼容
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
                # 但避免明文 key 因一次 PUT /api/settings 落盘（MINOR-8）
                data["api"]["api_key"] = ""
            # 配置非原子写修复：原 open('w') 截断写，并发保存或写一半崩溃会破坏 config.json，
            # 下次启动 load() 抛 JSONDecodeError 导致 API Key/模型全部丢失（回退默认配置）。
            # 改用 safe_save_json（临时文件 + os.replace 原子替换），与队列状态/章节结果一致。
            return safe_save_json(data, self.config_path, ensure_ascii=False)
        except Exception as e:
            print(f"保存配置失败: {e}")
            return False

    def update(self, **kwargs):
        """更新配置项"""
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
