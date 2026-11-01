# 2.0 管线合并实施计划复审报告 (PLAN-2.0-merge-2026-11-02 Review)

- **复审对象**：`docs/PLAN-2.0-merge-2026-11-02.md`
- **复审基准**：基于仓库实际代码（`backend/`、`docs/`）、测试及基线数据
- **复审原则**：只做评审，不修改代码，不执行计划，不提交 git
- **核心结论**：**需要修改后执行**（计划主干方向正确，但存在 6 项 P1 级结构性漏洞与事实偏差，直接执行会导致历史数据损坏、大面积漏切真实章节或产生返工）

---

## 1. 代码事实核实清单（19 项抽查逐条核验）

抽查覆盖计划涉及的所有核心模块，逐项比对仓库代码与行号：

| # | 检查项与断言 | 结论 | 实际位置与代码事实核对 |
|---|---|---|---|
| 1 | `backend/core/anchor.py:19` 标题正则、`build_chapter_spans`（约 :89） | **属实** | `CHAPTER_HEADING_RE` 确在 [anchor.py:19-22](file:///F:/AI/01_项目/小说分析器/backend/core/anchor.py#L19-L22)；`build_chapter_spans` 确在 [anchor.py:89](file:///F:/AI/01_项目/小说分析器/backend/core/anchor.py#L89)，且确实只按正则跨行切分，不保证单调性。 |
| 2 | `anchor.py` 的 `verify_result_anchors`（约 :152）与 `split_block_into_chapters`（约 :224） | **属实** | `verify_result_anchors` 在 [anchor.py:152](file:///F:/AI/01_项目/小说分析器/backend/core/anchor.py#L152)；`split_block_into_chapters` 在 [anchor.py:224](file:///F:/AI/01_项目/小说分析器/backend/core/anchor.py#L224)，其返回值用 `dict` 存 `{chapter: text}`，章号重复时后者会静默覆盖前者。 |
| 3 | `backend/services/recheck_v2.py` 里"章号只是提示 + 全书回退搜索"的实现 | **属实** | 确在 [recheck_v2.py:222-245](file:///F:/AI/01_项目/小说分析器/backend/services/recheck_v2.py#L222-L245)（注释详述 78% 丢失背景）及 [recheck_v2.py:266-281](file:///F:/AI/01_项目/小说分析器/backend/services/recheck_v2.py#L266-L281)（`locate_quote` 失败后回退 `global_text` 并反查修正章号）。 |
| 4 | `backend/vnext/rolling.py:1-8` 的模块说明与 `build_coverage` | **属实** | 模块 docstring 确在 [rolling.py:1-18](file:///F:/AI/01_项目/小说分析器/backend/vnext/rolling.py#L1-L18) 说明乱序覆盖与缺口洞察；`build_coverage` 确在 [rolling.py:100](file:///F:/AI/01_项目/小说分析器/backend/vnext/rolling.py#L100)。 |
| 5 | `backend/vnext/knowledge.py` 的 `build_snapshot(exclusive_before=...)` | **属实** | 确在 [knowledge.py:146-200](file:///F:/AI/01_项目/小说分析器/backend/vnext/knowledge.py#L146-L200) 实现排他快照过滤，并在 [knowledge.py:341](file:///F:/AI/01_项目/小说分析器/backend/vnext/knowledge.py#L341) `KnowledgeStore.snapshot` 中暴露。 |
| 6 | `backend/vnext/graph.py` 的 `CooccurrenceEdge` / `SemanticRelationship` | **属实** | 确在 [graph.py:58](file:///F:/AI/01_项目/小说分析器/backend/vnext/graph.py#L58) 与 [:101](file:///F:/AI/01_项目/小说分析器/backend/vnext/graph.py#L101) 定义，并在 [:378](file:///F:/AI/01_项目/小说分析器/backend/vnext/graph.py#L378) 显式校验两表永不混用。 |
| 7 | `backend/vnext/foreshadow.py` 的 `cutoff_view` | **属实** | 确在 [foreshadow.py:270](file:///F:/AI/01_项目/小说分析器/backend/vnext/foreshadow.py#L270)，按故事位置截断候选视图，不执行 dormant 判死。 |
| 8 | `backend/vnext/budget.py` 的 `Budget.reserve` / `rebuild_usage` | **属实** | `reserve` 确在 [budget.py:119](file:///F:/AI/01_项目/小说分析器/backend/vnext/budget.py#L119)（调用前预留）；`rebuild_usage` 确在 [:232](file:///F:/AI/01_项目/小说分析器/backend/vnext/budget.py#L232)。 |
| 9 | `backend/vnext/breaker.py` 的 `Breaker` | **属实** | 确在 [breaker.py:22-60](file:///F:/AI/01_项目/小说分析器/backend/vnext/breaker.py#L22-L60)，仅对账户级+永久性失败（置信度 STRUCTURED）跳闸。 |
| 10 | `backend/vnext/consistency.py` 的 `publish_atomically` / `_commit_state` | **属实** | 确在 [consistency.py:57](file:///F:/AI/01_项目/小说分析器/backend/vnext/consistency.py#L57)（`publish_atomically`）与 [:120](file:///F:/AI/01_项目/小说分析器/backend/vnext/consistency.py#L120)（`_commit_state`）。 |
| 11 | `backend/vnext/tasks.py` 的 `TaskLedger` / `TaskRecord` | **属实** | `TaskRecord` 在 [tasks.py:40](file:///F:/AI/01_项目/小说分析器/backend/vnext/tasks.py#L40)；`TaskLedger` 在 [:121](file:///F:/AI/01_项目/小说分析器/backend/vnext/tasks.py#L121)，以 `task_key` 管理幂等记录。 |
| 12 | `backend/vnext/fingerprint.py` 的 `block_signature` / `task_key` | **属实** | `block_signature` 在 [fingerprint.py:97](file:///F:/AI/01_项目/小说分析器/backend/vnext/fingerprint.py#L97)；`task_key` 在 [:124](file:///F:/AI/01_项目/小说分析器/backend/vnext/fingerprint.py#L124)。 |
| 13 | `backend/vnext/prompt_limits.py` 的 `PromptLimits`（9 项上限） | **属实** | 确在 [prompt_limits.py:45-60](file:///F:/AI/01_项目/小说分析器/backend/vnext/prompt_limits.py#L45-L60) 声明 9 项上限的不变 dataclass。 |
| 14 | `backend/core/pipeline.py` 里 `last_updated_chapter` 的写入点、`_fatal_blocks` | **属实** | `last_updated_chapter` 写入点在 [pipeline.py:1069](file:///F:/AI/01_项目/小说分析器/backend/core/pipeline.py#L1069)（读取点在 :448）；`_fatal_blocks` 登记在 [:237, :269, :757](file:///F:/AI/01_项目/小说分析器/backend/core/pipeline.py#L757)。 |
| 15 | `backend/core/memory_state.py` 的共现写关系处、`verified_facts` 入账处、`_trim_kb` | **属实** | `verified_facts` 无溯源入账在 [memory_state.py:372](file:///F:/AI/01_项目/小说分析器/backend/core/memory_state.py#L372)；共现直接写关系在 [:388-394](file:///F:/AI/01_项目/小说分析器/backend/core/memory_state.py#L388-L394)；`_trim_kb` 在 [:409-418](file:///F:/AI/01_项目/小说分析器/backend/core/memory_state.py#L409-L418)，确实只裁剪 7 个 list，两个 dict 无上限。 |
| 16 | `backend/core/prompt_builder.py:103-106` 的 4 项硬编码上限 | **属实** | 确在 [prompt_builder.py:103-106](file:///F:/AI/01_项目/小说分析器/backend/core/prompt_builder.py#L103-L106)；且 [analyzer.py:73-80](file:///F:/AI/01_项目/小说分析器/backend/core/analyzer.py#L73-L80) 确实未透传这 4 项，导致外部无法配置。 |
| 17 | `backend/core/llm_client.py` 里重试判定的四处 | **有出入** | 四个重试循环调用点属实（[llm_client.py:1266, 1391, 1688, 1831](file:///F:/AI/01_项目/小说分析器/backend/core/llm_client.py#L1266)），但**决策树本身已经收敛**在 `_decide_retry`（[:988](file:///F:/AI/01_项目/小说分析器/backend/core/llm_client.py#L988)）。出入在于：计划将其视为完全未收敛，但实际上决策逻辑已单点化，残留的是 4 处循环模板代码与 `error_taxonomy` / `error_types` 双重类型的转换胶水。 |
| 18 | `backend/utils/foreshadow_ledger.py` 的 `SCHEMA_VERSION` 与 `dormant` 判定 | **属实** | `SCHEMA_VERSION = 5` 在 [foreshadow_ledger.py:24](file:///F:/AI/01_项目/小说分析器/backend/utils/foreshadow_ledger.py#L24)；休眠判死在 [:142](file:///F:/AI/01_项目/小说分析器/backend/utils/foreshadow_ledger.py#L142)。 |
| 19 | `backend/services/final_summary.py` 的 checkpoint 覆盖范围、卷摘要截断点、伏笔总表构建 | **属实（有微小行号漂移）** | checkpoint 仅覆盖阶段 1（分卷摘要，[:1133-1240](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L1133)）；卷摘要字符对半截断在 [:1128](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L1128)（`vs[:len(vs)//2]`，计划引用的 :1105 是方法入口及注释）；伏笔总表在 [:805-950](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L805)；全书复检因无 active 伏笔跳过在 [:1560-1563](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L1560)（文档历史行号为 1487-1489）。 |

---

## 2. 深入审查发现：已被实现但被当成"缺失"的能力与隐藏依赖

### 2.1 计划当成"缺失/需新接"但实际已存在的能力（避免做无用功）

1. **`recheck_v2` 在 `final_summary.py` 中已经完全接线完成**：
   - 计划 §6 阶段 3 第 4 项写道："*复检接线：用带证据的 recheck_v2...旧的无阻断路径退役*"。
   - **代码事实**：[final_summary.py:1458](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L1458) 已经完整实现了 `_run_recheck_v2`，并在 [:1896-1899](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L1896-L1899) 接入运行主流程：
     ```python
     if getattr(self.config.analysis, "recheck_v2_enabled", False):
         await self._run_recheck_v2(results)
     else:
         await self._run_global_foreshadow_recheck(volume_summaries, foreshadow_catalog, volume_ranges)
     ```
   - 生产环境中没生效的**唯一原因**是 [settings.py:187](file:///F:/AI/01_项目/小说分析器/backend/config/settings.py#L187) 默认开关为 `recheck_v2_enabled = False`。
   - **计划失误**：把"退役旧分支、让新分支成为默认"的工作，误写成了"总结链路新接线"。

2. **重试决策树在 `llm_client.py` 内部已单点化**：
   - 计划 D-4 写道："*把 1.x llm_client 四处重试判定收敛到单点*"。
   - **代码事实**：[llm_client.py:988-1050](file:///F:/AI/01_项目/小说分析器/backend/core/llm_client.py#L988-L1050) 已经通过 `_decide_retry` 将分类决断、退避等待秒数与失败记录统一。真正未收敛的是 4 个循环结构各自进行 AST 拆包、以及 `ClassVerdict` 与 `ErrorVerdict` 间冗余的转换。计划若按"从零收敛决断"去执行，会与已有 `_decide_retry` 冲突。

### 2.2 计划未提及但开工必碰的隐藏前置依赖

1. **`final_summary.py` 对章节结果文件名的严格硬编码依赖**：
   - [final_summary.py:597-601](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L597-L601) 写明：
     `for f in self.output_dir.glob("chapter_*_result.json"):` 并且执行正则匹配提取章号。
   - 若阶段 1 或阶段 2 引入 vnext 的结果发布规范（如 `vnext/results.py` 的 `{result_id}.json` 或内容定址），而未同步修改 `final_summary.py._load_results`，阶段 3 之前的总结将**加载到 0 个结果文件**。
2. **`vnext/consistency.py:publish_atomically` 强依赖持久化 `Manifest`**：
   - 阶段 1 计划引入 `publish_atomically`，其判定核心是持久化 manifest 校验（[consistency.py:67-93](file:///F:/AI/01_项目/小说分析器/backend/vnext/consistency.py#L67-L93)）。
   - 但 1.x 的 `pipeline.py` 从未维护过 `Manifest` 对象（仅有内存 `pipeline_state` 与直接落盘）。若不先在 1.x 定义项目级 Manifest 契约，`publish_atomically` 根本无处回调 `commit`。
3. **`vnext/artifacts.py:assert_vnext_payload` 门禁与 1.x 聚合产物直接冲突**：
   - 阶段 1 计划将 `artifacts.py` 接到 `aggregate_utils.py`。
   - 但 `assert_vnext_payload`（[artifacts.py:59-72](file:///F:/AI/01_项目/小说分析器/backend/vnext/artifacts.py#L59-L72)）会强校验顶层字典含有 `format_id="vnext"` 与 `schema_version=1`。1.x 的 `character_tracking`、`characters` 等既有聚合产物均无此标记。若直接应用该门禁，所有下游读取方均会抛 `VnextFormatError` 崩溃。

---

## 3. 结构性问题挑毛病

### 3.1 阶段 0 地基分析："章号单调不回退"规则极不可靠

计划在 §3.2 提出：*严格标题解析：标题候选必须满足"章号单调不回退"。一旦解析出的章号 ≤ 已出现的最大章号，就判定为正文内的引用，不再当边界。*

**该规则存在致命的漏切与误杀风险，不能作为生产地基：**

1. **高危失败模式 1：正文引用未来回次，导致后续大批真实章节被彻底吞并（雪崩误杀）**
   - 场景：在第 10 章正文开头有一行引用或说书人套话："*第一百回方知分晓*"。
   - 结果：该行因 `100 > 10` 被误判为新章节，`max_chapter_seen` 瞬间被抬升至 100！
   - 灾难后果：后续从第 11 章到第 100 章的**所有真实章节**，均满足 `parsed <= 100`，**全部被判定为正文引用而不再切分**！整本书 90 个章节被当场吃掉。
2. **高危失败模式 2：小说分卷（Volume）章号重置被全盘误杀**
   - 绝大多数长篇网络小说按卷组织（例如"第一卷 破案篇 第1章..第100章"；"第二卷 官场篇 第1章..第80章"）。
   - 结果：进入第二卷第一章时，解析出章号为 1，由于 `1 <= 100`，第二卷整卷及后续所有小于 100 的章节全被当作正文丢弃，直到编号超过 100 才能重新切出章节。
3. **高危失败模式 3：番外、楔子、序章或倒序叙事**
   - 尾部附带"番外第一章"、"序章"（解析为 1 或 None）、甚至倒序回溯章节，都会被单调性规则全面误判。

> [!CAUTION]
> **稳妥判据建议（替代单一单调性规则）**：
> 不要把单调性当成二元门禁，应采用**复合标题验证器（Multi-factor Heading Validator）**：
> 1. **独立行与形态约束**：行长度限制（`len(line.strip()) <= 40`）；行尾严禁出现中文句末标点（`。`、`！`、`？`、`”`、`……`、`，`、`：`）；
> 2. **步长局部连续性约束（Bounded Step Delta）**：仅允许 `parsed_ch == last_ch + 1`，或微小跳跃 `parsed_ch in [last_ch + 1, last_ch + 2]`（容忍作者跳号）；跳跃超过阈值（如 > 3）的一律要求强前缀（如包含"卷"或空行环绕确认）；
> 3. **分卷感知（Volume Reset Handler）**：遇到"第 X 卷"标识时，重置章节连续性计数器，允许回退到 1；
> 4. **正文引用的排查**：正文引用回次通常紧接谓词（如"第十回中写道"、"见第一章"），标题行通常为纯章名或带副标题（"第十章 绝处逢生"）。

### 3.2 阶段顺序与依赖倒置问题

1. **阶段 1 与阶段 0 / 阶段 2 的概念倒置**：
   - 阶段 1 试图接入 `vnext/tasks.py` 与 `vnext/fingerprint.py`。
   - 但 `fingerprint.block_signature`（[fingerprint.py:97-112](file:///F:/AI/01_项目/小说分析器/backend/vnext/fingerprint.py#L97-L112)）强依赖阶段 0 的 `chapter_hashes: Sequence[tuple[str, str]]`。若阶段 0 的正文身份与切块还没跑通，阶段 1 的任务指纹根本算不出来。
   - 阶段 1 还试图接入 `consistency.publish_atomically`，如前所述，这依赖 Manifest 结构，而 Manifest 是阶段 2 知识库/项目改造的一部分。
2. **阶段 2 与阶段 3 的运行窗口期断层**：
   - 阶段 2 把 `pipeline.py` 的标量 `last_updated_chapter` 换成了 `Coverage` 与 `exclusive_before` 快照。
   - 但阶段 3 才是"总结链路重写"。在阶段 2 完成而阶段 3 未动工的窗口期内，1.x 的旧总结逻辑（`final_summary.py`）仍在尝试读取旧的标量与旧结果，此时执行完整回归必然报字段缺失或不兼容错误。

### 3.3 六个决策点（D-1..D-6）审查与缺失决策

计划现有的 6 个决策点存在盲区，且 D-6 的设计过于简略：

| 决策编号 | 现状与漏洞分析 | 补充建议 |
|---|---|---|
| **D-1** | 章节身份选 `(chapter_no, chapter_content_hash)`。方向正确，但未定章号重置时的命名空间（分卷）。 | 补充：`chapter_key = (volume_idx, chapter_no, content_hash[:12])`。 |
| **D-6** | 灰度开关仅写：*配置项 `pipeline_version: "1.x" \| "2.0"`，默认 1.x*。**完全没有定义**：放在配置哪一层？谁负责路由？产物如何隔离？ | **重大风险**：1.x 输出在 `workspace/{book}/output/`，vnext 在 `workspace_vnext/`。若共用同一目录，切换版本会互相覆盖脏数据。详见下节。 |
| **遗漏 D-7** | **证据 ID（`evidence_id`）两套实现合并**：`core/evidence.py`（[recheck_v2.py:181](file:///F:/AI/01_项目/小说分析器/backend/services/recheck_v2.py#L181) 按 `chapter + text` 哈希） vs `vnext/fingerprint.py:145`（按 `book_id + chapter_id + content_hash + start + end` 坐标哈希）。两套同名，口径完全不同。计划漏拍！ | **必须裁决**：选 `recheck_v2` 的正文内容哈希口径，避免段落起止偏移漂移导致的命中失效。 |
| **遗漏 D-8** | **工作区与产物隔离策略**：2.0 运行时的产物写在 `workspace/{book}` 还是独立子目录？旧分析产物如何升迁？ | **必须裁决**：2.0 的产物写入独立子目录或增加 `schema_version` 隔离层，防止灰度期双版本互踩。 |
| **遗漏 D-9** | **PromptLimits 的配置暴露面**：`PromptLimits` 声明为"内部设置不新增用户可见项"（[prompt_limits.py:11](file:///F:/AI/01_项目/小说分析器/backend/vnext/prompt_limits.py#L11)），但计划声称要解决"4 项不可配置"。两者直接冲突。 | **必须裁决**：在 `config.json` 的 `analysis` 区段正式补齐这 4 项字段并映射进 `PromptLimits`。 |

### 3.4 阶段 3（总结链路新写）拆细建议

阶段 3 标注了"工作量最大"，目前将卷摘要截断、全阶段 checkpoint、伏笔分类、复检接线、四栏报告、聚合原子写 6 件事揉在一起，粒度过粗，极易在中断时无法定位故障源。

建议将其拆解为 3 个可独立编译与回归的子阶段：
- **阶段 3a：复检引擎收敛与四栏结构化数据生成**
  - 正式移除 `final_summary.py` 中的 `_run_global_foreshadow_recheck` 遗留无阻断分支；
  - 固化 `_run_recheck_v2` 为唯一生产复检路径；
  - 输出规范化的 `recheck_verdicts.json`（已具备 4 态，剥离报告渲染，先行断言数据结构正确性）。
- **阶段 3b：卷摘要重构与全局断点续跑（Checkpoint）**
  - 改造 `_compress_volume_summaries`，将 [final_summary.py:1128](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L1128) 的字符对半截断改为自然句边界截断；
  - 将现有的分卷 checkpoint（[:1133-1240](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py#L1133-L1240)）扩展覆盖阶段 2（复检）、阶段 3（总报告）、阶段 4（风格分析）。
- **阶段 3c：最终报告装配与聚合产物原子化**
  - 编写四栏 Markdown 报告生成器（未回收 / 自动已回收 / 证据不足 / 待确认）；
  - 将 `aggregate_utils.py` 的 ~10 处 `open(w)` 替换为 `write_json_atomic`（注意：不加 vnext 的 header 门禁）。

---

## 4. 风险与验收标准严密性审查

### 4.1 验收标准可判定性诊断

| 阶段 | 计划原有标准 | 缺陷诊断 | 改进建议（可判定脚本化） |
|---|---|---|---|
| **阶段 0** | 锚点命中率 ≥ 88.78% | 依赖全书重跑 LLM，受模型随机性影响，一次花费高且无法秒级断言。 | 改为：用已提取的 1669 条带锚点候选做**离线重放断言**，在无全书回退时定位命中率 ≥ 99%。 |
| **阶段 1** | 真实书回归不劣于当前基线；账户级熔断有一条例会 | "不劣于"无量化阈值；"例会"系笔误（应为测试用例 fixture）。 | 明确：`backend/tests/test_breaker.py` 新增注入 402/401 模拟用例，断言后续派发立即被拦截；PromptLimits 单元测试全覆盖。 |
| **阶段 2** | 补跑一个早期块，摘要与人物状态不被后续覆盖 | 未指定具体的测试场景和验证数据集。 | 编写参数化测试：给定 [Block1, Block3] 已提交状态，插入迟到的 Block2，断言 snapshot(exclusive_before=3) 严格不可见 Block3。 |
| **阶段 3** | 真实书跑出四栏报告；中断能续跑 | "跑出"是软标准，没有 schema 与断言规格。 | 增加脚本断言：校验 `recheck_verdicts.json` 覆盖守恒（`SUPPORTED + INSUFFICIENT + DIFFERENT_ANSWER + INVALID == total`），且四栏渲染字段非空。 |
| **阶段 4** | 精确率/召回率达标（阈值写进代码注释） | 阈值未在计划中定义，事后容易妥协。 | 开工前明确锁定指标：在 30 条人工标注定标集上，门禁精确率 ≥ 95%，召回率 ≥ 85%。 |

### 4.2 四大关键失败模式专项评估

1. **历史产物（旧账本 v5、旧锚点）不兼容风险**：
   - 阶段 0 修改章节切分并在账本 v5 增加 `anchor_chapter_hash` 后，用户已有的分析工作区加载旧账本时会因缺少哈希字段导致复检失效。
   - **必补方案**：必须编写向后兼容反序列化器：当 `anchor_chapter_hash` 为空时，回退到按旧章号定位，并输出迁移告警，绝不能硬校验报错。
2. **两条线并存的过渡期窗口破坏**：
   - 阶段 2 把标量 `last_updated_chapter` 改造后，若未提供兼容属性，阶段 3 尚未就绪的 1.x 代码会直接取不到该标量。
   - **必补方案**：在改造后的快照对象上保留只读属性兼容层：`last_updated_chapter = property(lambda self: self.coverage.consumable_upto)`。
3. **单一书目《大奉打更人》回归导致的过拟合风险**：
   - 《大奉打更人》是规整的一文件一章/多章网络小说。以此为唯一基准无法覆盖古典小说（如《三侠五义》回号无空行）、分卷从 1 重置的小说、或单文本大文件。
   - **必补方案**：回归集必须增加合成多卷语料（22 回带分卷重置用例）及《三侠五义》短切片作为门禁守护。
4. **`recheck_v2_enabled` 默认关闭的语境矛盾**：
   - 2.0 合并的核心目标是"只留一条管线，以 vnext 语义为准"。如果 2.0 合并后仍然让 `recheck_v2_enabled` 默认保持 `False`，等于生产环境默认跑的还是 1.x 遗留的旧复检（会因 dormant 误判跳过全书复检）。
   - **必补方案**：在 2.0 模式下强制启用 `recheck_v2`，彻底废弃 `recheck_v2_enabled` 这一临时过渡开关，仅在降级回 1.x 时保留。

---

## 5. 评审问题分级与结论

### 5.1 问题分级清单（P1 / P2 / P3）

| 级别 | 编号 | 问题描述 | 潜在后果 | 修改建议 |
|---|---|---|---|---|
| **P1** | P1-1 | **Phase 0 单调不回退规则会因正文回次引用引发雪崩式吞并真实章节** | 正文出现"见第一百回"导致后续 11~100 章全部被丢弃；分卷重置小说整卷漏切 | 放弃纯单调性判据，改为"行长/无句末标点 + 步长局部连续性（+1/+2） + 分卷感知"复合校验 |
| **P1** | P1-2 | **D-6 灰度开关未定义配置层级与产物目录隔离机制** | 1.x 与 2.0 在同一工作区混写，导致历史数据和新数据相互污染，无法干净回滚 | 明确配置路径；2.0 产物写入专用目录或带 schema 版本隔离，不与 1.x 混盘 |
| **P1** | P1-3 | **遗漏关键决策 D-7：`evidence_id` 两套同名冲突实现未裁决** | 导致阶段 1 和 2 引用混乱，坐标偏移哈希在文本调整后失效 | 明确废弃坐标哈希，统一定案为 `recheck_v2` 的 `chapter + text` 内容哈希 |
| **P1** | P1-4 | **阶段 1 误将 `assert_vnext_payload` 格式门禁套用在 1.x 聚合子产物** | 1.x 聚合结果未带 vnext 元信息头，写入或读取直接抛异常阻断流程 | 在 `aggregate_utils.py` 中仅使用 `write_json_atomic`，严禁直接套用 `assert_vnext_payload` |
| **P1** | P1-5 | **阶段 2 与阶段 3 之间存在数据结构断层，无过渡兼容层** | 阶段 2 改完滚动快照后，尚未重写的 1.x 总结链路全面瘫痪 | 快照结构提供 `last_updated_chapter` 等只读属性兼容垫片，支持平滑过渡 |
| **P1** | P1-6 | **未定义历史产物（旧账本 v5 与无 hash 结果）的兼容/迁移策略** | 用户无法对 1.x 已分析完成的书籍运行 2.0 复检与报告 | 增加向后兼容反序列化，缺 hash 自动降级为章号比对并告警 |
| **P2** | P2-1 | 阶段 3 工作量过大且概念耦合（包含 6 个异构任务） | 交付周期长，中间出错难以定位 | 按 3a（复检收敛）、3b（卷摘要与Checkpoint）、3c（报告原子写）拆成 3 个子阶段 |
| **P2** | P2-2 | 回归语料严重单一，仅依赖《大奉打更人》 | 针对特定书目特征过拟合，对分卷重置或古典章节泛化性极差 | 强制纳入分卷重置合成语料与古典回次样本作为阶段门禁 |
| **P2** | P2-3 | 误把 `final_summary.py` 中已接好的 `recheck_v2` 当成缺失 | 导致重复接线，做无用功 | 计划中明确将"复检接线"修正为"清理旧复检代码分支并移除开关" |
| **P2** | P2-4 | 计划中多处验收标准不可量化、不可脚本自动判定 | 阶段验收主观性强，无法作为 CI/CD 自动化门禁 | 补充确定性的 pytest 单元测试断言与离线指标断言脚本 |
| **P3** | P3-1 | `PromptLimits` 声明为内部设置但计划声称要解决用户配置 | 需求定义不一致，后续配置面混乱 | 明确在 `config.json` 中暴露对应的 4 项参数映射 |
| **P3** | P3-2 | 计划文案存在微小笔误（如"有一条例会"、行号轻微漂移） | 影响阅读体验 | 修正文案与行号标注 |

---

## 6. 开工前必须先定的事（阻塞项决策清单）

在修改实施计划并正式动工前，架构必须先敲定以下 5 项决策：

1. **确定切章判据的具体算法规则**：明确放弃单调二元规则，采用"行长 + 标点过滤 + 步长连续性 + 分卷感知"的具体参数（行长上限取多少、跳步允许范围）。
2. **定案 D-6 灰度配置与产物隔离方案**：敲定 `pipeline_version` 置于 `config.analysis.pipeline_version` 还是全局；确定 2.0 输出是否使用独立子目录或带文件名前缀。
3. **定案 D-7 证据 ID 口径**：正式废止 `backend/vnext/fingerprint.py` 中的坐标式 `evidence_id`，统一定为 `recheck_v2.py` 的内容哈希口径。
4. **定案历史账本与产物向后兼容规范**：明确 2.0 加载历史书籍时的行为准则（是就地读写升级，还是只读回退）。
5. **定案 2.0 语境下 `recheck_v2_enabled` 的处理**：确认在 `pipeline_version == "2.0"` 时无条件执行带证据复检，旧无阻断复检彻底下线。
