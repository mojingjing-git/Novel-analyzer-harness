---
doc_type: report
plan: docs/PLAN-cleanup-batch-3-2026-10-14.md
date: 2026-10-14
status: Step0/R0 至 Step5/R5 已通过；R5 第三轮主审通过（2026-10-15）；用户要求 Step6 先不进行、等待讨论；自动回访已暂停
---

# 第三批清理交付报告（Step0/R0 通过 + Step1 通过 + Step2 实施版）

本报告记录 Step0/R0 至 Step5/R5 的实施、审批与返修历史；当前均已通过主审。用户明确要求 Step6 先不进行，等待讨论，自动回访已暂停；未实施第六步修复或迁移，第三批整体尚未完成。全程未做 Git 提交 / 分支切换 / worktree，历史误操作如实记录。

## Step 编号与状态

- **Step 0 / Review R0：一轮未通过 → 二轮写前隔离，R0 三轮已审批通过**。
- **Step 1 / Review R1：一轮与二轮未通过，三轮返修后审批通过**。
- **Step 2 / Review R2：第三轮主审通过**（2026-10-15），仅放行 Step3；一轮与二轮未通过和返修历史保留。
- **Step 3 / Review R3：第三轮主审已通过**（2026-10-15）；前两轮未通过与返修历史保留，部分缓存 spy 为有恢复保障的 try/finally，并非全部使用 monkeypatch。
- **Step 4 / Review R4：主审通过**（2026-10-15），仅放行 Step5；后缀列表单一来源，书名规则保留不同职责，不合并。
- **Step 5 / Review R5：两类服务生命周期审计完成，抽取收益不足风险故保留重复**（2026-10-15），待 R5 主审。
- 对照基线：真实 HEAD `7e70c8395bd5a3e1eecc074453f77369b8e520ac`（前两批清理提交 `90c3699`，勘查时 HEAD 为 `7e70c83`）。
- 工作区对照：执行前已含其他会话未提交改动（`CHANGELOG.md`、`agent.md`、`backend/api/routes_settings.py`、`desktop.py`、`backend/tests/test_desktop_pythonw.py`、`frontend/src/**`、`docs/PLAN-*` 等，以及未跟踪 `test_settings_probe_route.py`、`sessionToken.*`）。本步未触碰、未还原、未提交这些文件。

## R0 第一轮被拒原因（阻塞项 P1）

主审判定暂不通过，理由是我第一轮的日志隔离是「写后移除」而不是「写前隔离」：

1. 原 `_redirect_api_failures_logger`（conftest 第 99 行）先 `import backend.core.llm_failure_logger`，模块顶层 `FailureLogger()` 已按真实 cwd 创建/打开 `api_failures.log`，之后才移除 handler；默认文件原本不存在时，仅 collection/import 也会创建真实文件。
2. 原 `_enforce_api_failures_isolation` 只在用例前后调用，测试内部重新 `FailureLogger()` 仍会按真实 cwd 建 handler 并在 `record_failure` 中写入。
3. 主审以「完全临时外层 cwd + importlib 加载现有 conftest + `FailureLogger().record_failure(...)`」复现：`default_log_created_by_isolation_import=True`、`default_log_grew_inside_test=True`、`failure_handlers_outside_isolation=1`。该复现只写临时目录，未动真实文件。

约束：不得通过恢复/截断真实日志、测试后删 handler、或增加"handler 此刻正常"的断言来通过。

## 修正方案 + 本步修改（与既有脏改动区分）

改为 **import 前的 cwd 隔离**：在任何 `backend.*` import 之前把测试进程 cwd 切到会话级隔离根，并把 `NOVEL_ROOT` 指向它。这样「默认相对路径」在首次 import、模块顶层实例化、以及用例内任意重建实例时都落在隔离根，不再需要事后移除 handler。保留 NOVEL_ROOT、合成配置、密钥剥离与单例/缓存处理；不改生产功能，也不为测试新增生产配置项。隔离初始化异常改为抛可诊断错误（不再 `except Exception: return` 后继续跑未隔离测试）。

| 文件 | 类型 | 修改目的 |
|---|---|---|
| `pytest_isolation.py` | 新增（根目录） | 隔离原语独立成模块：`install()` 在任何 import 前 `chdir` 到会话临时根 + 设 `NOVEL_ROOT` + 写合成 `config.json` + 剥离密钥环境变量 + 建 `testpaths` 占位目录；提供 `assert_isolation_intact()`、`api_failures_handlers_outside_isolation()`、`_is_null_device()`、`reset_runtime_singletons()`、`cleanup()` |
| `conftest.py` | 新增（根目录） | 只做 `pytest_isolation.install(REPO_ROOT)`；autouse fixture 用例前后 `assert_isolation_intact()`，用例后重置进程级单例与 `book_service` 缓存 |
| `backend/tests/test_runtime_isolation.py` | 新增 | 进程内隔离契约 + 独立子进程回归（默认日志 absent / sentinel 两场景）+ 正向对照（真实泄漏必被抓、nul 设备精确排除） |
| `backend/tests/test_archive_path_update.py` | 修改 | 两个用例补齐 `queue._items` 与 `book_service._books`/`_manual_books` 还原，并断言 `QUEUE_STATE_FILE.parent == NOVEL_ROOT` |

为什么拆成 `pytest_isolation.py`：仓库根与 `tests/` 各有一个 `conftest.py`，`import conftest` 会随收集范围解析到不同模块对象——全量运行时曾出现 `AttributeError: module 'conftest' has no attribute ...`（定向运行却不复现）。独立模块保证测试与 conftest 共享同一份状态。

**准确 diff**：仅含本步的补丁存于 `docs/patches/step0-r0.patch`（LF 行尾，25242 字节；`git apply --check -R` 退出码 0，说明补丁与当前工作区逐字一致）。三个新文件用 `git diff --no-index -- /dev/null <file>` 呈现，避免未跟踪文件被 `git diff` 漏掉。

本步修改之外的脏改动均属其他会话，保持原样：`CHANGELOG.md`、`agent.md`、`backend/api/routes_settings.py`、`desktop.py`、`backend/tests/test_desktop_pythonw.py`、`frontend/src/**`、`docs/PLAN-lane-*`、`docs/PLAN-session-token-*`、`docs/PLAN-thinking-mode-*`、`backend/tests/test_settings_probe_route.py`、`docs/PLAN-provider-error-taxonomy-2026-09-30.md`、`frontend/src/api/sessionToken.*`。

计划修订（纳入独立 reviewer 的 8 条证据）写入 `docs/PLAN-cleanup-batch-3-2026-10-14.md` §2.1，并在 §3 / §5 / §6 / §9 / §11.1 落实验收口径。

## 测试与运行隔离根

- **运行隔离根**：`%TEMP%\novel-analyzer-pytests-*`（`pytest_isolation.install` 顶层创建，cwd + NOVEL_ROOT 双向一致）；运行结束后自动删除，`leftover_temp_dirs=0`。
- **主审复现方式复验**：临时目录作原始 cwd + importlib 加载隔离原语 + 首次 `import backend.core.llm_failure_logger` + 用例内 `FailureLogger().record_failure(...)`，结果 `default_log_created_by_isolation_import=False`、`orig_cwd_files=[]`、`handlers_outside=[]`，两个 handler 均指向隔离根。三个原始判据全部为 false/空。
- **2 个 warning**：`backend/tests/test_moderation.py` 两例 `RuntimeWarning: coroutine ... was never awaited`（`backend/core/pipeline.py:653`），既有、与本步无关。
- **未执行项**：`backend/scripts/fix_metadata_titles.py`（约束禁止执行）；任何真实 LLM 调用（无 mock 的探针不跑）。
- 环境未继承真实密钥：`LLM_API_KEY`/`MINIMAX_API_KEY`/`NOVEL_ANALYZER_SESSION_TOKEN` 执行前均未设置（仅记布尔），conftest 仍主动清空以防子进程继承。


| 命令 | 结果 | 退出码 |
|---|---|---|
| `python -m pytest --collect-only -o addopts=''` | **496 tests collected** | 0 |
| `pytest backend/tests/test_runtime_isolation.py backend/tests/test_archive_path_update.py` | 全部通过 | 0 |
| 易泄漏子集 `test_streaming/test_llm_mock/test_provider_heal/test_anthropic_provider/test_moderation/test_failure_logger` | 全部通过 | 0 |
| 路由/书目子集 `test_settings_probe_route/test_session_token_auth/test_book_structure` | 全部通过 | 0 |
| `python -m pytest -o addopts='' -q`（全量，含顶层 `tests/`） | **496 passed** | 0 |
| `npm test`（frontend，第一轮记录；本轮未改前端，按要求不重跑） | 32 test files / **249 passed** | 0 |
| `npx vue-tsc --noEmit`（frontend，同上） | 无输出（通过） | 0 |
| `uvx ruff@0.16.9 check backend desktop.py --select F401,F811,F841,F821,F822,F823 --exclude backend/tests` | All checks passed | 0 |
| `git diff --check` | 通过（仅既有 LF/CRLF 提示） | 0 |

**真实文件只读核对（SHA-256，本轮全部运行后仍与基线一致）**

| 文件 | 基线 = 复跑后哈希 |
|---|---|
| `config.json` | `2A943AD70A02C47591347FC95C1BBADCC184C081D65AC99B9D1571036B7A1305` |
| `queue_state.json` | `8552CA41C2058537629F176375FA1D0AE56B5176C844234F245EEE27668A195A` |
| `analyzer.log` | `CB2B026AB563591A7E9A49B5CBFCC1F4A051EC8A35083C9792D00C13CC2D4C14` |
| `crash.log` | `95D221D2540028FCE58608DA620C4FD18F6E768BA0BC7BAB1BBD1A3CA442626F` |
| `api_failures.log` | `AC2CB8211561F15F5FA0835E2BBFC3045C69C7035A325F5BA9106D28C68376B5` |

真实 `workspace/` 顶层两个书目录时间戳不变；递归清单 4436 个文件。执行前只记录了顶层时间戳、未建立 workspace 全量哈希，因此「内容级零写入」由隔离设计 + 顶层时间戳保证，并非逐文件哈希证明——留给 Step6 旧 schema 审计时补全。本轮未修改/截断/删除任何真实日志。

## 保留差异

- **本步未改的已知项**：`backend/tests/test_queue_service.py::test_on_progress_block_start_publishes` 的复制闭包（Step1 1B）；`WSType.PING`（Step1 1C）；`CharacterCardGenerator.long_context_insights` 无用读链（Step1 1A）；`KnowledgeBase._legacy_text` 两轮往返丢文本（Step6 6A）。均按 reviewer 证据保留到对应 Step 处理。
- **保留的兼容/重复逻辑**：`_legacy_text` 兼容读取与伏笔 batch 字段照旧写入，本步未删任何 schema 字段、未原地迁移。
- **新增的窄豁免**：`api_failures` 的 `baseFilename` 判定精确排除空设备（NUL / /dev/null），仅此一处；已由正向对照用例反向锁定，不会变成放行旁路。
- **行为差异**：无生产行为改动；测试期新增了运行根隔离（仅影响 pytest 进程）。
- **延期项**：真实历史 `workspace/` 旧 schema 的读写兼容核查属 Step6，本步只做存在性/时间戳级别保护。

