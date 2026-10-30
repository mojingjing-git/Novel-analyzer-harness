# 双报告统合审查：外部静态审查 × 内部七维度勘察

- 审查基线：`3a27b6c`（`feat(error): 厂商错误码分类体系 error_taxonomy`），工作区干净
- 报告日期：2026-10-30
- 项目：Novel Analyzer（`F:\AI\01_项目\小说分析器`）
- 合并来源：
  - **外部审查**（8 项，静态源码阅读，未运行测试/未复现）→ 本文以 `E-P1-01` 等原编号引用
  - **内部勘察**（7 个 agent 分维度勘察 + 手工实测）→ 以 `I-*` 引用

---

## 0. 判定方法说明

外部审查明确要求逐项给出 `CONFIRMED / PARTIALLY_CONFIRMED / NOT_REPRODUCED / FALSE_POSITIVE`。
本文对这一要求做了**同等标准的回打**：不仅复核外部的 8 项，也复核内部勘察自己的结论。

复现手段（全部只读，不修改仓库）：

| 脚本 | 覆盖 | 位置 |
|---|---|---|
| `verify_review_8.py` | E-P1-01 / 02 / 03 / 07 / 08，用 `MemoryState` 真实 API 跑通 | `%TEMP%\verify_review_8.py` |
| `verify_prompt_budget.py` | E-P2-05 的字符量级量化 | `%TEMP%\verify_prompt_budget.py` |
| `verify_my_findings.py` | I-* 结论复核（分类器误报、真实账本、原子写、信号量、desktop） | `%TEMP%\verify_my_findings.py` |

运行方式（本机必须，见附录 A）：

```powershell
python -X utf8 -c "
import os
for k in list(os.environ):
    if 'roxy' in k.lower(): del os.environ[k]
import runpy
runpy.run_path(r'C:\Users\wrz20\AppData\Local\Temp\verify_review_8.py', run_name='__main__')
"
```

---

## 1. 结论摘要

**外部审查 8 项全部真实存在，无 FALSE_POSITIVE**；但 3 项的**触发条件或影响面被高估**：

| 编号 | 判定 | 修正要点 |
|---|---|---|
| E-P1-01 | CONFIRMED | 性质不是"缺少校验"，而是**命名误导 + 无溯源**（代码里根本没有"校验"这个概念） |
| E-P1-02 | CONFIRMED | 复现结果比描述更糟：完全无互动的两人也生成"关系" |
| E-P1-03 | CONFIRMED（机制） | **正常单趟流程不可达**，仅续跑/补跑路径可达 |
| E-P1-04 | CONFIRMED | 已有 `needs_review` + 审计报告**标记**，缺的是**阻断** |
| E-P2-05 | PARTIALLY | 结构成立，**量级高估约 10 倍**（8 路上下文合计仅占 8%） |
| E-P2-06 | CONFIRMED | 描述准确，但触发阈值 80,000 字符，概率不高 |
| E-P1-07 | CONFIRMED | **落盘会自愈**（排序重建），受损仅限运行期 prompt |
| E-P1-08 | CONFIRMED | 与 E-P1-03 **同根因**，必须合并修 |

**内部勘察 10 项复核后：8 项站得住，1 项降级，1 项属设计意图**。
其中一项（分类器误报）经实测**严重度高于最初判断**，且位于刚提交的代码中，列为最高优先级。

**两边重叠度仅 5/13**——互补性强于一致性：

- 完全重叠：E-P1-01、E-P1-02、E-P1-03、E-P1-04、E-P2-06
- 仅外部：E-P1-07、E-P1-08、E-P2-05
- 仅内部：分类器误报、伏笔 active 归零、并发轮无熔断、聚合层非原子写、破坏性操作零测试等

---

## 2. 统合问题清单（C 编号）

评级说明：
- **P0** 已在生产运行、会把正常任务判死 → 立即修
- **P1** 确定性一致性缺陷、零 LLM 成本、可用不变量测试锁死 → 本迭代
- **P2** 计费浪费 / 可靠性 → 本迭代后半
- **P3** 语义可信度，涉及 schema 与迁移设计 → 需单独方案
- **P4** 工程基建 → 持续

---

### P0

#### C-01 · 错误分类器对纯文本做数字码扫描，误判为不可重试 → 整块永久失败

- **来源**：I-classify-message-conflict（内部勘察唯一 P0）
- **判定**：**CONFIRMED（实测）**，且严重度超出最初判断
- **位置**：
  - `backend/core/error_taxonomy.py:216` —— 数字码扫描使用 `\b{code}\b`
  - `backend/core/llm_client.py:99` —— `_error_action()` 把 `structured` 置信当作"首次即停"的强判据
  - `backend/core/pipeline.py:708-714` —— 据此登记 `_fatal_blocks`
- **实测**（调用真实 `classify_message`）：

