---
doc_type: plan
status: draft
---

# 第三批清理：重复逻辑收敛与旧数据兼容审计

| 项目 | 约定 |
|---|---|
| 日期 | 2026-10-14 |
| 目标 | 消除已证实的无用依赖和重复实现，保持现有业务行为及旧数据可读性 |
| 范围 | 后端流式进度、JSON 解析、名称规则、任务生命周期和持久化兼容层；必要的测试与文档同步 |
| 执行方式 | 每一步先确认契约，再做小范围修改，完成步间 review 后进入下一步 |
| 当前状态 | Step0/R0 至 Step5/R5 已通过；用户要求 Step6 先不进行，等待讨论，自动回访已暂停；第三批整体未完成 |
| 清理基线 | 前两批已提交为 `90c3699`；勘查时 HEAD 为 `7e70c83`，执行前重新记录实际 HEAD 与工作区状态 |

## 1. 边界与前置约束

- 本批只做清理和复审发现的直接修正，不增加 API、设置项、业务规则或依赖。
- 不修改 Prompt 文本、模型选择、重试策略、并发上限、账本休眠阈值、Token 计费统计及 checkpoint 失效规则。
- 现有工作区含未提交的 session token、设置探测、SummaryPage 等改动。执行前记录涉及文件的基线 diff；后续修改叠加在其上，不能还原或覆盖。
- 与 `docs/PLAN-provider-error-taxonomy-2026-09-30.md` 重叠的 LLM 错误处理不纳入本批。
- 测试使用临时目录、合成 JSON 和 mock LLM；默认验证不得调用真实模型、写真实工作区或运行 `backend/scripts/fix_metadata_titles.py`。
- 工作区/归档的格式盘点只读取 schema 与字段形态，不输出 API Key、小说正文或完整用户数据，也不执行原地迁移。
- 不以“无静态调用”判定落盘字段可删；生产写入、读取、序列化、恢复、脚本和前端消费者必须一起核对。
- 每步 review 记录结论、测试命令与实际结果、保留差异、未处理问题；基线失败与新增失败分别记录。

## 2. 已核实的候选与处理判断

| 候选 | 当前证据 | 本批决定 | 难度 |
|---|---|---|---|
| 角色卡无用洞察读取 | `character_card_generator.py` 的 `long_context_insights` 只有初始化/赋值，没有输出消费者；损坏 JSON 会使索引为空、有效角色详情报 404 | 删除这条无用状态与读取链，补真实入口回归 | 低 |
| 进度测试复制生产实现 | `test_queue_service.py::test_on_progress_block_start_publishes` 调用测试内 `on_progress_like`，未调用 `make_pipeline_callbacks` | 改测真实回调，保留行为断言 | 低 |
| PING 协议声明遗漏 | `api/ws.py` 仍发心跳，前端仍识别 `ping`；`WSType` 已删 PING，却宣称包含全部消息类型 | 恢复协议常量，明确心跳不带 payload 的现有例外，不改变线上消息结构 | 低 |
| 三份 Token 进度回调 | `analyzer.py`、`FinalSummaryRunner._make_on_progress`、`style_analyzer.py` 的过滤/耗时/速率/广播主体相同 | 抽取最小回调工厂，保留调用点身份与生命周期 | 中 |
| JSON 解析组合重复 | analyzer 先提取后解析；归一化先解析后提取；总结先解析后提取；风格分析有独立校验 | 先做输入/输出对照，只收敛严格等价部分 | 中 |
| 角色名后缀重复 | `_CHAR_NAME_SUFFIXES` 与 `viz_service.normalize_name` 列表相同；空白和空名字处理不同 | 优先共享后缀规则，保留调用方边界行为 | 中 |
| 书名清洗分散 | splitter 的 `infer_book_name` 返回文件名推断结果；总结 `_clean_book_name` 清洗展示名；修复脚本也导入总结函数 | 保留不同职责，仅提取确认等价的规则片段 | 中 |
| 两类任务生命周期重复 | SummaryService 与 LocationNormalizationService 有相同状态字段/进度转发；互斥、结果判定、Token 持久化等不同 | 审计后择小片段抽取；默认不引入通用任务基类 | 中高 |
| `_legacy_text` / KnowledgeBase 旧层字段 | PromptBuilder 仍渲染 `_legacy_text`；pipeline 仍读取并尝试重新初始化；模型负责转换旧层字段 | 保留兼容读取，核查往返与恢复；不直接删字段 | 高 |
| 伏笔 batch 字段 | final_summary 仍写入 `first_seen_batch`、`last_seen_batch`、`resolved_batch`、`batch_size_snapshot`；模型序列化保留 | 保留当前 JSON 契约，审计实际用途；删字段/升级 schema 另列迁移任务 | 高 |

## 2.1 复审修订（2026-10-14，独立 reviewer：结论「计划需修订后执行」）

本节把复审证据固化进各 Step 的执行口径；与正文冲突时以本节为准。

1. **P1 测试副作用，必须在收集前隔离。** `backend/tests/test_archive_path_update.py::test_archive_then_get_book_path_finds_new_location` 使用真实 `get_service()`，替换 `svc.queue._items` 后经 `_archive_item → save_queue` 写默认 `queue_state.json`；测试只恢复 `config_manager`，未恢复队列与书目缓存。`NOVEL_ROOT` / `LOG_FILE` / `CONFIG_FILE` / `QUEUE_STATE_FILE` 均在 import 时求值，收集期 import 同样触发，所以隔离必须写进 collection 之前的 rootdir `conftest.py`，不能"先跑全量再补隔离"。真实 config/queue/workspace/日志只做哈希与存在性核对；验证子进程不得继承真实 API Key；不写真实小说目录，不执行 `backend/scripts/fix_metadata_titles.py`。→ 已在 Step0/R0 实施。
2. **P2 旧摘要丢失是原有缺陷，不是本批回归。** `backend/models/knowledge.py::_load_rolling_structured` 跳过已保存的 `_legacy_text`，`from_dict` 在检测到 `_legacy_text` 时又清空 `rolling_layer_early/recent`；旧层 → `from_dict` → `to_dict` → 再 `from_dict` 会变成 `{}`，marker-only 记录同样被丢弃。→ Step6 必须单独写失败回归再修正，保留合法 marker，覆盖混合数据、两轮往返、Prompt 渲染与重启/checkpoint 恢复；不得当作清理新回归，也不得删 schema。
3. **P2 角色卡无用读链。** `backend/utils/character_card_generator.py` 的 `long_context_insights` 只有赋值、没有消费者；`long_context_insights_aggregated.json` 损坏会让合法 `character_tracking` 的索引为空，有效角色详情报 `KeyError` / 路由 404。→ Step1 删除该无用状态与读链，测试走真实生成器与真实入口异常契约。
4. **P2 测试缺口。** `backend/tests/test_queue_service.py::test_on_progress_block_start_publishes` 的 `on_progress_like` 是手写复制闭包，即使禁用真实 `make_pipeline_callbacks` 仍然全绿。→ Step1 改测真实工厂，覆盖结构化 range 优先 / message 回退、DONE/FAILED/SKIPPED 事件与统计（生产回调目前正确，只补测试）。
5. **P3 JSON 解析覆盖面。** `safe_parse_json` / `parse_json_robust` 只返回 dict/None，总结里的 list 分支不可达；裸数组经 extract 首个对象可被接受，围栏数组却失败；首个对象为空/无关时不会继续寻找后续有效对象。→ Step3 先修正 plan 的"列表对象选择"表述与样本矩阵，新 helper 不得返回 list 从而重新激活旧分支。
6. **P3 PING 与文档。** `backend/api/ws.py` 仍发 `{"type":"ping"}`，前端仍识别，但 `backend/ws_events.py::WSType` 已删 PING 却声明类型全集。→ Step1 恢复常量并注明「心跳不带 payload」例外，线上消息结构不变。`agent.md` 仍把无消费者的 networkx/pyvis 写成当前依赖，测试示例 `test_pipeline.py` 不存在。→ Step1 修正当前说明，保留历史记录。
7. **流式工厂提取须保留原计时创建位置。** 总结的 `on_progress` 在拿到并发槽之后再创建；累计值已由 `LLMClient` 维护，工厂内不得二次累计；上游 `reasoning` 可能转成 content 型进度通知，不得改变过滤链；保持 `await broadcast_token_delta` 与 Hub 节流入口。→ 约束 Step2。
8. **保留差异优先，不越权改动。** 任务生命周期与书名差异保留；伏笔 batch 字段仍在写入，无权直接删字段 / 升级 schema / 原地迁移；Ruff 使用已确认版本（本仓 `.ruff_cache` 为 `0.16.9`），不静默升级。

## 3. Step 0：固定验证基线

**R0 前置（先做，再跑任何收集/全量）：测试运行根隔离**

依据 §2.1 第 1 条。隔离必须在 collection/import 之前完成：

