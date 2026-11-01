# Agent 工作指南（小说智能分析器）

> **⚠️ 重要提醒：本文件是项目的核心知识库。任何 Agent 在完成修改后，必须同步更新本文件中受影响的章节（模块说明、数据结构、API 端点、配置项等），确保后续 Agent 能快速理解最新代码状态。**

本文件面向在 `小说分析器` 仓库中执行任务的 AI Agent，提供项目背景、架构全景、模块详解、常用命令、硬性约束、当前状态与验证方法。

---

## 1. 项目概述

基于 LLM 的长篇中文网络小说深度分析工具。核心流程：`txt 切分 → 逐章分析 → 滚动总结 → 全书总结`。

- **前端**：Vue 3.5 + TypeScript 5.7 + Vite 6 + Tailwind 4，Win11 Fluent 设计系统
- **后端**：FastAPI（异步），Python 3.11+
- **桌面壳**：pywebview + WebView2（Windows）
- **LLM 兼容**：任何 OpenAI 兼容 API（8+ 厂商预设），以及 Anthropic 协议
- **测试**：后端约 827 个用例 / 73 个测试文件（2026-10-15 累计，含第三批清理 Step0-R5 新增 10 个测试文件与测试隔离基建；数量以 `pytest --collect-only -q` 实时为准）；前端 249 个用例 / 32 个测试文件 + vitest coverage（`npm run test:coverage`，2026-09-30）

---

## 2. 仓库结构（完整）

```
.
├── backend/                      # FastAPI 后端
│   ├── app.py                    # FastAPI 工厂（CORS、静态文件、路由注册、生命周期）
│   ├── ws_events.py              # WS 事件协议权威定义（WSType/WSState/PipelineStatus + payload 契约 + 工厂）
│   ├── progress_hub.py           # 进度广播中枢（每连接队列/慢消费者分级驱逐/token_delta 节流）
│   ├── core/                     # 核心分析引擎
│   │   ├── pipeline.py           # 主分析流水线（三阶段）
│   │   ├── llm_client.py         # LLM 客户端主类（双协议调用 + 三层重试链 + 三分竞争）
│   │   ├── llm_probe.py          # 禁用思考参数探测（probe_thinking_params 多模式检测）
│   │   ├── llm_failure_logger.py # API 失败日志记录器（FailureLogger 单例，RotatingFileHandler）
│   │   ├── error_taxonomy.py     # 厂商错误码分类体系（ErrorClass/RETRY_POLICY/classify_exception/classify_message，2026-10-25）
│   │   ├── llm_stream.py         # 流式数据结构（StreamChunk/StreamResult）+ 双协议事件/响应解析
│   │   ├── stream_progress.py    # 流式 Token 进度回调工厂（make_stream_progress_callback，章节/总结/风格三路径共用，2026-10-15 抽取）
│   │   ├── analyzer.py           # 单章分析引擎
│   │   ├── prompt_builder.py     # Prompt 构建器（9路上下文注入）
│   │   ├── knowledge_base.py     # 知识库管理器（增量合并、缓存、快照隔离）
│   │   ├── memory_state.py       # 内存状态容器（全内存IO、检查点；检查点落盘均走 safe_save_json 原子写）
│   │   ├── style_analyzer.py     # 风格分析器（22统计+8语义维度）
│   │   └── moderation.py         # 内容审核识别
│   ├── services/                 # 业务服务层
│   │   ├── queue_service.py      # 分析运行编排层（AnalysisService 单例；启动扫描移出 __init__，改由 app lifespan 经 to_thread 触发）
│   │   ├── queue_manager.py      # 队列管理：QueueItem/QueueManager（状态机/工作区扫描/API 预检/持久化）
│   │   ├── analysis_stats.py     # 分析会话统计累加（AnalysisStats：token 五件套/每章记录/重试成本/冻结耗时）
│   │   ├── pipeline_events.py    # WS 事件翻译层（pipeline 回调工厂 + discovery 提取 + ETA 格式化）
│   │   ├── final_summary.py      # 最终总结执行器（4阶段 + 断点续跑）
│   │   ├── splitter_service.py   # 小说切章服务（i18n：15 章 + 5 卷正则，支持 PG/古典/裸 Roman 风格；2026-09-28 起在 `_detect_and_read` 单一入口支持 txt/docx 双格式）
│   │   ├── workspace_service.py  # 工作区/归档管理
│   │   ├── book_service.py       # 书名目录映射
│   │   ├── summary_service.py    # 总结任务生命周期
│   │   ├── style_service.py      # 风格分析任务生命周期
│   │   ├── viz_service.py        # 可视化数据聚合
│   │   └── location_normalization_service.py  # 地点归一化任务编排（单例；start/stop/status，进度经 ProgressHub 广播）
│   ├── api/                      # REST 路由 + WebSocket
│   │   ├── routes_analysis.py    # 分析控制 + 队列管理（20端点；delete_book 先移回收站成功再改队列）
│   │   ├── routes_summary.py     # 总结控制（3端点）
│   │   ├── routes_books.py       # 书籍数据（10端点）
│   │   ├── routes_viz.py         # 可视化数据（3端点）
│   │   ├── routes_workspace.py   # 工作区管理（5端点）
│   │   ├── routes_splitter.py    # 切章（4端点）
│   │   ├── routes_aggregate.py   # 聚合 + Excel 导出（4端点）
│   │   ├── routes_foreshadow.py  # 伏笔分类（1端点）
│   │   ├── routes_location_normalization.py  # 地点归一化任务控制（4端点；start/stop/status/result）
│   │   ├── routes_prompt.py      # Prompt 预览（1端点）
│   │   ├── routes_settings.py    # 配置管理（6端点）
│   │   ├── auth.py               # session token 鉴权（D 方案，PR-1 修复，2026-09-10）
│   │   └── ws.py                 # WebSocket /ws/progress
│   ├── config/
│   │   ├── settings.py           # 三层 dataclass 配置 + ConfigManager（save 走 safe_save_json 原子写）
│   │   └── constants.py          # 所有硬编码默认值 + 50类伏笔定义
│   ├── models/
│   │   ├── analysis_result.py    # AnalysisResult 9类结构化输出
│   │   └── knowledge.py          # KnowledgeBase 12类跨章记忆
│   ├── utils/
│   │   ├── json_utils.py         # 9级 JSON 容错修复链（含全角归一）
│   │   ├── foreshadow_ledger.py  # 伏笔账本（状态机）
│   │   ├── aggregate_utils.py    # 聚合工具（11种 JSON 输出）
│   │   ├── character_card_generator.py  # 角色卡片生成器
│   │   ├── excel_export.py       # Excel 导出（11个 sheet）
│   │   ├── export_utils.py       # Markdown 导出
│   │   ├── text_utils.py         # 编码检测、文本去重、伏笔去重
│   │   └── docx_extract.py       # DOCX 正文抽取（zipfile+ElementTree 纯标准库，重建"一行一个语义单元"）
│   └── tests/                    # pytest 默认收集 backend/tests；顶层 tests/ 随项目配置同步收集
├── frontend/                     # Vue 3 前端
│   ├── src/
│   │   ├── api/
│   │   │   ├── client.ts         # 56 个 API 方法 + TypeScript DTO + 统一 X-Session-Token 注入
│   │   │   ├── sessionToken.ts   # 会话 token 单例（pywebview js_api 获取，PR-1 收尾）
│   │   │   └── useProgressSocket.ts  # 单例 WebSocket + pub/sub
│   │   ├── components/
│   │   │   ├── AppLayout.vue     # 根布局（标题栏 + 侧边栏 + pywebview 窗口控制）
│   │   │   ├── ChapterDetailPanel.vue  # 章节详情面板（自动跟踪最新章节）
│   │   │   ├── BookSelector.vue  # 通用书籍选择器
│   │   │   ├── ChapterValue.vue  # 递归数据渲染组件
│   │   │   ├── LogConsole.vue    # 日志控制台
│   │   │   ├── ProgressBar.vue   # 进度条（含块→章转换）
│   │   │   ├── CountUp.vue       # 数字缓动组件（QueuePage session stats）
│   │   │   ├── ConfirmDialog.vue # 玻璃材质确认对话框
│   │   │   └── Icon.vue          # 30+ SVG 图标
│   │   ├── composables/
│   │   │   └── useLogStore.ts    # 全局日志存储（localStorage 持久化）
│   │   ├── pages/
│   │   │   ├── QueuePage.vue     # 主页：队列管理 + 实时进度
│   │   │   ├── SettingsPage.vue  # 配置：API/分析/伏笔分类/滚动总结
│   │   │   ├── SummaryPage.vue   # 聚合 + 最终总结（4阶段加权进度）
│   │   │   ├── SplitterPage.vue  # 批量切章
│   │   │   ├── WorkspacePage.vue # 工作区/归档管理
│   │   │   ├── TimelinePage.vue  # 时间线（事件+伏笔，分类筛选）
│   │   │   ├── GraphPage.vue     # 角色关系图（ECharts force 力导向）
│   │   │   ├── MapPage.vue       # 地图（SVG 树形布局）
│   │   │   ├── CharacterCardPage.vue  # 角色数据库
│   │   │   ├── StylePage.vue     # 风格分析
│   │   │   ├── StatsPage.vue     # Token 统计面板
│   │   │   └── PromptPreviewPage.vue  # Prompt 预览
│   │   ├── utils/
│   │   │   ├── markdown.ts       # 零依赖 Markdown→HTML 渲染器
│   │   │   ├── summaryLanes.ts   # 总结阶段→LaneView 合成块（id ≥ 900000）纯函数
│   │   │   ├── laneRegistry.ts   # 车道注册表自愈：GC + inflight 对账 + normalizeRange 归一 + 超并发截断（纯函数）
│   │   │   ├── queueFeed.ts      # QueuePage WS 状态机纯函数（block_start/done/token_delta/discovery + 会话汇总）
│   │   │   ├── summaryProgress.ts # 总结 4 阶段加权进度/阶段序号/思考参数 JSON 纯函数
│   │   │   ├── settingsForm.ts   # 空串剪除 / 422 字段映射 / 分类勾选 / 思考模式 JSON 纯函数
│   │   │   ├── splitterView.ts   # 切分页：baseName / 去重追加 / F-2 回滚 / 卷分组纯函数
│   │   │   └── workspaceView.ts  # fmtSize 阶梯 / fmtTime 格式化纯函数
│   │   ├── router.ts             # 12 条路由（AppLayout 包裹）
│   │   ├── App.vue
│   │   ├── main.ts
│   │   └── main.css              # Win11 Fluent 设计系统（--win-* 变量）
│   ├── package.json
│   └── vite.config.ts
├── desktop.py                    # pywebview 桌面入口（单实例锁 O_EXCL 原子抢锁 + 关闭确认 + H15 崩溃诊断走 logging 体系 + Win11 Mica + graceful 退出）
├── config.json                   # 运行配置（含 API Key，已 gitignore）
├── queue_state.json              # 队列状态（已 gitignore）
├── workspace/                    # 分析工作区（已 gitignore）
├── docs/superpowers/             # 设计/实施计划
├── agent.md                      # 本文件
├── README.md                     # 项目说明
├── CHANGELOG.md                  # 更新日志
└── requirements.txt              # Python 依赖
```

---

## 3. 技术栈详情