| 输入文本 | 判定 | 置信 | 策略 |
|---|---|---|---|
| `第 401 章 我叫王二` | `auth` | structured | **stop** |
| `他说：这单生意见鬼，赚了 1008 块` | `billing` | structured | **stop** |
| `总字数 1026 万，稿费 2013 元` | `moderation_input` | structured | **stop** |
| `本章共 402 字……402 分` | `unknown` | text | backoff |
| `HTTP 500 是服务器错误` | `server` | text | backoff |
| `余额不足` | `billing` | text | 正确 |

- **后果链条**：
  ```
  错误文本含 1008 / 1026 / 401 等数字
    → 判为 billing / moderation，置信 structured
    → _error_action 返回 stop，重试链第一次即断
    → pipeline 登记 _fatal_blocks，补跑轮剔除
    → 该块永久失败；若判成 moderation 还会被 skip_moderation_blocked 静默跳过
  ```
- **可达性**：厂商把请求内容回显进错误消息时（审核类 400/422 常见）即可触发。
- **根因**：给纯文本通道 `classify_message` 赋予了与结构化通道 `classify_exception` 同等的置信权限。作者已在 `error_taxonomy.py:22-25` 记录该残余风险，但当前实现把风险集中在"不可重试"这个最贵的判断上。
- **最小修复**（零 LLM 成本）：
  1. 数字码只在**带码字段形态**触发：`"code": 1008` / `code=1008` / `error_code(1008)`；取消裸 `\b1008\b`
  2. `_error_action` 的 `stop` 判据收窄为"来自 exception 通道的 structured"；text 置信一律 `backoff`
  3. 把上表前 3 条作为**反向 fixture** 加入 `backend/tests/test_error_taxonomy.py`
- **说明**：现有 37 个 gold fixture 全来自真实错误串，因此测试全绿——**这类误报天然逃过 fixture 覆盖**，必须显式补负样本。

---

### P1 · 确定性一致性（零 LLM 成本，不变量可测）

#### C-02 · 滚动摘要向早期章节泄漏未来信息

- **来源**：E-P1-03
- **判定**：CONFIRMED（机制）；触发条件需修正
- **位置**：`backend/core/memory_state.py:150-152`（`_apply_rolling_to_snapshot` 只判 `isinstance(rs, dict)`，不看 `last_updated_chapter`）；快照取用于 `backend/core/pipeline.py:691`（`chapter_limit=block_id`）
- **复现**：置 `rolling={"rolling_structured":{第120章信息},"last_updated_chapter":120}` 后，`get_kb_snapshot(chapter_limit=5)` 拿到该 rolling，泄漏文本原样出现
- **重要修正**：外部审查假设的"分析第 100 章时 rolling 已吸收第 120 章"在正常单趟流程**不可能发生**，三层原因：
  1. 快照 `chapter_limit=block_id`；
  2. 块按序创建，`asyncio.Semaphore` waiter 为 FIFO，块 N 必须在块 N−concurrency 完成后启动；
  3. rolling **滞后**：`pipeline.py:445` 每 `concurrency` 块才触发，`_update_rolling_summary` 只处理 `bid > last_chapter`
- **真实可达路径**：仅 **续跑**（上次 block 5 失败、其余推进到 300 章，重启后用 `chapter_limit=17` 的快照吃进 283 章未来）与 **补跑轮**
- **既有测试反向锁定**：`backend/tests/test_memory_state_rolling.py:43-54` 用 `chapter_limit=1` 断言 `== STRUCTURED`，修复必须同步改
- **最小修复**（约 5 行）：
  ```python
  lu = rolling.get("last_updated_chapter")
  if isinstance(lu, int) and lu > chapter_limit:
      return   # 不注入含超出本章范围信息的滚动摘要
  ```
  取舍：续跑低章号块失去 rolling 上下文（仅剩 `compressed_arcs` + `recent_summaries`），符合项目既有 fail-safe 取向
- **测试**：新增"未来信息隔离"不变量——`chapter_limit=5` 的快照不得含 `last_updated_chapter=120` 的内容

#### C-03 · 补跑的早期章节进不了滚动摘要

- **来源**：E-P1-08
- **判定**：CONFIRMED（实测）
- **位置**：`backend/core/pipeline.py:588`
  ```python
  if bid in valid_block_ids and bid > _rolling_state["last_chapter"] and bid in state.results
  ```
- **复现**：`last_chapter=120`、候选块 `[100, 124]` → 仅 124 进入 rolling，第 100 章被丢弃
- **与 C-02 同根因**：均以单一 `last_chapter` 标量代表"已覆盖范围"。它同时造成——低章号块**读进**不该看的未来（C-02）、低章号块**写不进**该进的摘要（C-03）
- **修复建议**：`rolling_data` 增加附加字段 `covered_block_ids: [...]`（缺省空，旧 JSON 向后兼容），过滤改为 `bid not in covered`；**必须**处理"字段缺失时退化为旧 `> last_chapter` 行为"，否则首次运行会把所有块重进 rolling，等于重跑一遍摘要
- **成本**：不增加 LLM 调用（每块仍恰好进一次）
- **测试**：补跑块必进覆盖集

