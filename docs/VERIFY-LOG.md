# 验证记录（外置追加日志）

本文件是所有计划/报告文档的**验证记录索引**。外置存放的原因：若把验证记录写进被哈希的正文，写入哈希值会再次改变正文，形成自指，无法稳定。

---

## 规则

1. **只追加，不修改历史行。** 任何一行一旦写入，其 `scope`、`result`、`doc_sha256` 不得改动。
2. **两类验证分开记录**，不得合并成一个"通过"：
   - `doc_check`：文档层面的检查（章节完整、编号连续、覆盖表无遗漏、diff 可应用）
   - `business_test`：业务测试结果（pytest / vitest / 构建）
3. `doc_sha256` 对当时版本的**正文**计算，排除 frontmatter 与本文件引用。
4. 文档修订后新增一行，不覆盖旧行；旧行的结论**仅对该行的 hash 有效**。
5. 历史产物（patch、合入对照报告）保留原字节，通过本表索引标注其适用版本，不在文件内改写。

---

## 记录

| 日期 | commit | scope | 类型 | result | doc_sha256 | 说明 |
|---|---|---|---|---|---|---|
| 2026-10-30 | 尚未入库 | PLAN v2 合入（T01–T42） | doc_check | 通过 | 未记录 | 当时记录"42 项验收、diff 可逆"。该结论仅对当时的两个文档版本有效；后续对当前文档执行 `git apply --reverse --check` 已不能应用，属版本漂移，不推翻当时的记录 |
| 2026-10-30 | 尚未入库 | S6 补丁 v3 并入（T43–T44） | doc_check | 未重新验证 | 未记录 | 追加 T43/T44 与 S6 第 11、12 条。**未**对合入 patch 重新做可逆性验证，故本行 result 不含"通过" |
| 2026-10-30 | `3a27b6c` | 全量后端测试 | business_test | **864 passed / 92.17s** | — | 清理代理环境变量后实测。此为 v2 基线**之前**的历史数字，保留不改写 |
| 2026-10-30 | `5937cb1` | 全量后端测试（S0 后） | business_test | **892 collected / 566.84s** | — | S0 交付记录：原始未清理代理环境下 892 passed、5 warnings、exit 0。较 `3a27b6c` 增加 28 例（21 代理守卫 + 7 语料自检） |
| 2026-10-30 | `5937cb1` | 全量后端测试独立复现 | business_test | **892 collected / exit 0** | — | 另一会话独立复跑，未手动清代理，exit 0。耗时未记录 |
| 2026-10-30 | 尚未入库 | PLAN v2.2 复审二收口（scope 词汇统一 / 覆盖状态互斥 / 资源计量对象 / 附表 A 冻结口径 / T45·T47 自动化方式 / D-12 外置索引） | doc_check | 通过 | 未记录 | 主计划 703 行、delta 395 行、D-01 设计 257 行。校验：§5 内 T 唯一 47 条（T01–T47）无缺失无重复（附表 B 的 T 行是用例键，非重复定义）；两文档 `CALL/BLOCK/ACCOUNT` 旧词汇残留 0；`planned = executed` 旧守恒式残留 0 |
| 2026-10-30 | 尚未入库 | PLAN v2 S1 收口（U1–U6b：C-01 止血 + 判定贯通 + usage 语义） | doc_check | 通过 | 未记录 | `agent.md` 新增 §10.36 并改写 §5.1 的 `error_taxonomy` 三条 bullet；`CHANGELOG.md` 新增 S1 段落；`docs/architecture.md`「两级重试链」改为「两级重试链与错误判定」。行尾保真：CHANGELOG 纯 CRLF(722)、agent.md 纯 LF、architecture 原 CRLF 保持 |
| 2026-10-30 | 尚未入库 | 全量后端测试（S1 后） | business_test | **962 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，657.79s。相对 S0 基线 892 增加 70 例：新增 4 个测试文件 61 例（`test_error_types.py` 29 / `test_retry_decision_u2.py` 20 / `test_verdict_pipeline_u4b.py` 4 / `test_concurrency_and_usage_u6.py` 8）+ `test_error_taxonomy.py` 37→46（+9） |
| 2026-10-30 | 尚未入库 | 全量后端测试（S2 后） | business_test | **1022 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，1493.09s。相对 S1 基线 962 增加 60 例：`test_vnext_constants_alignment` 4 / `test_vnext_artifacts` 15 / `test_vnext_fingerprint` 15 / `test_vnext_project` 15 / `test_vnext_blocks` 11 |
| 2026-10-30 | 尚未入库 | PLAN v2 S2 文档同步 + 数据契约 | doc_check | 通过 | 未记录 | `agent.md` 新增 §10.37；`CHANGELOG.md` 新增 S2 段落；新建 `docs/data-contract-vnext.md`（385 行，LF 无 BOM，唯一 JSON 例子经 `json.loads` 校验）。行尾保真：CHANGELOG 纯 CRLF(753)、agent.md 纯 LF |
| 2026-10-30 | 尚未入库 | 全量后端测试（S2 接线后） | business_test | **1055 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，647.75s。相对 S2 基础层基线 1022 增加 33 例：`test_vnext_registry` 10 / `test_vnext_results_queue` 12 / `test_vnext_service_routes` 11 |
| 2026-10-30 | 尚未入库 | S2 接线文档同步（registry/results/queue/门面/路由） | doc_check | 通过 | 未记录 | `agent.md` §10.37 增两条接线 bullet；`CHANGELOG.md` 增「目录发现与队列恢复」小节；`backend/app.py` 差异经 `git diff -w --numstat` 核对为 **+2 行**（一个 import、一个 include_router），未触碰其他逻辑 |
| 2026-10-30 | 尚未入库 | 全量后端测试（外部复审六条修复后） | business_test | **1075 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，691.44s。相对 S2 接线基线 1055：`test_vnext_results_queue.py` 按新协议重写（12→18，+6）+ 新增 `test_vnext_review_fixes.py`（9）+ 新增 `test_llm_verdict_tokens_review.py`（5）= +20 |
| 2026-10-30 | 尚未入库 | 外部复审六条修复文档同步 | doc_check | 通过 | 未记录 | `agent.md` 新增 §10.38（六条逐条：问题/现在）；`CHANGELOG.md` 新增「外部复审六条修复」段落。行尾保真：agent.md 纯 LF、CHANGELOG 纯 CRLF |
| 2026-10-30 | 尚未入库 | 全量后端测试（依赖链 + S2 基础入口后） | business_test | **1092 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，691.03s。相对复审修复基线 1075：新增 `test_vnext_dependency_chain.py`（7）+ `test_vnext_create_and_progress.py`（10）= +17 |
| 2026-10-30 | 尚未入库 | 依赖链与基础入口文档同步 | doc_check | 通过 | 未记录 | `docs/data-contract-vnext.md` 新增 §10（依赖链语义与判定表）、§11（S2 基础入口表），附录 B 补"本轮已完成的 5 行"说明；`agent.md` 新增 §10.39；`CHANGELOG.md` 增两小节 |
| 2026-10-30 | 尚未入库 | 全量后端测试（S3 U1/U2 安全快照后） | business_test | **1111 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，704.78s。相对依赖链基线 1092：新增 `test_vnext_knowledge_snapshot.py`（19）|
| 2026-10-30 | 尚未入库 | S2 完成口径落文 + S3 拆解 | doc_check | 通过 | 未记录 | 主计划 §4 S2 加「状态：已完成」与口径 A 说明（vnext 读入口 = `/api/vnext/*` + 共享发现/队列；旧 1.x 端点不在本批加拒绝）；新建 `docs/PLAN-S3-task-breakdown-2026-10-30.md`（U1–U8，U1/U2 完成）；`agent.md` §10.40；`CHANGELOG.md` S3 U1/U2 段落 |
| 2026-10-30 | 尚未入库 | 全量后端测试（S3 U5 引用校验后） | business_test | **1130 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，766.98s。相对 S3 U1/U2 基线 1111：新增 `test_vnext_citations.py`（19）|
| 2026-10-30 | 尚未入库 | 引用校验文档同步 + S3 未决定稿 | doc_check | 通过 | 未记录 | `agent.md` §10.41；`CHANGELOG.md` S3 U5 段落；`docs/PLAN-S3-task-breakdown-2026-10-30.md` 的 U5 状态更新、未决第 2 条标记为已定（五级判定 + 失败降级不丢数据） |
| 2026-10-30 | 尚未入库 | 全量后端测试（S3 U3–U8 收口后） | business_test（**全量**） | **1235 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，717.19s。相对 S3 U5 基线 1130 增加 105 例：`knowledge_reduce` 14 + `prompt_limits` 31 + `prompt_view` 9 + `graph` 49 + `citations` 19→21（+2）|
| 2026-10-30 | 尚未入库 | S3 收口文档同步 | doc_check | 通过 | 未记录 | `agent.md` §10.42（U3–U8 逐条 + 派工记录）；`CHANGELOG.md` S3 收口段落；`docs/data-contract-vnext.md` 新增 §12（三层职责 / 证据与引用 / 图谱边 / 三条口径）；`docs/PLAN-S3-task-breakdown-2026-10-30.md` U1–U8 全部标完成 |
| 2026-10-30 | 尚未入库 | 全量后端测试（S4–S6 后） | business_test（**全量**） | **1329 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，699.24s。相对 S3 收口基线 1235 增加 94 例：`rolling` 19 + `foreshadow` 29 + `resolution` 34 + `context` 12 |
| 2026-10-30 | 尚未入库 | S4–S6 文档同步 | doc_check | 通过 | 未记录 | `agent.md` §10.43；`CHANGELOG.md` S4–S6 段落；`docs/data-contract-vnext.md` 新增 §13（覆盖视图 / 伏笔实体 / 裁决 / 上下文取材）与 §13.2.1（S5 三处口径：整条排除优先、entity_id 首次确定不再重算、去重键为二元组）；新建 `docs/PLAN-S4-S6-task-breakdown-2026-10-30.md` |
| 2026-10-30 | 尚未入库 | 全量后端测试（复审二七条修复后） | business_test（**全量**） | **1353 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，771.53s。相对 S4–S6 基线 1329 增加 24 例：新增 `test_vnext_review_round2.py`（23）+ `foreshadow` 29→30（+1，前缀不变性用例替换原"整条排除"用例）|
| 2026-10-30 | 尚未入库 | 复审二文档同步 + 契约口径撤回 | doc_check | 通过 | 未记录 | `agent.md` §10.44（七条逐条）；`CHANGELOG.md` 复审二段落；`docs/data-contract-vnext.md` 新增 §13.3 并在 §13.2.1 原文标注"已撤回"（整条排除）；行尾保真：agent.md 纯 LF、CHANGELOG 纯 CRLF(982)、contract 纯 LF |
| 2026-10-30 | 尚未入库 | 全量后端测试（S7 任务缓存与预算 / S8 熔断后） | business_test（**全量**） | **1398 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，736.78s。相对复审二基线 1353 增加 45 例：`test_vnext_tasks_budget.py`（24）+ `test_vnext_recheck_plan.py`（11）+ `test_vnext_breaker.py`（10）|
| 2026-10-30 | 尚未入库 | vnext 端到端验收（离线，零模型调用） | acceptance | **ACCEPTED 27/27** | — | `python scripts/acceptance_vnext.py -v`：建项目→块→覆盖→快照→归约→窗口→引用校验→伏笔视图→裁决→覆盖计数→任务缓存→预算→汇总 |
| 2026-10-30 | 尚未入库 | S7–S10 文档同步 | doc_check | 通过 | 未记录 | `agent.md` §10.45；`CHANGELOG.md` S7/S8 段落；新建 `docs/PLAN-S7-S10-status-2026-10-30.md`（含 S8 部分完成、S9 付费实验未执行、S10 打包未执行的具体原因）与 `docs/RELEASE-NOTES-vnext.md`；行尾保真：agent.md 纯 LF、CHANGELOG 纯 CRLF(1015) |
| 2026-10-30 | 尚未入库 | 全量后端测试（复审三七条修复后） | business_test（**全量**） | **1404 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，698.54s。相对 S7/S8 基线 1398 增加 6 例：新增 `test_vnext_runner.py`（执行→落盘→重启→复用闭环）|
| 2026-10-30 | 尚未入库 | vnext 端到端验收（含生产入口闭环） | acceptance | **ACCEPTED 32/32** | — | `python scripts/acceptance_vnext.py -v`：原 27 条 + 生产入口 5 条（首次调用一次 / 真实引文通过 / 账本落盘 / 重启复用不再调用 / 费用可重建）|
| 2026-10-30 | 尚未入库 | 复审三文档同步 | doc_check | 通过 | 未记录 | `agent.md` §10.46（七条逐条）；`CHANGELOG.md` 复审三段落；`docs/data-contract-vnext.md` 新增 §14（预留身份 / 记账与应用分离 / 用量重建字段 / 覆盖完整性 / 反证可核实 / 先落盘再前进）；`docs/PLAN-S7-S10-status-2026-10-30.md` 的 S7 补生产入口说明；行尾保真：CHANGELOG 纯 CRLF(1051)、agent.md 与 contract 纯 LF |
| 2026-10-30 | 尚未入库 | 全量后端测试（S8 一致性/并发 + S9 脚本后） | business_test（**全量**） | **1416 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，713.13s。相对复审三基线 1404 增加 12 例：新增 `test_vnext_consistency.py`（8 发布故障点 + 4 取消/并发）|
| 2026-10-30 | 尚未入库 | 真实模型提示词花费测量 | measurement | **4 次调用 / 3591 token / usage_status=KNOWN** | — | `python scripts/measure_prompt_cost.py --chapters 1|6`：1 章 405 vs 379、6 章 1065 vs 1039 prompt tokens；结论写入 `docs/MEASURE-S9-2026-10-30.md`（明确写了"合成样本不足以支撑取舍结论"）|
| 2026-10-30 | 尚未入库 | 打包与首启冒烟 | packaging | **构建 exit 0 / 启动存活 / crash.log 干净** | — | `python -m PyInstaller desktop.spec` → `dist/novel-analyzer.exe` 31.45 MB；隐藏窗口启动 16s 存活，`analyzer.log` 显示 `127.0.0.1:8464` 就绪、WebSocket 连接、健康检查 6/6。**首启发现并修掉 crash.log 自刷屏 bug**，重新打包复验只剩 1 行 INFO |
| 2026-10-31 | 尚未入库 | 全量后端测试（S8 重做：发布三态/线程结算后） | business_test（**全量**） | **1426 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，736.02s。相对 S8 首版基线 1416 增加 10 例：`test_vnext_publish_faults.py`（6，入口级故障）+ `test_vnext_consistency.py` 12→16（取消后关循环仍以线程结束结算 / 结算回调失败不挂起 / wait_for_finish）|
| 2026-10-31 | 尚未入库 | vnext 端到端验收（发布接入三态后） | acceptance | **ACCEPTED 32/32** | — | `python scripts/acceptance_vnext.py` |
| 2026-10-31 | 尚未入库 | 便携包真实用户闭环 | smoke（打包产物） | **5/5 PASS** | — | `python scripts/smoke_portable.py`：启动 `dist/novel-analyzer.exe` → 从日志取端口 → `/`(HTML) + `/api/vnext/books` + `/api/vnext/queue` + 空工作区 0 本书。与进程内验收互补，覆盖打包后的启动路径 |
| 2026-10-31 | 尚未入库 | 最小 vnext 界面（前端） | build | **通过** | — | `npm run build` 5.68s；HTTP 验证 bundle 含 `/vnext` 与「新版空间」；重新打包后便携包闭环仍 5/5 |
| 2026-10-31 | 尚未入库 | 全量后端测试（vnext 写操作端点后） | business_test（**全量**） | **1433 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，751.73s。相对 1426 增加 7 例：新增 `test_vnext_queue_endpoints.py`（入队/重复入队幂等/未知项目/缺 book_id/移出/移出不存在/缺 title 422）|
| 2026-10-31 | 尚未入库 | vnext 写操作界面（前端） | build | **通过** | — | `npm run build` 5.37s；`VnextPage.vue` 重写为「新建项目 + 项目列表 + 队列入队/移出 + 被拒目录」，并修正 `title`/`chapter_count` 字段名（上一版读 `name`/`total_chapters`，书名与章节数一直是空的）|
| 2026-10-30 | 尚未入库 | 测试节奏调整 + 相关集工具 | doc_check | 通过 | 未记录 | 新增 `scripts/pytest_related.py`（按"文件名对应 + import 引用"挑选相关测试）；`agent.md` §8 增加测试节奏约定：**迭代期跑相关集，批次收口跑全量**，且 VERIFY-LOG 必须写明这一遍是全量还是相关集。实测：改动 2 文件 → 选 1 个测试文件 → 3 秒；全量为 ~1100 例 / 12 分钟 |
| 2026-10-31 | 尚未入库 | 全量后端测试（A 批后） | business_test（**全量**） | **1456 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，739.38s。相对 1433 增加 23 例：`test_vnext_project_atomic.py` |
| 2026-10-31 | 尚未入库 | 前端构建与超时用例 | frontend_test | **vitest 13 passed / build 6.19s** | — | `npx vitest run src/api/client.test.ts` + `npm run build`；新增 4 例（超时/中断/信号/文案映射） |
| 2026-10-31 | 尚未入库 | A/B/C 计划复审并入 | doc_check | 通过 | 未记录 | 只读审查结论 NEEDS_REVISION，五条修订并入 `docs/PLAN-ABC-2026-10-31.md` §7；`agent.md` §10.49；`CHANGELOG.md` A 批段落 |
| 2026-10-31 | 尚未入库 | 全量后端测试（B0 前置后） | business_test（**全量**） | **1464 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，792.55s。相对 A 批 1456 增加 8 例：`test_vnext_knowledge_persist.py` |
| 2026-10-31 | 尚未入库 | 端到端验收（B0 后） | acceptance | **ACCEPTED 32/32** | — | `python scripts/acceptance_vnext.py`；vnext 相关集 504 passed |
| 2026-10-31 | 尚未入库 | B/C 计划复审并入 + B0 文档 | doc_check | 通过 | 未记录 | 复审结论 NEEDS_REVISION，五条 P1 并入 `docs/PLAN-BC-2026-10-31.md` §9；`docs/data-contract-vnext.md` 新增 §16；`agent.md` §10.50 |
| 2026-10-31 | 尚未入库 | 全量后端测试（B1a 后） | business_test（**全量**） | **1472 passed / exit 0** | — | 父 agent 亲自跑 `python -m pytest -o addopts= -q`，763.93s。相对 B0 的 1464 增加 8 例：`test_vnext_analyzer.py` |
| 2026-10-31 | 尚未入库 | B1a 文档同步 | doc_check | 通过 | 未记录 | `agent.md` §10.51；`CHANGELOG.md` B1a 段落；vnext 相关集 512 passed、验收 32/32 |

