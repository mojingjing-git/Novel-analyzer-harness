# 配置参考

源码模式下配置文件位于 `runtime/config.json`，首次启动由 `desktop/config.example.json` 复制生成；打包便携 EXE 模式下则位于 EXE 同级目录 `config.json`。系统通过统一配置元模型（`backend/config/schema.py`）暴露全量 **131 项**参数，设置页与 CLI `config schema` 均完整提供读写能力，保存后立即落盘。

**所有默认值以 `backend/config/constants.py` 与 `backend/config/schema.py` 为准**。本文表格里的"默认"列为代码默认值。

## 配置的读写机制与元模型

- 统一配置元模型 `backend/config/schema.py` 定义全量 **131 项**字段，划分为 **11 个分组**（3 个核心分组、1 个复检独立端点分组、7 个高级参数分组），`GET /api/settings/schema` 与 `python -m backend.cli config schema --json` 对外暴露同一套元数据。
- `PUT /api/settings` 执行全量替换，`PATCH /api/settings` 执行局部增量更新；分析或总结运行中受写护栏保护（非纯总结参数返回 409），空闲期间写入立即生效，无需重启。
- 设置页高级区域支持按名称/键名/说明关键词搜索、`仅看已修改`筛选、分组折叠与单字段一键重置；保存前会剪除空字符串字段以触发服务端默认值回落，`working_directory` 留空提交 `null`。
- `ConfigManager` 对未知键按 dataclass 白名单清洗与强转，并在环境变量注入 `LLM_API_KEY` / `MINIMAX_API_KEY` 时保留既有磁盘 `api_key`，避免环境变量密钥写盘或抹除原有配置；配置文件解析失败时锁定写入，直到 JSON 修复。

## 环境变量

| 变量 | 作用 |
|---|---|
| `LLM_API_KEY` | 覆盖 `api.api_key` |
| `MINIMAX_API_KEY` | 覆盖 `api.api_key`（MiniMax 专用） |
| `NOVEL_RUNTIME_DIR` | 显式指定源码模式下的运行时数据根目录（默认 `<repo>/runtime/`，打包 EXE 始终写 EXE 同级） |
| `NOVEL_ROOT` | 指向代码仓库或外置数据目录；当指向代码仓库根（如桌面端本地副本场景）时，运行数据落入 `<NOVEL_ROOT>/runtime/`，同时用于定位只读 `frontend/dist` 与模板 |
| `NOVEL_ANALYZER_SESSION_TOKEN` | 桌面端日志下载端点的鉴权 token |

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
| `streaming_enabled` | bool | `true` | 是否走流式调用；关闭则回退非流式 |
| `temp_retry_wait_base` | int | `3` | 温度退火重试基础等待秒数 |
| `rate_limit_wait_base` | int | `30` | 429 限流重试基础等待秒数 |
| `rate_limit_wait_cap` | int | `120` | 429 限流等待秒数上限 |
| `rate_limit_min_backoff_retries` | int | `3` | 遭遇 429 时触发的最小退避轮数 |
| `backoff_wait_cap` | int | `60` | 指数退避等待秒数上限 |
| `hard_timeout_margin` | int | `30` | 硬超时附加安全边界（秒） |
| `moderation_short_circuit_hits` | int | `2` | 单块内容审核拦截累计触发短路的次数阈值 |
| `breaker_open_after` | int | `1` | 账户级熔断器连续失败触发开闸的阈值 |

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
| `rolling_update_interval` | int | `0` | 滚动总结更新间隔块数；0 = 自适应默认 |
| `rolling_summary_max_tokens` | int | `0` | 滚动总结输出上限；0 = 跟随策略默认 |
| `rolling_summary_max_retries` | int | `1` | 滚动总结单次最大重试次数 |
| `rolling_summary_temperature` | float | `0.2` | 滚动总结采样温度 |
| `rolling_max_milestones` | int | `20` | 全局里程碑上限，FIFO 淘汰最旧条目 |
| `rolling_max_momentum` | int | `15` | Prompt 中携带的近期势头条目上限 |
| `rolling_momentum_window` | int | `50` | 滚动势头归档窗口长度 |
| `rolling_archive_trigger_count` | int | `10` | 触发势头归档为里程碑的累积条数阈值 |
| `rolling_max_paradigm_layers` | int | `5` | 范式层记录条数上限 |
| `rolling_max_causal_chains` | int | `5` | 活跃因果链条数上限 |
| `rolling_archive_max_tokens` | int | `100` | 势头归档摘要字数/token 上限 |
| `failed_block_retry_rounds` | int | `3` | 分析流水线失败块补跑最大轮数 |

### 知识库容量