#### C-04 · 乱序完成的低章号结果覆盖较新的人物状态

- **来源**：E-P1-07
- **判定**：CONFIRMED（实测）；影响面需修正
- **位置**：`backend/core/memory_state.py:384-386` —— `kb.character_states[arc.name] = arc.surface_action` 不受 `update_timeline=in_order` 保护；`in_order` 判定在 `:328`/`:333`，该开关只护 `timeline`/`story_timeline`/`recent_summaries`（`:358`/`:362`/`:406`）
- **复现**：先 `add_result(第12章 方醒="已登基为帝")` → 再 `add_result(第4章 方醒="刚穿越，饥寒交迫")` → 最终 `character_states["方醒"]` = **第4章值**，`regressed=True`，而 `_kb_last_chapter` 仍为 12
- **既有保护（外部审查未发现）**：
  - `backend/core/pipeline.py:615-616` 收尾执行 `merge_results` → `save`
  - `backend/core/knowledge_base.py:319` 首行 `results.sort(key=lambda r: r.chapter_number)`
  - **因此最终 `knowledge.json` 及所有基于它的产物（报告/伏笔/图谱）顺序正确**；受损仅限**运行期实时 KB**（后续块 prompt 可能读到倒退状态），且断点续跑后会被重建修复
- **最小修复**（约 4 行，零 schema 变更）：把 `character_states` / `character_relationships` 的写入移入 `if in_order:` 门禁。因为落盘有排序重建兜底，**乱序时跳过写入完全安全**
- **不建议采纳外部方案**："为每个角色维护最新有效章号"会引入新字段与迁移成本，在已有 rebuild 兜底前提下属过度设计

#### C-05 · 伏笔休眠判定把 78% 条目判死，`active` 归零 → 阶段 2 复检永久跳过

- **来源**：I-foreshadow-dormant（内部勘察；外部审查完全未覆盖）
- **判定**：CONFIRMED（真实数据实测）
- **真实账本**（`workspace/《带着仓库到大明》/output/foreshadow_ledger.json`，`schema_version=3`）：

  | 指标 | 值 |
  |---|---|
  | 总条目 | 700 |
  | `resolved` | 154 |
  | `dormant` | **546** |
  | `active` | **0** |
  | `needs_review` | 37 |
  | dormant 占比 | **0.78** |

- **位置与根因**：
  - `backend/utils/foreshadow_ledger.py:105-133` `reconcile_and_update`，判据 `(current_chapter - item.last_seen_chapter) >= DORMANT_CHAPTER_THRESHOLD`，阈值 `200` 为硬编码常量
  - `backend/services/final_summary.py:1531` 建条目时 `last_seen` 直接取 `cat["last_seen"]`，即"该 clue 最后一次被 LLM 写出的章"，**不是"伏笔在剧情中最后被呼应的章"**
  - 于是只在第 5 章被检出一次的伏笔，到第 205 章必然 dormant，与剧情是否仍在呼应无关
  - 阈值 `200` 对短篇永不触发、对 2600 章长篇过度触发
- **连带后果（比 E-P1-04 更致命）**：`backend/services/final_summary.py:1384` —— `active_ids` 为空即 `return`，**全书伏笔复检从未真正执行过**；`foreshadow_audit.md` 的"活跃"段恒为空
- **最小修复**（零 LLM 成本）：
  1. 区分"检测章"与"剧情呼应章"，`last_seen` 语义改为后者（可由 reconciliation 的证据章回填）
  2. 休眠阈值按书长自适应（如 `min(200, max(20, 总章数 // 10))`）
  3. 增加"至少保留 N 条 active"的兜底，或改为"未回收即 active，仅超长静默才降级"
- **注意**：C-05 与 C-10（E-P1-04）是同一账本两个方向的失真，**必须一起修**——只修回收侧，活跃侧仍为空

#### C-06 · 同章共现被当作人物关系

- **来源**：E-P1-02
- **判定**：CONFIRMED（实测）
- **位置**：`backend/core/memory_state.py:388-394`、`backend/core/knowledge_base.py:370-378`（两处同构）
- **复现**：两个"甲在城东 / 乙在城西"、**无任何互动**的弧光 → `{"乙-甲": "乙心情变好；甲心情变差"}`
- **规模**：真实 `knowledge.json` 中 `character_relationships` 已有 **5285 个 key**，绝大多数为此类产物
- **最小修复**（零 LLM 成本）：`desc` 改为 `f"第{N}章同章出现"`，保留配对供关系图做共现边，但不再伪装成关系语义；彻底分离共现/关系需新字段，归入 P3
- **兼容性**：仅改 `desc` 文案，影响 `GraphPage` 关系边 tooltip，不改边结构、不改 API 契约

---

### P2 · 计费与可靠性

#### C-07 · 并发轮缺全局熔断，欠费场景仍整轮烧

