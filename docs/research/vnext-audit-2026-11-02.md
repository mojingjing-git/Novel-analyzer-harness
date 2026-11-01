日期：2026-11-02
范围：vnext 侧只读盘点｜方法：AST import 分析（不是 grep 文本匹配）

# vnext 侧架构盘点（task-3）

方法：对 `backend/vnext/` 26 个文件做 AST 解析，提取公开符号，再用 **AST import 分析**（不是 grep 文本——`to_dict`/`blocks_dir` 这类通用名会让 grep 产生大量误报）在全仓定位真实引用点。分三类口径：**生产**＝`backend/api`、`backend/services`、`backend/core`、`backend/utils`；**scripts**＝测量与验收脚本；**测试**＝`backend/tests`。所有结论带 `文件:行`。

## 0. 结论速览

| | 数量 | 说明 |
|---|---|---|
| 有**生产**调用方的 vnext 模块 | **8 / 25** | 其中 7 个只被 vnext 自己的 API 层调用；**只有 `resolution` 被 1.x 生产路径调用** |
| 仅 `scripts/` 调用 | **11** | 有完整实现与验收脚本，但**没有产品入口** |
| **完全无外部调用** | **6** | 只有 vnext 内部与测试在用：`analyzer` `breaker` `concurrency` `consistency` `fingerprint` `graph` |

**对 2.0 最关键的三句话**：

1. **vnext 与 1.x 生产路径的唯一交叉点**是 `backend/services/recheck_v2.py:23` import 了 `backend.vnext.resolution`（判定层）。除此之外两条线完全平行。
2. **vnext 没有分析启动入口**——`backend/api/routes_vnext.py:194-198` 的注释直说「vnext 本版没有分析流水线，入队只是把条目写进 queue_state_vnext.json」。`backend/vnext/analyzer.py`（块分析）与 `backend/vnext/runner.py`（复检执行入口）都只被 `scripts/` 调用。
3. **vnext 没有总结/报告链路**（全库无 `final_report` 生成，只有 `backend/vnext/budget.py:34,225` 的预算项）。所以「2.0 还缺什么」的答案不是「把 vnext 接进 1.x」，而是**总结链路必须新写**。

---

## 1. 已实现的能力（按子系统）

**任务编排与执行**
- `backend/vnext/runner.py:605` 行，14 个公开符号。`run_analysis`（块级顺序分析）、`run_recheck`（复检全链：规划→预算→裁决→账本→落盘）。docstring 明说它存在的原因：「`tasks`/`budget`/`recheck_plan`/`resolution` 单独都测过，但没有任何生产入口消费它们」。**仅 scripts 调用**。
- `backend/vnext/analyzer.py:408` 行。`analyze_block`（单块分析）、`build_messages`、`parse_block_output`、`normalize_extracted`、`extracted_fingerprint`。硬约束：`extracted` 必须带 `SourceRef`。**无外部调用**（仅 runner 内部 + 测试）。
- `backend/vnext/tasks.py:229` 行。`TaskLedger`/`TaskRecord`——按 `task_key`（输入指纹）记录「跑没跑、结论、花了多少」，业务语义不进这里（解决「状态被别的路径改写」与「分不清没跑/跑了没结论」）。**仅 scripts**。
- `backend/vnext/concurrency.py:132` 行。`run_thread_job`/`wait_for_finish`——结算只认「线程真的结束了」，不认「等待方被取消」。**仅测试**。

**覆盖与快照**
- `backend/vnext/rolling.py:245` 行。`build_coverage`/`slots_from_results`/`uncommitted_prefix`——把乱序完成的块整理成「可按故事顺序消费」的覆盖视图，明确说出哪里有洞。docstring 举了要害：「100/102 完成而 101 缺失」不能算「98% 接近完成」。**仅 scripts**。
- `backend/vnext/knowledge.py:513` 行。`build_snapshot(exclusive_before=...)`（`:148-198`）、`KnowledgeStore.snapshot()`（`:341`）——**排他上界**的显式快照；未知来源范围一律排除。**仅 scripts**。
- `backend/vnext/results.py:415` 行。`classify`（结果新鲜度判定）、`publish_result`、`scan_orphans`、`make_result_id`（文件名是 `result_id` 而非块签名，避免后写覆盖先写）。**生产**（经 `vnext_service.py`）。
- `backend/vnext/blocks.py:150` 行。`plan_blocks`/`write_block`/`verify_block`/`is_block_fresh`——块失效必须**程序可判定**（签名 = 策略版本 + 有序的（章节 ID，正文哈希））。**生产**（`vnext_service.py:26,144`）。

