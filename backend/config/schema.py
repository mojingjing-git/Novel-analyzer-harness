"""全量配置元数据单一真源：字段定义、分组、校验约束、CLI 类型解析与默认值表。

仅依赖标准库与 ``.constants``，不引用 ``settings.py``，避免循环导入并保持对
``test_smoke.py`` 分层与导入零副作用约束的完全兼容。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import difflib
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .constants import (
    BACKOFF_MAX_RETRIES,
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    DEFAULT_BATCH_SUMMARY_MIN_WORDS,
    DEFAULT_BLOCK_SIZE,
    DEFAULT_CHECKPOINT_INTERVAL,
    DEFAULT_CONCURRENCY,
    DEFAULT_FINAL_REPORT_MIN_WORDS,
    DEFAULT_FORESHADOW_MIN_CONFIDENCE,
    DEFAULT_FORESHADOW_MIN_IMPORTANCE,
    DEFAULT_LANGUAGE,
    DEFAULT_MAX_ARC_LENGTH,
    DEFAULT_MAX_ARCS_IN_PROMPT,
    DEFAULT_MAX_CHARACTER_STATES_IN_PROMPT,
    DEFAULT_MAX_FORESHADOW_CATALOG_HIGH,
    DEFAULT_MAX_FORESHADOW_CATALOG_MID,
    DEFAULT_MAX_FORESHADOW_ENTRIES_IN_PROMPT,
    DEFAULT_MAX_SUMMARIES_IN_PROMPT,
    DEFAULT_MAX_WORLD_ITEMS_IN_PROMPT,
    DEFAULT_MODEL,
    DEFAULT_SUMMARY_TIMEOUT,
    DEFAULT_TEMPERATURE,
    DEFAULT_TEMPERATURE_STEP,
    DEFAULT_TIMEOUT,
    DEFAULT_TIMELINE_TRUNCATE,
    DEFAULT_VOLUME_COMPRESS_GROUP,
    DEFAULT_VOLUME_COMPRESS_THRESHOLD,
    ENCODING_CANDIDATES,
    FORESHADOW_CATEGORY_DEFS,
    KNOWLEDGE_FILE_NAME,
    MAX_COMPRESSED_ARCS,
    MAX_FORESIGHT_NETWORK,
    MAX_KB_CHARACTER_RELATIONSHIPS,
    MAX_KB_CHARACTER_STATES,
    MAX_LONG_TERM_ARCS,
    MAX_OUTPUT_TOKENS,
    MAX_PACING_TRACKER,
    MAX_RECENT_SUMMARIES,
    MAX_RETRIES,
    MAX_THEMATIC_ELEMENTS,
    MAX_VERIFIED_FACTS,
    MAX_WORLD_BUILDING,
    ROLLING_ARCHIVE_TRIGGER_COUNT,
    ROLLING_EARLY_CHAPTERS,
    ROLLING_MAX_MILESTONES,
    ROLLING_MAX_MOMENTUM,
    ROLLING_MOMENTUM_WINDOW,
    SDK_MAX_RETRIES,
    TEMPERATURE_MAX_RETRIES,
)

DEFAULT_FORESHADOW_CATEGORIES: Tuple[str, ...] = tuple(
    name for name, _, _ in FORESHADOW_CATEGORY_DEFS
)
DEFAULT_ENCODING_PRIORITY: Tuple[str, ...] = tuple(ENCODING_CANDIDATES)


@dataclass(frozen=True)
class ConfigGroupMeta:
    """配置分组元数据，同时驱动 GUI 渲染与 CLI `config schema` 展示。"""

    id: str
    title: str
    description: str
    ui_placement: str  # "core" | "recheck_endpoint" | "advanced"


@dataclass(frozen=True)
class ConfigFieldSpec:
    """单个配置项的元信息与校验边界。"""

    path: str
    section: str  # "api" | "analysis" | "gui" | "root"
    key: str
    field_type: str  # "str" | "optional_str" | "int" | "float" | "bool" | "dict" | "list_str"
    default: Any
    group: str
    label: str
    description: str
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    step: Optional[float] = None
    choices: Tuple[str, ...] = ()
    sensitive: bool = False
    is_url: bool = False
    status: str = "active"  # "active" | "legacy" | "reserved"
    ui_placement: str = "advanced"  # "core" | "recheck_endpoint" | "advanced"


CONFIG_GROUPS: Tuple[ConfigGroupMeta, ...] = (
    ConfigGroupMeta(
        id="api_connection",
        title="主 API 连接",
        description="主模型端点地址、密钥与协议格式。",
        ui_placement="core",
    ),
    ConfigGroupMeta(
        id="api_models",
        title="模型调用与并发",
        description="分析、总结与复检各阶段模型、思考档位及并发数。",
        ui_placement="core",
    ),
    ConfigGroupMeta(
        id="strategy_core",
        title="运行与流程策略",
        description="分块粒度、总结批次、复检引擎开关与队列自动化。",
        ui_placement="core",
    ),
    ConfigGroupMeta(
        id="recheck_channel",
        title="复检独立端点",
        description="为 Stage 2 伏笔复检单独覆盖 Base URL、API Key、协议与 JSON 模式。",
        ui_placement="recheck_endpoint",
    ),
    ConfigGroupMeta(
        id="api_advanced",
        title="请求超时、重试、退避与熔断（API 进阶）",
        description="输出上限、超时、温度退火步长、429/指数退避等待及账户级熔断策略。",
        ui_placement="advanced",
    ),
    ConfigGroupMeta(
        id="analysis_prompt_rolling",
        title="逐章分析上下文与滚动总结预算",
        description="逐章提示词注入窗口、原文字符上限、锚点开关、断点间隔与滚动摘要预算。",
        ui_placement="advanced",
    ),
    ConfigGroupMeta(
        id="kb_limits",
        title="知识库容量上限（KB 裁剪）",
        description="内存与磁盘知识库各类别落盘前的最大保留条目上限。",
        ui_placement="advanced",
    ),
    ConfigGroupMeta(
        id="summary_style_atlas",
        title="最终总结、图谱与写作风格预算",
        description="卷摘要压缩、伏笔总表过滤/休眠阈值、全书图谱与写作风格采样参数。",
        ui_placement="advanced",
    ),
    ConfigGroupMeta(
        id="recheck_tuning",
        title="伏笔复检引擎调优（V3 / V2 全参数）",
        description="面向不同上下文窗口（128K/256K/512K）调节共享证据池、跳数、窗口数与 V2 核对参数。",
        ui_placement="advanced",
    ),
    ConfigGroupMeta(
        id="foreshadow_categories",
        title="伏笔分类（50 类功能类别）",
        description="筛选最终总结伏笔目录时保留的功能类别白名单。",
        ui_placement="advanced",
    ),
    ConfigGroupMeta(
        id="runtime_misc",
        title="运行路径、编码优先级与界面预留",
        description="工作区目录、知识库文件名、文本解码优先级及界面语言预留。",
        ui_placement="advanced",
    ),
)

_PROVIDER_CHOICES = ("auto", "openai", "anthropic")
_OPT_PROVIDER_CHOICES = ("", "auto", "openai", "anthropic")
_JSON_MODE_CHOICES = ("default", "qwen", "deepseek", "glm47")
_OPT_JSON_MODE_CHOICES = ("", "default", "qwen", "deepseek", "glm47")
_EFFORT_CHOICES = ("", "low", "medium", "high", "xhigh", "max")
_LEVEL_CHOICES = ("高", "中", "低")


def _api(
    key: str,
    ftype: str,
    default: Any,
    group: str,
    label: str,
    desc: str,
    *,
    ui_placement: str = "advanced",
    **kw: Any,
) -> ConfigFieldSpec:
    return ConfigFieldSpec(
        path=f"api.{key}",
        section="api",
        key=key,
        field_type=ftype,
        default=default,
        group=group,
        label=label,
        description=desc,
        ui_placement=ui_placement,
        **kw,
    )


def _ana(
    key: str,
    ftype: str,
    default: Any,
    group: str,
    label: str,
    desc: str,
    *,
    ui_placement: str = "advanced",
    **kw: Any,
) -> ConfigFieldSpec:
    return ConfigFieldSpec(
        path=f"analysis.{key}",
        section="analysis",
        key=key,
        field_type=ftype,
        default=default,
        group=group,
        label=label,
        description=desc,
        ui_placement=ui_placement,
        **kw,
    )


CONFIG_FIELD_SPECS: Tuple[ConfigFieldSpec, ...] = (
    # ==================== 1. 主 API 连接 (3) ====================
    _api(
        "base_url", "str", DEFAULT_BASE_URL, "api_connection", "Base URL",
        "OpenAI 兼容或 Anthropic 接口根地址，支持本地网关或厂商直连。",
        ui_placement="core", is_url=True,
    ),
    _api(
        "api_key", "str", DEFAULT_API_KEY, "api_connection", "API Key",
        "主模型 API 密钥；配置环境变量 LLM_API_KEY 时优先级更高且不覆盖磁盘。",
        ui_placement="core", sensitive=True,
    ),
    _api(
        "provider", "str", "auto", "api_connection", "协议格式",
        "请求协议：auto（按端点与模型名自动识别）、openai 或 anthropic。",
        ui_placement="core", choices=_PROVIDER_CHOICES,
    ),
    # ==================== 2. 模型调用与并发 (9) ====================
    _api(
        "model", "str", DEFAULT_MODEL, "api_models", "主模型",
        "逐章分析与滚动总结使用的主模型名称。",
        ui_placement="core",
    ),
    _api(
        "thinking_mode", "dict", {}, "api_models", "主模型思考控制",
        "传给主模型请求的额外思考控制对象（如 reasoning_effort 或 thinking）。",
        ui_placement="core",
    ),
    _ana(
        "concurrency", "int", DEFAULT_CONCURRENCY, "api_models", "分析并发",
        "逐章分析并发 worker 数；summary/recheck 并发设 0 时同样跟随该值。",
        ui_placement="core", min_val=1, max_val=64,
    ),
    _api(
        "summary_model", "str", "", "api_models", "总结模型",
        "最终总结专用模型；留空则复用主模型。",
        ui_placement="core",
    ),
    _api(
        "summary_thinking_mode", "dict", {}, "api_models", "总结思考控制",
        "最终总结专用思考控制对象；为空时沿用主模型设置。",
        ui_placement="core",
    ),
    _ana(
        "summary_concurrency", "int", 0, "api_models", "总结并发 (0=跟随)",
        "最终总结卷摘要与对账并发数；0 表示跟随主分析并发。",
        ui_placement="core", min_val=0, max_val=64,
    ),
    _ana(
        "recheck_model", "str", "", "api_models", "复检模型",
        "伏笔复检专用模型；留空则跟随主模型。",
        ui_placement="core",
    ),
    _ana(
        "recheck_reasoning_effort", "str", "", "api_models", "复检思考档位",
        "复检推理深度（low/medium/high 等）；留空继承主配置，low 常能大幅降低耗时。",
        ui_placement="core", choices=_EFFORT_CHOICES,
    ),
    _ana(
        "recheck_concurrency", "int", 0, "api_models", "复检并发 (0=跟随)",
        "Stage 2 伏笔复检并发数；0 表示跟随 analysis.concurrency。",
        ui_placement="core", min_val=0, max_val=64,
    ),
    # ==================== 3. 运行与流程策略 (9) ====================
    _ana(
        "block_size", "int", DEFAULT_BLOCK_SIZE, "strategy_core", "批大小（章/批）",
        "单次 LLM 请求最多打包的章节数；请求内逐章独立输出、落盘统一为单章文件，"
        "中途修改不影响断点续跑；动态装包同时受正文字符上限与模型输出预算约束。",
        ui_placement="core", min_val=1, max_val=200,
    ),
    _ana(
        "summary_batch_size", "int", 30, "strategy_core", "总结批次大小（章/卷）",
        "最终总结 Stage 1 每批次处理的分析块上限。",
        ui_placement="core", min_val=1,
    ),
    _ana(
        "recheck_version", "str", "v3", "strategy_core", "复检引擎版本",
        "Stage 2 伏笔复检引擎：v3（共享证据池多跳）或 v2（单跳窗口）。",
        ui_placement="core", choices=("v3", "v2"),
    ),
    _ana(
        "recheck_max_calls", "int", 1200, "strategy_core", "复检调用上限",
        "单轮伏笔复检允许的最多 LLM 调用次数，超出则标 BUDGET_SKIPPED。",
        ui_placement="core", min_val=1,
    ),
    _ana(
        "recheck_v3_gray_u4", "bool", False, "strategy_core", "V3 灰区终审 U4",
        "开启后对 U3 未明朗或低把握条目额外触发 U4 四防终审，准确率更高但约多耗 33% token。",
        ui_placement="core",
    ),
    _ana(
        "recheck_v3_mid_single_hop", "bool", False, "strategy_core", "V3 中重要度单跳省钱",
        "开启后中重要度伏笔仅保留少窗口并跳过 Hop-2/3，高重要度仍完整深挖。",
        ui_placement="core",
    ),
    _ana(
        "auto_archive", "bool", False, "strategy_core", "自动归档",
        "分析完成后自动将整本书目录归档到 workspace/分析结果/。",
        ui_placement="core",
    ),
    _ana(
        "auto_summary", "bool", False, "strategy_core", "队列完成后自动总结",
        "分析队列全部完成后自动触发全书最终总结。",
        ui_placement="core",
    ),
    _ana(
        "skip_moderation_blocked", "bool", True, "strategy_core", "跳过内容审核拦截章节",
        "触发审核拦截重试一次后跳过并隔离该块，不纳入失败补跑集。",
        ui_placement="core",
    ),
    # ==================== 4. 复检独立端点 (4) ====================
    _ana(
        "recheck_base_url", "str", "", "recheck_channel", "复检 Base URL",
        "复检专用接口地址；留空继承主 api.base_url。",
        ui_placement="recheck_endpoint", is_url=True,
    ),
    _ana(
        "recheck_api_key", "str", "", "recheck_channel", "复检 API Key",
        "复检专用密钥；留空继承主 api.api_key。",
        ui_placement="recheck_endpoint", sensitive=True,
    ),
    _ana(
        "recheck_provider", "str", "", "recheck_channel", "复检协议格式",
        "复检专用协议；留空完全跟随主配置。",
        ui_placement="recheck_endpoint", choices=_OPT_PROVIDER_CHOICES,
    ),
    _ana(
        "recheck_json_mode", "str", "", "recheck_channel", "复检 JSON 模式",
        "复检专用结构化输出模式；留空跟随主配置。",
        ui_placement="recheck_endpoint", choices=_OPT_JSON_MODE_CHOICES,
    ),
    # ==================== 5. API 进阶：超时、重试、退避与熔断 (20) ====================
    _api(
        "max_tokens", "int", MAX_OUTPUT_TOKENS, "api_advanced", "主通道 max_tokens",
        "逐章分析与最终总结单次请求的输出 token 上限；部分厂商上限为 65536。",
        min_val=1,
    ),
    _api(
        "timeout", "int", DEFAULT_TIMEOUT, "api_advanced", "单块请求超时（秒）",
        "逐章分析与普通 LLM 请求超时时间。",
        min_val=1,
    ),
    _api(
        "summary_timeout", "int", DEFAULT_SUMMARY_TIMEOUT, "api_advanced", "总结阶段超时（秒）",
        "最终总结、复检与图谱抽取请求超时时间。",
        min_val=1,
    ),
    _api(
        "json_mode", "str", "default", "api_advanced", "JSON 模式",
        "OpenAI 兼容端点的 JSON response_format 风格（default/qwen/deepseek/glm47）。",
        choices=_JSON_MODE_CHOICES,
    ),
    _api(
        "temperature", "float", DEFAULT_TEMPERATURE, "api_advanced", "初始采样温度",
        "主请求起始 temperature，失败退火时按 temperature_step 逐次下降。",
        min_val=0.0, max_val=2.0, step=0.01,
    ),
    _api(
        "temperature_step", "float", DEFAULT_TEMPERATURE_STEP, "api_advanced", "温度退火步长",
        "温度退火阶段每次重试降低的 temperature 幅度。",
        min_val=0.0, max_val=2.0, step=0.01,
    ),
    _api(
        "temperature_max_retries", "int", TEMPERATURE_MAX_RETRIES, "api_advanced", "温度退火重试上限",
        "JSON 解析失败或空回包时温度退火阶段的最多尝试次数。",
        min_val=1,
    ),
    _api(
        "backoff_max_retries", "int", BACKOFF_MAX_RETRIES, "api_advanced", "指数退避重试次数",
        "网络异常或限流阶段在温度退火之后追加的重试次数。",
        min_val=0,
    ),
    _api(
        "sdk_max_retries", "int", SDK_MAX_RETRIES, "api_advanced", "SDK 底层隐式重试",
        "AsyncOpenAI / AsyncAnthropic 客户端自带重试次数；建议保持 0 以免绕过上层记账。",
        min_val=0,
    ),
    _api(
        "reasoning_effort", "str", "", "api_advanced", "主通道默认推理档位",
        "OpenAI 路径默认 reasoning_effort；当 api.thinking_mode 已含 reasoning_effort 时以后者为准。",
        choices=_EFFORT_CHOICES,
    ),
    _api(
        "streaming_enabled", "bool", True, "api_advanced", "启用流式响应",
        "启用流式 chunk 读取并推送实时生成速率；关闭后走一次性 HTTP 响应。",
    ),
    _api(
        "temp_retry_wait_base", "int", 3, "api_advanced", "退火重试基础等待（秒）",
        "非 429 温度退火重试的线性等待基数（秒 × (attempt + 1)）。",
        min_val=0,
    ),
    _api(
        "rate_limit_wait_base", "int", 30, "api_advanced", "429 限流退避基数（秒）",
        "响应缺少 Retry-After 时用于 429 等待的基础秒数（乘以退避次数）。",
        min_val=0,
    ),
    _api(
        "rate_limit_wait_cap", "int", 120, "api_advanced", "429 最大等待上限（秒）",
        "单次 429 退避等待时间的封顶秒数（同时约束 Retry-After 与线性等待）。",
        min_val=1,
    ),
    _api(
        "rate_limit_min_backoff_retries", "int", 3, "api_advanced", "429 最少退避次数",
        "当 backoff_max_retries > 0 且遇到 429 时保底执行的退避次数。",
        min_val=0,
    ),
    _api(
        "backoff_wait_cap", "int", 60, "api_advanced", "普通退避等待上限（秒）",
        "非 429 指数退避等待时间的封顶秒数。",
        min_val=1,
    ),
    _api(
        "hard_timeout_margin", "int", 30, "api_advanced", "外层硬超时余量（秒）",
        "asyncio.wait_for 在 timeout 基础上追加的硬中断宽限秒数。",
        min_val=0,
    ),
    _api(
        "moderation_short_circuit_hits", "int", 2, "api_advanced", "内容审核短路阈值",
        "连续命中内容审核特征文本达到该次数后在下一跳立即短路跳过该块。",
        min_val=1,
    ),
    _api(
        "breaker_open_after", "int", 1, "api_advanced", "账户熔断阈值",
        "连续出现结构化欠费/鉴权等不可恢复错误的次数达到该阈值后触发全局熔断。",
        min_val=1,
    ),
    _api(
        "max_retries", "int", MAX_RETRIES, "api_advanced", "日志重试总数标注",
        "兼容遗留字段：用于失败日志展示“尝试 n/m”，设为 0 时同时关闭 SDK 隐式重试。",
        min_val=0, status="legacy",
    ),
    # ==================== 6. 逐章分析上下文与滚动总结预算 (27) ====================
    _ana(
        "max_content_chars", "int", 30000, "analysis_prompt_rolling", "正文截断字符上限",
        "单块正文注入逐章分析提示词时的最大字符数；超长块会保留尾部并截断前段。",
        min_val=1,
    ),
    _ana(
        "max_arcs_in_prompt", "int", DEFAULT_MAX_ARCS_IN_PROMPT, "analysis_prompt_rolling", "提示词压缩主线上限",
        "构建逐章分析提示词时携带的 compressed_arcs 条数上限。",
        min_val=1,
    ),
    _ana(
        "max_summaries_in_prompt", "int", DEFAULT_MAX_SUMMARIES_IN_PROMPT, "analysis_prompt_rolling", "提示词前情摘要上限",
        "提示词中携带的最近逐章摘要条数上限。",
        min_val=1,
    ),
    _ana(
        "timeline_truncate", "int", DEFAULT_TIMELINE_TRUNCATE, "analysis_prompt_rolling", "摘要时间线截断字数",
        "每条近期摘要附带时间线说明时的字符截断长度。",
        min_val=20,
    ),
    _ana(
        "max_character_states", "int", DEFAULT_MAX_CHARACTER_STATES_IN_PROMPT, "analysis_prompt_rolling", "提示词人物状态上限",
        "逐章分析提示词中注入的人物状态条目上限。",
        min_val=1,
    ),
    _ana(
        "max_world_items", "int", DEFAULT_MAX_WORLD_ITEMS_IN_PROMPT, "analysis_prompt_rolling", "提示词世界设定上限",
        "逐章分析提示词中注入的世界观条目上限。",
        min_val=1,
    ),
    _ana(
        "max_foreshadow_entries", "int", DEFAULT_MAX_FORESHADOW_ENTRIES_IN_PROMPT, "analysis_prompt_rolling", "提示词伏笔状态上限",
        "逐章分析提示词中注入的未闭环伏笔网络条目上限。",
        min_val=1,
    ),
    _ana(
        "max_relationships", "int", 15, "analysis_prompt_rolling", "提示词人物关系上限",
        "逐章分析提示词中注入的人物关系边上限。",
        min_val=1,
    ),
    _ana(
        "max_themes", "int", 8, "analysis_prompt_rolling", "提示词主题元素上限",
        "逐章分析提示词中注入的主题元素上限。",
        min_val=1,
    ),
    _ana(
        "max_verified_facts_in_prompt", "int", 15, "analysis_prompt_rolling", "提示词已核实事实上限",
        "逐章分析提示词中注入的 verified_facts 条数上限。",
        min_val=1,
    ),
    _ana(
        "anchor_quote_enabled", "bool", True, "analysis_prompt_rolling", "采集原文锚点引文",
        "逐章采集伏笔时是否要求输出正文逐字 anchor_quote（供复检精确定位）。",
    ),
    _ana(
        "anchor_min_chars", "int", 6, "analysis_prompt_rolling", "锚点最短字数",
        "逐章采集筛选 anchor_quote 的最小字数门槛。",
        min_val=1,
    ),
    _ana(
        "checkpoint_interval", "int", DEFAULT_CHECKPOINT_INTERVAL, "analysis_prompt_rolling", "分析断点保存间隔（章）",
        "每完成 N 章落盘一次内存知识库快照，0 表示仅在任务结束落盘。",
        min_val=0,
    ),
    _ana(
        "failed_block_retry_rounds", "int", 3, "analysis_prompt_rolling", "失败块自动补跑轮数",
        "主并发完成后对可重试失败块执行的串行补跑轮次上限。",
        min_val=0,
    ),
    _ana(
        "rolling_early_chapters", "int", ROLLING_EARLY_CHAPTERS, "analysis_prompt_rolling", "滚动摘要首触发章数",
        "累计分析章节达到该阈值时首次初始化结构化滚动摘要。",
        min_val=1,
    ),
    _ana(
        "rolling_update_interval", "int", 0, "analysis_prompt_rolling", "滚动摘要更新步长 (0=跟随并发)",
        "每隔多少批执行一次滚动摘要增量更新；0 表示跟随 analysis.concurrency。",
        min_val=0,
    ),
    _ana(
        "rolling_summary_max_tokens", "int", 0, "analysis_prompt_rolling", "滚动摘要输出上限 (0=不限)",
        "滚动摘要调用的 max_tokens；0 表示不传递限制让模型自然结束。",
        min_val=0,
    ),
    _ana(
        "rolling_summary_max_retries", "int", 1, "analysis_prompt_rolling", "滚动摘要重试次数",
        "滚动摘要单次更新失败后额外重试次数。",
        min_val=0,
    ),
    _ana(
        "rolling_summary_temperature", "float", 0.2, "analysis_prompt_rolling", "滚动摘要温度",
        "滚动摘要 LLM 调用的 temperature。",
        min_val=0.0, max_val=2.0, step=0.05,
    ),
    _ana(
        "rolling_max_milestones", "int", ROLLING_MAX_MILESTONES, "analysis_prompt_rolling", "全局里程碑上限",
        "滚动摘要保留 global_milestones 条数上限（先进先出淘汰）。",
        min_val=1,
    ),
    _ana(
        "rolling_max_momentum", "int", ROLLING_MAX_MOMENTUM, "analysis_prompt_rolling", "近期动态保留上限",
        "滚动摘要保留与注入提示词的 recent_momentum 条数上限。",
        min_val=1,
    ),
    _ana(
        "rolling_momentum_window", "int", ROLLING_MOMENTUM_WINDOW, "analysis_prompt_rolling", "近期动态归档章窗",
        "recent_momentum 最老与最新章距达到该值时触发里程碑压缩归档。",
        min_val=1,
    ),
    _ana(
        "rolling_archive_trigger_count", "int", ROLLING_ARCHIVE_TRIGGER_COUNT, "analysis_prompt_rolling", "近期动态归档条数阈值",
        "recent_momentum 超出最大条数且达到此待归档量时触发 LLM 归档压缩。",
        min_val=1,
    ),
    _ana(
        "rolling_max_paradigm_layers", "int", 5, "analysis_prompt_rolling", "范式层级保留上限",
        "滚动摘要中保留的历史世界观/力量体系范式层级条数上限。",
        min_val=1,
    ),
    _ana(
        "rolling_max_causal_chains", "int", 5, "analysis_prompt_rolling", "因果链保留上限",
        "滚动摘要 active_causal_chains 最多保留条数。",
        min_val=1,
    ),
    _ana(
        "rolling_archive_max_tokens", "int", 100, "analysis_prompt_rolling", "里程碑归档输出上限",
        "近期动态压缩归档为单句里程碑时的 max_tokens；推理模型建议调高。",
        min_val=1,
    ),
    _ana(
        "max_arc_length", "int", DEFAULT_MAX_ARC_LENGTH, "analysis_prompt_rolling", "主线长度字段（兼容保留）",
        "历史保留字段，当前提示词按条目数而非单条字数裁剪。",
        min_val=1, status="legacy",
    ),
    # ==================== 7. 知识库容量上限 (10) ====================
    _ana(
        "max_compressed_arcs", "int", MAX_COMPRESSED_ARCS, "kb_limits", "KB 压缩主线容量",
        "知识库落盘时保留的 compressed_arcs 最大条数。",
        min_val=1,
    ),
    _ana(
        "max_recent_summaries", "int", MAX_RECENT_SUMMARIES, "kb_limits", "KB 近期摘要容量",
        "知识库落盘时保留的 recent_summaries 最大条数。",
        min_val=1,
    ),
    _ana(
        "max_foreshadow_network", "int", MAX_FORESIGHT_NETWORK, "kb_limits", "KB 伏笔网络容量",
        "知识库落盘时保留的 foreshadowing_network 最大条数。",
        min_val=1,
    ),
    _ana(
        "max_world_building", "int", MAX_WORLD_BUILDING, "kb_limits", "KB 世界观词条容量",
        "知识库落盘时保留的 world_building 最大条数。",
        min_val=1,
    ),
    _ana(
        "max_verified_facts", "int", MAX_VERIFIED_FACTS, "kb_limits", "KB 已核实事实容量",
        "知识库落盘时保留的 verified_facts 最大条数。",
        min_val=1,
    ),
    _ana(
        "max_thematic_elements", "int", MAX_THEMATIC_ELEMENTS, "kb_limits", "KB 主题元素容量",
        "知识库落盘时保留的 thematic_elements 最大条数。",
        min_val=1,
    ),
    _ana(
        "max_kb_character_states", "int", MAX_KB_CHARACTER_STATES, "kb_limits", "KB 人物状态容量",
        "知识库落盘时按最近活跃保留的最大人物状态数。",
        min_val=1,
    ),
    _ana(
        "max_kb_character_relationships", "int", MAX_KB_CHARACTER_RELATIONSHIPS, "kb_limits", "KB 人物关系容量",
        "知识库落盘时按最近出现保留的最大人物关系边数。",
        min_val=1,
    ),
    _ana(
        "max_long_term_arcs", "int", MAX_LONG_TERM_ARCS, "kb_limits", "KB 长线剧情容量（旧 Schema）",
        "兼容旧知识库格式的 long_term_arcs 上限。",
        min_val=1, status="legacy",
    ),
    _ana(
        "max_pacing_tracker", "int", MAX_PACING_TRACKER, "kb_limits", "KB 节奏追踪容量（兼容保留）",
        "历史保留字段，当前 KnowledgeBase 无 pacing_tracker 存储。",
        min_val=1, status="legacy",
    ),
    # ==================== 8. 最终总结、图谱与写作风格预算 (20) ====================
    _ana(
        "batch_summary_min_words", "int", DEFAULT_BATCH_SUMMARY_MIN_WORDS, "summary_style_atlas", "分卷摘要建议字数",
        "提示最终总结 Stage 1 每卷摘要期望达到的最低字数。",
        min_val=0,
    ),
    _ana(
        "final_report_min_words", "int", DEFAULT_FINAL_REPORT_MIN_WORDS, "summary_style_atlas", "最终报告建议字数",
        "提示 Stage 4 全书脉络报告期望达到的最低字数。",
        min_val=0,
    ),
    _ana(
        "volume_compress_threshold", "int", DEFAULT_VOLUME_COMPRESS_THRESHOLD, "summary_style_atlas", "卷摘要压缩字符阈值",
        "拼接后的全书卷摘要超出此字符数时触发分段截断压缩。",
        min_val=1000,
    ),
    _ana(
        "volume_compress_group", "int", DEFAULT_VOLUME_COMPRESS_GROUP, "summary_style_atlas", "卷摘要压缩组大小",
        "触发压缩时首尾各保留的完整卷摘要组数（兼作分组单位）。",
        min_val=1,
    ),
    _ana(
        "volume_compress_ratio", "float", 0.5, "summary_style_atlas", "中间卷压缩保留比例",
        "超出压缩阈值时中间卷摘要截取前部字符的比例（0.1–1.0）。",
        min_val=0.1, max_val=1.0, step=0.05,
    ),
    _ana(
        "foreshadow_min_importance", "str", DEFAULT_FORESHADOW_MIN_IMPORTANCE, "summary_style_atlas", "伏笔最低重要度",
        "最终总结整理伏笔总表时允许进入候选的最低重要度等级。",
        choices=_LEVEL_CHOICES,
    ),
    _ana(
        "foreshadow_min_confidence", "str", DEFAULT_FORESHADOW_MIN_CONFIDENCE, "summary_style_atlas", "伏笔最低置信度",
        "最终总结整理伏笔总表时的最低置信度筛选门槛。",
        choices=_LEVEL_CHOICES,
    ),
    _ana(
        "max_foreshadow_catalog_high", "int", DEFAULT_MAX_FORESHADOW_CATALOG_HIGH, "summary_style_atlas", "高重要度伏笔上限 (-1=不限)",
        "伏笔总表保留高重要度条目的最大数量，-1 表示不限制。",
        min_val=-1,
    ),
    _ana(
        "max_foreshadow_catalog_mid", "int", DEFAULT_MAX_FORESHADOW_CATALOG_MID, "summary_style_atlas", "中重要度伏笔上限 (-1=不限)",
        "伏笔总表保留中重要度条目的最大数量，-1 表示不限制。",
        min_val=-1,
    ),
    _ana(
        "dormant_chapter_threshold", "int", 200, "summary_style_atlas", "伏笔休眠章距阈值",
        "未回收且非待确认伏笔超过多少章未提及后自动标记为 dormant。",
        min_val=1,
    ),
    _ana(
        "foreshadow_min_clue_chars", "int", 15, "summary_style_atlas", "伏笔线索最小字数",
        "构建伏笔总表前丢弃过短无信息线索字符数的门槛。",
        min_val=1,
    ),
    _ana(
        "foreshadow_dedup_keyword_overlap", "int", 2, "summary_style_atlas", "伏笔去重最小重叠词数",
        "总表跨章去重候选需满足的最少共同关键词数。",
        min_val=1,
    ),
    _ana(
        "foreshadow_dedup_similarity", "float", 0.6, "summary_style_atlas", "伏笔去重相似度阈值",
        "跨章伏笔合并判定的文本综合相似度阈值（0.0–1.0）。",
        min_val=0.0, max_val=1.0, step=0.05,
    ),
    _ana(
        "atlas_max_tokens", "int", 32768, "summary_style_atlas", "全书图谱输出上限",
        "Stage 4 全书人物谱系与多世界树抽取请求使用的 max_tokens。",
        min_val=1024,
    ),
    _ana(
        "atlas_max_characters", "int", 60, "summary_style_atlas", "图谱人物锚点 Top-N",
        "送入人物关系图谱抽取提示词的确定性核心人物数量上限。",
        min_val=1,
    ),
    _ana(
        "atlas_max_locations", "int", 50, "summary_style_atlas", "图谱地点锚点 Top-N",
        "送入多世界树抽取提示词的确定性地点数量上限。",
        min_val=1,
    ),
    _ana(
        "style_sample_chapters", "int", 6, "summary_style_atlas", "风格分析采样章数",
        "Stage 3 语义文风抽取时前后均衡采样的章节数。",
        min_val=1,
    ),
    _ana(
        "style_sample_chars", "int", 1500, "summary_style_atlas", "风格分析每章截取字数",
        "送入语义文风提示词的每章正文截取长度。",
        min_val=100,
    ),
    _ana(
        "style_max_tokens", "int", 20000, "summary_style_atlas", "风格分析输出上限",
        "Stage 3 写作风格语义抽取 LLM 请求的 max_tokens。",
        min_val=512,
    ),
    _ana(
        "style_temperature", "float", 0.3, "summary_style_atlas", "风格分析采样温度",
        "Stage 3 写作风格语义抽取 LLM 请求的 temperature。",
        min_val=0.0, max_val=2.0, step=0.05,
    ),
    # ==================== 9. 伏笔复检引擎调优（V3 / V2 全参数） (23) ====================
    _ana(
        "recheck_max_tokens", "int", 32768, "recheck_tuning", "复检单次输出上限",
        "Stage 2 复检单次请求 max_tokens；在 auto 模式下低于 16384 会自动切换紧凑模式。",
        min_val=256,
    ),
    _ana(
        "recheck_timeout", "int", 0, "recheck_tuning", "复检请求超时 (0=跟随)",
        "复检专用超时秒数；0 表示继承 api.timeout。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_mode", "str", "auto", "recheck_tuning", "V3 上下文挡位",
        "auto（按模型上限与 max_tokens 自动判定）/ wide（强制宽窗）/ compact（强制紧凑）。",
        choices=("auto", "wide", "compact"),
    ),
    _ana(
        "recheck_v3_batch_size", "int", 0, "recheck_tuning", "V3 共享池批条数 (0=挡位默认)",
        "单批 U3/U4 共享证据池内最多同时审计的伏笔条数（0=8/4）。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_pool_max_chars", "int", 0, "recheck_tuning", "V3 共享池目标字符上限 (0=挡位默认)",
        "单批共享证据池的目标字符预算（0=95000/45000）；256K/512K 模型调大此项可显著复用原文。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_prompt_hard_cap_chars", "int", 0, "recheck_tuning", "V3 提示词硬封顶字符数 (0=自动)",
        "单次复检 prompt 绝对字符硬上限；0 时在默认 110000 与 pool_max_chars+15000 间取大者。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_u1_superchunk_chars", "int", 0, "recheck_tuning", "V3 U1 超块目标字符数 (0=挡位默认)",
        "U1.2 别名扫描与 U1.3 双向路由划分章节事件超块的字符预算（0=58000/30000）。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_u1_split_batch_size", "int", 0, "recheck_tuning", "V3 U1.1 问题拆解批大小 (0=默认)",
        "U1.1 单次拆解 core_question/setup_event_anchor 的伏笔条数（0=50/25）。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_u1_route_q_batch", "int", 0, "recheck_tuning", "V3 U1.3 路由批问题数 (0=默认)",
        "U1.3 单次双向路由提问最多携带的问题条数（0=75/40）。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_chapter_chars", "int", 0, "recheck_tuning", "V3 单章正文窗字符数 (0=默认)",
        "加入共享证据池时每章正文截取上限（0=6800/2800）。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_max_post_windows", "int", 0, "recheck_tuning", "V3 Hop-1 后向最大窗数 (0=默认)",
        "单条伏笔 Hop-1 最多选取的设伏后正文章节数（0=5/4）。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_max_pre_windows", "int", 1, "recheck_tuning", "V3 设伏前因果回指窗数",
        "needs_pre_setup_backref 为真时附带的设伏前章节窗数量。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_hop2_new_cap", "int", 0, "recheck_tuning", "V3 Hop-2 新增窗上限 (0=默认)",
        "第 2 跳多跳深挖每条最多新引入的正文章节数（0=4/3）。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_hop3_new_cap", "int", 0, "recheck_tuning", "V3 Hop-3+ 新增窗上限 (0=默认)",
        "第 3 跳及后续深挖轮次每条最多新引入的正文章节数（0=3/2）。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_max_hops", "int", 3, "recheck_tuning", "V3 最大侦探跳数",
        "U3 多跳追索最大跳数（1=仅首轮核查不追跳，默认 3）。",
        min_val=1, max_val=6,
    ),
    _ana(
        "recheck_v3_max_audit_windows", "int", 0, "recheck_tuning", "V3 终审/修复窗上限 (0=默认)",
        "U4 终审与 U5 引文修复时为单条伏笔聚合的最大章节窗数（0=8/6）。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_pre_lookback_units", "int", 45, "recheck_tuning", "V3 设伏前回指最大章距",
        "因果回指检索与 U5 门控允许追溯设伏前多少个物理章节。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_nms_cluster_radius", "int", 3, "recheck_tuning", "V3 NMS 剧情簇半径",
        "U2 证据选窗按剧情簇非极大值抑制时相邻章聚类半径。",
        min_val=0,
    ),
    _ana(
        "recheck_v3_nms_max_per_cluster", "int", 2, "recheck_tuning", "V3 NMS 每簇最大取窗数",
        "单个剧情簇内最多保留的高分正文章节窗数。",
        min_val=1,
    ),
    _ana(
        "recheck_v2_batch_size", "int", 6, "recheck_tuning", "V2 批大小",
        "recheck_version=v2 时单批送检的伏笔条数。",
        min_val=1, max_val=100,
    ),
    _ana(
        "recheck_v2_window_radius", "int", 400, "recheck_tuning", "V2 局部证据窗半径（字）",
        "V2 按关键词命中点前后各截取的字符半径。",
        min_val=50,
    ),
    _ana(
        "recheck_v2_max_windows", "int", 3, "recheck_tuning", "V2 每条最大窗口数",
        "V2 为每条伏笔构建的设伏后原文局部窗口上限。",
        min_val=1,
    ),
    _ana(
        "recheck_v2_retry_rounds", "int", 2, "recheck_tuning", "V2 轮次上限（含首轮）",
        "V2 总扫描轮次（1=仅首轮不补漏，2=首轮+1轮补漏）。",
        min_val=1,
    ),
    # ==================== 10. 伏笔分类白名单 (1) ====================
    _ana(
        "foreshadow_kept_categories", "list_str", list(DEFAULT_FORESHADOW_CATEGORIES),
        "foreshadow_categories", "伏笔保留功能类别",
        "50 类功能类别白名单；取消勾选的类在构建总表时过滤丢弃。",
    ),
    # ==================== 11. 运行路径、编码优先级与界面预留 (5) ====================
    _ana(
        "encoding_priority", "list_str", list(DEFAULT_ENCODING_PRIORITY),
        "runtime_misc", "文本解码优先级",
        "读取小说 TXT 与 blocks/*.txt 时的候选字符编码顺序列表。",
    ),
    ConfigFieldSpec(
        path="workspace_dir", section="root", key="workspace_dir", field_type="str",
        default="workspace", group="runtime_misc", label="工作区目录",
        description="切分后的书目与分析结果根目录（相对运行目录或绝对路径）。",
    ),
    ConfigFieldSpec(
        path="knowledge_file", section="root", key="knowledge_file", field_type="str",
        default=KNOWLEDGE_FILE_NAME, group="runtime_misc", label="知识库文件名",
        description="分析结束后保存跨章记忆知识库的文件名或相对路径。",
    ),
    ConfigFieldSpec(
        path="working_directory", section="root", key="working_directory", field_type="optional_str",
        default=None, group="runtime_misc", label="当前工作目录覆盖",
        description="可选：显式指定当前单本书目录路径；空则由队列/CLI 自动解析。",
    ),
    ConfigFieldSpec(
        path="gui.language", section="gui", key="language", field_type="str",
        default=DEFAULT_LANGUAGE, group="runtime_misc", label="界面语言（预留）",
        description="未来 i18n 多语言切换预留字段，当前 UI 仍固定展示中文。",
        status="reserved",
    ),
)

SPEC_BY_PATH: Dict[str, ConfigFieldSpec] = {spec.path: spec for spec in CONFIG_FIELD_SPECS}
SPECS_BY_SECTION_KEY: Dict[Tuple[str, str], ConfigFieldSpec] = {
    (spec.section, spec.key): spec for spec in CONFIG_FIELD_SPECS
}


def get_config_schema() -> Dict[str, Any]:
    """返回完整配置 Schema（供 HTTP `/api/settings/schema` 与 CLI `config schema` 共用）。"""
    return {
        "total_fields": len(CONFIG_FIELD_SPECS),
        "groups": [asdict(g) for g in CONFIG_GROUPS],
        "fields": [
            {
                **asdict(spec),
                "default": list(spec.default) if isinstance(spec.default, list) else (
                    dict(spec.default) if isinstance(spec.default, dict) else spec.default
                ),
                "choices": list(spec.choices),
            }
            for spec in CONFIG_FIELD_SPECS
        ],
    }


def resolve_spec(raw_key: str) -> ConfigFieldSpec:
    """按点路径或唯一短键名解析配置项元数据，未知键带近似候选建议。"""
    key = (raw_key or "").strip()
    if key in SPEC_BY_PATH:
        return SPEC_BY_PATH[key]
    by_leaf = [s for s in CONFIG_FIELD_SPECS if s.key == key]
    if len(by_leaf) == 1:
        return by_leaf[0]
    if len(by_leaf) > 1:
        raise KeyError(f"配置键名存在歧义: {raw_key} -> {', '.join(s.path for s in by_leaf)}")
    matches = difflib.get_close_matches(key, SPEC_BY_PATH.keys(), n=4, cutoff=0.45)
    hint = f"；相近项: {', '.join(matches)}" if matches else ""
    raise KeyError(f"未知配置键: {raw_key}{hint}")


def parse_cli_value(spec: ConfigFieldSpec, raw_val: str) -> Any:
    """将 CLI 传入的字符串按目标字段类型严格解析为 Python 值。"""
    ft = spec.field_type
    text = raw_val
    stripped = text.strip()

    if ft == "str":
        return text
    if ft == "optional_str":
        if stripped == "" or stripped.lower() in ("null", "none"):
            return None
        return text
    if ft == "bool":
        low = stripped.lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{spec.path} 需要布尔值（true/false/1/0），收到: {raw_val!r}")
    if ft == "int":
        try:
            if stripped.startswith(("0x", "0X")):
                raise ValueError
            return int(stripped)
        except ValueError as exc:
            raise ValueError(f"{spec.path} 需要整数，收到: {raw_val!r}") from exc
    if ft == "float":
        try:
            val = float(stripped)
        except ValueError as exc:
            raise ValueError(f"{spec.path} 需要数字，收到: {raw_val!r}") from exc
        if val != val or val in (float("inf"), float("-inf")):
            raise ValueError(f"{spec.path} 不能为 NaN 或无穷大")
        return val
    if ft == "dict":
        try:
            parsed = json.loads(stripped)
        except Exception as exc:
            raise ValueError(f"{spec.path} 需要合法 JSON 对象: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{spec.path} 必须为 JSON 对象")
        return parsed
    if ft == "list_str":
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except Exception as exc:
                raise ValueError(f"{spec.path} JSON 数组格式无效: {exc}") from exc
            if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
                raise ValueError(f"{spec.path} 必须为字符串数组")
            return parsed
        if not stripped:
            return []
        return [part.strip() for part in text.split(",") if part.strip()]
    raise ValueError(f"不支持的字段类型: {ft}")


def set_nested_value(payload: Dict[str, Any], spec: ConfigFieldSpec, value: Any) -> Dict[str, Any]:
    """将单条配置修改写入 `{api/analysis/gui/...}` 载荷字典。"""
    if spec.section == "root":
        payload[spec.key] = value
    else:
        sec = payload.setdefault(spec.section, {})
        if isinstance(sec, dict):
            sec[spec.key] = value
    return payload


def read_value_from_dict(config_dict: Dict[str, Any], spec: ConfigFieldSpec) -> Any:
    """从 `AppConfig.to_dict()` 结果中按 Spec 读取当前值。"""
    if spec.section == "root":
        return config_dict.get(spec.key, spec.default)
    sec = config_dict.get(spec.section)
    if isinstance(sec, dict) and spec.key in sec:
        return sec[spec.key]
    return spec.default


def clone_default_value(spec: ConfigFieldSpec) -> Any:
    if isinstance(spec.default, list):
        return list(spec.default)
    if isinstance(spec.default, dict):
        return dict(spec.default)
    return spec.default


def iter_credential_paths() -> Iterable[str]:
    return (
        "api.api_key",
        "api.base_url",
        "api.model",
        "api.provider",
        "analysis.recheck_api_key",
        "analysis.recheck_base_url",
        "analysis.recheck_model",
        "analysis.recheck_provider",
    )
