# PLAN v2.2 修订总表（完整）

- 基线：`5937cb1`（S0 已提交，892 用例）
- 前序：`docs/PLAN-v2-long-novel-reliability-2026-10-30.md`（v2.1，536 行，T01–T44）
- 复审依据：`docs/REVIEW-post-S0-plan-findings-2026-10-30.md`（R-1–R-7 + 附带更正）
- 本文件性质：**完整修订规格**。逐条给"目标位置 / 现状 / 改为 / 依据"。执行者照此落文，不需要再判断。
- 分工：**文档由父 agent（本节作者）与外部审查完成；主会话只改代码（S1 起），不改文档。**

---

## 0. 文档治理（新增，对应 R-7）

### 0.1 每份计划/报告文档的必备头部

```yaml
plan_version: 2.2
doc_sha256: <正文 body 的 sha256，不含 frontmatter>
supersedes: 2.1
superseded_by: null
baseline_commit: 5937cb1
```

### 0.2 验证记录表（只追加，不修改历史行）

| 字段 | 含义 |
|---|---|
| `verified_commit` | 验证时的 HEAD |
| `verified_date` | 日期 |
| `scope` | 本次验证覆盖的验收项范围，例如 `T01–T44` |
| `result` | 结论（通过 / 部分通过 / 未跑） |
| `doc_sha256` | 该次验证针对的文档哈希 |
| `superseded_note` | 后续修订对本次结论的影响 |

硬规则：

- **历史验证行只允许追加，禁止修改数字或结论**。v2.1 的"42 项、diff 可逆"记录保留原样。
- 新增验收项（T43/T44/T45–T47）必须**新增一行**验证记录，不得把旧行的 42 改成 44。
- 已过期产物（`docs/patches/PLAN-v2-autonomous-resolution-2026-10-30.patch`、`docs/REPORT-plan-v2-autonomous-merge-2026-10-30.md`）在头部加一行：

  > 本文件为历史版本，对应验收项 T01–T42 / 合入时文档 hash `<hash>`。当前规范见 PLAN v2.2。

---

## 1. 修订项总览

| 编号 | 修订内容 | 目标位置 | 依据 | 阻塞批次 |
|---|---|---|---|---|
| D-01 | typed verdict 贯通契约 + 载体清单 | 新 §2.5；S1 交付物 | R-1 | **S1 前** |
| D-02 | 逐入口"旧形状 → 新类型"清单（7 文件 18 点） | S1 交付物 + 附表 A | R-2 | **S1 前** |
| D-03 | 最小只读诊断输出（不建页面） | S5 交付物；§1.3 扩展 | R-3 | S5 验收前 |
| D-04 | 资源护栏：字节/单项/并发复制/峰值 | 新 §3.3；S3 交付物 | R-4 | S3 前 |
| D-05 | 冻结选择计划（排序→装箱→预留→派发） | §3.2 改写；S7；T42 扩 | R-5 | S7 前（现在写） |
| D-06 | RECENT↔STALE 正交矩阵 | T24 改写 | R-6 | S5/S6 |
| D-07 | 版本追踪与历史保留 | 新 §0 | R-7 | 立即 |
| D-08 | `max_verified_facts` 表述更正 | BASELINE `:75`；报告 C-21 | 附带更正 | 立即 |
| D-09 | 批次映射更正 C-04→S3 / C-07→S8a / C-09→S2-S8b | BASELINE `:98/:100/:101` | 附带更正 | 立即 |
| D-10 | C-21 拆成"1 项未接线 + 3 项无配置字段" | 报告 C-21 | 附带更正 | 立即 |
| D-11 | 864 → 892 全量替换 | 报告 / S6 补丁 | S0 交付 | 立即 |
| D-12 | patch / merge report 标历史版本 | 两文件头部 | R-7 | 立即 |
| D-13 | `recheck_status` + 覆盖汇总进 §2.3 | §2.3 schema | 前序遗留 | **S5 前** |
| D-14 | 语料真值索引 §S0.1 | 新 §4.0.1 | 前序遗留 | 立即 |
| D-15 | 基线文档路径更正 | §4 S0 交付物 | 前序遗留 | 立即 |
| D-16 | 本地 `pytest-timeout` 策略定案 | BASELINE 未达标项 | 前序遗留 | 立即 |
| D-17 | S5/S10 范围缩减（C-17 已由 S0 关闭） | §4 S5、S10；§7 覆盖表 | 前序遗留 | S5/S10 |
| D-18 | §1.3 同批契约扩展到诊断输出 | §1.3 | R-3 | S5 |