- **来源**：I-no-global-breaker
- **判定**：CONFIRMED
- **位置**：`backend/core/pipeline.py:560-582` —— `_fatal_blocks` 过滤**只在补跑轮**生效；并发轮内每块仍完整走一遍重试链
- **依据**：`docs/PLAN-provider-error-taxonomy-2026-09-30.md:316` 原文「Phase 4 不应是可选的」，但当前标为可选未做
- **修复**：并发轮内检测"连续 ≥3 个不同块登记 fatal"即 `request_stop()`（PLAN 原方案）
- **成本**：零额外调用，纯削减

#### C-08 · 重试决策逻辑四份拷贝

- **来源**：I-retry-duplication
- **判定**：CONFIRMED
- **位置**：`backend/core/llm_client.py:838` / `:986` / `:1238` / `:1387`（流式/非流式 × 温度退火/指数退避 四份同构分支）
- **风险**：本次 C-01 的修复点需要同时改 4 处；漏改即行为不一致
- **建议**：先修 C-01（4 处机械替换），再抽 `_decide(err, moderation_hits) -> Action` 单点

#### C-09 · 聚合层 11 个产物中 10 个非原子写

- **来源**：I-aggregate-nonatomic
- **判定**：CONFIRMED（实测计数）
- **位置**：`backend/utils/aggregate_utils.py` —— `safe_save_json` 仅 1 处调用（`:178`），`open(..., 'w')` 11 处、`json.dump(` 10 处
- **后果**：前端在聚合进行中拉 `aggregated/*.json` 会读到半个文件；`backend/utils/excel_export.py:67-72` 用裸 `json.load`，`JSONDecodeError` 冒泡成 500
- **修复**：10 处统一改走已有的 `safe_save_json`（`backend/utils/json_utils.py:318-361`，含 Windows `PermissionError` 8 次退避）

#### C-10 · 伏笔回收无阻断（`needs_review` 只是标记）

- **来源**：E-P1-04
- **判定**：CONFIRMED（读码）
- **位置**：`backend/services/final_summary.py:1322-1336` —— `is_resolved` 为真即 `status='resolved'` 并写 `resolution`/`resolved_chapter`/`source_type='llm_judged'`；`confidence != '高'` 或缺 `resolution` **只**做 `needs_review=True` + notes 追加
- **阶段 2 同构**：`:1380-1439` 全书复检同样以 LLM 判定为准
- **既有保护**：`backend/utils/foreshadow_ledger.py:135-175` 的 `audit_text_for_report` 已把低置信标注写进 `foreshadow_audit.md`——问题不是"静默通过"，而是"标记了但没人看"
- **最小修复**（零 LLM 成本）：判定倒置——不满足条件时**不提交** `resolved`，退回 `active` + `needs_review=True`。不需要新表、不需要复核调用
- **与 C-05 合并执行**

#### C-11 · `final_summary.run()` 单方法 253 行，阶段 2/3/4 无 checkpoint

- **来源**：I-final-summary-monolith
- **判定**：CONFIRMED
- **位置**：`backend/services/final_summary.py:1498`（`run()` 253 行，类约 1250 行）；checkpoint 仅覆盖阶段 1（`:1097-1221`，`manifest` + `results_fingerprint = sha256(name:mtime_ns:size)` 前 16 位，`:1108`）
- **后果**：阶段 2/3/4 中途停止 = 重付全部 token
- **建议**：按阶段拆 Runner + 统一 checkpoint 接口；优先级低于 C-09（改动面大、收益是容错而非省钱）

---

### P3 · 语义可信度（需 schema 设计与迁移）

#### C-12 · LLM 提取结果原样进入 `verified_facts`，无溯源

- **来源**：E-P1-01
- **判定**：CONFIRMED（实测）
- **位置**：`backend/core/memory_state.py:369-372`（增量）、`backend/core/knowledge_base.py:340-346`（全量重建）
- **复现**：`CoreEvent(event="角色甲生死不明，下落未交代")` → `verified_facts == ["角色甲生死不明，下落未交代"]`
- **性质修正**：不是"缺少校验"，而是**命名误导 + 无溯源**——代码里没有"校验/证据"概念，`verified_facts` 实际是"LLM 概括文本的去重列表"，却以 `[已验证事实]` 标签进入 prompt
- **零成本先行**：prompt 段标题改 `[模型提取事实（未逐条校验）]`，入账追加 `（第N章）` 溯源后缀；字段名保持不变以兼容旧 JSON 与前端
- **完整方案**（第二阶段）：`LLM_EXTRACTED` / `SOURCE_VERIFIED` 双轨 + 证据章引用 + 局部语义复核——**会新增 LLM 调用，需 A/B 论证**

#### C-13 · `knowledge.json` 无 schema_version，两个 dict 无界增长

- **来源**：I-knowledge-unversioned
- **判定**：CONFIRMED
- **位置**：`backend/models/knowledge.py:14-31`（无版本字段）、`backend/core/knowledge_base.py:306-312`（`_trim_kb` 只裁 5 个 list）
- **真实数据**：文件 1022 KB；`character_states` **704** key、`character_relationships` **5285** key，两者**永不裁剪**；`character_relationships` 每章做 O(n²) 同章配对
- **对比**：`backend/utils/foreshadow_ledger.py:20` 的 `SCHEMA_VERSION = 3` 是项目唯一有版本号的数据文件
- **风险**：下一次字段语义变更只能靠 `from_dict` 逐字段 try/降级硬扛（`_load_rolling_structured` 已需兼容三种历史格式）
- **建议**：加 `schema_version` + 对两个 dict 做 LRU/章号窗口裁剪

