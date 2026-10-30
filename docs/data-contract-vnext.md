# vnext 数据契约（novel-analyzer-vnext / schema_version = 1）

> 状态：**S2 交付物**，2026-10-30 起草并随实现同步。
> 上游依据：`docs/PLAN-v2-long-novel-reliability-2026-10-30.md` §1.1、§2、§4 S2。
> 本文与代码的对应关系：常量与错误码在 `backend/vnext/constants.py`，指纹算法在
> `backend/vnext/fingerprint.py`，格式门禁与原子写在 `backend/vnext/artifacts.py`。
> **代码是实现的权威，本文是语义的权威**；两者冲突时以本文修订代码，不得反向解释文档。

---

## 0. 一句话概括

新版本只认一种数据：带 `format_id = "novel-analyzer-vnext"` 与 `schema_version = 1`
的文件，放在独立的数据空间里，身份由**内容哈希 + 策略版本**决定。其余一律拒绝，
不猜字段、不回退解析、不写转换脚本。

---

## 1. 格式标识与版本

| 标识 | 值 | 谁决定 | 不匹配时 |
| --- | --- | --- | --- |
| `format_id` | `novel-analyzer-vnext` | 代码常量 | `UNSUPPORTED_ARTIFACT_FORMAT` |
| `schema_version` | `1`（整数，不是字符串） | 代码常量 | `UNSUPPORTED_SCHEMA_VERSION` |

**规则**

1. 每个持久化 JSON 产物的**顶层**必须同时有这两个字段。缺任一即拒绝——不存在
   "只有版本号也算"的例外。
2. 两个值都来自代码常量（`backend/vnext/constants.py`），**不接受**配置、环境变量
   或文件内容里的覆盖值。任何"版本自适应"都是"回退解析"的变种。
3. `schema_version` 只在不兼容变更时 +1。**不为旧版本提供降级解析**：读到
   `schema_version = 0`（或未来的 2）直接报错。

### 1.1 策略版本（不写进文件头，但参与指纹）

| 名称 | 位置 | 何时 +1 |
| --- | --- | --- |
| `TEXT_NORMALIZATION_VERSION` | `fingerprint.py` | 改变章节文本规范化规则（换行、行尾空白、空行压缩） |
| `block_strategy_version` | 分块方案记录 | 改变"如何把章节切成块"（含 `block_size` 语义变化） |
| `prompt_strategy_version` | 结果记录 | 改变 Prompt 模板结构、字段语义、few-shot |
| `recheck_strategy_version` | 复检任务记录 | 改变 recheck 接口、枚举语义、上下文取材规则 |

**判定口诀**：会改变"同一输入的字节"就升版本；只改变"展示"或"性能参数"不升。
模型名与温度属于**运行参数**，写进 `model_fingerprint`，不升策略版本。

---

## 2. 目录布局

```
<项目根>/
├── config_vnext.json          # 独立运行配置（首次启动生成；不读旧 config.json）
├── queue_state_vnext.json     # 独立队列状态（运行时状态）
├── books_vnext.json           # 独立书目索引（运行时状态）
└── workspace_vnext/           # 独立数据根（默认值，可由配置覆盖）
    └── <book_dir>/            # 目录名仅供人看；身份是 manifest 里的 book_id
        ├── manifest.json      # 项目清单：身份、源快照、活动块/结果引用、revision
        ├── source/
        │   ├── chapters/
        │   │   └── <chapter_id>.txt     # 规范文本（权威，source_ref 只指向这里）
        │   └── index.json               # 章节顺序与 content_hash 索引
        ├── blocks/
        │   └── <block_signature>.json   # 块定义（成员、顺序、策略版本）
        ├── results/
        │   └── <result_id>.json         # 分析结果（含依赖指纹）
        ├── ledger/
        │   └── foreshadow.json          # 伏笔：occurrence + entity + candidate
        ├── derived/                      # 派生产物（可重建，均带 fingerprint + coverage）
        │   ├── final_summary_report.md
        │   ├── foreshadow_audit.md
        │   ├── style.md
        │   └── aggregated/*.json
        └── cache/
            └── tasks.json               # 任务完成态（task_key → 状态/结果引用）
```