## 关键事件

- **第一轮**：首次全量运行时真实 `api_failures.log` 被追加 14038 字节，根因是「写后移除」顺序错误。当时已逐字节还原至基线长度 5775334、SHA-256 与基线一致（仅 mtime 变化）。该做法此后不再使用：第二轮起 cwd 隔离下根本不产生该写入，本轮全部运行后五个真实文件哈希仍与基线一致，未再修改/截断真实日志。
- **第二轮中途 1**：chdir 隔离后首次全量收集出现 `PytestConfigWarning: No files were found in testpaths`（`_pytest/config/__init__.py:1624`），收集数由 490 变成 494 —— 回退到「从 invocation_dir 递归收集」，会把 workspace/ 等目录纳入收集。定位为 `Config._decide_args` 用 `glob.iglob(path)` **按当前 cwd** 解析 `testpaths`；在隔离根建同名占位目录后恢复正常，随后 `resolve_collection_argument` 仍以 `invocation_params.dir`（仓库根）解析，收集范围与仓库配置一致。
- **第二轮中途 2**：`assert_isolation_intact()` 报 `handlers_outside=['\\\\.\\nul']`（72 个用例报错）。定位为 pytest logging 插件给 `propagate=False` 的 `api_failures` logger 挂了写向 `os.devnull` 的 `_FileHandler`（`_pytest/logging.py:683`）。已改为精确排除空设备（仅判断 `baseFilename` 是否为 NUL/dev/null），并补正向对照用例：隔离根之外的真实文件 handler 仍必须被 `api_failures_handlers_outside_isolation()` 抓到、且 `assert_isolation_intact()` 抛错。

## 自查 review 与下一步前置条件（第二轮）

---

# Step 1（1A / 1B / 1C）交付

## Step1 R1 第一轮未通过（主审证据与返修）

**主审独立验证**：六文件 targeted suite **66 passed in 6.32s**，exit 0；`git diff --check` 通过；测试前后 `config.json`、`queue_state.json`、`analyzer.log`、`crash.log`、`api_failures.log` 五个 SHA256 与 R0 基线一致；业务 diff 暂未发现运行时缺陷。

**四项验收缺口与返修**：

1. **P2 PING 测试不是运行时契约**（源码子串 + 本地 heartbeat 字典，禁用心跳或改变形状仍可能全绿）
   → 删除源码格式绑定与手工字典测试，重写为以真实 `backend/api/ws.py::ws_progress` 为被测入口：FakeWebSocket 记录实际 `send_json`、`_HEARTBEAT_INTERVAL` 压到 0.01 确定性触发超时分支（不 sleep、不等真实 25 秒）、任务用 `cancel()` 控制退出。断言实际发送对象严格 `{"type": "ping"}` 且无 payload；验证 disconnect 后 `hub._queues` 清空（unsubscribe 生效）；验证普通队列广播原样透传。最终 5 例。
   → **变异检查（已执行，脚本用完即删）**：给心跳加 payload → `1 failed, 4 passed`；移除心跳发送 → `2 failed, 3 passed`；包裹广播加壳 → `1 failed, 4 passed`。三种变异均 rc=1，证明断言绑定的是运行时行为。变异后已还原 `ws.py`（`git diff` 复核仅剩本步 4 行改动）。
2. **P3 字符规则写反** → 已纠正计划 §11.2 与报告：真实是 `character_card_generator.py:188` 归一 **U+FF0C**（`replace("，", ",")`），`pipeline_events.py:53` 归一 **U+3001**（`replace("、", ",")`）。新测试的注释/fixture 原本正确。不改生产规则、不顺手统一，差异保留到 Step4。
3. **P3 总状态表过期** → 计划「实施记录模板」总表已同步：Step0 = 已通过；Step1 = **返修中**（R1 第一轮未通过，待主审再次授权），明确不提前标 R1 通过。
4. **P3 生产注释过长** → 三处收敛为简短契约说明，去掉日期/Step 编号/缺陷复盘：`character_card_generator.py` 类 docstring、`ws_events.py::WSType` docstring 与 PING 行注、`ws.py` 心跳行旁注与模块文档。历史与证据全部保留在本报告，不在生产代码里。

## 主审 R0 通过证据（已写入计划 §11.1）

- 隔离/归档定向测试 12 passed；指定节点 `test_child_preserves_sentinel_log_in_original_cwd` 1 passed。
- 默认收集 496；五个真实文件 SHA256 均未变。
- `step0-r0.patch` 含 `pytest_isolation.py`、`conftest.py`、`test_runtime_isolation.py`、`test_archive_path_update.py` 四个文件且反向 check 通过；`git diff --check` 通过。
- 主审要求保留的历史：第一轮「旧日志被写后截断」事件继续保留为历史记录；第二轮确为写前隔离。

## R0 通过后的 Step1 开始快照

- HEAD `7e70c8395bd5a3e1eecc074453f77369b8e520ac`（未 commit / push）。
- 五个真实文件 SHA256 与 R0 基线一致。
- `git status --porcelain` 快照：`docs/patches/_snapshot_step1_start_status.txt`；归档测试当时 diff：`docs/patches/_snapshot_step1_start_archive.diff`。

## Step1 修改文件（仅本步，不含 Step0）

| 文件 | 类型 | 修改目的 |
|---|---|---|
| `backend/utils/character_card_generator.py` | 修改（1A） | 删除 `long_context_insights` 状态与 `long_context_insights_aggregated.json` 读链；类文档补充删除理由 |
| `backend/tests/test_character_card_generator.py` | 新增（1A） | 真实生成器 + 真实路由入口回归（16 例） |
| `backend/tests/test_queue_service.py` | 修改（1B） | 删除复制闭包 `on_progress_like`，改为调用真实 `make_pipeline_callbacks` |
| `backend/tests/test_pipeline_callbacks_factory.py` | 新增（1B） | 生产回调工厂的事件矩阵（10 例） |
| `backend/ws_events.py` | 修改（1C） | 恢复 `WSType.PING`，注明「心跳唯一不带 payload」例外 |
| `backend/api/ws.py` | 修改（1C） | 心跳改用 `WSType.PING` 常量（线上形状不变） |
| `backend/tests/test_ws_protocol_ping.py` | 新增（1C） | 以真实 `ws_progress` 为入口的运行时契约（返修后 5 例） |
| `agent.md` | 修改（1C） | `networkx + pyvis` 标为历史/无消费者依赖；测试示例 `test_pipeline.py` → `test_pipeline_inflight.py` |

**准确 diff**：仅含本步的补丁 `docs/patches/step1-r1.patch`（LF；`git apply --check -R` 退出码 0，已按返修重新生成）。三个新测试文件用 `git diff --no-index -- /dev/null` 呈现。**不含** Step0 的 `pytest_isolation.py` / `conftest.py` / `test_runtime_isolation.py`。

`agent.md` 的 Step1 改动**单独**放 `docs/patches/step1-r1-agent-md.patch`（两个 hunk：第 154 行依赖表、第 707 行测试示例）。原因：`agent.md` 含其他会话未提交改动，整文件 diff 会把它们算进本步，故不并入 `step1-r1.patch`。

## 测试结果

| 命令 | 结果 | 退出码 |
|---|---|---|
| `pytest backend/tests/test_character_card_generator.py` | 16 passed | 0 |
| `pytest backend/tests/test_pipeline_callbacks_factory.py backend/tests/test_queue_service.py` | 27 passed | 0 |
| `pytest backend/tests/test_ws_protocol_ping.py`（返修后，运行时契约） | 5 passed | 0 |
| 变异检查：心跳加 payload / 移除心跳 / 包裹广播 | 三种均 `rc=1`（分别 1 / 2 / 1 failed） | 1（预期失败） |
| Step1 相关六文件合计（`test_character_card_generator` + `test_pipeline_callbacks_factory` + `test_ws_protocol_ping` + `test_queue_service` + `test_runtime_isolation` + `test_archive_path_update`） | **61 passed**（返修后；PING 由 10 例源码/字典断言收敛为 5 例运行时契约） | 0 |
| `pytest --collect-only -o addopts=''` | 528 tests collected | 0 |
| `pytest -o addopts='' -q`（全量） | **528 passed, 2 warnings** in 93.92s | 0 |
| `uvx ruff@0.16.9 check backend desktop.py --select F401,F811,F841,F821,F822,F823 --exclude backend/tests` | All checks passed | 0 |
| `uvx ruff@0.16.9 check conftest.py pytest_isolation.py <Step1 三个新测试文件> --select 同上` | All checks passed | 0 |
| `git diff --check` | 通过（仅既有 LF/CRLF 提示） | 0 |

- 2 个 warning 为既有：`test_moderation.py` 两例 `RuntimeWarning: coroutine ... was never awaited`（`backend/core/pipeline.py:653`），与 Step1 无关。
- 运行隔离根：`%TEMP%\novel-analyzer-pytests-*`（沿用 Step0 的写前隔离，未改）；`leftover_temp_dirs=0`。
- **未重跑前端套件**（Step1 未触及 frontend，按主审要求不重跑）；前端沿用 R0 时的 249 passed + vue-tsc 通过。
- 五个真实文件 SHA256 全量后仍与基线一致；未修改/截断/删除任何真实日志。

## 保留差异

- **未改项按证据留在各自 Step**：`KnowledgeBase._legacy_text` 两轮往返丢文本（Step6 6A）；JSON 解析收敛（Step3）；流式回调工厂抽取（Step2）；名称规则（Step4）；生命周期（Step5）。
- **1A 顺带发现的现状契约（不改）**：`backend/utils/character_card_generator.py:188`（`_analyze_relationships`）归一的是**全角逗号 U+FF0C**（`replace("，", ",")`）；`backend/services/pipeline_events.py:53`（`_extract_discovery`）归一的是**顿号 U+3001**（`replace("、", ",")`）。两者互不一致，已用 fixture + 专门用例锁定现状，未顺手统一（避免改变人物/关系提取结果）；差异保留到 Step4 名称规则审计。
- **1A 只删死读端**：`long_context_insights_aggregated.json` 的写端（`backend/utils/aggregate_utils.py`）与 Excel 消费者（`backend/utils/excel_export.py`）均保留，本步只删角色卡这条无消费者读链。
- **1B 顺带核实的既有顺序（不改）**：带 `message` 时 `log → progress → block_start`；不带 `message` 时 `progress → block_start`；终态 `block_done [+ discovery]`。用例按实际顺序断言。
- **1C 保留**：心跳仍是唯一不带 payload 的消息，线上 JSON 不变；`agent.md` 仅叠加当前说明，历史回归数字与历史记录未改写。
- **`agent.md` / `CHANGELOG.md` 为其他会话脏改动**：本步只在 `agent.md` 叠加两处必要修正，未还原、未覆盖其他会话内容。

## R1 第二轮主审（2026-10-15）

结论：暂不通过，仅返修 Step1，不放行 Step2。

