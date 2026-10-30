---
doc: S3 执行拆解
plan: docs/PLAN-v2-long-novel-reliability-2026-10-30.md §4 S3
plan_version: 2.2
status: in_progress
baseline_commit: 27d28c5
created: 2026-10-30
---

# S3 执行拆解：安全快照、人物时序与有来源的知识库

对应主计划 §4 S3 的九条。原则同 S1：**先补能红的回归，再改实现**；单元边界按"可以独立验证"划，
不按文件划。

## 单元表

| 单元 | 内容 | 对应条目 | 现行状态 |
| --- | --- | --- | --- |
| U1 | 快照上界：`exclusive_before` 排他位置；来源范围触及上界及之后的记录整条排除；**未知范围一律排除并单列** | S3-1 | ✅ 本轮完成 |
| U2 | 内容与覆盖元数据同源读取：快照为不可变值（`frozen` dataclass + 元组），构造后与外部数据无引用关系 | S3-3 | ✅ 本轮完成 |
| U3 | 人物状态来源顺序：同人取来源位置最新；**迟到低章号不得覆盖**；同位置冲突按"确定优先"，遍历顺序不影响结论 | S3-4 | 部分（覆盖规则已实现，落盘与增量合并待接） |
| U4 | 统一归约规则：增量合并、快照子集重建、最终落盘共用同一套排序与去重 | S3-5 | 待做 |
| U5 | `fact_records`：真实来源 + `verification_state`；引用校验分级（逐字/规范化/重定位/分段/模糊）与 `CITATION_INSTRUCTION` | S3-6 | 部分（校验器与 Prompt 规则已落地；接线到结果写入、`mismatch_reason` 落盘待做） |
| U6 | 图谱边分型：`cooccurrence_edges` 与 `semantic_relationships` 分表，后者只收有互动证据的记录 | S3-7 | 待做 |
| U7 | 存档与 Prompt 窗口分离：去掉"落盘裁剪"，Prompt 限额单独命名；`max_verified_facts` 口径更正 | S3-8 | 待做 |
| U8 | Prompt 限额内部设置由统一构建入口传入（不新增用户按钮） | S3-9 | 待做 |

## U1/U2 已落地的判据

新增 `backend/vnext/knowledge.py`：

- `build_snapshot(exclusive_before=N, facts=…, character_records=…)`
  - **排他**上界：传 5 ⇒ 第 1–4 章可见；写成包含上界会让每块边界差一，调用处极易错
  - 来源范围 `end >= N` 的记录整条排除（不做"部分可见"）
  - 来源缺失 ⇒ 排除，并记进 `dropped_unknown_range`（要能说清"排除了什么"，而不是静默丢弃）
- `KnowledgeSnapshot.dependency_fingerprint()`：只含**实际进入快照**的内容，
  因此"同样可见内容、不同上界"得到同一指纹——块前进了不该让依赖变
- `snapshot_for_block(block_first_ordinal=N)`：逐块入口，语义等价于 `exclusive_before=N`

**验收（`backend/tests/test_vnext_knowledge_snapshot.py`，19 例）**：上界排他、
未知来源不猜、排序确定、指纹对无关变化不敏感、迟到低章号不覆盖、新人物仍可加入、
同位置冲突与遍历顺序无关、快照构造后不受外部列表修改影响。

## 与依赖链的接口

`KnowledgeSnapshot.dependency_fingerprint()` 就是 `results.expected_dependency()`
里"上下文"那一项的输入。二者接起来后：**快照变了 → 依赖它的块自动 `STALE_DEPENDENCY`**，
不需要额外的失效逻辑。衔接点待 U4 落地时在 `result_strategy()` 一侧补上下文版本号来源。

## 未决

1. U3 的"落盘形态"：人物版本是每人一个数组，还是全局事件表按 (chapter, start) 索引？后者便于重放，前者便于查询。
2. ~~U5 的引用校验强度~~ —— **已定（2026-10-30）**：Prompt 里写死引用规则（逐字照抄、不省略、拿不准留空），比对分五级（EXACT / NORMALIZED / RELOCATED / SEGMENTED / FUZZY），只把「是否定位」一位给下游，粒度留在 `CitationCheck` 里供审计与统计。失败不丢数据，只降级为 `UNVERIFIED`。
3. U6 的"明确互动证据"判据：由模型给 `interaction: true`，还是由程序判定（同段共现 + 对话动词）？前者依赖模型自律，后者可测但会漏。
