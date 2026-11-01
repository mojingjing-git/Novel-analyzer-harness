# PLAN：2.0 管线合并（1.x 作业层 + vnext 数据层）

> **v2（2026-11-02）**：按 AGY 复审（[research/plan-2.0-review-2026-11-02.md](./research/plan-2.0-review-2026-11-02.md)）
> 的 6 条 P1 全部修订：阶段 0 判据换掉、D-6/D-7/D-8/D-9 补齐、阶段 1 修正接线范围、
> 阶段 2 加兼容垫片、阶段 3 拆 3a/3b/3c、历史产物兼容补上、验收全部改成可脚本判定。
> 依据：[data-contract-2.0.md](./data-contract-2.0.md)、[research/vnext-audit-2026-11-02.md](./research/vnext-audit-2026-11-02.md)、
> [baseline-data.md](./baseline-data.md)。本文件是过程稿，执行完成后结论并入 `data-contract-2.0.md`。

---

## 1. 目标与非目标

**目标**：只留一条 2.0 管线；语义取 vnext（有序覆盖、排他快照、来源溯源、证据门禁、预算缓存、熔断、原子写）；
作业层沿用 1.x（分析编排 → 滚动摘要 → 伏笔账本 → 四阶段总结 → 报告 → 前端），但**总结链路要新写**。

**非目标**：不重写前端（只对齐字段与合并视图）；不做安装包与上线；不改 `vnext/resolution.py` 的门禁语义
（它已是两条线唯一交叉点）；不动 1.x 分析提示词的内容（只改输入与限额来源）。

---

## 2. 决策（开工前必须敲定）

| # | 决策 | 定案 | 依据 |
|---|---|---|---|
| **D-1** | 正文身份 | **`anchor_file_index` + `anchor_content_hash`**（文件序号 + 该文件正文哈希）。**章号/回号只用于人读与排序，不作身份** | 采集侧记的 `anchor_chapter` 是"合并块内标题反查出的真实回号"（如 12,13,14,15），复检侧按单文件切分得到的是文件序号（1..922）——两套章号空间根本不对齐（实测按记录章号定位率仅 22.8%）。文件序号唯一、文件哈希内容定址，不受标题解析影响 |
| **D-2** | 改造方向 | **改造 1.x**（保留 `pipeline.py`/`final_summary.py`/前端，只换数据层与接线） | 阶段 3 是新写，反向重建代价更大 |
| **D-3** | 账本 | 合并成一套，以 1.x 的 v5 形状为准，吸收 vnext `tasks.json` 的"跑没跑/结论/花费"语义 | `foreshadow_ledger.py:24` |
| **D-4** | 错误分类 | 取 `core/error_types.py`（`ErrorVerdict`/`ErrorScope` 4 值）。**注意：重试决策早已单点化**（`llm_client.py:988-1050` 的 `_decide_retry`），真正要收敛的是 4 处循环各自 AST 拆包 + `ClassVerdict`/`ErrorVerdict` 冗余转换 | 复审 2.1 |
| **D-5** | 判定层 | `vnext/resolution.py` 不动 | 两条线唯一交叉点，已在生产用 |
| **D-6** | 灰度 | `config.json` 的 `analysis.pipeline_version: "1.x" \| "2.0"`，默认 `1.x`；由 `pipeline.py`/`final_summary.py` 读 | 复审 3.3 |
| **D-7** | 证据 id | **废止** `vnext/fingerprint.py` 的坐标式 `evidence_id`，统一为 `recheck_v2.py:181` 的 `chapter + text` 内容哈希口径 | 复审 P1-3 |
| **D-8** | 产物隔离 | 2.0 产物写 **`output_v2/`** 子目录（`workspace/{book}/output_v2/`），与 1.x 的 `output/` **物理隔离**，不共写、不共读 | 复审 P1-2 |
| **D-9** | 限额配置面 | `config.json` 的 `analysis` 段正式补齐 4 项：`max_relationships`/`max_verified_facts`/`max_themes`/`max_content_chars`（现硬编码在 `prompt_builder.py:103-106`），映射进 `PromptLimits` | 复审 P3-1 |
| **D-10** | 复检开关 | `pipeline_version == "2.0"` 时**无条件**执行带证据复检；`recheck_v2_enabled` 作为过渡开关保留仅供降级回 1.x | 复审 4.2-4 |

---

## 3. 阶段 0：正文身份与切章口径统一（地基）

### 3.1 症结

