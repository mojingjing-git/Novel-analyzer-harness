# 配置参考

配置文件是仓库根目录的 `config.json`，首次启动时由 `config.example.json` 复制生成。
设置页提供图形化编辑，保存后立即落盘。

**所有默认值以 `backend/config/constants.py` 为准**。本文表格里的"默认"列是代码默认值，
不是某一台机器上的实例值。

## 配置的读写机制

- `PUT /api/settings` 是**全量覆盖**：请求体整体替换配置。分析正在运行时会被拒绝
  （409），其余时候可以正常写入，**不需要重启**。
- 设置页保存前会删掉所有空字符串字段，服务端据此把该项**回落成代码默认值**——
  这就是"清空输入框 = 恢复默认"的实现方式。
- 服务端只接受 dataclass 里声明过的键，并做类型强转（字符串 `"4"` → `4`）。
  **未声明的键会被静默丢弃**，写错字段名不会报错，只会不生效。
- 配置文件损坏时会被标记，修复前拒绝写入，避免默认配置覆盖真实 API Key。

## 环境变量

| 变量 | 作用 |
|---|---|
| `LLM_API_KEY` | 覆盖 `api.api_key` |
| `MINIMAX_API_KEY` | 覆盖 `api.api_key`（MiniMax 专用） |
| `NOVEL_ROOT` | 覆盖默认工作区根目录，桌面端网络盘场景必须用 |
| `NOVEL_ANALYZER_SESSION_TOKEN` | 桌面端日志下载端点的鉴权 token |
| `NOVEL_ENABLE_VNEXT_ROUTES` | 设为 `1` 时挂载默认冻结的 `/api/vnext/*` 调试路由 |

密钥来自环境变量时，保存配置会把 `api_key` 置空后落盘——磁盘上不留环境变量里的密钥。

## api 段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `base_url` | str | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容端点基址 |
| `api_key` | str | `""` | 密钥，可由环境变量覆盖 |
| `model` | str | `""` | 分析主模型；为空时启动分析会被拦下 |
| `provider` | str | `auto` | 协议格式：`auto` / `openai` / `anthropic`，非法值回落 `auto` |
| `max_tokens` | int | `130000` | 单次输出上限；超过 65537 会在启动时告警 |
| `timeout` | int | `300` | 分析阶段超时（秒） |
| `summary_timeout` | int | `600` | 总结阶段超时（秒），总结阶段单独用这个值 |
| `temperature` | float | `0.1` | 温度退火的起始温度 |
| `temperature_step` | float | `0.05` | 每轮退火降低的温度 |
| `temperature_max_retries` | int | `3` | 温度退火阶段的最大轮数 |
| `backoff_max_retries` | int | `1` | 指数退避轮数；遇到 429 时至少退避 3 轮并尊重 `Retry-After` |
| `max_retries` | int | `5` | **只用于失败日志里标注"第 n/m 次尝试"，不控制任何重试**。真正的重试由上面两个重试项决定 |
| `sdk_max_retries` | int | `0` | 传给 OpenAI / Anthropic 底层 SDK 的重试次数（项目本身已有两级重试链，默认 0 关闭） |
| `json_mode` | str | `default` | JSON 输出协议。只有 `default` 与非 `default` 有区别：非 `default` 且非 Anthropic 协议、非 localhost 时，额外加 `response_format={"type":"json_object"}` |
| `thinking_mode` | dict | `{}` | 思考链控制。空 dict 表示不注入任何参数、跟随端点默认。不同厂商认的键不同（`{"thinking":{"type":"disabled"}}` 或 `{"enable_thinking":false}`），Anthropic 协议只放行 `thinking` 键 |
| `reasoning_effort` | str | `""` | 推理深度档位（`low` / `medium` / `high` / `xhigh` / `max`）；空 = 不传，由服务端决定 |
| `summary_model` | str | `""` | 总结专用模型，空值跟随 `model` |
| `summary_thinking_mode` | dict | `{}` | 总结专用思考控制，空值跟随 `thinking_mode` |
| `streaming_enabled` | bool | `true` | 是否走流式调用；关掉则回退非流式。**设置页没有这一项**，只能手改配置文件 |

## analysis 段

### 并发与切块

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `concurrency` | int | `2` | 章节分析的并行 worker 数 |
| `block_size` | int | `1` | 每块包含几章（1 = 单章一块） |
| `checkpoint_interval` | int | `5` | 每 N 批落盘一次，0 = 只在结束时落盘 |
| `encoding_priority` | list[str] | 11 种编码候选 | 文件编码探测顺序。设置页没有这一项 |

