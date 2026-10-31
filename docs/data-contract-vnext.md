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

## §13 覆盖、实体身份与裁决（S4–S6 定稿）

### 13.1 覆盖视图（`rolling.py`）

| 字段 | 含义 | 不能混淆的点 |
| --- | --- | --- |
| `commit_cursor` | 从第一块起"已完成且依赖新鲜"的**连续**前缀长度 | 不是"已完成块数" |
| `committed_batches` | 已完成块数 // 批大小 | — |
| `consumable_upto` | 可安全喂给摘要的最远位置，**对齐批次** | 半个批次不许进摘要 |
| `batch_aligned_upto` | 脏点收缩前的对齐位置 | 用来判断"是否需要重建" |
| `claimed_range` | 可对外声明的章号区间，从**真实首章**起算 | 分析从第 100 章开始就不能称"全文前缀" |
| `gaps` | 被 `TERMINAL_SKIPPED` 越过的位置 | 越过 ≠ 完整，整体标 `PARTIAL` |
| `dirty_from` | 已提交结果改版/迟到修复的位置 | 其后摘要不可信 |

**不变量**：`PENDING_RETRY` 暂停推进；`TERMINAL_SKIPPED` 推进但留 gap；
`DONE` 要求签名一致（签名不符的结果属于另一个块，忽略）。

### 13.2 伏笔实体（`foreshadow.py`）

- `occurrence_id` = 完整 64 hex（`book_id + chapter_id + content_hash + start + end + kind`）；12 位只用于展示。
- `entity_id` = `fs_` + 16 hex，只依赖 `(book_id, seed_occurrence_id)`，seed 取**来源最早**的出现 ⇒ 补入更早证据不改号。
- `candidate_id` = `cd_` + 16 hex，依赖 `(entity_id, volume_start_ordinal, batch_index)` ⇒ 同一轮重放不产生新身份。
- `cutoff_view` 的顺序固定：筛可见来源 → **整条排除**（实体只要有一条越过截止点就整个不出现）→ 算属性 → `min_mentions` 过滤 → 排序（`-mentions, -importance, first_seen, entity_id`）→ 截断。
- **本层不做相似度合并**：两个悬念是否同一实体由调用方给的 `entity_id` 决定。"谁藏了王冠"与"王冠在哪里"不会被文本相似度误合并。

### 13.3 裁决（`resolution.py`）

- 模型只许给四枚举；**小写、近义词、缺失值一律 `INVALID`**（宽容解析会让下游以为已核对）。
- `SUPPORTED` 的门禁：答案非空 + 无反证引文 + 反证已执行 + 无材料不足 + 引文落在给定范围内 + ID 属于本任务。
- 实体归约：任一 `SUPPORTED` → `AUTO_RESOLVED`；仅有不足/冲突 → `PROPOSED_RESOLVED`；只有垃圾 → `OPEN`。**垃圾候选不降级已支持结论。**
- 覆盖守恒式：`candidates_total = supported + insufficient + conflict + invalid + stale + not_checked`，
  其中 `conflict = DIFFERENT_ANSWER + CONTRADICTED`；`invalid` 必须等于 `invalid_by_reason` 之和。
- 任务状态有序互斥：`NOT_RUN → RUNNING → STOPPED → PARTIAL → COMPLETED`；
  **空候选跑过 = `COMPLETED` 且计数 0**，**缺覆盖汇总 = `PARTIAL`**。

### 13.4 上下文取材（`context.py`）

- 先扩到句读边界（`。！？；…\n`），再按 `PRE_TARGET/POST_TARGET` 补足，最后受 `HARD_MAX_CHARS` 约束。
- **到章节尽头不是截断**；**撞硬上限才是截断**（`truncated=True`, `reason=HARD_BUDGET`），下游必须据此判证据不足。
- 硬上限下**保证引用本身完整**（引用被切掉就没法核对）；引用本身超过上限时标 `QUOTE_EXCEEDS_HARD_MAX`。

#### 13.2.1 三处实现口径（实现过程中发现规格有张力，此处定稿）

