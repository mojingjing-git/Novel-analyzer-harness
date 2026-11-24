# Agent 工作指南（小说分析器）

本文件面向在 `小说分析器` 仓库中执行任务的 AI Agent，提供项目背景、架构全景、模块职责、
常用命令、硬性约束与验证方法。

文档只描述**当前状态**：改代码时同步修正受影响的章节（模块说明、数据结构、端点、配置项），
但不要在文档里记录改动历史——那是 git 的职责。

---

## 1. 项目概述

基于 LLM 的长篇中文网络小说深度分析工具。核心流程：`txt 切分 → 逐章分析 → 滚动总结 → 全书总结`。

- **前端**：Vue 3.5 + TypeScript 5.7 + Vite 6 + Tailwind 4，Win11 Fluent 设计系统
- **后端**：FastAPI（异步），Python 3.11+
- **桌面壳**：pywebview + WebView2（Windows）
- **LLM 兼容**：任何 OpenAI 兼容 API（10 个厂商预设），以及 Anthropic 协议
- **测试**：后端 14 个领域测试文件（`backend/tests/`），前端 5 个测试文件（`frontend/src/__tests__/`），统一由 `python scripts/run_tests.py` 运行（见 [docs/testing.md](./docs/testing.md)）。
  数量以 `pytest --collect-only -q` 与 `npm run test` 实时为准


---

## 2. 仓库结构（完整）

