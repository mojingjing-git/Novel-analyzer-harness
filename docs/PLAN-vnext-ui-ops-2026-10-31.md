---
doc: vnext 写操作界面（S10 余项）
date: 2026-10-31
status: reviewed_revised
baseline_commit: 023ff15
scope: 前端 3 个写操作 + 后端 4 个缺口的补齐
---

# PLAN：vnext 写操作界面

## 0. 问题

`/vnext` 页面现在**只读**：能看项目列表、被拒目录、队列。要真正用起来，第一步就卡住——
**没法建项目、没法把项目放进队列、没法看块进度、没法取消**。API 层已有 `POST /api/vnext/projects`
和 `POST /api/vnext/queue/restore`，其余三件事没有入口。

因此这批的目标不是"做一个好看的界面"，而是**让一个新用户从零走到"有块在跑"**，
且每一步失败都能看懂为什么。

## 1. 现状盘点（已核实，非推测）

| 能力 | 后端 | 前端 |
| --- | --- | --- |
| 列项目/被拒目录 | `GET /api/vnext/books` | ✅ `/vnext` |
| 项目详情 | `GET /api/vnext/books/{id}` | ❌ |
| 块进度（done/stale/missing） | `GET /api/vnext/books/{id}/blocks` | ❌ |
| 从原文建项目 | `POST /api/vnext/projects`（201；`chapters` 优先于 `text`；400/404/422 有错误码） | ❌ |
| 读队列 | `GET /api/vnext/queue` | 仅列表暴露在只读页 |
| 重启后校正进度 | `POST /api/vnext/queue/restore` | ❌ |
| 入队 / 取消 / 重跑 | **缺** | ❌ |
| 删除项目 | **缺**（且不该在本批做，见 §6） | ❌ |

## 2. 范围（本批做四件事）

1. **建项目**：输入书名 + 粘贴原文（或选择已切好的章节），调 `POST /projects`，
   成功后跳到该项目详情。空文本、零章节、重名各自给出明确提示。
2. **块进度**：项目详情页展示每块 `done` / `stale:<原因>` / `missing:<原因>`，含块签名前 8 位。
3. **入队**：`POST /api/vnext/queue` 新增——把 `book_id` 放进 `queue_state_vnext.json`，
   返回整条队列。
4. **取消**：`DELETE /api/vnext/queue/{book_id}` 新增——把该条从队列移出（不是"暂停"；
   本版没有运行中的分析进程，取消 = 移出队列，语义要在界面上说清）。

## 3. 后端补齐（两个端点）

- `POST /api/vnext/queue`：body `{book_id}`。校验：项目必须存在且能打开（否则 404/409）；
  已存在则幂等返回 200（不重复插入）。写入走 `queue.save_queue_state`（原子写）。
- `DELETE /api/vnext/queue/{book_id}`：不存在返回 404；存在则移出并返回剩余队列。
- 两个端点都**不**触发任何分析（vnext 尚无分析流水线），响应体里带
  `"analysis_started": false` 与说明字段，避免界面让人误以为"点了就在跑"。

## 4. 前端结构

- `VnextPage.vue` 拆成两块：项目列表（含"新建项目"按钮 → 弹窗）与队列列表（含"入队/移出"）。
- 新增 `frontend/src/pages/VnextProjectPage.vue`（路由 `/vnext/:bookId`）：详情 + 块进度 + 入队。
- 纯函数抽到 `frontend/src/utils/vnextForm.ts`：`normalizeChaptersInput()`（把"每行一章"或
  JSON 数组转成 `chapters`）、`describeBlockStatus()`（`done`/`stale:…` → 中文短句 + 颜色类名）、
  `describeErrorCode()`（把 `MISSING_REQUIRED_FIELD`/`INVALID_FIELD_VALUE`/`REVISION_CONFLICT`
  映射成用户能懂的话）。**抽出来是为了能单测**：这三个函数最容易在边界上出错。
- 所有写操作按钮在请求期间禁用，失败把 `code` 原样显示在按钮下方（不做统一"操作失败"）。

## 5. 测试与验收

