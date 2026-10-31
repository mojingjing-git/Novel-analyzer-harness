# PLAN：把"波的宽度"写进依赖契约（二次运行必须零调用）

- 日期：2026-10-31
- 状态：v2（复审一后已改，待实施）
- 目标批次：vnext 执行层（`backend/vnext/`）
- 前置事实：真书《三侠五义》64 章 / 32 块 / 8 并发已跑通（`committed_blocks=32`、`prefix_complete=True`、938,921 token / 1045s）

## 1. 问题

第一次运行 32 块全部提交，**第二次运行却重跑了 31 块**（`调用 31  committed=32`）。

诊断（`classify` 结果）：

```
done: 1   stale: 31   missing: 0
block0 done? True
record.dependency_fingerprint : 8280a7af5f4c8258
expected（按串行推算）        : c6a7556f35bf83e2
```

**根因**：并发的语义与依赖指纹的定义打架。

- 运行期（`backend/vnext/runner.py:455-470`）：同一波的 8 个块共享**波开始时的**前情快照
  `prior_snapshot = list(prior)`；于是块 1–7 记录的依赖指纹是 `f(prior=[])`。
- 判定期（`backend/vnext/results.py` 的 `classify(..., context_fingerprints=...)`）：
  按**串行**推算，块 1 的期望值是 `f(prior=[块0的 result_id])`。
- 两者天然不等 ⇒ 块 1–31 永久 `STALE_DEPENDENCY` ⇒ 每次重跑都重付一遍钱。

**代价量化**：本书 938,921 token / 次。若用户重跑一次，白烧同样的量。

**为什么现有用例没抓住**：`test_vnext_runner_analysis.py` 的"续跑 ≡ 一次跑完"用的是
`concurrency=1`，波宽为 1 时两条路径重合。

## 2. 不做什么

- **不改**并发模型本身（波内共享快照是正确的：它保证同一块的输入与"谁先返回"无关）。
- **不改成**"波内串行化前情"（把块 1 的前情依赖块 0 的结果）——那等于放弃并发，
  每块都要等前一块完成，32 块只能一个一个跑。
- **不引入**"记录里存 prior 列表然后直接比对"的自描述方案：它只能回答"这条记录当时
  依赖了什么"，回答不了"现在这些输入还成立吗"。

## 3. 方案：波宽进契约

### 3.1 数据结构

`ResultStrategy` 增加字段 `wave_size: int = 1`（`backend/vnext/results.py`）：

```python
@dataclass(frozen=True)
class ResultStrategy:
    prompt_strategy_version: int
    model_fingerprint: str
    context_strategy_version: int
    wave_size: int = 1          # 新增：同一波共享前情的宽度
```

- 它进 `expected_dependency()` 的哈希输入 ⇒ **改并发度会让依赖失效**（这是正确行为：
  输入确实变了）。
- `ResultRecord` 增加 `wave_size: int = 1` 冗余字段，只用于诊断与审计（判据仍以指纹为准）。

### 3.2 唯一的推算规则

新增共享函数（`backend/vnext/results.py`）：

```python
def wave_prior_groups(count: int, wave_size: int) -> list[list[int]]:
    """返回每个块"应当看到"的前序下标列表：块 i 看到 [0, wave_start)。"""
```

规则：`wave_start = (i // wave_size) * wave_size`，块 i 的前情 = 下标 `[0, wave_start)` 范围内
**已完成**的块（未完成处压 `None`，与现状一致）。

运行期与判定期**都调这一个函数**：

- 运行期：波内所有块用 `[0, wave_start)` 的 result_id 列表。
- 判定期：`classify` 内部对每个块用同一条规则推算期望依赖指纹。

> 当前 `classify` 的期望值由调用方通过 `context_fingerprints` 传入（`runner._expected_context_fingerprints`）。
> 本次要把"分组"这件事从 runner 挪进 `results`，让 runner 只负责提供"每块的前情窗口指纹"。

### 3.3 向后兼容

历史记录里没有 `wave_size`：`ResultRecord.from_dict` 缺省填 `1`。
于是老数据会被判 stale 一次（一次重跑），之后稳定。**这是可接受的**，且比"猜一个波宽"诚实。