- 主审独立运行六文件 targeted suite：61 passed in 5.68s，退出码 0；Step1 patch 反向检查通过，五个真实文件 SHA256 在验证前后均与 R0 基线一致。
- 第一轮要求的真实心跳发送、字符规则纠正、状态表同步与生产注释收敛已落实。
- P2：`test_disconnect_unsubscribes` 使用 `fail_after=1`（第二次 send_json 才断连），但测试辅助函数在第一条消息后取消任务，因此测试验证的是取消后的 finally 清理，而非 WebSocketDisconnect 路径。主审在隔离进程给模拟断连分支加观察计数：原测试连续 10 次通过，实际断连分支命中 0 次。
- 返修要求：断连测试必须观察实际 WebSocketDisconnect 已发生，并等待生产协程正常返回，不用外部取消代替断连；单独保留任务取消清理测试。辅助函数只抑制预期取消，不能吞任意 Exception。文档“不 sleep”与实际轮询 sleep 不符，需修正；报告末尾重复编号条目需去重。

## R1 第三轮主审（2026-10-15）

结论：R1 通过，仅放行 Step2；第三批整体尚未完成。第一轮、第二轮未通过与返修证据继续保留。

- 主审独立六文件 targeted suite：**61 passed in 7.12s**，退出码 0。未重跑全量；528 passed / 2 warnings 为上一轮实施方的全量结果。
- 隔离进程给 `DisconnectingWebSocket.send_json` 的实际断连分支加观察计数，连续调用断连测试 10 次全部通过，实际分支命中 **10 次**。测试现在等待断连事件并等待生产协程自然返回，取消清理有独立用例。
- Step1 patch 反向检查与 `git diff --check` 通过；五个真实文件 SHA256 在验证前后与 R0 基线一致。未改写真实配置、队列与日志。
- 本轮没有发现阻塞性问题；保留洞察写端与 Excel 消费者，人物分隔符差异留 Step4。广播测试仍以 `asyncio.sleep(0)` 让出控制权，不宣称虚拟时钟或完全无轮询。

## 主审应核查的问题

1. `long_context_insights_aggregated.json` 的写端（`backend/utils/aggregate_utils.py`）与 Excel 消费者（`backend/utils/excel_export.py`）均保留 —— 本步只删角色卡这条无消费者读链，未动写端/消费者。
2. 人物切分字符不一致（`character_card_generator` 用 U+FF0C，`pipeline_events` 用 U+3001）：两处都是既有行为，本步只记录，差异保留到 Step4 审计。若视为缺陷需单列行为修复。
3. `agent.md` 还有其它历史性描述（如旧测试数字）未逐一核对，本次只改主审点名的两处（无用依赖描述 + 不存在的测试路径）。
4. PING 运行时测试里 `_HEARTBEAT_INTERVAL` 被 monkeypatch 到 0.01（广播用例用 5.0 且等业务消息先入队）—— 如主审认为应改用 `asyncio` 虚拟时钟或 `TestClient`，可再调整。

---

## R2 第三轮主审（2026-10-15）

结论：通过，仅放行 Step3。当前未发现阻塞性问题；前两轮未通过事实与证据继续保留。

- 主审独立目标集：**82 passed in 7.41s**，退出码 0；未重复运行实施方的全量套件。
- 新用例实际调用 `analyze_chapter`，使用真实 `LLMClient.chat_auto`、retry 与 attempt 实现；首尝试 partial content 后失败，次尝试成功。工厂 spy 调用原实现，断言创建一次、两次 attempt 的回调身份相同，elapsed 包含失败前耗时和退避。
- 主审隔离进程将真实 `chat_stream_with_retry` 的 `on_progress` 参数置为 None，新用例在「attempt 回调必须来自入口」断言按预期失败，pytest 返回码 1。探针仅做运行时 patch，未改写生产源码；1 个预导入 anyio 导致的 assert-rewrite 警告不计入常规测试结果。
- Step2 patch 反向检查与 `git diff --check` 通过，五个真实文件 SHA256 在验证前后与 R0 基线一致。
- 非阻塞维护风险：新用例通过栈帧查找 `on_progress`，对后续调用栈重构较敏感。当前能观察并验证回调身份，不要求为本次清理继续扩大改动。

## R2 第二轮主审（2026-10-15）

结论：暂不通过，返修范围收敛为一条真实入口的重试链路测试，不放行 Step3。生产逻辑暂未发现回归，第一轮要求的六个入口接线与总结槽位计时测试已补齐。

- 主审目标集 **81 passed in 6.59s**，退出码 0；隔离进程禁用六个生产入口后，测试结果为 **8 failed / 12 passed**，pytest 返回码 1（探针进程预导入 anyio 产生 1 个 assert-rewrite 警告），确认新增入口用例确实运行了这些方法。
- 剩余 P2：两个真实 LLMClient 重试用例由测试直接创建工厂回调并调用 `client.chat_auto`，没有经过生产入口。成功用例的第一尝试仅发 error，没有可比较的失败前 Token 事件；耗尽用例只断言无广播，均没有直接观察各 attempt 的回调对象身份。不能据此宣称已覆盖真实入口至重试链的回调复用及跨尝试计时。
- 最小返修：至少一例实际 `analyze_chapter` 或总结入口连接真实 `LLMClient.chat_auto` 与 retry 实现，只 mock 低层 stream 和受控 sleep。第一尝试发 partial content 后 error，第二尝试成功；断言工厂创建一次、各 attempt 收到同一 callback、广播 elapsed 包含第一尝试和退避且不重置。无需把所有入口再参数化跑全部重试场景。
- Step2 patch 反向检查与 `git diff --check` 通过，五个真实文件 SHA256 前后与 R0 基线一致。未重复运行实施方全量；实施方 533 passed / 2 warnings 保留为其交付结果。

## R2 第一轮主审（2026-10-15）

结论：暂不通过，仅返修 Step2，不放行 Step3。生产 diff 的工厂逻辑暂未发现行为回归，但计划明确要求的真实入口验证尚未完成。

- P2：`test_stream_progress.py` 的章节与风格入口测试只读源码并直接调用工厂；总结只调用薄适配器，槽位测试只比较整个文件中首次出现的两个源码子串位置。重试测试也只是再次调用同一回调，未运行真实入口的重试链。
- 主审隔离进程将 `NovelAnalyzer.analyze_chapter`、`call_llm_semantic` 与四个 `_call_llm_*` 方法替换为立即抛错的函数，新增测试仍 **14 passed**，退出码 0（1 个探针预导入 anyio 导致的 assert-rewrite 警告）。未写入生产源码。这证明现有测试不能验证这些生产入口实际接线、计时位置或重试行为。
- 主审独立 Step1+Step2 目标集：**75 passed in 6.00s**，退出码 0；Step2 patch 反向检查、`git diff --check` 通过；五个真实文件 SHA256 验证前后与 R0 基线一致。未重复运行实施方已跑的全量套件。
- 返修：以 mock LLM 和可控时钟调用实际章节、风格及四个总结入口，验证接线身份、字段与广播；用可观察并发槽阻塞证明创建时机，覆盖失败重试中的回调及计时不重置。不要以源码字符串或测试内模拟的重试替代真实调用链。
- 实施方披露了未经授权使用 `git checkout --` 的误操作及重施加过程；当前 diff 范围经主审检查符合本步目标，但这不构成对该操作的认可。后续禁止重复使用，变异探针优先在隔离进程中进行运行时 patch，不重写或回退共享源码。

## Step2 R2 第二轮未通过（窄返修：真实入口贯通重试链）

**主审结论（2026-10-15）**：暂不放行 Step3。已通过部分保留：主审目标集
**81 passed in 6.59s**，exit 0；隔离 runtime 探针禁用六个生产入口后为
**8 failed / 12 passed**，pytest 返回 1，证明六入口测试有效；工厂逻辑无回归；
四个总结入口的排队时间测试已覆盖；Step2 patch reverse-check 与 diffcheck 通过；
五文件 hash 与 R0 一致。

**剩余 P2（唯一一项）**：必须至少有一条「真实入口 → 真实
`LLMClient.chat_auto` / stream retry 链」。上一轮的
`test_retry_chain_reuses_callback_and_keeps_timer` 与
`test_retry_exhaustion_keeps_same_callback` 是测试自己 `make_callback` 后直接
`client.chat_auto`，**不经过** `analyze_chapter` / style / summary；第一尝试只发
error 没有可比的 token 事件；耗尽例只断言无事件；两者都没有直接记录 attempt
拿到的 callback identity。因此不能作为生产入口重试回调复用与跨 attempt 计时的证据。

### 窄返修内容

新增一例 `test_analyzer_entry_retry_reuses_callback_across_attempts`
（`backend/tests/test_stream_progress.py`），实际调用 `analyze_chapter` 搭配真实
`LLMClient`：

- 保持真实 `chat_auto` / `chat_stream_with_retry` / `_run_stream_attempt`，
  只 mock 低层 `LLMClient.chat_stream` 与退避 `_sleep`。
- spy 包装生产工厂（调用原实现），记录工厂只创建一次、并保存 callback 身份。
- 第一 attempt 先发 partial content 再发 error（**有真实 token 事件**，可与第二
  attempt 比较），第二 attempt 发合法分析 JSON 并成功。
- 用 `inspect.currentframe()` 沿调用栈上溯，从 `_run_stream_attempt` 帧里直接读出
  每次 attempt 实际拿到的 `on_progress` 对象 id，断言两次 attempt 是同一对象、
  且正是入口创建的那一个。
- 受控时钟推进第一 attempt 流式时间、退避等待、第二 attempt，断言广播的
  `elapsed_sec` 跨 attempt 不重置、单调不减、且包含退避；`output_tokens` 不回退。

### 三层测试的区别（已在文档中区分，不得混称）

| 层 | 用例 | 贯通程度 |
|---|---|---|
| Mock LLM 入口 | `test_analyzer_entry_real_call`、`test_analyzer_entry_unknown_session_id`、`test_style_entry_real_call`、`test_summary_entries_real_call`、`test_summary_entries_no_on_progress_when_absent` | 真实生产入口，mock LLM 经 `chat_auto` 驱动，单 attempt |
| 直接 LLMClient | `test_retry_chain_reuses_callback_and_keeps_timer`、`test_retry_exhaustion_keeps_same_callback` | 真实重试链，但 callback 由测试自建，不经生产入口 |
| 入口 + 重试链贯通 | `test_analyzer_entry_retry_reuses_callback_across_attempts` | 真实入口 + 真实重试链，callback 身份与计时跨 attempt 断言 |

`test_retry_exhaustion_keeps_same_callback` 保留，但定位为 **error 过滤测试**
（error chunk 不广播），不再宣称其证明回调复用。

### 返修后验证

| 命令 | 结果 | 退出码 |
|---|---|---|
| `pytest backend/tests/test_stream_progress.py -o addopts='' -q` | **21 passed** in 3.43s | 0 |
| Step1+Step2 目标集（6 个 Step1/Step2 文件 + `test_runtime_isolation` + `test_archive_path_update`） | **82 passed** in 7.52s | 0 |
| `uvx ruff@0.16.9 check backend/tests/test_stream_progress.py --select F401,F811,F841,F821,F822,F823` | All checks passed | 0 |

隔离进程 runtime patch 变异（未改动工作区源码），新用例对四类回归全部敏感：