1. ~~**"整条排除"优先于"截止点之前可见"**~~ —— **已撤回，见 §13.3 第 1 条**（原口径破坏前缀不变性）

   （原内容保留供追溯）：一个实体若横跨 1–10 章，而 `cutoff_ordinal=5`，
   它在这个视图里**整个不出现**（而不是"只显示第 1–4 章那几次出现"）。理由：实体级属性
   （类型、重要度、代表描述）是对全部出现记录算的，只给一部分会把属性算成半成品。
   因此"1..10 取 5 → 只有 1–4"只适用于**每章一个实体**的构造，不能理解为"同一实体可被切片"。
2. **`entity_id` 在首次登记时确定，之后不再重算**：``seed_occurrence_id`` 只是记录"当时最早的那次出现"。
   补入更早证据时，`seed_occurrence_id` 会前移，但 `entity_id` 必须**沿用旧值**——否则
   "补一条早期证据"就等于"换了一个悬念"，历史裁决全部失联。
3. **去重键是 `(entity_id, occurrence_id)`**：`occurrence_id` 的公式不含 `entity_id`（同一处原文
   确实只是同一处原文），所以同一锚点被两个实体引用时 ID 必然相同；靠二元组区分，不靠改公式。

### 13.3 复审二（2026-10-30）：§13 的两条口径被推翻

外部复审复现了 7 个会直接产生错误结论的缺口。以下四条改的是**语义**，不是实现细节：

1. **撤回 §13.2.1 第 1 条（实体整条排除）**。它破坏前缀不变性：实体在第 3 章出现、第 12 章也出现时，
   `cutoff=11` 的视图里它消失；而"第 12 章还没写"时的同一视图里它在。**过去没变，过去能查到的视图却变了。**
   正确口径：截止点之后的出现记录**不进视图**，但实体的次数/首末次/跨度一律**只按可见记录重算**。
   §13.2.1 第 2、3 条（`entity_id` 首次确定不重算、去重键为二元组）仍然成立。
2. **`SUPPORTED` 必须有可定位证据**。原先 `evidence_id`/`supporting_span`/`supporting_quote` 全缺也能过，
   于是"模型觉得答对了"直接变成 `AUTO_RESOLVED`。现在四件套缺一不可，且**必须有校验器**
   （没有校验器 = 无人核对引文，判 `NO_QUOTE_VERIFIER`）。
3. **`INVALID` 与 `INSUFFICIENT` 的边界**：响应不可用（枚举写错、字段缺失、payload 非对象）→ `INVALID`
   且 `retryable=True`，**计入"未核对"，可重跑**；原文没给出足够证据（没查反证、材料不足、
   说支持却列反证）→ `INSUFFICIENT`，**计入"已核对"**。把后者打成 `INVALID` 会让实体退回 `OPEN`，
   等于把"没查"读成"没问题"。
4. **覆盖计数按候选集合核对，不按返回行数**。同一候选答两次时行数求和照样守恒，但其中一行
   根本没被采用。新增 `duplicates` / `foreign` 两类计数与 `integrity_error()`：
   出现这两类时该轮复检**不得标记 `COMPLETED`**（降为 `PARTIAL`）。

另一条改的是**状态继承**：块签名不符时不得继承 `DONE` 与旧 `result_id`（原先"纠正签名"后照旧继承，
等于把旧块结果挂在新块下面还宣称前缀完整），现降为 `PENDING` 并记 `SIGNATURE_CHANGED`。


## §14 预算、账本与执行入口（S7 定稿）

### 14.1 预留必须有身份

`Reservation` 带 `reservation_id` + `ledger_id`。规矩：

- 释放/结算**按 ID**，不按值相等——两个同额预留在途时，按值删除会误删另一个；
- `settle` 幂等：同一预留结算两次只扣一次（否则一次重放回调让费用翻倍）；
- 跨账本的预留一律拒绝操作。

### 14.2 记账与应用分开去重

`TaskRecord` 有两件事必须分开：**结论应用**（同一条结论只能应用一次）与**费用记账**
（每次尝试都要记）。早先实现把两者合并，第二次真实消耗 12 token 的记录被整条拒收，
账本只剩 10——少算费用比重复应用更难发现。

实现：重复应用时仍追加记录并置 `applied=False`；`totals()["duplicate_applies"]` 可查。

### 14.3 用量重建的字段契约

`rebuild_usage()` 的入参就是 `TaskRecord.to_dict()`，因此 `TaskRecord` **必须**导出
`group`（预算组）。这一处字段错位不会报错，只会静默得到空账——重启后预算就白花了。

### 14.4 覆盖完整性需要预期集合