---

## 2. 逐项规格

### D-01 · typed verdict 贯通契约（新 §2.5）

**位置**：主计划新增 §2.5「错误判定类型（ErrorVerdict）贯通」。

**正文**：

> 流式 error chunk 与终结结果携带本次调用的 `ErrorVerdict`（`cls` / `confidence` / `retry_after` / `source_channel`）；attempt 返回对象保留该字段。取消、解析失败和正常完成**不得**挪用其他尝试或并发调用的判定。
>
> 所有重试层与外层补跑**优先消费 typed verdict**；仅在其缺失时，才允许按"来源受限"的有限兜底（`classify_message` 且强制 `confidence=text` → 只可 `backoff`，永不可 `stop`）。

**载体清单**（实施契约，逐层不可跳过）：

| 层 | 位置 | 现状 | 改为 |
|---|---|---|---|
| chunk | `backend/core/llm_stream.py:35` | `error: str` | `error: str` + `verdict: ErrorVerdict \| None` |
| 终结结果 | `backend/core/llm_stream.py:43` | `error: str` | 同上 |
| attempt | `backend/core/llm_client.py:788` | `attempt_error = chunk.error` | 同时取 `chunk.verdict`，attempt 返回值携带 |
| 重试层 | `:840` / `:991` / `:1238` / `:1391` | `_error_action(err, moderation_hits)` 四次从字符串重猜 | 优先用 `verdict`；四处收敛为单点 |
| chat_auto | `:1108` | StreamResult → 五元组 | 五元组中保留 verdict 字段 |
| analyzer | `backend/core/analyzer.py:159` | 只传播错误字符串 | 透传 verdict |
| pipeline | `backend/core/pipeline.py:711` | `classify_message(err_text)` 二次猜 | 消费上层 verdict，不再重分类 |

**边界**：只收窄"判停"的准入，**不得**禁止正常温度退火与指数退避。纯文本数字码误报（C-01）仍须独立修复，不得以"已改流式边界"代替。

### D-02 · 逐入口调用清单（S1 交付物 + 附表 A）

**位置**：附表 A 新增于主计划 §5 之后。

| 文件 | 调用点数 | 分布 |
|---|---|---|
| `backend/core/analyzer.py` | 1 | chat_auto：159 |
| `backend/core/style_analyzer.py` | 1 | chat_auto：357 |
| `backend/services/final_summary.py` | 4 | chat_auto：625、646、667、690 |
| `backend/services/location_normalizer.py` | 2 | chat：557、617 |
| `backend/core/pipeline.py` | 4 | chat：928、1104、1149；chat_with_retry：1061 |
| `backend/services/queue_manager.py` | 1 | chat：304 |
| `backend/core/llm_client.py` | 5 | chat_stream：778；chat_stream_with_retry：1102；chat_with_retry：1116；chat：1181、1334 |

合计 **7 个生产文件 / 18 个调用点（外部 13、内部 5）**。此数为静态调用点统计，不是运行次数上限。

**返回形状**（现状，勿写错）：

| 方法 | 形状 |
|---|---|
| `chat` | 四元组（`llm_client.py:267–272`） |
| `chat_with_retry` | **五元组**（`:1142–1148`） |
| `chat_auto` | 五元组（`:1084–1099`） |
| `chat_stream` | async generator，产出 `StreamChunk` |
| `chat_stream_with_retry` | `StreamResult`（`:705–712`） |