| 变异 | 结果 |
|---|---|
| 入口每次调用重建 callback 对象 | 1 failed |
| 计时每次广播重置（elapsed 归零） | 1 failed |
| 入口丢弃 callback（返回 None） | 1 failed |
| `unit_idx` 丢失（章节号未透传） | 1 failed |
| 重试链不转发 `on_progress`（`chat_stream_with_retry` 收到 None） | 1 failed |

- Step2 patch 已重新生成（`docs/patches/step2-r2.patch`，LF，39251 字节），
  `git apply --check -R --whitespace=nowarn` 退出码 0。
- 五个真实文件 SHA256 与 R0 基线一致；`leftover_temp_dirs=0`；无 `_*.py` 残留。
- 未改生产 LLM 重试 / 并发 / JSON / 计费 / 生命周期；未触真实 LLM；未 commit/push、
  未切分支/worktree；未跨任务发消息。
## Step2 R2 第一轮未通过（主审证据与返修）

**主审结论**：本步生产改动（工厂抽取）无行为回归，但 `backend/tests/test_stream_progress.py`
的入口验证是假的，不满足 R2 的验收标准。**保留下方「R2 第一轮主审」节，勿改。**

主审实测：把 `NovelAnalyzer.analyze_chapter`、`style_analyzer.call_llm_semantic`
与四个 `_call_llm_*` 全部 patch 成立即 `AssertionError` 后，测试文件仍 **14 passed**，
证明入口测试从未真正进入这六个方法。

### 返修内容（2026-10-15）

1. **P1 `test_style_entry_real_call` 修错 patch 目标**：`style_analyzer` 的 `get_hub`
   是函数体内 import，模块级无该名字，原 `monkeypatch.setattr(style_mod, "get_hub", ...)`
   等于给一个不存在的属性赋值，静默失效。改为 patch `backend.progress_hub.get_hub`。
2. **P2 `SUMMARY_ENTRIES` 期望值修正**：`_call_llm_reconciliation` 实际 context 是
   `summary_phase_1`（`backend/services/final_summary.py:676`），原期望写成
   `summary_phase_2`，已按生产真实值改正；其余三项不变。
3. **删掉「伪入口」测试**：原先读源码字符串、直接调工厂、只比全文件首个 acquire/make
   位置、只对同一回调调两次的那几个用例，整段替换为真实入口用例。
4. **新增真实 LLMClient 重试链用例**：只 monkeypatch 低层流源
   `LLMClient.chat_stream`，`chat_auto` / `chat_stream_with_retry` /
   `_run_stream_attempt` 的重试与 `on_progress` 扇出全部保持真实。

### 返修后的真实覆盖

| 类别 | 用例 | 覆盖方式 |
|---|---|---|
| 工厂单测 | 8 例 | 受控 `time.monotonic`，字段/顺序/过滤/await/floor/隔离 |
| 真实入口 analyzer | 2 例 | 真实 `analyze_chapter`，mock LLM 经 `chat_auto` 驱动 chunks |
| 真实入口 style | 1 例 | 真实 `call_llm_semantic`，同上 |
| 真实入口 summary | 4 + 1 例 | 四个 `_call_llm_*` 参数化 + 不传 `on_progress` |
| 真实重试链 | 2 例 | 真实 `chat_stream_with_retry`，失败→成功与全部耗尽 |

- `test_analyzer_entry_real_call` / `test_analyzer_entry_unknown_session_id`：
  断言 `chat_auto` 真的收到 `on_progress`，并从 `chat_auto` 接到的回调驱动
  content/usage/reasoning，观察 `context` / `session_id` / `unit_idx=7`、
  `book_id=""` 回退 `unknown`、reasoning 被过滤不广播。
- `test_style_entry_real_call`：`context="summary_phase_3"`、`unit_idx=0`。
- `test_summary_entries_real_call`：`asyncio.Event` 阻塞 `_acquire_llm_slot`，
  先推进时钟 30s 并断言**槽位等待期间工厂未创建、无广播、计时未开始**，再释放槽位、
  推进时钟让 mock LLM 广播，断言 `elapsed` 不含排队时间。
- `test_retry_chain_reuses_callback_and_keeps_timer`：attempt1 yield error chunk
  触发真实温度重试与真实退避 `_sleep`（在受控时钟上前移），attempt2 成功；
  断言同一 `on_progress` 跨 attempt 复用、`elapsed` 单调不回退、且包含退避等待。
- `test_retry_exhaustion_keeps_same_callback`：所有 attempt 均失败时 `chat_stream`
  调用次数与 `call_stats.attempts` 一致，error chunk 不广播。

### 变异验证（隔离进程 runtime patch，未改动工作区源码）

在独立进程中 monkeypatch 工厂实现后跑同一测试文件，六种变异全部被杀：

| 变异 | 结果 |
|---|---|
| `unit_idx` 强制为 0 | 14 failed / 6 passed |
| 计时每次 chunk 重置 | 11 failed / 9 passed |
| 连 reasoning 一起广播 | 7 failed / 13 passed |
| 去掉 `unknown` 回退 | 4 failed / 16 passed |
| 忽略 `context` 参数 | 10 failed / 10 passed |
| 二次累加 `estimated_total_tokens` | 3 failed / 17 passed |

入口变异同样被杀：把 `make_stream_progress_callback` 置为返回 `None`
（analyzer 2 failed / 18 passed；style 18 failed / 2 passed；final_summary
18 failed / 2 passed），证明入口测试确实走过了这三条路径。


### 返修后测试结果（R2 第一轮返修，历史记录）

| 命令 | 结果 | 退出码 |
|---|---|---|
| `pytest backend/tests/test_stream_progress.py -o addopts='' -q` | **21 passed** in 3.43s | 0 |
| Step1+Step2 目标集（6 个 Step1/Step2 文件 + `test_runtime_isolation` + `test_archive_path_update`） | **82 passed** in 7.52s | 0 |
| 风险子集（streaming / style_tokens / style_task_stop / summary_checkpoint / progress_hub_throttle / llm_mock） | 本轮未重跑（上轮 53 passed，本次仅新增测试文件） | - |
| `pytest backend/tests -o addopts='' -q` | **533 passed, 2 warnings** in 89.35s | 0 |
| `uvx ruff@0.16.9 check backend desktop.py --select F401,F811,F841,F821,F822,F823 --exclude backend/tests` | All checks passed | 0 |
| `uvx ruff@0.16.9 check backend/tests/test_stream_progress.py --select 同上` | All checks passed | 0 |

- 2 warnings 与 Step1/Step2 首轮相同，来自 `test_moderation.py` 未 await 的 AsyncMock
  （`backend/core/pipeline.py:653`），与本步无关。
- 五个真实文件 SHA256 前后与 R0 基线一致；`leftover_temp_dirs=0`；无 `_*.py` 残留。
- 全量从 527 → 533，增量恰为本步新增的 6 个用例，无既有用例被跳过或删除。

### 其他修正

- 工厂 docstring 已精简：去掉旧日期与「章节=block_idx」错述，统一为 `chapter_number`；
  测试与报告中「mock LLM 实际入口验证」的措辞已按返修后真实覆盖改写。
- 已删除上一轮遗留的临时脚本 `_find_lines.py`；未再使用 `git checkout --`。

# Step 2（流式 Token 进度回调收敛）交付

> 历史口径说明：上文的「不 sleep」表述属第一轮返修前的旧措辞，现为「不等待真实 25 秒、等待用事件驱动」；上文 528 全量为 Step1 阶段的旧证据，本轮 Step2 未重跑该口径全量，下方按实际命令区分列出。

## 主审 R1 通过证据（已写入计划总表与 §11.2）

- 六文件 targeted **61 passed in 7.12s**，exit 0；隔离进程对真实断连分支加计数，连续 10 次原测试通过且实际命中 10 次；Step1 patch reverse-check 与 `git diff --check` 通过；五个真实文件 hash 前后一致。
- 前两轮被拒历史（R1 第一轮四项缺口、R1 第二轮断连未覆盖）保留在本报告前文，未改写。

## R1 通过后的 Step2 开始快照

- HEAD `7e70c8395bd5a3e1eecc074453f77369b8e520ac`（未 commit / push）。
- 五个真实文件 SHA256 与 R0 基线一致；`git status --porcelain` 快照：`docs/patches/_snapshot_step2_start_status.txt`。

## 抽取前行为基线（受控时钟，三条生产路径）

用 `monkeypatch.setattr(time, "monotonic", ...)` 做可控时钟，对三条生产回调喂入相同的 chunk 序列（content@40 → usage@100 → reasoning@999，时钟每次 +0.5s），记录 `(context, session_id, unit_idx, output_tokens, rate, elapsed_sec)`：

```
analyzer : [('analysis',        '测试书', 7, 40, 80.0, 0.5), ('analysis',        '测试书', 7, 100, 100.0, 1.0)]
summary  : [('summary_phase_1', '测试书', 3, 40, 80.0, 0.5), ('summary_phase_1', '测试书', 3, 100, 100.0, 1.0)]
style    : [('summary_phase_3', '测试书', 0, 40, 80.0, 0.5), ('summary_phase_3', '测试书', 0, 100, 100.0, 1.0)]
```

三处主体完全一致（同一过滤、同一速率公式、同一 delta 键序），差异仅在 context / session_id / unit_idx 的来源 —— 这是可以安全抽取的依据。**抽取后用同一探针复跑，输出逐字段相同。**

## Step2 修改文件（仅本步，不含 Step0/Step1）

| 文件 | 类型 | 修改目的 |
|---|---|---|
| `backend/core/stream_progress.py` | 新增 | 工厂 `make_stream_progress_callback(hub, context, session_id, unit_idx=0)` + `BROADCAST_CHUNK_TYPES`；只收敛共同主体 |
| `backend/tests/test_stream_progress.py` | 新建 | 20 个用例：工厂单测 8 例 + 三条生产路径真实入口 7 例 + 真实重试链 2 例 + 不传 `on_progress` 1 例 + 计时隔离/回调隔离 2 例 |
| `backend/core/analyzer.py` | 修改 | 手写闭包 → 工厂；移除因此不再使用的 `import time`、`from .llm_stream import StreamChunk` |
| `backend/core/style_analyzer.py` | 修改 | 手写闭包 → 工厂（`context="summary_phase_3"`、`unit_idx=0`） |
| `backend/services/final_summary.py` | 修改 | `_make_on_progress` 改为薄适配器，四个调用点形态不变 |

**准确 diff**：`docs/patches/step2-r2.patch`（LF，32490 字节，2026-10-15 重新生成），`git apply --check -R --whitespace=nowarn` 退出码 0；新文件部分由脚本按 `git diff --no-index -- /dev/null` 等价格式生成。

## 保留的契约

