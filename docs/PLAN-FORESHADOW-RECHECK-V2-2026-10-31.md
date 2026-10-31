# PLAN：伏笔复检 V2（v3 修订版）

- 日期：2026-10-31
- 版本：v3（取代同日 v2；v2 被复审判 8 条 P1 + 2 条事实更正）
- 状态：待批准执行
- 底座：**1.x 现役管线**；老结果撇掉重跑；整本分析期间不启动总结
- 用户已定：账本**备份后重建**（不写历史迁移代码）；两章试跑须等前置闭环修好、并先固定调用/token 上限

---

## 0. 本轮修订对照（R1–R8 → 处置）

| # | 复审意见 | 本版处置 |
| --- | --- | --- |
| R1 | 新字段会在解析/去重途中丢失；`anchor_stats` 动态赋值不序列化 | §4.1 全链路字段传递契约 + 端到端往返用例 |
| R2 | D1 把"合并块"当"本章"；`located=True` 不能作准入（省略号拼接也返回 located） | §4.2 块内章节归属 + 只接受 `EXACT/NORMALIZED/RELOCATED` |
| R3 | 后置复检缺"读原文"接口；`full_text` 实为卷摘要拼接 | §4.3 显式 `ChapterReader` + `evidence_id` 绑定正文版本 |
| R4 | 身份公式仍依赖模型措辞，换引文即换键 | §4.4 身份首次登记即持久；指纹只用于去重/缓存 |
| R5 | 九条门禁不全；`resolution.py:384` 越界位置漏洞 | §4.5 门禁补齐 + 越界回验（**已修复并回归**，见 §9） |
| R6 | 阶段 1/2 状态权限未划清；命名混乱 | §4.6 阶段 1 只产候选；`AUTO_RESOLVED` 唯一写入方 |
| R7 | "预算化、可续跑"只有目标没有执行契约 | §4.7 预留/计费/任务键/落盘/恢复/失效 六项契约 |
| R8 | 回退承诺过宽；验收缺产品入口集成测试 | §6 开关只切算法不恢复数据；§7 增产品入口集成用例 |
| — | `active=0` 不能证明"历史上从未执行复检" | §2 事实表述已更正 |
| — | C2 偏移问题来自 vnext 评测解析器，不能归因 1.x | §2 已区分"评测口径"与"现役实现" |

---

## 1. 目标

让每条伏笔从识别起就带**可回验的原文坐标**；把总结阶段第 2 步换成
**证据可核验、覆盖可计数、结论可失效**的实现。不重写分析链路，不换数据空间。

---

## 2. 现状（更正后的事实）

| # | 事实 | 证据 |
| --- | --- | --- |
| 1 | 伏笔记录无原文锚点，`clue` 是模型概括 | `models/analysis_result.py:32`；真实结果 `clue = "内容简介预告许七安起初只想自保…"` |
| 2 | 伏笔身份按出现顺序编号 | `utils/text_utils.py:325` `f"fs_{g_idx+1:03d}"` ⇒ 补一条线索全体位移 |
| 3 | 复检在候选为空时直接 return | `services/final_summary.py:1386`、`:1400` |
| 4 | 真实账本 `active = 0`（`resolved 154 / dormant 546`） | 这意味着**当前这一轮会跳过复检**；不能据此断言历史上从未执行过 |
| 5 | 休眠判据 200 章硬阈值，大量伏笔被判死 | `utils/foreshadow_ledger.py:105` |
| 6 | `resolved` 仅凭 `is_resolved_in_this_batch` | `final_summary.py:1322-1336` |
| 7 | C2 的 30 条标注里 11 条因 span 对不上被判 INVALID | 来源是 **vnext 评测解析器**（`backend/vnext/resolution.py`），属评测口径；不能直接推断 1.x 现役复检的行为 |

---

## 3. 改动边界

```
前置（本轮加字段 + 落盘前校验）
  analyzer.analyze_chapter(:95) ← pipeline.py:705 传入的是**块**（多章拼接）
  → prompt_builder.build_messages(:320)
  → validate_json_response 内解析 → AnalysisResult.from_dict（手写字段表）
  → 写 chapter_N_result.json → memory_state 累积

后置（本轮只换阶段 2）
  阶段1 分卷摘要 + _build_foreshadow_catalog(:789) → 候选
  阶段2 _run_global_foreshadow_recheck(:1380)      → 整块替换
  阶段4 :1720 audit_text_for_report                 → 四栏渲染
```

**不动**：`pipeline.py`、`memory_state` 合并逻辑、结果文件命名、前端、`backend/vnext/`。

---

## 4. 八条契约（本版核心）