- [x] **写前隔离（R0 第一轮被拒后重写）**：新增 `pytest_isolation.py`，在任何 `backend.*` import 之前把测试进程 **cwd** 切到会话级临时根（替代原先"先 import 再移除 handler"的写后移除），并把 `NOVEL_ROOT` 指向它；写一份仅含占位值的合成 `config.json`；清空 `LLM_API_KEY` / `MINIMAX_API_KEY` / `NOVEL_ANALYZER_SESSION_TOKEN`。隔离初始化失败抛可诊断错误，不再 `except Exception: return` 后继续跑未隔离测试。
- [x] rootdir `conftest.py` 只做 `pytest_isolation.install(REPO_ROOT)` + autouse fixture：逐用例 `assert_isolation_intact()`（cwd 未偏离隔离根、无隔离根之外的真实 api_failures file handler），并重置 `AnalysisService` / `SummaryService` / `LocationNormalizationService` 单例与 `book_service` 的 `_books` / `_manual_books` / `_refreshing`。
- [x] collection 路径兼容：`pytest_isolation` 读 `pytest.ini` 的 `testpaths` 并在隔离根建同名占位目录——pytest 用 `glob.iglob` **按当前 cwd** 解析 `testpaths`，不建占位会回退成"从 invocation_dir 递归收集"（实测收集数由 490 变 494）。
- [x] `backend/tests/test_archive_path_update.py` 两个用例补齐队列 `_items` 与书目缓存还原，并断言 `QUEUE_STATE_FILE.parent == NOVEL_ROOT`，锁死"落盘目标是隔离根"。
- [x] 新增默认收集的守卫测试 `backend/tests/test_runtime_isolation.py`：进程内契约 + **独立子进程回归**（原始 cwd 无默认日志 / 存在 synthetic sentinel 两种场景；首次 import + 用例内新建 `FailureLogger().record_failure` 均不得创建或改动原始 cwd 默认文件，handler 只指向隔离根）+ 正向对照（真实泄漏必须被抓到、nul 设备 handler 必须被精确排除）。
- [x] 全量运行后核对真实 `config.json` / `queue_state.json` / `analyzer.log` / `crash.log` / `api_failures.log` 与真实 `workspace/`：哈希与顶层时间戳不变，临时根已清理。

- [ ] 记录实际 HEAD、`git status --short`、相关文件现有 diff 和测试收集范围。
- [ ] 确认环境已有依赖，记录测试可执行性；不为本批擅自升级工具或依赖。
- [ ] 执行现有针对性测试，覆盖 streaming、summary、JSON、角色/书名、账本、memory 和模型。
- [ ] 若基线通过，做一次后端全量 pytest 和前端 Vitest/typecheck，记录实际数量，避免引用旧的 463/483/490 等数字。
- [ ] 为各步记录现有行为对照。已有明确 bug 不写成必须永久维持的“正确行为”，而是单列问题。

**Review R0**：相关失败均已归因，涉及文件的既有改动可区分。无法获得可靠基线的模块暂停修改，其他独立步骤仍可继续。

## 4. Step 1：先关闭前两批复审遗留

### 1A. 角色卡无用依赖

- [ ] 删除 `CharacterCardGenerator.long_context_insights` 和对应聚合文件读取。
- [ ] 在 `backend/tests/` 增加可默认收集的角色卡回归，使用真实生成器与合成聚合文件，不恢复失效的绝对路径冒烟脚本。
- [ ] 验证正常索引排序、详情字段、空目录和不存在角色。
- [ ] 验证洞察文件缺失/损坏不影响正常角色；异常的真实角色数据仍按原有契约处理，不能用“全吞异常”掩盖问题。

### 1B. 生产回调测试

- [ ] 将复制闭包的测试改为构造 `make_pipeline_callbacks(hub, item, stats, seen_characters)` 返回的真实回调。
- [ ] 验证结构化 range 优先、message 回退，以及 DONE/FAILED/SKIPPED 转发和统计记录。
- [ ] 用隔离 hub/mock 或严格清理订阅的真实 hub，避免 singleton 和异步任务污染其他测试。

### 1C. 协议与文档

- [ ] 恢复 `backend/ws_events.py::WSType.PING`：`backend/api/ws.py` 仍每 25 秒发 `{"type":"ping"}`，前端仍识别；常量须注明「心跳不带 payload」这一现有例外，线上消息结构不变。
- [ ] 修正 `agent.md`：把 `networkx + pyvis` 明确为"当前无消费者的历史依赖/已移除"，改掉不存在的 `test_pipeline.py` 测试示例；当前测试数字按本次实测填写（后端 490 passed / 前端 249 passed），历史回归数字保留其时间语境。

**Review R1**：无用读取链完全消失；新测试会在恢复旧读取或破坏生产回调时失败；协议声明符合实际消息。该步独立验收，不与后续抽取混成一个 diff。

## 5. Step 2：收敛流式 Token 进度回调

拟新增 `backend/core/stream_progress.py`，只负责创建 `StreamChunk -> ProgressHub` 的异步回调，不接管 LLM 重试或计费统计。

- [ ] 用当前三条生产路径建立行为样本：输入 chunk 序列、时间、广播事件与顺序。
- [ ] 保留过滤条件：只对 `content` / `usage` 广播，`reasoning` / `error` 不新增广播。
- [ ] 保留 `estimated_total_tokens or 0`、`elapsed_sec`、`max(elapsed, 0.1)` 速率计算和累计量语义。
- [ ] 工厂创建时启动单独的 monotonic 计时；不同调用不能共享计时器/累计状态，重试过程中不能额外重置计时。
- [ ] 保留三处现有 context、session_id 来源、unknown 回退和 unit_idx；风格路径固定 unit_idx=0。
- [ ] 将调用方替换为工厂调用，按引用审计移除遗留 import。总结的 `_make_on_progress` 可保留为薄适配器，若仍有语义或测试价值。
- [ ] 通过可控时钟与 mock hub 检查等价输出，并验证并发回调彼此隔离、usage-only、零耗时和失败重试。
- [ ] 跑真实 analyzer/summary/style 调用路径测试，确认工厂确实接线；只测工厂自身不足以验收。
- [x] （R2 实施）`backend/core/stream_progress.py` 提供 `make_stream_progress_callback`；三个调用点均已接线，源码级断言 + 行为级对照。
- [x] （R2 实施）`_make_on_progress` 保留为薄适配器，四个调用点形态不变，计时仍在并发槽之后。
- [ ] 计时创建位置保持原样：总结路径必须先拿到并发槽、再创建 `on_progress`（不得前移到 `_make_on_progress` 调用点）；章节与风格路径沿用各自原有创建时机，不同调用不共享计时器或累计状态。
- [x] （R2 实施）源码顺序断言 + 变异检查：把创建前移到 acquire 之前会让测试失败（2 failed）。
- [ ] 累计量只由 `LLMClient` 维护，工厂内不得二次累加；上游 `reasoning` 可能被转成 content 型进度通知，过滤链（content/usage 广播，reasoning/error 不新增广播）不得改变。
- [x] （R2 实施）`test_no_double_accumulation` 锁定透传语义；`test_only_content_and_usage_broadcast` 锁定过滤链。
- [ ] 保持 `await broadcast_token_delta(...)` 与 Hub 节流入口不变，事件身份、字段、顺序和等待方式不因抽取改变。
- [x] （R2 实施）`test_broadcast_is_awaited` 用 `await asyncio.sleep(0)` 探针证明被调用方 await；RecordingHub 只替换记录、不改节流。

**Review R2**：事件身份、字段、顺序、广播等待方式和节流入口均不变；成功/失败成本统计不变。发现身份来源现有问题时另列修复，不借抽取改变值。

## 6. Step 3：JSON 解析重复审计与有限收敛

| 路径 | 必须保留的区别 |
|---|---|
| analyzer 验证与 `_parse_response` | 先 extract 再 safe_parse；必要字段校验、parsed_cache、重试错误消息 |
| location_normalizer `_parse_llm_dict` | 先直接 parse，失败/非 dict 后 extract；最终只接受 dict |
| final_summary `extract_foreshadow_json` | 当前 truthiness 判定、从数组中取「第一个 dict 元素」（首个为空/非 dict 时不再向后寻找）、伏笔 schema 校验 |
| style_analyzer | 当前代码围栏处理、语义结果校验与失败回退，不能顺手扩大修复接受范围 |

- [x] 建立样本矩阵：裸对象、数组、空对象/空数组、围栏、前后说明、多个对象、字符串内花括号、截断/可修复 JSON、标量和完全非法文本。
- [x] 矩阵必须记录既有事实：`safe_parse_json` / `parse_json_robust` 只返回 dict/None，总结的 list 分支当前不可达；修正的是 plan 的「列表对象选择」表述，新 helper 不得返回 list 从而重新激活该分支。
- [x] 单独锁定「裸数组 vs 围栏数组」差异与「首个对象为空/无关时不继续向后寻找」的现状：裸数组经 extract 首个对象可被接受，围栏数组当前失败；这是现状契约，收敛时逐条对照，不得顺手放宽接受范围。
- [x] 对各路径记录返回值、是否接受、校验错误和是否触发重试；不能仅比较“能不能 json.loads”。
- [x] 在现有 `json_utils.py` 中只提取等价组合，保持 `parse_json_robust` 修复顺序及策略。
- [x] 对不同解析顺序保留语义明确的调用路径；若抽取需要大量开关才能维持行为，保留小重复并记录原因。
- [x] 保留调用方业务 schema 校验、日志及失败返回类型，不将解析 helper 做成领域校验器。
- [x] 验证 parsed_cache 仍复用成功解析结果，不增加修复调用或 LLM 调用次数。