S1 交付物要求：

1. 逐入口「旧形状 → 新类型」表
2. 同步上述 18 点的消费者
3. 同步所有 mock/fixture（`test_llm_mock.py`、`test_streaming.py`、`test_anthropic_provider.py`、`test_summary_checkpoint.py` 等）
4. 新的共享类型放在**不会产生循环导入**的位置（建议 `backend/core/error_taxonomy.py` 内定义 `ErrorVerdict` dataclass，`llm_stream.py` 从该处导入）

### D-03 · 最小只读诊断输出（S5）

**位置**：S5 交付物新增第 N 条 + §1.3 补充。

> **最小只读诊断输出**：在已有只读 API（或一个诊断命令）中输出以下字段，**不新建功能页、不产生模型调用**：
>
> - 数据格式版本、来源 revision
> - `occurrence` / `entity` / `candidate` 计数
> - `resolution_state` 分布
> - `recheck_status`
> - 覆盖与跳过原因（分 `reason_code`）
>
> 未运行总结时按 T43 显示 `NOT_RUN`，**不得**以空 candidate 集合冒充"已核对"。

### D-04 · 资源护栏（新 §3.3）

**位置**：主计划新增 §3.3「资源约束」。S3 交付物同步。

```
S3 必须定义的资源门槛：
1. KB 缓存的条目数上限 与 估算字节预算（KB_CACHE_BUDGET_MB）
2. 单项超预算 → 不缓存，记 SKIP_OVERSIZE，并计入诊断输出
3. KB 构建/复制的并发上限（KB_BUILD_CONCURRENCY）
4. 可观测峰值：主 KB + results + 快照 + 临时复制，分别记录估算字节
5. 达到预算时的行为：安全小视图 / 按需读取 / 可恢复的"资源不足"退出
6. 禁止以删除磁盘全史来换内存（与 §3.1「全史不删」冲突）
```

明确约束：**"条目数有界"不得被表述为"字节有界"的证明。**

### D-05 · 冻结选择计划（§3.2 改写 + S7 + T42）

**位置**：§3.2 预算一节整体改写。

```
recheck 选择计划（确定性）：
1. 固定来源、策略版本、缓存快照与单条/单批容量；先排除本质超限项并披露原因
2. 对可执行项稳定排序：重要度降序 → 埋设故事位置升序 → 稳定来源/实体/候选逻辑键
   （随机 UUID 只有在已持久化并复用时才能作为最后 tie-breaker）
3. 确定性装箱：批次边界不得由剩余运行预算临时改变
4. 在固定重试预留规则下，选择"能支付的最长批次前缀"；到首个不能支付的批次即停止，
   不得跳到后面的便宜项
5. 全部未入选项标记 BUDGET_SKIPPED（未执行态），**不是** INSUFFICIENT；恢复时复用同一计划
6. 排序键、装箱结果、入选集合必须先持久化，再并发派发
```

**预算单调性**为可选产品约束（非自动结论）：若采用，须满足"仅放宽运行预算、其他输入与缓存固定时，入选集合是原集合的前缀超集"。一般贪心**不满足**（A 耗 8、B 耗 5，预算 5 选 B，预算 8 选 A，B 反被挤出），必须按上述第 1–4 步实现。

### D-06 · T24 改写（RECENT↔STALE 正交）

| 原 T24 | 改为 |
|---|---|
| 断言 `recency_state` 与 `resolution_state` 正交（无具体方法） | 固定 occurrence、来源、当前适用裁决与候选集，**通过改变纯显示阈值**，或在不改变裁决适用范围时推进显示观察时点，使 RECENT/STALE 双向切换；断言 `OPEN` / `PROPOSED_RESOLVED` / `AUTO_RESOLVED` 三种状态及其支撑候选**均不改变**，且不新增模型调用 |

