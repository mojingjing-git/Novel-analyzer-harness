# API 参考

后端所有 REST 端点统一挂在 `/api` 前缀下，运行起来之后也可以看 Swagger UI
（`/docs`）。WebSocket 只有一个端点：`/ws/progress`。

路由的注册顺序见 `backend/app.py`（`include_router` 那一段），前端封装在
`frontend/src/api/client.ts`。

## 健康检查

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/health` | GET | 就绪探针 + 依赖体检。`status` 为 `ok` / `degraded` / `critical`，另有 `deps`、`missing`、`critical_missing` |

桌面端用它判断后端是否已就绪（`desktop.py` 轮询这个路径）。

## 分析与队列

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/analysis/start` | POST | 启动队列分析 |
| `/api/analysis/stop` | POST | 优雅停止当前分析 |
| `/api/analysis/status` | GET | 分析状态：运行中 / 队列 / 在途块 |
| `/api/analysis/token_stats` | GET | 分类 token 统计与 KV 缓存命中 |
| `/api/analysis/token_stats/session` | GET | 本次会话累计（分析侧 + 总结侧） |
| `/api/analysis/logs` | GET | 下载 `analyzer.log`。**需要 `X-Session-Token`** |

队列操作与上面共用一组响应体（当前队列快照）：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/queue` | GET / PUT | 读 / 全量替换队列 |
| `/api/queue/scan` | POST | 扫描指定目录下的 `{书名}/blocks` |
| `/api/queue/scan_workspace` | POST | 重新扫描工作区 |
| `/api/queue/remove` | POST | 移出队列（数据进回收站，不删） |
| `/api/queue/move_up` / `/api/queue/move_down` | POST | 调整顺序 |
| `/api/queue/delete_book` | POST | 永久删除一本书 |
| `/api/queue/reset_item` | POST | 把已完成 / 失败项重置为待分析 |
| `/api/queue/clear` | POST | 清空队列 |

## 风格分析

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/style/start` | POST | 启动风格分析（`book_id`、`use_llm`、`limit`） |
| `/api/style/status` | GET | 状态：`running` / `phase` |
| `/api/style/stop` | POST | 停止 |
| `/api/style/result/{book_id}` | GET | 读取 `style.md` |

## 书目

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/books` | GET | 全部书目（id、名称、章节数、是否有报告 / 聚合） |
| `/api/books/register` | POST | 手动注册一本书（不入队） |
| `/api/books/{book_id}/results` | GET | 该书的聚合结果（无聚合文件时实时聚合） |
| `/api/books/{book_id}/latest_chapter` | GET | 已分析的最大章节号 |
| `/api/books/{book_id}/report` | GET | 最终总结报告（Markdown） |
| `/api/books/{book_id}/ledger` | GET | 伏笔账本 |
| `/api/books/{book_id}/foreshadow_ranking` | GET | 伏笔排行（综合分降序） |
| `/api/books/{book_id}/token_stats` | GET | 该书落盘的 token 统计 |
| `/api/books/{book_id}/characters` | GET | 角色卡索引 |
| `/api/books/{book_id}/characters/{character_name}` | GET | 单个角色卡详情 |
| `/api/books/{book_id}/chapter/{chapter}` | GET | 单章分析结果 |

## 最终总结

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/summary/status` | GET | 总结状态：阶段、批次进度、错误、token 统计 |
| `/api/summary/start` | POST | 启动总结（`book_id`、章节范围、`batch_size`、`concurrency`） |
| `/api/summary/stop` | POST | 优雅停止 |

## 可视化

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/viz/timeline/{book_id}` | GET | 时间线：事件 + 伏笔 + 章节 |
| `/api/viz/graph/{book_id}` | GET | 关系图。支持 `chapter_start`、`chapter_end`、`min_edge_weight`、`max_nodes`、`min_node_count` |
| `/api/viz/map/{book_id}` | GET | 地点地图（`needs_normalization` 提示是否需要先归一化） |

## 地点归一化

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/location-normalization/status` | GET | 归一化状态 |
| `/api/location-normalization/start` | POST | 启动归一化 |
| `/api/location-normalization/stop` | POST | 停止 |
| `/api/location-normalization/result/{book_id}` | GET | 归一化摘要（模型、章节指纹、地点数） |

## 切分

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/splitter/preview` | POST | 预览切分结果，不落盘 |
| `/api/splitter/save` | POST | 切分并保存 blocks |
| `/api/splitter/batch` | POST | 批量切分多本到工作区 |
| `/api/splitter/infer_name` | POST | 从文件名推断书名 |

切分参数：`pattern`（正则）、`mode`（`auto` 等）、`use_volume`、`min_words`、
`max_words`、`merge_tiny`、`remove_ads`、`strip_whitespace`。

## 工作区

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/workspace/novels` | GET | 工作区里的书 |
| `/api/workspace/archives` | GET | 归档区里的书 |
| `/api/workspace/archive` | POST | 归档一本（`mode`：`rename` / `skip` / `overwrite`） |
| `/api/workspace/archive_all` | POST | 归档全部 |
| `/api/workspace/delete_archive` | POST | 删除归档 |