### 4.1 字段全链路传递（R1）

新增字段必须在**五处**都被传递，缺一处即等于没加：

| 环节 | 位置 | 要求 |
| --- | --- | --- |
| 模型输出 | `prompt_builder.py:57` | schema 增 `anchor_quote`、`motif_key` |
| 解析 | `analysis_result.py` `Foreshadowing.from_dict` **手写字段表** | **必须显式读取**新字段（`asdict` 只帮写、不帮读） |
| 块级统计 | `AnalysisResult` | `anchor_stats` 必须是**dataclass 字段**（`total/located/missing`），不得动态赋值 |
| 恢复 | `AnalysisResult.from_dict` | 同上，缺省给 `{0,0,0}` |
| 目录 | `final_summary._build_foreshadow_catalog(:789)` | 候选结构从"五元组"改为带锚点的 dict |
| 去重 | `text_utils.deduplicate_foreshadows(:193)` | 重建字典时**保留** `anchor_quote/anchor_chapter/motif_key` |
| 账本 | `final_summary.py:1526` | 建条目时带上三字段与 `entity_key` |

**验收用例（端到端往返）**：构造一条带锚点的 `AnalysisResult` →
`to_dict` → `from_dict` → `_build_foreshadow_catalog` → `deduplicate_foreshadows` → `ForeshadowItem`，
逐字段断言锚点/母题/章号仍在。**这条用例是所有后续工作的地基。**

### 4.2 块内章节归属（R2）

`pipeline.py:705` 传给 `analyze_chapter` 的是**块**（`block_size` 章拼接）。因此：

- `analyzer` 拿到章节列表与每章起始偏移，构造 `chapter_spans: [(chapter_number, start, end)]`
- 锚点定位在**块文本**上做，命中后反查落在哪个 `chapter_span` ⇒ 得 `anchor_chapter` 与
  `anchor_offset_in_chapter = pos - span.start`
- 同时记录该章 `content_hash`（`sha256(normalize(text))`），用于 evidence 绑定正文版本
- 跨章边界的引文（同时跨两章）一律判缺失：那说明模型在拼接

**准入只认三种匹配级别**：`EXACT` / `NORMALIZED` / `RELOCATED`。
`SEGMENTED`（省略号拼接）与 `FUZZY`（相似即过）**一律拒绝**——复现实测：
`verify_citation` 对省略号拼接引文返回 `SEGMENTED` 且 `located=True`，
若用 `located` 作准入，L1 指标会虚高。

多处匹配：取首次出现并记 `ambiguous=True`；若模型给了 `anchor_span`，只允许用于**在若干命中里挑一处**，
最终位置必须重新切片回验（见 §4.5）。

### 4.3 后置复检的原文接口（R3）

现状 `final_summary.py:1404` 的 `full_text` 是**卷摘要拼接**，不是原文；
拿它核验等于在核验模型自己写的话。因此新增：

```python
class ChapterReader(Protocol):
    def text(self, chapter: int) -> str: ...          # 该章正文（来自 blocks/ 或结果快照）
    def content_hash(self, chapter: int) -> str: ...   # 该章正文规范化哈希
```

`evidence_id` 规范（程序算，不由模型给）：

```
evidence_id = sha256(f"{book_id}|{chapter}|{content_hash}|{start}|{end}")[:12]
```

复检流程：

1. 由**卷摘要**定位候选的"回收段候选章范围"（摘要只用来缩小范围，不作为证据）
2. 用 `ChapterReader` 读这些章的原文
3. 模型只输出 `evidence_chapter` + 逐字 `quote`（不给位置）
4. 程序在该章原文里定位 → 算 `start/end` → 生成 `evidence_id` → 回验切片
5. 上下文窗口按句界取材，截断则记 `context_truncated`（见 §4.5）

**红线**：任何"用摘要当证据"的路径都算实现错误，用例要覆盖"摘要里有的句子、原文里没有"必须判证据不足。

### 4.4 身份稳定（R4）

- `entity_key` **首次登记时生成并持久化**，之后不再由内容重算
- 新候选只能走两条路：**匹配到已有实体**（`motif_key` 相同 + 锚点相似 + 章号邻近）或**新建实体**
- `anchor_quote` 指纹只用于：去重、缓存键、失效检测 —— **不参与身份**
- 空锚点候选**不得**参与身份派生；只能挂在已匹配实体下，否则记为 `unlinked` 候选

### 4.5 门禁与位置回验（R5）

九条判据（在 v2 基础上前置两条、补一条）：