```
.
├── backend/                      # FastAPI 后端（生产代码 96 个 .py）
│   ├── app.py                    # FastAPI 工厂（CORS、SPAStaticFiles 静态托管、路由注册、lifespan 工作区扫描）
│   ├── main.py                   # 后端启动器（127.0.0.1:8000）
│   ├── ws_events.py              # WS 协议权威定义（WSType 10 种 / WSState 8 种 / PipelineStatus + payload 契约）
│   ├── progress_hub.py           # 进度广播中枢（分级驱逐慢消费者 + token_delta 250ms 节流 + log 转发）
│   ├── common/                   # 共用纯逻辑层（10 个模块：错误分类 / 审核 / 关闭条件 / LLM 错误信号 / 复检请求日志 / proxy_env；只依赖标准库）
│   ├── core/                     # 核心引擎（23 个模块；error_types / error_taxonomy / moderation 为 common 的兼容转发层）
│   │   ├── pipeline.py           # 主分析流水线（预热 / 并发 / checkpoint / 滚动总结 / 失败补跑）
│   │   ├── analyzer.py           # 单章分析引擎（LLM 调用 + 校验回调 + 知识库更新）
│   │   ├── llm_client.py         # LLM 客户端（OpenAI/Anthropic 双协议 + 温度退火与指数退避 + 流式/非流式）
│   │   ├── llm_probe.py          # 禁用思考参数探测
│   │   ├── llm_failure_logger.py # 失败日志 + 敏感信息 redact
│   │   ├── llm_stream.py         # 流式数据结构与双协议解析
│   │   ├── stream_progress.py    # 流式 Token 进度回调工厂（章节/总结/风格共用）
│   │   ├── error_taxonomy.py     # 厂商错误分类与重试策略（RETRY_POLICY / classify_*）
│   │   ├── error_types.py        # 错误判定共享类型
│   │   ├── prompt_builder.py     # 提示词构建（system 11 条规则 + 9 路上下文注入）
│   │   ├── knowledge_base.py     # 知识库管理器（增量合并 / 压缩 / 快照隔离）
│   │   ├── memory_state.py       # 内存状态容器（结果 + 知识库 + 断点恢复）
│   │   ├── style_analyzer.py     # 风格分析（单章 22 项统计 + 8 维 LLM 语义）
│   │   ├── anchor.py             # 伏笔锚点：把模型引文钉回原文坐标
│   │   ├── citations.py          # 引用校验：核对"第 N 章第 a–b 字"是否属实
│   │   ├── evidence.py           # 本地证据取材：把"全书"换成几段原文窗口
│   │   ├── file_processor.py     # 章节文件扫描与编码读取
│   │   └── moderation.py         # 内容审核拦截识别
│   ├── services/                 # 16 个单文件模块 + final_summary/ 4 层无环子包
│   │   ├── queue_service.py      # 分析运行编排（AnalysisService 单例）
│   │   ├── queue_manager.py      # 队列状态机、扫描、模型预检与持久化
│   │   ├── analysis_stats.py     # 会话统计累加
│   │   ├── pipeline_events.py    # WS 事件翻译层
│   │   ├── final_summary/        # 最终总结四层子包（prompts / parsers / atlas_extractor -> catalog_mixin / checkpoint_mixin / recheck_mixin -> runner -> __init__）
│   │   ├── recheck_v3.py         # 带证据的伏笔复检 V3 多跳证据链（默认）
│   │   ├── recheck_v2.py         # 带证据的伏笔复检 V2 批量核对
│   │   ├── recheck_resolution.py # 原文证据校验与全自动裁决核
│   │   ├── splitter_service.py   # 切分引擎（18 条章正则 + 5 条卷正则 + 去重/合并/拆分）
│   │   ├── workspace_service.py  # 工作区与归档管理
│   │   ├── book_service.py       # 书名 → 目录映射
│   │   ├── summary_service.py    # 总结任务生命周期
│   │   ├── usage_ledger.py       # 持久用量账本（usage_state.json）
│   │   └── viz_service.py        # 可视化与全书图谱只读视图
│   ├── api/                      # 10 个路由模块 + auth + ws
│   │   ├── routes_analysis.py    # 分析控制、队列、日志
│   │   ├── routes_books.py       # 书目与结果查询（含 style_profile / 人物谱系）
│   │   ├── routes_summary.py     # 总结控制
│   │   ├── routes_viz.py         # 可视化（时间线 / 全书语义图谱 atlas）
│   │   ├── routes_workspace.py   # 工作区与归档
│   │   ├── routes_splitter.py    # 切分
│   │   ├── routes_aggregate.py   # 聚合缓存与 Excel
│   │   ├── routes_foreshadow.py  # 伏笔分类元数据
│   │   ├── routes_prompt.py      # 提示词预览
│   │   ├── routes_settings.py    # 配置读写 / 预设 / 模型探测
│   │   ├── auth.py               # X-Session-Token 鉴权
│   │   └── ws.py                 # WebSocket /ws/progress（25s 心跳 + Origin 白名单）
│   ├── config/
│   │   ├── constants.py          # 全部硬编码默认值 + 50 类伏笔定义（7 组）
│   │   ├── paths.py              # 代码根 / 资源根 / runtime 运行目录解析真源
│   │   ├── settings.py           # APIConfig / AnalysisConfig / GUIConfig / AppConfig + ConfigManager
│   │   └── presets.py            # 厂商预设
│   ├── models/
│   │   ├── analysis_result.py    # AnalysisResult 结构化输出
│   │   └── knowledge.py          # KnowledgeBase 跨章记忆
│   ├── utils/
│   │   ├── json_utils.py         # JSON 提取 + 9 步容错解析 + 原子保存
│   │   ├── aggregate_utils.py    # 按需指纹聚合缓存（10 类分类文件 + 1 个全量）
│   │   ├── foreshadow_ledger.py  # 伏笔账本状态机
│   │   ├── character_card_generator.py  # 人物谱系与别名归并角色卡生成
│   │   ├── excel_export.py       # Excel 导出
│   │   ├── export_utils.py       # Markdown 导出
│   │   ├── text_utils.py         # 编码检测 / 文本工具
│   │   └── docx_extract.py       # DOCX 正文抽取（纯标准库）
│   └── tests/                    # 14 个领域测试文件 + conftest / pytest_isolation / pytest_ssl_cache + fixtures 语料
├── frontend/src/                 # Vue 3 + TypeScript（含 __tests__/ 下 5 个聚合测试文件）
│   ├── pages/                    # 9 个主页面（router.ts）
│   │   ├── QueuePage.vue         # 分析队列
│   │   ├── SettingsPage.vue      # 设置（API/模型调用与并发/策略/高级区 3 大纯配置分组）
│   │   ├── CharacterCardPage.vue # 人物谱系（ECharts 关系网 + 别名检索 + 角色卡）
│   │   ├── SummaryPage.vue       # 最终总结（含文风量化栏与 Excel 导出）
│   │   ├── SplitterPage.vue      # 小说切分
│   │   ├── WorkspacePage.vue     # 工作区与归档
│   │   ├── WorldStagePage.vue    # 世界舞台（换地图阶段流 + 多世界树）
│   │   ├── StatsPage.vue         # 统计与诊断（累计账本 / 会话 / 历史 / 日志 / Prompt 预览）
│   │   └── TimelinePage.vue      # 时间线
│   ├── components/               # 14 个（AppLayout / BookSelector / ChapterDetailPanel /
│   │                             #   ChapterValue / ConfirmDialog / CountUp / DiscoveryFeed /
│   │                             #   Icon / LaneView / LogConsole / ProgressBar / RunDashboard /
│   │                             #   ThroughputChart / ToggleSwitch）
│   ├── composables/              # useLocale / useLogStore / useRipple / useSelectedBook / vTooltip
│   ├── api/                      # client.ts / sessionToken.ts / useProgressSocket.ts
│   ├── utils/                    # 9 个纯函数模块（laneRegistry / markdown / queueFeed /
│   │                             #   settingsForm / splitterView / summaryLanes /
│   │                             #   summaryProgress / theme / workspaceView）
│   ├── router.ts                 # 1 个根布局 + 9 条主路由 + 4 条兼容重定向
│   └── main.css                  # Win11 Fluent 设计系统（--win-* 变量）
├── desktop/                      # 桌面入口与打包资产
│   ├── desktop.py                # pywebview 桌面入口（单实例锁 + 窗口控制 + 崩溃诊断）
│   ├── desktop.spec              # PyInstaller 单文件打包配置
│   ├── build_exe.bat             # 单文件 EXE 打包脚本
│   ├── build_frontend.bat        # 前端本地构建并回写 dist 脚本
│   ├── config.example.json       # 默认配置模板
│   └── _welcome_workspace.md     # 工作区首次启动欢迎页模板
├── runtime/                      # 源码模式统一运行目录（.gitignore 忽略：config.json / workspace / logs / state）
├── scripts/                      # 测试、打捞验收与测量脚本
├── docs/                         # 文档（见 docs/README.md）
├── agent.md                      # 本文件
├── README.md                     # 项目说明
├── pytest.ini                    # pytest 根配置
├── run_desktop.bat               # Windows 桌面端启动入口
├── run_dev.bat                   # Windows 开发模式启动入口
└── requirements.txt              # Python 依赖
```