**禁止**：在真实集成测试中直接篡改本应从 occurrence 派生的 `last_seen`，然后声称"来源没变"——那是在测试不合法的内部状态。

### D-07~D-12 · 文档更正（立即执行）

| 编号 | 目标 | 现状 | 改为 |
|---|---|---|---|
| D-08 | `docs/BASELINE-2026-10-30.md:75` | "`max_relationships` / `max_verified_facts` / `max_themes` / `max_content_chars` **从未生效**，且这 4 项不在 config 中" | "`backend/core/analyzer.py:61-69` 只把 7 个参数传进 `PromptBuilder`。其中 `max_verified_facts` **在 config 中存在**（`backend/config/settings.py:188`）但**未接线**；`max_relationships` / `max_themes` / `max_content_chars` **无 config 字段**，仅有 `prompt_builder.py:100-103` 的默认值" |
| D-09 | `docs/BASELINE-2026-10-30.md:98/:100/:101` | C-04→S4、C-07→S5、C-09→S9 | C-04→**S3**、C-07→**S8a**、C-09→**S2/S8b**（保留采集数字，只改归属列） |
| D-10 | `docs/REPORT-review-consolidated-2026-10-30.md` C-21 | "这 4 项配置项不存在，无法调" | 拆成：① `max_verified_facts` 存在但 `analyzer.py:61-69` 未传（修法：补传 1 行）② 另 3 项无 config 字段（修法：`settings.py` 加 3 字段） |
| D-11 | 报告与 S6 补丁的用例数 | 864 | **892**（S0 新增 28：21 代理守卫 + 7 语料自检） |
| D-12 | `docs/patches/…patch`、`docs/REPORT-plan-v2-autonomous-merge…md` | 写"验收场景 42 项" | 头部加历史版本标注，正文不改（保留历史事实） |

### D-13 · §2.3 ledger schema 补字段

```python
ledger {
  schema_version: 1
  recheck_status: NOT_RUN | RUNNING | COMPLETED | PARTIAL | STOPPED

  recheck_task: {
    task_key, strategy_version, source_revision,
    started_at, finished_at
  }

  recheck_coverage: {
    candidates_total, submitted, evidence_located,
    supported, insufficient, conflict,
    invalid_by_reason: { QUOTE_NOT_FOUND, QUOTE_AMBIGUOUS, WRONG_ENTITY,
                         ORDER_VIOLATION, NO_COUNTER_SEARCH },
    stale_source,
    not_checked: { count, by_reason: { BUDGET_SKIPPED, FAILED, CANCELLED } },
    llm_calls, cost
  }
}
```

约束：

- 覆盖汇总缺失时**不得**标记 `COMPLETED`
- `recheck_status` 与 `resolution_state` **正交**，不得互相代替
- S5 定 schema 时必须预留这两个字段（这是 D-13 列为"S5 前"的原因）
- **状态判定按顺序、互斥**：未进入流程 → `NOT_RUN`；执行中 → `RUNNING`；已结束且被取消 → `STOPPED`；已结束且 `not_checked.count > 0`（**无论是否另有 `supported`**）→ `PARTIAL`；已结束、无 `not_checked`、汇总已提交 → `COMPLETED`
- `not_checked` 已是最终分类，故**不得**与"全部候选都有最终分类"并存判为 `COMPLETED`
- 候选为空时，只有**实际完整执行过一次复检流程**才记 `COMPLETED` 且计数为 0；未执行仍记 `NOT_RUN`，使"没有候选"与"没有运行"可区分
- **候选维度守恒式**：`candidates_total = supported + insufficient + conflict + invalid_by_reason 合计 + stale_source + not_checked.count`；`submitted` / `evidence_located` / `llm_calls` / `cost` 属过程计数，**不参与该等式**

### D-14 · 新增 §4.0.1 语料真值索引