`count_verdicts` 只有拿到 `expected_candidate_ids` 才能宣布完整：
没有集合时 `expected_known=False`，`integrity_error()` 报"无法核对"，`recheck_status`
只能给 `PARTIAL`。数字能对上不代表没漏候选（重复两行就能凑平）。

### 14.5 反证检索也要可核实

`counter_search.performed=true` 不足以证明"查过"：它必须同时给出
`checked_evidence_ids`，且这些 ID 必须属于本任务。否则判 `INSUFFICIENT`
（`COUNTER_SEARCH_UNVERIFIED`）——"我查了"是声明，不是证据。

### 14.6 执行入口先落盘再前进

`runner.run_recheck` 每完成一个单元就调用一次 `persist`。崩溃后重启时，
"已经花了钱的那几次"必须留在账上，否则预算会被重复花掉。

## §15 发布与结算的四条硬规则（S8 重做，2026-10-31）

复审三指出四条会导致**数据丢失**的封装缺陷。它们共同的性质是：看起来保护了，实际保护不了。

### 15.1 提交是否成立，看持久化状态，不看回调有没有返回

`commit()` 正常返回 **不等于** 已登记，抛错 **也不等于** 没登记——完全可能"manifest 写完、
随后写日志炸了"。因此判定必须读持久化状态（重读 manifest 看有没有这个 id），三态处理：

| 探针结果 | 含义 | 动作 |
| --- | --- | --- |
| `True` | 已登记 | 视为发布成功，**保留文件** |
| `False` | 明确没登记 | 可安全回滚（删文件） |
| `None` | 探针缺失或自己失败 | **保留文件**，记 `kept_for_safety` |

第三条是关键：孤儿可以被 `scan_orphans()` 扫出来回收，**被引用的文件删了就再也拿不回来**。
两类代价不对等，必须偏保守。

### 15.2 结算只认"线程真的结束"

`asyncio.to_thread` 取消 `await` **不会**取消线程。若结算挂在事件循环回调里，取消后再关循环，
回调要么不来、要么带着 `CancelledError` 提前到达，于是"还没跑完的活"被记成已结算。
因此 `on_finish` 放在**工作线程自己的 `finally`** 里执行：与循环是否存活无关。
循环只负责把结果交回等待方，交不回去不影响账目。

### 15.3 结算回调失败必须可见

`on_finish` 自己抛错时，Future 不能永久挂起（等待方只能超时而且不知道为什么）。
现在：记 `settle_error` → 置 `payload["settle_failed"]=True` → 结果照常交回。
调用方据此把该任务记为 `FAILED`，而不是假定"已结算"。

### 15.4 新保护必须接在生产入口上

封装只有接进真实路径才算保护。`results.publish_result` 现在走 `publish_atomically`，
并在失败时抛 `PublishFailed(outcome)`——`outcome.committed=True` 说明数据其实一致（只是流程有异常），
`kept_for_safety=True` 说明文件被保守保留。入口级故障用例见 `backend/tests/test_vnext_publish_faults.py`。

**仍未覆盖**（诚实边界）：进程强杀后的恢复验证需要子进程 + 真中断，本批用异常注入替代，
不能等同于"崩溃注入"。

## §16 结果内容、知识库落盘与指纹闭环（B0）

### 16.1 `extracted` 是定死的 schema，不是自由字典

```json
{
  "facts":       [{"fact_id","text","source":{"chapter_id","chapter_ordinal","start","end","content_hash"},"verification_state"}],
  "characters":  [{"name","state","uncertain","source":{...同上...}}]
}
```

**`source` 必须有**。没有来源的条目在重放时会被丢弃——不是保守，是因为没有它就无法
判断"这条事实属于哪一章"，快照上界（`exclusive_before`）随之失效。这正是 B0 要消灭的
静默降质：库看着有内容，续跑时却按"没有前情"重新分析。

### 16.2 库落盘 `derived/knowledge.json`

- 顶层带 `format_id`/`schema_version`（通用门禁）+ `knowledge_store_version`（库结构版本）
- 必含 `meta`：`source_snapshot_id`、`ordered_result_ids`（按块序，元素是 `result_id`）、
  `store_fingerprint`（整库指纹）
- **写入必须与发布同一临界区**（`project.commit_manifest` 的书锁）：读结果 → 追加 → 写库
  → publish 一次性完成。否则两路并发 publish 会丢事实，而"丢事实"和"没分析过"在界面上
  长得一样。