**Step3 实施结论（2026-10-15，R3 二轮返修后）**：勛单全部完成，生产方面仅删除了一处已授权的不可达 list 分支，**不新增 `json_utils.py` helper、不扩大接受范围**。原因：四处入口两两不等价——两处 parse-first 与两处 extract-first 分别在空 dict 上分歧；extract-first 与 parse-first 顺序相反；summary 多出 schema 转换。按计划准则保留调用方重复。“未抽取”是收益/风险判断，非“不存在等价片段”的完整证明。现状矩阵固化为 `backend/tests/test_json_parse_boundaries.py`（134 个用例，全部跟真实入口，mock 均经 monkeypatch 作用域管理）。

## 7. Step 4：名称规则共享

### 4A. 角色名后缀

- [x] 在 `backend/utils/` 放置角色名后缀的单一来源，优先消除重复规则列表。
- [x] 保留聚合与图谱各自的空白处理时机、纯后缀输入处理、大小写和只剥一次的行为。
- [x] 样本覆盖全角/半角标注、首尾空白、名字恰好等于后缀、连续标注、合法括号姓名和未识别后缀。
- [x] 用固定 result fixture 比较聚合角色键、图谱节点 ID、边权和角色计数；只比较函数字符串输出不足以验收。

### 4B. 书名

- [x] 比较 `infer_book_name`、`_clean_book_name`、`_looks_valid_book_name`，明确文件名、元数据、展示名和工作区目录名职责。
- [x] 保留 splitter 的权威标题优先级、书名号保留方式、双层书名号和版本后缀规则；保留总结的有效性判断及目录名 fallback。
- [x] 只迁移证实等价的片段；不同职责函数保留。若无足够共享收益，记录“审计完成，保留不同规则”。
- [x] 核对 `backend/scripts/fix_metadata_titles.py` 的直接 import；迁移函数时同步脚本 import 或保留薄转发，测试不得执行真实修复脚本。
- [x] 验证 TXT/DOCX 文件名、metadata.title、detect_book_name、checkpoint/book_id 与真实目录定位不改变。

**Review R4**：不新增角色合并或拆分，不重命名目录/书目 ID，不改报告标题规则。边界差异保留或另列行为修复，不随共享默认统一。

## 8. Step 5：生命周期重复评估

- [x] 记录两类服务 start/stop/status、成功/停止/失败状态事件、时间戳和任务完成清理。
- [x] 特别保护总结的分析互斥、`allow_during_analysis` 自动总结入口、报告写盘和会话 Token 累计。
- [x] 特别保护归一化的 `True`/`None`/`False` 三态、LLM 配置 replace、错误状态和 batch_done phase 更新。
- [x] 不因字段相同就建立 `BaseTaskService`。仅当小的状态/广播片段有完全一致的语义且明显减小复杂度时提取 helper。
- [x] 若抽取需要 runner 类型分支、回调开关或把持久化塞入基类，保留现有两类服务并记录取舍。
- [x] 修改时补真实服务生命周期测试：重复 start、未运行 stop、任务停止、异常、再次启动、自动总结入口和统计只累计一次。

**Review R5**：公开 status 字段、异常类型、运行互斥和事件时机保持一致。保留合理重复是有效审计结论，不将未实施抽取标为“已删除”。

## 9. Step 6：旧 schema 兼容审计

**执行限制（2026-10-15）**：用户明确要求 Step6 先不进行，讨论范围后再决定。R5 通过不构成 Step6 授权；当前不实施摘要兼容修复、不删 ledger 字段、不做 schema 升级或数据迁移。自动回访已暂停。

本步交付兼容性结论与回归样本；默认不删除 JSON 字段、不升级 schema、不执行批量迁移。

### 6A. KnowledgeBase 与滚动摘要

- [ ] 对照读取、Prompt 渲染、pipeline 初始化/重新初始化、MemoryState 快照与 flush、KnowledgeBaseManager 读取、to_dict 全链路。
- [ ] 覆盖单层 rolling_summary、early/recent 旧层字段、仅有 `_legacy_text`、现代 rolling_structured、混合格式及空值。
- [ ] **已确认缺陷（Step6 先写失败回归再修正）**：`backend/models/knowledge.py::_load_rolling_structured` 在 `rolling_structured` 已含 `_legacy_text` 时直接跳过，`from_dict` 又清空 `rolling_layer_early/recent`；因此「旧层字段 → `from_dict` → `to_dict` → 再 `from_dict`」会把 `rolling_structured` 变成 `{}`，marker-only（只有 `_legacy_text` 的记录）同样丢失。回归须覆盖该两轮往返并断言文本仍在。
- [ ] 保留合法 marker：`_legacy_text` 是 Prompt 与 pipeline 重新初始化依赖的合法载体，修正目标是"不丢文本"，不是删字段或删 schema；须覆盖混合数据（`_legacy_text` + 结构化字段）与纯 marker 两种输入。
- [ ] 验证旧摘要仍进入 Prompt，重新初始化失败时保留旧文本，重启和 checkpoint 恢复不会静默丢失摘要。
- [ ] 将“已有兼容性缺陷”与“抽取造成的回归”分开。若发现丢失问题，先写失败回归，再单独提交保持原始信息的修正，经 review 后恢复清理。

### 6B. 伏笔账本

- [ ] 核查 v1/v2/v3 reader、ForeshadowItem 构造要求、to_dict、reconciliation、全书复检、checkpoint 和前端消费者。
- [ ] 在有数据可读时只统计版本及关键字段形态；无旧样本时明确证据缺口，并使用合成历史 fixture。
- [ ] 验证现有合法历史样本读写往返，保留 ID、状态、证据章节、importance、confidence、composite_score、批次字段和时间元数据。
- [ ] 验证并发 batch 乱序与 batch_size 改变时，休眠依然只按章距判断，回收状态和断点恢复不变。
- [ ] 列出字段为“活跃逻辑/兼容存储/可移除候选”的证据；不能把“业务不读但仍写 JSON”归为可直接删。
- [ ] 真正删字段必须另写版本化迁移方案，包含可逆备份、混合版本读取、失败恢复与旧版本回退限制；本 plan 不授权执行该迁移。

**Review R6**：兼容样本、真实样本的可用范围、保留字段和迁移候选均可核验。存在未解释的字段/摘要损失时不能宣称兼容审计通过。

## 10. 测试与最终验收

执行环境采用 PowerShell，以下从仓库根目录运行。仅在相关步骤实际修改时添加对应测试；本步新增测试须在默认 testpaths 内。

```powershell
# 执行前的收集核对，取消配置中的静默参数以看到实际摘要
.\.venv\Scripts\python.exe -m pytest --collect-only -o addopts=''

# 流式 / JSON / 总结 / 风格相关现有回归
.\.venv\Scripts\python.exe -m pytest backend/tests/test_streaming.py backend/tests/test_llm_mock.py backend/tests/test_json_repair.py backend/tests/test_summary_checkpoint.py backend/tests/test_style_tokens.py backend/tests/test_style_task_stop.py -o addopts='' -q

# 兼容 / 名称 / 服务相关现有回归
.\.venv\Scripts\python.exe -m pytest backend/tests/test_models.py backend/tests/test_memory_state_rolling.py backend/tests/test_foreshadow_ledger.py backend/tests/test_foreshadow_composite_score.py backend/tests/test_book_name_inference.py backend/tests/test_infer_book_name.py backend/tests/test_infer_book_name_dquote.py backend/tests/test_location_normalizer.py backend/tests/test_location_normalization_service.py backend/tests/test_location_normalization_e2e.py backend/tests/test_auto_summary.py backend/tests/test_session_token_stats.py -o addopts='' -q

# 最终后端全量回归，包含各步骤新增测试和顶层 tests/
.\.venv\Scripts\python.exe -m pytest -o addopts='' -q

# 生产静态检查与 diff 检查；使用已有 Ruff，不自动升级/修改格式
uvx ruff check backend desktop.py --select F401,F811,F841,F821,F822,F823 --exclude backend/tests
git diff --check

# 前端最终验证，从 frontend/ 目录逐条运行
npm test
npx vue-tsc --noEmit
npm run build
```

步间执行受影响的现有测试与新增契约测试，最终做一次全量验证。若出现长时间挂起，先定位具体测试并有界停止；不得将中断套件报告为通过，也不通过禁用测试获得绿灯。

最终交付要求：

1. R0-R6 每步都有完成、明确保留或有证据的延期结论，保留/延期项不得计入“删除完成”。
2. 已实施抽取的调用点真正使用共享实现，旧副本及无用 import/状态按引用审计清理。
3. 未改变 Token 事件契约、JSON 接受范围、名称定位、服务状态机或历史数据可读性；任何直接缺陷修复均单列证据。
4. 新测试调用生产入口，不能重写一份被测逻辑；兼容样本使用临时目录并可复现。
5. 报告实际测试数量、失败/警告、未验证的部署/历史数据范围；不将类型检查当成视觉验收或完整运行验收。
6. 同步 `agent.md`、当前架构说明和 CHANGELOG。旧计划与历史变更记录只补状态说明，不重写历史事实。
7. 每步保持可独立审查的 diff；提交前区分本批与其他会话变更，不将整棵脏工作区一次性提交。用户未要求 Git 提交时只报告建议分组。

## 11. 实施记录模板

每步 review 后追加记录：