**为什么把"运行时状态"和"分析数据"分开**：`queue_state_vnext.json` 与
`books_vnext.json` 是会随操作频繁变化的小文件，混进数据根会让"整个数据根能否
整体搬迁"这件事变得不可靠；而 `workspace_vnext/` 可以整体拷走、整体备份。

**为什么派生目录存在**：派生产物**必须可重建**，但它们很贵（LLM 调用）。带指纹
的派生产物可以复用，无指纹或指纹不符时必须重算——不能"文件在就算有"。

---

## 3. 对象定义

所有对象的 JSON 例子都是**完整可解析**的（字段齐全，不用 `...` 省略）。

### 3.1 `manifest.json` — 项目清单

| 字段 | 类型 | 必需 | 语义 | 谁绑定 |
| --- | --- | --- | --- | --- |
| `format_id` / `schema_version` | str / int | ✓ | 见 §1 | 程序 |
| `book_id` | str | ✓ | 稳定书身份（`bk_` + 12 hex），首次建项目即定，永不改 | 程序 |
| `workspace_id` | str | ✓ | 数据根身份（`ws_` + 12 hex），防止"换工作区后 ID 撞车" | 程序 |
| `title` | str | ✓ | 展示书名（可改，不影响身份） | 用户/程序 |
| `revision` | int | ✓ | 清单版本，每次成功提交 +1 | 程序 |
| `source_snapshot_id` | str | ✓ | 当前源快照 ID；变更正文即换新值 | 程序 |
| `text_normalization_version` | int | ✓ | 生成这些 `content_hash` 时用的规范化规则版本；不匹配即全库 `STALE_INPUT` | 程序 |
| `chapters` | list | ✓ | 有序 `[{chapter_id, ordinal, display_no, content_hash}]` | 程序 |
| `active_blocks` | list | ✓ | 当前有效块签名（有序，**不得重叠**） | 程序 |
| `active_results` | list | ✓ | 当前有效结果 ID（每个 `active_block` 至多一个） | 程序 |
| `created_at` / `updated_at` | str | ✓ | ISO8601，**不参与任何指纹** | 程序 |

```json
{
  "format_id": "novel-analyzer-vnext",
  "schema_version": 1,
  "book_id": "bk_8f3a1c9d2e4b",
  "workspace_id": "ws_11223344aabb",
  "title": "《示例长篇小说》",
  "revision": 7,
  "source_snapshot_id": "src_5c1e9a7b3d2f",
  "chapters": [
    {"chapter_id": "ch_a1b2c3d4e5f6", "ordinal": 1, "display_no": 1,
     "content_hash": "9f2c4d1b8e7a6305f4c2b9d8e1a07c3b6d5f4e3a2b1c0d9e8f7a6b5c4d3e2f1a"},
    {"chapter_id": "ch_0f1e2d3c4b5a", "ordinal": 2, "display_no": 2,
     "content_hash": "3d7b1a9c5e2f8460b1d3c7a5e9f2b4d6c8a0e1f3b5d7c9a2e4f6b8d0c2a4e6f8"}
  ],
  "active_blocks": ["b7e2c4a6f8d0b2e4c6a8f0d2b4e6c8a0f2d4b6e8c0a2f4d6b8e0c2a4f6d8b0e2"],
  "active_results": ["r4c9e1a3b5d7f9c1e3a5b7d9f1c3e5a7b9d1f3c5e7a9b1d3f5c7e9a1b3d5f7c9"],
  "created_at": "2026-10-30T09:00:00+08:00",
  "updated_at": "2026-10-30T09:41:12+08:00"
}
```

**约束**

- `active_blocks` 之间**不得有重叠章节**。允许并存的是"同一段的多个重算版本"，
  那种情况只有一个进 `active_results`，其余留在 `results/` 里供审计。