- 重写成本：单次几十 KB–1MB；>2MB 或 >500 块时改追加日志 + 周期合并（本期未做，阈值写死在此）

### 16.3 指纹链（三层，缺一层就有静默降质）

| 层 | 指纹 | 覆盖什么 | 漏了会怎样 |
| --- | --- | --- | --- |
| 内容 | `result_id` 含 `extracted_fingerprint` | 结果内容 | 同条件重跑原地覆盖，"结果变了"看不见 |
| 上下文 | `PromptWindow.fingerprint()` | 可见事实/人物 + **dropped 计数** + **限额** | 改限额或开始截断，模型看到更少，却判"没变" |
| 依赖 | `expected_dependency(..., context_fingerprint=…)` | 前序结果 + 三版本 + 窗口指纹 | 前面块变了、窗口变了，后面块仍"新鲜" |

`classify(..., context_fingerprints={block_sig: fp})`：期望窗口指纹由调用方（runner）用
**当前库**算出后传入。不传则退化为只比前序结果与三版本——留这个口子是为了扫描预览，
**不能拿它决定是否跳过**。

## §17 分析入口的三条约束（B1b）

**17.1 按块序，失败即停。** `run_analysis` 顺序跑块；某块失败写 `FAILED` 结果后**停止**。继续往后跑没有收益：后续块的期望上下文包含"前序结果"，前序失败 → 依赖断 → 跑出来的结果也用不上，只是多花钱。

**17.2 跳过判定必须带上下文指纹。** 只比块签名会漏掉"前情变了但正文没变"。`classify(strategy=…, context_fingerprints=…)` 的期望值由 runner 用**重放后的库**算窗口指纹得出；算不出来（库损坏/结果缺失）时按"需重算"处理，不猜。

**17.3 库的读-改-写与结果发布在同一把书锁内。** 临界区只做四件事：读库、追加本块记录、写库、发布结果并把 `result_id` 登记进 manifest。**LLM 调用必须在锁外**——否则一本书内所有块串行，等于放弃并发。

**删除的顺序（B2）**：`os.replace` → 摘队列 → `rmtree(ignore_errors=True)`。改名是原子的，一旦成功这本书立刻从列表消失；后续失败只留 `.trash-*` 垃圾，不会留下"没有 manifest 的目录"（那会被判成旧版本目录，界面上再也删不掉）。

## §17 引文定位：模型给原话，程序给位置（D-19）

**背景（实测）**：C2 的 30 条标注里，模型判"支持"20 次、引文逐字命中 9/9，但有 **11 次**
因为自报的 `supporting_span` 对不上而被判 `INVALID`。C3 同类 5/18。**不是判定错，是数不出偏移。**

### 17.1 判据（九条，全部满足才是 SUPPORTED）

见 `docs/PLAN-D19-quote-locate-2026-10-31.md` §10.5，实现即 `resolution.parse_recheck`。
要点：`supporting_span` **不再是必需字段**；位置由程序算，写进 `located_span`。

### 17.2 定位只接受三种匹配级别

`citations.verify_citation` 有六级，但作为**证据门禁**只接受：

| 级别 | 接受 | 理由 |
| --- | --- | --- |
| `EXACT` | ✅ | 逐字命中 |
| `NORMALIZED` | ✅ | 只差空白/全半角 |
| `RELOCATED` | ✅ | 偏移错、引文真——正是要救的那类 |
| `SEGMENTED` | ❌ | 省略号拼引文 |
| `FUZZY` | ❌ | 相似即过，等于关掉闸门 |
| `MISMATCH` | ❌ | 判 `QUOTE_NOT_FOUND` |

定位后必须回验：`EXACT` 严等；其余用**双侧规范化**相等（`_to_raw_offset` 会把夹在中间的
标点带进切片，严等会把合法结果打成失败）。

### 17.3 引文最短 10 字

`MIN_QUOTE_CHARS = 10`。实测语料里「这一日无事」出现 13 次——短句在长段里几乎必然命中，
不设下限等于把闸门交给巧合。

### 17.4 歧义：用 span 指路，指不了就判 INVALID

引文在同一证据里出现 ≥2 次时：