| 步骤 | 状态 | 修改文件 | 验证及结果 | 保留差异/后续问题 |
|---|---|---|---|---|
| Step 0 / R0 | **已通过**（主审第二轮复核，2026-10-14） | `pytest_isolation.py`(新)、`conftest.py`(新)、`backend/tests/test_runtime_isolation.py`(新)、`backend/tests/test_archive_path_update.py` | 收集 496；全量 496 passed / 2 warnings；主审复跑定向 12 passed；五文件 SHA256 不变（详见 §11.1） | 第一轮「写后截断」事件保留为历史；第二轮改为写前 cwd 隔离 |
| Step 1 / R1 | **已通过**（主审第三轮，2026-10-15；前两轮未通过事实保留） | 见 §11.2 | 主审六文件 **61 passed in 7.12s**；断连用例 10 次通过 / 真实分支命中 10 次；patch 反向检查与 diff 检查通过；五文件 SHA256 不变 | 洞察写端及 Excel 消费者保留；分隔符差异留 Step4；仅放行 Step2 |
| Step 2 / R2 | **已通过**（第三轮主审，2026-10-15；前两轮未通过事实保留） | `backend/core/stream_progress.py`(新)、`backend/tests/test_stream_progress.py`(新)、`analyzer.py`、`style_analyzer.py`、`final_summary.py` | 主审目标集 **82 passed in 7.41s**；切断 retry 回调传递的隔离探针触发新用例失败；patch 反向检查、diff 检查通过，五文件哈希不变 | 入口至真实重试链已贯通；仅放行 Step3。回调身份观察使用栈帧，属于测试维护风险，不阻塞本步 |
| Step 3 / R3 | **已通过**（第三轮主审，2026-10-15；前两轮未通过历史保留） | `backend/tests/test_json_parse_boundaries.py`(新)、`final_summary.py`(删不可达 list 分支) | 主审同进程相邻 **185 passed in 54.80s**；独立进程整文件 **134 passed**，三处全局对象身份均恢复；patch 与 diff 检查通过，五文件哈希不变 | JSON 接受范围和旧 schema 未扩大；不强行合并解析策略；仅放行 Step4 |
| Step 4 / R4 | **已通过**（2026-10-15 主审；仅放行 Step5） | `backend/utils/aggregate_utils.py`、`backend/services/viz_service.py`、`backend/tests/test_char_name_suffix_source.py`(新)、`backend/tests/test_book_name_rules.py`(新) | 主审相关集 **268 passed in 5.19s**；既有书名推断/DOCX/原子聚合集 **99 passed in 3.54s**；patch 与 diff 检查通过，五文件 SHA256 不变 | 后缀内容与顺序不变，空白/纯后缀差异保留；书名生产逻辑未改，不强行合并；未运行真实修复脚本 |
| Step 5 / R5 | **已通过**（第三轮主审，2026-10-15）；不放行 Step6 | `backend/tests/test_service_lifecycle_matrix.py`(新)；两个生产服务本步未修改 | 主审关联集 **88 passed in 6.75s**；禁用真实 fixture 后守卫按预期失败，探针任务已清理；patch 与 diff 检查通过，五文件哈希不变 | 等待、登记、真实 teardown 守卫已落实；保留生产服务重复；Step6 等待用户讨论 |
| Step 6 / R6 | **未开始，用户要求先讨论**（2026-10-15） | - | - | 不自动放行，不修兼容或迁移字段；回访已暂停 |

只有实际实施并验证过的任务才勾选。全部范围完成或明确记录保留/延期且满足最终验收后，才将 plan 状态从 `draft` 改为 `done`。

### 11.1 Step 0 / R0 实施记录（2026-10-14）

**状态：R0 已通过（主审第二轮复核），已放行 Step1。** 第一轮未通过 → 重写隔离方案 → 复验通过。

**主审独立验证证据（R0 通过依据）**

- 隔离/归档定向测试 12 passed；指定节点 `test_child_preserves_sentinel_log_in_original_cwd` 1 passed。
- 默认收集 496（与我方一致）。
- 五个真实文件 SHA256 均未变（`config.json` / `queue_state.json` / `analyzer.log` / `crash.log` / `api_failures.log`）。
- `docs/patches/step0-r0.patch` 含 `pytest_isolation.py`、`conftest.py`、`test_runtime_isolation.py`、`test_archive_path_update.py` 四个文件，且 `git apply --check -R` 通过。
- `git diff --check` 通过。
- 主审要求保留的历史：第一轮「旧日志被写后截断」事件必须继续保留为历史记录；第二轮确为写前隔离（见下方「关键事件」，第二轮起不再依赖事后还原）。

**R0 第一轮被拒原因（阻塞项 P1）**

主审指出：`conftest.py` 的日志隔离是「写后移除」而非「写前隔离」。

1. 原 `_redirect_api_failures_logger` 先 `import backend.core.llm_failure_logger`，模块顶层 `FailureLogger()` 已按真实 cwd 创建/打开 `api_failures.log`，之后才移除 handler；若默认文件原本不存在，仅 collection/import 也会创建真实文件。
2. 原 `_enforce_api_failures_isolation` 只在用例前后调用，测试内部重新 `FailureLogger()` 仍会按真实 cwd 建 handler 并在 `record_failure` 中写入。
3. 主审用「完全临时的外层 cwd + importlib 加载现有 conftest + `FailureLogger().record_failure(...)`」复现，得到 `default_log_created_by_isolation_import=True`、`default_log_grew_inside_test=True`、`failure_handlers_outside_isolation=1`。

**修正方案**：改为 import 前的 cwd 隔离（详见 §3 的 R0 前置清单），并在守卫测试中补齐子进程级回归与正向对照。复验结果见下。

**基线（执行前）**

- 真实 HEAD：`7e70c8395bd5a3e1eecc074453f77369b8e520ac`；分支 `main`（ahead 10）。
- 工作区已含其他会话未提交改动（`CHANGELOG.md`、`agent.md`、`backend/api/routes_settings.py`、`desktop.py`、若干 `SettingsPage/SummaryPage`/API/会话鉴权文件，以及未跟踪的 `test_settings_probe_route.py`、`docs/PLAN-provider-error-taxonomy-2026-09-30.md`、`frontend/src/api/sessionToken.*`），本步未触碰、未还原、未提交。
- 环境：Python 3.12.12、pytest 9.1.1；`LLM_API_KEY` / `MINIMAX_API_KEY` / `NOVEL_ROOT` / `NOVEL_ANALYZER_SESSION_TOKEN` 均未设置（仅记布尔）。Ruff 缓存版本 `0.16.9`。
- 真实文件基线哈希（SHA-256）：`config.json=2A943AD7…A1305`、`queue_state.json=8552CA41…A195A`、`analyzer.log=CB2B026A…D4C14`、`crash.log=95D221D2…626F`、`api_failures.log=AC2CB821…B76B5`；真实 `workspace/` 两个书目录（事后递归清单 4436 个文件）。

**修改文件（R0 第二轮）**

- `pytest_isolation.py`（新增，根目录）：隔离原语独立成模块。原因：仓库根与 `tests/` 各有一个 `conftest.py`，`import conftest` 会随收集范围解析到不同模块对象（全量运行时曾因此 `AttributeError`）。`install()` 在任何 `backend.*` import 之前 `os.chdir` 到会话临时根、设 `NOVEL_ROOT`、写合成 `config.json`、剥离密钥环境变量、建 `testpaths` 占位目录；提供 `assert_isolation_intact()` / `api_failures_handlers_outside_isolation()` / `_is_null_device()` / `reset_runtime_singletons()` / `cleanup()`。初始化异常一律抛出，不再吞。
- `conftest.py`（新增，根目录）：只做 `pytest_isolation.install(REPO_ROOT)`；autouse fixture 在用例前后 `assert_isolation_intact()`，用例后重置进程级单例与 `book_service` 缓存。
- `backend/tests/test_runtime_isolation.py`（新增）：进程内契约（cwd/`NOVEL_ROOT`/`PROJECT_ROOT`/`CONFIG_FILE`/`QUEUE_STATE_FILE`/`LOG_FILE`/合成配置）+ 独立子进程回归（原始 cwd 无默认日志 / 存在 sentinel 两场景；首次 import + 用例内新建 `FailureLogger().record_failure`）+ 正向对照（真实泄漏必被抓、nul 设备 handler 精确排除）。
- `backend/tests/test_archive_path_update.py`（修改）：两个用例补齐 `queue._items` 与 `book_service._books`/`_manual_books` 还原，并新增 `QUEUE_STATE_FILE.parent == NOVEL_ROOT` 断言。
- 仅含本步的补丁：`docs/patches/step0-r0.patch`（LF；`git apply --check -R` 退出码 0，与工作区逐字一致）。三个新文件用 `git diff --no-index -- /dev/null` 呈现，未漏未跟踪文件。

**测试与运行隔离根**