### 后端
| 组件 | 版本/说明 |
|---|---|
| Python | 3.11+ |
| FastAPI | 异步框架，桌面端动态端口（desktop.py `_find_free_port`）；开发模式示例 8088 |
| OpenAI SDK | `AsyncOpenAI` 支持任何兼容 API |
| Anthropic SDK | `AsyncAnthropic` 支持 Claude 系列 |
| pywebview | 桌面 WebView2 壳 |
| openpyxl | Excel 导出（懒加载） |
| ~~networkx + pyvis~~ | **历史依赖、当前无消费者**（2026-10-14 核实：backend 无 import，`requirements.txt` 未列；关系图由 `viz_service.graph_data` 自行聚合，前端 ECharts 渲染）。保留此行仅作历史记录，不要当当前依赖安装 |
| json5 / json_repair | JSON 容错解析 |
| pytest | 默认收集 `backend/tests/` + 顶层 `tests/`；数量以 `pytest --collect-only -q` 为准 |

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
.txt 文件
  → _detect_and_read（单一文本入口：txt 走编码检测，docx 走 Word 正文抽取）
  → splitter_service（切章，15种章格式 + 5种卷格式正则，PG 头截断 + TOC 去重）
  → blocks/*.txt
  → pipeline（三阶段分析）
      → serial warmup（前 N 块串行）
      → streaming concurrent（信号量+as_completed 并发）
      → failure retry（最多 3 轮串行补跑）
  → MemoryState（全内存 IO）
  → output/chapter_N_result.json
  → output/rolling_summary.json
  → 最终保存（merge_results → 裁剪 → 写盘）
      ↓
  → final_summary（4阶段总结，与归一化解耦）
      → batch volumes + reconciliation
      → foreshadow recheck
      → style extraction
      → final report
  → final_summary_report.md
  → aggregate_utils → aggregated/*.json（11种）
  → style_analyzer → style.md
  → excel_export → *.xlsx
  → 归一化（独立路径，仅服务地图；详见 §5.2 location_normalizer 与 §10.13）
      → Phase 0a-batches（locations 并发）
      → Phase 0b-batches（spatial 并发）
      → Phase 0b-dedupe（机械去重）
  → output/locations_normalized.json + spatial_relationships_normalized.json
```

### 4.2 三阶段分析流水线（pipeline.py）

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

### 4.3 LLM 客户端（llm_client.py + llm_probe/llm_failure_logger/llm_stream）

**三层重试链：**
1. 温度退火：`temp_max_retries` 次，温度 = max(0, 初始 - attempt × step)
2. 指数退火：`backoff_max_retries` 次，等待 = min(2^N, 60s)
3. 429 限流：额外 3 轮，尊重 Retry-After 头（流式路径 2026-10-25 起也提取 Retry-After）

**stop 类立即停（error_taxonomy，2026-10-25）：** AUTH/BILLING/CONTEXT_LENGTH/BAD_REQUEST 首次命中即停
（`_error_action()` 按 RETRY_POLICY 决策，附用户可读提示，官方文档背书"重试不可恢复"）；
审核 MODERATION_INPUT/structured 首次短路、text 置信维持连续 3 次。pipeline 补跑轮剔除 fatal 块（`_fatal_blocks`）。

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

**十路上下文注入（system 4 路 + user 7 路，不含章节正文）：**
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
- **P0-1 修复**：只允许请求 limit ≥ 缓存 limit 才扩展，防止并发低章号块读到未来知识

**乱序保护（`_merge_one`）：**
- `_kb_last_chapter` 跟踪最大已合并章号
- 低章号迟到结果只合并非进度内容（事实/角色/世界等），跳过 timeline/recent_summaries

### 4.7 最终总结（final_summary.py）

**4 阶段：**
1. **分卷摘要 + 伏笔调和**（每批两次 LLM 调用）：
   - 第一次：Markdown 卷摘要
   - 第二次：JSON reconciliation（伏笔状态调和）
   - 卷摘要一完成即落盘（断点续跑）
2. **全书伏笔复检**（剩余 active 伏笔，40个/子批）
   - **复检超限保护**：全量卷摘要超 `RECHECK_FULLTEXT_BUDGET_CHARS`（默认 15 万字符）时按伏笔埋设章砍埋设前卷（召回无损），仍超则跳过该组不调用 LLM
3. **风格分析**：`extract_style_profile` → 注入最终报告
   - **2026-08-23**：风格分析与阶段 1 并行启动（数据无依赖：仅需 blocks_dir），用 `asyncio.create_task` 在阶段 1 启动时立即跑，阶段 2 完成后 await 收结果
4. **最终报告**：卷摘要 + 伏笔上下文 + 审计 + 风格 → `final_summary_report.md`

**全局 LLM 并发上限 Sem（2026-08-23）：**
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

### 5.1 核心模块（backend/core/）

#### pipeline.py（1177行）
- **职责**：全书分析总调度器
- **关键类**：`AnalysisPipeline`
- **关键方法**：`run()`（三阶段——串行预热→流式并发→补跑——自初始提交起就全部内联于单方法，经 `_worker`/`_async_rolling` 嵌套闭包共享可变计数与 rolling/检查点交错；**刻意取舍，勿拆成"阶段方法"**——拆分只会把共享状态摊到 self 或 context 对象上）、`_analyze_one_block()`（预热/并发/补跑三路径共用入口，try/finally 登记 `_inflight_blocks` 在途块）、`inflight_blocks()`（真实在途快照，供 status 端点给前端车道对账）。⚠️ 本行曾虚构 `_serial_warmup()`/`_streaming_concurrent()`/`_failure_retry()`/`_flush_checkpoint()` 四个不存在的方法名，2026-08-31 已修正
- **异常兜底**：`_worker` 与 `analyze_block_with_progress` 内包 try/except——异常带原 block_id 发 failed + 补 `state.add_failed`（否则消费循环兜底固定发 chapter:0 被前端忽略 = 车道泄漏，且异常会冲出 run() 整书标 failed）
- **补发限速**：断点续跑补发 block_done 每 50 条 `sleep(0)` 让出事件循环（千章级续跑 ~2700 条消息会打满 WS 每连接队列触发丢最旧）
- **进度广播**：通过 `ProgressHub` WebSocket 推送 log/progress/block_done/state_change/token_stats
- **停止机制**：`self._stop_requested` + `self.analyzer.stop()` + `self.rolling_client.request_stop()`

#### llm_client.py（1291行，2026-08-31 拆分后主类）
- **职责**：统一封装 OpenAI/Anthropic 双协议 LLM 调用与重试核心
- **关键类**：`LLMClient`
- **关键方法**：`chat()`、`chat_with_retry()`（温度退火+指数退避+429 三层）、`chat_stream()`/`chat_stream_with_retry()`（H16 流式）、`chat_auto()`、`list_models()`、`detect_provider()`
- **KV Cache 统计**：读取 `prompt_tokens_details.cached_tokens`
- **审核识别**：`moderation_hit_from_exception()` 委托 `classify_exception`（2026-10-25 起）；审核拦截 structured/输入侧首次短路、text 置信连续 3 次命中才短路退出重试链（瞬时误判多给一轮温度尝试）
- **重试等待**：统一经 `_sleep()` 封装；失败记录走 `llm_failure_logger._get_failure_logger()`；429 判定经 `classify_message`（原 `_is_429` "429" 子串判定已替换，`_is_429` 保留为兼容壳）
- ⚠️ 单测 patch 注意：`AsyncOpenAI` 的 patch 目标是 `backend.core.llm_client.AsyncOpenAI`（test_config_robustness/test_streaming 流式路径）；probe 相关 patch 在 `backend.core.llm_probe`

#### llm_probe.py（277行）
- **`probe_thinking_params(base_url, api_key, model, provider, max_tokens)`**：多模式探测端点实际认哪个禁用思考参数（7 维检测器 `_THINKING_DETECTORS`，任一命中即判定在思考；基线命中 + 候选全未命中 = 参数有效）
- `_get_reasoning_tokens()`：usage Pydantic/dict 双形态安全提取（全库仅 probe 使用）
- 自建 AsyncOpenAI 探针客户端，不走重试链/失败日志；单测 patch 目标 `backend.core.llm_probe.AsyncOpenAI`（test_anthropic_provider ×9 + test_streaming ×1）
- `_THINKING_DETECTORS` 字段清单与前端 SettingsPage 的分项展示是**手工镜像**关系，禁删字段

#### llm_failure_logger.py（~150 行，PR-1 +50 行）
- **FailureLogger**：线程安全，`RotatingFileHandler(10MB × 3)` 轮转，每行 JSON 便于 `jq`/grep；记录温度/错误类型/等待时长
- 进程级单例 `failure_logger` + `_get_failure_logger()`；`reset()` 供跨书清空
- **`redact()` 模块函数**（PR-1 修复，2026-09-10）：对文本中的 API key / token 脱敏，覆盖 OpenAI sk-... / Anthropic sk-ant-... / Authorization Bearer / x-api-key header；**显式长度 20+ 避免误伤短随机串**；**fail-open**（re.sub 抛异常时返回原文）。`FailureLogger.record_failure` 入口对 `error_message` 自动走 redact；`app.py` 的 `_SensitiveFieldsFilter` 也复用此函数

#### llm_stream.py（133行）
- **StreamChunk/StreamResult**：H16 流式调用数据结构（analyzer/final_summary/test_streaming 导入方）
- **`parse_openai_stream_event()`/`parse_anthropic_stream_event()`/`parse_anthropic_response()`**：双协议事件/响应解析纯函数（方法体无 self，自实例方法转正）

#### analyzer.py（223行）
- **职责**：单章分析引擎
- **关键类**：`ChapterAnalyzer`
- **流程**：`prompt_builder.build_messages()` → `llm_client.chat_with_retry(validate=validate_json_response)` → `_parse_response()` → `AnalysisResult.from_dict()`
- **JSON 验证**：检查必须字段（core_events/cross_block/long_context_insights）；`validate_parsed_analysis` 另做嵌套字段类型检查（嵌套字段非 dict 视为缺失，防 `from_dict` 默认值掩盖）

#### prompt_builder.py（320行）
- **职责**：构建 system + user 两条消息
- **关键类**：`PromptBuilder`
- **50类伏笔表**：`FORESHADOW_CATEGORY_TEXT` 恒定追加在 system prompt 后
- **结构化滚动总结渲染**：`_render_structured_rolling()` — 五层渲染
- **正文超限截断**：章节正文超 30000 字符时尾部保序截断（保留后段，丢弃开头字数显式告知模型，防超窗确定性失败）

#### knowledge_base.py（483行）
- **职责**：磁盘 KB 的加载/保存/合并/增量更新
- **关键类**：`KnowledgeBaseManager`
- **关键方法**：`merge_results()`、`build_temp_knowledge()`、`_merge_incremental()`、`_merge_result_into()`
- **近邻增量缓存**：近邻命中前校验 ≤best_limit 基线区间文件指纹（mtime_ns），基线被重写即回退全量重建；命中产物返回副本断开对象别名（陈旧 KB 不固化进最终 knowledge.json）

#### memory_state.py（466行）
- **职责**：全内存 IO，持有所有 AnalysisResult + 增量 KB
- **关键类**：`MemoryState`
- **关键方法**：`merge_one()`、`get_kb_snapshot()`、`flush_to_disk()`、`restore_from_disk()`
- **skipped 标记持久化**：审核拦截章写 `chapter_N_skipped.json`（重启续跑不再重付 LLM 费用），对应章节成功后清除；恢复时重新载入标记
- **失败范围过滤**：`failed_chapters_in(valid_block_ids)` 只返回本轮有效范围内的失败块，补跑不误扫旧账
- **快照锁**：KB 快照读写全程持 `_snapshot_lock`（含 `add_result`，收口 to_thread 化后的跨线程竞态）；分析用 KB 快照注入 `rolling_structured`，滚动总结对 prompt 生效

#### style_analyzer.py（505行）
- **两层架构**：
  1. 22 项统计硬指标（纯代码）：句法、对话、词汇、词表密度、标点
  2. 8 维语义风格（LLM）：signature_expressions/narrative_rhythm/dialogue_style/rhetorical_preferences/emotional_expression/narrative_voice/information_control/narrator_and_genre
- **采样策略**：前半随机 3 章 + 后半随机 3 章，每章随机位置取 1500 字

#### moderation.py（转发层）
- **职责**：`[MODERATION]` 标记契约（`mark_moderation`/`is_moderation_error`/`MODERATION_MARKER` 原样，pipeline/analyzer/前端零改动）；码表与文本判定 2026-10-25 起转发 error_taxonomy
- **标记机制**：`[MODERATION]` 前缀注入错误字符串

#### error_taxonomy.py（厂商错误码分类体系，2026-10-25）
- **`classify_exception(e)`**：结构化通道——异常属性码（`e.code`/`e.type`）→ body 深挖（OpenAI `error.code/type`、MiniMax `base_resp.status_code`、方舟/硅基 `error.code`）→ HTTP 状态兜底（401/403/402/413/429/5xx；**400/404/422 不兜底**，未识别返回 None 走保守退避）
- **`classify_message(msg)`**：字符串通道永不 None——只认**命名错误码**（snake/camel 子串）与**错误措辞**（含 `HTTP`/`Error code` 前缀的状态码），命中一律 `text` 置信；无信号 → `UNKNOWN` + `none`。**纯数字厂商码不再参与文本扫描**（2026-10-30 C-01：`\b\d{3,4}\b` 扫任意文本会把正文「第 401 章」「赚了 1008 块」「总字数 1026 万」判成 AUTH/BILLING/MODERATION_INPUT 并永久熔断整块）；数字码只留在 `BODY_CODE_MAP`，structured 独享
- **`ClassVerdict`（2026-10-30 S1）**：`(cls, confidence, action)` 三元组，`action` 由唯一策略函数 `resolve_action(cls, confidence)` 在构造时推导；`is_permanent()` = `structured` 且 `action == "stop"`，是判断"永久停止"的唯一谓词。文本通道的 action 上限是 `limited_retry`/`backoff`/`retry_after`，**永不为 stop**
- **`ErrorClass` 定义处**：2026-10-30 起收敛到 `backend/core/error_types.py`，本模块只 re-export——两份同值 `str`-Enum 会让跨模块 `is` 比较静默返回 False
- **判定优先级写死（P2-4）**：MODERATION_INPUT → MODERATION_OUTPUT → BILLING → CONTEXT_LENGTH → AUTH → BAD_REQUEST → RATE_LIMIT → SERVER → NETWORK → TIMEOUT → UNKNOWN；前插规则：百炼 `Throttling.*`；条件规则：422 `unprocessable_entity_error` + 敏感 message → 输入审核（MiniMax Anthropic 皮肤，日志 ~5525 条的最大样本），否则 BAD_REQUEST
- **审核判侧（P2-5）**：码表逐条带侧（1026 输入/1027 输出/方舟 SensitiveContentDetected 输入），双侧码默认 OUTPUT（保留 3 次短路）；`refusal`/裸词码（safety/moderation 等）/智谱 1000/1302 系仅 body 通道，不进消息扫描（防误报）
- **接入点**：流式生成器 except（分类 + 审核标记 + Retry-After 提取，llm_client D3）、`_decide_retry()`（2026-10-30 S1 起，四处重复决断收敛为单点，读 `verdict` 而非重分类）、chat() APIError/anthropic.APIStatusError 分支、pipeline 补跑 fatal 过滤（`:718-735` 起改为**结构化优先、文本兜底**：有 `retry_info["verdict"]` 就用 `ErrorVerdict.is_permanent`，无则回退文本判定；文本恒不永久，故兜底路径不会误登记）
- **gold fixtures**：`backend/tests/test_error_taxonomy.py`（真实日志串 + 厂商码样例；**改码表/优先级前先看**）

### 5.2 服务层（backend/services/）

#### queue_service.py（361行，2026-08-31 拆分后编排层）
- **AnalysisService**（单例）：
  - `_run_queue()`：逐本运行 `AnalysisPipeline`（经 `make_pipeline_callbacks` 挂事件翻译层）
  - 双向互斥：分析运行中禁总结，总结运行中禁分析
  - 自动总结：队列完成后逐本串行执行 `FinalSummaryRunner`
  - 自动归档：分析完成后 `shutil.move` 到 `分析结果/`（`_archive_item` 含 P5d 路径更新修复）
  - 每本书分析完成后 token 消耗落盘 `output/token_stats.json`（做差逻辑在 AnalysisStats.book_consumption）
- **status() 暴露 `inflight_blocks`**：pipeline 信号量窗口内的真实在途块（≤ concurrency 零丢失），前端车道对账源；`getattr` 兜底 `__new__` 构造的单测实例

#### queue_manager.py（408行）
- **QueueItem**：队列任务（持久化经 `_store_path`/`_load_path` 相对 PROJECT_ROOT 路径化，搬迁/换盘不失效）
- **QueueManager**：状态机（pending/running/done/failed/skipped；增删移推进/`scan_directory` 并行扫描/`infer_progress`/`save_state`/`load_state` 原子写）
- **`check_api()`**：API 预检（models 健康检查 → chat 探针 → 跨协议翻转探针）
- **跨协议探针纠正**：预检 models/chat 探针失败时翻转 provider 重探一次；OpenAI 协议暴露 claude-* 模型时就地纠正 provider 并经 config_manager 落盘持久化（config_manager 由 AnalysisService 注入）
- **状态持久化**：`queue_state.json`（相对路径存储）
- ⚠️ 单测 patch 注意：`LLMClient` 的 patch 目标是 `backend.services.queue_manager.LLMClient`（test_provider_heal ×8）

#### analysis_stats.py（119行）
- **AnalysisStats**：一次分析会话的统计值对象——分类 token 累计/每章记录/重试成本/KV 命中/起止时刻
- 方法：`reset()`（start 调）/`record_chapter()`/`accumulate_token_stats()`/`baseline()`（本书落盘基线）/`freeze()`（finally 冻结 elapsed）/`session_snapshot(running, cached_tokens)`/`book_consumption(item, baseline)`
- **不持有 pipeline**：运行中实时 KV 命中由 service 读 `self._pipeline._analyzer` 后传参

#### pipeline_events.py（167行）
- **`make_pipeline_callbacks(hub, item, stats, seen_characters)`**：返回 (on_progress, on_token_stats)，替代原 `_run_one_item` 嵌套闭包（捕获面经复审确认恰好完整；save_queue 不在此层）
- on_progress：status=start→block_start、done/failed/skipped→block_done（skipped 必须转发，否则前端车道泄漏）+ discovery（仅成功块）
- `_extract_discovery`/`_DISCOVERY_STOP_WORDS`/`_format_eta`/`_compute_eta` 随迁于此

#### final_summary.py（1703行）
- **职责**：全书总结执行引擎
- **关键类**：`FinalSummaryRunner`
- **关键方法**：`run()`、`_run_batch()`、`_reconcile_batch()`、`_recheck_remaining()`、`_write_report()`、`_plan_recheck_batches()`

#### summary_service.py
- 总结任务生命周期管理
- 总结结束落盘 `output/summary_token_stats.json`

#### splitter_service.py（1242行）
- **职责**：小说文本切分为独立章节文件
- **15 种章格式 + 5 种卷格式正则**：中文数字、阿拉伯数字、"回"、"节"、卷+章组合、英文 Chapter、Roman（`CHAPTER I.`）、裸 Roman（Sherlock 独行 `I.`/`II.`）、Book the First/Second + Book [IVXLCDM]（Dickens 风格）
- **核心算法**：两遍扫描（定位边界→提取内容）、评分式模式选择、次级格式比额门槛并入（绝对得分 ≥2 且 ≥ 主导的 15%，混排书不吞章、高频噪声列表不过度切分）、MD5 去重、超长章拆分
  - **T2.5 PG 头截断**：`_strip_gutenberg_header` 在 START 标记后切掉 license 头，保留正文
  - **T2.5b TOC 跳过**：START 后 100 行内识别 CONTENTS 块、跳过再找真章节；`_find_first_real_chapter_or_volume` 通过"12 行内 body 出现"判别 TOC vs 真章节；`_dedup_same_key` 保留字数最多（TOC 残留 < 1000 字、正文 ≥ 5000 字）
  - **T2.7 pre-START metadata**：`_extract_pg_header_meta` 识别 `Title:` / `Author:` 显式行（Dracula/Pride 等 7 本 metadata 全空 → 全填上）
- **并发写入**：`ThreadPoolExecutor`（最多 32 工作者）缓解网络盘延迟
- **运行护栏 + 原子写**：save/batch 路由有分析运行护栏；写盘暂存-交换原子化
- **已知限制**：4 条见 `splitter_service.py` docstring L51-75（Dracula 日记体 / Moby-Dick TOC 残留 / 裸罗马全大写约束 / pre-START 仅对 PG 有效）

#### workspace_service.py（381行）
- **职责**：工作区目录管理
- **安全机制**：`_is_within()` + `_safe_join()` 路径遍历防护
- **平台回收站**：Windows `SHFileOperationW` + macOS `osascript` + Linux `gio trash`；`fAnyOperationsAborted=TRUE`（用户/系统中止）同样视为失败
- **显式路径删除**：`delete_novel_to_trash` 支持显式路径参数（按队列项 workspace_dir 删除，防同名误删）
- **归档回滚**：归档失败自动回滚；回滚再失败则记录滞留临时目录路径并中止，不静默丢数据

#### book_service.py（226行）
- **职责**：书名→目录映射（catalog）
- **发现来源**：队列项 + 文件系统扫描 + 归档目录 + 手动注册
- **懒刷新**：冷启动（映射空）阻塞刷一次保证首查可用；此后 miss 仅触发后台单飞刷新并立即返回 None（消除事件循环秒级冻结，下次查询即命中）

#### viz_service.py（265行）
- **三种可视化数据**：
  1. `timeline_data()` — 事件 + 伏笔（含分类映射）
  2. `graph_data(output_dir, chapter_start?, chapter_end?, min_edge_weight=1, max_nodes=200, min_node_count=1)` — 角色节点 + 共现边，支持章节范围切片、边权阈值过滤、Top-N 截断，返回 `nodes/edges/total_characters/total_edges/filtered/chapter_range`
  3. `map_data()` — 地点 + 空间关系

#### location_normalizer.py（753行）
- **职责**：地点与空间关系 LLM 归一化（Phase 0，归一化与最终总结已解耦，仅服务地图）
- **关键类**：`LocationNormalizer`
- **3 子阶段**（2026-08-23 砍掉 Phase 0a consolidation，见 10.10）：
  1. `_run_phase_0a_batches`：locations 切片 + 并发 LLM 调 + 校验（canonical ∈ aliases）
  2. `_run_phase_0b_batches`：spatial 切片 + canonical 白名单 + 并发 LLM
  3. `_run_phase_0b_dedupe`：机械去重同 (from, to) 对
- **触发**：MapPage 经 `LocationNormalizationService`（单例编排器，见 `location_normalization_service.py`；REST 入口 `/api/location-normalization/start|stop|status|result`）后台运行归一化，进度经 ProgressHub 广播；不再在 `FinalSummaryRunner.run()` 里调用
- **复用配置**：`summary_model` / `summary_concurrency` / `summary_timeout` / `summary_thinking_mode`
- **输出**：`output/locations_normalized.json` + `output/spatial_relationships_normalized.json`
- **chapter 标记**：每章 chapter_*.json 顶部加 `_normalized_ref` + `_normalized_spatial_ref` 字段
- **地图强制依赖归一化**：`viz_service.map_data()` 检测到 `_normalized_ref` 缺失则返回 `needs_normalization: true`，MapPage 拦截展示归一化面板，不再读 raw locations

### 5.3 工具层（backend/utils/）

#### json_utils.py（383行）
- **9级修复链**：直接解析 → 截尾 → 去注释 → 漏引号修复(两轮) → 全角归一 → json5 → ast.literal_eval → json_repair → 单引号替换；全策略仅接受 dict 结果（非 dict 一律视为该策略失败，防字符串/列表混入下游）
- **原子写**：`safe_save_json()` 先写进程内唯一 tmp 名（pid + uuid 后缀，防并发同目标互踩）再 `replace`；replace 撞 `PermissionError` 时有界重试退避

#### foreshadow_ledger.py（189行）
- **状态机**：`active → resolved / dormant`
- **休眠判定**：`last_seen_chapter` 距当前进度 ≥ 200 章

#### aggregate_utils.py（884行）
- **职责**：逐章 JSON 结果聚合为 11 种输出格式
- **关键类**：`JSONAggregator`
- **去重机制**：角色名归一化（剥离角色后缀）、哈希去重、模糊主题去重
- **主文件原子写**：主聚合文件 novel_analysis_aggregated.json 经 `safe_save_json()` 原子落盘（唯一 tmp 名 + replace）；其余子聚合适用一次性生成场景，仍为直写

#### character_card_generator.py（198行）
- **职责**：生成角色索引与角色详情数据，供角色卡 API/前端消费
- **入口**：`get_character_summaries()` / `get_card_data()`

#### excel_export.py（292行）
- **职责**：聚合结果导出 Excel（11 个 sheet）
- **安全**：单元格文本以 `= + - @` 开头时前置转义，防公式注入（DDE 攻击面）
- **护栏**：聚合目录缺少可识别 JSON 时抛 `ValueError` 明确报错，不生成零 sheet 坏文件

#### text_utils.py（417行）
- **编码检测**：BOM/NUL 预检 + 候选编码采样罚分择优（U+FFFD×8 / 控制字符×4 / PUA×3 除以样本长度，取最低罚分；Big5 繁体书不再坠入 gb18030 乱码，`detect_and_decode` 与 `detect_encoding` 两入口共用同一选择器）
- **文本去重**：归一化相等 + 子串包含 + SequenceMatcher 相似度
- **伏笔去重**：关键词倒排索引 + 序列相似度（PERF-2 优化）

#### docx_extract.py（2026-09-28 新增）
- **职责**：`.docx` → 纯文本，为切章器重建「一行一个语义单元」的输入形态。**零第三方依赖**（`zipfile` + `xml.etree` 标准库）
- **为什么必须存在**：15 条章正则全带 `^` 行首锚点并配套 `len(s) <= 80` 整行闸门（`splitter_service.py:822`）。TXT 天然满足该前提；DOCX 里没有换行符，标题若与正文并进同一超长行会被闸门判负 → **整章被静默吞掉**。本模块的唯一任务是让下游以为自己还在吃 txt
- **关键设计**：
  - `<w:br/>` → `\n`、` <w:tab/>` → 空格
  - Heading/Title 样式段落前后补空行强制独占一行；样式名用正则 `^heading\s*\d*$|^title$` 覆盖 Heading 1/2/3...（不能只比对字面量）
  - **递归 `body.iter('w:p')`** 而非取直接子节点 —— 网页转制 DOCX 常把整章正文塞进单单元格表格
- **异常分诊**（全部转 `ValueError`，否则 `routes_splitter.py` 的 `except Exception` 会吞成 500）：ZIP magic 预检 → `BadZipFile` → **`RuntimeError`（加密 docx 抛的是这个，不是 BadZipFile）** → 空正文
- **⚠️ 不要在 heading 前后注入空行**：曾实现过"标题前后补空行强制独占一行"，被复审证明有害 —— 逐段落输出时标题本就独占一行，补空行会让 DOCX 与 TXT 行结构不等价，多出的空行会进 `blocks/NNNN.txt` 并把 `style_analyzer` 按第 N 个汉字定位的采样窗口（`style_analyzer.py:283`）推后。**该行为已删除**，回归测试见 `test_no_extra_blank_lines_around_heading`
- **`_is_heading` 的真实作用域**：仅当「标题 + 正文」共存于**同一个 `<w:p>`**（中间只有 `<w:br/>`）时才需要劈成两行，防止标题被折进超长行遭 `len(s) <= 80` 闸门判负。其余情况下它是无害的空转
- **`enable_short_line_heuristic` 已删除**：plan §D2 证明该启发（≤30 字无句末标点即断行）会污染 `lines[:800]` 采样窗口，属被证伪方案，不留开关

### 5.4 API 层（backend/api/）

**总计 61 个 HTTP 端点 + 1 个 WebSocket 端点**

| 路由模块 | 端点数 | 前缀 | 说明 |
|---|---|---|---|
| `routes_analysis.py` | 18 | `/api` | 分析 start/stop/status + token_stats（会话/累计）+ 队列 CRUD + 风格 + 日志 |
| `routes_books.py` | 10 | `/api/books` | 书籍列表/结果/报告/账本/角色/章节/token_stats |
| `routes_settings.py` | 6 | `/api/settings` | 配置读写/模型列表/思维探测/预设 |
| `routes_workspace.py` | 5 | `/api/workspace` | 工作区小说/归档管理 |
| `routes_splitter.py` | 4 | `/api/splitter` | 预览/保存/批量切章 |
| `routes_aggregate.py` | 4 | `/api/aggregate` | 聚合/文件列表/读取/Excel |
| `routes_summary.py` | 3 | `/api/summary` | 总结 start/stop/status |
| `routes_viz.py` | 3 | `/api/viz` | 时间线/关系图（支持 chapter_start/chapter_end/min_edge_weight/max_nodes/min_node_count query 参数）/地图 |
| `routes_location_normalization.py` | 4 | `/api/location-normalization` | 归一化任务 start/stop/status/result |
| `routes_prompt.py` | 1 | `/api/prompt` | Prompt 预览 |
| `routes_foreshadow.py` | 1 | `/api/foreshadow` | 50类伏笔分类定义 |
| `auth.py` | 0 | — | **session token 鉴权 D 方案**（PR-1 修复，2026-09-10）：`require_session_token` Depends，供 `/api/analysis/logs` 等敏感端点使用。读 `NOVEL_ANALYZER_SESSION_TOKEN` 环境变量 + `X-Session-Token` header；`hmac.compare_digest` 防时序攻击；环境变量未设置（开发模式）→ 放行 |
| `ws.py` | 1 WS | `/ws/progress` | WebSocket 实时进度（wire 协议权威定义见 `backend/ws_events.py`） |

**WS 事件协议**（2026-08-31 类型化）：`backend/ws_events.py` 为唯一权威定义——
`WSType`(11 值)/`WSState`(12 值)/`PipelineStatus`(7 值) 三个 StrEnum + 8 类 payload
契约 + 7 个纯同步工厂（log_event/progress_event/block_start_event/block_done_event/
discovery_event/state_change_event/token_delta_event）。前端镜像：
`frontend/src/api/useProgressSocket.ts` 的 ProgressMessage 可辨识联合。
修改任何事件字段必须两侧同步。

**安全机制：**
- 路径遍历防护（`routes_aggregate.py`）
- put_queue 原始条目预检（畸形条目跳过不入队）；delete_book 按显式 workspace_dir 删除（防同名误删）
- WebSocket 来源白名单（仅 localhost/127.0.0.1）
- API Key 在 POST body 中（避免 URL/日志泄露）
- 分析锁（`_ensure_idle()` / `_require_analysis_idle()`）

---

## 6. 前端模块详解

### 6.1 API 层

#### client.ts（454行）
- **60 个 API 方法**，覆盖所有后端端点
- **TypeScript DTO**：`AppConfigDto`、`QueueItemDto`、`AnalysisStatus`、`TokenStatsResponse` 等
- **错误增强**：HTTP 错误附加 `.status` 和 `.detail` 属性
- **防重复读取**：先 `res.text()` 再 `JSON.parse()`，避免 body stream already read

#### useProgressSocket.ts（123行+协议类型）
- **单例 WebSocket**：模块级变量，跨路由共享
- **可辨识联合 ProgressMessage**（2026-08-31）：10 类 payload 接口与 backend/ws_events.py 镜像，switch 自动窄化
- **pub/sub 模式**：组件注册 `onMessage` 回调，自动清理
- **指数退避重连**：1s → 2s → 4s → ... → 10s 上限
- **ping 过滤**：心跳消息静默丢弃
- **手动关闭保护**：`manualClose` 标志防止孤立重连

### 6.2 状态管理

**无 Vuex/Pinia**，采用：
- **模块级单例**：`useLogStore`（2000 条容量，localStorage 持久化，500ms 节流保存）+ `useProgressSocket`
- **组件内 ref()**：每个页面管理自己的状态
- **后端为真相源**：配置在后端持久化，前端挂载时读取

### 6.3 设计系统（main.css）

**Win11 Fluent：**
- CSS 变量：`--win-accent`、`--win-mica`、`--win-layer`、`--win-stroke`、`--win-shadow-*`、`--win-radius-*`
- 玻璃材质容器：`glass-card`、`glass-button`、`glass-input`、`glass-badge`
- 禁止 `backdrop-filter`（WebView2 不支持）

### 6.4 页面（12个）

| 页面 | 行数 | 职责 |
|---|---|---|
| QueuePage | 792 | 主页：队列表、启停控制、实时进度（WS+轮询）、运行概览仪表盘（车道 GC+对账自愈）、章节详情、日志 |
| SettingsPage | 558 | 配置：API 预设、模型选择器、思维探测、伏笔分类网格、滚动总结参数、复检批大小 |
| SummaryPage | 532 | 聚合+总结：4阶段加权进度、模型覆盖、日志抽屉、报告查看器 |
| SplitterPage | 422 | 批量切章：文件选择、预览、自定义正则、卷识别、pywebview 文件对话框 |
| WorkspacePage | 186 | 工作区/归档：列表、归档、删除（自定义确认对话框） |
| TimelinePage | 255 | 时间线：事件+伏笔、重要性/分类筛选 |
| GraphPage | 385 | 角色关系图：ECharts force 力导向、章节范围切片、Top-N 截断、边权阈值过滤、邻接高亮、右侧关联面板 |
| MapPage | 358 | 地图：SVG 树形布局、空间关系虚线、归一化状态面板（强制走归一化数据，未归一化时拦截） |
| CharacterCardPage | 277 | 角色数据库：统计、弧光、事件、状态演化、关系 |
| StylePage | 139 | 风格分析：启停、轮询、结果展示 |
| StatsPage | 220 | Token 统计：分类明细、每章详情、按书历史统计、5s 自动刷新 |
| PromptPreviewPage | 62 | Prompt 预览：系统+用户消息、可指定章节号、字符计数 |

### 6.5 组件（9个）

| 组件 | 行数 | 职责 |
|---|---|---|
| AppLayout | 361 | 根布局：标题栏、可折叠侧边栏、pywebview 窗口控制、暗色模式 |
| ChapterDetailPanel | 251 | 章节详情：自动跟踪最新章节（60s 空闲回退）、请求序列守卫 |
| BookSelector | 66 | 书籍下拉选择器：状态指示器、刷新按钮 |
| ChapterValue | 141 | 递归数据渲染：标量/数组/对象、中文字段标签 |
| LogConsole | 113 | 日志控制台：简化/完整模式、自动滚动 |
| ProgressBar | 41 | 进度条：块→章转换、ETA 显示 |
| CountUp | 74 | 数字缓动显示：QueuePage session stats（K/M 缩写 + 1 位小数%） |
| ConfirmDialog | 96 | 确认对话框：玻璃材质、危险样式 |
| Icon | 98 | SVG 图标库：30+ 图标 |

### 6.6 前端关键模式

- **双更新策略**：WebSocket 实时推送 + 5s REST 轮询降级
- **车道注册表自愈**：WS block_start/done 增量记账（有损通道，丢 done = 僵尸车道）+ 每 5s 用 `inflight_blocks`（信号量真实在途）对账纠偏 + GC 超窗回收；对账只替换 id<900000 分析块，保总结合成车道；分析块超 concurrency 时按 startedAt 最早优先截断（防最后一层）；`normalizeRange()` 把人类文案兜底剥成 `chN-chM`（后端事件已带结构化 range，正常路径不触发）；实现抽在 `utils/laneRegistry.ts` 纯函数
- **防抖刷新**：高频 WS 事件（block_done/token_stats）500ms 防抖
- **请求序列守卫**：单调递增计数器防止旧异步响应覆盖新数据
- **空闲自动回退**：ChapterDetailPanel 60s 无操作自动回到最新章节
- **参数持久化**：SummaryPage 使用版本计数器+防抖防止并发 PUT 竞态
- **切书请求守卫三种正确写法（2026-08-24 审计后确立，新增页面必须选用其一）**：
  ①seq 计数器型——适合同一资源反复加载（ChapterDetailPanel/CharacterCardPage.viewCard）；
  ②bookId 快照比对型——适合 watch(bookId) 触发、且 catch 分支也会写状态的页面（Timeline/Graph/Map/Summary.loadReport）；
  ③finally 条件复位型——loading 等互斥标志必须 `if (seq === requestSeq)` 才翻转。
  共同底线：catch 分支同样要守卫；早退分支也要 bump seq；useBookScope 统一上下文方案已评估、决定暂缓（见 10.16），新页面手写守卫时参照本节。

---

## 7. 数据模型

### 7.1 AnalysisResult（analysis_result.py，295行）

9 类结构化输出：
```python
core_events          # 核心事件（id, event, characters, function）
character_arcs       # 角色弧光（surface_action, deep_motivation, change_delta, driver）
foreshadowing        # 伏笔（clue, type, confidence, importance）
plot_holes           # 逻辑漏洞
locations            # 地点（name, parent, type, description）
spatial_relationships  # 空间关系（from, to, relation）
cross_block          # 跨章衔接（summary, unresolved_questions, new_leads）
updated_knowledge    # 知识库更新
long_context_insights  # 长上下文洞察（thematic_elements, character_relationships, world_building）
```

### 7.2 KnowledgeBase（knowledge.py，116行）

12 类跨章记忆：
```python
story_timeline           # 故事时间线
recent_summaries         # 近期逐章摘要
compressed_arcs          # 压缩弧光
character_states         # 角色当前状态
verified_facts           # 已验证事实
long_term_arcs           # 长期弧光
character_relationships  # 角色关系
world_building           # 世界观
thematic_elements        # 主题元素
foreshadowing_network    # 伏笔网络
rolling_summary          # 滚动总结（原始文本）
rolling_structured       # 结构化滚动总结（paradigm_layers/milestones/momentum/causal_chains）
```

### 7.3 配置结构（settings.py）

```python
AppConfig:
  api: APIConfig          # base_url, model, api_key, provider, max_tokens, timeouts, retries, thinking, json_mode
  analysis: AnalysisConfig  # concurrency, block_size, arc lengths, foreshadow params, rolling summary params, foreshadow_recheck_batch_size（默认 40）
  gui: GUIConfig          # language（i18n 接口预留）
  working_directory: str
  knowledge_file: str

APIConfig 预设：8+ 厂商（OpenAI/Anthropic/DeepSeek/Qwen/GLM/MiniMax/Moonshot/...）
```

`AppConfig.from_dict` 经 `_coerce_fields` 对数值/布尔字段做强转清洗（字符串 "4"→4 等，脏配置不再瘫痪启动）；config.json 解析失败置损坏标志位，修复前 `save()` 拒绝写入（防默认配置覆盖真实 API Key）。

---

## 8. 常用命令

### 后端

```bash
# 安装依赖
pip install -r requirements.txt

# 启动后端（开发）
python -m uvicorn backend.app:app --reload --port 8088

# 运行测试
python -m pytest
# （测试全程运行在 cwd/NOVEL_ROOT 双隔离的会话临时根：根目录 conftest.py + pytest_isolation.py
#   在任何 backend.* import 前生效，真实 config.json/queue_state.json/日志/workspace 零写入；
#   守卫见 backend/tests/test_runtime_isolation.py。不要在测试里依赖真实工作区数据。）

# 运行指定测试
python -m pytest backend/tests/test_pipeline_inflight.py -v

# 迭代期：只跑与本次改动相关的测试（约 3 秒 ~ 1 分钟，替代 12 分钟全量）
python scripts/pytest_related.py              # 先看会跑哪些
python scripts/pytest_related.py --run        # 直接跑
python scripts/pytest_related.py --run --base HEAD~3   # 与某个基线比较

# 批次收口 / 推送前：全量，但**并行切组**跑（1588 例约 2.5 分钟，替代 12 分钟）
python scripts/pytest_split.py -n 8

# 只有在需要逐条输出时，才用单进程全量（约 12 分钟）
python -m pytest -o addopts= -q
```

> **测试节奏（2026-11-01 修订，用户明确要求）**：
> 1. **迭代期只用相关集**：`scripts/pytest_related.py --run`（秒级）。不要每改二十行就全量。
> 2. **提交/推送前用切组并行全量**：`scripts/pytest_split.py -n 8`（实测 1588 例 142 秒）。
> 3. 单进程全量 `python -m pytest` 只在需要完整逐条日志时用，平时不要跑。
> 4. 相关集是"我的改动没弄坏自己"，全量是"没弄坏别人"——**宣布完成必须有一遍全量**
>    （切组跑的也算），`docs/VERIFY-LOG.md` 里要写明是全量还是相关集。
>
> **为什么不用 pytest-xdist**：本仓库的 `pytest_isolation` 在导入期 chdir 到临时目录并按
> testpaths 建占位目录，xdist 每个 worker 都这么干一遍，主进程就看不到用例了
> （实测 `-n 8` 直接 "no tests ran"）。`pytest_split.py` 改成按文件切组、各起独立进程。
> **切组必须核对用例总数**：脚本按 `--collect-only` 的每文件用例数分组，
> 曾经因为过滤条件写成 `"error" in line` 把 `test_error_*` 三个文件整份漏掉（80 条），
> 跑完还报"全组通过"——比慢更糟。现在脚本会打印各组用例数之和，必须等于全量数。

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

- Windows 下直接运行 `run_desktop.bat`
- 前端构建产物由 `build_frontend.bat` 生成到 `frontend/dist`
- `desktop.py`：pywebview 启动 → uvicorn 后台线程 → 端口发现 → UTF-8 编码修复

---

## 9. 硬性约束（必须遵守）

### 9.1 密钥与数据安全
- `config.json` 含真实 API Key，**禁止提交、打印、写入日志或纳入任何输出**
- `config.json`、`queue_state.json`、`workspace/`、`*.log`、`app.lock`、`1/`、`.bak_*` 均已被 `.gitignore` 忽略
- **PR-1 修复（2026-09-10）**：所有可能含 key 的错误日志**必须过 `redact()`**（`llm_failure_logger.redact`）。两重防御：
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

### 9.6 ⚠️ 修改后必须更新 agent.md
- **任何代码变更后**，检查本文件中受影响的章节并同步更新
- 更新范围：模块说明、行数、数据结构、API 端点、配置项、已知问题等
- 这是确保后续 Agent 能快速理解项目状态的关键

---

## 10. 当前状态

按时间顺序的变更记录（最早的在前）。每条目保持简短，只记「何时 + 改了啥 + 为啥改」。

### 📝 写作格式（下次添加条目时遵守）

**目标**：1-3 行 / 条目，让 LLM 一眼看懂「**X 在 Y 时候因为 Z 做了 W 改动**」。

**模板**：
```
### 10.X YYYY-MM-DD [主题]
- **改动标题**：`commit_hash` 一句话说明改了什么（可多条）
  - 关键改动 1（如果需要展开）
  - 关键改动 2
- **原因**：为什么要改（一句话）
- **保证 / 收益 / 未动 / 备注**（可选，区分清楚）
```

**示例**：
```
### 10.X 2026-08-20 后端加固
- 全量只读审计 24 确认 / 4 部分 / 1 误报，落地除 H3 外可实锤修复：
  - H1 事件循环冻结：`_auto_scan_workspace` 同步 → 改 `app.py` lifespan `asyncio.to_thread`
  - H2a 章节结果非原子写 → 改 `safe_save_json`
- **原因**：后端稳定性审计
- **未动**：H3 密钥/端口 boot 窗口/token None 等（详见 10.2）
```

**❌ 不要写**（其他工具能查的信息）：
- commit hash 长串列表（用 `git log` 查）
- 每个改动文件的完整路径（用 `git log --stat` 查）
- 代码片段（用 `git show` 查）
- 验证日志（`vue-tsc 0 错`、`pytest 174 passed` 等，跑 CI 自然验证）
- 大表格、3+ 级标题

**✅ 该保留的**：
- 改动类型一句话（如「新增 LocationNormalizer」「砍掉 Phase 0a consolidation」）
- 关键数字（仅当解释 WHY 的核心证据，如「15 万字符阈值」「4/9/20 三种配置」）
- 跨文件影响范围（仅当影响 §4/§5 主架构时）
- 未动 / 已知遗留（避免后续 Agent 重复发现）

### 10.1 Win11 前端重做主体（2026-08-16）
- 2026-08-16 重构 `main.css` 为 Win11 Fluent 设计系统 + AppLayout 标题栏/侧边栏 + 所有页面/组件切换 `--win-*` 变量；删 `sweep.ts`/`ensure-backdrop.mjs`。**原因**：从 macOS 视觉迁移到 Win11。

### 10.2 当前已知未修
- H3 密钥脱敏：用户明确跳过，本地小工具可接受
- `/analysis/logs` 无鉴权：仅绑 127.0.0.1，单用户桌面加 auth 过度设计
- llm_client token None 统计失真、style 快照并发边界、阻塞 IO 二次读、端口锁 boot 窗口残余竞态：需精确行定位/深度重构，未泛泛猜改

### 10.3 2026-08-17
- **复检上下文超限保护**：新增 `RECHECK_FULLTEXT_BUDGET_CHARS=15万` + `_plan_recheck_batches()` 按伏笔埋设章砍前面卷；超限跳过不调 LLM。**原因**：大书全书伏笔复检 prompt 溢出
- **复检批大小配置化**：`AnalysisConfig.foreshadow_recheck_batch_size`（默认 40）暴露到 SettingsPage。**原因**：让用户调省钱 vs 质量平衡
- **Token 统计可见性**：每本书落盘 `token_stats.json` + 新增 `GET /api/books/{id}/token_stats` + StatsPage 按书历史卡片。**原因**：之前看不到每本书花了多少 token
- **桌面端安全**：`desktop.py` 加单实例锁 + 关闭确认 + app.lock。**原因**：防多开、误关丢数据
- **Prompt 预览增强**：支持指定章节号输入
- **工程卫生**：`.gitignore` 补 `app.lock`/`1/`/`.bak_*`；`run_desktop.bat` 用 py 启动器；清理 13 项备份残留

### 10.4 2026-08-18
- **本次窗口会话 token 累计**：分析侧 `AnalysisService.token_stats()` 单例跨书累计 + 总结侧 `SummaryService._session_token_stats()` 内存累计；新增 `GET /api/analysis/token_stats/session`；QueuePage 顶部 5 项卡片（输入/输出/命中缓存/命中率/总消耗）；新增 `test_session_token_stats.py`（3 用例）。**原因**：每次重启都清零，看不到跨书累计成本
- **侧边栏宽度**：280px → 186.67px → 200px（10.5 续调）。**原因**：占空间过大

### 10.5 2026-08-18 GUI Win11 一系列调整
- `645a5f9` style(GUI)：6 文件 +44/-23，ConfirmDialog/LogConsole/QueuePage/AppLayout/Icon/ChapterDetailPanel 全贴近 Win11 Fluent（圆角/字号/字体/控件高度）。**原因**：与新设计系统对齐
- `d32c6ed` refactor(GUI)：2 文件 +29/-22，SettingsPage 抽 `.btn-sm`/`.btn-lg`/`.btn-danger-link` 按钮修饰类 + 去重冗余 CSS。**原因**：inline style 散落不一致
- `f6607a0` A 档审查整改：12 文件 +81/-35，背景 mica 去蓝 + 11 处 4px 网格归一 + 表格交互 + 状态标签 + 统计条软化。**原因**：用户给 11 项审查清单，5 真 6 假，按真问题修
- `dcb3b30` 动效补全：8 文件 +183/-10，新增 `useRipple.ts` 全局 ripple + 卡片 hover/按钮按下/列表 stagger/IconButton hover/错误 pulse/数字 tabular-nums/Toggle 弹簧/进度条缓动/状态标签 hover。**原因**：可交互部件补 Fluent 动效
- `6819b6e` sidebar 横向滚动条：`.nav-list` 加 `overflow-x: hidden`。**原因**：实测发现 ::before 溢出触发浏览器自动升级 overflow-x
- `7e58091` 删除 TokenBadge：删组件文件 + QueuePage 引用（-90 行）。**原因**：左侧 4 个彩色统计是开始分析后忘记删的残留
- `642fb90` 多处微调：sidebar 改 `clip` 不创建滚动容器 + ripple 600→800ms + `--win-duration-fast` 150→200ms + `prefers-reduced-motion` 检测。**原因**：用户本地编辑器顺手改
- `e6336b3` CountUp 动画：新增 `components/CountUp.vue`，QueuePage 5 个 session stats 接入（K/M 缩写 + 1 位小数%）。**原因**：5s 轮询数字跳变不直观，加缓动

### 10.6 2026-08-20 后端加固（审计 bug 修复）
- 全量只读审计 24 确认/4 部分/1 误报，落地除 H3 外可实锤修复：
  - H1 事件循环冻结：`_auto_scan_workspace` 同步 → 改 `app.py` lifespan `asyncio.to_thread`
  - H2a 章节结果非原子写 → 改 `safe_save_json`
  - H2b `os._exit(0)` 前加 `logging.shutdown()`
  - 端口锁竞态 → `O_CREAT|O_EXCL` 原子抢锁
  - config 非原子写 → `safe_save_json`
  - `delete_book` 顺序改为「先回收站成功再改队列」
- **原因**：后端稳定性审计
- **未动**：H3 密钥/端口 boot 窗口/token None/style 快照/阻塞 IO 二次读（详见 10.2）

### 10.7 2026-08-21 角色关系图重构
- 后端：`viz_service.graph_data()` + `routes_viz.py` 加 5 个 query 参数（`chapter_start`/`chapter_end`/`min_edge_weight`/`max_nodes`/`min_node_count`），返回加 `total_characters`/`total_edges`/`filtered`/`chapter_range`
- 前端：`package.json` 加 `echarts` 按需引入；`client.ts` 新增 `GraphNode`/`GraphEdge` 类型 + `getGraph()` params；`GraphPage.vue` 整页重写（ECharts force 力导向 替代环形 SVG，支持缩放拖拽/邻接高亮/标签避让/节点大小映射/社区四色/章节范围 + Top-N + 边权阈值）
- **原因**：原环形 SVG 不支持缩放、节点多时挤成一团

### 10.8 2026-08-21 graph bug 修复（chapter_range 锁死 + 图表白屏）
- **`chapter_range 锁死 [1,1]`**：后端 `chapter_min/max` 从全集算而非过滤后；前端用 `initialized=ref(false)` flag 替代 `[1,1]===[1,1]` 哨兵。**原因**：哨兵判首次导致用户手动输入 [1,1] 被覆盖
- **图表白屏**：抽出 `initChart()` 幂等函数；`renderChart()` 第一行 `initChart()`；`loadData()` 赋值后 `await nextTick()`；`onMounted` 保留空钩子。**原因**：`onMounted` 时 ref 还没挂载（条件 v-else 分支），`if (chartContainer.value)` 永远 false

### 10.9 2026-08-21 地点 + 空间关系归一化（Phase 0）
- 新增 `LocationNormalizer`；`FinalSummaryRunner.run()` 最前面插 4 子阶段（0a-batches → 0a-consol → 0b-batches → 0b-dedupe）；输出 `output/locations_normalized.json` + `output/spatial_relationships_normalized.json`；每章 `chapter_*.json` 加 `_normalized_ref` 字段
- **原因**：LLM 生成的 `locations.parent` 有 8 种值、`type` 有 9 种值、`spatial_relationships.relation` 是自由文本散文，地图渲染节点分散/无法过滤

### 10.10 2026-08-23 归一化解耦 + 砍掉 Phase 0a consolidation
- **改动 1 `93e1494`** 归一化从总结序列移除：`final_summary.py` 删前置检查；`viz_service.map_data()` 强制依赖归一化（无归一化返回 `needs_normalization=true`）；`MapPage.vue` 拦截展示归一化面板。**原因**：归一化失败/卡死会让"分析已完成 → 总结不能跑"成为最差体验
- **改动 2 `2d0d621`** 砍掉 Phase 0a consolidation：删 `_CONSOL_BATCH_SIZE`/`_CONSOLIDATION_SYSTEM_PROMPT`/`build_consolidation_prompt`/`parse_merge_map`/`apply_merges`/`_run_phase_0a_consolidate` 及对应测试（11 个）；174 测试通过。**原因**：跨 batch consolidation 单批串行撞 630s 硬超时，前 5 批结果白做
- **改动 3 `eb80654`** `run_desktop.bat` UTF-8 cmd 修复：所有 `rem 注释` 行改 ASCII；优先用 `.venv\Scripts\python.exe`。**原因**：`chcp 65001` 下 `rem` 行带中文被 cmd 误识别
- **当前归一化架构**：MapPage "运行归一化" CTA → `LocationNormalizer.run()`（独立进程，3 子阶段：0a-batches → 0b-batches → 0b-dedupe）→ 落盘 → MapPage 读；viz_service 检测 `_normalized_ref` 缺失则 `needs_normalization=true`

### 10.11 2026-08-23 全局 LLM 并发 Sem + 风格并行
- `73e998c` `FinalSummaryRunner.__init__` 新增 `self._llm_sem = asyncio.Semaphore(self.concurrency)` + in-flight 计数器；4 个 `_call_llm_*`（summary/reconciliation/recheck/final）+ `_run_style_extraction` 统一包 `_acquire_llm_slot`/`_release_llm_slot`；阶段 1 批内 summary/reconciliation 各自独立 acquire；`run()` 阶段 1 启动时 `create_task(style_task)` 并行；末尾日志 `peak in-flight = X / concurrency = N`
- 新增 `test_global_llm_sem.py`（3 用例，parametrize concurrency=3/9）
- **原因**：用户 API 上限 4~20 不等，原并发模型分散（阶段 1 一个 Sem、阶段 2 一个 Sem、阶段 3/4 无 Sem），风格用独立 LLMClient 完全不受限 — 撞 API 限额
- **保证**：硬上限 = `self.concurrency`，无任何硬编码并发数，配置多少就多少
- **收益**：风格（30-60s）从阶段 4 之前串行 → 阶段 1 启动时并行；总时长缩短 30-60s

### 10.13 2026-08-24 双批代码审计 + 14 个 P1 修复
- 第一批 5 路并行审计报 66 条 → 第二批 5 路对抗复核：53 确认 / 12 部分成立降级 / 1 驳回
- 落地 P1 修复（详见计划文档，已归档到 `_trash_2026-09-10/superpowers_plans/2026-08-24-p1-bug-fixes.md`）：
  - memory_state：分析用 KB 快照注入 rolling_structured（滚动总结此前对 prompt 零生效）
  - pipeline：rolling schema 元素容错 + 补跑后更新异常不再冲出 run()
  - llm_client：APIError body 非 dict 时审核嗅探不再崩穿重试链
  - final_summary：断点加 results 指纹失效机制 + 失效清目录 + stop 级联 cancel 风格任务
  - location_normalizer：全空归一化判败不落盘，空批次计入熔断，缓存短路要求非空
  - delete_book 按显式 workspace_dir 删除；切分保存加运行护栏+暂存交换
  - settings：from_dict 数值强转 + 损坏 config 修复前拒绝 save（保 API Key）
  - 前端：GraphPage ECharts 死 DOM 自动重绑；Timeline/CharacterCard/Summary 三处切书守卫
- **原因**：用户发起的全库双批审计
- **驳回记录**：「自动总结两本书之间互斥窗口」被证伪（analysis.is_running 全程封锁两个入口），勿重复上报

### 10.14 2026-08-24 P2/P3 划算项批量修复（第二批）
- S+A 两档共 19 项：pipeline 快照线程池化/failed 范围过滤/join 容错、解析健壮性双修、json 修复链 dict 守卫+tmp 唯一名、聚合原子写、put_queue 校验、假 done 广播、style_task 异常兜底、workspace 双修、excel 消毒、前端六页守卫/转义/定时器收口、moderation 阈值放宽、markdown 链接保护、client 编码、正文截断、设置页剪枝
- **原因**：P1 修复后对剩余 P2/P3 做性价比筛选落地
- **未动**：KB 近邻指纹、切分窄化算法、协议探测回退等高成本项（详见计划文档排除清单）

### 10.15 2026-08-24 深度修复批次（大改值得七项）
- G1 审核拦截 skipped 标记持久化（重启不再重付 LLM 费用）；G2 KB 近邻增量基线指纹校验+副本断别名（陈旧 KB 不再固化进最终 knowledge.json）；G3 风格 token 接入统计；G4 书目刷新后台单飞（消除事件循环冻结）；G5 预检跨协议探针自动纠正 provider 误判；G6 切分次级格式比额门槛并入（混排书不再吞章）；G7 编码采样罚分择优（Big5 不坠入 gb18030 乱码，两入口预检统一）
- **原因**：审计剩余项中「后果严重度×触发频率」最高、值得动核心逻辑的七项
- **取舍**：normalizer salvage、死代码功能、macOS osascript、polish 级继续搁置

### 10.16 2026-08-24 前端 vitest 基建 + P3 清扫
- 引入 vitest/jsdom/@vue/test-utils 与独立 vitest.config.ts；markdown 自运行脚本迁入；useLogStore/useProgressSocket 守护测试（含退避时序、manualClose 现状锁定）；TimelinePage 竞态守卫组件级代表测试；client.ts 非 JSON 200 显式抛错；viewAggFile/GraphPage 两处状态残留小修
- **原因**：竞态守卫类修复此前无自动化护栏，误删即静默回归
- **决策**：useBookScope 统一上下文评估后暂缓——6 处竞态已修完且新增页面低频，规范写入 §6.6 代替；待第 13 个页面落地时再抽 composable 迁移
- **未动**：OpenAPI 生成 client.ts（规模不够）、ECharts 再加固（已稳定）

### 10.17 2026-08-24 Win11 宿主质感（视觉验证驱动）
- 视觉取证（真实窗口+headless 截图）后落地：desktop.py 透明 WebView2 + DWM Mica（DWMWA_SYSTEM_BACKDROP_TYPE=2，失败静默降级纯色，前端 mica-on 类门控）；main.css Win11 细滚动条（::-webkit-scrollbar）+ mica-on 透明规则；选中态指示条改居中胶囊（3×20px）；移除侧边栏 stagger 入场（Win11 即时渲染）；SettingsPage API 失败显示错误卡+重试（原整页空白）
- **原因**：用户反馈"不是很 Win11"，视觉验证定位差距在宿主层（滚动条/Mica/指示条形状/入场动画）而非设计系统本身
- **两阶段排障**：DWM 调用成功但视觉仍灰白——根因是 WinForms 窗体背景擦除（不透明 BackColor）盖住 Mica，修复=窗体刷子改 alpha≈0（FromArgb(1,0,0,0)，避开 Transparent 特殊分支）；选中指示条按用户反馈贴齐灰色高亮左缘并加高至 22px；回退开关=去掉 create_window 的 transparent=True

### 10.18 2026-08-26 日志轮转彻底修复（H15）
- **根因**：`desktop.py` 的 `_StderrTee` 用独立 `open("a")` 句柄直接写 `crash.log`；与同文件 `RotatingFileHandler` 句柄并存，Windows 下 `os.rename(crash.log → crash.log.1)` 因 `ERROR_SHARING_VIOLATION` 失败；异常被 `logging.handleError` 捕获后又写回 `sys.stderr`（即 crash.log），形成「永远不轮转 + 永远增长」反馈环，实测 crash.log 60MB 无 `.1` 备份
- **修复 1 `desktop.py`**：`_StderrTee` 改为借 `crash_logger` 的 `RotatingFileHandler`（统一流、锁、轮转），不再独立 `open()`；stderr 写入走 `crash_logger.error(s.rstrip())`，控制台原 stderr 同步保留；Windows 句柄竞争彻底消失
- **修复 2 `llm_client.py`**：`FailureLogger` 由 `open("a")` 裸追加改为 `RotatingFileHandler(10MB × 3)`；取消 `_check_reset` 24h-unlink（轮转接管）；JSON 一行一条格式不变，便于 `jq`/grep
- **统一策略**：所有 log 单文件 ≤10MB，备份 3 份（30MB 总占用上限）。`analyzer.log` 沿用 `app.py:53-58` 既有 `RotatingFileHandler(10MB × 3)`
- **回归验证**：255 个 pytest 用例全过（0 改动行为契约）；`crash.log` 备份在下一轮桌面端启动后首次到 10MB 时自动出现
- **原因**：用户报告 `crash.log` 60MB 无轮转备份，根因分析发现 H14 的 `RotatingFileHandler` 修复被 `_StderrTee` 旁路；`api_failures.log` 1.7MB 同样无大小上限
- **未动**：`%LOCALAPPDATA%\NovelAnalyzer\run.log` / `build.log`（非工程内，桌面启动批处理自己管）
- **未动**：标题栏 caption 按钮（Win11 本就低调）、错误横幅（已近 InfoBar）

### 10.18 2026-08-26 M3 端点 thinking 默认差异（运行时经验）
- 现象：《我不可能是剑神》ch457 块（block_size=4，prompt 30K 字符 / 19.6K tokens）M3 卡死 6 次 230s 硬超时，**M2.7 一次过**
- 直接打 API 实测（同 prompt，无 thinking 显式参数）：M3 chat/completions 端点 status=200 elapsed=70.3s，response 中 `completion_tokens_details.reasoning_tokens=**7497**`、completion=11579 —— 证明 **chat/completions 端点对 M3 默认开 thinking**
- 用户观察：Anthropic / Response 端点对 M3 默认**关闭** thinking —— **同一模型不同端点默认行为不同**
- **根因**：M3 的 chat/completions 端点（项目当前默认 base_url）默认开 thinking，对 30K 复杂 prompt 触发 7497 tokens reasoning 链 → 撞 `AsyncOpenAI` HTTP/2 长连接的连接级超时边界（`api.timeout+30=230s` 兜不住）。`thinking_mode={}`（空 dict）= 走端点默认 = 开 thinking，**不是关闭** —— 旧 §10.18 这里写错了，已纠正
- **缓解**（按代价从小到大）：
  1. 临时改 `api.thinking_mode = {"thinking":{"type":"disabled"}}`（项目里 mimo/GLM/M3 的标准关闭语法，llm_client.py 已有路径）—— 一次过，无需换模型
  2. 临时切 `api.model` 到 `MiniMax-M2.7`（无 thinking，秒回，但见 10.8：50-110K 大 prompt 慢）
  3. 切 Anthropic / Response 端点（默认关 thinking，但需确认 base_url 协议兼容）
- **未来加固方向**（未做）：①在 SettingsPage 给 `api.thinking_mode` 加下拉/显式选项（当前是 JSON 自由字段，新手易留 `{}`），并显示当前"开启/关闭"推断状态；②补一个 `/api/analysis/probe_thinking` 自检端点（发 1-token 请求读 reasoning_tokens 字段，零成本探针）
- **关联**：10.8（M2.7 大 prompt 慢）—— 仍有效，仅适用于 M2.7 模型本身；M3 + chat/completions 的卡顿是**端点默认行为**而非模型本身问题
- **教训**：未来调 M3 卡死问题，先看 `thinking_mode` 是否显式 disable（不要假设 `{}` 是关闭），再排查 prompt/连接问题

### 10.19 2026-08-26 probe_thinking v2（多模式检测）
- **问题**：v1 `probe_thinking_params` 只看 `message.reasoning_content` 字段，对 M3 / DeepSeek R1 / QwQ / OpenAI o-series / Anthropic 协议全部误判为"默认无思考"——因为这些模型的 thinking 内容不在 `reasoning_content` 字段里
- **改动**（`backend/core/llm_client.py:probe_thinking_params`）：引入 `_THINKING_DETECTORS`（7 个检测维度，OR 判定）+ 扩展 candidates 4→7 个（新增 `thinking:disabled + reasoning_split` / `chat_template_kwargs:enable_thinking=false` / `reasoning:effort=none`）+ 返回 `detection_breakdown` / `think_tag_chars` / `reasoning_token_count` / `default_detection_breakdown` 字段
- **前端**（`SettingsPage.vue` + `client.ts:ProbeThinkingResult`）：探测结果区加 `<details>` 折叠面板显示 7 维度分项命中；每个 candidate 行加 think_tag_chars / reasoning_token_count 展示
- **测试**（`backend/tests/test_anthropic_provider.py`）：5 个新 mock 测试覆盖 think_tags 嵌 content / usage.reasoning_tokens / Anthropic thinking blocks / 全 clean 时各维度 False / 必返回 breakdown 字段
- **真机验证**（uvicorn 单独跑后端 + 真 M3 API）：
  - 基线：think_tags=True，think_tag_chars=196 字，M3 即便空 prompt 也开 thinking
  - `thinking:disabled` 候选：think_tag_chars=0，所有维度 False，worked=True
  - `enable_thinking:false` 候选：think_tag_chars=296 字，M3 不认此参数，worked=False
- **收益**：M3 chat/completions 卡死问题（10.18）的诊断链路彻底打通——用户现在能在 UI 上看到"基线检测到 thinking 痕迹" + 7 维度分项详情，直接定位"是否需关 thinking"
- **回归**：260/261 pytest 通过（1 个 pre-existing 失败：test_json_repair.py 因 json_repair 包未装，与本改动无关）；npm run build 成功（679 modules）
- **未做**：①探测 prompt 用简单数学题，对 M3 的"按需 thinking"特性有时不触发，复杂 prompt（小说分析）才稳定触发——未来可让探测跑两轮（简单+复杂）取 OR；②SettingsPage 没把 `_THINKING_DETECTORS` 拆到独立 ts 文件共享

### 10.12 相关文档### 10.12 相关文档
- Win11 重做计划：已归档到 `_trash_2026-09-10/superpowers_plans/2026-08-16-win11-frontend-redesign.md`
- 角色关系图重构计划：已归档到 `_trash_2026-09-10/superpowers_plans/2026-08-21-graph-redesign.md`
- 项目 README：`README.md`
- 更新日志：`CHANGELOG.md`

---

## 11. 验证清单

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
# 运行测试
python -m pytest

# 类型检查（如有 mypy）
mypy backend/
```

---

## 12. Agent 行为准则

1. **先读后改**：修改前先查看目标文件和相关上下文
2. **小步提交**：按文件/组件/页面拆分，避免一次性大改
3. **保持行尾**：不要批量格式化
4. **构建验证**：前端改完必须 `npm run build` 通过
5. **测试验证**：后端改完必须 `pytest` 通过
6. **不启动常驻服务**：除非任务明确要求
7. **遇到模糊需求先确认**：不要擅自扩大范围
8. **不修改用户明确要求"不要修改"的内容**
9. **⚠️ 修改后更新 agent.md**：完成变更后，检查并更新本文件中受影响的章节，确保知识库与代码同步

---

### 10.20 2026-08-26 队列运行中实时仪表盘（H17 / S1）

- **根因**：QueuePage 左面板原本只放 ChapterDetailPanel（章节详情），分析运行中无数据可显；用户看不到模型在不在干活、并发到第几块、最近发现什么
- **方案**：左面板 tab 化，新增「运行概览」+「章节详情」二选一；运行概览 = 聚合 4 卡 + 并发车道 + 发现流
- **后端（5 用例 / queue_service.py ~700 行 on_progress）**：
  - 新增 `block_start` WS 消息：转发 status=start（带 chapter/range/progress/total/ts）
  - `block_done` payload 补 `range` 字段（message 复用）
  - 新增 `discovery` 消息（status=done 且 result 存在时）：4 类摘要
    - core_events 数 / foreshadows（截 24 字最多 5）/ 新人物（首次登场 + 停用词过滤）/ unresolved（截 24 字最多 2）
  - 顶层 helper `_extract_discovery(result, seen_characters)` 纯函数好测
  - 类级 `_seen_characters: set` 维护跨块"已见"（断点续跑后全量"首次"是已知限制，标 Open Question）
- **前端（3 组件 + 12 用例）**：
  - `LaneView.vue` 状态机：block_start→占位 / block_done→释放；按 blockId 降序 finishedBlocks 补空槽；并发多块不闪烁（plan 决策 3）
  - `DiscoveryFeed.vue` ring buffer 200，hover 暂停滚动，新伏笔/新人物紫色高亮（#534AB7）
  - `RunDashboard.vue` 容器：4 卡聚合（已完成/ETA/本会话输入输出）+ LaneView + DiscoveryFeed；复用 CountUp 缓动
  - `QueuePage.vue` 左 split-col tab 化：默认 running→「运行概览」，否则「章节详情」；用户手动切换后本会话记忆（plan 决策 2）
  - 消息处理 `onMessage` 增 block_start / block_done（维护 Map）/ discovery 三个 case
- **进度中枢**：useProgressSocket `ProgressMessage` type 联合加 `'block_start' | 'discovery'`
- **信息形态原则（plan 决策核心）**：并发下凡"替换式"显示必闪烁；本方案只选**单调递增**（聚合计数）与 **append-only**（发现流）；车道按 block 固定绑定不互相覆盖
- **不做**（明确范围外）：
  - 不做流式 content_delta 输出（观感差）
  - 不做跨块伏笔去重（数据支撑不足，诚实不做）
  - 不动 ChapterDetailPanel 现有功能（tab 共存不替换）
- **风险与已验证缓解**：
  - 其他 Agent watcher：全程 `h16-recovery` 分支，commit 前 `git status` 双核对
  - 续跑后人物全量"首次"：V1 接受（UI tooltip 标注）
  - 车道渲染开销：纯文本 + Vue 静态提升足够
- **回归**：后端 293 passed / 前端 47 passed / 0 回归；vue-tsc 0 错；vite build 0 错
- **未做（V2 升级）**：车道进度条 + 卡死预警（依赖 H16 Phase 3 token_delta 业务接入，H16 当前仅完成 Phase 1 骨架）；Phase 3 单独排期

### 10.21 2026-08-27 用户实地跑出的 7 个 P1/P2 bug 批量修复

用户用 2 本真实小说（《超级能源强国》4.5MB GBK / 《从姑获鸟开始》6.3MB UTF-8）实地跑全套流程，撞出 7 个独立 bug。一次性修完。

**A. GUI 退出联动 `f439c52`**（`run_desktop.bat`）
- 旧：开 GUI 一个动作、关三个窗口（cmd + tail + GUI 都不联动）
- 改：bat 顶部加 `chcp 65001 >nul`（cmd 按 UTF-8 解析 bat，含中文 start 标题不乱码/引号不错位）+ GUI 退出后 `taskkill /FI "WINDOWTITLE eq NovelAnalyzer 运行日志" /T /F` 关掉独立 tail 窗口
- 关键：`taskkill /FI WINDOWTITLE` 比 `Start-Process -PassThru + PID` 简单无数；PowerShell `-NoExit` 不改 host title 所以标题稳定
- 附带：`chcp 65001` 顺便修了 `echo [ERROR] 未找到 python` 等中文 echo 的乱码

**B. GBK 4.5MB 小说"无法使用任何编码读取" `c1f3750`**（`backend/utils/text_utils.py` + `splitter_service.py`）
- 三个独立 bug 叠加：
  1. `_penalty_sample` 用 strict decode，256KB 采样窗口结尾切到 GBK 2-byte 字符尾字节 → `incomplete multibyte sequence` → GBK 整个被排除
  2. `splitter_service.ENCODING_PRIORITY` 缩水版（`["utf-8","gbk","gb2312","big5","utf-16"]`）缺 `gb18030`（GBK 超集），splitter 路径独有
  3. `latin-1` 兜底"任何字节都能解" → penalty 永远 0 → 盖过真正 GBK 解码
- 修法：(1) decode 改 `errors='replace'` 边界字符变 U+FFFD；(2) splitter 删本地 list 复用 `ENCODING_CANDIDATES` 常量；(3) 加 `_SINGLE_BYTE_BIAS=0.001` 给 1-byte 编码（latin-1/cp1252/ascii）基础偏置
- 测试：`tests/test_text_encoding_sampling.py`（3 用例，含用户真实 4.5MB 文件回归）

**C. 整本书切成一章（沉默失败） `1350cb3`**（`splitter_service.py` + `SplitterPage.vue`）
- 两个独立 bug：
  1. `mode=custom` + 不匹配 pattern（用户瞎写/写错）→ 0 命中 → 沉默当 1 章
  2. `min_words=10000` 阈值过高（网文章节大多 2000-5000 字）→ 过滤后剩 1 章
- 修法：(1) `_chapter_regex_for_mode` 编译成功但 0 命中回退 auto + warning；(2) min_words 过滤后 < 5 章且 ≥ 5 章过滤前 → `raise ValueError` 含调参建议
- 前端：`SplitterPage.vue:22` pattern 默认值 `'第[...]+章'` 改 `''`（custom 模式空 pattern 走后端 auto 兜底，去掉陷阱）
- 测试：`tests/test_splitter_silent_failure.py`（7 用例）

### 09-02 切章器国际化 Phase 2-3 修复（P1）

**目标**：把 `backend/services/splitter_service.py` 改成"包罗万象"切章器，覆盖 7 本 Project Gutenberg 外文书 + 4 类边界 case + 中文回归 0 破坏。

**Phase 2 核心修复**（T2.1a/b、T2.4、T2.5b、T2.6、T2.7）：
- **T2.1a 英文章节正则**：`CHAPTER_PATTERNS` 加 2 条——`[Cc]hapter\s+[IVXLCDM]+[\.\s:]?`（罗马数字）+ 裸 Roman 短篇（Sherlock 风格）；收紧原 `^\d+[\.、]\s*\S` 排除 Gutenberg license 的 `1.A.`/`1.B.`
- **T2.1b 章节号提取**：`_extract_chapter_num` 支持 Roman（`_roman_to_int`，含 `I` 单字符兜底）+ 英文数词（`_word_to_int`，含连字符/空格复合）
- **T2.4 卷归属**：`VOLUME_PATTERNS` 加 `Book the First/Second/...` + `Book [IVXLCDM]`（Dickens 风格）；`_normalize_volume_label` 同步把序数词映射为 Roman
- **T2.5b TOC 跳过** + **T2.7 PG header 提取**：见 §5.1 splitter_service 段

**Phase 3 边界加固**（T3.1-T3.6）：新增 `tests/test_splitter_edge_cases.py`（8 case：空文件/单章节/5 行诗集/20MB 单章 + `max_words=1M` 自动拆多章等）；`tests/conftest.py` 注册 `@pytest.mark.slow` marker。

**Phase 4 已知限制**：Dracula 日记体无显式 Chapter 标记（仍可识别 27 章 Roman）/ Moby-Dick TOC 重复（`dedup_same_key` 兜底后残留 1-2 条）/ 裸罗马短篇正则要求全大写 2-80 字符（小写标题不识别）/ pre-START 头 Title/Author 提取只对 Project Gutenberg 文件有效。

**回归结果**：
- baseline 7 本：7/7 章节数达标 + 7/7 metadata 含正确书名/作者
- 边界 8 case：8/8 通过
- 中文回归 16（mixed_formats 7 + atomic 2 + silent_failure 7）：16/16 通过
- `pytest backend/tests/`：421/421 通过

**未做**：不动 Phase 1 产物 / 不 git commit / 不重写整个 splitter_service.py / 日文片假名章节不识别。

详细 plan：[`docs/PLAN-splitter-i18n-2026-09.md`](docs/PLAN-splitter-i18n-2026-09.md) · 交付报告：[`docs/DELIVER-splitter-i18n-2026-09.md`](docs/DELIVER-splitter-i18n-2026-09.md) · baseline 报告：[`docs/REPORT-splitter-baseline-2026-09-02.md`](docs/REPORT-splitter-baseline-2026-09-02.md)

**D-G. 书名推断 `1ae7cc4` + `fdc3b8d` + `25c455b`**（`splitter_service.infer_book_name`）
- 旧：硬编码 suffix 白名单（校对版/完整版/全本）剥离 → 永远漏（精校版/精修版/典藏版/完结版/校对后/无括号版 全部漏网）
- D. 主路径改用 `re.search(r"[《<]([^》>]+)[》>]", name)` 截取《...》之间内容（截不到时回退）
- E. fallback 路径用通用 regex 覆盖分隔符变体（连字符/下划线/点/空格）+ 可选括号；版本词白名单扩到 12 个
- F. 双层书名号 `《《xxx》》` 修：P5 regex 在双层上输出 `《《xxx》`（少一个外层 `》`）；改用 `《+([^《》]+)》+` 自动找最内层
- 真实案例：用户上传 `《《大王饶命》》（精校版）.txt` → 修前《《大王饶命》（错位）→ 修后《大王饶命》
- 测试：`tests/test_infer_book_name.py`（58 用例 P5b）+ `tests/test_infer_book_name_dquote.py`（15 用例 P5c）

**H. _archive_item 搬目录后没更新 item.workspace_dir `56098be`**（`queue_service.py`）
- 现象：用户跑 `《大王饶命》` → 分析完成 → `auto_archive=true` 触发 `shutil.move(workspace/《大王饶命》/, workspace/分析结果/《大王饶命》/)` → `item.workspace_dir` **仍指向旧路径**（已搬走）
- 后果链：`_books["《大王饶命》"] = item.workspace_dir`（旧路径）→ `get_book_path` 命中后 `path.exists() False` → `KeyError("书目不存在")` → auto_summary 失败
- 修法：`shutil.move` 成功后更新 `item.workspace_dir = target` + `item.blocks_dir = target/"blocks"` + `self.save_queue()` 持久化
- **遗留状态**：用旧版本已归档的书，队列项里 workspace_dir 仍是旧路径，需用户重启或点"扫描工作区"重建
- 测试：`tests/test_archive_path_update.py`（2 用例）

**用户报告 vs 我猜测的方向错位（教训）**：
- 报告"《《大王饶命》》分析完成"——我以为是双书名号文件名（regex bug），实为单层 + log 装饰 `《{item.name}》` 加的外层
- 报告"书目不存在: 《大王饶命》"——我以为是书目录命名错位（splitter bug），实为归档搬走但路径引用没更新（queue_service bug）
- **教训**：下次类似报告先看 `workspace/` 实际目录结构再下结论，不要按"双层《》就当 regex 问题"线性推断

**关联 commit**（H16 收尾 P1/P2 在 10.20 之前）：
- `fa0f95e` recheck 阶段补 total_batches + 停止路径 sweep + range 补 /total
- `61fd242` 最终总结阶段→LaneView 兼容（summaryLanes helper）
- `5f654c4` 并发数显示/上半部分对齐（status.concurrency 暴露 + col-head 38→56px）
- `c258e77` 实时速率聚合 + 已完成块去浮点（Math.round）+ 日志卡底色对齐
- `6299a23` 并发模式补发 block_start（修 activeBlocks=0）
- `086a296` `run_desktop.bat` 开独立 tail 窗口（PowerShell Get-Content -Wait）
- `5e1597b` RunDashboard 套 .glass-card
- `8907e24` Pydantic CompletionUsage 无 .get() 兼容（`_get_reasoning_tokens` helper）
- `3fd8d57` P1-a 流式中断保留 partial content + P1-b `_seen_characters` 跨书重置

**回归**：本 session 累计后端 366/366 passed；测试文件 43 → 48；4 个 other Agent 文件（`test_anthropic_provider.py` / `client.ts` / `SettingsPage.vue` / queue-live-dashboard plan）保持 ` M` unstaged 不动

### 10.22 2026-08-27 伏笔组合因子排序 + 排行视图
- **组合因子排序**：`_build_foreshadow_catalog` sort_key 从 `imp×100 + conf×10 + evidence_count(≤99)` 改为 `imp×100 + conf×10 + evidence_count(≤50) + span(≤49)`。**原因**：纯 evidence_count 对早期隐蔽伏笔结构性歧视（好伏笔越隐蔽检测次数越低，越易被截断）。组合因子让"跨章时间范围"(span) 和"被检测次数"(evidence_count) 各贡献一半权重。
- **composite_score 保留**：sort_key 计算后不再 pop，改名 `composite_score` 保留到 catalog 条目 + ledger 条目。
- **ForeshadowItem 扩展**：加 `composite_score: int = 0` + `importance: str = "中"`，SCHEMA_VERSION 升到 3。
- **排行 API**：`GET /api/books/{id}/foreshadow_ranking` 从 ledger 读+算分+降序返回。
- **前端排行视图**：TimelinePage 加第三个 mode "伏笔排行"，按分数降序列表，含分数条+span+evidence_count+status+⚠️标注。旧数据（无 composite_score）实时计算兜底。
- **未改**：`max_foreshadow_catalog_high` 默认值仍为 500（用户可在 GUI 手动改到 1000）。

### 10.23 2026-08-27 伏笔总表日志 Bug 修复
- **症状**：日志 `伏笔总表构建完成：2431 原始 -> 1395 入总表（高 899, 中 1234, 其他 296）` 看起来截断没生效（中 1234 > max_mid=200 但日志没显示截断信息）。
- **根因**：`final_summary.py:911-913` 日志打印 `len(high_items)` / `len(mid_items)`（截断前原始数），不是 `truncated` 里实际高/中/其他数。
- **修复**：加 `high_kept` / `mid_kept` 变量跟踪实际入表数，日志改为 `高 899/899, 中 200/1234` 格式，截断信息也补全到 INFO 级别。
- **截断逻辑本身正确**：1234 → 200 = 1034 条中桶伏笔被截断，与 1395 总数（899+200+296）一致。
- **回归**：老 ledger JSON 反序列化兼容（新字段默认值 0/"中"）；排行端点对旧数据实时算分兜底。

### 10.24 2026-08-31 运行概览车道堆叠修复（车道注册表自愈）
- **症状**：用户上报长程运行后车道计数堆叠「57 / 8 运行中」，功能无影响。WS 挂测试客户端三轮实测定位：真实 LLM 并发恒 = 配置值（信号量从未超卖），是前端车道记账腐烂——「start 未 done」稳态虚高 ~2.5 倍（done 由消费循环补发且每 8 块阻塞等 rolling 1-3 分钟），叠加 WS 通道有损（hub 每连接队列 maxsize=1000 满了丢最旧、断线无重放、前端无对账），丢一条 done = 永久僵尸；续跑补发洪峰（916 块 ~2700 条消息必打满队列）+ 失败风暴为触发点
- **修复**（subagent 复审通过并按其 4 条修正落地）：
  - 后端 pipeline：`_analyze_one_block` try/finally 登记 `_inflight_blocks`（预热/并发/补跑单点覆盖）+ `inflight_blocks()` 快照；`_worker`/`analyze_block_with_progress` 异常兜底带原 block_id 发 failed + 补 `state.add_failed`（原兜底固定发 chapter:0 被前端忽略 = 隐藏泄漏路径）；续跑补发每 50 条 sleep(0)
  - queue_service：`status()` 暴露 `inflight_blocks`（getattr 兜底 `__new__` 单测）；`skipped` 也转发 block_done
  - progress_hub：队满分级驱逐 token_delta → log → 保 block_start/block_done 等关键事件
  - 前端：新增 `utils/laneRegistry.ts`（`gcActiveBlocks` 僵尸回收 + `reconcileActiveBlocks` 真实在途对账，纯函数可测）；QueuePage `syncLaneRegistry()` 挂 `refresh()`（5s 轮询 + block_done 防抖），GC 同步清 tokenByBlock/rateByBlock；GC 跳过 ≥900000 总结合成车道 + has-token 门控（复审修正项）
- **原因**：车道注册表纯增量记账，无对账、无超时回收，任何丢失永久累积只能刷新页面清零
- **回归**：后端 420 passed（+6，54 文件）/ 前端 vitest 77 passed（+11）/ vue-tsc 0 错 / vite build 通过；改动未提交（工作区另有前 session 的 31 个 ` M` 文件不动）
- **未做**：rolling 等待与消费循环解耦（消除 done 延迟基线，行为改动大，先观察自愈效果）

### 10.25 2026-09-10 API Key 日志泄露修复（PR-1，D 方案）
- **症状**：9 处 `f"...{str(e)}"` 错误处理无 redact；`FailureLogger.record_failure` 直接 `str(error_message)[:500]` 落盘；`/api/analysis/logs` 端点无任何鉴权一键下载完整 `analyzer.log`（含 key 泄露风险）
- **修复**：
  - **新工具** `backend/core/llm_failure_logger.py::redact()`：4 个正则（OpenAI sk- / Anthropic sk-ant- / Authorization Bearer / x-api-key），**显式长度 20+ 避免误伤短串**，**fail-open**（异常时返回原文）
  - **双层防御**：`FailureLogger.record_failure` 入口对 `error_message` 自动 redact + `app.py` 新增 `_SensitiveFieldsFilter`（logging.Filter）注册到 `_file_handler` 二次过滤所有 `analyzer.log` 落盘 record
  - **9 处 `str(e)` 全部走 `_redact(str(e))`**（`llm_client.py` 含认证失败 / 请求超时 / API 错误 / 连接失败 / 未知错误 / 流式异常等 6 类异常路径）
  - **D 方案鉴权**：`backend/api/auth.py` 新增 `require_session_token` Depends，读环境变量 `NOVEL_ANALYZER_SESSION_TOKEN` + `X-Session-Token` header，`hmac.compare_digest` 防时序攻击；`/api/analysis/logs` 端点加 `dependencies=[Depends(require_session_token)]`
  - **desktop.py**：启动时 `secrets.token_urlsafe(32)` 生成 token，写入 `session.token` + 设置环境变量；`_on_closed` 退出时清理
  - **测试**：`test_redact.py` 14 case（含 3 个反向：短串不误伤、已脱敏不重复、Retry-After 不误伤）+ `test_session_token_auth.py` 4 e2e（开发模式放行 / 无 header 403 / 错 token 403 / 正确 token 200）
- **原因**：用户/上游 API 异常 message 可能含 key；`/analysis/logs` 端点裸奔一键下载 30MB 日志是定时炸弹
- **未做**：D 方案"桌面端零摩擦"需要前端 `client.ts` 拦截 fetch 加 `X-Session-Token` header（独立 PR，避免本 PR scope 蔓延）；**当前桌面 webview 调受保护端点会被 403**——这是已知折中
- **回归**：PR-1 新增 18 case 全过；核心 LLM 关联测试（test_llm_moderation_sniff、test_global_llm_sem、test_provider_heal、test_anthropic_provider 首次跑、test_llm_mock 首次跑、test_streaming 首次跑）均无破坏；完整 pytest 验收待独立项装 `pytest-timeout` 后补做（test_anthropic_provider/test_streaming 既有 hang 测试需 timeout 防御）
- **新约束**（已写入 9.1）：未来加新错误字段时**必须**也走 `redact()`

### 10.26 2026-09-12 README 去 AI 味重写
- **改动**：拆 4 个 docs（architecture/configuration/api/troubleshooting）+ README 简化至 150 行
- **原因**：README 之前 ~10KB 偏 AI 产品发布稿风格（"深度""一键""全流程""双保险""秒级""零数据丢失哲学"等高频词密集），拆 docs 后定位更清晰
- **未动**：用户其他 session 10 个 M 改动（backend/config/ 等）由用户自己处理
- **顺手修**：第 17 行测试数字 420 → 427（v3 plan 独立项发现的失真；README 同步写"约 427 用例"并标注以 pytest --collect-only 实时输出为准）

### 10.27 2026-09-12 添加 Apache 2.0 许可证
- 仓库根加 `LICENSE` 文件（标准 Apache 2.0 文本 ~200 行）
- README "许可证"段改为"本项目使用 Apache License 2.0"
- **协议选择**：Apache 2.0（不是 MIT），原因：
  - 多一层专利授权（不亏）
  - 大公司看到 Apache 更愿意集成
  - 几乎所有"赚点小钱"路径（服务/咨询/捐赠）都不被协议影响
- **隐私保护**：Copyright 用 GitHub username（`mojingjing-git`），不暴露真名
- **状态**：仓库仍 private；等用户"再优化优化"后转 public（届时 LICENSE 已在位）
- **未做**：完整 NOTICE 文件、CONTRIBUTING.md（公开项目时再补）

---

### 10.28 2026-09-28 切章器支持 DOCX 输入
- **改动**：新增 `backend/utils/docx_extract.py`（纯标准库）+ `splitter_service._detect_and_read` 按后缀分派 + `desktop.py` / `SplitterPage.vue` 两处文件过滤器
- **原因**：用户的小说源文件大量是 Word 格式，原先只能手动另存为 txt
- **策略——入口归一化**：`_detect_and_read` 是全链路唯一文本入口（仅 preview_split / save_split 两个调用方），在此做格式分派即可让下游切章逻辑**零改动**；`requirements.txt` 无需加依赖
- **核心洞察**：15 条章正则全带 `^` 锚点 + `len(s) <= 80` 整行闸门，其成立前提是「一行一个语义单元」。TXT 天然满足，DOCX 无换行符必须显式重建 —— 本改动本质是**重建前提**而非重写规则
- **plan 已独立复审**：plan v1 派 agent 复审挑出 3 阻塞 + 2 重要项并全部修订（`docs/PLAN-docx-input-2026-09-28.md` §10）：
  - A 验收原要求 `word_count` 相等 → 实为 `len(body)` 字符数，DOCX 补空行必然漂升 → **移除该断言**，改断言 `detected_pattern` 一致
  - D2 原拟的「≤30 字短行启发」被证伪：会污染 `lines[:800]` 采样窗口稀释标题密度，可能选错正则 → **默认关闭**
  - 加密 docx 抛 `RuntimeError` 而非 `BadZipFile` → 补显式分支，否则被吞成 500
- **知识（后续已修）**：当时根目录 `tests/` 因 `pytest.ini` 的 `testpaths = backend/tests` 未参与默认收集；2026-09-29 清理批次已把 `testpaths` 扩为 `backend/tests tests`
- **未做**：EPUB（复杂度显著更高：opf:spine 排序 + 单 xhtml 多章 + DRM 三类问题）
- **实现后复审挑出的 2 个阻塞项（已修）**：① 标题前后补空行导致 `blocks` 正文比 TXT 多 2-3 空行（会推后 style_analyzer 采样偏移）→ 改为「一个 `<w:p>` 一行、不补空行」；② 表格场景 Case 3 断言太弱只测「文本里有」→ 改为 ≥6 章并要求 `detected_pattern` 正确。**教训**：四元组等价性断言测不到正文漂移，必须比对落盘 `blocks` 内容
- **回归**：463 passed（443 → +20 新增，0 破坏）/ 前端 vitest 77 passed / `vue-tsc` + `vite build` 通过

### 10.29 2026-09-30 前端测试深度加强（84 → 225 用例）+ coverage 基建

- **改动**：前端 vitest 84 → 225 用例（31 个测试文件），页面覆盖 1/12 → 7/12（新增 Queue/Summary/Settings/Style/Splitter/Workspace 六页测试），组件覆盖 3/13 → 10/13（新增 BookSelector/ProgressBar/ConfirmDialog/LogConsole/ChapterValue/Icon/ChapterDetailPanel）
- **纯函数抽取**（沿用 laneRegistry/summaryLanes 先例，逻辑原样搬移、props/事件/API 不动）：`utils/queueFeed.ts`（QueuePage onMessage 状态机）、`utils/summaryProgress.ts`（4 阶段加权进度）、`utils/settingsForm.ts`（pruneEmptyStrings/422 映射/思考模式 JSON）、`utils/splitterView.ts`（F-2 回滚/卷分组）、`utils/workspaceView.ts`；抽取层 coverage 全 100%
- **基建**：devDependencies 加 `@vitest/coverage-v8@^4.1.11` + script `test:coverage` + vitest.config.ts coverage 段（v8/text+html，排除 *.test.ts）
- **顺手修 1 个真 bug**：SummaryPage `paramsLoaded` 门闩时序缺陷——门闩在赋值后同步置位，但 Vue watcher flush 在下一个微任务，服务端值 ≠ 默认值时挂载即触发一次多余 PUT；修复 = 置位前 `await nextTick()`（本轮 watcher flush 跑完再开门）
- **测试写作教训**（复用既定范式）：mock 调用计数跨用例污染 → beforeEach `vi.clearAllMocks()` + 日志单例 `useLogStore().clear()`；`emitted()` 从未 emit 返回 undefined 不可直接改 length；`emitted('update:chapter')` 基线用 `(wrapper.emitted(x) ?? []).length`；文案断言注意「双重书名号」既有 UX（QueuePage/WorkspacePage 弹窗）
- **coverage 基线（2026-09-30）**：全仓 60.4% stmts / 55.2% branch / 44.8% funcs；utils 层 98.5%（funcs 100%）、components 76%、pages 51.7%（未测的 CharacterCard/Graph/Map/Stats/PromptPreview 拖低，ECharts 三页优先级最低）
- **回归**：前端 vitest 225 passed / `vue-tsc -b` 0 错 / `vite build` 通过；后端无改动
- **未做**：CharacterCardPage/GraphPage/MapPage/StatsPage/PromptPreviewPage/AppLayout/useRipple/vTooltip 测试（低频或 DOM 重、收益低）；markdown.ts 既有 15 例维持

### 10.30 车道显示一致性 + 计数准确性修复（归档）

- **改动**：`b901795` WS 事件结构化 + 前端车道文案归一与超并发截断（明细见 CHANGELOG [Unreleased] 同名条目与 `docs/PLAN-lane-display-concurrency-2026-09-28.md` §7）
- **原因**：车道面板混两种文案（WS 人类可读格式 vs REST 对账规范格式）+ 计数可超并发池上限（WS 增量记账丢 done 虚高）
- **目验**（2026-09-30 真实队列跑大明尾部补跑）：活跃车道全程 `chN-chM` 单一格式、计数恒 ≤ 并发值、审核拦截失败块车道即时释放跑完清零；**已知伪影**：续跑补发 DONE 不带 range（pipeline.py:337 有意限速）→ finished 槽位 `block N` 兜底占位，非 bug
- **回归**：pytest / vitest / build 全绿；plan 已翻 done（本仓库 PLAN 首个 done 态，约定自本文件起确立）

### 10.31 PR-1 收尾：前端 X-Session-Token 注入（修桌面端 403 折中）

- **改动**：`desktop.py` Api 类加 `get_session_token()`（js_api 返回 `NOVEL_ANALYZER_SESSION_TOKEN`，未设返回空串）；新增 `frontend/src/api/sessionToken.ts`（token 模块级单例 + pywebviewready 双路径就绪）；`client.ts` request() 统一附加 header（合并顺序：Content-Type → 调用方 headers → token）+ 新增 `downloadLogs()`；`QueuePage.vue` 下载按钮 `<a href>` → `<button @click>`
- **原因**：PR-1 已知折中——日志下载是 `<a href>` 顶层导航（不走 fetch），且 request() 对 200+非 JSON 显式抛错而该端点返回 text/plain，桌面端调受保护端点必 403。下载改走独立 fetch + blob，token 不复用 request() 的 JSON 契约
- **关键事实**：受保护端点仍仅 `GET /api/analysis/logs` 一个；信任域前提 = webview 只加载本地 dist 不渲染远程内容
- **测试**：client.test.ts +5（header 附加/null 不附/POST 共存/downloadLogs 200·403）+ sessionToken.test.ts 5 例（init 双路径 + js_api 异常静默 + **与 desktop.py 方法名契约一致性源码断言**）+ test_desktop_pythonw.py +1（源码级，不 import desktop.py——顶层副作用会污染 pytest 进程）
- **e2e**：env 设 token 后 no header 403 / 正确 header 200 / 错 token 403 实测通过
- **回归**：pytest 8 例相关全过；vitest 相关 14 例全过

### 10.32 思考模式状态提示 + 总结模型独立探测（plan 2026-09-29 之三）

- **改动**：`settingsForm.ts` 加 `inferThinkingState()`（五禁用语法三态判定）+ `makeThinkingPresets()`（两页预设单一来源，首项 label 按页传入——主模型 `{}` = 跟随端点默认、总结 `{}` = 回退全局，语义不同）；SettingsPage/SummaryPage 下拉旁常驻状态提示（`{}` ⚠ / 显式禁用 ✓ + Anthropic caveat）；SummaryPage `{}` 两级组合显示（全局已禁用不误报）；`routes_settings.py` `ModelPreviewRequest` 加可选 `model`（probe 端点支持总结模型独立探测，空值回退主模型逐字节等价）；SummaryPage 加「自动探测」按钮（命中自动填入 + 触发持久化）
- **原因**：M3 事故（10.18）未来加固①只完成一半——`{}` 的"默认可能开启"风险不点探测不可见；总结模型无探测而重型总结最易撞思考链超时
- **测试**：settingsForm.test.ts +9 / SummaryPage.test.ts +4（mock 补 probeThinking，10.29 组件测试工厂原本缺该方法）/ SettingsPage.test.ts +3 / test_settings_probe_route.py 2 例（新建，路由层参数透传，与 test_anthropic_provider 的函数级测试分层）
- **回归**：后端 486 collected 全过 / 前端 vitest 249 passed / `vue-tsc -b` + `vite build` 通过
- **plan**：`docs/PLAN-thinking-mode-status-probe-2026-09-29.md`（已过独立复审，3 阻塞 + 3 重要项全部修订后实施，见 §7）

### 10.33 第三批清理：重复逻辑收敛与旧数据兼容审计（Step0–R5，Step6 暂停）

- **改动**：Step0 测试隔离基建（根目录 `conftest.py` + `pytest_isolation.py`：任何 `backend.*` import 前 cwd/`NOVEL_ROOT` 切会话临时根 + 合成 config + 剥密钥环境变量 + 单例重置；守卫 `test_runtime_isolation.py` 含子进程回归与正向对照）；Step1 删 `CharacterCardGenerator.long_context_insights` 无消费者读链、恢复 `WSType.PING`（心跳唯一不带 payload）、修本文件两处失实（networkx/pyvis 历史依赖 + `test_pipeline.py`→`test_pipeline_inflight.py`）；Step2 三条生产路径的流式 Token 进度回调收敛为 `backend/core/stream_progress.py::make_stream_progress_callback`（计时/过滤/速率/await 契约逐字段等价）；Step3 JSON 解析现状矩阵固化为 `test_json_parse_boundaries.py`（134 例，四处入口两两不等价故**不抽取**，仅删 `extract_foreshadow_json` 不可达 list 分支）；Step4 角色名后缀单一来源（`aggregate_utils.CHAR_NAME_SUFFIXES`，viz 引用；书名两条规则审计后保留各自职责）；Step5 生命周期审计结论保留重复（`test_service_lifecycle_matrix.py` 26 例矩阵）
- **原因**：消除已证实无用依赖与重复实现；每步均过独立主审多轮复审（返修史见 `docs/REPORT-cleanup-batch-3-2026-10-14.md`）
- **测试隔离关键事实**：第一轮隔离方案（写后移除 handler）曾向真实 `api_failures.log` 写入 14KB 后逐字节还原；现行方案为 import 前 cwd 隔离，五真实文件 SHA256 全程不变
- **未做**：Step6（旧 schema 兼容）用户要求先讨论——已确认缺陷：`models/knowledge.py` 的 `_legacy_text` marker 在「旧层字段 → from_dict → to_dict → 再 from_dict」两轮往返后 `rolling_structured` 变 `{}`（旧滚动摘要静默丢失），修复须先写失败回归；伏笔 batch 字段仍在写入，删除/迁移另立方案
- **回归**：各步定向集与全量数字见 REPORT（口径不一，以各步实际命令为准）；当前默认收集 **827** 用例（2026-10-25 实测）；前端 249 passed / vue-tsc 通过（清理未触前端）；Ruff F401/F811/F841/F821/F822/F823 全过；每步独立 patch 存 `docs/patches/`
- **plan**：`docs/PLAN-cleanup-batch-3-2026-10-14.md`（Step6/R6 未授权，不自动回访）

### 10.34 厂商错误码分类体系 error_taxonomy（plan 2026-09-30，两轮复审后实施）

- **改动**：新增 `backend/core/error_taxonomy.py`（ErrorClass 11 类 + RETRY_POLICY + `classify_exception`/`classify_message` 双通道 + 各厂商结构化码表，优先级 MODERATION→BILLING→CONTEXT→AUTH→BAD_REQUEST→RATE_LIMIT→SERVER→NETWORK→TIMEOUT 写死）；moderation.py 改转发层（`[MODERATION]` 契约零改动）；llm_client 流式生成器 except 分类+标记+Retry-After 提取（原 bug：流式异常不做审核嗅探，558+5525 条日志全链白烧）、重试循环 4 处 `_error_action()` 决策（stop 类首次即停+可读提示、structured/输入侧审核首次短路、text 维持 3 次）、`_is_429` 子串判定 6 处替换；pipeline 补跑 fatal 过滤（`_fatal_blocks`，欠费场景整场浪费 剩余块数×4 → ×1 次调用）
- **原因**：日志实证三类浪费——MiniMax 1027/422 输入拦截被当普通失败烧 13-16 次/块（重试成功率恒为零）、402 计费错烧全链、429 伪装的账单错（OpenAI insufficient_quota/智谱 1113）被当限流退避；调研 11 家厂商官方文档收敛码表（"按 HTTP 状态码必错"，billing 必须按业务码白名单）
- **判侧设计（复审 P2-5）**：审核码逐条带 input/output 侧，双侧默认 OUTPUT；MiniMax 皮肤 422 `unprocessable_entity_error` 条件规则区分输入审核 vs 真 Anthropic 参数错；`refusal`/裸词码仅 body 通道防消息误报
- **测试**：`test_error_taxonomy.py` 37 例（日志真实串 gold fixtures + P2-4 歧义样本 + T2.1/T2.2/T2.4 集成）；既有 test_moderation/test_llm_moderation_sniff/test_moderation_threshold 零改动全过（text 置信 3 次短路语义是守卫）
- **plan**：`docs/PLAN-provider-error-taxonomy-2026-09-30.md`（§8 独立复审 4 项 P2 + §9 二次复审 P2-5 共 5 项修订并入正文后实施）

### 10.35 PLAN v2 S0：测试环境噪声根治 + CI + 合成语料（2026-10-30）

- **改动**：`pytest_isolation.py` 新增 `normalize_no_proxy()` / `_proxy_url_is_parseable()` / `sanitize_proxy_env()` / `assert_proxy_env_sane()`，`install()` 内在任何 `backend.*` import 之前调用净化；根 `conftest.py` 的 autouse fixture 前后双向校验；新增 `.github/workflows/ci.yml`（backend Windows+Ubuntu 矩阵、frontend npm ci+vitest+build、hygiene 拦截误跟踪产物）、`requirements-dev.txt`（含 `pytest-timeout`/`pytest-cov`）、`backend/tests/fixtures/vnext_corpus.py`（24 章确定性语料 + `CorpusTruth`）、`backend/tests/test_vnext_corpus_fixture.py`（7 例）、`backend/tests/test_pytest_isolation_proxy.py`（21 例）、`docs/BASELINE-2026-10-30.md`
- **原因**：本机 `NO_PROXY` 含方括号 IPv6 `[::1]`，httpx 当 URL 解析 → `Invalid port: ':1]'` → `AsyncOpenAI` 构造期抛 `InvalidURL`，凡构造 SDK 客户端的用例整片假失败（同环境 74 failed / 790 passed）。此前只能靠「进程内手工清代理」绕过，每次新机器/新会话都要重查一遍
- **要点**：只改本进程 `os.environ`，不触碰宿主全局环境；可解析的代理原样保留，因此依赖代理的 CI 不受影响；CI 显式声明**不带方括号**的 `NO_PROXY`
- **验收**：原始未清理代理环境下 `python -m pytest` → 892 用例全绿（S0 前 864）；`--collect-only` 3.14s
- **未做**：本批**不含功能修复**，C-01 等全部行为缺陷留待 S1 及后续批次；v1 产物零迁移
- **plan**：`docs/PLAN-v2-long-novel-reliability-2026-10-30.md` §S0；问题清单见 `docs/REPORT-review-consolidated-2026-10-30.md`

### 10.36 PLAN v2 S1：错误判定贯通与 C-01 止血（2026-10-30）

- **改动**：新增 `backend/core/error_types.py`（422 行：`ErrorClass`/`Confidence`/`SourceChannel`/`ErrorScope`/`UsageStatus` + `ErrorVerdict` + `LLMResult` + 显式 JSON DTO，最底层叶子模块，禁止反向 import）；`ErrorClass` 定义收敛到此、`error_taxonomy` 转为 re-export；`ClassVerdict` 扩为 `(cls, confidence, action)`，`action` 由 `resolve_action()` 推导；`chat`/`chat_with_retry`/`chat_auto` 返回 `LLMResult`，`StreamChunk`/`StreamResult` 携带 `verdict`；四处重复的重试决断收敛为 `_decide_retry()`；`pipeline` 致命块登记改为结构化优先
- **原因（C-01，真实计费损失）**：`classify_message` 原先用 `\b\d{3,4}\b` 扫任意文本，本机实测三条正文本全部被判成永久错误 → `action=stop` → `llm_client` 首次即停 → `pipeline` 登记 `_fatal_blocks` → **该块永久失败、补跑也不重试**；命中 moderation 的情形还被 `skip_moderation_blocked` 静默跳过。改造后三条全部落到 `unknown/none/limited_retry`
- **分级规则**：只有可信通道（SDK 异常属性 / 错误 envelope / 真实 HTTP 状态）有资格产出 `stop`；文本通道（命名码与错误措辞）上限是 `limited_retry`/`backoff`/`retry_after`；无信号 → `UNKNOWN` + 有限重试。真实 `status_code=401`/`402`/`400` 仍 structured 且 `attempts==1`，识别能力未被削弱
- **判定链**：异常/流事件 → 单次结果 → 客户端重试结果（`_decide_retry` 读 `verdict`）→ analyzer（`retry_info["verdict"]` dict 形态）→ pipeline（结构化优先、文本兜底）→ 外层补跑与熔断。全链只消费同一份判定，不对显示字符串重新猜
- **usage 语义（T04）**：`LLMResult.tokens` 为 `Optional`，无 usage 时 `None` + `usage_status=UNKNOWN`；`(0,0)` 记账口径保留但由 `usage_status` 区分"未知"与"真实为零"，`block_done` 与 chapter `token_stats` 两个载荷都带该字段。停止早退（未发请求）显式标 `KNOWN`
- **并发隔离（T03）**：判定随结果对象传递，无共享状态；6 路并发用例按消息 tag 分派六种输入并断言 `peak>=2`，用"结构化 402 与文本 BILLING 同为 `billing` 但 `(confidence, attempts)` 不同"锁死不串味
- **测试**：新增 `test_error_types.py`(29) / `test_retry_decision_u2.py`(20) / `test_verdict_pipeline_u4b.py`(4) / `test_concurrency_and_usage_u6.py`(8)；`test_error_taxonomy.py` 37→45（4 条旧契约断言改名改写，均因断言的是"文本数字判 stop"这类误判行为）；全量 **892 → 962**
- **刻意未做**：不加 `__iter__`/可解包兼容层（旧元组一次性删除）；U2 引入的 `contextvars` 临时槽在 U5 删除，结构化结论改由结果对象显式携带；`analyzer.analyze_chapter` 对外 `(result, tokens, retry_info)` 形状不变；`_emit_rolling_tokens` 未知时直接不发（不宣称零）
- **plan**：`docs/PLAN-v2-long-novel-reliability-2026-10-30.md` §4 S1；任务拆解 `docs/PLAN-S1-task-breakdown-2026-10-30.md`；类型契约 `docs/DESIGN-D01-verdict-transport-2026-10-30.md`

### 10.37 PLAN v2 S2：vnext 数据空间与格式门禁（2026-10-30）

- **改动**：新增包 `backend/vnext/`——`constants.py`（`FORMAT_ID="novel-analyzer-vnext"`、`SCHEMA_VERSION=1`、独立文件名常量、`ErrorCode` 9 值 + `VnextFormatError`）、`artifacts.py`（格式门禁 + 原子写）、`fingerprint.py`（文本规范化与六个指纹）、`project.py`（建项目/读清单/带 revision 的提交）、`blocks.py`（块规划与失效判定）；`.gitignore` 放行四个 vnext 文件名；`pytest_isolation.py` 增加 vnext 隔离段
- **数据空间**：`workspace_vnext/`（数据）+ `queue_state_vnext.json` / `books_vnext.json` / `config_vnext.json`（运行时状态，放项目根）。**与旧 `workspace/` 完全隔离**：不打开、不覆盖、不删除、不归档、不写迁移脚本
- **格式门禁**：每个产物顶层必须有 `format_id` + `schema_version`，缺失即 `UNSUPPORTED_ARTIFACT_FORMAT`（界面文案"此目录不属于本版本，请从原文创建新项目"）。校验顺序固定为「顶层对象 → format_id → schema_version → 必需字段 → 取值域」，**先判格式族再判版本**：旧文件同时缺这两个字段时，用户该看到的是"不属于本版本"，而不是指向错误方向的"版本号不对"
- **身份不随内容漂移**：`chapter_id = ch_ + sha256(book_id \x1f ordinal)[:12]`，**不含正文哈希**——否则改一个字就变成"换了一章"，`STALE_INPUT` 无法沿依赖传播。正文改写只改 `content_hash`，`source_snapshot_id` 随之更新
- **指纹**（全部 `canonical_json` = 键排序 + 无空白 + `ensure_ascii=False`，多段拼接用 `\x1f` 而非 `|`，避免 `a|b` 碰撞）：`block_signature`（策略版本 + 有序 `[[chapter_id, content_hash],…]`）、`dependency_fingerprint`、`task_key`（**不含 run_id**）、`result_id`、`evidence_id`（`book_id+chapter_id+content_hash+start+end`，12 hex）；`content_hash` 输入必须是规范化文本
- **文本规范化**（`TEXT_NORMALIZATION_VERSION=1`，幂等）：去 BOM → 换行统一 LF → 删行尾空格/制表（**保留行首缩进**）→ 连续空行压至多 1 → 文末恰好 1 LF。**不做**全半角/简繁转换——会改变字符数，让已存的 `source_ref` 偏移整体失效
- **写入规则**：`write_json_atomic` 用同目录 `mkstemp` + 唯一名 + `fsync` + `os.replace`，Windows `PermissionError` 固定 6 次退避重试；失败保留原文件并清理临时文件。**刻意不复用** `json_utils.safe_save_json`——它带 `json_repair` 兜底与"失败返回 default"，会把"没写进去"伪装成"写成功"
- **并发**：`commit_manifest(book_dir, mutate, expected_revision=None)` 锁粒度是**书**（按绝对路径的 `threading.Lock`，跨书不共享），锁内读→改→`revision+1`→原子写；版本不符抛 `RevisionConflict`，**不重试不合并**
- **块失效判定**：`verify_block` 按 manifest 重算签名，成员变/正文变/策略版本变三类都命中 `BLOCK_SIGNATURE_MISMATCH`；`is_block_fresh` 为布尔版供扫描过滤。块文件只存 `chapter_ids`，不重复存 `content_hash`（避免"两处哈希不一致"）
- **测试**：`test_vnext_constants_alignment.py`(4) / `test_vnext_artifacts.py`(15) / `test_vnext_fingerprint.py`(15) / `test_vnext_project.py`(15) / `test_vnext_blocks.py`(11)，共 60 例；全量 **962 → 1022**（父 agent 亲跑）
- **刻意未做**：不做旧字段兼容降级（缺字段即报错，不补默认值）；不为 `.md` 派生产物加 JSON 头（改旁挂 `.meta.json`）；S2 不逐域重写消费者，只把入口接上门禁（18 行入口矩阵见契约文档附录 B）
- **接线（S2 第二批）**：新增 `backend/vnext/registry.py`（`scan_projects` 只认合法 `manifest.json`，无清单即 `LEGACY_DIR` 记进拒绝清单，**绝不猜 `blocks/` 形状**；`locate_project` 按 `book_id` 定位，不按书名模糊匹配；`read_index`/`write_index` 路径存相对数据根）、`backend/vnext/results.py`（`results/<sig>.json`，状态 `done/failed/skipped/cancelled`，`ResultRecord` 未知用量保持 `None`；`classify` 先验签名后验状态，返回 `done/stale/missing`，`STALE` 三种原因 `STALE_BLOCK_CHANGED`/`STALE_NOT_DONE`/`STALE_MISSING_RESULT`）、`backend/vnext/queue.py`（`queue_state_vnext.json`；文件不存在=空队列，格式不对=报错，**不做"读不出来就当空"**；`normalize_after_restart` 只把 `running` 回落 `pending` 并清 `error_message`，`done/failed/skipped` 的历史结论不改写；`apply_progress` 判完成要求 `completed==total` 且 `stale==0`）
- **服务门面与路由（S2 接线入口）**：`backend/services/vnext_service.py`（`list_books` 同时返回 accepted 与带原因的 rejected；`open_book` 按 `book_id`；`block_size` 只在配置文件**不存在**时兜底 4，存在但非法必须报错）、`backend/api/routes_vnext.py`（`GET /api/vnext/books`、`GET /api/vnext/books/{book_id}`、`GET /api/vnext/queue`、`POST /api/vnext/queue/restore`；错误码→HTTP 映射：格式/版本/冲突→409、缺字段→404、取值非法→422，**不把格式错误归到 500**），已在 `backend/app.py` 注册
- **plan / 契约**：`docs/PLAN-v2-long-novel-reliability-2026-10-30.md` §1.1、§2、§4 S2；数据契约 `docs/data-contract-vnext.md`（含旧产物清单附录 A、入口矩阵附录 B、实现决定附录 C）

### 10.38 外部复审六条修复：S1/S2 不能算验收完成（2026-10-30）

复审独立复现了六处问题。**共同点：每条都能跑通、测试也绿，但给出的结论是错的**——这类问题的代价不是崩溃，而是"看起来正常"的错误数据。

**S1 侧（用量与判定）**

1. **无 usage 的失败继承前次 tokens**：旧实现把"上一次尝试报过的用量"存进 `last_consumed_tokens`，第二次尝试断连（无 usage）时仍返回 `(11,3)/KNOWN`。现在三种口径分开：`attempt_tokens`（本次尝试自己的用量，拿不到就是 `None`）、`prior_known_tokens`（已确认消耗但产出未被采用的累计，必须记账但不能算进 `tokens`）、`last_consumed_tokens`（仅内部统计）。新增字段 `LLMResult.prior_known_tokens`，DTO 同步。
2. **完整判定被压成三元组再重建**：`_decide_retry` 原先把 `ErrorVerdict` 换成 `ClassVerdict` 再往下传，`provider_code`/`http_status`/`source_channel`/`retry_after` 全丢，最终结果是"按置信度重建"的仿制品（来源从 `ERROR_ENVELOPE` 退化成 `SDK_ATTRIBUTE`）。现在新增 `_verdict_from_exception(exc, error_text)`：**在异常现场**一次性构造完整判定（含厂商码、HTTP 状态、来源通道、响应头 `Retry-After`），全链只搬不改；`_as_error_verdict` 对已是 `ErrorVerdict` 的输入原样返回。
3. **Retry-After 依赖字符串**：typed `retry_after=7` 但显示串不含 `Retry-After:` 时会退化成 30 秒。现在优先用 typed 值，字符串解析只作兜底。

**S2 侧（结果协议与恢复）**

4. **结果读取协议与索引没接通**：两个**反向**错误——合法登记且 `SUCCEEDED` 的结果被跳过（进度显示 missing）、未登记且指向不存在文件的 `done` 记录却被算完成。现已按契约对齐：文件名 `<result_id>.json`、状态 `SUCCEEDED/FAILED/SKIPPED/CANCELLED/BLOCKED`、**只有 `manifest.active_results` 里的结果才算数**，判定顺序为「登记 → 状态 → `source_chapters` 是否仍一致 → （可选）依赖指纹」。`publish_result` 返回**提交后的清单**（`commit_manifest` 在锁内重读，调用方手里的旧快照会看到空 `active_results`）。
5. **队列恢复不重算进度**：只做 `running→pending`，源变了仍显示 `done 2/2, stale=0`。现在 `restore_queue_after_restart()` 对每个可打开的书按当前源重算；算不出的书保留历史状态但写明"项目不可用，进度未校验"。
6. **书目定位被坏目录堵死 + 身份冲突任取其一**：`locate_project` 原来自行遍历并加载所有 manifest，一个排序靠前的坏清单会让好书也打不开；两个相同 `book_id` 都进 accepted，打开时任取第一个。现在定位复用 `scan_projects` 的已校验结果，冲突目录进 `rejected`（`DUPLICATE_BOOK_ID`，保留第一个并给出冲突路径）。

**另修**：`block_size` 配置校验——`true` 曾被 `int()` 顺成 1、`2.9` 被截成 2、`analysis: [1]` 抛未封装的 `AttributeError`（接口直接 500）。现在先校验容器类型，再要求严格整数（排除 `bool`），非法一律 `INVALID_FIELD_VALUE`。

**回归**：`backend/tests/test_vnext_review_fixes.py`（9 例）、`backend/tests/test_llm_verdict_tokens_review.py`（5 例），以及 `test_vnext_results_queue.py` 按新协议重写（18 例）。

### 10.39 块间依赖链与 S2 基础入口（2026-10-30）

**依赖链：局部失效的准确边界。** 只比块签名会有一种死法——某块读的是前情摘要，"签名没变"但它依赖的前情换过了，于是被判"还能用"。现在每块的期望依赖由 **有序的前序结果 ID + 生成条件** 算出（`results.expected_dependency`），`classify(..., strategy=...)` 按计划顺序前向滚链：

- 前面某块换了结果（新 `result_id`）→ 它之后各块 `STALE_DEPENDENCY`，它之前的不受影响；
- Prompt 策略 / 模型指纹 / 上下文策略变更 → 全部失效；
- 前面某块未完成 → 其后各块依赖不成立（`prior.append(None)`）。

`ResultStrategy(prompt_strategy_version, model_fingerprint, context_strategy_version)` 三件分开演进，"改措辞"和"改给模型看哪几章前情"不再互相牵连。不传 `strategy` 时退化为主看块签名，**仅供扫描预览**，不能用来决定"跳过"。

**S2 基础入口（原计划排在 S3–S9，复审后归回 S2）**：`vnext_service.create_book()`（`text` 走 `splitter_service.split_text`，不另写切章正则）、`vnext_service.list_blocks()`（块级 `done` / `stale:<原因>` / `missing:<原因>`）、`POST /api/vnext/projects`（201；`chapters` 优先于 `text`）、`GET /api/vnext/books/{id}/blocks`。

**两个坑**：① `project.create_project()` 返回**项目目录 Path**，不是 `Manifest`——想拿 `book_id` 必须 `load_manifest(book_dir)` 回读，直接当对象用会 `AttributeError`；② 没有章节标题的文本，切章器会整篇成 1 章而不是 0 章，所以"零章项目"这条错误路径实际不会触发（`INVALID_FIELD_VALUE` 只在 `chapters=[]` 时出现）。

### 10.40 PLAN v2 S3 U1/U2：安全快照（2026-10-30）

- **改动**：新增 `backend/vnext/knowledge.py`——`SourceRef`（chapter_id + ordinal + 码点区间 + content_hash）、`FactRecord`（`verification_state` 只有 `UNVERIFIED`/`EVIDENCE_LOCATED` 两档，没有"高置信"第三档：置信是模型的自我评价，不是事实属性）、`CharacterStateRecord`、`KnowledgeSnapshot`、`build_snapshot()`、`snapshot_for_block()`
- **上界是排他的**：`exclusive_before=5` ⇒ 第 1–4 章可见。写成包含上界会让每块的边界都差一，调用处极难查。来源范围 `end >= N` 的记录**整条**排除，不做"部分可见"
- **未知范围一律排除**：没有可定位来源的记录不进快照，并记进 `dropped_unknown_range`（要能说清排除了什么，不能静默丢）。宁可少给一条事实，也不能塞进一条未来事实
- **快照是不可变值**：`frozen` dataclass + 元组，构造完与外部数据无引用关系。仅 deepcopy 内容、另外再读一遍覆盖元数据，会读到"另一个时刻"的范围——这是 S3-3 明确点名的坑
- **指纹只含可见内容**：`dependency_fingerprint()` 不含 `exclusive_before` 本身，所以"同样可见内容、不同上界"得到同一依赖——块前进了不该让依赖变。它正是 `results.expected_dependency()` 里"上下文"那一项的输入
- **人物覆盖顺序**：同人取来源位置最新；迟到低章号不覆盖（复审第 4 条的靶子）；同位置冲突按"确定优先"，结论与遍历顺序无关；新人物照样能加入（限制的是覆盖顺序，不是加入资格）
- **测试**：`backend/tests/test_vnext_knowledge_snapshot.py` 19 例
- **plan**：`docs/PLAN-v2-long-novel-reliability-2026-10-30.md` §4 S3-1/3/4/6；拆解 `docs/PLAN-S3-task-breakdown-2026-10-30.md`（U1/U2 完成，U3–U8 待做）

### 10.41 PLAN v2 S3 U5：引用校验分级与 Prompt 引用规则（2026-10-30）

- **改动**：新增 `backend/vnext/citations.py`——`CITATION_INSTRUCTION`（写进 Prompt 的引用规则）、`verify_citation()`、`verify_fact()`、`summarize_checks()`
- **Prompt 侧**：规则要求逐字照抄、不改标点、不加省略号、偏移用**码点**左闭右开、找不到可照抄的连续片段就填 `null`。让模型给可逐字比对的引文，比事后用模糊匹配猜它想说什么有效得多
- **比对分五级**，只把「是否定位」一位给下游：`EXACT`（逐字）→ `NORMALIZED`（去标点/空白/全半角后相同，模型改标点是常态）→ `RELOCATED`（偏移算错但整章搜得到，并给出修正位置）→ `SEGMENTED`（用"……"把两段原文接起来，各段按顺序都在）→ `FUZZY`（≥10 字且最长公共块覆盖率 ≥0.90，容忍个别错字）
- **为什么必须分级**：全判过等于放弃校验（编造引用会污染结论）；全判不过会把真引用大量误杀（模型几乎从不逐字照抄）。分级让"过错的类型"可统计
- **失败不丢数据**：`verify_fact()` 失败时原记录原样返回、状态仍是 `UNVERIFIED`；成功只把状态升为 `EVIDENCE_LOCATED`，**不偷偷修正偏移、不改写结论文字**——偏移由模型给，程序只判真假
- **编造率统计**：`summarize_checks()` 输出 `located_ratio` 与 `by_kind` 分布。这个数字是"能不能换小模型"的判据
- **测试**：`backend/tests/test_vnext_citations.py` 19 例（含"半角标点仍算通过"、"偏移错但引文真实"、"省略号顺序颠倒判否"、"编造长句判否"）
- **plan**：§4 S3-6；拆解 U5（校验器完成，接线到结果写入与 `mismatch_reason` 落盘待做）

### 10.42 PLAN v2 S3 收口：U3–U8（2026-10-30）

S3 剩余六个单元一次做完。核心判据只有一句：**同一份输入，无论分批到达还是一次给全、无论块按什么顺序跑完，最终知识库与快照指纹必须完全相同**。

**U3/U4 归约与人物时序（`knowledge.py` 扩展）**
- `reduce_knowledge(facts, character_records)`：事实按 `fact_id` 去重（来源更晚的胜；来源相同时**已定位原文的胜**）、按来源位置排序（**不按到达顺序**）；人物记录保留**全部版本**（快照要按时间点取"最新可见"，历史不能丢），去重键 `(name, chapter, start)`，同位置"确定优先"
- `KnowledgeStore`：不缓存派生状态（最新状态、可见集合），每次查询都走 `reduce_knowledge` —— 无状态偏移、天然可重放，`add_block` 重复并入是幂等的
- **不变量用例**：`test_incremental_equals_bulk`、`test_block_order_does_not_matter`、`test_snapshot_after_incremental_matches_direct_snapshot`。这三条挡住的是"续跑与重跑各得一套自称正确的结果"

**U8 Prompt 限额（`prompt_limits.py`）**
- `PromptLimits` 不可变 9 项（与旧 `prompt_builder.py` 参数名一致，便于对照迁移），`from_dict` 严格校验：**拒 `bool`**（`True` 会被 `int()` 悄悄变成 1）、**拒 `float`**（`2.0` 说明配置写错）、拒字符串数字、拒 `0`/负数；非法一律 `INVALID_FIELD_VALUE`，`detail` 必须带字段名与收到的值
- 未知键忽略（配置可能来自更新版本），但顶层非 Mapping 直接拒收
- `VnextFormatError.path` 约定填 `prompt_limits.<字段>`
- 本期**不新增任何用户可见设置项**：这是内部设置，只有构建入口与测试能用

**U7 存档与窗口分离（`prompt_view.py`）**
- **库永不裁剪**：`KnowledgeStore` 里有多少就是多少，限额不影响落盘
- **窗口在读取时裁剪**：`build_prompt_window(store, exclusive_before=…, limits=…)`，同一份库可被不同限额读成不同窗口，库本身不动
- 截断方向：事实取**来源位置最靠后**的 N 条（离当前块最近才相关），渲染仍按时间正序（给模型读的顺序是故事顺序）；人物按名字排序取前 N（没有"最近活跃"信息时，确定性优于到达顺序）
- **截断必须被报告**：`PromptWindow.truncated`/`dropped` 只在真截断时出现。模型不知道"上面还有 800 条被截掉了"，就会把"没提到"当成"不存在"，于是重复发现、重复推理——这是长篇小说分析最典型的退化

**U6 图谱边分型（`graph.py`）**
- `CooccurrenceEdge`（同章邻近，带两者首次出现的码点位置）与 `SemanticRelationship`（有互动证据的关系）**分表，永不合表**
- `semantic_relationships` 只接受 `interaction is True`（严格同一性：`1`/`"true"` 不算）且四个来源字段齐备的记录；`a == b`、`relation` 为空、区间倒置/为负一律**静默丢弃**（输入是模型产出，容错优于中断）
- 名字对 `normalize_pair` 归一（`甲-乙` 与 `乙-甲` 同一对），`a == b` 抛 `ValueError`
- `merge_edges` 按 `(a, b, chapter_id)` 去重且幂等，结果与输入顺序无关

**U5 收口（`citations.py` 扩展）**
- `verify_facts_against_book(facts, chapter_texts)` 为批量入口：章文本由调用方一次性读入，**校验本身零 IO**（否则批量校验会变成 N 次文件读取，边界也难在测试里构造）
- 缺章文本按 `CHAPTER_MISSING` 处理，不猜

**测试**：`test_vnext_knowledge_reduce.py`（14）、`test_vnext_prompt_limits.py`（31，子智能体产出，父级独立复跑确认）、`test_vnext_prompt_view.py`（9）、`test_vnext_graph.py`（49，子智能体产出）、`test_vnext_citations.py` 增至 21。S3 全部单元用例 **143 例**（含 U1/U2 的 snapshot 19）
- **派工记录**：U8（Prompt 限额）交 MiniMax、U6（图谱分型）交 deepseek，都给到"只建两个文件 + 精确 API 签名 + 完成标准 + 只回报四行"的规格；两者一次成功。对照之前"读遍全库 + 写长文档"两次零产出——**派工要窄、要可验证、要有明确停止条件**

**plan**：`docs/PLAN-v2-long-novel-reliability-2026-10-30.md` §4 S3；拆解 `docs/PLAN-S3-task-breakdown-2026-10-30.md`（U1–U8 全部完成）

---

## 13. i18n 接口预留（L2 阶段，2026-09-02）

当前状态：**只搭骨架，未翻译**。vue-i18n 已安装，locale 文件为空，
不抽任何现有字符串。所有 UI 仍硬编码中文。

### 接口已就位
- 后端 `GUIConfig.language: str = "zh-CN"`（写进 config.json）
- 前端 `vue-i18n@^10` 已 init（`useLocale()` composable 可用）
- 前端 `AppConfigDto.gui.language?: string`

### 未来启用 i18n 的步骤
1. 创建新页面/新功能时，新字符串**必须**用 `t('namespace.element')` 包裹
2. 翻译抽取：按 `frontend/src/pages/*.vue` + `components/*.vue` 顺序批量替换
3. 填 `frontend/src/locales/en.json`（zh-CN.json 由原硬编码生成）
4. 启用 `useLocale().setLocale()` 的后端持久化（PUT /api/settings）
   - **Open Question**：当前 `routes_settings.py:97` 的 `is_running` 护栏只挡 analysis，
     未来启用 setLocale 持久化时需扩到 summary/style/location_normalization 全套服务
5. 跑伪本地化测试（pseudo locale）验证布局

### Open Question（来自 verifier 审计，2026-09-02）
- `useLocale()` 的 type cast `(supportedLocales as readonly string[]).includes(l) ? l as SupportedLocale : 'zh-CN'`
  对 `'zh_CN'`（下划线）等 typo 不会拒，未来加 setLocale 的来源校验
- vue-i18n dev 模式可能在 console.warn "no messages"（messages 为空），**影响为零**但需知
- `useLocale()` 必须在 setup 上下文调（vue-i18n 的 inject 限制），不能在 main.ts / 事件回调里直接用

### 当前已规划的 i18n 翻译键命名约定
- 页面级：`queue.*`, `settings.*`, `summary.*`, `splitter.*`, `viz.*`, ...
- 组件级：`component.button.start`, `component.modal.confirm`
- 公共：`common.*`（如 common.loading, common.cancel, common.confirm）

---

## 14. 小说书名推断合并（2026-09-04）

### 问题
splitter 内部有两条平行的"书名推断"路径：
- `infer_book_name(path)` 从**文件名**（"《书名》（精校版）.txt"）→ 权威 ✅
- `_extract_metadata` 从**正文前 8 行** → 错值 ❌

原本 splitter 写 `blocks/metadata.json.title` 用第二条路径，**完全不知道**第一条路径已经推断过。
《轮回乐园》txt 第一段没有 `《...》` 包裹，前 8 行策略误把"夜晚时分..."当书名。

### 修法 A（splitter 治本）
`save_to_workspace` 把 `book_name` 沿 `save_split → split_text → _extract_metadata` 透传为 `external_title`。
`_extract_metadata` 接受 `external_title`，**传入时直接返回 `{"title": external_title}` 跳过正文推断**。
未传入时（直接调 `split_text` 的场景，如 `split_text` 单独测试）才走正文推断。

### 兜底（final_summary 拒垃圾）
`detect_book_name._looks_valid` 加标点过滤：title 含 `。` / `？` / `！` → 视为正文句子，拒之。
对所有历史污染的 metadata.json 都生效：兜底 fallback 到 `output_dir.parent.name`。

### 关键设计
- 两条书名路径**只合并一条**（外部权威），正文推断仅作 fallback
- 不重切已分析的书（重切会清 `chapter_N_result.json`）
- 不改 `infer_book_name` 算法（已经过 P5/P5b/P5c 修复）

### 10.43 PLAN v2 S4–S6：有序覆盖、稳定实体、全自动裁决（2026-10-30）

三批的共同判据：**结论必须能被程序复算**。凡是要靠排序序号、完成顺序或模型自评置信度才能得出的结论，一律不算。

**S4 覆盖视图（`backend/vnext/rolling.py`，19 例）**
- `commit_cursor` 是"从第一块起连续完成"的前缀长度，**不是"完成了几个"**：1、3 完成而 2 缺失时它是 1，`prefix_complete=False`——这正是"100/102 完成被说成基本完成"的防线
- `consumable_upto` **对齐批次**：半个批次的信息不许进摘要；`claimed_range()` 用实际完成位置（诚实），两者分开是刻意的
- `TERMINAL_SKIPPED` 可越过但留 `gaps` 并整体 `PARTIAL`；`PENDING_RETRY` **暂停推进**（它不是洞，是还没好）
- `dirty_from` 使 `consumable_upto` 收缩到脏批次起点，并用 `batch_aligned_upto` 判断是否需要重建——否则"脏点还没被消费到"会被误判成必须重建
- 用例覆盖：10 种完成顺序收敛到同一视图、签名不符的结果被忽略、`batch_size` 校验

**S5 稳定实体（`backend/vnext/foreshadow.py`，29 例，子智能体产出，父级独立复跑确认）**
- `occurrence_id` 用**完整 64 hex**（含 kind），12 位只作展示——缩写做键会撞
- `entity_id = fs_ + 16 hex`，只依赖 `(book_id, seed_occurrence_id)`，seed 取**来源最早**的出现 ⇒ **补入更早证据不改号**（复审第 2 条 R2 的靶子）
- `cutoff_view` 顺序固定：筛可见来源 → **整条排除**（实体只要有一条越过截止点就整个不出现，不能"凭首次出现靠前就放进来"）→ 算属性 → `min_mentions` → 排序 → 截断
- **本层不做相似度合并**：两个悬念是否同一实体由调用方给的 `entity_id` 决定，"谁藏了王冠"与"王冠在哪里"不会被文本相似度误合并

**S6 裁决（`backend/vnext/resolution.py` 34 例 + `context.py` 12 例）**
- 模型只许给四枚举；**小写/近义词/缺失一律 `INVALID`**——宽容解析会让下游以为这条已核对
- `SUPPORTED` 六道门禁：答案非空 + 无 `material_insufficient_reason` + 反证 `performed is True` + 无 `counter_quotes` + 引文落在给定范围 + ID 属于本任务
- 归约：任一 SUPPORTED → `AUTO_RESOLVED`；仅有不足/冲突 → `PROPOSED_RESOLVED`；只有垃圾 → `OPEN`；**垃圾候选不降级已支持结论**
- 覆盖守恒式：`candidates_total = supported + insufficient + conflict + invalid + stale + not_checked`（`conflict` = DIFFERENT_ANSWER + CONTRADICTED）；`invalid` 必须等于按 reason 分组之和
- 任务状态有序互斥 `NOT_RUN → RUNNING → STOPPED → PARTIAL → COMPLETED`；**空候选跑过 = COMPLETED 且计数 0**、**缺覆盖汇总 = PARTIAL**（"没运行"不能显示成"已核对、无发现"）
- 上下文取材：先扩句读边界再补目标，受 `HARD_MAX_CHARS` 约束；**到章节尽头不是截断、撞硬上限才是**，且硬上限下必须保住引用完整（引用被切掉就没法核对）

**与 S7 的边界**：S4–S6 建的是**判定与状态层**——给定输入给出唯一可复算结论。接进真实流水线（真跑 LLM、真写摘要、真出报告）是 S7；本批**不改** 1.x 的 `pipeline.py` / `final_summary.py`。

**plan**：`docs/PLAN-v2-long-novel-reliability-2026-10-30.md` §4 S4/S5/S6；拆解 `docs/PLAN-S4-S6-task-breakdown-2026-10-30.md`

### 10.44 复审二：S4–S6 判定层的七个错误结论（2026-10-30）

复审独立复现七条，**每条都能通过原有用例**——因为原用例把错误行为当成了正确行为。这是本轮最值得记住的一点：绿灯不代表语义对。

1. **没有证据也能自动回收**：`parse_recheck` 接受 `evidence_id`/`span`/`quote` 全缺的 `SUPPORTED`，随后归约成 `AUTO_RESOLVED`；即使传了恒返回 False 的校验器，空引文也能绕过。现在四件套缺一不可，且**必须有校验器**（`NO_QUOTE_VERIFIER`）。
2. **`INVALID` / `INSUFFICIENT` 混用**：没查反证本是"材料不足"，却判 `INVALID`，实体退回 `OPEN`——把"没查"读成"没问题"。现在响应不可用 → `INVALID` 且 `retryable=True`（计入未核对、可重跑）；证据不足 → `INSUFFICIENT`（计入已核对）。
3. **覆盖计数按行数而非按候选集合**：同一候选答两次时 `supported=2, not_checked=0, COMPLETED`。现在按 `expected_candidate_ids` 逐项归类，重复/外来/缺失各自可数，出现重复或外来行时**不得标 `COMPLETED`**。
4. **整条排除破坏前缀不变性**：实体第 3 章出现、第 12 章也出现时，`cutoff=11` 视图里它消失；"第 12 章还没写"时的同一视图里它在。判据：**过去没变，过去能查到的视图就不该变**。改为"截止点后的记录不进视图，属性只按可见记录重算"。
5. **同锚点不同悬念撞 ID**：`entity_id` 只用 `(book_id, seed_occurrence_id)`，两处不同悬念若共用同一处原文作锚点就会算出同一个 ID。加 `motif_key`（调用方给的稳定母题键）参与哈希。
6. **块签名不符仍继承完成状态**：原实现把 slot 的旧签名"纠正"成新的却保留 `DONE` 与 `result_id`，等于把旧块结果挂在新块下面还宣称前缀完整。现在降为 `PENDING` + `SIGNATURE_CHANGED`。
7. **窗口边界落在句中却标完整**：补足目标后未再对齐句界。现在末尾一定重新对齐；预算装不下时如实标 `HARD_BUDGET`/`SENTENCE_SPLIT`，且**硬上限永远优先**、引用必须完整覆盖。

**回归**：`backend/tests/test_vnext_review_round2.py`（23 例）+ 四个旧文件按新语义重写（`resolution` 34、`foreshadow` 30、`context` 12、`rolling` 19）。

### 10.45 S7–S10：任务缓存、预算、熔断与端到端验收（2026-10-30）

**S7 完成**（`tasks.py` / `budget.py` / `recheck_plan.py`，35 例）。
- `TaskLedger` 让"跑没跑过"与"业务状态"彻底分开：`NO_CANDIDATE` 是**结论**（下次直接复用），`BUDGET_SKIPPED`/`FAILED`/`CANCELLED` 是**未完成**（不写成功标记，拿到预算可续）。旧实现拿"已回收"当复检缓存，于是调和、人工、休眠判定都能间接改写"有没有核过"。
- 任务键含**全部**输入与策略版本：只哈希"某一处引用"证明不了整份材料相同。
- `Budget` 在**派发前**预留（十路并发"先查再调"会同时通过，事后记账拦不住）；失败按实际扣（失败也花钱）；未开始可退回；`rolling_rebuild`/`semantic_review` 默认 **0 次**——用 0 表达"本版不做"，真被调用会当场抛错，而不是悄悄多花钱。
- `RecheckSummary` 把新调用/缓存复用/无候选/失败/取消/预算跳过分开报；`not_completed_reason()` 非空即未完成。
- 未做：`STYLE`/`FINAL_REPORT` 的完整缓存（S7-8 明确列为条件优化，需先测重复费用）。

**S8 部分**（`breaker.py`，10 例）。
- 只认**结构化判定**且 `scope=ACCOUNT_GLOBAL` 才跳闸；文本通道永不跳闸（C-01 的教训）；跳闸粘性，恢复要显式 `reset()`。
- 写入一致性复用既有 `write_json_atomic` + `publish_result` 先落盘后登记；**未做**崩溃点注入测试。
- 交互保护（持锁不 await、取消不终止工作线程、预留等线程结束才释放）目前只有**文字规则**，没有代码守卫。

**S9 观测就位，付费实验未执行**：用量/预算快照与任务计数已可用；A/B/C Prompt 实验与标注集评测需要独立预算与授权（PLAN §4 S9-7），本批不做——且 §4 S9-7 明确"基础修复不依赖实验结论才能交付"。

**S10 验收自动化，打包未执行**：`scripts/acceptance_vnext.py` 离线跑通全链 **27/27**；PyInstaller 当前未安装、`build_exe.bat` 跑不通，Windows 安装包与前端接入均未做。


### 10.46 复审三：S7 七个缺口与生产入口闭环（2026-10-30）

复审指出"27/27 只证明零件能拼，不证明链路成立"。七条全部复现并修复，其中最要紧的是第 1 条：

1. **S7 没有生产入口** → 新增 `backend/vnext/runner.py`：`run_recheck()` 串起规划→预算→裁决→账本→落盘，`call_model` 与 `verify` 由调用方注入（离线可跑）。验收脚本新增 5 条：首次执行调用一次、真实引文通过、账本落盘、**重启后复用且不再调用**、重启后费用可重建。验收 27 → **32** 条。
2. **规划器释放了预留** → 预留改为**持有**到执行结束。原实现"预留后立刻释放"，于是"只允许 1 次调用"的预算被拿去发 2 次请求（实测）。`RecheckPlan.reservations` 交给执行器结算。
3. **费用重建字段错位** → `TaskRecord` 导出 `group`；`rebuild_usage()` 才读得到。原先真实账本 1 次调用 10 token 重建出 `{}`，测试却另造含 `group` 的字典绕过了它。
4. **记账与去重混在一起** → 重复应用仍记费用、只置 `applied=False`。
5. **预留无身份** → `reservation_id` + `ledger_id`；`settle` 幂等、`release` 不误伤同额预留。
6. **反证 ID 未校验** → `checked_evidence_ids` 必须非空且属于本任务，否则 `INSUFFICIENT`。
7. **汇总混淆历史与当前** → 当前状态按每个 `task_key` 的**最新**记录，费用按**全部历史**累计；空账本 + 有候选 ⇒ `not_started`，不得报"无未完成"。

### 10.47 S8 写入一致性与交互保护 + S9 真实测量 + S10 打包（2026-10-30）

**S8 完成**
- `consistency.publish_atomically`：把"写文件"与"登记进 manifest"包成一个可注入故障的操作，四个故障点 `before_write`/`after_write`/`before_commit`/`after_commit` 各自可复现。文件写了但登记失败 → **回滚并如实报告**（`cleanup_done`）；删不掉时记 `cleanup_done=False`，孤儿由 `orphan_files()` 扫出——孤儿比失败更麻烦，它看起来像成功。
- `concurrency.run_thread_job`：**记账与状态转换只由"线程真正结束"触发**。取消等待方不打断登记（取消只决定"还要不要等"）。这是 S8 里最反直觉的一条：`asyncio.to_thread` 取消 await 不会取消线程，若在 `finally` 里结算，实际仍在跑的请求就成了没记账的消耗。
- 12 例测试（8 一致性 + 4 并发）。

**S9 测量能力就位，结论性实验未做**
- `scripts/measure_prompt_cost.py` 用真实模型发少量调用、读 `usage`（不猜 token）。实测 4 次：1 章样本 405 vs 379 prompt tokens，6 章 1065 vs 1039——去重块在这两档里是**固定 26 token**，因为合成样本的主线摘要只有 38 字符，而真实数据是 7,323 字符。
- 因此**不能用这组数字决定去不去重**。真正有效的结构性事实是满配提示词的分布：正文 65% / 历史块 16% / system 10.5% / 其余 7 段 8%。要下结论需 50+ 章真实样本、每组 3 次重复。
- 记录见 `docs/MEASURE-S9-2026-10-30.md`。

**S10**
- 端到端验收 `scripts/acceptance_vnext.py` **32/32**。
- PyInstaller 6.19.0 已安装并尝试 `desktop.spec` 构建（结果见 `docs/PLAN-S7-S10-status-2026-10-30.md`）。
- 前端 vnext 界面仍未做：目前只有 `/api/vnext/*` 只读 API。

### 10.48 S8 重做：发布与结算的四条硬规则（2026-10-31）

复审三判定 S8 不能签"完成"，四条全部成立，重做后收口。

- **提交判定改三态**：`commit()` 抛错 ≠ 没登记（可能 manifest 已写完、随后才炸）。现在由 `verify_commit()` 读持久化状态回答：True=已登记（保留文件）/ False=明确没登记（可回滚）/ **None=不明（保留文件）**。`_commit_state()` 的 docstring 写明理由：孤儿能扫出来回收，被引用的文件删了就没了——两类代价不对等。
- **结算搬进线程**：`asyncio.to_thread` 取消 await 不取消线程；原实现把 `on_finish` 挂在循环回调上，取消后关循环就会"提前结算"。现在 `on_finish` 在工作线程的 `finally` 里执行，循环只负责把结果交回等待方（关了就放弃，不影响账）。
- **结算失败可见**：`on_finish` 抛错不再让 Future 永久挂起——记 `settle_error` + `payload["settle_failed"]`，结果照常交回。
- **接进生产入口**：`results.publish_result` 改走 `publish_atomically`，失败抛 `PublishFailed(outcome)`；新增 `results.scan_orphans()` 作为 `orphan_files` 的生产用法。入口级故障用例 `backend/tests/test_vnext_publish_faults.py`（6 例，含"manifest 写完才抛错时文件绝不能删"这条正例）。
- **诚实边界**：用异常注入替代进程强杀，不能等同于崩溃注入；真实中断恢复仍需子进程测试。

### 10.49 A 批：建项目事务化、路径校验、请求超时（2026-10-31）

三条都是"失败会被伪装成正常"的类型，按 A/B/C 计划先做。

- **A1 建项目事务化**：原实现逐章直接写进最终目录，写 manifest 时失败会留下没有清单的目录，`registry` 判成 `LEGACY_DIR` —— 用户看到的是"我的新项目变成旧版了"，比报错更误导。现在全部写在 `data_root/.staging-<hex>/` 里、**manifest 最后写**、最后一步 `os.replace` 整套换上；异常分支 `rmtree` 暂存目录。
  - Windows 两条硬限制写进 `_publish_dir` docstring：目标必须不存在（`WinError 5` 不可自愈）、源与目标必须同卷（放 `%TEMP%` 会 `EXDEV`），故暂存目录一律建在 `data_root` 下；句柄占用（杀毒/索引器）重试 6 次。
  - 残留只能靠启动清理：暂存目录以点开头，`scan_projects` 会跳过它——**看不见等于永远不被发现**。`project.cleanup_stale_staging()` 挂在 `vnext_service.list_books()` 入口（扫描应只读，清理是入口职责）。
  - 用例只断言"异常路径无残留"，不宣称"进程被杀也无残留"。
- **A2 名字校验**：`validate_name()` 是唯一允许外部输入变成路径分量的地方。拒绝空、中文/空格、`/`、`\`、盘符、`..`、Windows 保留设备名（`CON`/`NUL`/`COM1`…）、超长；`book_id` 与 `dir_name` **都校验**（`dir_name` 缺省时 `book_id` 就是目录名）。
  - 顺带修一个静默行为：原用 `dir_name or book_id`，显式传空串会被悄悄替换成 `book_id`；改成 `is None` 判断，空串直接报错。
- **A3 请求超时**：`request()` 此前无 `AbortSignal`，后端卡住时按钮永久禁用。现在默认 30s（`AbortSignal.timeout`），重接口按调用覆盖：`vnext/projects` 300s、`splitter/preview` 120s、`prompt/preview` 120s、`aggregate/run` 300s、excel 导出 300s；超时映射 `TIMEOUT`、主动取消映射 `ABORTED`，两者文案不同（超时=稍后重试，取消=正常），并进错误码中文映射。
- **测试**：`backend/tests/test_vnext_project_atomic.py`（23 例，含 13 个非法名参数化、中途失败无残留、不被判 `LEGACY_DIR`、启动清理）；`frontend/src/api/client.test.ts` 增 4 例（超时/中断/信号存在/文案映射）。

### 10.50 B0 前置：结果 schema、库落盘、指纹闭环（2026-10-31）

复审指出的五条 P1 里有四条指向同一件事：**"续跑"与"一次跑完"会得到不同结论，而系统看不出来**。
本批先把这条链闭合，再谈分析流水线。

- **`extracted` 定死 schema**：`facts[]` + `characters[]`，两者都带 `SourceRef`。没有来源的条目在重放时**丢弃**——没有它无法判断事实属于哪一章，快照上界失效。
- **`records_from_extracted()` / `KnowledgeStore.from_dict()`**：`extracted` → 记录的还原路径；对畸形条目静默跳过、对缺来源条目丢弃。
- **库落盘** `derived/knowledge.json`（`save_store`/`load_store`）：带 `knowledge_store_version` + `meta`（`source_snapshot_id`、`ordered_result_ids`、`store_fingerprint`）。版本不符抛 `UNSUPPORTED_SCHEMA_VERSION`、坏文件抛 `UNPARSEABLE_ARTIFACT`，由调用方决定是否重放重建。
- **指纹三层**：① `result_id` 并入 `extracted_fingerprint`（否则同条件重跑原地覆盖、`ordered_result_ids` 不变、重放不触发）；② 新增 `PromptWindow.fingerprint()`，含可见内容 + `dropped` + **限额**（否则"改限额让窗口变小"判不出变化）；③ `expected_dependency(..., context_fingerprint=…)` 把②接进依赖，`classify(..., context_fingerprints=…)` 由 runner 用当前库算出后传入。
- **写入临界区**：库的读写必须与 `publish_result` 同一把书锁（`project.commit_manifest`），否则并发 publish 丢事实——而"丢事实"与"没分析过"在界面上长得一样。
- **测试**：`backend/tests/test_vnext_knowledge_persist.py`（8 例：schema 往返、缺来源丢弃、畸形跳过、落盘读回指纹一致、版本不符、坏文件、重放≡一次跑完、块序无关）。
- **未做**：>2MB 或 >500 块时的追加日志（阈值已写进契约 §16.2）；重放与 publish 的接线在 B1a/B1b。

### 10.51 B1a：单块 async 分析（2026-10-31）

vnext 第一次真的会调模型。四条约束全部来自复审：

- **全程 async**：`llm_client.chat(messages) -> LLMResult`；文件 IO 走 `asyncio.to_thread`。`make_chat_caller()` 提供"messages → 文本"的最小适配。
- **`extracted` 带 `SourceRef`**：`normalize_extracted()` 把模型的 `quote` 转成 `SourceRef`，没有可用 quote 的条目不进 `extracted`（契约 §16.1：无来源的事实重放时也会丢，留着只是假象）。
- **校验的是引文，不是结论**：结论是概括，本来不可能逐字出现在正文里。`_verify_state()` 用 `verify_citation(quote_text=..., chapter_text=..., start, end)` 判 `EVIDENCE_LOCATED`；模型给的偏移**不被修正**（只用于判定），偏移错但引文真实会走 `RELOCATED` 通过。
- **解析失败必须炸**：`parse_block_output()` 用 `parse_json_robust`，返回非对象即 `UNPARSEABLE_ARTIFACT`；不许把空 dict 当成功落盘——那会让"模型抽风"变成"这一块没内容"。

指纹与身份：`PROMPT_STRATEGY_VERSION=1`；依赖用 `expected_dependency(prior, strategy, context_fingerprint=window.fingerprint())`；`result_id` 并进 `extracted_fingerprint`（内容变则身份变）。Prompt 里**显式告知截断**（"更早的记录因篇幅被截断"），否则模型把"没提到"当"不存在"。

测试：`backend/tests/test_vnext_analyzer.py`（8 例，fake client 不触网）：正向发布+已定位、`classify` 判 done、解析失败不落盘、无引文不入库、伪造引文留 `UNVERIFIED`、内容变则身份变、代码围栏可解析而数组拒收、截断写进 Prompt。

### 10.52 B1b/B2/B3：顺序跑整本、删除项目、项目详情页（2026-10-31）

- **B1b `run_analysis`**：按块序跑，跳过判定用 `classify(strategy=…, context_fingerprints=…)`；失败写 `FAILED` 结果并停（不继续往后跑，否则后续块的依赖全断还白花钱）；知识库读改造与 `publish_result` 在同一把书锁里，临界区只做读-改-写-发布；`_expected_context_fingerprints()` 用**重放**算期望窗口指纹（不重放就得不出"库里有什么"，指纹也就没有对照物）。
- **修 `slots_from_results`**：同块既有旧 `FAILED` 又有新 `SUCCEEDED` 时以成功为准。原来是"最后一条记录说话"，于是重跑成功后仍被判为失败过 —— 那种状态下永远跳不过去。
- **B2 删除项目**：顺序是硬的 —— **先 `os.replace` 改成 `.trash-<ts>-<id>`，再摘队列条目，最后尽力 `rmtree`**。不能直接 rmtree：删到一半失败会留下没有 `manifest.json` 的目录，`scan_projects` 会判成 `LEGACY_DIR`（用户看到"我的项目变成了旧版本"，而界面只对 vnext 项目给删除入口，于是再也删不掉）。二次确认要求原样输入 `book_id`。
- **B3 项目详情页**：`/vnext/:bookId`，块清单区分 `已完成` / `需重算（原因）` / `未分析（原因）`，删除区要输入完整 `book_id` 才亮。
- **踩坑**：`test_vnext_runner_analysis.py` 里对照项目必须同 `book_id` —— `chapter_id` 由 `(book_id, ordinal)` 决定，两个项目各自 `create_project` 会得到不同 `chapter_id`，指纹必然不同，用例会假红。

### 10.51 D-19 引文定位下沉：模型给原话，程序给位置（2026-10-31）

C2 实测把问题定性了：模型判"支持"20 次、**引文逐字命中 9/9**，却有 11 次因自报 span 对不上被判 INVALID。
所以补的不是模型能力，是**分工**。

- `locate_quote()`：先逐字查（`count_occurrences`，含重叠），找不到才退到 `citations.verify_citation`
  的 `NORMALIZED/RELOCATED`，并**回验**（EXACT 严等、其余双侧规范化相等）
- 只接受 `EXACT/NORMALIZED/RELOCATED`；拒 `SEGMENTED`（省略号拼引文）与 `FUZZY`（相似即过）
- `MIN_QUOTE_CHARS=10`：语料里「这一日无事」出现 13 次，短句在长段里几乎必然命中
- `supporting_span` 降为提示：窗口内含引文就用来破歧义，不含就**忽略**、永不因此判失败
- `CandidateVerdict` 增 `located_span`/`reported_span`/`ambiguous`/`quote_hash`
- `count_verdicts.located` 改按 `located_span` 统计（原来按 `supporting_span`，改后会把"有位置"算成 0）
- 账本 `apply_key` 加 `evidence_id` + `quote_hash`，**位置不进键**
- `locate(quote, evidence_id)` 真的被调用（旧校验器拿不到 evidence_id，验不了"属于哪一段"）
- 键名收敛：`RECHECK_MODEL_KEYS` + `render_recheck_schema()` + 双向核对用例

**踩到的坑（都是我自己写的）**：
1. 改写门禁时删了 `start, end = span[0], span[1]`，返回语句仍在用 → 任何成功 SUPPORTED 都 `NameError`。
   子智能体复现报出，`file:line` 精准。
2. 定位回验一开始用严等，会把 `NORMALIZED/RELOCATED` 全打成失败——正好抹掉要救的那类。
3. `locate` 一开始只是"开关"，没真被调用；测出后改为真正注入。

### 10.51 伏笔锚点字段落地（阶段 0，2026-10-31）

伏笔复检要"查证据"，第一件事是让证据在采集时就存在。这一批只动三处字段 + 一个校验器，不碰管线。

- `Foreshadowing` 增 `anchor_quote`/`anchor_chapter`/`motif_key`；`AnalysisResult` 增 `anchor_stats`（**必须是 dataclass 字段**，动态属性不会进 `to_dict`，重启后统计消失）。
- 新增 `backend/core/anchor.py`：`build_chapter_spans()` 用块内章节标题切边界（**块 ≠ 章**，把块首章当引文所在章是错的）；`locate_anchor()` 先逐字、再忽略空白的规范化，且**回验切片**；`verify_result_anchors()` 就地改写成原文切片、写 `anchor_chapter`、跨章边界判缺、返回 `{total, located, missing, by_kind}`。
- `analyzer.py` 在 `AnalysisResult.from_dict` 之后调用，异常只 warning——一条伏笔缺锚点不该让整章失败。
- `text_utils.deduplicate_foreshadows` 支持 6..8 位的 `anchor_quote/anchor_chapter/motif_key` 并把代表条目的锚点带出（全链路传递，否则等于没加）。
- `prompt_builder` schema 增两个字段 + 一条约束：逐字照抄、≥6 字、不改写不拼接；找不到原句就**不要列这条伏笔**。
- 配置项：`anchor_quote_enabled`(true)、`anchor_min_chars`(6)、`recheck_max_calls`(300)、`recheck_max_tokens`(60000)。
- 用例：`test_anchor_quote.py`(12) + `test_result_roundtrip_anchor.py`(4)。实测编造引文被清空、跨章拼接被拒、章号由标题反查得出。

### 10.52 账本 v4：锚点与稳定实体键（阶段 1）

- `ForeshadowItem` 增 `anchor_quote`/`anchor_chapter`/`entity_key`，`SCHEMA_VERSION` 3 → 4；旧 v3 账本照常读入（缺字段补默认值，不迁移）。
- `final_summary._build_foreshadow_catalog` 的线索从 5 元组扩到 8 元组，把锚点带过 `deduplicate_foreshadows`；catalog 条目增 `entity_key`。
- `anchor.entity_key_for(motif_key, fallback_id)`：优先母题词，否则 `clue:<id>`；**一旦写进账本不可变**——用指纹重算会把同一件事拆成两条。
- `anchor_quote_enabled=false` 时 `anchor_stats` 记 `{"disabled": true}`，语义是"没有证据"而非"没找到证据"；`anchor_min_chars` 可配。
- 用例：`test_foreshadow_ledger_anchor.py`（8 例，含 v3 旧账本兼容、键稳定性、未知字段过滤）；旧断言 `SCHEMA_VERSION == 3` 同步改 4。

### 10.53 引用校验收敛为单一实现（阶段 2 前置）

- 实现搬到 `backend/core/citations.py`（`verify_citation`/`_normalize`/`_to_raw_offset`/`summarize_checks`/五级匹配常量）。
- `backend/vnext/citations.py` 变转发层，只留 `verify_fact`/`verify_facts_against_book` 两个依赖 vnext 类型的包装。
- 理由：两套定位语义漂移时，同一引文一处对一处错，而且"两边各自都通过测试"会把问题掩盖掉。

### 10.54 复检服务层 recheck_v2（阶段 2）

- 新增 `backend/services/recheck_v2.py`：`run_recheck_v2(items, *, book_text, call_model, ...)`，判定核复用 `vnext/resolution`，本模块只做 1.x 胶水（账本→候选→提示词→调用→写回）。
- 三条硬边界：无锚点/锚点定位不到/预算用尽 → 一律 `not_checked`，不许判回收；`status != COMPLETED`。
- 编造引文 → `INVALID/QUOTE_NOT_FOUND` 且 `retryable` → 计 `not_checked`（下轮重问），不算"已核对"、也不算 `invalid`（后者表示候选本身有问题）。
- 两个坑写进代码注释：`parse_recheck` 的 `locate` 第二参是 `evidence_id` 不是正文；`extract_json_from_text` 返回 JSON 文本不是对象。
- 用例 `test_recheck_v2.py`（11 例）。

### 10.55 阶段 2 接线：开关默认关

- `final_summary.run()` 阶段 2 分流：`analysis.recheck_v2_enabled` 为真走 `_run_recheck_v2`，否则走旧复检。开关只切算法，账本/报告/进度事件/接口形状不变，默认 `false`。
- 新增 `_collect_book_text()`：从 `blocks/*.txt` 按文件名排序拼接**原文**；取不到就返回空串，`_run_recheck_v2` 据此记 `NOT_RUN` 并写 `last_recheck_summary`——**绝不拿空文本或卷摘要去核对**（摘要核引文等于没有证据）。
- 配置：`recheck_v2_enabled`(false)、`recheck_max_calls`(300)、`recheck_max_tokens`(60000)；模型调用走 `client.chat_auto(purpose="foreshadow_recheck_v2")`。
- 用例 `test_recheck_v2_wiring.py`（4 例：顺序读取、working_directory 回退、无 blocks 返回空、开关默认关）。

### 10.56 真书首测抓到的两个"假保护"

**锚点校验插错函数**：校验代码原先被插进 `_parse_response()`，那里没有正文变量，
`NameError` 被 `except` 吞成一行 warning——于是"每章都校验"实际是"每章都没校验"，
`anchor_stats` 全 0 而账本里锚点齐全（全是模型自述）。已移进 `analyze_chapter()`。
守卫用例不看行为，直接断言"`verify_result_anchors` 出现在 `analyze_chapter` 而不是
`_parse_response`，且 `chapter_content` 是该函数形参"。

**独立脚本缺代理净化**：`NO_PROXY` 里的 `[::1]` 让 httpx 抛 `InvalidURL: Invalid port: ':1]'`。
测试里由 `pytest_isolation.sanitize_proxy_env()` 处理，**脚本必须自己调**。

**首测数字（2 章 8 条）**：锚点命中率 8/8；复检在 `--max-calls 4` 下核对 2 条、
2 条均 `INSUFFICIENT`、6 条 `not_checked`、状态 `RUNNING`。样本太小，只证明链路通，
不构成对准确率的结论——见 `docs/MEASURE-RECHECK-V2-2026-10-31.md`。

### 10.57 采集契约：细节与问题分开（2026-11-01）

- **撤掉** `prompt_builder.py:32` 的“`implication` 必须预测具体未来事件”——那条指令直接让模型写剧情预测。
- schema 拆三问：`clue`（出现了什么）/ `question`（待交代什么）/ `answer_criteria`（什么算回答）；规则补“`question` 必须能被后文回答”“`answer_criteria` 不要写死结局”。
- `Foreshadowing` 增 `question`/`answer_criteria`/`lead`；账本 `SCHEMA_VERSION` 4 → 5（旧 v4 照常读入）。
- `anchor.mark_leads()`：两字段**都为空**才标 `lead`，不进复检队列；**刻意不用疑问词/专名词表**（那筛表达形式，会错杀真铺垫、放过假悬念）。
- 复检 `run_recheck_v2` 跳过 `lead` 并单独报 `leads_skipped`，不计入 `not_checked`。
- 参考事实：16 章产物里已有 51 条 `cross_block.unresolved_questions`，专项重建时可作提示。

### 10.58 有界证据包：复检不再把整本原文塞进 prompt

- 新增 `backend/core/evidence.py`：`EvidenceIndex(chapters)` 提供 `chapter_text`（定位用整章）、`window`（展示用，带 `…` 截断标记）、`find_mentions`（只取埋设之后）、`bundle_for`（最多 3 个窗口，`WINDOW_RADIUS=400`）。
- `recheck_v2` 新增 `chapters` 参数，逐候选生成证据包；`DEFAULT_BATCH_SIZE` 改为 **1**（一个候选一份证据，合并提问模型只答第一条）。
- 三个必修的坑：①定位必须用整章，用窗口会漏掉章末锚点；②`ITEM_TEMPLATE` 里的 `evidence_text` 曾仍传 `book_text`；③批大小 12 时 7 条候选只回 1 条判定。
- 实测：12 条候选 → 7 次调用、单次约 33 秒、**不再出现 token 超限**（改造前 12 次全部 100k 超限）；6 条判 `INSUFFICIENT`（16 章语料不含回收，属正确行为）。
- 采集侧复测：`question`/`answer_criteria` 覆盖 60/60，但 `lead` 为 0——这条护栏在当前模型下从不触发，只是护栏不是过滤器。

### 10.59 复审四：六处"跑得通但结论会错"

- 章号改从**标题**解析（`anchor.chapter_number_from_heading`，中文/阿拉伯/Chapter N），不再按块内序号推算——块含两章时序号必然错位（实测 60 条错 39 条）。离线重算 58/58 可定位。
- 总结入口修：`self.client` → `self._llm`，`chat_auto(prompt=…, purpose=…)` → `chat_auto(messages=[…])`，批大小固定 1，并改用 `chapters={章号: 正文}`（新增 `_collect_chapters()`）。
- 提示词补 `question`/`answer_criteria`：不补则采集字段对裁决零影响。
- 证据取窗对准**真正命中的关键词**，找不到命中词就跳过该章并标 `truncated`（原来围绕第一个关键词取窗，词不在就退化成章首，章末答案永远看不到）。
- `resolved_chapter` 改由引文位置反查（`Bundle.spans` 记窗口区间），不再写埋设章。
- 覆盖报告增 `input_total` / `dropped_no_anchor` / `dropped_anchor_missing`，能追平输入与分母。
- 用例 `test_review_round3_fixes.py`（8 例）。教训：**"证据不足"必须先证明"该找的地方找过了"**。

### 10.60 四组已知答案样本：证据分段与检索词条化

- 四组样本（`scripts/verify_recheck_fixtures.py`）：跨章正例、答案在章末、反证、截断，全部通过；A/B 判 SUPPORTED 且回收章=9。
- **证据按段编号**：一条候选给多段证据，每段一个 `evidence_id`，模型回填"引文所在那段"——否则跨章回收永远过不了门禁。
- **检索词条化**：`_keywords_for` 从整句改为 2–4 字片段 + 频次加权；整句检索只有在原句照抄时才命中，等于没检索。
- **`_normalize_payload`**：把模型写的逗号串归一成数组，但 id 仍逐个比对——格式问题与证据问题分开。
- **状态语义**：有 dropped（无锚点/定位失败）时降为 `PARTIAL`，不许报 `COMPLETED`。

### 10.61 组合场景：证据隔离、丢弃口径、截断语义

- `evidence_id` 改按**内容**算（`sha256(章|正文)`）：同章两段等长窗口曾算出同一 id，后写覆盖前写，导致"A 引用自己的原文被判 QUOTE_NOT_FOUND，而引用只展示给 B 的文字反而通过"。
- `truncated` 改按**实际遗漏**：命中多要一个（`limit=max_windows+1`），`omitted = 超出数 + 取不到词而跳过的章`，`truncated = omitted > 0`；并把遗漏数写进提示词。
- 丢弃（无锚点/定位失败）单列 `dropped_ids`，不再算作 `not_checked`；状态机按"待查"算，避免"没有证据"把整轮压成 RUNNING。
- 状态收尾的 `COMPLETED` 曾未导入 → `NameError`；已改字面量，并补空候选分支的 PARTIAL。
- 用例 `test_recheck_evidence_isolation.py`（6 例，含同章双候选与交叉引用）；样本脚本自检收紧（C 必须有有效判定且已核对、D 必须有实际遗漏）。

### 10.62 批量复检：一次问多条，漏答补问一次

- **根因纠正**：一直以为"模型一批只答一条"，实测是 `_extract_payloads` 只抽第一个 JSON 对象。改为括号配平扫描（整段 → `[...]` → `{...}` → 旧解析兜底），支持 ```json 围栏。
- 实测（16 章 58 条候选）：batch=1 → 6 次调用 / completion 19.7k / 192s；batch=6 → **1 次调用 / completion 8.8k / 54s**；batch=12 → 1 次调用 / 12 条全答。
- 默认批大小改 6（`foreshadow_recheck_batch_size`）；漏答的候选**补问一次**（只给漏的那几条），仍漏才记 `not_checked`，并计 `retried_batches`。
- 用例 `test_recheck_batching.py`（3 例：6 条一次答满 / 漏答补问只给漏的 / 两次都失败才 not_checked）。

### 10.63 并发复检：波次并行、预算先记后发、失败隔离

- `run_recheck_v2(..., concurrency=4)`：按波次 `asyncio.gather` 并行，信号量限流；只省墙钟不省 token。
- 预算**发出前**记账（并发下先发后记必然超支，而超支是已花的钱），额度不足按剩余截断波次。
- `gather(return_exceptions=True)`：失败批计 `failed_batches`，候选落 `not_checked`，不许当成"通过"或"没有回收"。
- 配置 `analysis.recheck_concurrency`（默认 4），接线在 `final_summary._run_recheck_v2`。
- 实测 24 条候选、batch=6：并发 1 → 235s，并发 4 → 69s，均 24/24 全判。
- 用例 `test_recheck_concurrency.py`（3 例：真并发且不超上限、预算不超支、单批失败隔离）。

### 10.64 复检复用分析阶段的在途上限

- `run_recheck_v2(..., limiter=...)`：总结阶段传入自己的 `_llm_sem`，让卷摘要 / 复检 / 风格共用**同一个**并发闸门。
- 顺手修旧账：`pipeline` 与 `final_summary` 各建 `Semaphore(concurrency)`，并行时峰值可到 2× 配置值。
- `analysis.recheck_concurrency` 默认 **0** = 跟随 `analysis.concurrency`；正数只能压窄波次（`min`），不能抬高。
- 用例 `test_recheck_shared_limiter.py`（2 例：外部限流是硬上限、复检与另一阶段共用时不超限）。
