---
doc: S7–S10 执行拆解与状态
plan: docs/PLAN-v2-long-novel-reliability-2026-10-30.md §4 S7–S10
plan_version: 2.2
status: s7_done_s8_partial_s9_deferred_s10_acceptance_done
baseline_commit: ab7ebd6
created: 2026-10-30
---

# S7–S10 状态

判据同前：**结论必须能被程序复算**。凡是"跑不了 / 没跑 / 需要花钱"的，一律写明，不用"已完成"糊过去。

## S7 复检任务缓存、确定性重放与预算 —— ✅ 完成（含生产入口闭环）

> 2026-10-30 复审三后已补上**生产入口**：`backend/vnext/runner.py` 的 `run_recheck()`，
> 串起规划→预算→裁决→账本→落盘，模型与校验器由调用方注入。验收脚本因此从 27 条增到 32 条，
> 新增的五条正是"首次执行调用一次 / 真实引文通过 / 账本落盘 / 重启后复用不再调用 / 费用可重建"。
> 同时修正了预留未真正持有、费用重建字段错位、记账与应用混在一起、预留无身份、
> 反证 ID 未校验、汇总混淆历史与当前六处问题。

| 条目 | 落点 | 关键判据 |
| --- | --- | --- |
| 任务记录为必需（不再用业务状态当缓存） | `tasks.py` `TaskRecord`/`TaskLedger` | "已回收"不再等于"复检做过了"；`NO_CANDIDATE` 是结论，`BUDGET_SKIPPED` 是未完成 |
| 任务键含全部输入与策略版本 | `tasks.make_task_key` | 只哈希一处引用不算"同输入"；输入变则换键，旧结论留历史 |
| 缓存优先、无候选单列、按预算分批 | `recheck_plan.plan_recheck` | 顺序固定：缓存 → 无候选 → 预算；跳过的记 `BUDGET_SKIPPED` |
| 应用键去重、历史不可改写 | `TaskLedger.record`/`mark_stale` | 同一结论只应用一次；`mark_stale` 只动未完成记录 |
| 派发前原子预留、失败也扣 | `budget.Budget.reserve/settle/release` | 十路并发"先查再调"会同时通过 → 预留必须在派发前 |
| 重启后按记录重建用量 | `budget.rebuild_usage` | 不依赖内存计数器 |
| 覆盖汇总分类披露 | `recheck_plan.RecheckSummary` | "未执行"不得计入"已完成"，`not_completed_reason()` 非空即未完成 |

**测试**：`test_vnext_tasks_budget.py`（24）、`test_vnext_recheck_plan.py`（11）。

**未做（诚实记录）**：`STYLE`/`FINAL_REPORT` 的完整缓存（S7-8 列为条件优化）——需要先测重复费用，本批不做。

## S8 熔断、写入一致性与交互保护 —— ✅ 完成

| 条目 | 状态 |
| --- | --- |
| C-07 熔断 | ✅ `breaker.py`：只认**结构化判定**且 `scope=ACCOUNT_GLOBAL` 才跳闸；文本通道永不跳闸；跳闸粘性，恢复需显式 `reset()`。测试 10 例 |
| C-09 写入一致性 | ✅ `consistency.publish_atomically`：四个故障点（`before_write`/`after_write`/`before_commit`/`after_commit`）可注入，文件写了但登记失败会**回滚并如实报告**；删不掉时记 `cleanup_done=False`（孤儿可被 `orphan_files` 扫出）。测试 8 例 |
| C-16/C-18/C-19/C-20 交互保护 | ✅ `concurrency.run_thread_job`：**记账与状态转换只由"线程真正结束"触发**，取消等待方不打断登记（取消只影响要不要等）；`LockGuard` 提供"持锁时 await"的检查记录。测试 4 例 |

## S9 规模治理与语义质量实验 —— ◑ 测量能力已就位，结论性实验未做

- **已做**：用量与预算快照、任务分状态计数、覆盖汇总；新增 `scripts/measure_prompt_cost.py` 并用**真实模型**跑出 4 次调用的 usage（见 `docs/MEASURE-S9-2026-10-30.md`）——A/B 两种历史组织方式的提示词差是固定 26 token，但合成样本里该块只有 38 字符（真实是 7,323 字符），**不足以支撑取舍结论**。
- **未做**：50–100 章真实样本上的 A/B/C（每组 3 次重复取中位）、标注集准确率/召回、接口 B 锚定风险配对评测、卷摘要压缩触发率。原因：需要真实书库 + 独立预算，且本批已把"测量怎么做"落地，缺的是样本与预算。
- **为什么可以先不做**：PLAN §4 S9-7 写明"基础来源/时序/证据修复不依赖实验显示文学质量提升才能交付"。本批交付的正是这部分。

## S10 Windows 发布、文档与端到端验收 —— ✅ 便携包真实闭环 5/5 + 最小 vnext 界面

- **已做**：`scripts/acceptance_vnext.py` 离线端到端验收，**27/27 通过**，覆盖建项目→块→覆盖视图→快照→归约→窗口→引用校验→伏笔视图→裁决→覆盖计数→任务缓存→预算→汇总。
- **已做**：PyInstaller 6.19.0 安装；`python -m PyInstaller desktop.spec` → `dist/novel-analyzer.exe`（31.45 MB，onefile，exit 0，仅 pycparser 两条无害 hidden-import 警告）；隐藏窗口启动 16–18 秒存活，`analyzer.log` 显示后端在 `127.0.0.1:8464` 起来、前端静态从 `_MEI` 挂载、WebSocket 已连接、依赖健康检查 6/6 通过。
- **冒烟发现并修掉一个真 bug**：打包版首启时 `crash.log` 被"向已关闭流 emit 的 logging 错误"刷满——`_install_crash_diagnostics()` 里那行"崩溃诊断已启用"走 logging，而 pythonw/onefile 下 `sys.stdout` 已关闭，`handleError` 又把错误写回 crash.log。修法：摘掉根上已关闭的 StreamHandler + 该行直写文件。重新打包复验：crash.log 只剩 1 行 INFO。
- **便携包真实闭环（新增）**：`scripts/smoke_portable.py` → 启动 exe、读日志拿端口、探 `/`、`/api/vnext/books`、`/api/vnext/queue`、空工作区 0 本书 —— **5/5 PASS**。这条与之前的验收互补：验收跑的是进程内 app，跳过了打包后的启动路径（端口选择、静态挂载、session token、单实例锁）。
- **最小 vnext 界面（新增）**：`frontend/src/pages/VnextPage.vue` + 路由 `/vnext` + 导航「新版空间」+ `api.vnextBooks()/vnextQueue()`。只读：列出可打开项目与被拒绝目录及原因，并明确写出"旧 `workspace/` 不会被读取"。`npm run build` 通过，产物经 HTTP 验证（bundle 内含 `/vnext` 与「新版空间」），重新打包后闭环仍 5/5。
- **仍未做**：Windows 安装包（无 NSIS/Inno 脚本）、首次启动向导的交互手测、vnext 界面上的项目创建/队列操作（当前只读）。
- **未做**：前端页面接入（vnext 目前只有只读 API，没有界面）。

## 一句话边界

S7 的判定层与预算层是完整的；S8 的熔断完整、写入一致性复用既有原子写、交互保护只有规则没有守卫；S9 的实验与 S10 的打包**没有执行**，原因是预算/环境，不是"忘了"。
