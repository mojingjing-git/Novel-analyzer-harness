# 测试体系

本项目采用**轻量领域聚合测试体系**，统一由 `scripts/run_tests.py` 编排后端 `pytest` 与前端 `vitest`。所有涉及等待、重试退避的测试均通过 mock 消除真实 `sleep`，全仓测试可在数秒内跑完。

## 目录与模块划分

### 后端测试（`backend/tests/`，19 个领域测试文件）

| 测试文件 | 覆盖领域 |
|---|---|
| `test_smoke.py` | `backend.*` 全模块自动导入、架构分层边界（`vnext` 零反向依赖、`common` 纯标准库、`api` 委托 `services`）、测试隔离自检 |
| `test_config.py` | `ConfigManager`（UTF-8 BOM、坏配置禁写护栏）、`AppConfig` 转型、Anthropic 空地址守卫、`model_catalog` 费用估算 |
| `test_models_and_utils.py` | `AnalysisResult`、`parse_json_robust` 容错、并发原子写 `safe_save_json`、`redact` 脱敏、编码检测、`JSONAggregator`、角色卡、Excel 公式注入防护 |
| `test_error_and_moderation.py` | `error_types`、`error_taxonomy` 错误分类、内容审核拦截与短路、账户级 `Breaker` |
| `test_llm_client.py` | `LLMClient` 流式/非流式调用、温度退火与指数退避、429 `Retry-After`、取消信号、OpenAI/Anthropic 协议、思考参数探测、流式进度节流 |
| `test_pipeline_and_kb.py` | `KnowledgeBaseManager` 快照隔离与近邻去重、`MemoryState` 滚动总结、`PromptLimits`、`AnalysisPipeline` 在途块追踪与事件回调 |
| `test_splitter.py` | `chapter_split`（正文引用防误切、卷切重置、句末标点守卫）、中文经典语料切分、TXT/DOCX 等价性、书名推断防污染 |
| `test_foreshadow_and_summary.py` | 原文引文锚点定位与核验、`ForeshadowLedger` 休眠与调和、`FinalSummaryRunner` 断点恢复 |
| `test_recheck.py` | `EvidenceIndex` 有界证据包、`run_recheck_v2` 复检裁决与无锚点跳过 |
| `test_b_trace.py` | B 类全链路遗漏追踪（B1–B4 事件落盘与默认关闭零干扰） |
| `test_c_audit_selector.py` | C 类按需复审选择器（机械风险信号与触发条件） |
| `test_bc_contracts.py` | 打捞/自动验收、逐断言证据契约（`claims_v1`/`claims_v2`）、关闭条件归约与语料抽样隔离 |
| `test_t3_b_fixes.py` | B2/B4 修复、行车记录仪请求落盘与校准匹配器回归 |
| `test_bc_production_readiness.py` | B/C 生产收尾契约（闭环按需复审、跨度时序、溯源缓存、登记位置规则两臂预算与冻结校验） |
| `test_location_and_viz.py` | 地点等价判定与聚合、`LocationNormalizationService` 状态与路由 |
| `test_queue_and_workspace.py` | `QueueManager` 状态机、ETA 计算、`UsageLedger` 跨进程累计账本与路由 |
| `test_api_and_ws.py` | `X-Session-Token` 鉴权、`ProgressHub` 广播、`/ws/progress` 心跳与断连 |
| `test_vnext_core.py` | `vnext` 项目清单、排他上界知识快照、伏笔实体登记与 `cutoff_view` |
| `test_vnext_workflow.py` | `vnext` 复检证据核验、`runner.run_analysis` 顺序执行与断点续跑、默认路由冻结（404）与 `NOVEL_ENABLE_VNEXT_ROUTES=1` 启用 |

此外保留 `backend/tests/fixtures/`（`classic_corpus.py` 与 `vnext_corpus.py`），供切分测试及 `scripts/measure_c1_pipeline.py`、`scripts/measure_c3_sampling.py` 评测脚本复用。

### 前端测试（`frontend/src/__tests__/`，4 个聚合测试文件）

将同域测试聚合为 4 个文件，大幅降低 Vitest 在每个测试文件上初始化 `jsdom` 环境的开销；并由 `frontend/src/__tests__/setup.ts` 为 Node 26+ 环境补齐内存版 `localStorage` / `sessionStorage` 垫片：

| 测试文件 | 覆盖领域 |
|---|---|
| `api.test.ts` | `sessionToken` 单例、`api` HTTP 客户端鉴权与 422 错误展开、`useProgressSocket` 指数退避重连与主动关闭 |
| `utils.test.ts` | `router`、`markdown`、`splitterView`、`laneRegistry`、`queueFeed`、`settingsForm`、`summaryLanes`、`summaryProgress` |
| `components.test.ts` | `DiscoveryFeed`、`LaneView`、`RunDashboard`、`BookSelector`、`ChapterDetailPanel`、`useLogStore` |
| `pages.test.ts` | `QueuePage`、`SettingsPage`、`SplitterPage`、`StylePage`、`SummaryPage`、`TimelinePage`、`WorkspacePage` |

## 常用命令

```bash
# 全仓验证（后端 pytest + 前端 vitest）
python scripts/run_tests.py

# 只跑后端 pytest（约 4~5 秒）
python scripts/run_tests.py --no-frontend

# 只跑指定测试文件或透传 pytest 参数
python scripts/run_tests.py backend/tests/test_llm_client.py --no-frontend
python scripts/run_tests.py --no-frontend -- -k recheck

# 单独跑前端测试或构建检查
cd frontend && npm run test
cd frontend && npm run build
```

## 底层机制

- **写前隔离**（根 `conftest.py` + `pytest_isolation.py`）：在任何 `backend.*` 模块导入前，把 `cwd` 和 `NOVEL_ROOT` 切到会话级临时目录，并清理本地代理环境变量，确保模块顶层实例化的日志、队列、配置文件绝不污染真实工作区。每条用例前后由 autouse fixture 校验隔离不变量并重置运行时单例。
- **SSL Context 缓存**（`pytest_ssl_cache.py`）：缓存 `ssl.create_default_context`，避免 `httpx` / `AsyncOpenAI` 每次实例化客户端重复加载系统证书库。