### 3.4 调用点

| 位置 | 改动 |
| --- | --- |
| `runner.run_analysis(concurrency=N)` | 构造 `ResultStrategy(..., wave_size=N)`；波内用 `[0, wave_start)` |
| `runner._expected_context_fingerprints(...)` | 增加 `wave_size` 参数，按 `wave_prior_groups` 分组算窗口 |
| `vnext_service.result_strategy()` | 从 `config_vnext.json` 读 `analysis.concurrency`（缺省 1），写进 strategy |
| `scripts/e2e_real_book.py` | `--concurrency` 同时传给 strategy 与 runner |

## 4. 用例（先写红，再改实现）

| # | 用例 | 断言 |
| --- | --- | --- |
| 1 | `concurrency=8` 首跑后二次运行 | 第二次 **0 次调用**，`committed=32` |
| 2 | 同上，但第二次改用 `concurrency=1` | 判 stale 31（并发度变了，输入确实变了），**且报出原因** |
| 3 | `concurrency=1` 的既有行为 | `test_vnext_runner_analysis.py` 全部不改动即通过 |
| 4 | 波内有一块永久失败（跳过） | 该波后续块的前情里该项为 `None`；二次运行仍 0 调用 |
| 5 | 波宽 3、块数 8（末波不满） | 末波 2 块共享同一前情；二次运行 0 调用 |
| 6 | 历史记录（无 `wave_size`） | 判 stale 一次，第二次运行 0 调用 |

## 5. 验收

1. 全量 `python -m pytest -o addopts= -q` 通过（当前 551 例 + 新增约 8 例）。
2. `python scripts/acceptance_vnext.py` 32/32（若加了段则相应提高）。
3. **真书实测（hy3 网关）**：首次 32 块提交，**二次运行 0 次调用、0 token**。
4. `docs/VERIFY-LOG.md` 记录 business_test 与真书两行；`docs/data-contract-vnext.md` 增一节
   "波宽属于输入契约"；`agent.md` 增一条。

## 6. 风险

| 风险 | 处置 |
| --- | --- |
| 把分组逻辑挪进 `results` 会让 `classify` 依赖"块序" | 本来就需要（依赖链是前向滚链），但要在 docstring 写明"plans 必须是故事序" |
| 老数据一次性重跑 | 文档写明；可选提供 `--accept-legacy-wave` 之类开关，默认不加 |
| 波宽与 `block_size` 混淆 | 两个概念分开：`block_size` 决定"块怎么切"，`wave_size` 决定"几块同时跑"；两者都进 strategy，但只有 `wave_size` 影响前情分组 |
| 窗口指纹在"库更满"时是否仍一致 | 需要核实：`prompt_view.build_prompt_window` 按 `exclusive_before` 过滤后再裁剪，理论上与库的总量无关；**待复审确认** |

## 7. 待复审确认的三个问题

1. `build_prompt_window` 的裁剪（`limits`、`dropped`）是否真的只依赖"可见范围内的记录"？
   若它依赖全库统计（例如"最靠后的 N 条"是从全库取的），那么分组推算与运行期就会不一致，
   方案要改为"把窗口指纹显式落进结果记录"。
2. `classify` 目前只接受 `context_fingerprints` 映射；把它改成"传 wave_size + 由 classify 自己算"
   会不会破坏 `acceptance_vnext.py` 与既有测试的调用方式？
3. 末波不满、以及波内有永久跳过时，`prior` 里 `None` 的位置是否与运行期完全一致？

## 8. 复审一（v2）：放弃"重推期望"，改为自描述记录

第一轮复审判 **NEEDS_REVISION**，核出一条会让方案失效的硬事实：**分组规则与运行期不一致**。

- `runner.py:475-489` 不是按 `i // wave_size` 切波：它先把已完成块塞进 `prior` 且**不占波位**
  （`runner.py:480`），再连续填满 `wave_size` 个待跑块，快照在填满后才取（`:494`）。