- 模型给了 `supporting_span` 且窗口能指向其中一处 → 采纳，记 `ambiguous=True`；
- 没给或指不到 → `INVALID/AMBIGUOUS_QUOTE`（**不是** `INSUFFICIENT`：后者属 `REUSABLE`，
  `recheck_plan` 会直接复用缓存，歧义会被永久粘住）。

### 17.5 退化模式

只传旧 `quote_within_range(quote, start, end)` 时行为与 D-19 之前完全一致
（`supporting_span` 必填、必须逐字对上、失败码 `QUOTE_OUT_OF_RANGE`）。保留它是为了
让旧调用方不被一次性推翻，但**新代码不该用它**——它拿不到 `evidence_id`。

### 17.6 账本应用键：`entity|candidate|verdict|evidence_id|quote_hash`

**位置不进键**：位置是派生物，模型报错、程序算对之后不该产生第二条应用记录。
**引文哈希要进键**：换了原话就是换了一条论据，必须重新记账。

### 17.7 键名只有一个来源

`resolution.RECHECK_MODEL_KEYS` 是模型回包字段名的唯一定义；`render_recheck_schema()`
渲染 Prompt 里的字段示例；`test_vnext_schema_keys.py` 做**双向核对**（渲染键集合 == 常量；
解析器源码实际读取的键 == 常量）。2026-10-31 的 36 次调用 / 109k token 事故就是
"两边各写一份字段名"造成的。

## §11 锚点字段落地（阶段 0，2026-10-31）

### 11.1 三处字段、一条铁律

`Foreshadowing` 增 `anchor_quote` / `anchor_chapter` / `motif_key`；`AnalysisResult` 增 `anchor_stats`。
铁律：**锚点必须在落盘前校验**（`analyzer` 手上有正文），否则等复检才发现锚点是编的，
这本书的钱已经花完了。

### 11.2 校验规则（`backend/core/anchor.py`）

| 情形 | 处理 |
| --- | --- |
| 逐字命中 | 采用；`anchor_quote` 改写为**原文切片**（不信模型的空白/标点） |
| 仅空白差异 | 规范化后命中，回验通过才采用 |
| 少于 6 字 | 判缺（`MIN_ANCHOR_CHARS`）——太短的句子在任何长段里都命中 |
| 跨章边界 | 判缺——引文跨章说明模型在拼接 |
| 定位不到 | 清空 `anchor_quote`、`anchor_chapter=0`，计 `missing`，**不抛错** |

章归属用**块内标题反查**（`CHAPTER_HEADING_RE`）：块 ≠ 章，把块首章当成引文所在章是错的。
块内无标题时退回"整块算首章"，此时 `anchor_stats` 仍照记，可信度由调用方自行判断。

### 11.3 为什么要 `anchor_stats` 是 dataclass 字段

动态属性不会被 `to_dict` 序列化，重启后统计凭空消失——那正好是"看起来加了保护、其实没有"。
`test_result_roundtrip_anchor.py::test_anchor_stats_is_a_real_dataclass_field_and_roundtrips` 钉住这条。

### 11.4 旧数据兼容

旧结果没有锚点字段：`from_dict` 补默认值（空串/0/空 dict），**不报错、不写迁移脚本**。
仅当 `anchor_quote` 非空才做校验，所以旧产物读出来仍然是"无证据"状态，语义正确。

### 11.5 配置项

`analysis.anchor_quote_enabled`（默认 true）、`anchor_min_chars`（6）、
`recheck_max_calls`（300）、`recheck_max_tokens`（60000）。
`recheck_max_tokens` 单独给足量，是因为实测该模型 `max_tokens` 撞上限时一个字的正文都拿不到。

## §12 账本 v4：锚点与稳定实体键（阶段 1，2026-10-31）

### 12.1 三个新字段

| 字段 | 语义 | 谁写 |
| --- | --- | --- |
| `anchor_quote` | 建成时的**原文逐字引文** | 采集阶段（`analyzer` 校验后）→ catalog → 账本 |
| `anchor_chapter` | 引文实际所在章（程序反查，不是模型说的） | 同上 |
| `entity_key` | "这是同一件事"的稳定身份 | 建条目时一次定下，**此后不改** |

`SCHEMA_VERSION` 3 → 4；旧 v3 账本照常读入，缺字段补默认值，**不迁移、不重写历史**。

### 12.2 为什么 `entity_key` 不能用指纹重算