### Prompt 预算

这些字段决定一次提示词里能带多少历史信息。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `max_arc_length` | int | `1500` | 主线的最大字数 |
| `max_arcs_in_prompt` | int | `30` | 单次提示词最多带几条主线 |
| `max_summaries_in_prompt` | int | `5` | 单次提示词最多带几条摘要 |
| `timeline_truncate` | int | `200` | 时间线截断字符数 |
| `max_character_states` | int | `20` | 人物状态条目上限 |
| `max_world_items` | int | `20` | 世界观条目上限 |
| `max_foreshadow_entries` | int | `5` | 活跃伏笔条目上限 |
| `max_relationships` | int | `15` | 人物关系条目上限（提示词窗口） |
| `max_themes` | int | `8` | 主题条目上限（提示词窗口） |
| `max_content_chars` | int | `30000` | 提示词携带的章节正文字符上限 |
| `max_verified_facts_in_prompt` | int | `15` | 已证事实的提示词窗口上限（知识库裁剪字段 `max_verified_facts` 管"库里留多少"，这个管"提示词带多少"，两层分开） |

### 滚动总结

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `rolling_early_chapters` | int | `100` | 首次生成结构化 JSON 的触发章数 |
| `rolling_max_milestones` | int | `20` | 全局里程碑上限，FIFO 淘汰最旧的 |
| `rolling_max_momentum` | int | `15` | 近期势头条目上限 |

### 知识库容量

以下字段限制磁盘知识库各子表的条目数，满了按写入顺序淘汰。设置页没有这几项。

| 字段 | 默认 | 对应子表 |
|---|---|---|
| `max_compressed_arcs` | 50 | 压缩主线 |
| `max_recent_summaries` | 5 | 最近章节摘要 |
| `max_pacing_tracker` | 100 | 节奏追踪 |
| `max_foreshadow_network` | 100 | 伏笔网络 |
| `max_world_building` | 200 | 世界观 |
| `max_verified_facts` | 200 | 已验证事实 |
| `max_long_term_arcs` | 200 | 长线主线 |
| `max_thematic_elements` | 100 | 主题要素 |

### 伏笔筛选

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `foreshadow_kept_categories` | list[str] | 全部 50 类 | 保留哪些伏笔分类，时间线页按这个筛选 |
| `foreshadow_min_importance` | str | `中` | **取值是中文等级 `高` / `中` / `低`，不是 0–1 的数值** |
| `foreshadow_min_confidence` | str | `低` | 同上，中文等级字符串 |
| `max_foreshadow_catalog_high` | int | `500` | 高重要度伏笔在总表里的上限，`-1` 表示不限制 |
| `max_foreshadow_catalog_mid` | int | `200` | 中重要度伏笔在总表里的上限 |

### 总结与归档

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `summary_batch_size` | int | `30` | 最终总结每批处理多少章 |
| `summary_concurrency` | int | `2` | 总结阶段的并发数 |
| `batch_summary_min_words` | int | `2000` | 分卷摘要的建议最低字数 |
| `final_report_min_words` | int | `5000` | 最终报告的建议最低字数 |
| `volume_compress_threshold` | int | `80000` | **单位是字符数**：卷摘要拼接后的总字符数超过它就触发分层压缩 |
| `volume_compress_group` | int | `5` | 压缩时每组几卷；首尾各一组保留全文，中间组截断到一半 |
| `auto_summary` | bool | `false` | 队列里的书全部分析完成后，逐本自动跑最终总结 |
| `auto_archive` | bool | `false` | 队列分析完成后，自动把整本书的目录归档到 `分析结果/` |

### 伏笔复检