**知识归约与提示词**
- `backend/vnext/knowledge.py:513`：`reduce_knowledge`、`records_from_extracted`、`store_fingerprint`、`save_store`。**仅 scripts**。
- `backend/vnext/prompt_view.py:114`：`build_prompt_window`——**知识库保存全部，窗口是读取时的动作**，裁剪时如实报告丢了什么（1.x 是落盘前裁剪，「库里有什么取决于当初配了多大窗口」）。**仅 scripts**。
- `backend/vnext/prompt_limits.py:133`：`PromptLimits` 不可变 dataclass，9 个上限统一定义 + 校验。**仅 scripts**。
- `backend/vnext/context.py:160`：`build_context`——把一处引用扩成**句子边界完整**的上下文窗口（固定字节切会把句子切半，模型读到半句话就下结论而材料不足看不出来）。**仅 scripts**。

**证据与门禁**
- `backend/vnext/resolution.py:732` 行，44 个符号——**vnext 最成熟、也是唯一被 1.x 复检采用的模块**。`parse_recheck`（把模型答复变成程序可判定的四枚举）、`locate_quote`（模型给原话、程序给位置）、`reduce_entity`（候选级→实体级归约）、`count_verdicts`（覆盖守恒）、`recheck_status`。**生产**（`backend/services/recheck_v2.py:23`）。
- `backend/vnext/citations.py:89`：`verify_fact`/`verify_facts_against_book`——**转发层**，实现已收敛到 `backend/core/citations.py`。**仅 scripts**。
- `backend/vnext/foreshadow.py:362`：`make_entity_id`（身份只由 `(book_id, seed_occurrence_id)` 决定，不含排序序号/章号）、`cutoff_view`（截至当前卷的候选视图）、`importance_score`。**仅 scripts**。
- `backend/vnext/graph.py:428`：`CooccurrenceEdge` 与 `SemanticRelationship` **分表永不合表**——「共现不是关系」。**仅测试**。

**预算与缓存**
- `backend/vnext/budget.py:249`：`Budget.reserve()`（**调用前**预留，额度不足抛 `BudgetExceeded` → `BUDGET_SKIPPED` 未完成态）、`rebuild_usage`（从账本重建用量）。设计理由：并发十批若「先调用后记账」，额度用尽时钱已经花了。**仅 scripts**。
- `backend/vnext/fingerprint.py:217`：`block_signature`/`dependency_fingerprint`/`task_key`/`evidence_id`/`result_id`/`content_hash`——**指纹只由内容与策略版本决定**（不含时间戳/随机 ID/当前最大章号）。**无外部调用**（内部 207 处）。

**熔断与错误分类**
- `backend/vnext/breaker.py:105`：`Breaker`——只有账户级+永久性错误才跳闸，判据只认结构化 `ErrorVerdict`（「文本里有个 401 就永久停机」是典型误判来源）。**无任何生产调用方**。
- `backend/vnext/constants.py:109`：`ErrorCode`（10 个）+ `VnextFormatError`——**格式族与版本必须由代码决定**，任何来自配置/环境/文件内容的「版本覆盖」都会让「不猜字段、不回退解析」失效。**生产**（`routes_vnext.py:21,22`、`vnext_service.py:27`）。

**写入一致性、存储与项目**
- `backend/vnext/consistency.py:148`：`publish_atomically`——写结果文件与登记 manifest 必须同时成立；判定原则是「**能不能删文件要看持久化状态**，不能看 `commit()` 有没有正常返回」。**仅测试**。
- `backend/vnext/artifacts.py:150`：`assert_vnext_payload`（没有格式标记就拒绝，不降级解析）、`write_json_atomic`（同目录临时文件 + 唯一名 + `fsync` + `os.replace`）。**生产**（`vnext_service.py:216`）。
- `backend/vnext/project.py:499`：`create_project`/`load_manifest`/`Manifest`/`RevisionConflict`——身份不随内容漂移、提交要带 revision。**生产**（`routes_vnext.py:22`、`vnext_service.py:26,144`）。
- `backend/vnext/registry.py:223`：`scan_projects`——旧目录**识别但拒绝**（归 `LEGACY_DIR`），绝不猜 `blocks/` 形状（对比 1.x `book_service._is_book_dir`「有 blocks/ 就算书」）。**生产**。
- `backend/vnext/queue.py:201`：`QueueState`/`normalize_after_restart`（崩溃后遗留 `running` 一律回落 `pending`，不猜它跑到哪）。**生产**（经包级导入）。