| `CorpusTruth` 键 | 语料内容 | 对应验收项 |
|---|---|---|
| `MISSING_CHAPTERS=(17,)`（24 章实际 23） | 断号 + 多章块跨缺口 | T06–T09、R1 块起始章 |
| `FORESHADOWS[...].plant=3 / resolve=20` | 正常回收（跨卷） | T19 / T20 正样本 |
| `plant=8 / resolve=12` | 回收跨越卷边界 | T18 前缀视图 |
| `same_chapter_pair` plant=12 / resolve=12 | 同章先埋后收 | R4 其他修订① 的验收 |
| `AMBIGUOUS_QUOTE`（第 19 章「他又提起那顶铜匣」） | 真实存在、词面相关、**非回收** | T35 |
| `CO_PRESENT_ONLY_PAIR`（第 15 章 城东/城西） | 同章无互动 | T13 负样本（C-06） |
| `INTERACTING_PAIR`（第 14 章 对话） | 有互动 | T13 正样本 |
| `RETRY_CHAPTERS=(9, 22)` | 补跑缺口 | T10 / T11 / T42 |

规则：**每个涉及负样本的验收项必须标注所用语料章号**，实现者不得自造样本。

### D-15 · 基线文档路径

§4 S0 交付物原写 `docs/audit/baseline-<commit>.md` → 改为 `docs/BASELINE-<date>.md`，并补注：

> `docs/audit/`、`docs/plans/`、`docs/superpowers/plans/` 被 `.gitignore`（`:64`–`:66`）忽略，**不要**往这些目录放需要入库的文档。

### D-16 · 本地 pytest-timeout 策略

采用"**明确接受**"：

> CI 以 `-p pytest_timeout --timeout=180` 强制超时；本地不强制安装该插件，以免耦合开发机环境。本地无超时保护属**已接受**的已知限制，登记于 `docs/BASELINE-2026-10-30.md:104`。若出现本地 hang，以 `Ctrl-C` 终止并按用例子集重跑定位。

### D-17 · S5/S10 范围缩减

- C-17（零 CI / 无 timeout / 无 coverage）**已由 S0 关闭**，§7 覆盖表对应行改为「S0 已解决」
- S10 的基建项（CI、依赖、基线段）从"待做"改为"已由 S0 完成，S10 只做剩余文档与发布收尾"

---

## 3. 验收矩阵增量

现有 T01–T44 保留。新增与改写：

| 编号 | 名称 | 触发批次 | 断言 |
|---|---|---|---|
| **T24**（改写） | recency 与 resolution 正交 | S5/S6 | 见 D-06。改显示阈值切换 RECENT/STALE，三种 resolution 状态及其支撑候选不变，无新增模型调用 |
| **T42**（扩写） | 预算冻结与前缀单调 | S7 | ① 乱序派发 / 重放结果不变 ② 仅放宽运行预算、其他输入与缓存固定时，入选集合为原集合的前缀超集 ③ 未入选项为 `BUDGET_SKIPPED` 而非 `INSUFFICIENT` |
| **T45** | verdict 端到端贯通 | S1 | 一条可信结构化永久错误（如 1027 审核）：流式与非流式**均首次明确停止**；外层不补跑；显示文本**不含可识别错误码**；`Retry-After` 与费用统计仍正确 |
| **T46** | 诊断输出完整性 | S5 | 诊断输出含格式版本、来源 revision、entity/occurrence/candidate 计数、resolution 分布、`recheck_status`、跳过原因分项；未运行总结时显示 `NOT_RUN` |
| **T47** | 资源护栏 | S3 | 超字节预算的单项不进入缓存并记 `SKIP_OVERSIZE`；并发构建受上限约束；峰值可观测；超限走可恢复退出，不删磁盘全史 |

合计 **T01–T47**。

---

## 4. 批次增量（S0–S10）

