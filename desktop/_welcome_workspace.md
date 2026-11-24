# 工作空间

把你的 **.txt 小说文件** 放在这里，然后启动应用分析。

## 目录结构

- `<workspace>/<书名>/` — 单本书的工作目录
- `<workspace>/<书名>/blocks/` — 切分后的章节文件（启动分析后生成）
- `<workspace>/<书名>/output/` — 分析产物（章节结果、伏笔账本、Excel、风格报告等）

## 第一次跑

1. 把 `xxx.txt` 放进 `workspace/你的书名/`
2. 启动应用 → 主界面会看到这本书
3. 选书 → 点"启动分析" → 等几小时（百万字约 4-8 小时，取决于 LLM 速度）
4. 分析完成后看「伏笔账本」+「最终报告」+ Excel 导出

## 关闭后再次启动

- 进度已落盘，**自动断点续跑**——上次跑到哪这次继续
- 想从头开始？删 `workspace/<书名>/output/` 整个目录

## 故障排查

- **界面空白** → 检查 `../config.json` 是否填了 API Key
- **某章失败** → 看 `../analyzer.log` 末尾，找 `chapter_N` 相关
- **重启后无法续跑** → 检查 `workspace/<书名>/output/chapter_*.json` 是否齐全
- **WebView2 缺失**（Win10 部分用户）→ 下载安装 https://developer.microsoft.com/microsoft-edge/webview2/

## 文件位置

| 文件 | 位置 |
|---|---|
| 配置文件 | `../config.json`（EXE 同级目录） |
| 分析日志 | `../analyzer.log` |
| 崩溃日志 | `../crash.log` |
| 队列状态 | `../queue_state.json` |
| 单实例锁 | `../app.lock` |

所有数据都在 EXE 同级目录——删除整个文件夹即彻底清理。