指纹是"内容变没变"，身份是"是不是同一件事"。用指纹当键会有一个隐性后果：
后续章节补进一条**更早**的线索，指纹变了、键跟着变，同一件事被拆成两条，
一条旧的一条新的——而两条都"看起来有证据"。所以键首次登记即持久，
指纹只用于去重（`test_entity_key_is_stable_when_earlier_clue_arrives` 钉住）。

派生规则：优先模型给的 `motif_key`，没有则退回 `clue:<代表条目 id>`（`anchor.entity_key_for`）。

### 12.3 开关关闭时必须显式标记

`anchor_quote_enabled=false` 时 `anchor_stats` 记 `{"disabled": true, "missing": n}`，
语义是"这一章没有证据"，**不是**"证据只是没找到"。复检阶段必须区别对待：
前者只能产出"证据不足"，不能凭模型自述判回收。

### 12.4 引用校验的单一实现

`backend/core/citations.py` 现在持有唯一实现（`verify_citation` / `_normalize` / `_to_raw_offset` /
`summarize_checks` / 五级匹配常量）。`backend/vnext/citations.py` 改为**转发层**，只保留依赖
`FactRecord`/`SourceRef` 的两个包装（`verify_fact` / `verify_facts_against_book`）。

理由：两套定位语义一旦漂移，同一个引文在一个地方算对、在另一个地方算错，而错误还会被
"两边各自都通过测试"掩盖。校验器是复检的地基，地基只能有一份。

## §13 复检服务层 `recheck_v2`（阶段 2，2026-10-31）

### 13.1 形状

`backend/services/recheck_v2.py`：

```
run_recheck_v2(items, *, book_text, call_model, max_calls=300, batch_size=10, emit=None) -> RecheckOutcome
```

- `items`：账本条目（只处理 `status` 为 `active`/`open` 且**有锚点**的）
- `book_text`：**全文**。回收发生在后文，证据不能只看埋设那一章
- `call_model`：注入的模型调用（异步），便于离线测试与换模型
- 判定核复用 `backend/vnext/resolution.py`：枚举、门禁顺序、覆盖守恒都在那边被三轮复审压过

### 13.2 三条硬边界

| 情形 | 处理 |
| --- | --- |
| 条目没有锚点 | 记 `not_checked`，**绝不**凭模型自述判回收 |
| 锚点在全文里定位不到 | 同上（锚点是采集时校验过的，定位不到说明正文变了） |
| 预算用尽 | 剩余候选全记 `not_checked`，`status != COMPLETED` |

### 13.3 编造引文与"不可用响应"

模型说 `SUPPORTED` 但 `supporting_quote` 在全文里找不到 → `INVALID/QUOTE_NOT_FOUND`，
且 `retryable=True` → 计入 `not_checked`（下一轮重问），**不计入 `invalid`**，
更不会被当成"已核对通过"。这个区分有用：`invalid` 表示"这条候选本身有问题"，
`not_checked` 表示"这次没问出结果"。

### 13.4 两个踩过的坑

1. `parse_recheck` 的 `locate` 回调**第二个参数是 `evidence_id`，不是正文**。传错时每条 SUPPORTED 都变
   `QUOTE_NOT_FOUND`，而测试会"看起来通过了错误的东西"。
2. `extract_json_from_text` 返回的是 **JSON 文本**，不是已解析对象；直接当 dict 用会静默得到空列表。

### 13.5 接进总结流程（开关默认关）

`final_summary.run()` 的阶段 2 改成按 `analysis.recheck_v2_enabled` 分流：

```
if recheck_v2_enabled:  await self._run_recheck_v2(results)
else:                   await self._run_global_foreshadow_recheck(...)
```

开关**只切算法**：账本文件、报告结构、进度事件、接口形状都不变。默认 `false`，
真书验证通过后才考虑改默认——这样万一不对，关掉即回到旧行为，不需要回滚代码。

全文取自 `blocks/*.txt`（按文件名排序），**不用卷摘要**：卷摘要是模型写的概括，
拿它核对"引文是否逐字存在"永远核不出问题，等于没有证据。取不到原文时整轮记
`NOT_RUN`（原因写进 `last_recheck_summary`），绝不用空文本去核对。

`recheck_max_calls` / `recheck_max_tokens` / `foreshadow_recheck_batch_size`
沿用既有配置键；调用走 `client.chat_auto(purpose="foreshadow_recheck_v2")`，
失败由上层既有重试链处理。