#### C-14 · 单次章节分析背 9 类输出 + 8 段上下文（量级已下调）

- **来源**：E-P2-05
- **判定**：**PARTIALLY_CONFIRMED**
- **结构成立**：`backend/core/prompt_builder.py:11-79` 的 Output Schema 恰为 9 个顶层键（core_events / character_arcs / foreshadowing / plot_holes / locations / spatial_relationships / cross_block / updated_knowledge / long_context_insights）；user prompt `:226-250` 恰为 8 段 + 正文
- **量级实测**（`verify_prompt_budget.py`，所有上限填满）：

  | 区段 | 字符 | 占 user |
  |---|---|---|
  | 正文 | 30,019 | 72.9% |
  | `[完整故事历史]` | 7,323 | 17.8% |
  | `[前情摘要]` | 1,233 | 3.0% |
  | `[角色当前状态]` | 1,150 | 2.8% |
  | `[已验证事实]` | 609 | 1.5% |
  | `[角色关系]` | 578 | 1.4% |
  | `[伏笔网络]` | 243 | 0.6% |
  | `[当前时间线]` | 15 | 0.0% |
  | system | 4,857 | — |
  | **总计** | **46,057** | — |

  **8 路上下文合计仅 3,828 字符（8%）**。上限本身很小（`backend/core/prompt_builder.py:91-118`：max_arcs=30、max_summaries=5、max_character_states=20、max_world_items=20、max_foreshadow_entries=5、max_relationships=15、max_verified_facts=15、max_themes=8、max_content_chars=30000）
- **结论**：真正的重复只有 `[完整故事历史]`（rolling 主线 + `compressed_arcs`）与 `[前情摘要]`（`cross_block.summary`）之间，语义是"时间线 vs 摘要"，合并收益有限。**"注意力竞争"在当前量级不是主要矛盾**，建议降为最后处理
- **附带真 bug（见 C-21）**：4 个上下文上限配置项**从未传入**

#### C-15 · 最终总结按字符位置截断中间卷摘要

- **来源**：E-P2-06
- **判定**：CONFIRMED
- **位置**：`backend/services/final_summary.py:1089-1094`
  ```python
  trimmed = [vs[:len(vs) // 2] + "\n（此处已压缩，省略后半）"]
  ```
  首尾组保全文，中间组砍后半
- **触发阈值**：`analysis.volume_compress_threshold` 默认 80,000 字符，长篇通常只有 1–3 个中间组受影响
- **建议**：按关键事件/因果链/证据索引选择信息，属长线优化，排最后

---

### P4 · 工程基建