源文件里**行首出现的"第N回"**（正文引用回次）被标题正则 `anchor.py:19` 当成章节边界，
实测合并块 spans 形如 `[229, 230, 1, 2]`——章号 1、2 在整本书里反复出现。
采集侧记的 `anchor_chapter` 于是指向"另一份同号章"的文本，在那里当然找不到那句引文：
1669 条带锚点候选只进复检 366 条（22%）。今天的修法是"定位失败回退全书搜索"
（`recheck_v2.py`，救回 1303 条、0 条仍丢失）——**那是兜底，不是正解**。

### 3.2 切章判据（**放弃"单调不回退"**）

> 纯单调规则会被两种场景雪崩式误杀：正文出现前瞻回次引用（"第一百回方知分晓"会把
> max 抬到 100，后续 11~100 章全被吞）；分卷小说进入第二卷时章号重置为 1，整卷被吞。

改用**复合标题校验器**，四条同时满足才算章节边界：

1. **独立行 + 形态**：整行 strip 后长度 ≤ 40；行尾不得是 `。，！？；：…、”』」` 之一；
   前面必须是空行或文件首行。
2. **步长局部连续**：解析出的章号 ∈ {last+1, last+2}（容忍作者跳号）。
   跳号 > 2 一律不切。
3. **分卷感知**：遇到 `第X卷` / `第X部` 标识 → 重置 `last` 计数，允许回到 1。
4. **不是引用句**：标题行本身是纯章名或"第N章 + 副标题"；正文引用形如
   "第十回中写道"、"见第一章"——含"中写道/见/如前所述"等谓词的行不切。

**切出的章号允许重复吗**：允许（回号不唯一是事实），但**定位靠 `content_hash`**，不靠章号。

### 3.3 要做的

0. **（新增，先于其它）正文身份落到文件级**：
   - `blocks` 目录下**一个文件 = 一段正文**，文件序号即 `anchor_file_index`（唯一）；
   - 该文件的正文哈希即 `anchor_content_hash`（内容定址，补跑不变）；
   - 文件内的"第N回"标题只作**展示信息**（回号），不参与定位——
     实测：采集侧记的 `anchor_chapter` 是"合并块内标题反查出的真实回号"
     （12,13,14,15），复检侧按单文件切分得到文件序号（1..922），两套章号空间
     不对齐，按记录章号定位率仅 **22.8%**；连复现合并块口径也只有 31% 匹配
     （采集时的块文本与事后重建的不完全一致）。
   - 落盘前把每个伏笔的锚点**重新定位到文件级**：遍历块文件，找到包含该引文的
     文件（跨文件时取首个并告警）→ 记 `anchor_file_index` + `anchor_content_hash`。
1. **切章器**（`backend/core/chapter_split.py`，已落地）：复合标题校验，
   用于把文件内的回号解析出来供展示与取窗参考；**不参与定位**。
2. **锚点落盘**：`anchor_quote` + `anchor_file_index` + `anchor_content_hash`
   + `anchor_chapter`（展示用，可空）。
3. **复检取窗**：按 `anchor_file_index` 找到那段正文（并用 hash 校验）→ 在其内定位引文；
   找不到则全书按引文内容搜索（保留今天这条兜底，但兜底率必须下降）。
4. **输入过滤**：`blocks` 目录只放正文——切分报告（`split_report.txt`）被当成第 922 章
   参与分析与复检（两轮都如此）。入口按"文件头是正文标记或长度下限"过滤并报告被排除的文件。
5. **历史兼容**（P1-6）：账本读到 `anchor_file_index` / `anchor_content_hash` 为空 →
   **降级为按章号定位**并打迁移告警，**绝不硬校验报错**。

### 3.4 验收（全部可脚本判定）

| 项 | 标准 | 判定方式 |
|---|---|---|
| 切分正确性 | 合成语料（22 回带分卷重置）+《三侠五义》短切片上，切出的回号单调不回退、无误切 | 离线断言脚本 |
| 定位率 | 用**已有的 1669 条带锚点候选离线重放**，在**禁用全书回退**时定位命中 ≥ 99% | 离线重放脚本（不调模型、不花钱） |
| 兜底依赖 | 全书回退救回的条数 ≤ 1%（今天 78%） | 同上 |
| 身份唯一性 | 同一文件内的所有伏笔锚点，`anchor_file_index` + `anchor_content_hash` 唯一确定一段正文 | 离线断言脚本 |
| 历史兼容 | 旧账本（无 hash）能加载并复检，产出迁移告警 | pytest |
| 单元测试 | 正文内"第一回"不被当边界；跨块引文被拒；跨文件引文被记为告警 | pytest |

