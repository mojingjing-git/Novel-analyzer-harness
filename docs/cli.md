# 命令行工具（CLI）

Novel Analyzer 提供无头命令行入口 `python -m backend.cli`，面向自动化脚本与外部模型（Agent）执行书目检索、原文切片提取、伏笔账本审校、流水线运行与配置诊断。

## 启动与全局选项

```bash
python -m backend.cli [全局选项] <命令> [子命令与参数]
```

全局选项放在子命令前后均等价生效：

| 选项 | 说明 |
|---|---|
| `--runtime-dir PATH` | 显式指定运行目录（默认读取 `paths.get_runtime_dir()`） |
| `--book-dir PATH` | 显式指定单本书目目录（含 `blocks/` 或 `output/`）；指定后需双定位参数的子命令可省略书名 |
| `--config PATH` | 显式指定配置文件路径（优先级高于 `<runtime_dir>/config.json`；文件不存在报退出码 `3`） |
| `-o` / `--set KEY=VALUE` | 在内存中临时覆盖任意配置项（支持点路径与唯一短键名；可重复且跨父/子命令累积生效，不写回磁盘） |
| `--server URL` | 显式指定运行中的桌面或 Web 后端地址 |
| `--token TOKEN` | 显式提供 `X-Session-Token` 鉴权口令 |
| `--offline` | 禁用活体服务代理，直接执行本地加锁读写 |
| `--json` | 在 `stdout` 输出结构化纯 JSON（包括参数解析失败与运行时异常） |
| `--progress-jsonl` | 在 `stderr` 输出单行紧凑 NDJSON 实时进度事件 |
| `-v` / `--verbose` | 将 `stderr` 日志级别提高到 `DEBUG` |
| `-q` / `--quiet` | 静默非 JSON 常规文本输出，`stderr` 仅保留错误日志 |

## 退出码与输出契约

### 退出码

| 退出码 | 含义 | 常见触发场景 |
|---|---|---|
| `0` | 成功 | 命令正常完成 |
| `1` | 运行失败 | 未预期异常、远端 HTTP `5xx`、独立复检返回空结果 |
| `2` | 用法或配置错误 | 参数缺失、动作值非法、书名越界、批量输入 JSON 非法、`config.json` 损坏 |
| `3` | 资源不存在 | 书目、章节、角色、报告、账本或目标伏笔条目缺失 |
| `4` | 并发冲突 | `.book.lock` 被持有、后端队列或总结任务运行中、`--server` 书目目录不匹配、未使用 `--force` 覆盖已有 `blocks/` |
| `5` | 通道熔断 | LLM 账户级熔断或复检通道连续熔断 |
| `130` | 用户中断 | 收到 `SIGINT` / `SIGTERM` 后协作退出 |

### `stdout` / `stderr` 隔离规则

- `stdout` 在 `--json` 模式下仅输出单一 JSON 对象：
  - 成功：`{"ok": true, "data": ...}`
  - 失败：`{"ok": false, "error": {"type": "...", "message": "...", "details": {...}}}`
- 运行期会将注册在根 logger 与子 logger 上指向 `stdout` 的 `StreamHandler` 自动重定向到 `stderr`。
- `--progress-jsonl` 向 `stderr` 按行输出 `{"event": "...", ...}` 事件流；分析进度事件会自动剥离体积庞大的 `result` 字段，保持实时事件流紧凑可控。

## 子命令参考

### 书目只读查询与导出（`books`）

| 命令 | 核心参数 | 说明 |
|---|---|---|
| `books list` | 无 | 列出工作区与队列中已发现的全部书目及产物状态 |
| `books status [BOOK]` | `[BOOK]` | 汇总章节数、分析块数、伏笔七栏统计、复检概况与 Token 统计 |
| `books chapter [BOOK] CHAPTER` | `--view {text,analysis,both}` `--offset` `--limit` | 读取物理分章原文窗口（兼容补零文件名）及分析结果（兼容历史合块归档） |
| `books search [BOOK] QUERY` | `--from-chapter` `--to-chapter` `--max-hits` `--radius` | 先按章号范围过滤，再对物理分章执行有界上下文窗口字面检索 |
| `books characters [BOOK]` | `--name` `--offset` `--limit` | 透明更新指纹聚合缓存并返回角色摘要列表或指定角色卡详情 |
| `books report [BOOK]` | `--offset` `--limit` `--style` | 读取 `final_summary_report.md`（支持字符分片）及可选风格画像 |
| `books export [BOOK]` | `--output PATH` | 更新聚合缓存后原子导出 `.xlsx` 工作簿 |