---

## 3. 技术栈详情

### 后端
| 组件 | 版本/说明 |
|---|---|
| Python | 3.11+ |
| FastAPI | 异步框架，桌面端动态端口（desktop.py `_find_free_port`）；开发模式 8000 |
| OpenAI SDK | `AsyncOpenAI` 支持任何兼容 API |
| Anthropic SDK | `AsyncAnthropic` 支持 Claude 系列 |
| pywebview | 桌面 WebView2 壳 |
| openpyxl | Excel 导出（懒加载） |
| json5 / json_repair | JSON 容错解析 |
| pytest | 默认收集 `backend/tests/`（14 个领域测试文件）；数量以 `pytest --collect-only -q` 为准 |

### 前端
| 组件 | 版本/说明 |
|---|---|
| Vue | 3.5（Composition API） |
| TypeScript | 5.7 |
| Vite | 6 |
| Tailwind CSS | 4 |
| Vue Router | 4 |
| 设计系统 | Win11 Fluent（`--win-*` CSS 变量） |

---

## 4. 核心架构

### 4.1 数据流全景

```
.txt / .docx 文件
  → _detect_and_read（单一文本入口：txt 走编码检测，docx 走 Word 正文抽取）
  → splitter_service（切章，18 种章格式 + 5 种卷格式正则，PG 头截断 + TOC 去重）
  → blocks/{index:04d}.txt
  → pipeline（串行预热 → 流式并发 → 失败补跑）
  → MemoryState（全内存 IO）
  → output/chapter_N_result.json
  → output/rolling_summary.json
  → 最终保存（merge_results → 裁剪 → 写盘）
      ↓
  → final_summary（4 阶段总结与语义图谱）
      → 分卷摘要 + 伏笔调和
      → 全书伏笔复检
      → 风格分析
      → 最终报告 + 语义图谱并发提取
  → final_summary_report.md + output/story_atlas.json
  → aggregate_utils → output/*_aggregated.json（10 类分类 + 1 个全量）
  → style_analyzer → style.md
  → excel_export → analysis_data.xlsx
```

### 4.2 分析流水线（pipeline.py）

**阶段一：串行预热**
- 前 `concurrency` 个块串行处理
- 建立初始 KnowledgeBase，保证后续块读不到未来章节知识

**阶段二：流式并发**
- `asyncio.Semaphore` 限流 + `as_completed` 流式调度
- 每 N 块触发一次 rolling summary（后台 asyncio.Task）
- 每 `checkpoint_interval` 批触发检查点落盘
- `state.get_kb_snapshot(chapter_limit)` 实现 KB 快照隔离

**阶段三：失败补跑**
- 最多 3 轮串行重试失败块
- 每轮只重试上一轮标记为失败的块
- 致命错误类（认证、计费、上下文超限、参数错）不进入补跑

### 4.3 LLM 客户端（llm_client.py + llm_probe/llm_failure_logger/llm_stream）

**重试链（两级 + 429 特例）：**
1. 温度退火：`temperature_max_retries` 次（默认 3），温度 = max(0, 0.1 − attempt × 0.05)，末轮降到 0
2. 指数退避：`backoff_max_retries` 次（默认 1），等待 = min(2^N, 60s)
3. 429 限流：额外 3 轮，尊重 Retry-After 头