| 编号 | 问题 | 判定 | 位置 | 备注 |
|---|---|---|---|---|
| C-16 | 破坏性操作无回归网 | CONFIRMED | `backend/api/routes_workspace.py`、`backend/services/workspace_service.py`（13.9 KB，最大未测服务）；`backend/api/` 13 个路由中 9 个零直接 import | 唯一可能造成**不可逆用户数据损失**的路径；优先补删除/归档 |
| C-17 | 零 CI + `pytest-timeout` 未装 | CONFIRMED | 全仓无 `.github/`、无 `pyproject.toml`/`tox.ini`/`setup.cfg`、无任何 `.yml`；`agent.md:1164` 自记此债挂了两个月 | 建 CI 时须显式写**不带方括号**的 `NO_PROXY`（见附录 A） |
| C-18 | `v-html` 依赖自研 markdown 渲染器，仅 15 用例 | CONFIRMED | `frontend/src/pages/SummaryPage.vue:391`、`frontend/src/utils/markdown.ts`（链接白名单 `:33`、占位符还原顺序 `:52-55`） | 输入来自 LLM（非可信），漏转义即存储型 XSS |
| C-19 | 配置写回全量快照竞态 | CONFIRMED | `frontend/src/pages/SummaryPage.vue:101-116` 与 `SettingsPage.vue:174-199` 均整份 PUT；后端全量替换 `backend/api/routes_settings.py:98-113` | 两页同开互相覆盖；仅 SummaryPage 侧提示 409 |
| C-20 | 全站轮询叠无超时请求 | CONFIRMED | `QueuePage.vue:303-309`(5s)、`SummaryPage.vue:266`(2s)、`StylePage.vue:56`(2s)、`MapPage.vue:60`(2s)、`StatsPage.vue:23`(5s)；`frontend/src/api/client.ts:270` 无 `AbortSignal`/无重试 | 已有逐点 seq 防串号补丁，缺统一治理 |
| C-21 | 4 个上下文上限未接到配置（**1 项未接线** + **3 项无配置字段**） | CONFIRMED（实测） | `backend/core/analyzer.py:61-69` 只传 7 个参数给 `PromptBuilder`；**未接线（1 项）**：`max_verified_facts` 配置字段**确实存在**（`backend/config/settings.py:188`）但未被传入，只能走 `prompt_builder.py:100-103` 的默认值；**无配置字段（3 项）**：`max_relationships` / `max_themes` / `max_content_chars` 在 `AnalysisConfig`（`settings.py:143-152`）中根本不存在，用户**无法调**，只能走默认值；**裁剪路径不一致**：`backend/core/memory_state.py:414` 读配置，`backend/core/knowledge_base.py:309` 与 `:476` 硬编码 `MAX_VERIFIED_FACTS`——同一语义三处不同来源，改配置只影响其中一条 | 改配置无感知（`max_verified_facts` 改的是 KB 裁剪口径，与 Prompt 注入上限互不影响；另 3 项本就无配置字段可改） |
| C-22 | onefile 下 Mica 深色探测读不到配置 | CONFIRMED | `desktop.py:676` 用 `Path(__file__).resolve().parent / "config.json"`（指向 `_MEIPASS`），而 `:60` 用的是正确写法 `PROJECT_ROOT = Path(os.environ.get("NOVEL_ROOT") or str(EXE_DIR))` | 深色模式在 exe 中永久静默失效 |
| C-23 | 打包配置自相矛盾且跑不通 | CONFIRMED | `desktop.spec:60` hiddenimports 含 `pytest_asyncio` 而 `:78` excludes 也含它；`build_exe.bat` 未走本地磁盘构建（会踩 esbuild 网络盘坑）；实测 `.venv` 未装 PyInstaller | `desktop.spec:8` 头部注释 65–80 MB 已过时（实测 26,391,437 B） |
| C-24 | `.gitignore` 宽泛规则 | CONFIRMED | `.gitignore:19` 的 `1/`、`:47` 的裸 `v`；`git check-ignore -v` 实测 `backend/v` 命中 `:47:v` | 应改 `/v`、`/1/` |
| C-25 | 文档状态漂移 | CONFIRMED | `agent.md` 1308 行同时承担架构手册/Agent 规则/微型 CHANGELOG，§10.20–10.34 被追加到 §12 之后；`PLAN-docx-input-2026-09-28.md`、`PLAN-desktop-silent-launch-2026-09-28.md` 已实现但仍标 `draft` | 建议建 `docs/archive/`、拆历史附录 |
| C-26 | 两个独立并发上限（已降级） | PARTIALLY | `backend/core/pipeline.py` 的 `sem = asyncio.Semaphore(concurrency)` 与 `backend/services/final_summary.py` 的 `self._llm_sem = asyncio.Semaphore(self.concurrency)` 确为独立实例 | 自动总结路径在 pipeline 结束后运行、**不重叠**；真实重叠仅经 style/location 独立服务。架构上无全局限流器，但可达性低 |
| — | `MAX_RETRIES=5` 未接入重试链 | **非缺陷** | `backend/config/constants.py:51` 注释明写「预留…仅用于失败日志标注」，实际只作为 `record_failure(max_retries=...)` 字段 | 建议改名 `MAX_RETRIES_LABEL` 消除歧义 |

---

## 3. 既有保护机制清单

外部审查明确提问"是否存在我没有注意到的既有保护机制"。经核对，有 8 处值得记录——它们直接改变了多项问题的严重度：

| # | 机制 | 位置 | 对哪项问题起作用 |
|---|---|---|---|
| 1 | 收尾**排序重建**：`merge_results` 首行 `results.sort(key=lambda r: r.chapter_number)` | `backend/core/pipeline.py:615-616` → `backend/core/knowledge_base.py:319` | **C-04** 落盘自愈，仅运行期受损 |
| 2 | `in_order` 乱序骨架（已护 timeline/story_timeline/recent_summaries） | `backend/core/memory_state.py:328`/`:333`/`:358`/`:362`/`:406` | **C-04** 只需"补挂两个字段"，非从零建设 |
| 3 | `needs_review` + `audit_text_for_report` 三段式审计 | `backend/utils/foreshadow_ledger.py:135-175` | **C-10** 缺的是"阻断"而非"发现" |
| 4 | 快照缓存按 `chapter_limit` 分档（更小则重建、更大则增量） | `backend/core/memory_state.py:117-129`、`_snapshot_lock:104` | **C-02** 修复不需重构缓存 |
| 5 | 伏笔两阶段去重（关键词倒排 + SequenceMatcher≥0.6） | `backend/utils/text_utils.py:193-336` | C-05 修复不会因去重引入重复条目 |
| 6 | KB 备份回退（主文件损坏自动回落最新备份，保留 10 份） | `backend/core/knowledge_base.py:57-78`/`:80-95` | C-13 迁移有回滚兜底 |
| 7 | 原子写能力**已存在**（`safe_save_json`，含 Windows `PermissionError` 8 次退避） | `backend/utils/json_utils.py:318-361` | **C-09** 是"没铺开用"，不是"没有能力" |
| 8 | WS 三层自愈（节流 → 队满驱逐保关键事件 → `inflight_blocks` 权威对账） | `backend/progress_hub.py:30`/`:91`、`backend/core/pipeline.py:647` | 与本清单全部无冲突，改造时勿破坏 |

