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
| `/api/analysis/usage` | GET | 跨进程持久化全局用量账本与费用估算 |
| `/api/analysis/usage/clear` | POST | 清零全局用量账本（自动备份 `.bak`）。**需要 `X-Session-Token`** |
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

## 书目

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/books` | GET | 全部书目（id、名称、章节数、是否有报告 / 聚合 / 语义图谱 `has_atlas`） |
| `/api/books/register` | POST | 手动注册一本书（不入队） |
| `/api/books/{book_id}/results` | GET | 该书的聚合结果（经 `ensure_aggregated_cache` 指纹透明刷新 `output/aggregated/`） |
| `/api/books/{book_id}/style_profile` | GET | Stage 3 落盘的 `style_profile.json` 量化与语义画像（未生成时返回 `200 exists=false`） |
| `/api/books/{book_id}/recheck_verdicts` | GET | 读取落盘的 `recheck_verdicts.json` 复检明细，并叠加账本实时状态字段（`ledger_status` / `ledger_needs_review` / `ledger_resolution_quality` / `ledger_source_type` / `ledger_resolution` / `ledger_resolved_chapter`；兼容 V3/V2，未生成返回 `200 exists=false`） |
| `/api/books/{book_id}/latest_chapter` | GET | 已分析的最大章节号 |
| `/api/books/{book_id}/report` | GET | 最终总结报告（Markdown） |
| `/api/books/{book_id}/ledger` | GET | 伏笔账本 |
| `/api/books/{book_id}/foreshadow_ranking` | GET | 伏笔排行（综合分降序，附每条 `source_type` 与全书 `lifecycle_bars`） |
| `/api/books/{book_id}/ledger/{item_id}/review` | POST | 人工复核覆写伏笔账本条目（需 `X-Session-Token`；`action` 取 `confirm_resolved` / `reject_to_active` / `mark_dormant`，写入 `source_type="human_override"` 并刷新 `lifecycle_bars`；运行期持 `.book.lock`，冲突返回 `409`） |
| `/api/books/{book_id}/ledger/batch_review` | POST | 原子批量复核覆写伏笔账本条目（需 `X-Session-Token`；请求体 `{"items": [{"item_id", "action", "resolution"?, "resolved_chapter"?, "note"?}]}`，任一条校验失败则整批回滚并返回 `400` / `404`；持 `.book.lock` 防并发冲突，冲突返回 `409`） |
| `/api/books/{book_id}/token_stats` | GET | 该书落盘的 token 统计 |
| `/api/books/{book_id}/characters` | GET | 人物谱系索引（自动触发指纹聚合缓存与 `story_atlas.json` 马甲归并） |
| `/api/books/{book_id}/characters/{character_name}` | GET | 单个角色卡详情（支持别名直查并自动合并别名下的弧光与事件） |
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
| `/api/viz/atlas/{book_id}` | GET | 全书语义图谱（`story_atlas.json`：核心人物关系网 + 换地图阶段流 + 多世界树；缺失返回 `exists=false`） |

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
| `/api/workspace/restore_blocks` | POST | 从归档目录仅还原 `blocks/` 切分并自动入队（支持去时间戳命名与冲突校验） |

## 聚合与导出

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/aggregate/run` | POST | 手动触发聚合（`include_raw` 控制是否含原始数据） |
| `/api/aggregate/{book_id}/files` | GET | 列出聚合文件（自动跳过 `_` 前缀内部缓存元数据） |
| `/api/aggregate/{book_id}/file/{name}` | GET | 读单个聚合文件（带路径穿越防护） |
| `/api/aggregate/{book_id}/excel` | POST | 自动刷新指纹聚合缓存并导出多 Sheet Excel |

## 设置

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/settings` | GET / PUT / PATCH | 读 / 全量替换 / 局部更新配置。运行中受并发与阶段写护栏限制 |
| `/api/settings/schema` | GET | 返回全量 131 项配置元模型、11 个 UI 分组、默认值与校验约束 |
| `/api/settings/profiles` | GET | 列出 6 个配置方案槽位（3 个内置通用预设 + 3 个用户自定义槽）及当前激活状态（需 `X-Session-Token`，响应脱敏不返回明文密钥） |
| `/api/settings/profiles/{slot_id}` | PUT / DELETE | 存入或更新指定用户自定义槽位（`user_1..3`），或清空该槽位（需 `X-Session-Token`；内置槽返回 `400`） |
| `/api/settings/profiles/{slot_id}/apply` | POST | 一键切换指定槽位到当前生效配置，支持 `128k` / `256k` / `512k` 短别名（需 `X-Session-Token`；运行中返回 `409`） |
| `/api/settings/presets` | GET | 厂商预设列表 |
| `/api/settings/models` | GET | 按已保存配置拉取可用模型 |
| `/api/settings/preview/models` | POST | 用未保存的表单试查模型（不写回配置） |
| `/api/settings/probe-thinking` | POST | 探测端点认哪个"关闭思考"参数 |
| `/api/settings/probe-recheck` | POST | 探测复检判定通道可用性与思考控制参数 |
| `/api/settings/model-spec` | GET | 查询 models.dev 中指定模型的上下文、思考能力与费率规格 |
| `/api/settings/models/sync` | POST | 从上游 models.dev 同步最新模型能力与定价目录 |

## 伏笔元数据

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/foreshadow/categories` | GET | 伏笔分类定义、用户保留集、重要度 / 置信度阈值 |

## Prompt 预览

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/prompt/preview` | POST | 按真实配置拼装某块的 system / user 提示词并返回（`max_chars` 可截断） |

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
| `state_change` | `state`、`detail`。`state` 取值：idle / running / stopped / done / summary_* |
| `summary_progress` | `status`、`phase`、`batch`、`total_batches`、`batches_done`、`elapsed`、`message` 等 |
| `ping` | 无 payload（心跳） |