### 伏笔账本与复检裁决（`ledger`）

| 命令 | 核心参数 | 说明 |
|---|---|---|
| `ledger list [BOOK]` | `--status` `--needs-review` / `--no-needs-review` `--importance` `--source-type` `--sort` `--offset` `--limit` | 按条件筛选排序列出伏笔条目，附全书七栏 `lifecycle_bars` |
| `ledger show [BOOK] ITEM_ID` | `--with-evidence` `--radius` | 查看单条伏笔明细与复检裁决；开启 `--with-evidence` 后提取埋设锚点窗（优先复用 `locate_records`，回退有限字面定位）与回收证据链原文窗 |
| `ledger verdicts [BOOK]` | `--tier` `--verdict` `--needs-review` `--gate-demoted` `--offset` `--limit` | 查看 V2/V3 逐条复检明细，叠加实时账本字段 |
| `ledger review [BOOK] ITEM_ID` | `--action {confirm_resolved,reject_to_active,mark_dormant}` `--resolution` `--resolved-chapter` `--note` | 对单条伏笔应用人工/模型覆写（`source_type="human_override"`，`needs_review=False`） |
| `ledger batch-review [BOOK]` | `--file PATH` | 从 JSON 文件或 `-`（`stdin`，兼容 UTF-8 BOM）读取列表或 `{"items": [...]}` 并原子批量覆写；任一条校验失败则整批回滚 |

### 切分、流水线与独立复检

| 命令 | 核心参数 | 说明 |
|---|---|---|
| `split FILE` | `--name` `--workspace` `--pattern` `--mode` `--use-volume`/`--no-use-volume` `--min-words` `--merge-tiny` `--max-words` `--remove-ads` `--strip-whitespace` `--dry-run` `--preview-count` `--force` | 切分整本小说 TXT；`--dry-run` 仅预览不写盘；严格拦截书名路径穿越与越界目录 |
| `analyze [BOOK]` | `--start` `--end` `--chapters` `--force` `--concurrency` `--block-size` `--model` `--reasoning-effort` `--temperature` `--update-rolling` | 运行逐章批量分析或 `--chapters SPEC` 定点章节重跑；定点重跑默认不刷新 `rolling_summary` 且保留既有全书 `token_stats.json` |
| `summary [BOOK]` | `--start` `--end` `--batch-size` `--concurrency` `--recheck-version {v2,v3}` `--model` | 运行全书最终总结与四阶段报告生成并落盘 `output/summary_token_stats.json` |
| `recheck [BOOK]` | `--version {v2,v3}` `--concurrency` `--max-calls` `--v2-batch-size` `--gray-u4`/`--no-gray-u4` `--mid-single-hop`/`--no-mid-single-hop` `--model` `--from-baseline` `--include-dormant` | 独立重跑 Stage 2 伏笔复检；保留既有 `batch_size_snapshot` 与 `human_override`，成功后清除陈旧 `stage2_recheck_state.json` |

### 配置与诊断（`config`）

