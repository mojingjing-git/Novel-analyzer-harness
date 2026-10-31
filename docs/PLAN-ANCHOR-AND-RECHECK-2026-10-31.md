# PLAN：前置加锚点 + 后置复检替换（在 1.x 底座上跑）

- 日期：2026-10-31
- 状态：draft（待你确认后开工）
- 目标：让"每条伏笔"从被识别那一刻起就带原文坐标；用新复检替换旧的总结阶段第 2 步
- 前置决定：老结果撇掉重跑；跑分析期间**不启动最终总结**

## 0. 你的方案我判断是合理的，但有三个前提要先钉住

| 前提 | 为什么 |
| --- | --- |
| 前置改动必须先落地并**小样本验证**，再让你整本跑 | 若模型不照抄（改写、拼接、编造），整本 500 万字跑完才发现锚点是假的，等于白跑 |
| 锚点**落盘前就地校验**，不在复检时才验 | 校验需要本章正文；analyser 手上就有，复检时反而要再读一遍全文 |
| 移植期间**只新增文件，不修改运行路径上的文件** | 你随时可能重启后端；改一半的文件会让正在跑的书出错 |

## 1. 阶段 0：前置改动（约 40 行 + 16 行校验）

### 1.1 提示词

`backend/core/prompt_builder.py:57` 的 `foreshadowing` schema 增加两个字段：

```json
"anchor_quote": "从本章原文逐字照抄的一句（8-40 字，不改标点、不概括、不拼接）",
"motif_key": "2-6 字稳定母题词（如「残破玉印」），用于给这条伏笔一个跨章不变的名字"
```

并在 `:50` 附近加一句硬约束：

> `anchor_quote` 必须逐字取自本章正文。找不到可抄的完整句子就留空字符串，**禁止改写、概括或拼接**；编造引文会被程序判为空。

### 1.2 数据模型

`backend/models/analysis_result.py:32` 的 `Foreshadowing`：

```python
anchor_quote: str = ""   # 本章原文逐字锚点（校验后可能被清空）
motif_key: str = ""      # 稳定母题词
```

序列化无需改（`to_dict` 用 `asdict`，`:114-115`）。

### 1.3 落盘前校验（关键）

`backend/core/analyzer.py` 在拿到 `foreshadowing` 列表后、写盘前逐条校验：

- 用 `citations.verify_citation`（搬自 vnext，五级匹配）在**本章正文**里定位 `anchor_quote`
- 定位成功 → 把 `anchor_quote` 规范化为原文切片（`text[start:end]`），并暂存 `(start, end)`
- 定位失败 → `anchor_quote = ""`，累计 `anchor_missing` 计数，**不抛错**（一条伏笔缺锚点不该让整章失败）
- 结果里同时写 `anchor_stats: {total, located, missing}`，便于事后统计"模型照抄率"

> 这一步的产出是**可用性指标**：如果 located/total < 0.8，说明提示词或模型不合适，要在整本跑之前调整，
> 而不是等复检。

### 1.4 账本侧（只加字段，本阶段不启用新逻辑）

`backend/utils/foreshadow_ledger.py:32` 的 `ForeshadowItem` 增加：

```python
anchor_quote: str = ""
anchor_chapter: int = 0        # 锚点所在章（校验时得出）
motif_key: str = ""
entity_key: str = ""           # sha256(book|anchor_quote_hash|motif_key)[:16]
```

`SCHEMA_VERSION` 3 → 4；`from_dict` 对缺字段给默认值（向后兼容读取，但**本阶段不写新逻辑**）。
`final_summary.py:1526` 建条目时把这些字段一起带上（此时仍是旧复检逻辑在跑，只是字段更全）。

### 1.5 阶段 0 验收

1. 全量 `python -m pytest -o addopts= -q` 通过（当前 1505 例 + 新增约 10 例）
2. `backend/tests/test_anchor_quote.py` 覆盖：逐字命中 / 改写（应判缺）/ 空串 / 跨句拼接（应判缺）/ 出现多次（取首次并记 ambiguity）
3. **小样本真跑**：取 2 章跑一遍分析，人眼看 3 条伏笔的 `anchor_quote` 是否真的逐字出现在正文里，并报告 `anchor_stats`
4. 达标线：`located / total ≥ 0.8`。达不到就回来改提示词，不进阶段 1

## 2. 阶段 1：你整本跑（我们不碰运行路径）

- 配置：`auto_summary = false`（或在总结页不点"开始"），确保分析跑完就停
- 期间我们只做阶段 2 的新增文件，**不修改** `pipeline.py` / `analyzer.py` / `final_summary.py` / `foreshadow_ledger.py`
- 交付物：一批带 `anchor_quote` 的 `chapter_N_result.json` + 一本待复检的书