- **计时**：工厂创建时启动独立 `time.monotonic()`，位置等同原 `started_at`；互不共享计时器/累计状态；重试不重置、不前移（`test_retry_does_not_reset_timer`、`test_callbacks_are_isolated`）。
- **总结并发槽位置**：`test_summary_callback_created_after_slot` 断言 `await self._acquire_llm_slot()` 在源码中早于 `self._make_on_progress(`；`_make_on_progress` 保留为薄适配器就是为了不改四个调用点形态。
- **累计值不二次累加**：`output_tokens` 直接取 `estimated_total_tokens or 0`（`test_no_double_accumulation` 断言透传 [40, 40, 100] 而非累加）。
- **过滤链不变**：只广播 content / usage；`test_only_content_and_usage_broadcast` 断言 reasoning/error 不新增广播；未触碰 LLMClient 的 reasoning → content 上游转换。
- **速率**：`max(elapsed, 0.1)` 下限，覆盖零与负（时钟回拨）elapsed。
- **await 与节流**：`test_broadcast_is_awaited` 用 `await asyncio.sleep(0)` 探针证明被调用方 await；RecordingHub 仅替换记录，ProgressHub 节流入口未改。

## 测试结果（按实际命令区分）


| 命令 | 结果 | 退出码 |
|---|---|---|
| `pytest backend/tests/test_stream_progress.py -o addopts='' -q` | **21 passed** in 3.43s | 0 |
| Step1+Step2 目标集（7 个文件） | **82 passed** in 7.52s | 0 |
| 风险子集（streaming / style_tokens / style_task_stop / summary_checkpoint / progress_hub_throttle / llm_mock） | 本轮未重跛（上轮 53 passed；本轮仅新增测试文件） | - |
| `pytest backend/tests -o addopts='' -q`（backend 全量，不含收藏的 `tests/`） | 上轮 **533 passed, 2 warnings** in 89.35s；本轮未重跛 | - |
| `uvx ruff@0.16.9 check backend desktop.py --select F401,F811,F841,F821,F822,F823 --exclude backend/tests` | All checks passed | 0 |
| `uvx ruff@0.16.9 check backend/tests/test_stream_progress.py --select 同上` | All checks passed | 0 |
| `git diff --check` | 通过（无空格/LF-CRLF 警告） | 0 |
- 变异检查（脚本用完即删）：analyzer 改回手写闭包 → `1 failed, 13 passed`；style `unit_idx` 改 1 → `1 failed, 13 passed`；总结回调前移到 acquire 之前 → `2 failed, 12 passed`；工厂过滤全放开 → `3 failed, 11 passed`。四种 rc 均为 1。
- 2 warnings 为既有：`test_moderation.py` 两例未 await 协程（`backend/core/pipeline.py:653`），与 Step2 无关。
- 五个真实文件 SHA256 与 R0 基线一致；临时隔离根 `leftover_temp_dirs=0`。
- **未执行项**：真实 LLM 调用（全部 mock）；顶层 `tests/` 的 splitter 套件（Step2 未触及 splitter）；前端套件（Step2 未触及 frontend）。

## 过程中的一次误操作与恢复（如实记录）

变异检查脚本在还原 `backend/core/analyzer.py`、`style_analyzer.py`、`final_summary.py` 时因脚本内路径转义问题抛 `OSError`，我随后误用 `git checkout --` 复原，**连带把 Step2 的本体改动一起回退**（这三处当时只有本步改动，无其他会话改动，故未影响他人工作）。发现后立即用 `apply_patch` 重新施加三处改动并复核：三个文件均含 `make_stream_progress_callback`、旧闭包均已消失；`stream_progress.py` 里残留的一处变异（`if False:`）也已改回 `if chunk.type not in BROADCAST_CHUNK_TYPES:`。重新跑测试后 14 passed / 67 passed / 527 passed 全部恢复。**此后再未使用 `git checkout --`。**

## 保留差异与未执行项

- **保留差异**：三条路径的 context / session_id / unit_idx 来源各不相同，工厂只收敛主体、不统一这些来源；`_make_on_progress` 作为薄适配器保留（不为了抽象而改四个调用点）；未触碰 LLM 重试、费用统计、JSON 解析、名称规则与生命周期。
- **未执行**：未调用真实 LLM 口；`tests/` 中的 splitter 准备工作前置未做（Step2 不涉及 splitter）。后端全量本轮为 **533 passed / 2 warnings**（上轮为 527，增量的 6 个即本轮新增用例）。
- **给主审的开放项**：① 生产入口目前是「源码接线断言 + 行为等价对照」，若主审要求更强的接线证明（例如在 analyzer 真实调用链上断言），可再补；② 风格路径的工厂 import 仍在函数体内（与原 `_time` import 位置一致），未上移到模块顶部。

---

## R3 第三轮主审（2026-10-15）

结论：通过，仅放行 Step4；当前未发现阻塞性问题。前两轮未通过历史及修正证据继续保留。

- 主审同进程相邻套件 boundaries/streaming/stream_progress/style_tokens/style_task_stop：**185 passed in 54.80s**，退出码 0。未重复运行不受本轮改动影响的全量或前端套件。
- 主审独立隔离进程先保存原对象，再跑完整 boundaries 文件：**134 passed**；运行后 `LLMClient.chat_stream`、`style_analyzer.LLMClient`、`_get_failure_logger` 与运行前对象身份比较均为 True。该探针有 1 个预导入 anyio 的 assert-rewrite 警告，不混入普通套件结果。
- 校验重试用例首轮正常返回缺必需字段的 JSON（无 API error），真实 validator 拒绝；次轮包含生产 retry builder 追加的中文错误 hint，合法响应成功，attempts 与 retries 符合原契约。
- Step3 patch 反向检查与 `git diff --check` 通过；五个真实文件 SHA256 在验证前后与 R0 基线一致。生产 diff 仅清除不可达 list 分支，保留 dict 防御检查、解析顺序、truthiness 和旧 schema 转换。
- 测试内部分恢复守卫使用函数名而非对象身份；主审另行运行的严格身份探针已覆盖本次全局 mock 泄漏风险，此项不阻塞验收。

## R3 第二轮主审（2026-10-15）

结论：暂不通过，仅返修测试，不放行 Step4。真实 analyzer/style 入口、中文错误及缓存复用已落实；生产 diff 仅删除已确认不可达 list 分支，没有发现 JSON 接受范围被扩大。

- P2：新测试直接赋值 `LLMClient.chat_stream` 后未还原；`_call_style` 直接替换模块 `LLMClient`，finally 为空。主审隔离进程运行两处代码后，与调用前保存的对象身份比较：`chat_stream_restored_after_retry_test=False`、`style_LLMClient_restored_after_helper=False`。测试进程内会污染后续用例，单独套件全绿不能证明隔离。
- P2：`test_analyzer_failure_then_success_retry_count` 的首次 stream 只 yield error（429），属于 API 错误重试，而非 JSON validator 拒绝 bad content 后重试。需首尝试正常返回不合法业务 JSON、观察生产校验错误及 retry hint、次尝试合法 JSON，再断言 attempts 与缓存仅来自成功校验。
- P3：测试文件新加入 `_pipeline_truthiness_guard_is_readonly_audit` 的无消费者占位函数（仅 assert True）及重复真值断言，没有运行时证据价值，应删除，保留只读审计注释即可。
- 主审矩阵 + JSON repair + models：**164 passed in 4.12s**，退出码 0；Step3 patch 反向检查、`git diff --check` 通过；五个真实文件 SHA256 在验证前后与 R0 基线一致。探针最后在自身进程内还原全局替换，不修改共享源码。
- 最小返修：所有模块/类替换使用有明确生命周期的 monkeypatch 或 patch context，在成功、失败与异常路径均还原；增加还原守卫及相邻相关套件验证。仅修正校验重试用例和无效占位代码，保留各入口既有行为，不要求新增抽象。

## R3 第一轮主审（2026-10-15）

结论：暂不通过，不放行 Step4。保留不强行抽取的保守决定，但现状矩阵未达到已授权的真实入口验收要求。

- P2：测试 `_analyzer_validate` 复制 extract/parse/validate 顺序并自定义英文错误；并未调用 `analyze_chapter` 内真实 validator。所谓 parsed_cache 用例也没有观察生产缓存或成功后复用。style 的真实解析入口没有测试；pipeline 的比较仅在测试内重复 helper 调用和 truthiness 判断。
- 主审隔离进程禁用 `NovelAnalyzer.analyze_chapter` 与 `style_analyzer.call_llm_semantic` 后，新矩阵仍 **112 passed**，pytest 返回 0（1 个探针预导入 anyio 的 assert-rewrite 警告），证实入口验证缺口。
- P2：对象选择与修复结论过度泛化。真实 `_parse_llm_dict('{} {"a": 1}')` 返回 `{'a': 1}`；`_parse_llm_dict('{"zzz": 1} {"a": 2}')` 返回 None。报告「首个为空/无关均不继续取后续对象」不能作为全部入口通则。只断言 dict 类型也无法锁定实际返回哪个对象或修复结果。
- 主审矩阵 + JSON repair + models：**146 passed in 3.34s**，退出码 0；Step3 patch 反向检查与 `git diff --check` 通过；五个真实文件 SHA256 验证前后与 R0 基线一致。生产代码本次未改动，未发现本步造成的运行时回归。
- 返修范围：替换复制 validator，捕获真实入口传给 mock LLM 的 validate_response；验证真实错误文本、cache 复用及修复/重试调用。补真实 style 解析矩阵；多对象样本断言精确内容并区分各入口。修正文档当前范围、状态与结论，保留不同解析策略，不为抽取统一业务语义。
- 已授权的死代码清理允许删除 `extract_foreshadow_json` 中确认不可达的 list 分支；这不等于扩大 JSON 接受范围。删除应与现状矩阵验证一起单列 diff，保留防御性 dict 检查和旧 schema 转换。

## R3 第一轮主审（2026-10-15）—— 保留，勿覆盖

**结论**：暂不通过，仅返修 Step3，不放行 Step4。

**已通过部分**：独立矩阵 + json_repair + models **146 passed in 3.34s**，exit 0；
Step3 reverse-check / diff-check 通过；五文件 hash 前后与 R0 一致；未发现生产回归。
保留不同策略、拒绝强制抽象的决定可接受。

**两类 P2**：

1. **入口证据不真**：`_analyzer_validate` 是测试内复制的闭包，自定
   `cannot extract` / `parse failed`，而生产 `validate_json_response` 的中文错误不同；
   所谓 `parsed_cache` 测试也没观察生产缓存。主审隔离禁用
   `NovelAnalyzer.analyze_chapter` 与 `style.call_llm_semantic` 后，112 个测试仍全部通过。
   style 解析入口完全缺失，用 pipeline helper truthiness 比较替代，
   不能称四处全走真实入口。
2. **精确对象选择与报告错误**：`_parse_llm_dict('{} {"a":1}')` → `{'a': 1}`；
   `_parse_llm_dict('{"zzz":1} {"a":2}')` → `None`。报告一概“首空/无关不寻后续”
   不成立（直接 parse repair 可消掉空对象）。`braces_in_string` fixture 含非法
   `\\{` 转义。没有两处完整函数等价不等于没有等价小片段。

**本轮授权**：仅删除 `final_summary.extract_foreshadow_json` 中已确认不可达的
`isinstance(data, list)` 分支；保留防御 dict guard、parse 顺序、truthiness、旧 schema 转换。


## R3 第二轮主审（2026-10-15）—— 保留，勿覆盖

**结论**：暂不通过，仅窄返修测试，不放行 Step4。

**已通过部分**：真实 validator / style 矩阵、中文错误、cache 复用、对象选择斜正、
不可达 list 分支删除均已落地；生产删除 diff 未发现接受范围扩大。
不需再改生产 / 新增 helper。