另有 `pytest_isolation.py` 的**写前隔离**（`os.chdir` 临时根 + 剔真实密钥 + 按 `testpaths` 建同名占位目录 + `atexit` 清理），是 C-16/C-17 补测的最低成本地基。

---

## 4. 两方结论的交叉矩阵

| 问题 | 外部审查 | 内部勘察 | 最终 |
|---|---|---|---|
| 滚动摘要泄漏未来 | E-P1-03 | — | C-02 |
| 补跑块不进 rolling | E-P1-08 | — | C-03 |
| 人物状态乱序覆盖 | E-P1-07 | — | C-04 |
| 共现当关系 | E-P1-02 | （数据层勘察提及 5285 key） | C-06 |
| `verified_facts` 无溯源 | E-P1-01 | — | C-12 |
| 伏笔回收无阻断 | E-P1-04 | — | C-10 |
| 上下文负担过重 | E-P2-05 | — | C-14（降级） |
| 字符截断 | E-P2-06 | — | C-15 |
| 分类器数字码误报 | — | I-classify-message | **C-01（P0）** |
| 伏笔 active 归零 | — | I-foreshadow-dormant | **C-05** |
| 并发轮无熔断 | — | I-no-global-breaker | C-07 |
| 重试逻辑四份拷贝 | — | I-retry-duplication | C-08 |
| 聚合层非原子写 | — | I-aggregate-nonatomic | C-09 |
| 破坏性操作无测试 | — | I-test-blindspot | C-16 |
| knowledge 无版本号 | — | I-knowledge-unversioned | C-13 |
| 零 CI / 无 timeout | — | I-no-ci | C-17 |
| Mica 读不到 config | — | I-desktop-mica | C-22 |
| 配置项不生效 | — | I-analyzer-params | C-21 |

**关键洞察**：外部审查的 E-P1-07 与 E-P1-08 同属"用标量代表状态"这一类；内部勘察的 C-01 与 C-05 则是"把标记当阻断"和"把检测章当呼应章"。四者都不是孤立 bug，而是四类反复出现的建模错误。

---

## 5. 合并执行计划

### 第 0 批 · 止血（约 30 分钟）

1. **C-01**：数字码收窄为带码字段形态；`_error_action` 的 `stop` 只认 exception 通道 structured；补 3 个反向 fixture
2. `agent.md` 的「指数退火」笔误（实际是「指数退避」，退火是 temperature 那层）
3. 验证：`python -m pytest backend/tests/test_error_taxonomy.py -q` + 全量回归

### 第 1 批 · 确定性一致性（约 0.5–1 天，全部零 LLM 成本）

4. **C-02 + C-03** 合并修（同一根因 `last_chapter` 标量），同步改 `backend/tests/test_memory_state_rolling.py:43-54`
5. **C-04** 用已有 `in_order` 门禁（约 4 行）
6. **C-05 + C-10** 合并修伏笔账本（休眠语义 + 回收阻断）
7. **C-06** `desc` 改为"第N章同章出现"
8. 补齐 5 个**不变量测试**（见 §6），作为 CI 门禁候选

### 第 2 批 · 计费与可靠性（约 0.5 天）

9. **C-07** 并发轮全局熔断（PLAN Phase 4）
10. **C-09** 聚合层 10 处统一 `safe_save_json`
11. **C-08** 抽 `_decide()` 单点（在 C-01 之后做，减少冲突）

### 第 3 批 · 语义可信度（需先出方案，涉及迁移）

12. **C-12** 双轨事实 + 证据章（先做"改名 + 溯源"的零成本版，复核版需 A/B）
13. **C-13** `knowledge.json` schema_version + dict 裁剪
14. **C-14 / C-15** 长线优化，需 A/B 基线

### 第 4 批 · 工程基建（持续）

15. **C-16** → **C-17**（先补 routes_workspace 测试，再建 CI）
16. C-18 / C-19 / C-20 / C-21 / C-22 / C-23 / C-24 / C-25 按需

---

## 6. 质量验证方案

外部审查的最后一问是"修复后如何证明质量改善，而不仅仅是代码变复杂"。必须**分两类**，不能都用 A/B：

### 6.1 确定性不变量（二元判定，可作 CI 门禁）