| 命令 | 核心参数 | 说明 |
|---|---|---|
| `config show` | 无 | 输出当前生效配置（含 `--config` 与 `--set` 叠加视图），自动脱敏凭据与 URL 口令；配置损坏返回退出码 `2` |
| `config health` | 无 | 返回运行目录、目标配置文件、配置合法性、关键依赖就绪状态及活体后端探测摘要 |
| `config schema` | `--group` `--search` | 列出全量 131 项配置元模型、分组、类型与取值范围，支持按分组与关键词筛选 |
| `config get KEY` | `--include-credentials` | 读取单个配置项的当前生效值与默认值元数据（敏感项默认脱敏） |
| `config set KEY=VALUE ...` | 若干 `KEY=VALUE` | 校验并持久化修改一个或多个配置项；活体后端在线且未使用 `--config`/`--offline` 时走 HTTP `PATCH /api/settings` |
| `config patch` | `--file PATH_OR_DASH` | 从 JSON 文件或 `-`（`stdin`）应用局部嵌套补丁；校验未知键并走 `PATCH /api/settings` 或原子写盘 |
| `config reset` | `[KEYS ...] \| --all` `--include-credentials` | 将指定键或全量配置重置为默认值；`--all` 默认保留端点与密钥凭据，显式 `--include-credentials` 时一并清空 |
| `config presets` | 无 | 列出内置 API 厂商预设（密钥字段脱敏） |
| `config profile list` | 无 | 列出 6 个配置方案槽位（3 个内置通用预设 + 3 个用户自定义槽）及当前激活状态 |
| `config profile apply SLOT` | `SLOT` | 一键切换指定槽位到当前配置（支持 `128k`/`256k`/`512k` 别名；响应配置字段自动脱敏） |
| `config profile save SLOT` | `SLOT` `--name` `--description` `--include-credentials` | 将当前配置（可叠加 `-o/--set`）存入用户槽 `user_1..3`；在线叠加先拉取服务端配置再合成，环境变量密钥永不落盘 |
| `config profile clear SLOT` | `SLOT` | 清空指定用户自定义槽位（`user_1..3`） |

## 并发锁与活体后端代理

1. **跨进程文件锁（`.book.lock`）**：所有本地写命令（`split` 正式写入、`analyze`、`summary`、`recheck`、`ledger review` / `batch-review`）及 Web API 审校路由共享非阻塞书目文件锁。其他进程持锁时立即失败并返回退出码 `4`（HTTP `409`）。
2. **写任务互斥探针**：`split` 正式写入、`analyze`、`summary` 与 `recheck` 在未开启 `--offline` 时会探测活体后端 `/api/analysis/status` 与 `/api/summary/status`，存在运行中任务或状态未知时拒绝并发写入。
3. **审校代理与规范路径校验**：未开启 `--offline` 时，`ledger review` 与 `ledger batch-review` 按 `--server` 或 `<runtime_dir>/app.lock` 探测活体后端，并比对 `/api/books` 返回目录与本地目标 `book_dir` 规范路径：
   - 路径一致时，自动携带 `--token`、环境变量 `NOVEL_ANALYZER_SESSION_TOKEN` 或回环服务对应 `<runtime_dir>/session.token` 通过 HTTP 接口写入。
   - 由 `app.lock` 自动发现且书目目录不一致时，自动回退本地加锁写入；显式指定 `--server` 且目录不一致时返回退出码 `4`。

## 外部模型审校示例

```bash
# 1. 拉取待确认且重要度高的伏笔列表
python -m backend.cli ledger list 《示例小说》 --needs-review --sort score --json

# 2. 按条目调取设伏原文窗与复检证据链上下文窗
python -m backend.cli ledger show 《示例小说》 fs_014 --with-evidence --radius 280 --json

# 3. 如有需要，按物理章号追加检索或分页查看正文章节
python -m backend.cli books search 《示例小说》 青莲玉佩 --from-chapter 10 --to-chapter 80 --json
python -m backend.cli books chapter 《示例小说》 42 --view text --offset 0 --limit 4000 --json

# 4. 将模型裁决结果以 JSON 通过标准输入原子写回账本
python -m backend.cli ledger batch-review 《示例小说》 --file - --json << 'EOF'
[
  {
    "item_id": "fs_014",
    "action": "confirm_resolved",
    "resolution": "第42章明确揭示玉佩归属与持有人身份",
    "resolved_chapter": 42,
    "note": "核对原文后确认闭环"
  }
]
EOF
```

## 凭据安全约定

- `config show` 与异常消息统一经过凭据脱敏器处理，屏蔽 `sk-*`、Bearer Token、`X-Session-Token`、查询参数密钥及 URL 中的 `user:pass@`。
- 非回环自定义 `--server` 不会自动读取本地 `<runtime_dir>/session.token`，需显式传入 `--token` 或设置 `NOVEL_ANALYZER_SESSION_TOKEN`。