**stop 类立即停（error_taxonomy）：** AUTH / BILLING / CONTEXT_LENGTH / BAD_REQUEST 首次命中即停
（`_error_action()` 按 RETRY_POLICY 决策）；审核 MODERATION_INPUT / structured 首次短路、
text 置信维持连续 3 次。pipeline 补跑轮剔除 fatal 块（`_fatal_blocks`）。

**三分竞争（单次 chat 调用）：**
- API 任务 / 停止事件等待 / 硬超时（timeout + 30s）
- `asyncio.wait(FIRST_COMPLETED)` 任一先完成即返回

**双协议支持：**
- `detect_provider()`：按 base_url/api_key/model 自动检测 OpenAI 或 Anthropic
- Anthropic：提取 system 消息、合并相邻同角色消息、拼接 text block
- OpenAI：`extra_body` 注入思考控制、`response_format` json_object 模式

**思考链清洗：** `_strip_thinking()` 移除 `<think>...</think>`、`<reasoning>...</reasoning>` 等标签

### 4.4 Prompt 构建（prompt_builder.py）

**KV Cache 布局（变化频率低→高）：**
```
system: SYSTEM_PROMPT + 50类伏笔表 + 已知世界观 + 主题元素  ← 恒定前缀
user:   时间线 + 角色状态 + 角色关系 + 已验证事实 + 前情摘要 + 伏笔网络 + 故事历史 + 章节正文
```

**九路上下文注入（system 4 路 + user 5 路，不含章节正文）：**
1. 完整故事历史（结构化滚动总结 + 近期逐章 timeline）
2. 前情摘要（最近 N 章）
3. 当前时间线
4. 角色当前状态
5. 角色关系
6. 已验证事实
7. 已知世界观
8. 主题元素
9. 伏笔网络

### 4.5 知识库管理（knowledge_base.py）

**三种 KB 构建方式：**
1. `merge_results(output_dir)` — 全量合并（最终保存用，带裁剪）
2. `build_temp_knowledge(output_dir, chapter_limit)` — 带缓存的临时 KB
3. `_merge_incremental(kb, new_results)` — 增量更新（缓存命中时）

**内存缓存（`_temp_kb_cache`）：**
- key: `(output_dir, chapter_limit)`
- value: `(fingerprint, results, kb)`
- 支持近邻增量：找缓存中 limit 最大且 ≤ 当前值的条目
- 精确匹配/指纹比对/mtime 检测变化文件

### 4.6 内存状态（memory_state.py）

**核心数据：**
```python
self.results: Dict[int, AnalysisResult]    # {章号: 结果}
self.kb: KnowledgeBase                     # 增量维护的 KB
self.rolling: dict                         # 滚动总结数据
self._flushed_chapters: Set[int]           # 已落盘的章号集合
```

**KB 快照隔离（`get_kb_snapshot(chapter_limit)`）：**
- 三级缓存策略：精确命中 / 增量扩展 / 从子集重建
- 只允许请求 limit ≥ 缓存 limit 才扩展，防止并发低章号块读到未来知识

**乱序保护（`_merge_one`）：**
- `_kb_last_chapter` 跟踪最大已合并章号
- 低章号迟到结果只合并非进度内容（事实/角色/世界等），跳过 timeline/recent_summaries

### 4.7 最终总结（backend/services/final_summary/）

**4 阶段：**
1. **分卷摘要 + 伏笔调和**（每批两次 LLM 调用）：
   - 第一次：Markdown 卷摘要
   - 第二次：JSON reconciliation（伏笔状态调和）
   - 卷摘要一完成即落盘（断点续跑）
2. **全书伏笔复检**（剩余 active 伏笔，40个/子批）
   - **复检超限保护**：全量卷摘要超 `RECHECK_FULLTEXT_BUDGET_CHARS`（默认 15 万字符）时按伏笔埋设章砍埋设前卷（召回无损），仍超则跳过该组不调用 LLM
3. **风格分析**：`extract_style_profile` → 注入最终报告
   - 风格分析与阶段 1 并行启动（数据无依赖：只需 blocks_dir），阶段 2 完成后 await 收结果
4. **最终报告**：卷摘要 + 伏笔上下文 + 审计 + 风格 → `final_summary_report.md`

**全局 LLM 并发上限 Sem：**
- 4 阶段 + 风格提取**共用** `self._llm_sem = asyncio.Semaphore(self.concurrency)`，硬上限 = `self.concurrency`（用户配置 4/9/20 均不超）
- 4 个 `_call_llm_*`（summary/reconciliation/recheck/final）+ `_run_style_extraction` 内部均通过 `_acquire_llm_slot()` / `_release_llm_slot()` 包裹 LLM 调用
- in-flight 计数器 + peak 监控；`run()` 末尾日志打印 `peak in-flight = X / concurrency = N`，超限打 ERROR
- 阶段 1 批内 summary 与 reconciliation 各自独立 acquire（checkpoint IO 不再抱死 Sem 槽）