- `active_results` 的每个结果必须能通过 §5 的依赖校验；校验失败的条目必须从
  `active_results` 移除并标记 `STALE_INPUT`，不能留着让读链读到旧值。

### 3.2 章节源与 `source_ref`

| 字段 | 类型 | 必需 | 语义 |
| --- | --- | --- | --- |
| `chapter_id` | str | ✓ | `ch_` + `sha256(book_id \x1f ordinal)[:12]`；**不含正文哈希** |
| `ordinal` | int | ✓ | 故事顺序（1 起）；**不要求 display_no 连续** |
| `display_no` | int \| null | ✓ | 展示章号（原文写的"第几章"），允许断号/重复 |
| `content_hash` | str | ✓ | 规范文本的 sha256（64 hex） |
| `source_file` | str | ✓ | 原始导入文件名（追溯用，不参与身份） |

**为什么 `chapter_id` 不含正文哈希**：§2.1-7 要求"改动早期来源 → 依赖产物标
`STALE_INPUT`"。若 ID 随正文变化，改动就变成"删掉一章 + 新增一章"，依赖关系
断开，`STALE_INPUT` 无从标记。所以 ID 绑定**位置身份**，正文变化只改
`content_hash`。

**`source_ref`**

| 字段 | 语义 |
| --- | --- |
| `book_id` / `chapter_id` / `content_hash` | 定位到"哪一章的哪一版正文" |
| `start` / `end` | **Unicode 码点**偏移，左闭右开 |
| `quote` | 该区间的原文（冗余存储，用于人核对与漂移检测） |

- 偏移只能指向 `source/chapters/<chapter_id>.txt` 的**规范化后**内容，不能指向
  摘要、不能指向模型输出。
- emoji 等增补平面字符按 **1 个码点** 计；不得用 UTF-16 码元数（JS 的
  `string.length`）算偏移。
- 越界直接失败（`SOURCE_REF_UNRESOLVED`），**不截断**。

### 3.3 块（`blocks/<block_signature>.json`）

| 字段 | 语义 |
| --- | --- |
| `block_signature` | `sha256(canonical_json({block_strategy_version, chapters:[[chapter_id, content_hash], …]}))` |
| `chapter_ids` | 有序成员列表；顺序参与指纹 |
| `block_strategy_version` | 见 §1.1 |
| `start_display_no` | **仅供展示**，不参与指纹 |

> 实现说明：`block_signature` 的**计算输入**是 `[[chapter_id, content_hash], …]`，
> 而块文件里只存 `chapter_ids`——因为 `content_hash` 已由 manifest 的 `chapters`
> 提供，重复存储会让"两处哈希不一致"成为可能。校验时按 manifest 重算。

**签名覆盖什么**：块策略版本 + 有序的（章节 ID，正文哈希）对。
**不覆盖什么**：展示章号、块大小数字本身（已隐含在成员列表里）、任何时间戳。

### 3.4 分析结果（`results/<result_id>.json`）

| 字段 | 必需 | 语义 |
| --- | --- | --- |
| `result_id` | ✓ | `sha256(canonical_json({block_signature, prompt_strategy_version, model_fingerprint, dependency_fingerprint}))` |
| `block_signature` | ✓ | 指向 §3.3 |
| `source_chapters` | ✓ | 实际送进模型的 `[{chapter_id, content_hash}]`（**必须与块成员一致**，不一致即 `BLOCK_SIGNATURE_MISMATCH`） |
| `extracted` | ✓ | 提取数据（core_events / character_arcs / …，语义见 §4 数据域） |
| `model_fingerprint` | ✓ | 模型名 + 关键参数（温度、max_tokens、json_mode、思考模式） |
| `prompt_strategy_version` | ✓ | 见 §1.1 |
| `dependency_fingerprint` | ✓ | 本次用到的上游结果/快照的指纹（空依赖也要算一个确定值，不能缺字段） |
| `status` | ✓ | `SUCCEEDED` / `FAILED` / `SKIPPED` / `CANCELLED` / `BLOCKED` |
| `usage` | ✓ | `{prompt_tokens, completion_tokens, usage_status}`；未知写 `null` + `UNKNOWN` |
| `created_at` | ✓ | 不参与指纹 |