| 命令 | 结果 | 退出码 |
|---|---|---|
| `python -m pytest --collect-only -o addopts=''` | **496 tests collected** | 0 |
| 目标组1 streaming/llm_mock/json_repair/summary_checkpoint/style_tokens/style_task_stop | 71 passed in 74.80s | 0 |
| 目标组2 models/memory_state_rolling/foreshadow*/book_name*/location_normalization*/auto_summary/session_token_stats | 161 passed in 5.09s | 0 |
| `python -m pytest backend/tests/test_runtime_isolation.py backend/tests/test_archive_path_update.py` | 12 passed | 0 |
| `python -m pytest test_streaming/test_llm_mock/test_provider_heal/test_anthropic_provider/test_moderation`（易泄漏子集） | 72 passed, 2 warnings | 0 |
| `python -m pytest test_settings_probe_route/test_session_token_auth/test_book_token_stats/test_session_token_stats/test_location_normalization_e2e/test_workspace_scan/test_book_service_async_refresh` | 19 passed | 0 |
| `python -m pytest -o addopts='' -q`（全量，含 `tests/`） | **496 passed, 2 warnings** in 91.42s | 0 |
| `npm test`（frontend，第一轮基线，本轮未改动前端故不重跑） | 32 files / **249 passed** | 0 |
| `npx vue-tsc --noEmit`（frontend，同上） | 无输出（通过） | 0 |
| `uvx ruff@0.16.9 check backend desktop.py --select F401,F811,F841,F821,F822,F823 --exclude backend/tests` | All checks passed | 0 |
| `git diff --check` | 通过（仅既有 LF/CRLF 提示） | 0 |

- 隔离运行根：`%TEMP%\novel-analyzer-pytests-*`（会话级临时目录，运行结束后自动删除，无残留：`leftover_temp_dirs=0`）。
- **主审复现方式复验**（临时目录作原始 cwd + importlib 加载隔离原语 + 首次 import + 用例内新建 `FailureLogger().record_failure`）：`default_log_created_by_isolation_import=False`、`orig_cwd_files=[]`、`handlers_outside=[]`，两个 handler 均指向隔离根。三个原始判据全部为 false/空。
- 本轮未重跑前端套件（本轮改动不触及 frontend），沿用第一轮 249 passed 与 vue-tsc 通过记录。
- 2 个 warning 为既有：`backend/tests/test_moderation.py` 两例 `RuntimeWarning: coroutine ... was never awaited`（`backend/core/pipeline.py:653`），与本步改动无关。
- 未执行项：`backend/scripts/fix_metadata_titles.py`（按约束不执行）；真实 LLM 调用（无 mock 的探针不跑）。

- **第一轮事件（已由本轮方案替换）**：首次全量运行时 `api_failures.log` 被追加 14038 字节，根因是「写后移除」顺序错误；当时已逐字节还原至基线长度 5775334、SHA-256 与基线一致（仅 mtime 变化）。第二轮起不再依赖"事后还原"——cwd 隔离下不再产生该写入；本轮全部运行后五个真实文件哈希仍与基线一致。
- 真实 `config.json` / `queue_state.json` / `analyzer.log` / `crash.log` 在全部运行前后哈希不变；真实 `workspace/` 顶层两目录时间戳不变（执行前只记录顶层时间戳、未做全量哈希，内容级零写入由隔离设计保证，全量哈希留给 Step6）。
- 保留：`backend/tests/test_queue_service.py::test_on_progress_block_start_publishes` 的复制闭包本步**未改**（属 Step1 1B）；`WSType.PING`、`CharacterCardGenerator.long_context_insights`、`KnowledgeBase._legacy_text` 往返缺陷本步均未改（分别属 Step1/Step1/Step6）。
- 临时根通过 `NOVEL_ROOT` 生效，未改 cwd；`tests/` 顶层用例用 `__file__` 相对路径，不受影响。

**自查 review / 前置条件**

- 隔离在 collection 前生效（守卫测试可证）；真实文件零改动；失败测试无一被禁用或跳过。
- 尚存问题：`api_failures.log` mtime 在第一轮还原时已变（内容一致）；R0 未覆盖真实历史 `workspace/` 旧 schema 的读写（属 Step6 样本范围）。
- 新增的 nul 设备排除仅限 `api_failures` 的 `baseFilename` 判定，且有正向对照用例证明真实文件 handler 仍会被抓到，不会变成放行旁路。
- Step1 前置条件已具备：基线可复现（收集 496）、目标模块无未解释失败、本步 diff 可独立审查。

### 11.2 Step 1 / R1 实施记录（2026-10-14）

**当前状态**：R1 第三轮主审已通过（2026-10-15），仅放行 Step2。第一轮、第二轮未通过与返修证据保留如下；通过不代表第三批整体完成。

**第三轮主审证据**：独立六文件 targeted suite **61 passed in 7.12s**，退出码 0；隔离进程观察断连分支，原测试连续 10 次通过、真实断连分支命中 10 次；Step1 patch 反向检查与 `git diff --check` 通过，五个真实文件 SHA256 在测试前后与 R0 基线一致。未重跑全量，528 passed / 2 warnings 仍是上一轮实施方的全量结果。

**R1 第一轮未通过原因（返修依据）**

主审独立运行六文件 targeted suite：66 passed，exit 0；`git diff --check` 通过；五文件 SHA256 与 R0 基线一致；业务 diff 未发现运行时缺陷。但有以下验收缺口：

1. **P2**：`test_ws_protocol_ping.py` 的源码子串断言与本地 heartbeat 字典**不是**生产 `ws_progress` 的运行时契约测试——即使生产不再发心跳、或在发送前改变形状，仍可能全绿。→ 已重写为以真实 `ws_progress` 为入口的运行时测试（FakeWebSocket + 极小 `_HEARTBEAT_INTERVAL` 确定性触发超时分支），并用变异检查验证：给心跳加 payload / 移除心跳 / 包裹广播三种变异下均失败。
2. **P3**：§11.2 与报告把字符规则写反了。事实是 `character_card_generator.py:188` 归一 U+FF0C、`pipeline_events.py:53` 归一 U+3001。→ 已纠正（新测试注释/fixture 原本就是对的）。不改生产规则、不顺手统一，差异保留到 Step4。
3. **P3**：总状态表 Step0/Step1 仍显示"未开始"，与细节冲突。→ 已同步为 R0 通过 / R1 返修中。
4. **P3**：新加的生产类注释过长（含日期/Step 编号/缺陷复盘）。→ 已收敛为简短契约说明（三处），历史与证据保留在报告。

**R1 第二轮未通过原因（二轮返修依据）**

主审独立运行六文件 targeted suite：61 passed，exit 0；Step1 patch 反向 check 0；五文件 SHA256 前后与 R0 一致。第一轮四项中「真实心跳消息测试、字符规则、状态表、生产注释」已落实。剩余缺口：

1. **P2 断连未真覆盖**：旧 `test_disconnect_unsubscribes` 用 `fail_after=1`（第二次 `send_json` 才抛断连），但 `run(socket)` 默认 `wait_sent=1` 在第一条 ping 后就 cancel 任务。主审在隔离进程里 patch `DisconnectingWebSocket.send_json` 统计：连续跑原测试 10 次，`passing_disconnect_test_runs=10`、`actual_disconnect_branch_hits=0` —— 只验证了 cancel 后的 finally，没覆盖真实 WebSocketDisconnect 分支。
   → 已重写：`DisconnectingWebSocket` 在**第二次发送时立即抛断连**并设置 `disconnected` 事件作为可观察标记；测试以该标记为前提，用 `asyncio.wait_for(task, timeout=5.0)` 等协程**自然返回**（`except WebSocketDisconnect → finally`），不以 cancel 代替断连。另设 `test_cancel_path_also_unsubscribes` 作为对照组，明确区分「断连自然返回」与「外部 cancel」两条路径；`cancel()` 仅在等待超时/失败时做资源清理。helper 与 feeder cleanup 只抑制预期 `asyncio.CancelledError`，不再吞所有 `Exception`。
   → 用主审同法复核（隔离进程 patch `send_json` 记 hit）：`passing_disconnect_test_runs=10`、`actual_disconnect_branch_hits=10`。
2. **删除自证哨兵**：`test_no_ping_when_heartbeat_disabled` 把测试 transport 的 `send_json` mock 成 no-op 再断言没记录，属自证循环，不是生产变异检测 → 已移除（已有真实发送测试 + 变异结果）。
3. **P3 表述不实**：文件 docstring 与报告声称「不 sleep」，实际 `run` 与 `feed` 里有 `asyncio.sleep` 轮询 → 等待改为事件驱动（`asyncio.Event`），表述改为「不等待真实 25 秒」；`feed` 里的轮询改为 `await asyncio.sleep(0)` 让出控制权配合 `subscribed` 事件。
4. **P3 报告末尾重复条目**：「主审应核查的问题」第 3 条 `agent.md` 重复两行 → 已去重。

**二轮返修后的变异检查（脚本用完即删）**：给心跳加 payload → `2 failed, 3 passed`；移除心跳发送 → `4 failed, 1 passed`；包裹广播加壳 → `1 failed, 4 passed`；**去掉 finally 的 unsubscribe（新增）** → `2 failed, 3 passed`。四种变异 rc 均为 1，变异后已还原 `ws.py`（`git diff` 复核仅剩本步 4 行改动）。

**开始快照（R0 通过后、Step1 前）**

- HEAD `7e70c8395bd5a3e1eecc074453f77369b8e520ac`（未 commit / push）。
- 五个真实文件 SHA256 与 R0 基线一致。
- `git status --porcelain` 快照：`docs/patches/_snapshot_step1_start_status.txt`；归档测试当时的 diff：`docs/patches/_snapshot_step1_start_archive.diff`。

**1A 角色卡无用读链（已完成）**