| 不变量 | 对应 | 断言 |
|---|---|---|
| 乱序不倒退 | C-04 | 注入第 4 章结果后，第 12 章的人物状态不得被覆盖 |
| 未来信息隔离 | C-02 | `chapter_limit=5` 的快照中不得出现 `last_updated_chapter=120` 的内容 |
| 补跑必入摘要 | C-03 | `last_chapter=120` 时补跑第 100 章，其块号必须进入覆盖集 |
| 无互动不生成关系 | C-06 | 两条无交集的弧光不得产出关系 desc |
| 事实不冒充已核实 | C-12 | `verified_facts` 条目必须带章节溯源 |
| 无证据不回收 | C-10 | `resolution` 为空或 `confidence != '高'` 时状态不得为 `resolved` |
| 分类器不误杀 | C-01 | `第 401 章…`/`赚了 1008 块`/`1026 万` 必须 `backoff` |
| 兼容性 | 全部 | 既有 JSON、工作区、断点恢复、前端接口零破坏 |

### 6.2 质量类（需 A/B）

对象：`workspace/《带着仓库到大明》`（2605 章，已完整跑通）+ `《大奉打更人》`。
方法：取 **50–100 章**人工核实子集，比较修改前后：

- 事实字段错误率、伏笔回收误判率、`active` 条目数量
- token 成本、单次运行耗时
- 报告可读性（人工盲评）

**不能仅凭单元测试通过就认定 LLM 分析质量提升**——这一点外部审查说得对，本文完全采纳。

---

## 7. 双方分歧记录

保留分歧以便后续复盘，不强行统一：

| 议题 | 外部审查立场 | 本文立场 | 依据 |
|---|---|---|---|
| P1-03 可达性 | "分析第 100 章时滚动摘要已吸收第 120 章" | 正常单趟不可达，仅续跑/补跑可达 | 信号量 FIFO + rolling 滞后（`pipeline.py:445`/`:691`） |
| P1-07 影响面 | 需要每人物维护最新章号字段 | 落盘有排序重建兜底，用现有 `in_order` 门禁即可 | `knowledge_base.py:319`、`pipeline.py:615-616` |
| P2-05 量级 | 九类任务 + 多路上下文，"注意力竞争" | 8 路上下文仅占 8%，降为最后处理 | `verify_prompt_budget.py` 实测 46,057 字符 |
| P1-04 严重度 | 回收判定过松 | 更严重的是 C-05：`active=0` 使阶段 2 复检**从未执行** | 真实账本 700 条实测 |

---

## 附录 A · 本机环境坑（NO_PROXY 方括号致 74 例假失败）

本机 `NO_PROXY=localhost,127.0.0.1,::1,[::1]`，其中**方括号形式 `[::1]`** 被 httpx 当 URL 解析：

```
ValueError: invalid literal for int() with base 10: ':1]'
  → httpx.InvalidURL: Invalid port: ':1]'
  → backend/core/llm_client.py:143  self.client = AsyncOpenAI(...)
```

对照实验：去掉 `[::1]` 即恢复；完全不设 proxy 变量也恢复。**清掉后全量 `864 passed in 92.17s`**。

`Remove-Item Env:\NO_PROXY` 在 PowerShell 层无效（SDK 在 python 进程内读 `os.environ`），必须在进程内清：

```powershell
python -X utf8 -c "import os; [os.environ.pop(k) for k in list(os.environ) if 'roxy' in k.lower()]; import pytest,sys; sys.exit(pytest.main(['-q','--no-header','-p','no:cacheprovider']))"
```

建 CI 时必须在 workflow 里显式写**不带方括号**的 `NO_PROXY`。

## 附录 B · 复现脚本清单

| 脚本 | 复现项 | 关键断言 |
|---|---|---|
| `%TEMP%\verify_review_8.py` | E-P1-01/02/03/07/08 | `regressed=True`；`snapshot_at_5_has_rolling=True`；`chapter_100_dropped=True` |
| `%TEMP%\verify_prompt_budget.py` | E-P2-05 | `total=46057`，8 路上下文合计 3,828 |
| `%TEMP%\verify_my_findings.py` | I-* | `第 401 章…` → `auth/stop`；ledger `active=0`/`dormant=546`；`open('w')`×11 |

## 附录 C · 未完成事项

- C-12 / C-13 / C-14 / C-15 的具体设计文档（schema 迁移、回滚方案、A/B 协议）
- C-16 的测试补齐计划（夹具设计与覆盖目标）
- 修改对"并发/断点恢复/知识库缓存/前端数据契约"的逐项影响评估，目前只做到 C-02/03/04/06 四项的定性判断

## 修订记录

| 日期 | 编号 | 说明 |
|---|---|---|
| 2026-10-30 | D-10 | 按 BASELINE 更正 C-21 表述为 1 项未接线 + 3 项无配置字段，并补充裁剪路径不一致 |
| 2026-10-30 | C-17 备注 | 本报告 C-17「全仓无 `.github/`、零 CI、零 lint gate」写于 S0 **之前**；S0 已提交 `.github/workflows/ci.yml` 与 `requirements-dev.txt`，该结论对当前状态已失效。正文保留原样不动，此处标注替代关系 |
| 2026-10-30 | 口径说明 | 文中测试数字分属不同时点：附录 A 的 `864 passed in 92.17s` 为 S0 前基线，`docs/BASELINE-2026-10-30.md` 的 892 为 S0 后基线；二者不可互相比较，也不代表当前值 |
