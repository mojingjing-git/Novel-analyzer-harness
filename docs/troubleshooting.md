# 排障指南

> 排障详细指南。常见问题快速解答见根目录 [README](../README.md)。

## 日志文件

源码模式下以下运行日志均位于 `runtime/` 目录（便携 EXE 模式下位于 EXE 同级目录）：

| 文件 | 大小策略 | 内容 |
|---|---|---|
| `runtime/analyzer.log` | `RotatingFileHandler` (10MB × 3) | 主日志：LLM 调用 / token / 流程 / 错误 |
| `runtime/api_failures.log` | `RotatingFileHandler` (10MB × 3) | LLM 调用失败明细（温度 / 错误类型 / 等待时长，每行一条 JSON 便于 `jq` / `grep`） |
| `runtime/crash.log` | `RotatingFileHandler` (10MB × 3) | 桌面端未捕获异常 / pywebview 原生报错（stderr 走 logging 体系，无句柄共享冲突） |
| `%LOCALAPPDATA%\NovelAnalyzer\run.log` | 不在工程内 | 桌面启动诊断 |
| `%LOCALAPPDATA%\NovelAnalyzer\build.log` | 不在工程内 | 前端构建诊断 |

所有 log 单文件上限 10MB，备份 3 份（即 30MB 总占用上限）。`.log` / `.log.*` 已被 `.gitignore` 排除，不会进库。

## 常见问题

### 分析慢 / 超时

按以下顺序排查：

1. **调大超时**：`api.timeout`（默认 300 秒）→ 600+；`api.summary_timeout`（默认 600 秒）→ 1200+
2. **缩小重试链**：`api.temperature_max_retries` 和 `api.backoff_max_retries` 调小到 1，失败快速放弃而不是死磕
3. **总结页换快模型**：在设置页给"总结专用模型"选个快模型（重型任务专门用 M3 / DeepSeek V3 等）
4. **检查并发数**：`analysis.concurrency` 过高会触发厂商限流，过低浪费带宽，3-5 是常见甜区
5. **看 api_failures.log**：每行 JSON 含 `error_type` / `wait_seconds` / `temperature`，可以统计哪类错误最频繁

### 最终总结卡住

`api_failures.log` 是首选排查点：

```bash
# 统计错误类型分布
cat api_failures.log | jq -r '.error_type' | sort | uniq -c | sort -rn

# 找耗时最长的几次调用
cat api_failures.log | jq -r '"\(.wait_seconds)\t\(.error_type)"' | sort -rn | head -20
```

常见根因：

- **思考型模型无法关闭思考**（M2.7 等）：在设置页给"总结专用模型"换成 M3（`thinking:disabled` 支持）或其他快模型
- **prompt 超过模型上下文**：11 万字符级的总结 prompt 在某些模型上会失败，看 `error_message` 是否含 "context_length_exceeded"
- **网络抖动**：增加 `api.summary_timeout` 到 1200+

### 前端改了不生效

Web 前端的代码在 `frontend/src/`，开发模式 Vite HMR 应该实时生效。如果不生效：

1. **确认是在开发模式**：检查启动方式，`run_desktop.bat` 启动的是生产构建，HMR 不会生效
2. **重新构建生产**：跑 `desktop\build_frontend.bat` 重新生成 `frontend/dist/`
3. **重启桌面端**：`run_desktop.bat` 会从共享盘路径加载构建后的 `dist/` 并启用本地后端副本，dist 变了需要重启
4. **后端配置字段变了**：新加 dataclass 字段后必须重启后端进程，前端才能拿到新 schema

### 桌面端启动报"端口被占用"

`run_desktop.bat` 启动时 uvicorn 找空闲端口（8000-8099），如果全部被占：

1. 关掉占用端口的旧进程：`netstat -ano | findstr :8000` → `taskkill /PID <pid>`
2. 或修改 `desktop/desktop.py` 的 `_find_free_port` 逻辑
3. 或强制指定端口启动 dev 模式后用浏览器访问