- `backend/utils/character_card_generator.py`：删除 `__init__` 的 `self.long_context_insights` 与 `load_aggregated_data` 中读取 `long_context_insights_aggregated.json` 的整段；类文档补充删除理由。
- 新增 `backend/tests/test_character_card_generator.py`（16 例）：真实生成器 + 真实路由入口。覆盖正常索引排序/详情字段；洞察文件缺失/损坏/空数组/空对象/正常五种形态下索引与详情照常；空目录索引空列表、详情 `KeyError`；不存在角色 `KeyError`；arcs 缺 `driver` 仍 `KeyError`；tracking 损坏仍 `KeyError`；`output/aggregated/` 嵌套目录；路由 404 / 200。
- 顺带锁定的现状契约（不改）：`backend/utils/character_card_generator.py:188`（`_analyze_relationships`）归一的是**全角逗号 U+FF0C**（`replace("，", ",")`）；而 `backend/services/pipeline_events.py:53`（`_extract_discovery`）归一的是**顿号 U+3001**（`replace("、", ",")`）。两者互不一致，已用 fixture 与专门用例固定为现状，差异保留到 Step4 名称规则审计，不顺手统一。

**1B 生产回调工厂测试（已完成）**

- `backend/tests/test_queue_service.py`：删除复制闭包 `on_progress_like`，改为 `test_factory_callbacks_publish_block_start_and_done` 直接调用 `make_pipeline_callbacks`。
- 新增 `backend/tests/test_pipeline_callbacks_factory.py`（10 例）：结构化 `range` 优先 / `message` 回退、`block_start` 字段与顺序、DONE/FAILED/SKIPPED 三态转发与统计、`log` 级别（仅 FAILED 为 error）、discovery 去重、`on_token_stats` 累加与 unknown 归类、ETA 由 `item.start_time` 推算的哨兵。
- 已核实的既有事件顺序（写入用例）：带 `message` 时 `log → progress → block_start`；不带 `message` 时 `progress → block_start`；终态为 `block_done [+ discovery]`（不带 `message` 时无 `log`）。

**1C PING 与文档（已完成）**

- `backend/ws_events.py`：恢复 `WSType.PING = "ping"`，类文档注明「心跳是唯一不带 payload 的消息」这一既有例外。
- `backend/api/ws.py`：心跳改为 `{"type": WSType.PING}`（线上形状不变，仍不带 payload），并 import 常量；模块文档字符串同步。
- 新增 `backend/tests/test_ws_protocol_ping.py`（10 例）：常量存在与取值、服务端引用常量（源码级）、心跳 JSON 无 payload、其余 7 类事件均带 payload。
- `agent.md`：依赖表 `networkx + pyvis` 标为**历史依赖、当前无消费者**（backend 无 import、`requirements.txt` 未列、关系图由 `viz_service.graph_data` 自行聚合）；测试示例 `test_pipeline.py`（不存在）改为 `test_pipeline_inflight.py`。仅叠加必要内容，未改写历史记录。

**验证（详见报告，二轮返修后）**：PING 5 passed；六文件 targeted **61 passed**；Ruff（既有选择集）生产代码与新增测试文件均通过；`git diff --check` 通过；五个真实文件哈希不变；临时隔离根无残留。
- 说明：二轮返修只改 `test_ws_protocol_ping.py`（5 例，用例数不变）与文档，**上一轮全量 528 passed / 2 warnings** 仍作为上一轮的证据；本轮因改动范围受限未重跑全量，诚实区分。

### 11.3 Step 2 / R2 实施记录（流式 Token 进度回调收敛）

**当前状态**：R2 第三轮主审已通过（2026-10-15），仅放行 Step3。主审独立目标集 **82 passed in 7.41s**；隔离进程切断真实 retry 链的 on_progress 传递，新用例按预期失败（pytest 返回码 1）。Step2 patch 反向检查、diff 检查通过，五个真实文件 SHA256 前后与 R0 基线一致。未重跑全量；前两轮未通过与返修证据继续保留。

**开始快照（R1 通过后、Step2 前）**

- HEAD `7e70c8395bd5a3e1eecc074453f77369b8e520ac`（未 commit / push）。
- 五个真实文件 SHA256 与 R0 基线一致；`git status --porcelain` 快照：`docs/patches/_snapshot_step2_start_status.txt`。

**抽取前基线（受控时钟 `time.monotonic`，三条生产路径逐字段对照）**

```
analyzer : [('analysis',        '测试书', 7, 40, 80.0, 0.5), ('analysis',        '测试书', 7, 100, 100.0, 1.0)]
summary  : [('summary_phase_1', '测试书', 3, 40, 80.0, 0.5), ('summary_phase_1', '测试书', 3, 100, 100.0, 1.0)]
style    : [('summary_phase_3', '测试书', 0, 40, 80.0, 0.5), ('summary_phase_3', '测试书', 0, 100, 100.0, 1.0)]
```

元组含义 `(context, session_id, unit_idx, output_tokens, rate, elapsed_sec)`；三处主体完全一致，差异仅在 context / session_id / unit_idx 的来源。**抽取后复跑同一探针，输出逐字段相同。**

**修改文件（仅本步，不含 Step0/Step1）**

| 文件 | 类型 | 修改目的 |
|---|---|---|
| `backend/core/stream_progress.py` | 新增 | 工厂：`make_stream_progress_callback(hub, context, session_id, unit_idx=0)`；只收敛共同主体，不接管重试/并发/计费 |
| `backend/tests/test_stream_progress.py` | 新建 | 工厂单测 8 例 + 三条生产路径真实入口 7 例 + 真实 LLMClient 重试链 2 例 + 时钟/回调隔离 2 例 + 其余单测 1 例，共 20 个用例 |
| `backend/core/analyzer.py` | 修改 | 手写闭包 → 工厂调用；移除因此不再使用的 `import time` 与 `from .llm_stream import StreamChunk` |
| `backend/core/style_analyzer.py` | 修改 | 手写闭包 → 工厂调用（`context="summary_phase_3"`、`unit_idx=0`） |
| `backend/services/final_summary.py` | 修改 | `_make_on_progress` 改为薄适配器（保留四个调用点形态，计时仍在并发槽之后） |

**R2 二轮主审（2026-10-15）与窄返修**

- **已通过（保留）**：目标集 **81 passed in 6.59s**，exit 0；隔离 runtime 探针禁用六个生产入口后为
  **8 failed / 12 passed**，pytest 返回 1，证明六入口测试有效；工厂逻辑无回归；四个总结入口的排队时间测试已覆盖；
  patch reverse-check 与 diffcheck 通过；五文件 hash 与 R0 一致。
- **唯一剩余 P2**：缺少至少一来「真实入口 → 真实 `LLMClient.chat_auto` /
  stream retry 链」。上轮的两个 retry 用例是测试自建 `make_callback` 后直接
  `client.chat_auto`，不经生产入口；第一尝试只 error 无可比 token 事件；耗尽例只断言无事件；
  两者均未记录 attempt 拿到的 callback identity。
- **窄返修**：新增 `test_analyzer_entry_retry_reuses_callback_across_attempts`。实际调用
  `analyze_chapter` + 真实 `LLMClient`，保持真实 `chat_auto` /
  `chat_stream_with_retry` / `_run_stream_attempt`，只 mock 低层 `chat_stream` 与
  `_sleep`。第一 attempt 先发 partial content 再发 error（有可比 token 事件），
  第二 attempt 发合法响应并成功。spy 包装生产工厂记录只创建一次；用
  `inspect.currentframe()` 从 `_run_stream_attempt` 帧读取每次 attempt 的 `on_progress`
  对象 id，断言两次为同一对象且即入口创建者。受控时钟推进第一尝试/退避/第二尝试，
  断言 `elapsed` 跨 attempt 不重置且包含退避。
- **三层区别（文档不得混称）**：Mock LLM 入口测试（单 attempt）/
  直接 LLMClient 测试（真实重试链但 callback 自建）/真正贯通的入口 retry
  （新用例）。`test_retry_exhaustion_keeps_same_callback` 保留乆定位为 error 过滤测试。
- **结果**：单文件 **21 passed**；目标集 **82 passed**；Ruff 通过。隔离进程变异（重建
  callback / 计时重置 / 丢弃 callback / `unit_idx` 丢失 / retry 不转发
  `on_progress`）均被杀。
- Step2 patch 已重新生成（`docs/patches/step2-r2.patch`，LF，39251 字节），
  `git apply --check -R --whitespace=nowarn` 退出码 0；五文件 SHA256 与 R0 一致。


**R2 一轮被拒事实（保留）**

- 主审把 `NovelAnalyzer.analyze_chapter`、`style_analyzer.call_llm_semantic`
  与四个 `_call_llm_*` 全部 patch 成立即 `AssertionError`
  后，测试文件仍 **14 passed**，证明入口测试从未进入这六个方法。
- 被拒的入口测试均为“假验证”：读源码字符串、直接调工厂、
  只比全文件首个 acquire/make 位置、只对同一回调调两次。

**二轮返修内容（2026-10-15）**

1. 修正 `test_style_entry_real_call` 的 patch 目标：`style_analyzer` 的
   `get_hub` 为函数体内 import，应 patch `backend.progress_hub.get_hub`。
2. 修正 `_call_llm_reconciliation` 的期望 context 为 `summary_phase_1`
   （生产代码 `final_summary.py:676` 的真实值）。
3. 删除伪入口用例，换成真实入口用例：真实调用
   `analyze_chapter` / `call_llm_semantic` / 四个 `_call_llm_*`，由 mock LLM
   经 `chat_auto` 接到的 `on_progress` 驱动 chunks。