---

## 2. 调用方盘点（2.0 接线工作量的直接依据）

**生产代码里 import vnext 的全部位置（只有 6 行）**：

| 位置 | 导入 |
|---|---|
| `backend/api/routes_vnext.py:21` | `backend.vnext.constants`（ErrorCode, VnextFormatError） |
| `backend/api/routes_vnext.py:22` | `backend.vnext.project`（RevisionConflict） |
| `backend/services/vnext_service.py:26` | `backend.vnext`（blocks, project, queue, registry, results） |
| `backend/services/vnext_service.py:27` | `backend.vnext.constants` |
| `backend/services/vnext_service.py:144` | `backend.vnext`（同上） |
| `backend/services/vnext_service.py:216` | `backend.vnext.artifacts.read_json_artifact` |
| **`backend/services/recheck_v2.py:23`** | **`backend.vnext.resolution`**（INSUFFICIENT, CandidateVerdict, count_verdicts, locate_quote, parse_recheck, recheck_status, reduce_entity, render_recheck_schema） |

**分类表**

| 类别 | 模块 | 证据 |
|---|---|---|
| **有生产调用方（8）** | artifacts, blocks, constants, project, queue, registry, results, **resolution** | 上表 6 行；其中 7 个只被 vnext 自己的 API 层调用，只有 resolution 进了 1.x 生产路径 |
| **仅 scripts（11）** | budget, citations, context, foreshadow, knowledge, prompt_limits, prompt_view, recheck_plan, rolling, runner, tasks | `scripts/acceptance_vnext.py:24-48`、`scripts/e2e_real_book.py:185,208-209`、`scripts/measure_c1_pipeline.py:98,118-119`、`scripts/measure_c2_annotations.py:44,99`、`scripts/measure_c3_sampling.py:41,143,213`、`scripts/measure_full_book.py:56-60` |
| **无任何外部调用（6）** | analyzer, breaker, concurrency, consistency, fingerprint, graph | 仅 vnext 内部引用 + `backend/tests/test_vnext_*.py` |

**特别提示**：
- `backend/vnext/runner.py` 是「vnext 复检执行入口」，但**只在 `scripts/acceptance_vnext.py:228`、`scripts/e2e_real_book.py:208`、`scripts/measure_c1_pipeline.py:118`、`scripts/measure_full_book.py:60` 被调用**——生产要用它得自己接。
- `backend/vnext/breaker.py`（熔断）**没有任何生产调用方**（1.x 侧只有 `backend/core/pipeline.py` 的 `_fatal_blocks` 登记，不是熔断）。
- `backend/vnext/fingerprint.py` 的 `evidence_id` 与 1.x 的 `backend/core/evidence.py:84` 的 `evidence_id` **是两套同名实现**——合并时必须选一个。

---

## 3. 明确缺失的能力（对照 1.x）