**伏笔总表构建：**
```
收集 all_clues → type 归一化(50类直接用 / per-book映射 / fallback) 
  → 三维过滤(importance/confidence/category) → 语义去重 
  → 综合排序(imp×100 + conf×10 + evidence_count) → 分层截断(高500/中200)
```

**断点续跑：** `final_summary_checkpoint/` 目录，`volume_{idx}.md` + `recon_{idx}.json` + `manifest.json` + results 指纹失效校验

### 4.8 滚动总结（pipeline.py 中的 _async_rolling）

**五层截断策略：**
1. `paradigm_layers`（≤5）— 范式层
2. `milestones` — FIFO 淘汰 + 锁定首条 + 范式层首条
3. `momentum` — 归档触发后清空
4. `causal_chains`（≤5）— 因果链
5. `milestone` 单条（≤50字）

**势头归档：** 跨阈值/跨窗口 → LLM 压缩为一条里程碑

---

## 5. 后端模块详解

### 5.1 核心层（backend/core/，23 个模块）

| 模块 | 职责 |
|---|---|
| `pipeline.py` | 全书分析总调度：切块、预热、并发、补跑、checkpoint、滚动总结 |
| `analyzer.py` | 单章分析引擎：拼提示词、调用 LLM、校验与解析响应 |
| `llm_client.py` | LLM 客户端：OpenAI / Anthropic 双协议，温度退火 + 指数退避，流式与非流式 |
| `llm_probe.py` | 探测端点认哪个"关闭思考"参数 |
| `llm_failure_logger.py` | 失败日志（每行 JSON）+ 敏感信息脱敏 `redact()` |
| `llm_stream.py` | 流式数据结构与双协议事件解析 |
| `stream_progress.py` | 流式 token 进度回调工厂（章节 / 总结 / 风格共用） |
| `error_taxonomy.py` | 厂商错误分类与重试策略（`RETRY_POLICY`、`classify_exception`、`classify_message`） |
| `error_types.py` | 错误判定共享类型（枚举与纯数据） |
| `prompt_builder.py` | 提示词构建：system 11 条规则 + 9 路上下文注入 |
| `knowledge_base.py` | 磁盘知识库的加载、保存、增量合并与压缩 |
| `memory_state.py` | 内存状态容器：全部结果 + 增量知识库 + 断点恢复 |
| `style_analyzer.py` | 写作风格：单章 22 项统计 + 8 维 LLM 语义 |
| `anchor.py` | 伏笔锚点：把模型给的引文钉回原文坐标 |
| `citations.py` | 引用校验：核对"第 N 章第 a–b 字"是否属实 |
| `evidence.py` | 本地证据取材：把"全书"换成几段原文窗口 |
| `file_processor.py` | 章节文件扫描、编码探测与读取 |
| `moderation.py` | 内容审核拦截识别（provider 无关） |

### 5.2 服务层（backend/services/，16 个单文件模块 + final_summary/ 子包）

| 模块 | 职责 |
|---|---|
| `queue_service.py` | 分析运行编排（`AnalysisService` 单例） |
| `queue_manager.py` | 队列状态机、扫描、模型预检与持久化 |
| `analysis_stats.py` | 会话统计累加（token、每章记录、重试成本） |
| `pipeline_events.py` | WS 事件翻译层：流水线回调 → 广播消息 |
| `final_summary/` | 最终总结四层子包（`prompts` / `parsers` / `atlas_extractor` / `catalog_mixin` / `checkpoint_mixin` / `recheck_mixin` / `runner`）与三阶段断点续跑 |
| `recheck_v3.py` | 带证据的伏笔复检 V3 多跳证据链引擎（默认） |
| `recheck_v2.py` | 带证据的伏笔复检 V2 批量核对引擎（可选） |
| `recheck_resolution.py` | 原文证据校验与全自动裁决核 |
| `splitter_service.py` | 切分引擎：18 条章节正则 + 5 条卷正则，去重 / 合并 / 超章拆分 / 书名推断 |
| `workspace_service.py` | 工作区与归档管理 |
| `book_service.py` | 书名 → `workspace/{书名}/` 目录映射与风格画像读取 |
| `summary_service.py` | 总结任务生命周期 |
| `viz_service.py` | 可视化数据聚合（时间线与全书语义图谱只读视图） |

### 5.3 工具层（backend/utils/，8 个模块）