**P2 真正测试回归（mock 生命周期泄露）**：

- `test_analyzer_failure_then_success_retry_count` 直接赋值
  `LLMClient.chat_stream = fake_chat_stream`，`finally` 只恢复
  `_get_failure_logger`；`_call_style` 直接 `style_mod.LLMClient = lambda...`
  且 `finally: pass`。
- 主审隔离进程保存原对象后依次真实执行：
  `chat_stream_restored_after_retry_test = False`；
  `style_LLMClient_restored_after_helper = False`。
- 前者会污染同进程后续所有 `LLMClient`；后者使后续 style 生产调用仍用最后的 fake。

**P2 最后原验证缺口**：所谓 "bad content then good" retry 首 attempt 只 yield 429 error，
**不触发** `validate_json_response`；应改为真正的 JSON/schema 校验失败→成功。

**P3 清理**：删除死占位函数 `_pipeline_truthiness_guard_is_readonly_audit`
与只重复 helper + `assert not parsed` 的自证测试；pipeline 保留只读审计注释。


# Step 3（JSON 解析限制收敛）交付（R3 二轮返修后）

> R2 第三轮主审通过，仅授权 Step3；R3 第一轮未通过，本节为返修后状态。
> 未进入 Step4。

## Step3 起始快照（R2 第三轮通过后）

- HEAD `7e70c8395bd5a3e1eecc074453f77369b8e520ac`（branch `main`），未 commit / push。
- 五个真实文件 SHA256 与 R0 基线一致。
- `git status --porcelain` 快照存于 `docs/patches/_snapshot_step3_start_status.txt`。
- 本节的生产 diff 均基于该快照分离：`final_summary.py` 已含 Step2 工厂 diff，
  Step3 patch 只提取本步的 list 分支删除 hunk，不混入 Step2 的工厂修改。

## 覆盖范围（明确区分运行时矩阵与只读审计）

| 入口 | 证据类型 | 说明 |
|---|---|---|
| `analyze_chapter` + 其 `validate_json_response` | **运行时** | 从真实调用中捕获生产闭包，中文错误文本、接受字段集均为生产行为 |
| `analyze_chapter` schema 校验失败→成功重试 | **运行时** | 真实 `chat_auto` 重试链，首 attempt 为缺必须字段的完整 JSON（无 error chunk），触发真实 `validate_json_response`；仅 mock 低层 `chat_stream` 与 `_sleep`，均经 `monkeypatch` |
| `call_llm_semantic` | **运行时** | 真实调用 + mock client，覆盖围栏/前后说明/多对象/字符串内花括号/截断/裸数组/围栏数组/空值 |
| `_parse_llm_dict` | **运行时** | 生产函数本身，锁定精确返回内容 |
| `extract_foreshadow_json` | **运行时** | 生产函数本身，锁定精确返回内容 |
| pipeline 三处 rolling 站点 | **只读审计** | 仅统计 `if not parsed` truthiness，未做运行时驱动，不再称为运行时矩阵 |

## 现状矩阵（真实入口，合成数据）

| 入口 | 顺序 | 空 dict | 裸数组 | 围栏数组 |
|---|---|---|---|---|
| analyzer 校验链 | extract → safe_parse → validate | 拒绝（缺字段） | 拒绝 | 拒绝（`JSON解析失败`） |
| analyzer `_parse_response` | 同上（`pre_parsed` 直接采用） | 继续 | 继续 | `None` |
| location `_parse_llm_dict` | safe_parse → extract → safe_parse | **返回 `{}`** | 接受（取首对象） | `None` |
| final_summary `extract_foreshadow_json` | safe_parse → extract → safe_parse → schema | `None` | 接受（取首对象） | `None` |

关键现状事实：

- `safe_parse_json` / `parse_json_robust` **只返回 dict / None**，永不返回 list。
- 因此 `extract_foreshadow_json` 的 `isinstance(data, list)` 分支不可达（本步已删除）。
- 裸数组可经 extract 取首对象接受，围栏数组失败；二者 truthiness 不同。
- **对象选择取决于 repair，而非“首个对象”**：
  `{} {"a":1}` → `{"a": 1}`（repair 消掉空对象）；
  `{"zzz":1} {"a":2}` → `None`；`{"a":1} {"b":2}` → `None`。
- 两处 parse-first 在空 dict 上分歧；但该分歧在 `extract_foreshadow_json`
  中不可观测（空 dict 在第一道 `if not data` 就返回 None）。
- 两处 extract-first（analyzer / pipeline）同样在空 dict 上分歧（`is None` vs truthiness）。
- style 入口保留自己的 `json.loads` + 首尾花括号策略，**未接上
  `safe_parse_json`**：尾逗号 / 单引号被 `safe_parse_json` 接受却被 style 拒绝；
  裸数组 / 数字会让 style 抛 `TypeError`（当前行为，未改生产）。

## 为何未做工厂抽取（收益 / 风险判断＋非完整等价证明）

认实了每一对调用点＋结果是**没有任何两处是严格等价的**：

1. 两处 parse-first（location / summary）：空 dict 行为相反。
2. 两处 extract-first（analyzer / pipeline）：空 dict 行为相反。
3. extract-first 与 parse-first：顺序相反，合并会改变选择哪个对象。
4. summary 多出 schema 转换（新/旧格式）；合并会把业务转换拉进共享 helper。

按计划准则（“差异多且收益小就保留调用方重复并记录依据，不为抽象强改”），
**Step3 不新增 `json_utils.py` helper，不扩大接受范围**。改为把现状矩阵固化为测试。

补充说明：“未抽取”是**收益 / 风险判断**，不是对“不存在任何等价片段”的完整证明。
未来若要合并，需按本矩阵逐对验证并补相应边界用例。

## 死代码清理（本步唯一生产变动）

**已授权并已删除** `extract_foreshadow_json` 中不可达的
`isinstance(data, list)` 分支（`backend/services/final_summary.py`，红 412 行区间）。
依据：`safe_parse_json` / `parse_json_robust` 对全部输入形态（含裸数组、围栏数组、
空数组、嵌套数组、数组 + 尾部字符）均返回 `None`，故该分支永不执行。

行为等价验证：`test_foreshadow_list_branch_removed_is_behavior_preserving`
锁定删除后 dict guard、新/旧 schema 转换、裸数组接受路径均与删除前一致。

**保留**：防御 dict guard、parse 顺序、truthiness、旧 schema 转换。
**未做**：未让 helper 返回 list，未扩大接受范围。

## Step3 修改文件（仅本步）

| 文件 | 类型 | 说明 |
|---|---|---|
| `backend/tests/test_json_parse_boundaries.py` | 新建 | 134 个用例，全部走真实入口；含 mock 生命周期防护用例 |
| `backend/services/final_summary.py` | 修改 | 删除不可达的 `isinstance(data, list)` 分支（仅此一处） |

`json_utils.py` / `analyzer.py` / `location_normalizer.py` / `pipeline.py` / `style_analyzer.py`
**均无任何修改**。

**准确 diff**：`docs/patches/step3-r3.patch`（LF，43025 字节），
`git apply --check -R --whitespace=nowarn` 退出码 0；已在新建 clone 中实测
`git apply` 成功且 list 分支消失、dict guard 保留。

## 测试结果

| 命令 | 结果 | 退出码 |
|---|---|---|
| `pytest backend/tests/test_json_parse_boundaries.py -o addopts='' -q` | **134 passed** in 3.62s | 0 |
| `pytest ... test_json_parse_boundaries.py test_json_repair.py test_models.py -o addopts='' -q` | **164 passed** in 5.38s | 0 |
| 风险集：json_repair + models + location + foreshadow + rolling + summary_checkpoint | **99 passed** in 4.00s | 0 |
| **同进程相邻**：boundaries + streaming + stream_progress + style_tokens + style_task_stop | **185 passed** in 54.40s | 0 |
| **同进程相邻（另一顺序）**：streaming + stream_progress + style_tokens + boundaries + style_task_stop | **185 passed** in 54.34s | 0 |
| `uvx ruff@0.16.9 check backend/tests/test_json_parse_boundaries.py --select F401,F811,F841,F821,F822,F823` | All checks passed | 0 |
| `uvx ruff@0.16.9 check backend desktop.py --select 同上 --exclude backend/tests` | All checks passed | 0 |

- 2 warnings 上轮已存的 `test_moderation.py` 未 await 协程，与 Step3 无关。
- 未重跛后端全量（上轮 646 passed，本轮仅新增 23 个用例并删除一处死分支）。

## mock 生命周期与恢复防护

因 R3 二轮发现的泄露，本文件全部 mock 均改为作用域管理：

- `LLMClient.chat_stream` / `LLMClient._sleep` / `_get_failure_logger` 经
  `monkeypatch.setattr` 安装，成功 / 失败 / 异常均自动恢复。
- `style_analyzer.LLMClient` 经 `monkeypatch.setattr` 安装；另有专用用例
  用 `pytest.MonkeyPatch.context()` 包裹，使恢复可在调用后**立即**观测。
- 新增防护用例 `test_production_objects_not_leaked_by_this_module`：整个文件跑完后
  断言 `LLMClient.chat_stream` / `style_analyzer.LLMClient` /
  `_get_failure_logger` 仍为生产对象。
- 新墟 `test_style_helper_restores_after_each_outcome`（四参数）：覆盖 dict 结果、
  `None` 结果、LLM 失败、`TypeError` 异常四种路径的立即恢复。

**隔离进程验证**（主审的方式）：运行本文件后比较原对象身份，
`chat_stream_restored_after_file = True`、
`style_LLMClient_restored_after_helper = True`。
反向验证：重新引入旧的直接赋值写法后，防护用例 `1 failed`，证明防护有效。

## 变异验证（隔离进程 runtime patch，未改工作区源码）

**入口禁用**：

| 禁用入口 | 结果 |
|---|---|
| `NovelAnalyzer.analyze_chapter` | 6 failed / 128 passed |
| `style_analyzer.call_llm_semantic` | 23 failed / 111 passed |
| 两者同时 | 29 failed / 105 passed |

**行为变异**：

| 变异 | 结果 |
|---|---|
| helper 开始返回 list（复活死分支） | 9 failed |
| helper 接受数组并取首 dict | 6 failed |
| location 改为 truthiness 判定 | 3 failed |
| 重新引入直接赋值泄露 | 防护用例 1 failed |

两个早期的变异实为行为等价（`extract` 已返回围栏内容，数组由
`safe_parse_json` 丢弃；跳过空首对象已被 repair 覆盖），已作为无效变异记录。


## 保留差异与未执行项

- **保留差异**：四处入口的 extract/parse 顺序、空 dict 判定、truthiness、
  旧 schema 转换均保持原样；未收敛任何 JSON 解析重复代码。
- **未执行**：未调用真实 LLM；未做后端全量（上轮 646 passed）；
  未涉及 frontend / splitter；pipeline 三处站点仅只读审计，未做运行时驱动。
- **给主审的开放项**：若 R3 认为合并收益大于风险，可基于本矩阵逐对合并；
  每一对都需先补边界用例。


# Step1 R1 第二轮（2026-10-15）—— 未通过，已二轮返修