4. 新增真实重试链用例：只 monkeypatch 低层流源
   `LLMClient.chat_stream`，保持 `chat_stream_with_retry` 与
   `_run_stream_attempt` 的重试、退透与 `on_progress` 扇出为真。
5. 精简工厂 docstring（去旧日期与“章节=block_idx”错述，统一为
   `chapter_number`）；清理上轮遗留的 `_find_lines.py`。

**返修后验证（隔离进程 runtime patch，未改工作区源码）**

- 单文件 **20 passed**；Step1+Step2 目标集 **81 passed**；风险子集 **53 passed**；
  后端全量 **533 passed, 2 warnings**（相对上轮 527 增加 6 个新用例）。
- 六种工厂变异（`unit_idx` 固定 0、计时重置、广播 reasoning、
  去掉 unknown 回退、忽略 context、二次累加）全部被杀。
- 三处入口变异（工厂返回 `None`）同样被杀，证明入口测试
  确实走过三条生产路径。
- 五个真实文件 SHA256 与 R0 基线一致；`leftover_temp_dirs=0`；无 `_*.py` 遗留。
- `docs/patches/step2-r2.patch` 已重新生成（LF，`git apply --check -R --whitespace=nowarn` 退出码 0）。

**保留的契约（对应计划 §5 与 §2.1 第 7 条）**

- 计时用独立 `time.monotonic()`，创建位置等同原 `started_at` 的位置；每次调用互不共享计时器/累计状态，重试不重置、不前移。
- `output_tokens` 取 `estimated_total_tokens or 0`（累计值由 LLMClient 维护，**不**二次累加）；速率 `(estimated_total_tokens or 0) / max(elapsed, 0.1)`。
- 过滤不变：只广播 `content` / `usage`，`reasoning` / `error` 不新增广播；**不动** LLMClient 里 reasoning → content 的上游转换。
- `session_id` 缺失回退 `"unknown"`；`unit_idx` 章节=chapter_number、总结=batch_idx、风格固定 0。
- `await hub.broadcast_token_delta(...)` 与 ProgressHub 节流入口完全不变（测试用 RecordingHub 只替换记录，不改节流）。
- 总结回调创建位置：源码顺序断言 `await self._acquire_llm_slot()` 必须早于 `self._make_on_progress(`。

**验证（详见报告）**

- 单文件 **20 passed**；Step1+Step2 目标集 **81 passed**；风险子集（streaming/style_tokens/style_task_stop/summary_checkpoint/progress_hub_throttle/llm_mock）**53 passed**；后端全量 **533 passed, 2 warnings**；Ruff 两条命令通过；`git diff --check` 通过；五个文件 SHA256 均未变化。
- 变异检查（脚本用完即删）：analyzer 改回手写闭包 → 1 failed；style `unit_idx` 改 1 → 1 failed；总结回调前移到 acquire 之前 → 2 failed；工厂过滤全放开 → 3 failed。四种 rc 均为 1。
- Ruff（既有选择集）生产代码 + 新增测试文件通过；`git diff --check` 通过；五文件 SHA256 不变；临时隔离根无残留。

**1C 返修后的 PING 测试（运行时契约）**

- 被测入口是真实 `backend/api/ws.py::ws_progress`；FakeWebSocket 记录实际 `send_json`；`_HEARTBEAT_INTERVAL` 用 monkeypatch 压到 0.01，不等待真实 25 秒。断连用例等待可观察的断连标记与协程自然返回；取消清理另设独立用例。广播用例保留 `asyncio.sleep(0)` 让出控制权，并未实现虚拟时钟。
- 断言：实际发送对象严格为 `{"type": "ping"}`（只有一个 `type` 键、无 payload）；`type` 与 `WSType.PING` 同源；普通队列广播原样透传；disconnect 后 `hub._queues` 清空（unsubscribe 生效）；禁用心跳时不得再记录消息（哨兵）。
- **变异检查（脚本用完即删）**：给心跳加 payload → `1 failed, 4 passed`；移除心跳发送 → `2 failed, 3 passed`；包裹广播加壳 → `1 failed, 4 passed`。三种变异 rc 均为 1，证明绑定的是运行时行为而非源码格式。变异后已还原 `ws.py`。


### 11.4 Step 3 / R3 实施记录（JSON 解析限制收敛，2026-10-15）

**当前状态**：R3 第三轮主审通过（2026-10-15），仅放行 Step4。主审同进程 boundaries/streaming/stream_progress/style_tokens/style_task_stop **185 passed in 54.80s**，退出码 0；独立进程整文件 134 passed 后三处原对象身份均恢复。schema-invalid 首轮响应被真实 validator 拒绝，第二轮包含真实错误 hint 并成功。Step3 patch 反向检查、diff 检查通过，五文件 SHA256 前后与 R0 基线一致。未重跑全量；前两轮被拒与返修证据继续保留。
**未进入 Step4**。

**起始快照**：HEAD `7e70c83`，未 commit/push；五文件 hash 与 R0 一致；
`docs/patches/_snapshot_step3_start_status.txt`。

**覆盖范围**：`analyze_chapter` + 其 `validate_json_response`、
schema 校验失败→成功的真实重试链、`call_llm_semantic`、`_parse_llm_dict`、
`extract_foreshadow_json` 均为**运行时**验证；
pipeline 三处 rolling 站点为**只读审计**，未称为运行时矩阵。

**mock 作用域**：因 R3 二轮发现的泄露，全部 mock 改为
`monkeypatch.setattr` / `pytest.MonkeyPatch.context()`；新增两个防护用例：
`test_production_objects_not_leaked_by_this_module`（整文件跑完后比较原对象身份）与
`test_style_helper_restores_after_each_outcome`（四种路径立即恢复）。
隔离进程实测：`chat_stream_restored_after_file = True`、
`style_LLMClient_restored_after_helper = True`；反向引入旧泄露写法时防护用例失败。

**现状矩阵**：见报告 Step 3 节。关键点：
`safe_parse_json` / `parse_json_robust` 只返回 dict/None；裸数组可接受而围栏数组失败；
对象选择取决于 repair（`{} {"a":1}` → `{"a":1}`，`{"zzz":1} {"a":2}` → `None`）；
两处 parse-first 与两处 extract-first 分别在空 dict 上分歧。

**生产变动**：仅删除 `extract_foreshadow_json` 中不可辿的
`isinstance(data, list)` 分支（R3 明确授权）。保留 dict guard、parse 顺序、truthiness、
旧 schema 转换。未让 helper 返回 list，未扩大接受范围。

**修改文件**：`backend/tests/test_json_parse_boundaries.py`（新建，134 个用例）+
`backend/services/final_summary.py`（仅 list 分支 hunk，与 Step2 diff 分离）。
独立 patch：`docs/patches/step3-r3.patch`（LF，43025 字节），`git apply --check -R` 退出码 0，
已在新建 clone 实测应用成功。

**测试**：单文件 **134 passed**；JSON 相关 **164 passed**；风险集 **99 passed**；
**同进程相邻集（两种顺序）均 185 passed**；Ruff 两条命令通过。

**变异**：入口禁用（analyzer / style / 两者）分别造成 6 / 23 / 29 failed；
helper 返回 list → 9 failed；helper 取首 dict → 6 failed；location 改 truthiness → 3 failed；
重引泄露写法 → 防护用例 1 failed。

**未执行**：未调用真实 LLM；未重跛后端全量；未涉及 frontend / splitter；
pipeline 站点仅只读审计。
### 11.5 Step 4 / R4 实施记录（名称规则审计，2026-10-15）

**当前状态**：R4 主审通过（2026-10-15），仅放行 Step5。主审独立相关集 268 passed in 5.19s、既有书名推断/DOCX/原子聚合集 99 passed in 3.54s，均退出码 0；Step4 patch 反向检查与 diff 检查通过，五文件 SHA256 前后与 R0 基线一致。仅共享重复后缀 tuple；书名生产逻辑未改，各调用方行为差异保留。未重跑全量、未执行真实修复脚本。
开始前已存储 `docs/patches/_snapshot_step4_start_status.txt`；
`aggregate_utils.py` 与 `viz_service.py` 开始时均为 HEAD 干净，故这两个文件的 `git diff` 只含本步改动；
`final_summary.py` 开始时已脏（Step2 工厂 + Step3 list 分支删除），本步未再改它。

**4A 角色名后缀：单一来源已建立。**

- `backend/utils/aggregate_utils.py`：`_CHAR_NAME_SUFFIXES` 改名导出 `CHAR_NAME_SUFFIXES`，
  内容与顺序逐字节保留；`_normalize_char_name` 改为引用它。
- `backend/services/viz_service.py`：删除 `_aggregate_chars_edges` 内重复的局部字符串列表，
  改为 `for suffix in CHAR_NAME_SUFFIXES:`。
- **保留的边界行为差异（不随共享默认统一）**：聚合先 strip 且纯后缀返回原名；
  图谱仅 return 时 strip 且纯后缀返回 `""`。已在涐码和测试中注释。

**4B 书名：审计完成，不合并。**

- `splitter_service.infer_book_name`（文件名推断）与 `final_summary._clean_book_name`（元数据/目录名清洗）
  **不等价**：21 个样本中 10 个结果不同。
  `infer_book_name` 保留《》并剥版本后缀；`_clean_book_name` 剥《》【】与尾部括号备注但不剥版本后缀。
  合并会同时改变两类调用方，故不做提取。