1. `verdict == SUPPORTED`
2. `answer` 非空
3. 无 `material_insufficient_reason`
4. `counter_search.performed is True` 且 `counter_quotes` 为空
5. `evidence_id` 属本次复检、且与 `ChapterReader` 的 `content_hash` 一致（正文变过即失效）
6. `supporting_quote` 非空且长度 ≥ 6
7. 引文在该章原文里定位成功，匹配级别 ∈ {EXACT, NORMALIZED, RELOCATED}
8. **位置必须由程序重算并回验**：`text[start:end]` 等于引文（EXACT）或规范化后相等（其余）；
   模型自报位置只能用于在多点命中里挑一处
9. `checked_evidence_ids` 非空且全部属本次任务
10. **回收章 ≥ 埋设章**（同章先埋后收允许；早于埋设章判 `DIFFERENT_ANSWER` 异常）
11. 上下文截断时若**截断区可能包含反证**，降级 `INSUFFICIENT` 并记 `CONTEXT_TRUNCATED`

> R5 提到的越界漏洞已在 vnext 侧复现并修复（`8f9d191`）：模型给 `[10000,10020]`、原文 26 字，
> 旧实现落出越界位置仍判 SUPPORTED。修复后：模型位置只能挑一处，位置一律程序算 + 切片回验。
> **这条回归要先移植进 1.x 再开跑。**

### 4.6 状态权限（R6）

| 阶段 | 允许做什么 | 禁止 |
| --- | --- | --- |
| 阶段 1 分卷摘要 | 产出候选、待核对答案、候选的埋设章与锚点 | **禁止**设置任何"已回收"状态 |
| 阶段 2 复检 | 唯一有权写 `AUTO_RESOLVED` 的地方 | 单条候选失败时**不得**覆盖其他候选的有效结论 |

命名统一：

- 伏笔状态：`open` / `resolved` / `dormant`（`active` 全部改为 `open`，旧字段读取时映射）
- 结论态：`OPEN` / `AUTO_RESOLVED`（恢复此名；**删除** `CONFIRMED_RESOLVED`、`PROPOSED_RESOLVED`）
- `needs_review` 从产品流程中移除（历史值读取时映射为 `open` + `notes`）
- `INSUFFICIENT` 的语义是"**无法确认**"，不得在任何界面/报告里被表述为"未回收"

### 4.7 预算与续跑的执行契约（R7）

```
派发前       budget.reserve(candidate_id, est_tokens)   # 累计预留，超上限即停
调用中       client 内部重试与失败计费一并计入该 candidate 的 usage
调用后       budget.settle(candidate_id, actual_usage)   # 幂等；失败也记
任务键       task_key = sha256(entity_key|candidate_id|evidence_fingerprint)[:16]
落盘         recheck_ledger.json（原子写）：{task_key, status, verdict, usage, finished_at}
完成标记     只有 status ∈ {SUCCEEDED, INSUFFICIENT, CONFLICT, INVALID} 才算"已核对"
             未执行态（BUDGET_SKIPPED / FAILED / CANCELLED）不写完成标记
重启恢复     读 recheck_ledger，跳过已核对任务，只跑剩余
失效         正文 hash 变了 ⇒ 该候选旧结论标 STALE，重新核对
```

**验收（R7 要求的三条硬用例）**：

1. `max_calls = 1` 时**不得派发两批**（第二批判 `budget_skipped`）
2. 中途退出 → 重启 → **只跑剩余任务**，已核对的零调用
3. 原文改动 → 旧结论 `STALE`，该候选重新核对

### 4.8 回退与验收（R8）

- 开关 `analysis.foreshadow_recheck_v2` **只切换算法**，不承诺恢复数据状态
- 为避免"切回旧算法后账本已被新逻辑改过"，新实现写**独立账本** `foreshadow_ledger_v2.json`；
  旧账本只读不写。这样 `false` 时产品走旧账本，行为与改前一致
- 验收增加**产品入口的离线集成测试**：不经手工构造，直接调 `FinalSummaryRunner` 的阶段 2
  （mock 掉模型调用），断言产物与覆盖计数
- 人工抽查 20 条保留为**研发评测**，不作为产品运行步骤

---

## 5. 改动清单

### 阶段 0（前置，约 120 行）

| 文件 | 改动 |
| --- | --- |
| `core/prompt_builder.py:57`、`:50` | schema 增 `anchor_quote`/`motif_key`；加"逐字、不改写、找不到留空"约束 |
| `models/analysis_result.py:32`、`from_dict` | `Foreshadowing` 增三字段；`AnalysisResult` 增 `anchor_stats` 字段并**显式解析** |
| `core/analyzer.py` | 构造 `chapter_spans`；按 §4.2 定位 + 归属章节 + 记录 `content_hash`；汇总 `anchor_stats` |
| `utils/text_utils.py:193` | `deduplicate_foreshadows` 重建时保留新字段 |
| `config/settings.py` `AnalysisConfig` | `foreshadow_recheck_v2=false`、`foreshadow_recheck_max_calls=200`、`foreshadow_recheck_max_tokens=400000`、`anchor_min_ratio=0.8` |