| 缺失项 | vnext 现状 | 1.x 现状（合并时要补） |
|---|---|---|
| **总结/最终报告链路** | 全库无 `final_report` 生成，只有预算项 `backend/vnext/budget.py:34,225` | `backend/services/final_summary.py`（1969 行、四阶段：批次摘要→卷摘要→最终报告→全书复检） |
| **伏笔总表 + 50 类分类** | 无 `FORESHADOW_CATEGORY`；伏笔只有 occurrence/entity/candidate 三层，无总表 | `backend/config/constants.py:76` 的 `FORESHADOW_CATEGORY_DEFS`（50 类）+ `backend/services/final_summary.py:858-878` 的分类与别名归一 |
| **风格分析** | 无 | `backend/core/style_analyzer.py` |
| **聚合产物** | 无 | `backend/utils/aggregate_utils.py` |
| **卷摘要/滚动摘要生成** | `backend/vnext/rolling.py:1-8` 明说「本模块不执行分析，也不写摘要」，只出覆盖视图 | `backend/core/pipeline.py:1069` 写 `rolling_data["last_updated_chapter"]` |
| **分析启动入口** | `backend/api/routes_vnext.py:194-198` 明说「没有分析流水线」；9 个端点（`:72-213`）无启动/无总结/无报告 | `backend/services/queue_service.py` 驱动 `backend/core/pipeline.py` |
| **队列执行器** | `backend/vnext/queue.py` 只有状态读写（`normalize_after_restart`/`apply_progress`），**没有执行器** | `backend/services/queue_service.py`、`backend/services/queue_manager.py` |
| **前端可用性** | `frontend/src/pages/VnextDetailPage.vue:30` 读 `b.status === 'done'`，后端给的是 `state`（`backend/services/vnext_service.py:319-330`）→ 块状态列空白；`frontend/src/api/client.ts:386-395` 的 `VnextQueueEntry` 声明 `total_chapters`，而 `backend/api/routes_vnext.py:92-105` 的 queue 端点不返回它 | — |
| **配置面** | `backend/config/settings.py` 里 vnext 相关**几乎为零**（只有通用的 `workspace_dir:233`）；vnext 自带 `config_vnext.json`（`backend/vnext/constants.py` 的 `CONFIG_FILE_NAME`） | `backend/config/settings.py` 集中管理 |
| **桌面端/打包** | `desktop.py` 无任何 vnext 引用（只有 `workspace:232`）；`desktop.spec` 不含 vnext | `desktop.py:226-247` 首次启动创建 workspace |

**注**：`docs/issues.md` 里「vnext 前端页面与后端字段名不一致」三条中的**第一条已修复**——`backend/services/vnext_service.py:78` 现在返回 `"chapter_count": ref.total_chapters`，与 `frontend/src/api/client.ts:376` 的 `VnextBook.chapter_count` 一致。剩下两条（blocks 的 `status`/`state`、queue 的 `total_chapters`）仍在。

---

## 4. 与 1.x 的语义冲突点（合并时必须逐条选一个）

| # | 冲突项 | vnext 口径 | 1.x 口径 |
|---|---|---|---|
| 1 | **章切分** | `backend/vnext/project.py` 独立切章器（`routes_vnext.py:114-117` 称「后端用**同一套**切章器切分」）；`backend/core/anchor.py:89` 的 `build_chapter_spans` 按标题反查 | `backend/services/book_service.py:175-177` 一文件=一章；块=章 |
| 2 | **快照上界** | **排他** `exclusive_before`（`backend/vnext/knowledge.py:148-198,341`）：只纳入来源范围完全早于该位置的记录 | **标量** `last_updated_chapter`（`backend/core/pipeline.py:448,1069`）：按「当前最大章号」取全量 |
| 3 | **共现 vs 关系** | 分表（`backend/vnext/graph.py`：`CooccurrenceEdge` vs `SemanticRelationship`，docstring 明说「共现不是关系」） | **同章两两配对直接写成关系**：`backend/core/memory_state.py:388-394` |
| 4 | **伏笔休眠** | `cutoff_view` 按故事位置出候选，**不判死**（`backend/vnext/foreshadow.py`） | **判死**：`backend/utils/foreshadow_ledger.py:142` `item.status='dormant'`；`backend/services/final_summary.py:1487-1489` 因 active=0 直接跳过全书复检 |
| 5 | **账本** | 自有 `recheck_ledger.json`（`backend/vnext/runner.py` 的 `LEDGER_FILE_NAME`）+ `tasks.json` | `foreshadow_ledger.json`，`SCHEMA_VERSION = 5`（`backend/utils/foreshadow_ledger.py:24`） |
| 6 | **错误分类** | `ErrorVerdict`/`ErrorScope` 4 值（`backend/core/error_types.py`），判据只认结构化通道 | `backend/core/error_taxonomy.py` 另一套枚举与置信度 |
| 7 | **提示词限额** | 9 项统一在 `backend/vnext/prompt_limits.py` 的不可变 dataclass | 5 项来自 config（`backend/core/analyzer.py:74-79`），**4 项硬编码**：`backend/core/prompt_builder.py:103-106`（`max_relationships=15`/`max_verified_facts=15`/`max_themes=8`/`max_content_chars=30000`） |
| 8 | **引用定位** | `backend/vnext/citations.py:1-8` 明说已收敛到 `backend/core/citations.py` | 同源 ✅ **这一项已统一** |
| 9 | **证据 id** | `backend/vnext/fingerprint.py` 的 `evidence_id`（内容哈希口径） | `backend/core/evidence.py:84` 的 `evidence_id`（另一套实现） |
| 10 | **裁剪时机** | 窗口是**读取时**的动作（`backend/vnext/prompt_view.py`） | 落盘前裁剪（`backend/core/memory_state.py:414`），「库里有什么取决于当初配了多大窗口」 |