- `_looks_valid_book_name` 在 splitter 侧无对应，保留。
- `backend/scripts/fix_metadata_titles.py` 直接 import `_clean_book_name` / `_looks_valid_book_name`，
  故这两个名字必须保留；测试仅用 `ast` 校验 import 关系，**不执行真实修复脚本**。
- **已知缺口（记录，不修）**：`_NON_TITLE_PREFIXES` 不含「连载」「新书」，
  因此 `_looks_valid_book_name("连载")` 与 `"新书"` 返回 True。已用专门用例锁定该现状。
- **记录的怪异现状（保留不修）**：`_clean_book_name("《》")` → `""`；
  `_clean_book_name("《魔道祖师》【精编】")` → `"魔道祖师》【精编"`（只剥首尾各一层）；
  `infer_book_name("《》.txt")` → `"《》"`；`_looks_valid_book_name` 对含「作者」/「著」的任意位置即拒。

**测试**：`test_char_name_suffix_source.py` **20 passed**（含真实生产入口回归）；
`test_book_name_rules.py` **98 passed**。相邻风险集（角色卡/聚合/splitter/总结/图谱/JSON）合计 **306 passed**。
Ruff（F401/F811/F841/F821/F822/F823）通过。

**变异（隔离进程 runtime patch，不动共享源）**：viz 自带副本 1 failed；删后缀 10 failed；倒序序 2 failed；
`_clean_book_name` 不剥书名号 11 failed；`_NON_TITLE_PREFIXES` 删项 2 failed；补项 2 failed；
`infer_book_name` 不剥版本后缀 10 failed。

**未执行**：未调用真实 LLM；未违后端全量；未执行 `fix_metadata_titles.py`；未提交代码。
`docs/patches/step4-r4.patch`（LF，29032 字节）仅含上述四个文件，
`git apply --check -R --whitespace=nowarn` 退出码 0；五个真实文件 SHA256 与 R0 基线一致。
### 11.6 Step 5 / R5 实施记录（生命周期重复审计，2026-10-15）

**最新主审状态**：R5 第三轮通过（2026-10-15）。主审关联集 **88 passed in 6.75s**；隔离进程禁用真实 fixture 后守卫按预期失败（pytest 返回码 1），观察后探针任务全部清理完成。服务登记与真实 teardown 测试已补齐，普通清理错误保留为失败证据。patch/diff 检查通过，五文件 SHA256 前后与 R0 基线一致。用户明确要求 Step6 先不进行；自动回访已暂停。下面的第一轮主审及交付记录继续保留为历史。

**当前状态**：R5 第一轮主审未通过（2026-10-15），仅返修测试等待/清理助手，不放行 Step6。生产服务未改，保留合理重复的决定可接受。主审独立关联集 85 passed in 5.17s；隔离探针令两个 runner 永不完成后，泄漏守卫仍 1 passed，证明超时被助手吞掉并主动取消。详见报告「R5 第一轮主审」。
开始前已存储 `docs/patches/_snapshot_step5_start_status.txt`；
两个生产文件本步**未修改**，因此 Step5 patch 只含一个新测试文件。

**审计结论**：四个小片段字字相同（`is_running` 属性、书目存在护关、
有章节结果护关、每次启动的状态复位块），但剩下的全部分遙：
`start()` 签名、runner 构造、`on_progress` 体（WS 类型、日志分类、
`batch_failed` 级别、phase 处理）、token 统计回调、终态映尌。
`SummaryService` 还多出分析互斤、报告写盘与会话 token 累计，归一化没有对应物。
共享这四块需要 runner 类型分支或回调开关，计划禁止，故**保留重复**并用具体矩阵说明。

**矩阵覆盘**（`backend/tests/test_service_lifecycle_matrix.py`，23 个用例，全部调真实服务方法）：
重复 start、未运行 stop、进行中 stop、异常、完成后再启动、
自动总结入口（`allow_during_analysis`）、会话 token 只累计一次、
归一化 `True`/`None`/`False` 三态、LLM 配置 replace、事件序列、
后台 task 与 mock 作用域恢复。

**测试**：单文件 **23 passed**；相邻服务集（归一化/e2e/自动总结/会话 token/normalizer）
合计 **84 passed**。Ruff（F401/F811/F841/F821/F822/F823）通过。

**变异**：总结终态 phase 5 failed；归一化三态分支 2 failed；
`stop()` 置空 3 failed；`start()` 置空 21 failed；
关掉 token 累计 5 failed；去掉 summary 模型 replace 1 failed。

**未执行**：未调用真实 LLM；未跑后端全量；未涉及 frontend；未提交代码。
`docs/patches/step5-r5.patch`（LF，仅一个新测试文件），
`git apply --check -R --whitespace=nowarn` 退出码 0；五个真实文件 SHA256 与 R0 基线一致。
**R5 第一轮主审返修与窄返修（2026-10-15）**：主审认可两类生产服务无本步 diff、
保留重复而不造基类的取舍合理，但指出 P2：`_settle` 吞掉 `TimeoutError`/`CancelledError` 后主动 cancel 再正常返回，
`_cleanup`/`_cleanup_norm` 吞任意 `Exception`，`except BaseException` 危险；主审隔离 runtime 把两个 fake runner 改成永不完成后，
`test_no_background_tasks_leak` 仍通过，即守卫无法发现任务未完成。

已修（仅测试助手，生产代码依然零改动）：

- `_settle` 改为只等待自然完成（`wait_for(asyncio.shield(task))`）；超时或意外取消时先 cancel 清理再**重新抛出**，使用例失败。
- 正常返回后新增 `task.done()` / `not task.cancelled()` / `exception() is None` 三条真实终态断言。
- 合并 `_cleanup` 与 `_cleanup_norm` 为单个 `_cancel_task`，只抑制预期的 `CancelledError`，不吞普通 `Exception`；丢弃 `except BaseException`。
- 新增 autouse-ish fixture `started_services`：每个启动过 service 的用例无条件在 teardown 终结任务（即使中途断言失败）。
- 新增超时哨兵 `test_never_completing_runner_fails_and_leaves_no_task`：两类 runner 各一例，证明永不返回会失败且最后不留任务。
- 新增 `test_started_services_fixture_cancels_mid_test_failure`：直接驱动 fixture 清理路径，不再靠“故意失败的用例”。

**文档纠正**：`status()` 并未释放 task / runner 引用（保留 done task），改为“完成/不再运行”表述；
“四片段字字相同”改为“四片段相同（两条 ValueError 消息本就不同），不提取的原因改为“收益小且职责不同”，不再称每个 helper 必需 runner 分支。
**R5 第二轮主审返修与窄返修（2026-10-15）**：`_settle` 超时失败、
正常终态三断言、不提取生产服务的决定继续获认。
唯一剩余 P2 是无条件 fixture 清理尚未正确接线/验证：

1. `test_summary_start_success_lifecycle` 等三个用例参数有 `started_services` 但 `svc` 从未 `append`（
   `test_progress_callbacks_are_not_interchangeable` 的两个 `svc` 也未登记）。
2. 守卫用例只在测试 `finally` 里复制 fixture 的 cancel 循环，没有驱动真实 fixture teardown；
   主审把模块 `started_services` 换成仅 `yield []` 无清理的 fixture 后，守卫仍通过。
3. fixture teardown 的 `except Exception: pass` 会静默吞掉普通异常。

**已修（仅测试助手，生产代码依然零改动）**：

- 补登记：`test_summary_start_success_lifecycle`、
  `test_progress_callbacks_are_not_interchangeable`（两个 `svc`）、
  `test_token_stats_sources_differ` 均在 `start` 之前 `started_services.append(svc)`。
  已有 `try/finally` 管理的用例不重复登记。
- 守卫改为驱动**真实 fixture**：通过 `__pytest_wrapped__.obj` 取到原始 async generator，
  `await gen.__anext__()` 拿到真实 `tracked` 列表，`append(svc)` 后再 `await gen.__anext__()` 触发真实 teardown；
  不再在测试里复制 cancel 循环。
- fixture teardown 只抑制 `CancelledError`；其余 `BaseException` 全部记录，等所有登记服务都清理完后一并重抛为 teardown 失败。

**主审提供的反向探针现在结果**：将模块 `started_services` 替换为仅 `yield []` 、
无清理逻辑的 fixture 后，`test_started_services_fixture_cancels_mid_test_failure` 现在
**FAILED**（`AssertionError: the fixture must cancel a task it cannot finish`），pytest 退出码 1。
另一探针直接驱动无清理 fixture 的 async generator；确认 `task.done()=False`、
`cancelled()=False`、`svc.is_running=True`、剩余 pending task 为 1，即捕置的是真漏漏。

**实际命令与结果**：

- `.venv\Scripts\python.exe -m pytest backend/tests/test_service_lifecycle_matrix.py -o addopts='' -q` → **26 passed in 3.80s**。
- `.venv\Scripts\python.exe -m pytest <七个同进程文件> -o addopts='' -q` → **90 passed in 6.44s**。
  （与主审的 **88 passed in 6.18s** 是不同命令，不混计数。）
- 变异六处全部被杀：总结终态 phase 6 failed、归一化三态分支 2 failed、
  `stop()` 置空 3 failed、`start()` 置空 24 failed、token 累计 5 failed、summary 模型 replace 1 failed。
