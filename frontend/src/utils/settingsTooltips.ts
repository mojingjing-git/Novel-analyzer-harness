/** SettingsPage tooltips, field key sets, JSON mode options, and detector labels. */
import { ALL_ADVANCED_FIELD_KEYS } from './settingsSchema'

export interface CategoryDef {
  name: string
  description: string
  examples: string[]
}

// 设置项 tooltip 字典：key 与模板中 :title 表达式一一对应
export const tooltips: Record<string, string> = {
  // 主 API 连接
  api_preset: '从已保存的厂商预设中快速填入 base_url / model / api_key；切到预设会自动覆盖当前未保存的输入',
  base_url: 'LLM 服务的入口地址，例如 https://api.openai.com/v1 或厂商网关地址',
  api_key: '厂商颁发的访问密钥；本地工具明文存储，仅写入本机 config.json',
  provider: '请求协议格式：\nauto = 按 base_url/API Key/模型名特征自动检测\nopenai = OpenAI 兼容 /chat/completions\nanthropic = Anthropic /v1/messages',
  // 模型调用与并发
  model: '章节分析（主调用）模型。点输入框展开列表（按当前 base_url/api_key 探测），也可手填自定义模型',
  thinking_depth: '控制模型思考深度（reasoning_effort/budget）。高优先级，浅度思考(low)可有效压制Token开销同时避免完全关闭思考导致输出崩塌',
  thinking_mode: '禁用/启用模型的思考链。仅在未设置思考深度时生效。\n点「自动探测」会让系统实测哪个参数有效',
  concurrency: '并行分析的章节块数。\n越大越快，但 token 峰值越高（受 API TPM/RPM 限制）',
  summary_model: '最终总结（卷摘要/脉络报告/写作风格/图谱）专用模型；留空 = 跟随主模型',
  summary_thinking: '最终总结思考控制；留空 = 跟随主思考配置',
  summary_concurrency: '最终总结并发数；0 = 严格跟随章节分析主并发',
  recheck_model: '伏笔复检（V3 / V2）专用模型；留空 = 跟随主模型',
  recheck_reasoning: '伏笔复检思考档位；留空 = 跟随主思考配置（仅 OpenAI 兼容端点生效）',
  recheck_concurrency: '伏笔复检并发数；0 = 严格跟随章节分析主并发',
  recheck_v3_gray_u4: 'V3 灰区终审（默认关闭）：开启后对多跳、非高置信或争议伏笔追加一轮 U4 终审，token 消耗约增加 33%',
  recheck_v3_mid_single_hop: 'V3 支线单跳省钱模式（默认关闭）：开启后中重要度支线伏笔缩窗至 3 个且跳过二三跳（高重要度主线仍保留 5 窗 3 跳），约再省 15%–25% token',
  // 运行与流程策略
  block_size: '单批最多打包发送给 LLM 的章节上限（动态受 max_content_chars 与 max_tokens 输出预算约束）。\n模型返回逐章独立槽位并在本地拆成单章结果文件，中途改大或改小均无缝断点续跑。',
  summary_batch_size: '总结阶段的批次大小；每批含 N 卷',
  auto_archive: '分析完成后自动把工作区搬到「分析结果/」归档目录',
  auto_summary: '队列全部完成后自动启动最终总结',
  skip_moderation_blocked: '识别为内容审核拦截的章节：重试 1 次后跳过并标记，不再反复重试白烧成本',
  // 请求超时、JSON 模式与重试
  max_tokens: '单次 LLM 请求的最大输出 token 上限；超出将被截断',
  timeout: '单次分析与默认复检请求的 API 超时（秒）；超时后会进入重试链',
  summary_timeout: '最终总结（卷摘要/最终报告/风格提取/图谱）使用的 API 超时，建议比分析 Timeout 长（默认 600s）',
  json_mode: 'JSON 输出约束模式：\ndefault = 通用\nqwen = Qwen 系列需关 thinking\ndeepseek = DeepSeek V4\nglm47 = GLM-4.7',
  temperature: '单次请求的初始温度；后续重试会逐步降温',
  temperature_step: '每轮退火重试时降低的温度（例：起始 0.7、步长 0.15 → 0.7 → 0.55 → 0.4 → ...）',
  temperature_max_retries: '温度退火最大重试轮数；用尽后再走退避',
  backoff_max_retries: '指数退避最大轮数；等待时长 min(2^N, 60s)；429 会额外 3 轮并尊重 Retry-After',
  // 提示词上下文与滚动总结预算
  max_arc_length: '主线（角色弧光）最大字数；超过会在合并阶段裁剪',
  max_arcs_in_prompt: '每章 Prompt 中携带的最近弧光条数；过多会稀释注意力',
  max_summaries_in_prompt: '每章 Prompt 中携带的近期章节摘要数；影响上下文长度与连续性',
  timeline_truncate: 'Prompt 中携带的时间线条数上限；超出按章节倒序截断',
  max_character_states: 'Prompt 中携带的最近角色状态条数',
  max_world_items: 'Prompt 中携带的世界观条目上限',
  max_foreshadow_entries: 'Prompt 中携带的伏笔网络条目上限；超过按综合权重截断',
  batch_summary_min_words: '每卷摘要的最低字数要求；不足会被 LLM 补写',
  final_report_min_words: '最终报告的最低字数要求',
  rolling_early_chapters: '开篇多少章内不做滚动总结（让 KB 自然增长）',
  rolling_max_milestones: '滚动总结中里程碑条数上限；超出 FIFO 淘汰',
  rolling_max_momentum: '势头条目上限；超出触发归档压缩为里程碑',
  checkpoint_interval: '每处理多少块做一次 checkpoint 落盘；0=关闭',
}

export const ADVANCED_FIELD_KEYS = ALL_ADVANCED_FIELD_KEYS

export const RECHECK_ENDPOINT_FIELD_KEYS = new Set([
  'recheck_base_url', 'recheck_api_key', 'recheck_provider', 'recheck_json_mode',
])

export const jsonModes = [
  { value: 'default', label: 'default - 通用' },
  { value: 'qwen', label: 'qwen - Qwen系列' },
  { value: 'deepseek', label: 'deepseek - DeepSeek V4' },
  { value: 'glm47', label: 'glm47 - GLM-4.7' },
]

export const PROBE_DETECTOR_LABELS: Record<string, string> = {
  reasoning_content: 'message.reasoning_content',
  reasoning_details: 'message.reasoning_details',
  think_tags: 'content 含 <think>',
  thinking_tags: 'content 含 <thinking>',
  reasoning_tag: 'content 含 <reasoning>',
  usage_reasoning_tokens: 'usage.reasoning_tokens>0',
  anthropic_thinking_blocks: 'content list + type=thinking',
}