**`status` 的五种取值必须分开**（PLAN §1.2）：审核跳过是 `SKIPPED`（不是成功，也不是
模型失败）；用户取消是 `CANCELLED`；账户级错误导致整本停摆是 `BLOCKED`。

### 3.5 派生产物（`derived/`）

| 字段 | 语义 |
| --- | --- |
| `artifact_id` | 产物身份（`da_` + 12 hex） |
| `depends_on` | 依赖的结果 ID / 账本 revision 列表 |
| `dependency_fingerprint` | 依赖集合的指纹 |
| `strategy_version` | 生成策略版本 |
| `coverage` | 覆盖范围（`{from_ordinal, to_ordinal, complete: bool, missing: []}`） |
| `completeness` | `COMPLETE` / `PARTIAL`（缺块时不得标 COMPLETE） |

`.md` 类派生产物不能带 JSON 字段，因此采用**旁挂清单**：`derived/<name>.meta.json`
存上述字段，正文另存同名 `.md`。两者缺一即视为不完整。

### 3.6 任务记录（`cache/tasks.json`）

| 字段 | 语义 |
| --- | --- |
| `task_key` | `sha256(canonical_json({kind, stage, inputs, strategy_versions}))`，**不含 run_id** |
| `run_id` | 产生它的运行（可多个任务共享，也可能是缓存命中时的**原** run_id） |
| `status` | `PENDING` / `RUNNING` / `SUCCEEDED` / `FAILED` / `CANCELLED` / `BUDGET_SKIPPED` / `STALE_INPUT` |
| `result_ref` | 成功时的 `result_id` 或产物 ID |
| `attempts` / `usage` | 实际尝试与消耗（`usage_status` 区分未知与零） |
| `finished_at` | 仅成功或终态失败才写；**未执行态不写完成标记** |

---

## 4. 数据域（后续批次接入，S2 只定形状）

S2 只交付"入口 + 来源 + 登记"，以下数据域的**完整语义**在后续批次实现，这里先固定
形状，避免 S3–S7 各自发明字段：

- **知识库**（S3）：`fact_records`（`fact_id` / 陈述 / 来源 / `verification_state` /
  冲突引用）、人物状态、`cooccurrence_edges`、`semantic_relationships` 四者分表。
  `UNVERIFIED` 与 `EVIDENCE_LOCATED` 是**两个不同状态**，后者不等于"语义已核实"。
- **伏笔**（S5/S6）：`occurrence` → `entity` → `candidate` 三层；`resolution_state`
  与 `recency_state` 正交；`recheck_status` 与 `resolution_state` 正交。

**S2 的边界很明确**：只保证"这些文件带合法格式标记、能被登记、能被拒绝"，不保证
它们的内容语义正确。

---

## 5. 校验顺序

读任何 vnext 产物，固定按此顺序；任一步失败即返回该步的错误码，**不继续**。

```
1. 文件存在？            否 → FileNotFoundError（调用方区分"没有"与"不合法"）
2. JSON 可解析？         否 → UNPARSEABLE_ARTIFACT
3. 顶层是对象？          否 → UNSUPPORTED_ARTIFACT_FORMAT
4. format_id 存在且相等？ 否 → UNSUPPORTED_ARTIFACT_FORMAT
5. schema_version 相等？  否 → UNSUPPORTED_SCHEMA_VERSION
6. 必需字段齐全？         否 → MISSING_REQUIRED_FIELD（不补默认值）
7. 字段类型/取值域合法？  否 → INVALID_FIELD_VALUE
8. 引用可解析？           否 → SOURCE_REF_UNRESOLVED
9. 块签名与当前源一致？   否 → BLOCK_SIGNATURE_MISMATCH
10. 依赖指纹与当前一致？  否 → STALE_INPUT
```

第 4 步必须在第 5 步之前：旧产物同时缺两个字段时，用户需要看到的是
"此目录不属于本版本"，而不是"版本号不对"这种指向错误方向的信息。