### 3.5 风险与回滚

- 风险：复合判据仍可能误判（尤其古籍无空行、标题带标点的形态）。
  缓解：切分结果写进 manifest 并出报告；人工抽查 5 个文件。
- 回滚：阶段 0 纯新增（切分器新模式 + 账本新字段 + 定位顺序），`pipeline_version` 默认仍 1.x。

---

## 4. 阶段 1：接线（vnext 机制接进 1.x 作业层，不重写）

| 接线项 | 从 | 到 | 边界（复审修正后） |
|---|---|---|---|
| `PromptLimits`（9 项） | `vnext/prompt_limits.py` | `core/analyzer.py:73-81` + `prompt_builder.py:103-106` 的 4 项硬编码 | 4 项要先进 `config.json`（D-9） |
| `breaker`（账户级熔断） | `vnext/breaker.py` | `core/pipeline.py` 派发链 | 1.x 现在只有 `_fatal_blocks` 登记，不熔断 |
| 错误分类收敛 | `core/error_types.py` | `core/llm_client.py` | **只收敛 4 处循环拆包与 `ClassVerdict`/`ErrorVerdict` 冗余转换**；`_decide_retry` 已有，不动（D-4） |
| `tasks`（跑没跑/结论/花费） | `vnext/tasks.py` | 1.x 块级任务记录 | 依赖阶段 0 的正文身份（指纹要 `chapter_hashes`），故排在阶段 0 之后 |
| `fingerprint`（指纹口径） | `vnext/fingerprint.py` | 取代 1.x 块失效判定 | 同上；`evidence_id` 按 D-7 用内容哈希 |
| `artifacts` 的原子写 | `vnext/artifacts.py` 的 `write_json_atomic` | `utils/aggregate_utils.py` 的约 10 处 | **只借原子写，严禁套用 `assert_vnext_payload`**（它强校验 `format_id="vnext"`，会打死 1.x 聚合产物）（P1-4） |
| `consistency`（原子提交） | `vnext/consistency.py` | 结果文件 + manifest 同时成立 | **依赖持久化 Manifest**，1.x 没有 → **推迟到阶段 2**（先在 1.x 定义 Manifest 契约）（P1-5/3.2） |

**验收**：`test_breaker.py` 新增注入 402/401 用例，断言后续派发被立即拦截；
`PromptLimits` 单测全覆盖；全量门禁绿；真实书回归不劣于当前基线（锚点 88.78%、复检覆盖 100%）。

---

## 5. 阶段 2：知识库换语义

1. **排他快照**替代标量上界（`pipeline.py:448,1069` 的 `last_updated_chapter`）：
   只纳入来源范围完全早于当前位的记录 → 解决"越早补跑的块既进不了摘要，又被高章号覆盖"。
2. **有序归约**（`vnext/rolling.py` 的 `build_coverage`）：乱序完成的块整理成可按故事序消费的视图。
3. **来源溯源**：`verified_facts` 带 `SourceRef`（现在原样入账，无法溯源）。
4. **共现 vs 语义关系分表**（`vnext/graph.py`）：`memory_state.py:388-394` 的"同章出现就写成关系"要拆开。
5. **账本合并**（D-3）：一套 v5 形状 + `tasks.json` 语义；`cutoff_view` 替代 dormant 判死。
6. **Manifest 契约**（阶段 1 推迟过来的）：项目级 manifest，支撑 `publish_atomically`。
7. **兼容垫片**（P1-5）：新快照对象保留只读属性
   `last_updated_chapter = property(lambda self: self.coverage.consumable_upto)`，
   让**尚未重写的 1.x 总结链路在阶段 2→3 的窗口期不瘫痪**。

**验收**：参数化测试——给定 Block1/Block3 已提交、插入迟到的 Block2，断言
`snapshot(exclusive_before=3)` 严格不可见 Block3；补跑早期块后摘要与人物状态不被高章号覆盖；
同章共现不进语义关系表；知识库有版本字段。

---

## 6. 阶段 3：总结链路新写（拆三段）

> vnext **没有**总结链路（全库无 `final_report` 生成），这一阶段是**新写**。