**第 1 条的实际代价（本轮实测）**：两种切分口径叠加会产生**重复章号**——源文件里行首出现的「第N回」（正文引用回次）被标题正则当成章节边界，合并块的 spans 形如 `[229, 230, 1, 2]`，章号 1、2 在整本书里反复出现。采集侧记下的 `anchor_chapter` 因此可能指向「另一份同号章」的文本，在那里当然找不到那句引文：1669 条候选里 **1303 条（78%）**被记成「锚点找不到」，复检只查了两成。已修为「章号只是提示，定位失败回退全书搜索」（`backend/services/recheck_v2.py`），实测救回 1303 条、0 条仍丢失。**合并 2.0 时这条必须先定，否则复检覆盖率会静默塌掉。**

---

## 5. 文档与实现不一致（`docs/data-contract-vnext.md`）

1. **账本路径与文件名**：文档 §2 目录布局写 `ledger/foreshadow.json`（在 `ledger/` 目录下）；代码是 `book_dir/recheck_ledger.json`（`backend/vnext/runner.py` 的 `LEDGER_FILE_NAME` + `ledger_path()` 返回 `book_dir / LEDGER_FILE_NAME`）——路径与文件名都不一致。
2. **文档 §2 的 `derived/` 目录描述的是目标形态，不是现状**：它列了 `final_summary_report.md` / `foreshadow_audit.md` / `style.md` / `aggregated/*.json`，**vnext 一个都没实现**（grep `final_report|style_profile|aggregat` 在 `backend/vnext/` 只命中 `budget.py:34,225` 的预算项与 `__init__.py:7` 的一句描述）。
3. **文档提到 `commit()`，代码里只有私有的 `_commit_state`**（`backend/vnext/consistency.py`）——`consistency.py` 的 docstring 讲的是「commit() 有没有正常返回」这个判据，但实现里没有这个公开函数名。
4. **文档 §8 错误码表缺 `NOT_FOUND`**：代码 `backend/vnext/constants.py` 的 `ErrorCode.NOT_FOUND` 存在，且 `backend/api/routes_vnext.py:42` 把它映射成 404。
5. **账本版本号对不上**：`backend/vnext/__init__.py:4` 写「伏笔账本 v3」，而 1.x 的 `SCHEMA_VERSION` 已经是 **5**（`backend/utils/foreshadow_ledger.py:24`）。
6. **数据根配置互不相干**：文档 §2 说 `workspace_vnext/` 是独立数据根、由 `config_vnext.json` 管；而 `backend/config/settings.py:233` 只有通用的 `workspace_dir`（默认 `"workspace"`）——vnext 数据根与 `config.json` 的 `workspace_dir` **没有任何联动**，两套配置各管各的。

---

## 6. 给 2.0 的三句话（如果只记三句）

1. **能直接复用的**：`resolution`（判定层，1.x 已在用）、`artifacts`（原子写+格式门禁）、`fingerprint`（指纹口径）、`blocks`（块失效可判定）、`queue`（状态与恢复）、`registry`（书目发现）、`project`（身份与 revision）、`knowledge`/`prompt_view`（排他快照 + 读取时裁剪）——这 8 个基本可以直接进 2.0。
2. **必须补写的**：总结/报告链路、伏笔总表与 50 类分类、队列执行器与分析启动入口、前端字段对齐、配置面合并——**没有一个能靠接线解决**。
3. **必须先决策的**：章切分口径（#1，它直接决定复检覆盖率）、快照上界（排他 vs 标量）、共现与关系是否分表、账本用哪套。`breaker`/`graph`/`consistency`/`concurrency`/`fingerprint` 这几个「无外部调用」的模块，正是 2.0 最该接上的——它们不是死代码，是**建好了但还没人用的地基**。

（另：本次盘点全程只读，未修改任何文件、未跑测试。）

---

本报告由 teammate vnext-audit 生成，lead 复核后并入 docs/data-contract-2.0.md