---

## 待补

- 上述两行 `doc_sha256` 为"未记录"。若需补，应基于**当时版本的文件字节**计算；不得用当前版本回填。

---

## 相关文件

| 文件 | 适用版本 | 状态 |
|---|---|---|
| `docs/PLAN-v2-long-novel-reliability-2026-10-30.md` | v2.2（T01–T47） | 活跃 |
| `docs/PLAN-S6-autonomous-resolution-2026-10-30.md` | v3 | 活跃 |
| `docs/REPORT-review-consolidated-2026-10-30.md` | 首版 | 活跃（待更正） |
| `docs/REVIEW-plan-v1-new-major-2026-10-30.md` | R1–R6 | 历史 |
| `docs/REVIEW-post-S0-plan-findings-2026-10-30.md` | R-1–R-7 | 现行复审 |
| `docs/PLAN-v2.2-delta-2026-10-30.md` | v2.2.1（含 §9 复审二增量） | 活跃 |
| `docs/patches/PLAN-v2-autonomous-resolution-2026-10-30.patch` | 对应 T01–T42 | **历史**，不适用于当前文档 |
| `docs/REPORT-plan-v2-autonomous-merge-2026-10-30.md` | 对应 T01–T42 | **历史**，记录 42 项 |
| `docs/BASELINE-2026-10-30.md` | S0 基线 | 活跃（待更正） |