| 模块 | 职责 |
|---|---|
| `json_utils.py` | JSON 提取、9 步容错解析、原子保存 |
| `aggregate_utils.py` | 按需指纹聚合缓存（`ensure_aggregated_cache`：10 类分类文件 + 1 个全量） |
| `foreshadow_ledger.py` | 伏笔账本状态机 |
| `character_card_generator.py` | 人物谱系与别名归并角色卡生成 |
| `excel_export.py` | Excel 导出 |
| `export_utils.py` | Markdown 导出 |
| `text_utils.py` | 编码检测与文本工具 |
| `docx_extract.py` | DOCX 正文抽取（纯标准库） |

### 5.4 API 层（backend/api/，10 个路由模块 + auth + ws）

全部挂载在 `/api` 前缀下，端点清单见 [docs/api.md](./docs/api.md)。

| 模块 | 职责 |
|---|---|
| `routes_analysis.py` | 分析控制、队列、日志下载 |
| `routes_books.py` | 书目、风格画像、人物谱系与结果查询 |
| `routes_summary.py` | 总结控制 |
| `routes_viz.py` | 可视化（时间线 / 全书语义图谱 `atlas`） |
| `routes_workspace.py` | 工作区与归档 |
| `routes_splitter.py` | 切分 |
| `routes_aggregate.py` | 聚合缓存与 Excel |
| `routes_foreshadow.py` | 伏笔分类元数据 |
| `routes_prompt.py` | 提示词预览 |
| `routes_settings.py` | 配置读写、预设、模型探测 |
| `auth.py` | `X-Session-Token` 鉴权依赖 |
| `ws.py` | `/ws/progress`：Origin 白名单 + 25 秒心跳 |

---

## 6. 前端模块详解

### 6.1 API 层

| 文件 | 职责 |
|---|---|
| `api/client.ts` | REST 客户端与 TypeScript DTO + 统一 `X-Session-Token` 注入 |
| `api/sessionToken.ts` | 会话 token 单例（pywebview js_api 获取） |
| `api/useProgressSocket.ts` | WebSocket 单例连接 + 协议镜像类型（与 `backend/ws_events.py` 对应） |

### 6.2 状态管理

没有独立的 store 目录：全局日志状态放在 `composables/useLogStore.ts`，其余状态由各页面
的 `ref` / `computed` 管理。`composables/` 下另有 `useLocale.ts`、`useRipple.ts`、
`useSelectedBook.ts`、`vTooltip.ts`。

### 6.3 设计系统（main.css）

Win11 Fluent 风格，颜色 / 圆角 / 阴影 / 间距统一走 `--win-*` 变量，禁用 iOS 硬编码色值
与 `backdrop-filter`。

### 6.4 页面（9 个主页面）

| 页面 | 职责 |
|---|---|
| `QueuePage.vue` | 分析队列：书籍队列、启停分析、进度车道、token 统计、日志下载 |
| `SettingsPage.vue` | 设置：API / 模型调用与并发 / 流程策略 / 高级参数与伏笔保留类别（3 大纯配置分组） |
| `CharacterCardPage.vue` | 人物谱系：核心关系网、别名检索与归并角色卡详情 |
| `SummaryPage.vue` | 最终总结：总结启停、阶段进度、风格量化概览、全书脉络报告与一键导出 Excel |
| `SplitterPage.vue` | 切分预览、批量切分、书名推断 |
| `WorkspacePage.vue` | 工作区与归档列表、切分还原、归档与删除 |
| `WorldStagePage.vue` | 世界舞台：主线换地图阶段流与 ECharts 多世界层级拓扑树 |
| `StatsPage.vue` | 统计与诊断：跨进程累计账本、会话统计、按书历史、实时日志与 Prompt 预览 |
| `TimelinePage.vue` | 时间线与伏笔分类排名 |

### 6.5 组件（14 个）

`AppLayout.vue`（根布局）、`BookSelector.vue`、`ChapterDetailPanel.vue`、
`ChapterValue.vue`、`ConfirmDialog.vue`、`CountUp.vue`、`DiscoveryFeed.vue`、`Icon.vue`、
`LaneView.vue`、`LogConsole.vue`、`ProgressBar.vue`、`RunDashboard.vue`、`ThroughputChart.vue`、`ToggleSwitch.vue`。

### 6.6 前端关键模式

- **纯函数外置**：`utils/` 下 9 个模块承载车道注册表、自研 Markdown、队列状态机、总结进度、设置表单、
  切分视图、主题、工作区视图等逻辑，便于单测。
- **自研 Markdown 渲染**：`utils/markdown.ts` 零依赖渲染 Markdown → HTML（已先做 `escapeHtml()` 转义）。
- **请求超时**：`client.ts` 默认 30 秒超时，重活请求单独放宽到 120–300 秒。