以下字段限制磁盘知识库各子表的条目数，满额按写入顺序淘汰。

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
| `max_kb_character_states` | 200 | 人物状态词典上限 |
| `max_kb_character_relationships` | 300 | 人物关系词典上限 |

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
| `volume_compress_group` | int | `5` | 压缩时每组几卷；首尾各一组保留全文，中间组截断 |
| `volume_compress_ratio` | float | `0.5` | 中间卷摘要截断保留比例（0.1–1.0） |
| `dormant_chapter_threshold` | int | `200` | 伏笔长期未复现进入 `dormant` 状态的章节跨度阈值 |
| `foreshadow_min_clue_chars` | int | `15` | 伏笔线索最小有效字符数 |
| `foreshadow_dedup_keyword_overlap` | int | `2` | 伏笔去重关键词重叠阈值 |
| `foreshadow_dedup_similarity` | float | `0.6` | 伏笔描述相似度合并阈值 |
| `atlas_max_tokens` | int | `32768` | Stage 4 全书图谱提取输出上限 |
| `atlas_max_characters` | int | `60` | 确定性图谱锚点提取角色上限 |
| `atlas_max_locations` | int | `50` | 确定性图谱锚点提取地点上限 |
| `style_sample_chapters` | int | `6` | 写作风格提取采样章节数 |
| `style_sample_chars` | int | `1500` | 单章风格采样字符上限 |
| `style_max_tokens` | int | `20000` | 写作风格语义提取输出上限 |
| `style_temperature` | float | `0.3` | 写作风格提取采样温度 |
| `auto_summary` | bool | `false` | 队列里的书全部分析完成后，逐本自动跑最终总结 |
| `auto_archive` | bool | `false` | 队列分析完成后，自动把整本书的目录归档到 `分析结果/` |

### 伏笔复检

总结复检阶段对每条伏笔用原文证据核对回收判定，默认使用带证据 V3 多跳证据链引擎（`recheck_version = "v3"`），也可切换为 V2（`"v2"`）。全量复检参数均在 `recheck_tuning` 分组暴露，完整列表与边界可通过 `config schema --group recheck_tuning` 或设置页高级区域查看：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `recheck_version` | str | `"v3"` | 带证据复检引擎版本：`v3`（默认共享池单阶段多跳引擎）或 `v2`（批量窗口核对） |
| `recheck_v3_mode` | str | `"auto"` | V3 窗口容量模式：`auto` / `wide` / `compact` |
| `recheck_v3_batch_size` .. `recheck_v3_nms_max_per_cluster` | int | `0` 等 | V3 U1 超块切分/拆解/路由批大小、章节与共享池上限、前后窗数、多跳上限、预埋回溯距离与 NMS 聚类半径等 16 项调优参数（`0` 表示跟随模式默认；详细说明以 `schema.py` 为准） |
| `recheck_v2_batch_size` / `recheck_v2_window_radius` / `recheck_v2_max_windows` / `recheck_v2_retry_rounds` | int | `6 / 400 / 3 / 2` | V2 单跳批大小、窗口半径、最大窗口数与缺漏重试轮数 |
| `recheck_v3_gray_u4` | bool | `false` | 仅 V3：是否在单阶段裁决后对多跳/非高置信/争议条目追加灰区 U4 终审 |
| `recheck_v3_mid_single_hop` | bool | `false` | 仅 V3：是否将中重要度条目缩至 3 窗口并跳过二三跳（高重要度主线保持 5 窗 3 跳） |
| `recheck_max_calls` | int | `1200` | 一次复检最多调用多少次，超出的候选记「未核对」，可续跑 |
| `recheck_max_tokens` | int | `32768` | 单次复检调用的输出上限；判定回包约 2k token |
| `recheck_concurrency` | int | `0` | 复检同时在途的并发上限；0 = 跟随主 `concurrency` |
| `recheck_model` | str | `""` | 判定模型；留空跟随主模型。判定要准确，可换用网关专用模型名 |
| `recheck_base_url` | str | `""` | 判定请求端点；留空跟随主通道。换模型通常要同源换通道 |
| `recheck_api_key` | str | `""` | 判定通道的 API Key；留空跟随主 Key。换第三方端点时必配 |
| `recheck_provider` | str | `""` | 判定协议格式；空=完全跟随主配置，`auto`=按判定端点特征检测 |
| `recheck_timeout` | int | `0` | 判定请求超时（秒）；0 = 跟随 `api.timeout` |
| `recheck_json_mode` | str | `""` | 判定 JSON 输出约束；空=跟随 `api.json_mode`（判定通道是本地地址时整体跳过） |
| `recheck_reasoning_effort` | str | `""` | 判定思考档位；空=跟随主配置的思考设置（low/medium/high 显式覆盖） |
| `anchor_quote_enabled` | bool | `true` | 逐章采集锚点引文；关闭则复检退化为无证据模式 |
| `anchor_min_chars` | int | `6` | 锚点引文的最短字数 |

全部 131 项配置参数（含 `api`、`analysis`、`gui` 与顶层路径字段）均可在设置页 7 大可检索高级分组及 CLI `config schema / get / set / patch / reset` 中直接查看与修改。

判定模型与判定通道必须同源：网关专用模型名（含 `/`，如 `codex/gpt-6-luna`）配官方端点会在运行时被阻断（复检记 `PARTIAL`）。

### 内容审核

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `skip_moderation_blocked` | bool | `true` | 判定为审核拦截的章节，重试一次后跳过并标记 |

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

## 配置扩展约定

- `backend/config/schema.py` 与 `APIConfig` / `AnalysisConfig` / `GUIConfig` / `AppConfig` 保持 1:1 字段对齐（`test_config.py` 校验双射与 `desktop/config.example.json` 一致性）。
- 前端 `AppConfigDto`（`frontend/src/api/client.ts`）与 `frontend/src/utils/settingsSchema.ts` 随后端元模型同步维护，并通过前端单测守护全量覆盖。
- 改动 `thinking_mode`、提示词内容会让远端 KV cache 失效，缓存命中率会下降。