- 于是带 gap 时，块 `i` 的 prior 里会包含**索引大于 i 的已完成块**。
  反例：32 块 / wave 8 / 块 3 永久失败 → 第二轮 `wave={3}`、`prior` 有 31 个 id，
  而 `wave_prior_groups` 给块 3 的期望是 `[]` → 仍永久 stale。

结论：**§3 的"由当前状态重推期望 prior"这条路在带 gap 时不收敛，重跑单块还会振荡。**
改为自描述记录。

### 8.1 新方案（v2）

`ResultRecord` 增加两个字段，由**写入方**落盘：

| 字段 | 内容 | 用途 |
| --- | --- | --- |
| `prior_result_ids: list[str \| None]` | 这块实际吃到的前情（按位置，未完成处 `None`） | 判据主体 |
| `context_fingerprint: str` | 该块 Prompt 窗口的指纹（已有，落到记录里） | 检测"同一批前情但窗口变了" |

判定规则（`classify`）：

```
for i, plan in enumerate(plans):
    rec = active(plan.block_signature)
    if rec is None: → MISSING
    if rec.status != SUCCEEDED: → 按状态处理（SKIPPED/FAILED 见 8.4）
    if rec.prior_result_ids != [当前 active id 或 None for 每个前序位置]: → STALE_DEPENDENCY
    if rec.context_fingerprint != 现算窗口指纹: → STALE_CONTEXT
    否则 DONE
```

这一版不需要 `wave_size` 参与判定——波宽只是**写入时的现象**，记录里的 prior 已经把
"这块到底看到了什么"说全了。`wave_size` 仍落进记录，但**只作诊断**，不参与判据。

### 8.2 wave_size 单一来源

`wave_size` 只由写入方（runner）落盘，`ResultStrategy` **不**新增该字段，`vnext_service`
也**不**从 config 读它喂给 strategy——否则"配置 8 / 实跑 1"会让记录与期望分叉。
（复审指出这正是会引入永久 stale 的地方。）

### 8.3 兼容

老记录缺 `prior_result_ids`：`from_dict` 填 `None`（**不静默填 1**），`classify` 给独立
原因 `LEGACY_NO_INPUT_TRACE`，行为是"重跑一次"。之后稳定。

### 8.4 失败/跳过块的语义（复审指出原 §4 用例 4 自相矛盾）

`SKIPPED`/`FAILED` 的 `status != SUCCEEDED`，现状下每次都会重跑。因此：

- 用例 4 断言改为：**其余 31 块 0 调用、该块 1 次**；
- 再加一条：**该块补跑成功后再跑一次 → 0 调用**（验证 gap 恢复后的收敛性）。

### 8.5 复审指出的其它必修项

1. **`vnext_service.py:312` 与 `queue.py:194` 传了 `strategy` 却没传 `context_fingerprints`** →
   现在就已经"记录有指纹、期望为空"→ 恒 stale。本批一并修，并补 `list_blocks` 的用例。
2. `e2e_real_book.py:268-270` 第二次运行没传 strategy 与 concurrency → 验收 3 不可能达成，补上。
3. 漏掉的调用点：`test_vnext_dependency_chain.py:70/94/112`、`test_vnext_results_queue.py:140-245`、
   `test_vnext_analyzer.py:130` 需同步。
4. `ResultStrategy` 三个字段都有默认值（`results.py:302-304`），计划 §3.1 的代码片段写错，v2 已删。
5. **`fact_id` 复用风险**（复审附注，独立于本方案但要记）：`analyzer.py:195` 采用模型给的
   `fact_id`，而 `knowledge.py:283-288` 全库按 `fact_id` 去重 → 后块可顶掉前块版本，
   更早块的窗口会随库变满而变。这条需要单独修（`fact_id` 应含块签名命名空间），
   否则 `context_fingerprint` 的判据会在长期运行中慢慢失真。

## 9. 验收（v2）

1. 全量 pytest 通过。
2. `acceptance_vnext.py` 32/32。
3. **真书（hy3 网关）**：首次 32 块提交 → **二次运行 0 调用、0 token**。
4. 新增用例覆盖：`concurrency=8` 两次同参 0 调用；gap 内重跑一次后 0 调用；老记录只重跑一次；
   换模型/换 Prompt 版本 → stale；换 block_size → missing/stale。