对应实现：`backend/vnext/artifacts.py:assert_vnext_payload`（第 2–5 步）与
`read_json_artifact`（第 1–5 步）。第 6–10 步由各数据域的构造器提供。

---

## 6. 指纹定义（精确到可复现）

通用规则：**`canonical_json` = 键排序 + 无空白 + `ensure_ascii=False`**；多段拼接
用 `\x1f`（单元分隔符）而不是 `|`，避免 `a|b` 与 `a|`+`b` 的歧义碰撞。

| 指纹 | 输入（按序） | 长度 | 实现 |
| --- | --- | --- | --- |
| `content_hash` | `sha256(NFC 无关的规范文本 utf-8)` | 64 | `fingerprint.content_hash` |
| `block_signature` | `sha256(canonical_json({block_strategy_version, chapters:[[cid, chash],…]}))` | 64 | `fingerprint.block_signature` |
| `dependency_fingerprint` | `sha256(canonical_json(依赖映射))` | 64 | `fingerprint.dependency_fingerprint` |
| `task_key` | `sha256(canonical_json({kind, stage, inputs, strategy_versions}))` | 64 | `fingerprint.task_key` |
| `result_id` | `sha256(canonical_json({block_signature, prompt_strategy_version, model_fingerprint, dependency_fingerprint}))` | 64 | `fingerprint.result_id` |
| `evidence_id` | `sha256(join(\x1f, [book_id, chapter_id, content_hash, start, end]))` | **12** | `fingerprint.evidence_id` |

**文本规范化规则**（`TEXT_NORMALIZATION_VERSION = 1`）：去 BOM → 换行统一 LF →
删行尾空格/制表 → 连续空行压至多 1 个 → 文末恰好 1 个 LF。
**不做**全角半角转换、不做简繁转换、不删行首缩进——这些都改变字符数，会让已保存的
`source_ref` 偏移整体失效。

---

## 7. 写入规则（并发与一致性）

1. **单书串行提交**：同一本书的 manifest 提交必须串行，粒度是"书"不是"进程"。
   不同书之间不共享全局锁——否则一本卡住会让所有书排队。
2. **先落结果，再发引用**：先 `write_json_atomic` 结果文件成功，才允许把它的 ID 写进
   `active_results`。反过来会出现"清单指向一个不存在的文件"。
3. **原子写**：临时文件与目标**同目录**（跨卷 rename 不原子）、名字唯一、
   `fsync` 后 `os.replace`；Windows 的 `PermissionError` 用固定次数重试
   （`artifacts.write_json_atomic`）。
4. **revision 冲突只重试元数据**：manifest 因无关任务推进而变了 → 重读 + 重试提交。
   只有**实际依赖**变化才判结果过期，不重跑模型。

---

## 8. 错误码

| 错误码 | 触发条件 | 用户可见文案 | 可恢复 |
| --- | --- | --- | --- |
| `UNSUPPORTED_ARTIFACT_FORMAT` | 缺格式标记 / `format_id` 不符 / 顶层非对象 | 此目录不属于本版本，请从原文创建新项目 | 否（需重新导入） |
| `UNSUPPORTED_SCHEMA_VERSION` | `schema_version` 不在支持范围 | 此数据由其他版本的软件创建，请升级后打开 | 否 |
| `MISSING_REQUIRED_FIELD` | 缺 §3 中标记为必需且无默认值的字段 | 数据文件缺少必需字段，可能存在写入中断 | 是（可重算该产物） |
| `INVALID_FIELD_VALUE` | 类型/取值域非法 | 数据文件字段取值不合法 | 是 |
| `UNPARSEABLE_ARTIFACT` | JSON 解析失败 | 数据文件损坏，可能需要重算 | 是 |
| `BLOCK_SIGNATURE_MISMATCH` | 结果/块的成员或哈希与当前源不符 | 源章节已变化，需要重算 | 是（重算） |
| `SOURCE_REF_UNRESOLVED` | 引用越界或无法唯一定位 | 引用的原文位置无法定位 | 是 |
| `STALE_INPUT` | 依赖指纹不符 | 依赖的输入已更新，结果需要重算 | 是 |
| `REVISION_CONFLICT` | 同一本书并发提交清单 | （内部，自动重试一次） | 是 |

