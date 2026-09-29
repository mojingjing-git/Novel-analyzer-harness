# 配置参考

> 完整配置字段参考。常用字段见根目录 [README](../README.md)。

配置文件：`config.json`（首次启动自动生成模板）。

设置页也提供图形化编辑，运行时 PUT `/api/settings` 会被拒绝（需要重启生效）。

## 环境变量

| 变量 | 优先级 | 说明 |
|---|---|---|
| `LLM_API_KEY` | 覆盖 `api.api_key` | 通用 OpenAI 兼容 key |
| `MINIMAX_API_KEY` | 覆盖 `api.api_key` | M2.7 / M3 专用，与 LLM_API_KEY 二选一 |
| `NOVEL_ROOT` | 覆盖默认工作区 | 桌面端双目录方案的关键，详见 [architecture.md](./architecture.md) |
| `NOVEL_ANALYZER_SESSION_TOKEN` | 桌面端自动生成 | 日志查询端点鉴权，详见 [architecture.md](./architecture.md) |

环境变量优先级高于 `config.json`，常用于 CI / 容器化部署。

## api 段

| 字段 | 默认 | 说明 |
|---|---|---|
| `base_url` | 预设空 | 任意 OpenAI 兼容端点，如 `https://api.minimaxi.com/v1` |
| `api_key` | 预设空 | 可用 `LLM_API_KEY` / `MINIMAX_API_KEY` 环境变量覆盖 |
| `model` | 预设空 | 默认分析模型 |
| `max_tokens` | 130000 | 输出上限（>65537 时警告） |
| `timeout` | 300 | 分析阶段 API 超时（秒） |
| `summary_timeout` | 600 | 总结阶段 API 超时（秒）。总结 prompt 11 万字符级，单独给长 |
| `json_mode` | `default` | `default` / `qwen` / `deepseek` / `glm47`，按厂商 JSON 输出协议选 |
| `temperature` | 0.5 | 初始温度 |
| `temperature_step` | 0.25 | 温度退火步长（每重试一次降多少） |
| `temperature_max_retries` | 2 | 温度退火轮数 |
| `backoff_max_retries` | 1 | 指数退避轮数 |
| `thinking_mode` | `{}` | 思考控制。M2.7 / GLM / M3 用 `{"thinking": {"type": "disabled"}}`，DeepSeek / Qwen 用 `{"enable_thinking": false}` |
| `summary_model` | `""` | 总结专用模型，空值跟随 `model` |
| `summary_thinking_mode` | `{}` | 总结专用思考控制，空值跟随 `thinking_mode` |

## analysis 段

### 并发与块大小

| 字段 | 默认 | 说明 |
|---|---|---|
| `concurrency` | 3 | 章节分析并发数（同时最多几个 LLM 请求） |
| `block_size` | 1 | 每个块包含多少章（按章号切块，容错断号目录） |

### Prompt 预算

| 字段 | 默认 | 说明 |
|---|---|---|
| `max_arcs_in_prompt` | 30 | 单章 prompt 最多带多少条人物弧光 |
| `max_foreshadows_in_prompt` | 50 | 单章 prompt 最多带多少条活跃伏笔 |
| `max_locations_in_prompt` | 20 | 单章 prompt 最多带多少个地点 |
| `volume_compress_threshold` | 8 | 卷摘要超过多少卷触发分层压缩 |
| `volume_compress_group` | 3 | 压缩时每组合并多少卷 |

### 伏笔筛选

| 字段 | 默认 | 说明 |
|---|---|---|
| `foreshadow_kept_categories` | 全 50 类 | 保留的伏笔类别集合，TimelinePage chip 多选 |
| `foreshadow_min_importance` | 0.3 | 重要度阈值（0-1） |
| `foreshadow_min_confidence` | 0.5 | 置信度阈值（0-1） |
| `max_foreshadow_catalog_high` | 500 | 高优先级伏笔总表上限 |
| `max_foreshadow_catalog_mid` | 200 | 中优先级伏笔总表上限 |

### 滚动总结

| 字段 | 默认 | 说明 |
|---|---|---|
| `rolling_summary_threshold` | 20 | 章数达到多少时触发滚动总结 |
| `rolling_summary_window` | 10 | 近期势头窗口大小 |
| `milestone_max_per_layer` | 5 | 每个范式层最多保留多少条里程碑 |
| `auto_archive` | true | 近期势头满窗是否自动 LLM 归档压缩 |

### 自动总结与总结批

| 字段 | 默认 | 说明 |
|---|---|---|
| `auto_summary` | false | 单书分析完成后是否自动跑最终总结 |
| `summary_concurrency` | 2 | 总结阶段并发数（伏笔复检子批并发） |
| `summary_batch_size` | 40 | 伏笔复检每批多少个 |
| `checkpoint_interval` | 5 | 章节分析每多少批落盘一次 |

## 预设厂商

设置页"预设"下拉可一键填充 `base_url` + 默认 `model`：

| 预设 | base_url |
|---|---|
| M2.7 | `https://api.minimaxi.com/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` |
| 火山引擎 | `https://ark.cn-beijing.volces.com/api/v3` |
| 硅基流动 | `https://api.siliconflow.cn/v1` |
| LM Studio | `http://localhost:1234/v1` |
| Ollama | `http://localhost:11434/v1` |
| 自定义 | 留空手动填 |

完整厂商 JSON 列表见 `backend/config/presets.py`。

## 修改字段时的注意

- `APIConfig` / `AnalysisConfig` 是 dataclass，加新字段后旧 `config.json` 仍可加载（`_coerce_fields` 过滤未知字段并清洗类型）
- 前端 `AppConfigDto` 必须同步补类型，否则 TypeScript 编译失败
- 涉及 KV cache 命中的字段（`thinking_mode`、`prompt_version`）变化会让远端缓存失效
