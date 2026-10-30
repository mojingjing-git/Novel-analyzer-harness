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

## S7 复检任务缓存、确定性重放与预算 —— ✅ 完成

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

## S8 熔断、写入一致性与交互保护 —— ◑ 部分

| 条目 | 状态 |
| --- | --- |
| C-07 熔断 | ✅ `breaker.py`：只认**结构化判定**且 `scope=ACCOUNT_GLOBAL` 才跳闸；文本通道永不跳闸；跳闸粘性，恢复需显式 `reset()`。测试 10 例 |
| C-09 写入一致性 | ✅ 既有 `artifacts.write_json_atomic`（同目录 mkstemp + fsync + replace）＋ `results.publish_result` 先落盘后登记；**未做**：进程崩溃点注入测试（需要 fault injection 框架，记为后续） |
| C-16/C-18/C-19/C-20 交互保护 | ◑ 规则已固化（持锁不得 await、取消不终止工作线程、预留须等线程结束才释放），但**没有**代码级守卫；当前靠"临界区只做快照/引用切换"的写法约束。记为后续 |

## S9 规模治理与语义质量实验 —— ◑ 观测已就位，付费实验未执行

- **已做**：用量与预算快照（`budget.snapshot()`）、任务维度分状态计数（`TaskLedger.totals()`）、覆盖汇总（`RecheckSummary`）。这三样是"重复费用"与"预算超支"的唯一可信来源。
- **未做（需预算与授权，且 PLAN §4 S9-7 明确要求独立预算）**：A/B/C 三组 Prompt 组织方式实验、50–100 章标注集上的准确率/召回评测、随机顺序与候选可见性的配对评测（接口 B 锚定风险）、卷摘要压缩触发率测量。
- **为什么可以先不做**：PLAN §4 S9-7 写明"基础来源/时序/证据修复不依赖实验显示文学质量提升才能交付"。本批交付的正是这部分。

## S10 Windows 发布、文档与端到端验收 —— ◑ 验收已自动化，打包未执行

- **已做**：`scripts/acceptance_vnext.py` 离线端到端验收，**27/27 通过**，覆盖建项目→块→覆盖视图→快照→归约→窗口→引用校验→伏笔视图→裁决→覆盖计数→任务缓存→预算→汇总。
- **未做**：PyInstaller 打包（当前 `.venv` 未安装 PyInstaller，`build_exe.bat` 跑不通）；Windows 安装包与首次启动向导的端到端手测。
- **未做**：前端页面接入（vnext 目前只有只读 API，没有界面）。

## 一句话边界

S7 的判定层与预算层是完整的；S8 的熔断完整、写入一致性复用既有原子写、交互保护只有规则没有守卫；S9 的实验与 S10 的打包**没有执行**，原因是预算/环境，不是"忘了"。