HTTP 映射：`UNSUPPORTED_*` → 409（客户端不该重试）；其余 → 422 或 500 视来源；
`STALE_INPUT` 不出现在 HTTP 层，它是任务状态。

---

## 9. 不做什么

- **不做旧格式迁移脚本**。旧产物留在原处，新版本不打开、不覆盖、不删除、不归档。
- **不做旧字段兼容降级**。缺字段即报错，不用旧默认值补全。
- **不建通用存储框架**，不引入数据库、不引入向量库、不做多 Agent。
- **不把时间戳、随机 ID、全书统计混进指纹**——它们让缓存必然失效或泄漏未来信息。

---

## 附录 A：旧产物清单（实测，`workspace/` 下真实存在的文件）

用于验证"拒绝矩阵"覆盖完整。这些文件**全部**缺少 `format_id` 与
`schema_version`，因此都会命中 `UNSUPPORTED_ARTIFACT_FORMAT`。

| 位置 | 文件名 | 写入方 |
| --- | --- | --- |
| 书根 | `knowledge.json` | `KnowledgeBaseManager.save` |
| `blocks/` | `NNNN.txt`（按序号的章节正文） | `splitter_service.save_split` |
| `blocks/` | `chapters.json`、`metadata.json`、`split_report.txt` | `splitter_service.save_split` |
| `output/` | `chapter_N_result.json` | `memory_state.flush_to_disk` |
| `output/` | `chapter_N_failed.json`、`chapter_N_skipped.json` | `memory_state` 失败/跳过标记 |
| `output/` | `rolling_summary.json` | `pipeline._update_rolling_summary` |
| `output/` | `token_stats.json`、`summary_token_stats.json` | 分析/总结统计 |
| `output/` | `foreshadow_ledger.json`（v3） | `ForeshadowLedger.save` |
| `output/` | `final_summary_report.md`、`foreshadow_audit.md`、`style.md`、`analysis_summary.md` | 各服务 |
| `output/` | `locations_normalized.json`、`spatial_relationships_normalized.json` | `location_normalizer` |
| `output/` | 11 个 `*_aggregated.json` | `aggregate_utils.aggregate_full_output` |
| `output/final_summary_checkpoint/` | `manifest.json` + `volume_N.md` | `final_summary._save_checkpoint_batch` |
| `backups/` | `knowledge_YYYYmmdd_HHMMSS.json` | `knowledge_base._create_backup` |

## 附录 B：读写入口与本批处理

S2 只把**入口**接上格式门禁，不逐域重写消费者（那是 S3–S9 的事）。本表是接线的
清单，也是后续批次的工作底稿。

