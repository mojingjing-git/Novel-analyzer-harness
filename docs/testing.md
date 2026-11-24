# 测试体系

本项目采用**轻量领域聚合测试体系**，统一由 `scripts/run_tests.py` 编排后端 `pytest` 与前端 `vitest`。所有涉及等待、重试退避的测试均通过 mock 消除真实 `sleep`，全仓测试可在数秒内跑完。

## 目录与模块划分

### 后端测试（`backend/tests/`，14 个领域测试文件）

| 测试文件 | 覆盖领域 |
|---|---|
| `test_smoke.py` | `backend.*` 全模块自动导入、架构分层边界（`common` 纯标准库、`api` 委托 `services`）、测试隔离自检 |
| `test_config.py` | `ConfigManager`（UTF-8 BOM、坏配置禁写护栏）、`AppConfig` 转型、Anthropic 空地址守卫、`model_catalog` 费用估算 |
| `test_models_and_utils.py` | `AnalysisResult`、`parse_json_robust` 容错、并发原子写 `safe_save_json`、`redact` 脱敏、编码检测、`JSONAggregator`、角色卡、Excel 公式注入防护 |
| `test_error_and_moderation.py` | `error_types`、`error_taxonomy` 错误分类、内容审核拦截与短路、账户级 `Breaker` |
| `test_llm_client.py` | `LLMClient` 流式/非流式调用、温度退火与指数退避、429 `Retry-After`、SDK 零隐式重试边界、取消信号、OpenAI/Anthropic 协议、思考参数探测、流式进度节流 |
| `test_pipeline_and_kb.py` | `KnowledgeBaseManager` 快照隔离与近邻去重、`MemoryState` 滚动总结、`PromptLimits`、`AnalysisPipeline` 在途块追踪与事件回调 |
| `test_splitter.py` | `chapter_split`（正文引用防误切、卷切重置、句末标点守卫）、中文经典语料切分、TXT/DOCX 等价性、书名推断防污染 |
| `test_foreshadow_and_summary.py` | 原文引文锚点定位与核验（含越界范围过滤）、`ForeshadowLedger` 休眠与调和、`FinalSummaryRunner` 断点恢复 |
| `test_recheck.py` | `EvidenceIndex` 有界证据包（含提名章与密度窗）、`run_recheck_v2` 复检裁决、`recheck_resolution` 原文门禁校验、行车记录仪请求落盘重建与无锚点跳过 |
| `test_recheck_v3.py` | V3 多跳复检分级裁决回写、UNRESOLVED 防冻结清理、证据门禁与前置因果回指、跨题材通用辅助、停机取消与版本路由 |
| `test_bc_contracts.py` | 打捞/自动验收、逐断言证据契约（`claims_v1`/`claims_v2`）、关闭条件归约与标签指纹绑定 |
| `test_location_and_viz.py` | 人物分隔、`ensure_aggregated_cache` 按需缓存、Excel 自动聚合导出、全书图谱清洗与 `/api/viz/atlas`、别名归并角色卡 |
| `test_queue_and_workspace.py` | `QueueManager` 状态机、ETA 计算、`UsageLedger` 跨进程累计账本与路由 |
| `test_api_and_ws.py` | `X-Session-Token` 鉴权、`SPAStaticFiles` 回退、`ProgressHub` 广播、`/ws/progress` 心跳与断连 |

此外保留 `backend/tests/fixtures/classic_corpus.py`，供切分测试复用。

### 前端测试（`frontend/src/__tests__/`，5 个测试文件）

将同域测试聚合为 5 个文件，降低 Vitest 在每个测试文件上初始化 `jsdom` 环境的开销；并由 `frontend/src/__tests__/setup.ts` 为 Node 26+ 环境补齐内存版 `localStorage` / `sessionStorage` 垫片：

| 测试文件 | 覆盖领域 |
|---|---|
| `api.test.ts` | `sessionToken` 单例、`api` HTTP 客户端鉴权与 422 错误展开、`useProgressSocket` 指数退避重连与主动关闭 |
| `utils.test.ts` | `router`、`markdown`、`splitterView`、`laneRegistry`、`queueFeed`、`settingsForm`、`summaryLanes`、`summaryProgress` |
| `components.test.ts` | `DiscoveryFeed`、`LaneView`、`RunDashboard`、`BookSelector`、`ChapterDetailPanel`、`useLogStore` |
| `pages.test.ts` | `QueuePage`、`SettingsPage`（含 Group 6 Prompt 预览）、`SplitterPage`、`SummaryPage`（含文风量化栏与 Excel 导出）、`TimelinePage`、`WorkspacePage`、`StatsPage`、`CharacterCardPage`、`WorldStagePage` |
| `summaryPageTelemetry.test.ts` | `SummaryPage` 并发车道切换与 WebSocket 吞吐遥测隔离（`summary_concurrency`、`v3_concurrency` 回退与实时波形） |

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

- **写前隔离**（`backend/tests/conftest.py` + `backend/tests/pytest_isolation.py`）：在任何业务 `backend.*` 模块导入前，把 `cwd`、`NOVEL_ROOT` 和 `NOVEL_RUNTIME_DIR` 切到会话级临时目录，并复用 `backend/common/proxy_env.py` 清理本地代理环境变量，确保模块顶层实例化的日志、队列、配置文件绝不污染 `runtime/` 或真实工作区。每条用例前后由 autouse fixture 校验隔离不变量并重置运行时单例；`scripts/run_tests.py` 本地执行时自动将 pytest 缓存定向至 `runtime/.pytest_cache/`，保持仓库根目录洁净。
- **SSL Context 缓存**（`backend/tests/pytest_ssl_cache.py`）：缓存 `ssl.create_default_context`，避免 `httpx` / `AsyncOpenAI` 每次实例化客户端重复加载系统证书库。