### 队列状态异常

队列状态持久化在 `runtime/queue_state.json`，如果出现"卡在运行中"等异常状态：

1. **重启进程**：进程级互斥锁会释放，队列回到空闲
2. **手动清状态**：删除 `runtime/queue_state.json`（会丢未保存的状态）
3. **检查重置端点**：`POST /api/queue/reset_item` 重置单项

### 工作区路径中文 / 网络盘异常

`pywebview` + WebView2 在中文路径 / 网络盘上有渲染限制。详见 [architecture.md](./architecture.md) 的"桌面端双目录方案"：

- 启动时复制到本地副本（`%LOCALAPPDATA%\NovelAnalyzer\app\`）
- 设 `NOVEL_ROOT` 与 `NOVEL_RUNTIME_DIR` 指回原共享盘及 `runtime/` 目录
- 分析结果仍写到原位置 `runtime/workspace/`

如果复制失败，看 `%LOCALAPPDATA%\NovelAnalyzer\run.log` 的 traceback。

### 伏笔账本不对

常见原因：

- **旧书 type 自由式**：没经过 50 类映射，账本字段空。看 `foreshadow_type_map.json` 是否生成
- **休眠判定过激**：最后出现后满 200 章未再出现即标 dormant（`DORMANT_CHAPTER_THRESHOLD`），调 `analysis.foreshadow_kept_categories` 提高保留集
- **复检阶段崩溃**：看 `final_summary_checkpoint/manifest.json` 哪些批次已完成，重启只跑剩下的

### 总结 checkpoint 恢复行为

启动总结时检查 `final_summary_checkpoint/`：

- **卷摘要** 完成 → 复用，不再调 LLM
- **伏笔调和** 完成 → 复用，只补缺的批次
- **伏笔复检** 完成 → 已回收的伏笔不重复付费，账本已落盘
- **batch_size 变更** → 之前的 checkpoint 作废，全量重跑（避免窗口错位）

如果不想复用，直接删 `final_summary_checkpoint/` 目录。

### 鉴权失败（桌面端访问 `/api/analysis/logs`）

桌面端启动时生成 32 字节 token 写入 `session.token` + 环境变量 `NOVEL_ANALYZER_SESSION_TOKEN`，前端 fetch 拦截加 `X-Session-Token` header。

如果 403：

1. 确认前端 `client.ts` 拦截逻辑没被关掉
2. 确认 `session.token` 文件存在
3. 开发模式（`run_dev.bat`）不走这个鉴权，可以直接访问

鉴权由 `backend/api/auth.py` 的 `require_session_token` 依赖实现，校验的是
`X-Session-Token` header 与 `session.token` 文件 / `NOVEL_ANALYZER_SESSION_TOKEN`
环境变量。

## 性能调优速查

| 现象 | 改这个 |
|---|---|
| 章节分析慢 | `api.timeout` ↑ `analysis.concurrency` ↑ |
| 总结慢 | `api.summary_timeout` ↑ 总结模型换快 |
| token 太贵 | 减少 `analysis.concurrency`（让 KV cache 命中串行复用） |
| 伏笔总表太大 | `analysis.foreshadow_min_importance` / `min_confidence` 提到更高等级（取值：高 / 中 / 低） |
| 总结报告卷摘要太长 | `analysis.volume_compress_threshold` ↓ `volume_compress_group` ↑ |
| checkpoint 频繁落盘 | `analysis.checkpoint_interval` ↑（默认 5） |

## 报 issue 前

提 issue 时附上：

1. 复现步骤（书名 / 章节范围 / 操作顺序）
2. 操作系统 + Python 版本 + Node 版本
3. `analyzer.log` 最后 100 行
4. `api_failures.log` 错误类型分布（`jq` 命令见上文）
5. `config.json`（**记得脱敏 api_key**）