| 批次 | 状态 | 增量 |
|---|---|---|
| S0 | **已完成**（`5937cb1`） | 交付已含代理净化、CI、语料、基线。仅需按 D-15/D-16 补文档说明，**不重做、不撤回** |
| S1 | 待开工 | 增 D-01 契约、D-02 清单、T45 |
| S2 | 未变 | — |
| S3 | 增 D-04 资源护栏、T47；补正 C-04 归属 |
| S4 | 未变 | — |
| S5 | 增 D-03 诊断输出、T46；D-13 的 schema 预留；C-17 关闭后范围缩减 |
| S6 | 已完成设计（全自动裁决） | 无新增 |
| S7 | 增 D-05 冻结选择计划、T42 扩展 |
| S8a | 熔断（C-07） | 归属更正 |
| S8b | 写入一致性（C-09 部分） | 归属更正 |
| S8c | CI 类 | **范围缩减**（S0 已完成） |
| S9 | 大规模优化与基准 | 若 S3 发现基础结构不满足资源预算，必要的按需读取**不得**一律推迟到 S9 |
| S10 | 发布收尾 | 范围缩减（基建已完成） |

---

## 5. 完整验收清单（T01–T47）

| 范围 | 内容 |
|---|---|
| T01–T04 | 错误分类与调用结果贯通（S1） |
| T05 | 取消 / 审核跳过 / 预算不足的状态区分 |
| T06–T09 | 新格式入口、来源模型、格式校验、旧产物拒绝（S2） |
| T10–T11 | rolling 有序化与缺口恢复（S4） |
| T12 | 人物状态时序（S3） |
| T13 | 共现与语义关系分离（S3，用语料第 14/15 章） |
| T14 | 知识库事实带来源与验证态（S3） |
| T15–T18 | 稳定实体身份、悬念区分、截至视图（S5） |
| T19–T21 | 候选无效 / 无证据 / 冲突 / 幂等与身份不挪用（S6） |
| T22–T23 | 复检缓存与确定性重放（S7） |
| **T24** | recency 正交（改写） |
| T25 | 全局熔断（S8a） |
| T26–T27 | 聚合原子写与并发读（S2/S8b） |
| T28 | CI 与超时（**S0 已部分完成**） |
| T29–T31 | 前端契约、竞态、XSS（S8d） |
| T32 | 配置字段接线（S3） |
| T33 | 事实来源贯通到报告（S3） |
| T34 | 桌面端与打包（S10） |
| T35–T39 | 语义负样本：复述否定、答案冲突、上下文截断、引用定位三态、说谎/梦境/假设 |
| T40–T41 | 同实体不同悬念不合并；同悬念复述应合并 |
| T42 | 预算冻结与前缀单调（**扩写**） |
| T43–T44 | 未运行总结标注 / 候选为空仍记完成 |
| **T45** | verdict 端到端贯通（新） |
| **T46** | 诊断输出完整性（新） |
| **T47** | 资源护栏（新） |

---

## 6. 覆盖表更正（C-01..C-27）

| C 编号 | 批次 | 验收 |
|---|---|---|
| C-01 | S1 | T01–T04、T45 |
| C-02 / R1 | S2 / S3 / S4 | T06–T09 |
| C-03 | S4 | T10、T11、T42 |
| C-04 | **S3** | T12 |
| C-05 / C-10 / R4 | S6 | T19–T21、T35–T37 |
| C-06 | S3 | T13 |
| C-07 | **S8a** | T25 |
| C-09 | **S2 / S8b** | T26、T27 |
| C-11 / R5 | S7 | T22、T23 |
| C-12 | S3 | T14、T33 |
| C-13 | S2 / S3 / S9 | — |
| C-14 / C-15 | S9 | — |
| C-16 | S0 / S8c | T28 |
| C-17 | **S0 已完成** | T28（部分） |
| C-18 / 19 / 20 | S8d | T29–T31 |
| C-21 | S3 | T32 |
| C-22..C-25 | S10 | T34 |
| C-26 | S9 | 无证据则 DEFER |
| C-27 / R2 / R3 | S5 / S7 | T15–T18、T23、T38、T40–T42 |