**结论：暂不通过，仅返修 Step1，不放行 Step2。**

**主审独立验证**：六文件 targeted suite **61 passed in 5.68s**，exit 0；Step1 patch reverse-check 0；五个真实文件 hash 前后与 R0 一致。第一轮四项中「真实心跳消息测试、字符规则、状态表与生产注释」已落实。

**剩余 P2 与附带 P3（二轮返修依据）**

1. **P2 断连未真实覆盖**：旧 `test_disconnect_unsubscribes` 用 `fail_after=1`（第二次 `send_json` 才抛断连），而 `run(socket)` 默认 `wait_sent=1` 在第一条 ping 后就 cancel。主审在隔离进程 patch `DisconnectingWebSocket.send_json` 统计：连续跑原测试 10 次，`passing_disconnect_test_runs=10`、`actual_disconnect_branch_hits=0` —— 只验证了 cancel 后的 finally，没有真实断连。
2. **P3**：docstring/报告称「不 sleep」，实际 `run` 与 `feed` 有 `asyncio.sleep` 轮询；报告末尾第 3 条 `agent.md` 重复。

**二轮返修内容**

- **断连真实覆盖**：`DisconnectingWebSocket` 改为「第二次发送立即抛 `WebSocketDisconnect`」并设置 `disconnected` 事件作为可观察标记；测试先以该标记为前提，再用 `asyncio.wait_for(task, timeout=5.0)` 等真实 `ws_progress` **自然返回**（走 `except WebSocketDisconnect → finally`），不以 cancel 代替断连。
- **两条路径分开**：新增 `test_cancel_path_also_unsubscribes` 作为「外部 cancel」对照组，与 `test_disconnect_unsubscribes`（断连自然返回）区分。
- **cancel 只做清理**：`_wait_for` 仅在等待超时/失败路径 cancel 并抑制预期 `CancelledError`；helper 与 feeder cleanup 只捕获 `asyncio.CancelledError`，不再吞所有 `Exception`。
- **等待改为事件驱动**：`FakeWebSocket.sent_event` / `DisconnectingWebSocket.disconnected` / `subscribed` 事件替代 sleep 轮询；`feed` 内改为 `await asyncio.sleep(0)` 让出控制权配合事件。
- **移除自证哨兵**：删除 `test_no_ping_when_heartbeat_disabled`（把测试 transport 的 `send_json` mock 成 no-op 再断言没记录，属自证循环，不是生产变异检测）—— 已有真实发送测试与变异结果。
- **表述改正**：「不 sleep / 不等待真实 25 秒」改为准确表述「不等待真实 25 秒，等待用事件驱动」。

**二轮返修后的验证**

| 项目 | 结果 | 退出码 |
|---|---|---|
| `pytest backend/tests/test_ws_protocol_ping.py`（改后 5 例） | 5 passed in 0.57s | 0 |
| 六文件 targeted（含 PING + 1A + 1B + 隔离 + 归档） | **61 passed in 6.03s** | 0 |
| 主审同法断连复核（隔离进程 patch `send_json` 记 hit，跑 10 次） | `passing_disconnect_test_runs=10`、`actual_disconnect_branch_hits=10` | 0 |
| 变异检查：加 payload / 移除心跳 / 包裹广播 / **去掉 unsubscribe（新增）** | rc=1（`2 failed,3 passed` / `4 failed,1 passed` / `1 failed,4 passed` / `2 failed,3 passed`） | 1（预期失败） |
| Ruff（既有选择集）生产代码 + 新增测试文件 | All checks passed | 0 |
| `git diff --check` | 通过 | 0 |

- 变异后已还原 `ws.py`（`git diff` 复核仅剩本步 4 行改动）；两个变异/复核脚本均用完即删，工作区无残留。
- 五个真实文件 SHA256 与 R0 基线一致；临时隔离根 `leftover_temp_dirs=0`。
- **本轮未重跑全量**：二轮返修只改 `test_ws_protocol_ping.py` 与文档。**上一轮全量 528 passed / 2 warnings** 作为上一轮证据保留，不冒充本轮结果。

**保留差异**：洞察写端（`aggregate_utils`）与 Excel 消费者（`excel_export`）保留，本步只删角色卡死读端；切分字符 U+FF0C / U+3001 不一致保留到 Step4；`agent.md` 其它历史描述未逐一核对；PING 测试用 monkeypatch 压 `_HEARTBEAT_INTERVAL`（若主审倾向虚拟时钟/TestClient 可再调）。
- 隔离在 collection 前生效，由 `test_runtime_isolation.py` 守卫；真实文件零内容改动；未禁用 / 跳过任何失败测试。
- 尚存问题：`api_failures.log` 的 mtime 在第一轮还原时已变（内容一致）；本步不覆盖真实历史旧 schema 数据读写。
- Step1 前置条件已具备：基线可复现（496 / 249）、目标模块无未解释失败、每步 diff 可独立审查、计划已按要求修订。
## R4 主审（2026-10-15）

结论：通过，仅放行 Step5；没有发现阻塞性问题。

- 生产 diff 仅公开既有后缀 tuple 并由 viz 引用，内容和顺序不变；聚合的先 strip 与纯后缀保留、viz 的后 strip 与纯后缀空名仍各自保留。现有 viz 已依赖 aggregate_utils，未引入新模块依赖环。
- 书名生产逻辑本步未改；不同用途不强行合并的审计结论可接受，既有 quirks 和「连载/新书」有效性缺口不借清理修复。真实 metadata 优先/fallback 测试及修复脚本只读 AST 引用审计保留。
- 主审相关集（新后缀、新书名、角色卡、JSON 边界）：**268 passed in 5.19s**；现有书名推断/DOCX/原子聚合集：**99 passed in 3.54s**。均退出码 0；与实施方的 306 项结果分开记录，未重跑全量或前端。
- Step4 patch 反向检查与 `git diff --check` 通过，五个真实文件 SHA256 前后与 R0 基线一致；旧私有后缀常量无残留生产引用，补丁不混入 Step2/Step3 的 final_summary 改动。
- 剩余测试限制：部分书名矩阵仅断言字符串类型，具体边界由独立精确值及服务入口用例验证。由于本步未变更书名实现，不构成本次行为回归，不要求扩大本步范围。

# Step 4（名称规则审计）交付（R4 已通过）

**范围**：仅 4A（角色名后缀单一来源）与 4B（书名规则审计）；
未进入 Step5，未提交代码，未剥夹会话的未提交改动。

## 修改文件（仅本步）

- `backend/utils/aggregate_utils.py`：`_CHAR_NAME_SUFFIXES` → 导出 `CHAR_NAME_SUFFIXES`，内容与顺序不变。
- `backend/services/viz_service.py`：`_aggregate_chars_edges` 的 `normalize_name` 删除重复字面量列表，
  改为遍历 `CHAR_NAME_SUFFIXES`；import 同步。
- `backend/tests/test_char_name_suffix_source.py`（新）、`backend/tests/test_book_name_rules.py`（新）。
- `backend/services/final_summary.py` 本步**未修改**（开始时已含 Step2/Step3 改动）。

## 保留的边界行为差异（不随共享默认统一）

聚合侧先 `strip()` 且纯后缀输入返回原名；
图谱侧仅 `return` 时 `strip()` 且绬后缀输入返回 `""`。
两者原本就不一致，本步仅共享后缀列表本身，行为分别锁定。

## 书名规则审计结论（不合并的核心依据）

`infer_book_name` 与 `_clean_book_name` 在 21 个样本上 **10 个结果不同**：
`infer_book_name` 保留《》并剥版本后缀；
`_clean_book_name` 剥《》【】与尾部括号备注，但不剥版本后缀。
合并会同时改变两类调用方（文件名推断 vs 展示名清洗），故不提取。

- `_looks_valid_book_name` 无 splitter 对应，保留。
- `fix_metadata_titles.py` 直接 import `_clean_book_name` / `_looks_valid_book_name`，两名字必须保留；
  测试仅用 `ast` 验证 import 关系，**未执行真实修复脚本**。
- 已知缺口（记录不修）：`_NON_TITLE_PREFIXES` 无「连载」「新书」，故两者通过有效性检查。
- 记录的怪异现状：`_clean_book_name("《》")` → `""`；混合书名号只剥首尾各一层；
  `infer_book_name("《》.txt")` → `"《》"`。

## 测试结果（按实际命令）

- `test_char_name_suffix_source.py`：**20 passed**；含真实生产入口回归
  （`JSONAggregator.aggregate_character_tracking` 与 `viz_service.graph_data`）。
- `test_book_name_rules.py`：**98 passed**。
- 相邻风险集：角色卡 + 聚合 **18 passed**；
  书名推断 + JSON 边界 + 后缀单一来源 **231 passed**；
  审计/自动总结/优化 **12 passed**；
  splitter/总结/图谱相关 **45 passed**。合计 **306 passed**。
- Ruff `F401,F811,F841,F821,F822,F823`：All checks passed。

## 变异验证（隔离进程 runtime patch，未改工作区源码）

| 变异 | 结果 |
|---|---|
| viz 自带后缀副本 | 1 failed |
| 删除一个后缀 | 10 failed |
| 后缀倒序 | 2 failed |
| `_clean_book_name` 不剥书名号 | 11 failed |
| `_NON_TITLE_PREFIXES` 删项 | 2 failed |
| `_NON_TITLE_PREFIXES` 补「连载/新书」 | 2 failed |
| `infer_book_name` 不剥版本后缀 | 10 failed |

## 保留差异与未执行项

- 书名两条规则保留原样，未合并、未收敛。
- 未调用真实 LLM；未跑后端全量；未执行 `fix_metadata_titles.py`。
- 未提交代码、未切分支/worktree。
- 五个真实文件 SHA256 与 R0 基线一致；`leftover_temp_dirs=0`。
- 独立 patch：`docs/patches/step4-r4.patch`（LF，29032 字节），仅含 4A/4B 相关四文件，
  `git apply --check -R --whitespace=nowarn` 退出码 0。
## R5 第三轮主审（2026-10-15）

结论：通过，没有发现阻塞性问题。用户要求 Step6 先不进行，因此本次不放行下一步；自动回访已更新为 PAUSED。

- 主审生命周期/归一化服务/e2e/自动总结/session tokens/normalizer 关联集：**88 passed in 6.75s**，退出码 0。未重复全量或前端套件。
- 原漏登记用例已登记服务；fixture 守卫现在驱动真实 async-generator teardown，非复制清理循环。普通清理异常被收集后抛出，不再静默吞掉。
- 主审隔离进程禁用真实 fixture teardown 后，守卫在「fixture 必须取消未完成任务」断言按预期失败，pytest 返回码 1。探针在失败观察之后单独清理自身任务，所有捕获任务均已完成。1 个预导入 anyio 的 assert-rewrite 警告归探针，不计入普通套件。
- Step5 patch 反向检查与 `git diff --check` 通过；五个真实文件 SHA256 前后与 R0 基线一致；两类生产服务本步仍未修改。
- 原生命周期重复因职责差异和有限收益保留；不是宣称已经删除重复。Step6 涉及旧摘要数据丢失修复与历史字段兼容/迁移，须先讨论范围，再单独授权。

