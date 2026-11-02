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
- **测试**：后端 1823 个用例 / 146 个测试文件，分 5 档（T0 冒烟 → T4 全仓，见
  [docs/testing.md](./docs/testing.md)）；前端 33 个测试文件。
  数量以 `pytest --collect-only -q` 与 `npm run test:coverage` 实时为准

---

## 2. 仓库结构（完整）

```
.
├── backend/                      # FastAPI 后端（生产代码 109 个 .py）
│   ├── app.py                    # FastAPI 工厂（CORS、静态托管、路由注册、lifespan 工作区扫描）
│   ├── main.py                   # 后端启动器（127.0.0.1:8000）
│   ├── ws_events.py              # WS 协议权威定义（WSType 11 种 / WSState 12 种 / PipelineStatus + payload 契约）
│   ├── progress_hub.py           # 进度广播中枢（分级驱逐慢消费者 + token_delta 150ms 节流 + log 转发）
│   ├── common/                   # 两层引擎共用纯逻辑层（错误分类 / 审核标记 / LLM 错误信号；只依赖标准库）
│   ├── core/                     # 1.x 引擎（error_types / error_taxonomy / moderation 为 common 的兼容转发层）
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
│   ├── services/                 # 15 个模块
│   │   ├── queue_service.py      # 分析运行编排（AnalysisService 单例）
│   │   ├── queue_manager.py      # 队列状态机与持久化
│   │   ├── analysis_stats.py     # 会话统计累加
│   │   ├── pipeline_events.py    # WS 事件翻译层
│   │   ├── final_summary.py      # 最终总结（4 阶段 + 伏笔账本 + 断点续跑）
│   │   ├── recheck_v2.py         # 带证据的伏笔复检（默认关闭）
│   │   ├── splitter_service.py   # 切分引擎（18 条章正则 + 5 条卷正则 + 去重/合并/拆分）
│   │   ├── workspace_service.py  # 工作区与归档管理
│   │   ├── book_service.py       # 书名 → 目录映射
│   │   ├── summary_service.py    # 总结任务生命周期
│   │   ├── style_service.py      # 风格分析任务生命周期
│   │   ├── location_normalization_service.py  # 地点归一化编排
│   │   ├── location_normalizer.py  # 地点与空间关系归一化
│   │   ├── viz_service.py        # 可视化数据聚合
│   │   └── vnext_service.py      # vnext 服务门面
│   ├── vnext/                    # 25 个模块：vnext 数据空间（独立数据层，禁止反向 import 1.x）
│   │   ├── constants.py          # 格式标识 / 版本 / 文件名常量 / 错误码
│   │   ├── project.py            # 建项目、目录约定、提交清单
│   │   ├── analyzer.py           # 单块分析
│   │   ├── blocks.py             # 块规划与失效判定
│   │   ├── results.py            # 块结果落盘与新鲜度判定
│   │   ├── knowledge.py          # 知识快照（排他上界）
│   │   ├── prompt_view.py        # 提示词视图：把知识库裁成窗口
│   │   ├── prompt_limits.py      # 提示词上下文的 9 项统一上限
│   │   ├── context.py            # 复检取材
│   │   ├── foreshadow.py         # 稳定伏笔实体与截止点候选视图
│   │   ├── recheck_plan.py       # 复检规划与覆盖汇总
│   │   ├── resolution.py         # 原文证据校验与全自动裁决
│   │   ├── graph.py              # 图谱边（共现与语义关系分表）
│   │   ├── citations.py          # 引用校验转发层
│   │   ├── runner.py             # 复检执行入口
│   │   ├── tasks.py              # 任务账本
│   │   ├── queue.py              # vnext 队列状态与恢复
│   │   ├── registry.py           # 书目发现与索引
│   │   ├── budget.py             # 调用预算
│   │   ├── breaker.py            # 账户级熔断（当前无调用方）
│   │   ├── concurrency.py        # 交互保护
│   │   ├── consistency.py        # 写入一致性
│   │   ├── fingerprint.py        # 身份与指纹判据
│   │   ├── rolling.py            # 有序 rolling 与缺口恢复
│   │   └── artifacts.py          # 产物读写（格式门禁 + 原子写）
│   ├── api/                      # 12 个路由模块 + auth + ws（72 个 REST 端点）
│   │   ├── routes_analysis.py    # 分析控制、队列、风格、日志
│   │   ├── routes_books.py       # 书目与结果查询
│   │   ├── routes_summary.py     # 总结控制
│   │   ├── routes_viz.py         # 可视化
│   │   ├── routes_workspace.py   # 工作区与归档
│   │   ├── routes_splitter.py    # 切分
│   │   ├── routes_aggregate.py   # 聚合与 Excel
│   │   ├── routes_foreshadow.py  # 伏笔分类元数据
│   │   ├── routes_location_normalization.py  # 地点归一化
│   │   ├── routes_prompt.py      # 提示词预览
│   │   ├── routes_settings.py    # 配置读写 / 预设 / 模型探测
│   │   ├── routes_vnext.py       # vnext 数据空间
│   │   ├── auth.py               # X-Session-Token 鉴权
│   │   └── ws.py                 # WebSocket /ws/progress（25s 心跳 + Origin 白名单）
│   ├── config/
│   │   ├── constants.py          # 全部硬编码默认值 + 50 类伏笔定义（7 组）
│   │   ├── settings.py           # APIConfig / AnalysisConfig / GUIConfig / AppConfig + ConfigManager
│   │   └── presets.py            # 厂商预设
│   ├── models/
│   │   ├── analysis_result.py    # AnalysisResult 结构化输出
│   │   └── knowledge.py          # KnowledgeBase 跨章记忆
│   ├── utils/
│   │   ├── json_utils.py         # JSON 提取 + 9 步容错解析 + 原子保存
│   │   ├── aggregate_utils.py    # 聚合（10 类分类文件 + 1 个全量）
│   │   ├── foreshadow_ledger.py  # 伏笔账本状态机
│   │   ├── character_card_generator.py  # 角色卡生成
│   │   ├── excel_export.py       # Excel 导出
│   │   ├── export_utils.py       # Markdown 导出
│   │   ├── text_utils.py         # 编码检测 / 文本工具
│   │   └── docx_extract.py       # DOCX 正文抽取（纯标准库）
│   └── tests/                    # 146 个测试文件（pytest 收集 backend/tests + tests）
├── frontend/src/                 # Vue 3 + TypeScript（80 个文件，含 32 个测试文件）
│   ├── pages/                    # 12 个页面（router.ts）
│   │   ├── QueuePage.vue         # 分析队列
│   │   ├── SettingsPage.vue      # 设置
│   │   ├── CharacterCardPage.vue # 角色卡
│   │   ├── SummaryPage.vue       # 最终总结
│   │   ├── SplitterPage.vue      # 小说切分
│   │   ├── WorkspacePage.vue     # 工作区与归档
│   │   ├── PromptPreviewPage.vue # 提示词预览
│   │   ├── StylePage.vue         # 风格分析
│   │   ├── StatsPage.vue         # 统计面板
│   │   ├── TimelinePage.vue      # 时间线
│   │   ├── GraphPage.vue         # 关系图
│   │   └── MapPage.vue           # 地图
│   ├── components/               # 13 个（AppLayout / BookSelector / ChapterDetailPanel /
│   │                             #   ChapterValue / ConfirmDialog / CountUp / DiscoveryFeed /
│   │                             #   Icon / LaneView / LogConsole / ProgressBar / RunDashboard /
│   │                             #   ToggleSwitch）
│   ├── composables/              # useLocale / useLogStore / useRipple / vTooltip
│   ├── api/                      # client.ts（58 个方法）/ sessionToken.ts / useProgressSocket.ts
│   ├── utils/                    # 9 个纯函数模块（laneRegistry / markdown / queueFeed /
│   │                             #   settingsForm / splitterView / summaryLanes /
│   │                             #   summaryProgress / theme / workspaceView）
│   ├── router.ts                 # 1 个根布局 + 12 条子路由
│   └── main.css                  # Win11 Fluent 设计系统（--win-* 变量）
├── desktop.py                    # pywebview 桌面入口（单实例锁 + 窗口控制 + 崩溃诊断）
├── scripts/                      # 测试与测量脚本（统一入口 run_tests.py，5 档分级）
├── pytest_tiers.py               # 测试 5 档分级的规则与清单（T0 冒烟 → T4 全仓）
├── pytest_isolation.py           # pytest 会话级隔离原语（cwd/NOVEL_ROOT 双隔离 + 代理净化）
├── docs/                         # 文档（见 docs/README.md）
├── agent.md                      # 本文件
├── README.md                     # 项目说明
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
| pytest | 默认收集 `backend/tests/` + 顶层 `tests/`；`--tier 0-4` 按档过滤（pytest_tiers.py）；数量以 `pytest --collect-only -q` 为准 |

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
  → final_summary（4 阶段总结，与归一化解耦）
      → 分卷摘要 + 伏笔调和
      → 全书伏笔复检
      → 风格分析
      → 最终报告
  → final_summary_report.md
  → aggregate_utils → output/*_aggregated.json（10 类分类 + 1 个全量）
  → style_analyzer → style.md
  → excel_export → analysis_data.xlsx
  → 归一化（独立路径，只服务地图）
      → 地点并发归一化 → 空间关系并发归一化 → 机械去重
  → output/locations_normalized.json + spatial_relationships_normalized.json
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

### 4.7 最终总结（final_summary.py）

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

### 5.1 核心层（backend/core/，18 个模块）

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

### 5.2 服务层（backend/services/，15 个模块）

| 模块 | 职责 |
|---|---|
| `queue_service.py` | 分析运行编排（`AnalysisService` 单例） |
| `queue_manager.py` | 队列状态机、扫描、模型预检与持久化 |
| `analysis_stats.py` | 会话统计累加（token、每章记录、重试成本） |
| `pipeline_events.py` | WS 事件翻译层：流水线回调 → 广播消息 |
| `final_summary.py` | 最终总结四阶段、伏笔账本与断点续跑 |
| `recheck_v2.py` | 带证据的伏笔复检（默认关闭，由 `recheck_v2_enabled` 控制） |
| `splitter_service.py` | 切分引擎：18 条章节正则 + 5 条卷正则，去重 / 合并 / 超章拆分 / 书名推断 |
| `workspace_service.py` | 工作区与归档管理 |
| `book_service.py` | 书名 → `workspace/{书名}/` 目录映射 |
| `summary_service.py` | 总结任务生命周期 |
| `style_service.py` | 风格分析任务生命周期 |
| `location_normalization_service.py` | 地点归一化任务编排 |
| `location_normalizer.py` | 地点与空间关系归一化实现 |
| `viz_service.py` | 可视化数据聚合（时间线 / 关系图 / 地图） |
| `vnext_service.py` | vnext 服务门面 |

### 5.3 工具层（backend/utils/，8 个模块）

| 模块 | 职责 |
|---|---|
| `json_utils.py` | JSON 提取、9 步容错解析、原子保存 |
| `aggregate_utils.py` | 聚合：10 类分类文件 + 1 个全量 |
| `foreshadow_ledger.py` | 伏笔账本状态机 |
| `character_card_generator.py` | 角色卡生成 |
| `excel_export.py` | Excel 导出 |
| `export_utils.py` | Markdown 导出 |
| `text_utils.py` | 编码检测与文本工具 |
| `docx_extract.py` | DOCX 正文抽取（纯标准库） |

### 5.4 API 层（backend/api/，12 个路由模块 + auth + ws）

共 72 个 REST 端点，全部挂在 `/api` 前缀下，端点清单见 [docs/api.md](./docs/api.md)。

| 模块 | 职责 |
|---|---|
| `routes_analysis.py` | 分析控制、队列、风格分析、日志下载 |
| `routes_books.py` | 书目与结果查询 |
| `routes_summary.py` | 总结控制 |
| `routes_viz.py` | 可视化 |
| `routes_workspace.py` | 工作区与归档 |
| `routes_splitter.py` | 切分 |
| `routes_aggregate.py` | 聚合与 Excel |
| `routes_foreshadow.py` | 伏笔分类元数据 |
| `routes_location_normalization.py` | 地点归一化 |
| `routes_prompt.py` | 提示词预览 |
| `routes_settings.py` | 配置读写、预设、模型探测 |
| `routes_vnext.py` | vnext 数据空间 |
| `auth.py` | `X-Session-Token` 鉴权依赖 |
| `ws.py` | `/ws/progress`：Origin 白名单 + 25 秒心跳 |

### 5.5 vnext 数据层（backend/vnext/，25 个模块）

vnext 是与 1.x 并存的第二套数据模型，目录与语义见
[docs/data-contract-vnext.md](./docs/data-contract-vnext.md)。`backend/vnext/__init__.py`
禁止反向 import 旧数据层。

当前 `backend/api/routes_vnext.py` 只提供项目与队列管理，**还没有接入生产分析流水线**
（`run_analysis` 仅被测试调用），因此 vnext 里已实现的机制不能算作 1.x 缺陷已修，
见 [docs/issues.md](./docs/issues.md)。

---

## 6. 前端模块详解

### 6.1 API 层

| 文件 | 职责 |
|---|---|
| `api/client.ts` | REST 客户端，63 个方法 + TypeScript DTO + 统一 `X-Session-Token` 注入 |
| `api/sessionToken.ts` | 会话 token 单例（pywebview js_api 获取） |
| `api/useProgressSocket.ts` | WebSocket 单例连接 + 协议镜像类型（与 `backend/ws_events.py` 对应） |

### 6.2 状态管理

没有独立的 store 目录：全局日志状态放在 `composables/useLogStore.ts`，其余状态由各页面
的 `ref` / `computed` 管理。`composables/` 下另有 `useLocale.ts`、`useRipple.ts`、
`vTooltip.ts`。

### 6.3 设计系统（main.css）

Win11 Fluent 风格，颜色 / 圆角 / 阴影 / 间距统一走 `--win-*` 变量，禁用 iOS 硬编码色值
与 `backdrop-filter`。

### 6.4 页面（14 个）

| 页面 | 职责 |
|---|---|
| `QueuePage.vue` | 分析队列：书籍队列、启停分析、进度车道、token 统计、日志下载 |
| `VnextPage.vue` | vnext 项目列表、创建项目、入队出队 |
| `VnextDetailPage.vue` | vnext 项目块详情与删除项目 |
| `SettingsPage.vue` | 设置：API / 分析参数、预设、模型探测、思考参数探测、伏笔保留类别 |
| `CharacterCardPage.vue` | 角色列表与角色卡详情 |
| `SummaryPage.vue` | 总结启停、阶段进度、最终报告、聚合文件浏览与 Excel 导出 |
| `SplitterPage.vue` | 切分预览、批量切分、书名推断 |
| `WorkspacePage.vue` | 工作区与归档列表、归档与删除 |
| `PromptPreviewPage.vue` | 查看某章的 system + user 提示词与长度统计 |
| `StylePage.vue` | 风格分析启停与结果查看 |
| `StatsPage.vue` | 会话与全书 token 统计 |
| `TimelinePage.vue` | 时间线与伏笔分类排名 |
| `GraphPage.vue` | 关系图（章区间、边权、节点数过滤） |
| `MapPage.vue` | 地点地图与地点归一化启停 |

### 6.5 组件（12 个）

`AppLayout.vue`（根布局）、`BookSelector.vue`、`ChapterDetailPanel.vue`、
`ChapterValue.vue`、`ConfirmDialog.vue`、`CountUp.vue`、`DiscoveryFeed.vue`、`Icon.vue`、
`LaneView.vue`、`LogConsole.vue`、`ProgressBar.vue`、`RunDashboard.vue`。

### 6.6 前端关键模式

- **纯函数外置**：`utils/` 下 8 个模块承载车道注册表、队列状态机、总结进度、设置表单、
  切分视图、工作区视图等逻辑，各自带同名测试文件，便于单测。
- **自研 Markdown 渲染**：`utils/markdown.ts` 零依赖渲染 Markdown → HTML。
  注意它是 `v-html` 注入路径，安全性见 [docs/issues.md](./docs/issues.md)。
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

# 运行测试（统一入口，5 档分级，规则与清单见 pytest_tiers.py / docs/testing.md）
python scripts/run_tests.py --tier 0        # 冒烟（~12s）：backend 全模块导入 + 核心纯函数
python scripts/run_tests.py --tier 1        # 快速单测（~25s）：T0 + 默认档单测
python scripts/run_tests.py --tier 2        # 标准门禁（~30s 并行）：T1 + 中速文件 + 顶层语料基线
python scripts/run_tests.py --tier 3        # pytest 全量（~90s 并行）：T2 + 慢速重试链路
python scripts/run_tests.py                 # 全仓验证（~2min）：T3 + 前端 vitest
python scripts/run_tests.py --list-tiers    # 查看各档文件/用例分布
# （测试全程运行在 cwd/NOVEL_ROOT 双隔离的会话临时根：根目录 conftest.py + pytest_isolation.py
#   在任何 backend.* import 前生效，真实 config.json/queue_state.json/日志/workspace 零写入；
#   守卫见 backend/tests/test_runtime_isolation.py。不要在测试里依赖真实工作区数据。）

# 运行指定测试
python -m pytest backend/tests/test_pipeline_inflight.py -v

# 迭代期：只跑与本次改动相关的测试（秒级），可与档位组合
python scripts/run_tests.py --related               # 选择 + 执行
python scripts/run_tests.py --related --tier 2      # 相关集 ∩ 档位
python scripts/pytest_related.py --run --base HEAD~3  # 独立用法（与某基线比较）

# 备选：切组并行（不依赖 xdist 插件）
python scripts/pytest_split.py -n 8

# 只有在需要逐条输出时，才用单进程全量
python -m pytest -o addopts= -q
```