---

## 7. 分工与红线

| 角色 | 职责 | 禁止 |
|---|---|---|
| 主会话 | S1 起的**代码**实施 | 不改 `docs/PLAN-v2*.md`、不改 `docs/BASELINE*.md`、不改 S6 补丁 |
| 本会话（父 agent） | 全部**文档**修订（D-01–D-18）、S6 补丁、统合报告 | 不改 `backend/` 业务代码 |
| 外部审查 | 复核 v2.2、R 系列跟踪 | 不动笔改文档 |

**唯一例外**：若代码实施发现文档契约不可实现，主会话**必须**先提 issue 式留言，由文档侧修改，不得边改代码边改计划。

---

## 8. 执行顺序

1. D-08–D-12（立即，纯更正，零风险）
2. D-07 文档治理头（立即，为后续所有修订建立版本锚）
3. D-14 语料索引 + D-15 路径 + D-16 timeout（立即）
4. D-13 `recheck_status` 进 §2.3（**S5 开工前必做**）
5. D-01 / D-02（**S1 开工前必做**）
6. D-03 / D-04 / D-05 / D-06 / D-18（可并行，各自批次验收前完成）
7. D-17 范围缩减（S5/S10 开工前）
8. 全部完成后，新增一条验证记录：`scope: T01–T47`，`doc_sha256: <v2.2 hash>`

---

## 9. 复审二增量（v2.2.1）

第二轮复审提出 5 项，均已在主计划 v2.2 落文。本节记录对**本规格**的相应修正，避免执行者按旧文回退。

| # | 问题 | 修正 |
|---|---|---|
| 1 | `scope` 词汇两套并存：本规格 D-01 的 plan 侧沿用 `CALL/BLOCK/ACCOUNT`，而 S1/S8a 与 T03/T25 用 `ACCOUNT_GLOBAL/REQUEST_LOCAL/TRANSIENT/UNKNOWN` | 统一为后者；保留 `UNKNOWN`，作用域不明**不得**凭字符串升级为账户熔断；`action` 由唯一策略函数计算并**接收重试上下文**（连续审核命中、剩余预算与轮数），不能只按类别推导 |
| 2 | `recheck_status` 判定重叠：`not_checked` 同属最终分类，可同时满足 `COMPLETED` 与 `PARTIAL` | 改为**有序互斥五条规则** + **候选维度守恒式**，见 D-13「约束」 |
| 3 | 资源护栏的可验证对象不清（RSS 不可跨环境断言） | 断言对象是**统一估算字节账本 + 在途预留 + 并发许可**，不是进程 RSS（RSS 仅作基准观测）。`pipeline.py:691` 用 `asyncio.to_thread`，**取消等待方不终止工作线程**，预留须等线程真正结束才释放。句柄 / 日志 / 临时目录各有归属阈值 |
| 4 | 附表 A 冻结口径过严（"点数与文件集合不得变化"） | 改为**冻结覆盖责任**：原 18 条逐条注明保留 / 替代 / 删除及其覆盖情况；改造后全部生产调用点使用新契约、无位置式解包；抽取公共调用函数致点位数量变化不算遗漏 |
| 5 | T45/T47 缺自动化方式 | **T45**：SDK mock 依次返回结构化 429 与 AUTH（显示串均不含码），替换等待函数，断言等待参数 / 2 次尝试 / 终态 AUTH / 外层不补跑 / DTO 往返。**T47**：预算 100、并发 2、两任务各预留 60，用屏障保持第一个在途，断言第二个在构建前排队或拒绝；取消第一个等待方后预留保持到线程真正结束 |

**D-12 更正**：历史产物**不修改原字节**，改由 `docs/VERIFY-LOG.md` 外置索引标注适用版本。

**收口结论**：统一 `scope` 后 S1 可开工；覆盖汇总在 S5 前收口，资源计量在 S3 前收口。