### 3a 复检收敛与四栏数据
- `final_summary.py` 里的 `_run_global_foreshadow_recheck`（旧无阻断路径）**退役**；
  `_run_recheck_v2` 固化为唯一生产复检路径（它**早已接线**，缺的只是开关默认，D-10）。
- 产出规范化的 `recheck_verdicts.json`（已具备 4 态）——**先断言数据结构，不做渲染**。

### 3b 卷摘要重构与全阶段 checkpoint
- `_compress_volume_summaries`（`final_summary.py:1128`）的字符对半截断 → 自然句边界截断。
- checkpoint（`:1133-1240`）从只覆盖阶段 1，扩到阶段 2/3/4。

### 3c 报告装配与聚合原子化
- 四栏 Markdown 报告生成器（未回收 / 自动已回收 / 证据不足 / 待确认）。
- `aggregate_utils.py` 的约 10 处 `open(w)` → `write_json_atomic`（**不加 vnext 的 header 门禁**）。

**验收**：脚本断言覆盖守恒（`SUPPORTED + INSUFFICIENT + DIFFERENT_ANSWER + INVALID == total`）
与四栏字段非空；阶段 2/3/4 中断后能续跑。

---

## 7. 阶段 4：判定门禁与定标

1. **人工标注定标集** ≥30 条（含 SUPPORTED/INSUFFICIENT/CONFLICT/过判），进 `backend/tests/fixtures/`。
2. **0 成本门禁**：答案引用证据窗口外章号 → 降级待复核
   （实测命中 11/208 = 5.3%，人工逐条确认 11/11 都是"答非所问"）。
3. **同款模型盲答复核**（可选）：复核方不弱于判定方；弱模型复核实测精确率约 80%（5 抽 4 对）。
4. **扰动稳定性**：控制变量（同序重采样），区分"格式稳定性"与"语义稳定性"。

**验收**：定标集上门禁**精确率 ≥ 95%、召回率 ≥ 85%**（阈值开工前锁定，不许事后妥协）。

---

## 8. 阶段 5：前端收敛

vnext 字段名不一致剩余 2 条（blocks 的 `state`/`status`、queue 的 `total_chapters`）；
合并 `VnextPage`/`VnextDetailPage` 与现有页面为单一 2.0 视图；报告页展示四栏；
顺手修 `summary` 自研 Markdown 渲染器的 `v-html` 不转义（报告来自模型输出）。

---

## 9. 全程规范

- **门禁**：阶段收尾跑 `python scripts/run_tests.py`（当前 1665 例 / ~70s）；阶段内迭代用
  `scripts/pytest_related.py --run`。
- **回归语料**（P2-2，不能只有一本书）：
  - 《大奉打更人》922 文件 / 434.9 万字（真实长篇，网络小说形态）；
  - **合成多卷语料**（分卷章号重置）——`backend/tests/fixtures/vnext_corpus.py` 扩一个多卷用例；
  - **《三侠五义》短切片**（古籍回号、无空行形态）。
- **真书回归**：阶段内用 32 块子集，阶段收口才跑整本（约 70 分钟）。
- **产物**：`docs/data/MEASURE-*.json` + `verdicts-*.json`，配套 `scripts/audit_recheck_verdicts.py`。
- **提交**：一阶段一组提交，message 说清"改了什么 / 为什么 / 验收结果"。
- **灰度**：`analysis.pipeline_version` 控制走 1.x 还是 2.0，默认 1.x 直到阶段 3c 收口；
  2.0 产物写 `output_v2/`，与 1.x 物理隔离（D-8）。

## 10. 风险清单

| 风险 | 缓解 |
|---|---|
| 阶段 0 判据仍会误判（古籍形态） | 复合判据 + 三种语料回归 + manifest 报告 + 人工抽查 |
| 阶段 3 是新写，工作量最大 | 已拆 3a/3b/3c，每段独立编译与回归 |
| 阶段 1↔2 顺序倒置（fingerprint 依赖阶段 0；consistency 依赖阶段 2 的 Manifest） | 已在阶段表里标注边界，consistency 推迟到阶段 2 |
| 阶段 2→3 窗口期 1.x 总结瘫痪 | 兼容垫片 `last_updated_chapter` |
| 灰度期双版本互踩 | 产物目录物理隔离（D-8） |
| 历史书籍在 2.0 下不可用 | 缺 hash 降级读取 + 迁移告警 |
| 回归过拟合单本书 | 三种语料（P2-2） |
| 并发与预算语义合并后走样 | `tasks`/`budget` 接线带断言：预留先于调用、结算只认真结束 |