| # | 入口 | 位置 | 读写 | 现状 | S2 处置 |
| --- | --- | --- | --- | --- | --- |
| 1 | 书目发现判定 | `backend/services/book_service.py:32` | 读 | 看目录有无 `blocks/`/`output/`（形状猜测） | 改为读 `manifest.json` 判格式 |
| 2 | 候选根收集 | `backend/services/book_service.py:37-76` | 读 | 队列/`working_directory`/`workspace_path`/`分析结果` | 只扫 `workspace_vnext/` |
| 3 | 书目刷新 | `backend/services/book_service.py:79-94` | 读 | `iterdir` 全扫 | 走 `books_vnext.json` |
| 4 | 断点恢复 | `backend/core/memory_state.py:236-262` | 读 | `glob chapter_*_result.json` 判已完成 | 改为 manifest `active_results` + 依赖校验 |
| 5 | 失败/跳过标记 | `backend/core/memory_state.py:294`、`:308` | 读 | `glob` 标记文件 | 并入结果 `status` |
| 6 | 队列状态 | `backend/services/queue_manager.py:36` | 读 | `PROJECT_ROOT/queue_state.json` | 换 `queue_state_vnext.json` + 格式校验 |
| 7 | 队列载入/保存 | `backend/services/queue_service.py:42`、`:183` | 读/写 | 直接读写 | 同上 |
| 8 | rolling 载入 | `backend/core/knowledge_base.py:279` | 读 | `output/rolling_summary.json` | 归入新知识域（S4） |
| 9 | 结果落盘 | `backend/core/memory_state.py:172` | 写 | `chapter_N_result.json` | 换 `results/<result_id>.json` |
| 10 | 切章落盘 | `backend/services/splitter_service.py:1155-1179` | 写 | `blocks/*` | 换 `source/`（S2-2） |
| 11 | 项目创建 | `backend/services/splitter_service.py:1249` | 写 | `workspace/<书名>/blocks` | 换 `workspace_vnext/<book>/` + manifest |
| 12 | 汇总聚合 | `backend/utils/aggregate_utils.py:637-717` | 写 | 11 个裸 `open('w')` | 换原子写 + `derived/` |
| 13 | 总结 checkpoint | `backend/services/final_summary.py:1105-1191` | 读/写 | manifest + `volume_N.md` | 换 `cache/tasks.json`（S7） |
| 14 | 报告读取 | `backend/api/routes_books.py:53`、`:89`、`:106`、`:124` | 读 | 直接读文件 | 加格式门禁或改读新产物 |
| 15 | 归一化产物 | `backend/api/routes_location_normalization.py:30-31` | 读 | `output/locations_normalized.json` | 同上 |
| 16 | 可视化数据 | `backend/services/viz_service.py:222-223` | 读 | 同上 | 同上 |
| 17 | 风格产物 | `backend/services/style_service.py:53`、`backend/api/routes_analysis.py:264` | 读/写 | `style.md` | 换 `derived/style.md` + `.meta.json` |
| 18 | 导出 | `backend/utils/export_utils.py:41` | 写 | `analysis_summary.md` | 换 `derived/` |

> **本轮已完成（原计划排在 S3–S9，复审后归回 S2）**：第 2（书目发现）、4（断点恢复）、
> 9（结果落盘）、10（切章落盘）、11（项目创建）行。其余 13 行仍是"按域重写消费者"，
> 留在 S3–S9；在那之前它们是**未接线**状态，不是"已兼容"。

## §10 依赖链：局部失效的准确边界

块的"已完成"不能只看块签名。某块读的是前情摘要，"签名没变"但它依赖的前情换过了——
这时它其实已经过期。因此每块的期望依赖由**有序的前序结果 ID + 生成条件**算出：

```
expected_dependency = sha256(canonical({
    prior_results: [前序各块当前有效 result_id, 未完成处为 null],
    context_strategy_version, prompt_strategy_version, model_fingerprint
}))
```

判定按计划顺序**前向滚链**：

| 情形 | 结果 |
| --- | --- |
| 前序全部当前有效、本块依赖一致 | `done` |
| 前面某块换了结果（新 `result_id`） | 本块及其后 `STALE_DEPENDENCY`，之前的块不受影响 |
| Prompt 策略 / 模型 / 上下文策略变更 | 全部 `STALE_DEPENDENCY` |
| 前面某块未完成 | 其后各块依赖不成立（`STALE_DEPENDENCY`；若本块根本没有结果则 `missing`） |

不传 `strategy` 时退化为"只比块签名与状态"，仅供早期扫描/预览——**不能**用于判定"可以跳过"。

## §11 S2 基础入口（本轮补齐）

| 能力 | 入口 | 说明 |
| --- | --- | --- |
| 从原文建项目 | `POST /api/vnext/projects` | `{title, text}` 走同一套切章器；`{title, chapters:[…]}` 为已切好的输入；两者都给以 `chapters` 为准 |
| 切章 | 同上（`text` 分支） | 复用 `splitter_service.split_text`，不另写正则——两套切章规则会分叉 |
| 结果落盘 | `results.publish_result()` | 写 `results/<result_id>.json` + 登记 `active_results`，返回**提交后的**清单 |
| 断点恢复（读） | `GET /api/vnext/books/{id}/blocks` | 返回每块 `done` / `stale:<原因>` / `missing:<原因>` |
| 断点恢复（写） | `POST /api/vnext/queue/restore` | 按当前源重算进度；算不出的书写明"项目不可用，进度未校验" |