> **测试节奏（按档选用）**：
> 1. **改几行 → T0**（~12s 冒烟）：确认模块能导入、核心纯函数没炸。
> 2. **改完一个功能点 → T1 或相关集**：`--tier 1`（~25s）或 `--related`（秒级）。
> 3. **提交前 → T2**（标准门禁）：相关集是"没弄坏自己"，T2 是"没弄坏别人"的最低门槛。
> 4. **宣布完成 / 推送 → T3 或 T4**：T3 是 pytest 全量，T4 再加前端 vitest（无参数即 T4）。
>    **宣布完成必须有一遍 T3/T4**，并在提交信息或 PR 描述里写明跑的是哪一档。
> 5. 新测试文件自动落 T1；实测超过 1s 的文件必须移进 pytest_tiers.py 的
>    `MEDIUM_FILES`，否则 T1 会被悄悄拖慢（守护见 test_tier_assignment.py）。
>
> **为什么绕开 pytest-xdist 的坑**：本仓库的 `pytest_isolation` 在导入期 chdir 到临时目录
> 并按 testpaths 建占位目录，xdist 每个 worker 都这么干一遍，主进程就看不到用例了
> （`-n 8` 直接 "no tests ran"）。绕法是**用绝对路径 + 显式 `-c` 指定 ini**
> （`scripts/run_tests.py` 已封装）；`pytest_split.py` 则改成按文件切组、各起独立进程。
> **小档默认串行**：xdist worker 各自做隔离 + 全量收集，T0/T1 用例少时并行反而慢。
> **切组必须核对用例总数**：`run_tests.py` 跑前先数一遍、跑后对账——分组过滤写错时
> 会漏掉整份测试文件，却仍报"全组通过"。

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
# 迭代期：改几行跑冒烟，改完功能点跑相关集或 T1
python scripts/run_tests.py --tier 0
python scripts/run_tests.py --related

# 提交前：标准门禁；宣布完成/推送前：pytest 全量或全仓
python scripts/run_tests.py --tier 2
python scripts/run_tests.py --tier 3
python scripts/run_tests.py            # T4 = pytest 全量 + 前端 vitest

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
4. 持久化语言需要扩 `routes_settings.py` 的 `is_running` 护栏——它当前只挡 analysis，
   而设置写入会影响 summary / style / location_normalization 服务
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