## 聚合与导出

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/aggregate/run` | POST | 执行聚合（`include_raw` 控制是否含原始数据） |
| `/api/aggregate/{book_id}/files` | GET | 列出聚合文件 |
| `/api/aggregate/{book_id}/file/{name}` | GET | 读单个聚合文件（带路径穿越防护） |
| `/api/aggregate/{book_id}/excel` | POST | 导出多 Sheet Excel |

## 设置

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/settings` | GET / PUT | 读 / 写配置。运行中 PUT 会被拒绝，需重启生效 |
| `/api/settings/presets` | GET | 厂商预设列表 |
| `/api/settings/models` | GET | 按已保存配置拉取可用模型 |
| `/api/settings/preview/models` | POST | 用未保存的表单试查模型（不写回配置） |
| `/api/settings/probe-thinking` | POST | 探测端点认哪个"关闭思考"参数 |

## 伏笔元数据

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/foreshadow/categories` | GET | 伏笔分类定义、用户保留集、重要度 / 置信度阈值 |

## Prompt 预览

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/prompt/preview` | POST | 按真实配置拼装某块的 system / user 提示词并返回（`max_chars` 可截断） |

## vnext 数据空间

前缀 `/api/vnext`。这一组是 vnext 项目（块、结果、知识库、队列）的 HTTP 面，
错误体统一为 `{"detail": {"code", "message", "path"}}`。

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/vnext/books` | GET | 列出 vnext 项目；被拒目录连原因一起返回 |
| `/api/vnext/books/{book_id}` | GET | 单本摘要：版本、章节数、块进度（done / stale / missing） |
| `/api/vnext/books/{book_id}/blocks` | GET | 块级进度，断点恢复的读取入口 |
| `/api/vnext/projects` | POST | 创建项目。请求体二选一：`{title, text}` 或 `{title, chapters:[{title, content}]}`；成功返回 201 |
| `/api/vnext/books/{book_id}` | DELETE | 删除项目，不可撤销；`confirm` 查询参数必须与 `book_id` 原样一致 |
| `/api/vnext/queue` | GET / POST | 读队列 / 把项目放进队列（只入队，不启动分析） |
| `/api/vnext/queue/{book_id}` | DELETE | 移出队列 |
| `/api/vnext/queue/restore` | POST | 启动后修复：遗留的 `running` 回落 `pending`，并按当前源重算进度（幂等） |

## WebSocket

| 端点 | 方向 | 说明 |
|---|---|---|
| `/ws/progress` | 服务端 → 客户端 | 进度、日志、token、状态机变更的统一推送通道 |

连接时会校验 `Origin`，只允许 localhost / 127.0.0.1（含空 Origin），否则直接关闭
（1008）。服务端每 25 秒发一次 `ping` 心跳。

消息统一线格式：

```json
{
  "type": "log",
  "payload": { }
}
```

### 消息类型

| type | payload 字段 |
|---|---|
| `log` | `level`（info / warn / error）、`text`、`source`（business / python）、`category` |
| `progress` | `current`、`total`、`eta` |
| `block_start` | `chapter`、`range`、`progress`、`total`、`ts` |
| `block_done` | `chapter`、`ok`、`range`、`elapsed?`、`tokens?` |
| `discovery` | `events`、`foreshadows[]`、`characters[]`、`unresolved[]` |
| `token_stats` | `category`、`input_tokens`、`output_tokens`、`current_tokens?`、`source?` |
| `token_delta` | `context`、`session_id`、`unit_idx`、`delta`、`timestamp` |
| `state_change` | `state`、`detail`。`state` 取值：idle / running / stopped / done / summary_* / location_normalization_* |
| `summary_progress` | `status`、`phase`、`batch`、`total_batches`、`batches_done`、`elapsed`、`message` 等 |
| `location_normalization_progress` | `type`、`phase`、`batch_idx`、`error`、`message`、`book_id`、`batches_done` |
| `ping` | 无 payload（心跳） |

## 前后端尚未对齐的字段

以下差异当前存在，写接口时注意以**后端实际返回**为准：

| 端点 | 后端返回 | 前端读取 | 影响 |
|---|---|---|---|
| `POST /api/vnext/projects` | `total_chapters` | `chapter_count` | 页面章节数显示为 undefined |
| `GET /api/vnext/books/{id}/blocks` | `state` | `block.status` | 块状态列为空 |
| `GET /api/vnext/queue` | 条目缺 `total_chapters` 等字段 | 声明了 `total_chapters` | 该字段缺失 |
| `GET /api/style/status` | `{running, phase}` | 声明了 `error` | 错误信息无处显示 |
| `GET /api/vnext/books` 的 rejected 项 | `{path, name, reason, message}` | 声明了 `{path, reason}` | 多余字段被忽略 |

明细见 [issues.md](./issues.md)。