- 前端：`vitest` 覆盖上面三个纯函数（目标 12+ 例），含空输入、超长书名、块状态未知值。
- 后端：`test_vnext_queue_endpoints.py`（6 例）：入队成功/重复入队幂等/项目不存在 404/
  移出成功/移出不存在 404/队列文件保持原子写。
- 端到端：`scripts/acceptance_vnext.py` 增一段"HTTP 层建项目 → 入队 → 读块进度 → 移出"。
- 打包验证：`scripts/smoke_portable.py` 增两项（建项目返回 201、队列条目数变化）。

## 6. 不做（本批）

- **删除项目**：破坏性且不可撤销，需要确认流 + 回收站语义，单独一批。
- **运行分析**：vnext 没有分析流水线；界面必须显式写"本版暂不执行分析"，不许让人以为在跑。
- **迁移旧数据**：口径 A 已定，旧 `workspace/` 不读不写。
- **权限/多用户**：本地单用户，session token 现状不变。

## 7. 需要审的四个判断

1. **取消 = 移出队列**是否够用？如果语义应是"暂停并保留进度"，那就得引入状态字段，
   而 vnext 目前没有运行态。
2. **入队是否该顺带校验块可算**（例如 manifest 里 `active_blocks` 为空时拒绝入队）？
   我倾向只校验"项目能打开"，把"有没有块"留给进度页展示。
3. **前端纯函数边界**是否够？`normalizeChaptersInput` 要处理"整篇无章节标记的文本会被切成 1 章"
   这个既有行为——要不要在界面上直接提示。
4. **错误码直显**是否合适？把 `INVALID_FIELD_VALUE` 原样给用户看很难懂，但隐藏成"操作失败"
   又失去可诊断性。我的做法是"中文主文案 + code 小字"。


---

## 8. 审查后的修订（v2，2026-10-31）

外部审查（deepseek，只读）报「需修订」，三处硬错全部成立，已按下表改：

| 审查意见 | 原文错在哪 | 修订后 |
| --- | --- | --- |
| 队列状态是小写 | 计划写 `PENDING/RUNNING/DONE`，代码里是小写 `pending/running/done`（`backend/vnext/queue.py:19-26`） | 入队写 `status="pending"`、`error_message=""`、`updated_at`、`name=manifest.title` |
| 唯一能毁状态文件的操作漏了 | 只写「已存在则幂等 200」，没写**插入前必须 `state.find(book_id)` 判重**（`queue.py:108-115`：重复 book_id 让 `from_dict` 抛错，此后读队列恒 422） | 入队/移出都在 `_QUEUE_LOCK` 内 read-modify-write，且先判重；测试 `test_enqueue_twice_is_idempotent` 直接断言"队列仍可读" |
| code 取不到 | 原计划要求"失败把 code 原样显示"，但 `request()` 只挂 `status` 与 `detail`（`frontend/src/api/client.ts:303-304`），detail 是对象时 message 退化成原始 JSON | 新增 `vnextErrorCode()`/`vnextErrorText()`；界面显示中文主文案 + code 小字 |

审查另查出四处既有缺陷，已一并修掉：

1. **前端字段名全错**：`VnextPage.vue` 读 `book.name`/`book.total_chapters`，后端返回 `title`/`chapter_count`（`backend/services/vnext_service.py:66-71`）——上一版页面书名与章节数其实是空的。已修正类型与模板。
2. **缺 title 返回 404**：语义错（"请求不合法"被说成"资源不存在"）。改为 422 + `INVALID_FIELD_VALUE`；同时新增 `ErrorCode.NOT_FOUND` 供"找不到项目/队列条目"使用（404），两者不再混用。
3. **`RevisionConflict` 漏成 500**：它不是 `VnextFormatError`，routes 只捕后者。新增 `_revision_error()` → 409 + `REVISION_CONFLICT`。
4. **服务层领域错误在 `asyncio.to_thread` 里抛出后没人接**：端点已补 `try/except VnextFormatError → _http_error`，否则 500「服务器错误」。

仍未做（与本批无关，单列）：`create_project` 非事务（中途失败留下无 manifest 的目录，会被 registry 判成 `LEGACY_DIR`）；`book_id`/`dir_name` 直接拼路径未做穿越校验；`request()` 无超时/AbortSignal。这三条写入 §6「不做」清单的下一批。