---

## 7. 数据模型

### 7.1 AnalysisResult（backend/models/analysis_result.py）

单章分析的结构化输出：核心事件、人物弧光、伏笔、地点、空间关系、未解问题、新线索、
世界观、长线主线、长上下文洞见、伏笔漏洞等。`from_dict()` 做字段清洗与类型强转。

### 7.2 KnowledgeBase（backend/models/knowledge.py）

跨章记忆容器：时间轴、压缩主线、最近摘要、人物状态、人物关系、伏笔网络、世界观、
已验证事实、长线主线、主题要素、节奏追踪等。`memory_state.py` 负责增量合并与裁剪。

### 7.3 配置结构（backend/config/settings.py）

`APIConfig` / `AnalysisConfig` / `GUIConfig` / `AppConfig` 四个 dataclass，加载时经
`_coerce_fields` 过滤未知字段并做类型强转（字符串 `"4"` → `4`）；配置文件解析失败会置
损坏标志，修复前拒绝写入，避免默认配置覆盖真实 API Key。

字段清单与默认值见 [docs/configuration.md](./docs/configuration.md)。

---

## 8. 常用命令

### 后端

```bash
# 安装依赖
pip install -r requirements.txt

# 启动后端（开发）
python -m uvicorn backend.app:app --reload --port 8000

# 运行测试（统一入口，详见 docs/testing.md）
python scripts/run_tests.py                 # 全仓验证（后端 pytest + 前端 vitest，数秒完成）
python scripts/run_tests.py --no-frontend   # 仅跑后端测试（约 4~5 秒）
python scripts/run_tests.py backend/tests/test_smoke.py --no-frontend
python scripts/run_tests.py --no-frontend -- -k recheck
# （测试全程运行在 cwd/NOVEL_ROOT/NOVEL_RUNTIME_DIR 隔离的会话临时根：backend/tests/conftest.py +
#   backend/tests/pytest_isolation.py 在任何业务 backend.* import 前生效，真实 runtime/ 配置/日志/workspace
#   零写入；守卫见 backend/tests/test_smoke.py。不要在测试里依赖真实工作区数据。）

# 对齐 CI 的无缓存单进程全量校验
python -m pytest -p no:cacheprovider -o addopts=""
```

### 前端

```bash
cd frontend

# 安装依赖
npm install

# 开发模式
npm run dev

# 类型检查 + 生产构建
npm run build
```

### 桌面运行

- Windows 下直接运行根目录 `run_desktop.bat`
- 前端构建产物由 `desktop/build_frontend.bat` 生成到 `frontend/dist`
- `desktop/desktop.py`：pywebview 启动 → uvicorn 后台线程 → 端口发现 → UTF-8 编码修复
- 源码模式所有用户配置、日志与工作区统一落盘在 `runtime/`；打包 EXE 模式落于 EXE 同级目录

---

## 9. 硬性约束（必须遵守）

### 9.1 密钥与数据安全
- `runtime/config.json` 含真实 API Key，**禁止提交、打印、写入日志或纳入任何输出**
- `/runtime/` 以及 `config.json`、`queue_state.json`、`usage_state*.json`、`*.log`、`app.lock`、`.bak_*` 均已被 `.gitignore` 忽略
- 所有可能含 key 的错误日志**必须过 `redact()`**（`llm_failure_logger.redact`）。两重防御：
  1. `FailureLogger.record_failure` 入口对 `error_message` 自动 redact
  2. `app.py` 的 `_SensitiveFieldsFilter` 对所有落 `analyzer.log` 的 record 二次过滤
  - **fail-open 原则**：redact 抛异常时返回原文（宁可漏脱敏也不丢日志）
  - 未来加新错误字段时**必须**也走 redact，否则可能漏脱敏

### 9.2 行尾符
- 仓库中存在大量历史 CRLF/LF 差异，**不要批量转换**
- 查看真实改动：`git diff -w -- <file>`

### 9.3 WebView2 兼容
- **禁止** `backdrop-filter` / `-webkit-backdrop-filter`
- 动画使用 DOM 元素 + `transform` / `opacity`
- Win11 Mica 风格使用纯色/低透明层

### 9.4 前端设计系统
- 所有颜色/圆角/阴影/字体/间距必须引用 `--win-*` 变量或 Tailwind 工具类
- 禁止硬编码 iOS 色值
- Win11 强调色克制使用

### 9.5 不要破坏业务
- 前端改动只允许调整视觉层，不得改变 props、事件、路由、API 调用、数据结构
- 后端改动除非任务明确要求，否则默认不触碰

---

## 10. 验证清单