### 阶段 2（移植，新文件）

| 新文件 | 内容 |
| --- | --- |
| `backend/shared/citation.py` | 五级匹配 + `locate_quote` + 级别白名单 |
| `backend/shared/verdict.py` | 11 条判据 + 四枚举 + 覆盖计数与守恒式 |
| `backend/shared/identity.py` | `entity_key` 首次登记持久化、匹配或新建 |
| `backend/services/chapter_reader.py` | `ChapterReader` 实现（按章读 blocks/、算哈希、缓存） |
| `backend/services/foreshadow_recheck.py` | 复检主体：候选→批规划→预算→裁决→状态→四栏渲染 |
| `backend/utils/recheck_ledger.py` | `recheck_ledger.json` 原子读写、任务键、状态机、重启恢复 |

### 阶段 3（接线，改 `final_summary.py`）

`:1380` 换主体；删 `:1386`/`:1400` 静默 return；`:1344` 预算化批规划；
`:1304` 消费新裁决；`:1693` 休眠判据；`:1720` 四栏渲染；`:113` 删"禁止同批回收"；
`:1526` 建条目带新字段；`:789` 候选结构升级。

---

## 6. 测试计划（新增约 55 例）

| 文件 | 例数 | 覆盖 |
| --- | --- | --- |
| `test_anchor_quote.py` | 12 | 逐字/改写/空串/跨句拼接/跨章拼接/多处匹配/块内章节归属/`anchor_stats`/旧结果缺字段 |
| `test_result_roundtrip_anchor.py` | 4 | **端到端往返**（§4.1 地基用例） |
| `test_shared_citation.py` | 8 | 五级匹配、拒 SEGMENTED/FUZZY、重叠计数、双侧规范化 |
| `test_shared_verdict.py` | 14 | 11 条判据逐条拒绝、回收章早于埋设、上下文截断、覆盖守恒、空候选 COMPLETED(0) |
| `test_identity.py` | 5 | 首次登记持久、换引文不换实体、同引文不同悬念不合并、空锚点不可派生 |
| `test_recheck_budget.py` | 6 | 预留/结算幂等、上限=1 不派两批、重启只跑剩余、原文变更判 STALE |
| `test_foreshadow_recheck.py` | 6 | 四栏渲染、覆盖计数、单条失败不覆盖其他、摘要当证据必须判不足 |

## 7. 验收（分层，逐条可判）

| 层 | 标准 |
| --- | --- |
| L1 前置 | 两章试跑：`anchor_stats.located/total ≥ 0.8`，且 **0 条越界位置** |
| L2 往返 | `test_result_roundtrip_anchor.py` 通过（字段不丢） |
| L3 单测 | 55 例全绿；覆盖守恒式无差 |
| L4 产品入口集成 | 走 `FinalSummaryRunner` 阶段 2（mock 模型）产出四栏报告 + 覆盖计数 |
| L5 真数据 | 你的书上跑一遍，报告 diff：`open` 从 0 → N；出现"无法确认"类别；抽样 20 条引文逐字命中 |
| L6 回退 | `foreshadow_recheck_v2=false` 时产品仍走旧账本，行为与改前一致 |

## 8. 执行顺序

1. 阶段 0 改动 + 16 例（含往返地基用例）→ 全量回归
2. **两章试跑**（先固定 `max_calls=20 / max_tokens=60000`，端点待你确认）→ 判 L1
3. 你启动整本（`auto_summary=false`）
4. 阶段 2 移植（新文件，不碰运行路径）
5. 阶段 3 接线（你跑完后）
6. 你的书上跑复检 → L5 抽查
7. 文档 + 提交

## 9. 本轮已修（先于计划落地）

- **越界位置漏洞**（R5）：`backend/vnext/resolution.py` 中模型自报位置被直接当作 `located_span`，
  给 `[10000,10020]` 而原文 26 字时输出越界位置且判 SUPPORTED。已修为"模型位置只能挑一处、
  位置一律程序算 + 切片回验"，回归用例断言不变量（要么拒绝，要么给正文范围内的正确切片）。
  提交 `8f9d191`；vnext 552 passed。**该修复需在阶段 2 一并移植进 `shared/citation.py`。**