## 3. 阶段 2：移植复检（新增文件，不动现役代码）

新增（全部新文件，零改动风险）：

| 文件 | 内容 | 来源 |
| --- | --- | --- |
| `backend/shared/citation.py` | 五级引用匹配 + `locate_quote` | 搬 vnext `citations.py`（282 行，零依赖） |
| `backend/shared/verdict.py` | 四枚举裁决 + 证据门禁 + 覆盖计数 | 搬 vnext `resolution.py`（666 行，零依赖） |
| `backend/shared/foreshadow_identity.py` | `entity_key` 派生 | 搬 vnext `foreshadow.py` 的稳定键部分 |
| `backend/services/foreshadow_recheck.py` | 复检主体：候选→批规划→裁决→状态迁移→四栏报告 | 新写，约 400 行 |

`foreshadow_recheck.py` 的接口（供阶段 3 接线）：

```python
async def run_recheck(
    *, book_dir: Path, ledger: ForeshadowLedger, results: list[dict],
    full_text: str, client, budget: dict, verify=None,
) -> RecheckOutcome   # 含 coverage 计数、四栏分类、每条的证据
```

### 阶段 2 验收

- 单测：候选构造 / 批规划预算 / 证据门禁四类拒绝 / 反证可核实 / 覆盖守恒式 / 空候选记 COMPLETED(0)
- **离线真数据演练**：拿阶段 1 跑出来的真实结果 + 真账本，离线跑一遍，输出四栏报告（不接产品路径）
- 人眼抽查 20 条：引文是否真在原文、位置是否对、判定是否合理

## 4. 阶段 3：接线（替换旧实现）

改动集中在 `backend/services/final_summary.py`：

| 位置 | 动作 |
| --- | --- |
| `:1380` `_run_global_foreshadow_recheck` | 改为调用 `foreshadow_recheck.run_recheck` |
| `:1386`、`:1400` 两句 `无活跃伏笔，跳过全书复检` | **删除**；空候选也要产出 `COMPLETED(0)` 记录 |
| `:1344` `_plan_recheck_batches` | 预算化（每次调用的 token 与条数记账） |
| `:1304` `_apply_reconciliation` | 改为消费新裁决结果 |
| `:1693` `reconcile_and_update` | 休眠判据修正（200 章硬阈值 → 与复检结论联动） |
| `:1720` `audit_text_for_report` | 三行统计 → 四栏报告 |
| `:113` prompt | 删掉"禁止同一批次回收" |

**安全阀**：加配置项 `analysis.foreshadow_recheck_v2`（默认 `false`）。为 `false` 时走旧实现，
为 `true` 时走新实现。这样你随时能回退，不用改代码。

## 5. 阶段 4：真数据验收

1. 打开 `foreshadow_recheck_v2`，在阶段 1 那本书上跑总结（只跑到第 2 步即可观察）
2. 报告对照：改前（活跃 0 / 已回收 N / 休眠 M）vs 改后（未回收 / 自动判定已回收 / 证据不足 / 休眠 + 覆盖计数）
3. 人眼抽查 20 条"自动判定已回收"：引文必须在原文里逐字存在，位置对
4. 若"证据不足"占比 > 60%，说明提示词或批大小需要调，不是复检逻辑的问题

## 6. 阶段 5：收尾

- 全量 pytest；`docs/VERIFY-LOG.md` 记 business_test 与真实验收两行
- 文档：`agent.md` 增一节；`docs/architecture.md` 的复检链路图更新
- 提交（前置一个提交、移植一个提交、接线一个提交、文档一个提交）

## 7. 明确不做

- 不动 `pipeline.py`（并发/预热/补跑/停止）
- 不动 `memory_state.py` / `knowledge_base.py` 的合并逻辑（`foreshadowing_network` 只是前情展示）
- 不动结果文件命名与目录结构
- 不动前端（本轮四栏报告先落 `foreshadow_audit.md`，界面后续再说）
- 不碰 vnext 目录（冻结为参考实现）

## 8. 风险与对策

| 风险 | 对策 |
| --- | --- |
| 模型不照抄，`located` 比例低 | 阶段 0 的 4 条达标线卡住，不达标不进阶段 1 |
| 锚点校验拖慢分析 | 校验是纯字符串匹配，单章 < 10ms；且只在有伏笔时跑 |
| 整本跑到一半我们改了代码 | 阶段 2 只新增文件；接线放到阶段 3（你跑完之后） |
| 新复检效果不好 | `foreshadow_recheck_v2=false` 一键回退 |
| 老账本混入 | 阶段 3 首次启用时，把旧账本备份为 `foreshadow_ledger.v3.bak.json`，按新结果重建 |
