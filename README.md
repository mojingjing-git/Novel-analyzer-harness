# Novel Analyzer

Novel Analyzer 是一个本地运行的长篇中文小说分析工具。它读取 txt / docx 文件，按章节切分，调用 OpenAI 兼容 API 分析每一章，生成全书总结、伏笔账本、角色卡、时间线、关系图和 Excel 等结果。需要自备 API Key。当前主要在 Windows 上验证，也可以前后端分离运行。

技术栈：FastAPI + Vue 3 + pywebview，兼容任何 OpenAI 兼容 API（M2.7 / DeepSeek / 智谱 / 火山 / 硅基流动 / LM Studio / Ollama）。

<!-- 截图位置：docs/screenshot-main.png（待补充） -->

**项目状态**：实验性项目，接口和输出格式可能变化。

## 功能

- 切分 txt / docx：识别常见章节和卷格式，清理广告，输出 blocks/
- 逐章分析：提取事件、人物变化、伏笔、地点等信息
- 全书总结：生成最终报告、伏笔审计、写作风格分析
- 可视化：时间线、角色关系图、地点地图、角色卡
- 导出：聚合 JSON 和 Excel
- 断点续跑：分析中断后可从已完成部分继续
- 桌面模式：Windows 下可双击 run_desktop.bat 启动
- 兼容 OpenAI API：支持 M2.7、DeepSeek、智谱、火山、硅基流动、LM Studio、Ollama 等

## 安装

环境要求：

- Python 3.12+（开发验证于 3.13）
- Node.js 18+（仅前端构建需要）
- 一个 OpenAI 兼容的 LLM API Key

步骤：

```bash
pip install -r requirements.txt
cd frontend
npm install
npm run build
```

或直接双击 `build_frontend.bat`（在本地磁盘构建后自动把 dist 同步回共享盘，规避 esbuild 网络盘限制）。

## 配置

启动后在设置页填写 `Base URL` / `API Key` / `模型`。预设多家厂商的BaseURL。

API Key 也支持环境变量 `LLM_API_KEY` / `MINIMAX_API_KEY` 注入，优先级高于配置文件。

工作区默认在仓库根目录下 `workspace/`，可通过环境变量 `NOVEL_ROOT` 改到其他位置（桌面端网络盘场景必须用）。运行中改 `config.json` 不生效，需要重启进程。

完整字段参考见 [docs/configuration.md](./docs/configuration.md)。

## 使用

桌面模式（推荐，Windows）：

```bash
run_desktop.bat
```

pywebview 自动找空闲端口、起 uvicorn、轮询健康检查、打开 1600×900 原生窗口。`run_desktop.bat` 用 `pythonw` 静默启动，无控制台 / tail 窗口；运行日志在应用内日志面板查看，文件日志完整落盘。中文 / 网络盘路径的 WebView2 限制通过"本地运行时副本 + `NOVEL_ROOT` 指回共享盘"解决。

开发模式（前后端热重载）：

```bash
run_dev.bat
# 或手动
python -m uvicorn backend.app:app --reload --port 8000   # 后端 http://localhost:8000/docs
cd frontend && npm run dev                                 # 前端 http://localhost:5173
```

5 步流程：

1. 切分页：导入 txt 或 docx，自动识别章节和卷，清理广告，批量切分到 workspace/《书名》/blocks/
2. 分析队列页：扫描工作区入队，开始分析（实时进度 / 日志 / token 统计）
3. 最终总结页：选书，跑 4 阶段总结（卷摘要、伏笔调和、风格分析、最终报告），看报告、审计、伏笔账本
4. 可视化页：时间线、关系图、地图、角色卡
5. 统计面板：token 消耗、缓存命中率、重试成本

### 关键产物

- **伏笔账本**（`foreshadow_ledger.json`）：全书所有伏笔的状态机，状态分 `active` / `resolved` / `dormant` 三种
- **最终总结报告**（`final_summary_report.md`）：卷摘要 + 伏笔上下文 + 风格分析
- **写作风格**（`style.md`）：22 项统计指标 + LLM 8 维语义特征
- **聚合 JSON**（`aggregated/`）：人物、事件、地点、关系、伏笔等 11 类汇总

Web UI 顶部 5 个 Tab 对应上面的 5 步流程。所有操作也可以通过 [API](./docs/api.md) 直接调。

## 输出文件

`workspace/《书名》/output/` 下的主要文件（每本书一个子目录）：

| 文件 | 内容 |
|---|---|
| `chapter_N_result.json` | 逐章分析结果（含原始响应） |
| `novel_analysis_aggregated.json` | 全书聚合 |
| `final_summary_report.md` | 最终总结报告 |
| `foreshadow_audit.md` | 伏笔审计报告 |
| `foreshadow_ledger.json` | 伏笔账本（active / resolved / dormant） |
| `style.md` | 写作风格分析 |
| `aggregated/` | 11 类聚合 JSON |
| `final_summary_checkpoint/` | 总结断点（卷摘要 / 调和 / 清单） |