## 附录 C：本批实现决定（不属契约，供审查）
| 决定 | 理由 |
| --- | --- |
| 配置文件名 `config_vnext.json`、书目索引 `books_vnext.json` 由本批确定 | 计划只要求"独立"，未给名字；集中定义在 `constants.py`，改名只改一处 |
| `chapter_id` 绑定 `(book_id, ordinal)` 而不含正文哈希 | 否则改一个字就变成"换了一章"，`STALE_INPUT` 无法沿依赖传播 |
| 空依赖也要算出确定指纹，不允许字段缺失 | 缺字段等价于"没算过"，而"没算过"与"依赖为空"在处理上必须可区分 |
| `.md` 派生产物用旁挂 `.meta.json` 存指纹 | Markdown 无法承载结构化字段；把 JSON 塞进注释会让产物不再是纯 Markdown |
| S2 不实现 `MISSING_REQUIRED_FIELD` 及之后步骤的通用框架 | 各数据域的必需字段在 S3–S7 落地时才知道；先建通用框架会把错误形状固化 |

## §12 知识与窗口（S3 定稿）

### 12.1 三层职责

| 层 | 模块 | 职责 | 关键约束 |
| --- | --- | --- | --- |
| 归约 | `knowledge.reduce_knowledge` | 去重、排序、定版 | **分批 ≡ 一次给全**；排序按来源位置而非到达顺序 |
| 快照 | `knowledge.build_snapshot` | 按故事位置切出"某点之前可见" | 上界**排他**；来源未知**一律排除** |
| 窗口 | `prompt_view.build_prompt_window` | 按限额切出"这次给模型看什么" | **库永不裁剪**；截断必须报告 |

旧实现把三者揉在一起，落盘前做裁剪，于是"库里有什么"取决于当初配了多大窗口。

### 12.2 证据与引用

- `FactRecord.verification_state` 只有两档：`UNVERIFIED` / `EVIDENCE_LOCATED`。**没有"高置信"**——置信是模型的自我评价，不是事实属性。
- 校验分五级（`EXACT` / `NORMALIZED` / `RELOCATED` / `SEGMENTED` / `FUZZY`），只把"是否定位"一位给下游；失败**不丢数据**，只降级为 `UNVERIFIED`，且**不修正模型给的偏移**。
- `content_hash` **不由模型提供**，由 `graph.stamp_evidence_hash` 从 manifest 覆盖回填；该章无已知哈希的语义关系**丢弃并回报**（否则它永远无法被校验，却一直以"有来源"的样子存在）。

### 12.3 图谱边

- `CooccurrenceEdge`（同章邻近）与 `SemanticRelationship`（有互动证据）**分表**，`merge_edges` 收到语义边直接 `TypeError`——"永不合表"由类型守住，不靠调用方自觉。
- 语义边准入：`interaction is True`（严格同一性）+ 来源区间合法 + `relation` 非空；`a == b` 丢弃；名字对归一（`甲-乙` ≡ `乙-甲`）。
- **已知限制（本层不解决）**：名字匹配是裸子串，`甲` 在 `甲三` 中出现也算一次，共现可能虚高；消歧属实体归并（S5/S6）。同一对在同章多次共现只保留一条边（位置取首次），因为图展示与判定只需要"是否邻近"。

### 12.4 三条口径（本批裁决）

1. `PromptLimits` 的非法值报错文案统一为 `{field} 必须是正整数，收到 {value!r}`，`path` 填 `prompt_limits.<field>`；
2. 未知配置键**忽略**（配置可能来自更新版本），但顶层非 `Mapping` 直接拒收；
3. `PromptLimits` 是**内部设置**，本期不新增任何用户可见开关——文档不得宣称它可在界面调整。