### 前端改动
```bash
cd frontend && npm run build

# 不应有 backdrop-filter
grep -RIn "backdrop-filter" frontend/src || true

# 不应有旧 iOS 硬编码色
grep -RIn "#007AFF\|#0A84FF\|#5AC8FA\|#FF3B30" frontend/src || true

# 不应有 sweep/shimmer 残留
grep -RIn "sweep\|shimmer" frontend/src || true
```

### 后端改动
```bash
# 仅跑后端测试（约 4~5 秒）
python scripts/run_tests.py --no-frontend
python scripts/run_tests.py backend/tests/test_smoke.py --no-frontend

# 全仓验证（后端 pytest + 前端 vitest）
python scripts/run_tests.py --no-frontend -- -k recheck
python scripts/run_tests.py


# 类型检查（如有 mypy）
mypy backend/
```

---

## 11. Agent 行为准则

1. **先读后改**：修改前先查看目标文件和相关上下文
2. **小步提交**：按文件/组件/页面拆分，避免一次性大改
3. **保持行尾**：不要批量格式化
4. **构建验证**：前端改完必须 `npm run build` 通过
5. **测试验证**：后端改完必须 `pytest` 通过
6. **不启动常驻服务**：除非任务明确要求
7. **遇到模糊需求先确认**：不要擅自扩大范围
8. **不修改用户明确要求"不要修改"的内容**
9. **文档只写当前状态**：改代码时同步修正受影响章节，但不要在文档里写改动历史、
   批次编号或日期标记；旧决策作废后只保留新决策

---
## 12. i18n 接口预留

当前状态：**只搭骨架，未翻译**。vue-i18n 已安装，locale 文件是空对象，所有 UI 仍硬编码中文。

接口已就位：

- 后端 `GUIConfig.language: str = "zh-CN"`（写进 `config.json`，后端目前没有消费点）
- 前端 `vue-i18n@^10` 已 init（`useLocale()` composable 可用）
- 前端 `AppConfigDto.gui.language?: string`

启用 i18n 时的约定：

1. 新页面 / 新功能的字符串用 `t('namespace.element')` 包裹
2. 翻译键命名：页面级 `queue.*` / `settings.*` / `summary.*` / `splitter.*` / `viz.*`；
   组件级 `component.button.start`、`component.modal.confirm`；公共 `common.loading`、
   `common.cancel`、`common.confirm`
3. `useLocale()` 必须在 setup 上下文调用（vue-i18n 的 inject 限制）
4. 持久化语言需要扩 `routes_settings.py` 的 `is_running` 护栏——它当前主要拦截分析与总结冲突，设置写入同样需要避开 `summary` 运行期间
5. `useLocale()` 对 `zh_CN` 这类下划线写法不会拒，加来源校验时需要覆盖

---

## 13. 小说书名推断合并

splitter 内部有两条平行的书名推断路径：

- `infer_book_name(path)` 从**文件名**（如"《书名》（精校版）.txt"）推断 —— 权威
- `_extract_metadata` 从**正文前 8 行**推断 —— 会把正文首句当成书名

规则：

- `save_to_workspace` 把 `book_name` 沿 `save_split → split_text → _extract_metadata`
  透传为 `external_title`；`_extract_metadata` 收到 `external_title` 时直接返回
  `{"title": external_title}`，跳过正文推断；只有直接调用 `split_text`（不经过保存）
  时才走正文推断
- 兜底：`detect_book_name._looks_valid` 拒绝含 `。` / `？` / `！` 的标题
  （那是被误当成书名的正文句子），fallback 到 `output_dir.parent.name`
- 不重切已分析的书——重切会清掉 `chapter_N_result.json`


## 伏笔打捞与验收契约

- `backend/common/closure.py` 是预登记校验、分支引文核验和关闭条件归约的共享实现；`scripts/run_salvage.py` 保留历史函数导入路径。
- 打捞为每个通过候选导出 `acceptance_input_<运行戳>.json`，包含相同预登记与原文上下文，交 `scripts/acceptance_gate.py` 预筛。验收不改生产账本。
- 验收分别判断关闭条件与答案忠实性；历史标签必须绑定当前 sample_id 才进入评测分母。未知/过期标签不自动转换为人工真值。
- 旧考卷可用验收器 `--prepare-only --blocks <目录>` 离线冻结。具体协议与用法见 `docs/自动验收器.md`。
- `backend/common/claim_evidence.py` 提供可选的逐断言证据契约（验收器 `--claim-audit`）；只核对声明一致性与定位，不把模型判断当真值。
- `backend/common/claim_evidence_v2.py` 提供固定答案片段与正交限定维度（`--claim-contract claims_v2`）；与第一版共用引文定位，仍为默认关闭的试验模式。