`workspace/分析结果/《书名》/` 是归档区，跑完的书可以归档到这里长期保存，源工作区清理后仍可回看。归档与恢复见 API `/api/workspace/*` 端点。

## 下载（v0.1.0）

从 [GitHub Releases](https://github.com/mojingjing-git/novel-analyzer/releases) 下载最新 `novel-analyzer.exe`（约 26 MB），双击即可运行。

**使用流程**：
1. 把 `novel-analyzer.exe` 放到任何可写目录（推荐 `D:\tools\novel-analyzer\`，避免 Program Files 权限问题）
2. 双击运行
3. 首次启动会自动创建：
   - `workspace/` 目录（含 README.md 欢迎页）
   - `config.json`（模板来自 `config.example.json`，已嵌入 exe）
4. 应用启动后会自动跳到**设置页**，填入 API Key 即可使用
5. 把你的 `.txt` 或 `.docx` 小说文件放到 `workspace/<书名>/` 下面
6. 主界面选书 → 启动分析 → 等几小时 → 看伏笔账本 + 最终报告

**Win10 用户首次启动**：可能提示"未检测到 WebView2 Runtime"。到 [Microsoft Edge WebView2](https://developer.microsoft.com/microsoft-edge/webview2/) 下载「Evergreen Standalone Installer」（约 100MB），安装一次后即可。Win11 预装 WebView2，无需额外操作。

**所有数据都在 EXE 同级目录**（不走 `%APPDATA%`，不写 C 盘）：
- `workspace/` — 你的小说
- `config.json` — 配置
- `analyzer.log` / `crash.log` / `api_failures.log` — 日志
- `queue_state.json` / `session.token` / `app.lock` — 运行时状态

**删除整个文件夹即彻底清理**。详细打包文档见 [docs/build.md](./docs/build.md)。

## 已知限制

- 目前只有支持中文长篇小说充分测试。英文 / 多语言混合小说仅进行切分可靠性验证，没有完整实测
- 主要在 Windows 上验证，macOS / Linux 桌面端未充分测试
- 需要自备 API Key
- 长书（百万字以上）单次跑完可能需要数小时
- 前端构建依赖 Node.js 18+，纯 Python 用户需要先装 Node（**v0.1.0+ 用 exe 不需要**）
- 输出 JSON 字段 schema 还在演化，跨版本升级后旧脚本可能需要适配
- 测试以 Python 后端为主，前端缺少端到端覆盖
- 商用 LLM（按 token 计费）跑百万字长书成本可观，跑前最好先试读几章估 token
- 完整分析跑完后，磁盘占用大致是原 txt 的 3-5 倍（聚合 + checkpoint + 日志）
- 不同厂商对 thinking_mode 协议支持不一样，详见 [docs/configuration.md](./docs/configuration.md) 的"api 段"
- v0.1.0 PyInstaller exe 未做代码签名，杀软可能误报（添加信任即可）

## 常见问题

- **分析慢或超时**：调大 `api.timeout` / `api.summary_timeout`，或换快模型
- **最终总结卡住**：查看 `api_failures.log` 的错误类型分布，或在设置页给总结换快模型
- **前端改了不生效**：运行 `build_frontend.bat` 重新构建 dist，再重启应用

更多排障内容见 [docs/troubleshooting.md](./docs/troubleshooting.md)。

## 开发

```bash
python -m pytest        # 默认收集 backend/tests + tests（以 pytest --collect-only 实时输出为准）
```

测试覆盖：LLM 重试链、JSON 容错、模型序列化、队列状态机、总结 checkpoint 断点续跑、知识库快照隔离、伏笔去重等。

修改后端后重启进程生效；修改前端后跑 `npm run build` 重新生成 `frontend/dist/`。新加 `APIConfig` / `AnalysisConfig` dataclass 字段后，前端 `AppConfigDto` 必须同步补类型。

详细架构说明见 [docs/architecture.md](./docs/architecture.md)，API 端点见 [docs/api.md](./docs/api.md)。

## 贡献

提交 issue 或 PR 前，请先运行 `python -m pytest` 确保现有测试通过。文档改动不需要跑测试。完整开发约定见 [agent.md](./agent.md)。

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。Copyright 2026 mojingjing-git。

允许自由商用、修改、分发（含再许可），需保留版权声明。完整条款见仓库根目录 `LICENSE` 文件。

## 致谢

- [FastAPI](https://fastapi.tiangolo.com/) / [Vue 3](https://vuejs.org/) / [pywebview](https://pywebview.flowrl.com/)
- [openpyxl](https://openpyxl.readthedocs.io/) / [jieba](https://github.com/fxsjy/jieba) / [Pydantic](https://docs.pydantic.dev/)
- 所有 OpenAI 兼容 API 提供商
- 所有 issue 与 PR 贡献者