## R5 第二轮主审（2026-10-15）

结论：暂不通过，仅返修测试 fixture，不放行 Step6。第一轮的 `_settle` 超时假阳性已修正；不改变生产服务或不抽取的审计结论。

- P2：`started_services` 接收手动登记的列表，但部分启动服务用例没有 append 且无 finally，例如 `test_summary_start_success_lifecycle`；跨服务进度及 token 来源用例同样缺少登记。中途断言失败时，fixture 无法清理这些任务。
- P2：`test_started_services_fixture_cancels_mid_test_failure` 在测试里复制清理循环，并非执行真实 fixture teardown。主审隔离进程把实际 fixture 换成仅 yield 空列表且不清理，守卫仍 **1 passed**，pytest 返回 0（1 个预导入 anyio 的 assert-rewrite 警告）。故当前守卫无法发现 fixture 失效。
- 实际 fixture teardown 仍有 `except Exception: pass`，与只抑制预期取消的要求不符；未记录的清理异常不应静默吞掉。
- 主审关联六文件：**88 passed in 6.18s**，退出码 0；Step5 patch 反向检查与 `git diff --check` 通过，五个真实文件 SHA256 前后与 R0 基线一致。
- 最小返修：所有已启动服务要么登记、要么受明确 finally 管理；守卫直接驱动真实 fixture 实现或运行隔离 pytest 故意失败用例后观察 teardown，不复制其逻辑；仅抑制 CancelledError，普通清理错误要保留为失败证据。

## R5 第二轮窄返修完成（2026-10-15，待 R5 再审）

**已修（仅测试助手，生产代码零改动）**：

1. **补登记**：`test_summary_start_success_lifecycle`、
   `test_progress_callbacks_are_not_interchangeable`（其中两个 `svc`）、
   `test_token_stats_sources_differ` 现在都在 `start()` 之前 `started_services.append(svc)`。
   已有完整 `try/finally` 管理的守卫 / never-runner 用例保留不动。
2. **守卫驱动真实 fixture**：通过 `__pytest_wrapped__.obj` 取到 fixture 的原始 async generator，
   `await gen.__anext__()` 拿真实 `tracked` 列表，`append(svc)` 后再 `await gen.__anext__()` 触发真实 teardown；
   测试里不再复制 cancel 循环。
3. **fixture 取消异常处理**：只抑制 `CancelledError`；其余 `BaseException` 全部记录，
   等所有登记服务都清理完后一并重抛为 teardown 失败，不静默吞掉。

**主审反向探针现在结果**：把模块 `started_services` 替换为仅 `yield []`、
无清理逻辑的 fixture 后，`test_started_services_fixture_cancels_mid_test_failure` 现在
**FAILED**（`AssertionError: the fixture must cancel a task it cannot finish`），pytest 退出码 1。
另一探针直接驱动无清理 fixture 的 async generator，确认 `task.done()=False`、
`cancelled()=False`、`svc.is_running=True`、剩余 pending task = 1，即捕置的是真漏漏。

**实际命令与结果**（与主审的 88 passed 分判记录，不混计数）：

命令：`.venv\Scripts\python.exe -m pytest backend/tests/test_service_lifecycle_matrix.py -o addopts='' -q`
结果：**26 passed in 3.80s**。

命令：`.venv\Scripts\python.exe -m pytest backend/tests/test_service_lifecycle_matrix.py backend/tests/test_location_normalization_service.py backend/tests/test_location_normalization_e2e.py backend/tests/test_auto_summary.py backend/tests/test_session_token_stats.py backend/tests/test_location_normalizer.py backend/tests/test_location_normalizer_empty_guard.py -o addopts='' -q`
结果：**90 passed in 6.44s**。

Ruff `F401,F811,F841,F821,F822,F823`：All checks passed。
变异六处全部被杀：总结终态 phase 6 failed、归一化三态分支 2 failed、
`stop()` 置空 3 failed、`start()` 置空 24 failed、token 累计 5 failed、summary 模型 replace 1 failed。
独立 patch：`docs/patches/step5-r5.patch`（LF，37756 字节），仅含一个新测试文件，
`git apply --check -R --whitespace=nowarn` 退出码 0；五个真实文件 SHA256 与 R0 基线一致。

## R5 第一轮主审（2026-10-15）

结论：暂不通过，仅修正测试助手，不放行 Step6。两类生产服务本步无 diff，没有发现生产回归；保留不同生命周期实现、不强行抽取的判断合理。

- P2：`_settle` 吞掉 TimeoutError 和 CancelledError，主动取消任务后仍正常返回；cleanup 又吞任意 Exception/BaseException。这将成功完成与超时后测试自行清理混为一谈，削弱生命周期完成和泄漏验证。
- 主审隔离进程把 FakeRunner.run、FakeNormalizer.run 都改为永不返回，运行 `test_no_background_tasks_leak` 仍 **1 passed**，pytest 返回 0（1 个预导入 anyio 的 assert-rewrite 警告）。两个 service 任务实际由测试超时后取消，守卫没有要求自然完成。
- 主审生命周期/归一化服务/e2e/自动总结/session tokens/normalizer 关联集：**85 passed in 5.17s**，退出码 0；Step5 patch 反向检查、`git diff --check` 通过，五个真实文件 SHA256 前后与 R0 基线一致。
- 最小返修：等待超时须清理后重新抛出，成功路径断言任务自然完成且未取消；外部取消清理单独验证。清理只抑制预期 CancelledError，不能吞任意异常；每个启动过服务的用例在失败断言时也须保证任务清理。修正文档中「task/runner 已释放」为真实事实：当前服务保留已完成 task 与 runner 引用，只是任务不再运行。
正常终态，cleanup 只抑制预期 CancelledError，启动过的用例无条件终结任务，并补超时哨兵。

## R5 窄返修完成（2026-10-15，待 R5 再审）

**已修（仅测试助手）**：

- `_settle` 只等自然完成（`wait_for(asyncio.shield(task))`）；超时/意外取消时 cancel 后重新抛出，不再吞错误。
- 正常路径新墟三条真实终态断言：`task.done()`、`not task.cancelled()`、`exception() is None`。
- `_cleanup` / `_cleanup_norm` 合并为 `_cancel_task`，只抑制预期 `CancelledError`；删除 `except BaseException` 与吞任意 `Exception` 的分攭。
- 新墟 `started_services` fixture：每个启动过 service 的用例无条件在 teardown 终结任务，中途断言失败也不泄漊。
- 新墟超时哨兵 `test_never_completing_runner_fails_and_leaves_no_task`（summary / norm 各一例）。
- 新墟 `test_started_services_fixture_cancels_mid_test_failure`，直接驱动 fixture 清理路径。

**主审提供的复现现在结果**：把 `FakeRunner.run` 与 `FakeNormalizer.run` 同时 patch 成永不完成后，
`test_no_background_tasks_leak` 现在 **FAILED**（`AssertionError: service task did not finish within 5.0s`），pytest 退出码 1；
同时它的 `finally` 仍然执行，失败后剩余 pending 任务为空。

**实际命令与结果**：

命令：`.venv\Scripts\python.exe -m pytest backend/tests/test_service_lifecycle_matrix.py -o addopts='' -q`
结果：**26 passed in 3.93s**。

命令：`.venv\Scripts\python.exe -m pytest backend/tests/test_service_lifecycle_matrix.py backend/tests/test_location_normalization_service.py backend/tests/test_location_normalization_e2e.py backend/tests/test_auto_summary.py backend/tests/test_session_token_stats.py backend/tests/test_location_normalizer.py backend/tests/test_location_normalizer_empty_guard.py -o addopts='' -q`
结果：**90 passed in 7.78s**。（与主审的 85 passed 是不同命令；主审未含本次新墟的两个哨兵用例。）

**变异**（隔离进程 runtime patch，六处，全部被杀）：总结终态 phase 6 failed、
归一化三态分支 2 failed、`stop()` 置空 3 failed、`start()` 置空 24 failed、
关掉 token 累计 5 failed、去掉 summary 模型 replace 1 failed。

**文档约正**：`status()` 保留 done task 与 runner 引用，不是“释放”，改称“完成/不再运行”；
四片段的不同之处（两条 ValueError 消息）已写入测试 docstring，不提取的理由改为“收益小且职责不同”。

# Step 5（生命周期重复审计）交付（R5 第一轮未通过，待窄返修）

**范围**：仅 `SummaryService` 与 `LocationNormalizationService` 的真实 start/stop/status 生命周期审计；
未进入 Step6，未提交代码。

## 修改文件（仅本步）

- `backend/tests/test_service_lifecycle_matrix.py`（新，23 个用例）。
- `backend/services/summary_service.py` 与 `backend/services/location_normalization_service.py`
  **未修改**；本步没有产生任何生产代码 diff。

## 审计结论：为何不抽取共享基类

字字相同的只有四块：`is_running` 属性、书目存在护关、
有章节结果护关、每次启动的状态复位块。
其余全部分遙：`start()` 签名与参数、runner 构造参数、
`on_progress` 体（WS 消息类型、日志分类、`batch_failed` 级别、
是否把 runner phase 写进状态）、token 统计回调、终态状态映射。
`SummaryService` 还多出分析互斤、`allow_during_analysis` 入口、报告写盘、
会话 token 累计与落盘；归一化则多出 `True`/`None`/`False` 三态与
LLM 配置 replace。

把这四块提进基类需要 runner 类型分攬、回调注入或持久化开关，
计划明确禁止，故**保留现有重复**并用具体矩阵说明，
不称“已移除重复”。

## 矩阵覆盖

重复 start、未运行 stop、进行中 stop、运行异常、完成后再启动、
自动总结入口（分析互斤与 `allow_during_analysis`）、会话 token 只累计一次、
归一化三态返回、LLM 配置 replace、报告写盘、事件序列（状态 + 进度 + 日志）、
后台 task 完成/取消与 mock 作用域恢复。
新片段不复制生产闭包、不以源码字符串代替行为断言。

## 测试结果

- 单文件 **23 passed in 4.54s**。
- 相邻集（归一化服务 / e2e / 自动总结 / 会话 token / normalizer）合计 **84 passed**。
- Ruff `F401,F811,F841,F821,F822,F823`：All checks passed。
- `leftover_temp_dirs=0`；无后台 task 泄漊。

## 变异验证（隔离进程 runtime patch，未改工作区源码）

| 变异 | 结果 |
|---|---|
| 总结终态 phase 改值 | 5 failed |
| 归一化三态分支改判 | 2 failed |
| `stop()` 置空 | 3 failed |
| `start()` 置空 | 21 failed |
| 关掉总结 token 累计 | 5 failed |
| 去掉 summary 模型 replace | 1 failed |

## 保留差异与未执行项

- 两类服务的重复片段保留原样，未合并、未抽取。
- `status()` 每次调用都会重读 `get_token_stats()`（总结侧），属于现状行为，未改。
- 未调用真实 LLM；未跑后端全量；未涉及 frontend；未提交代码。
- 五个真实文件 SHA256 与 R0 基线一致。
- 独立 patch：`docs/patches/step5-r5.patch`（LF，仅一个新测试文件），
  `git apply --check -R --whitespace=nowarn` 退出码 0。