总结第 2 阶段对每条伏笔用原文证据核对回收判定。带证据复检（V2）是默认路径；关掉 `recheck_v2_enabled` 即回退旧的全书复检。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `recheck_v2_enabled` | bool | `true` | 带证据复检开关（关闭回退旧路径） |
| `recheck_v2_batch_size` | int | `6` | 每次调用让判定模型同时核对几条候选（与旧路径批大小分开） |
| `recheck_max_calls` | int | `300` | 一次复检最多调用多少次，超出的候选记「未核对」，可续跑 |
| `recheck_max_tokens` | int | `32768` | 单次复检调用的输出上限；判定回包（6 候选 JSON）约 2k token |
| `recheck_concurrency` | int | `0` | 复检同时在途的批次数上限；0 = 跟随主 `concurrency` |
| `recheck_model` | str | `""` | 判定模型；留空跟随主模型。判定要准确，可换用网关专用模型名 |
| `recheck_base_url` | str | `""` | 判定请求端点；留空跟随主通道。换模型通常要同源换通道 |
| `recheck_api_key` | str | `""` | 判定通道的 API Key；留空跟随主 Key。换第三方端点时必配 |
| `recheck_provider` | str | `""` | 判定协议格式；空=完全跟随主配置，`auto`=按判定端点特征检测 |
| `recheck_timeout` | int | `0` | 判定请求超时（秒）；0 = 跟随 `api.timeout` |
| `recheck_json_mode` | str | `""` | 判定 JSON 输出约束；空=跟随 `api.json_mode`（判定通道是本地地址时整体跳过） |
| `recheck_reasoning_effort` | str | `""` | 判定思考档位；空=跟随主配置的思考设置（low/medium/high 显式覆盖） |
| `foreshadow_recheck_batch_size` | int | `40` | **仅旧路径**：全书复检每次调用携带的活跃伏笔数 |
| `anchor_quote_enabled` | bool | `true` | 逐章采集锚点引文；关掉则复检退化为无证据模式 |
| `anchor_min_chars` | int | `6` | 锚点引文的最短字数 |

复检开关、判定通道全参数（预设 / base_url / api_key / model / provider / timeout / json_mode / effort）与并发、批大小、调用/输出上限在设置页「伏笔复检（带证据 V2）」分组可改；锚点两项只能手改 `config.json`。

判定模型与判定通道必须同源：网关专用模型名（含 `/`，如 `codex/gpt-6-luna`）配官方端点会在运行时被阻断（复检记 `PARTIAL`）。

### 内容审核

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `skip_moderation_blocked` | bool | `true` | 判定为审核拦截的章节，重试一次后跳过并标记 |

### B/C 实验开关（默认全关闭）

以下 5 个开关对应伏笔登记/回收（B）与按需复审（C）的独立增强特性，默认全部为 `false`，保证基线零行为变化；评估结论与试产建议见 [结案总表](./research/bc-production-readiness-2026-11-06/结案总表.md)。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable_b_trace` | bool | `false` | 开启 B1–B4 遗漏全链路审计追踪 |
| `fix_b4_flow_and_storage` | bool | `false` | 开启知识库未裁剪归档与完整伏笔目录/同题无损合并 |
| `fix_b2_evidence_and_recheck` | bool | `false` | 开启 B2 复检证据召回增强与行车记录仪请求落盘 |
| `enable_c_on_demand_audit` | bool | `false` | 开启 C 类按需声明级闭环复审（通过后才写 `resolved`） |
| `enable_anchor_b_plus_rule` | bool | `false` | 开启伏笔登记 Rule B+（章末全局未解守卫与局部自足性解耦） |

## 顶层字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `working_directory` | str \| null | `null` | 运行时按书覆写，用于定位输出目录 |
| `knowledge_file` | str | `knowledge.json` | 知识库文件名 |
| `workspace_dir` | str | `workspace` | 工作区目录名 |
| `gui.language` | str | `zh-CN` | i18n 预留字段，**后端目前没有消费点**，不影响任何行为 |

## 厂商预设

设置页的"预设"下拉可以一键填入 `base_url`：

`LM Studio`、`OpenCodeGo`、`OpenRouter`、`MiniMax`、`阿里百炼`、`智谱清言`、
`火山引擎`、`硅基流动`、`DeepSeek`、`自定义`。

预设不预置模型名，`model` 一律留空由用户填。完整定义见 `backend/config/presets.py`。

## 已废弃的键

读到这些键会被丢弃，其中 `api.temperature_sequence` 会打一条警告日志：

`api.temperature_sequence`、`analysis.compress_every`、
`analysis.rolling_summary_target_chars`、`analysis.rolling_early_target_chars`、
`analysis.rolling_recent_target_chars`。

## 修改配置时注意

- `APIConfig` / `AnalysisConfig` 是 dataclass，新增字段后旧 `config.json` 仍能加载
  （`_coerce_fields` 过滤未知字段并清洗类型）。
- 前端 `AppConfigDto` 必须同步补类型，否则 TypeScript 编译不过。
- 改动 `thinking_mode`、提示词内容会让远端 KV cache 失效，缓存命中率会掉一截。
