# 文档导航

本目录是 Novel Analyzer 的文档入口。所有文档只描述**当前状态**：有什么、为什么这样、
怎么用。变更历史由 git 记录，不在文档里。

## 文档清单

| 文档 | 内容 |
|---|---|
| [architecture.md](./architecture.md) | 系统结构、数据流、流水线与各子系统的职责边界 |
| [api.md](./api.md) | 全部 HTTP 端点与 WebSocket 消息协议 |
| [configuration.md](./configuration.md) | `config.json` 全部配置项的默认值与语义 |
| [troubleshooting.md](./troubleshooting.md) | 日志位置、常见故障的排查路径与调优建议 |
| [build.md](./build.md) | PyInstaller 打包流程、产物布局与打包期问题 |
| [data-contract-vnext.md](./data-contract-vnext.md) | vnext 数据空间的语义契约（格式、指纹、裁决、账本） |
| [data-contract-2.0.md](./data-contract-2.0.md) | 2.0 管线（1.x 作业层 + vnext 数据层合并）的形态与缺口清单 |
| [issues.md](./issues.md) | 已知缺陷与未做的取舍 |
| [baseline-data.md](./baseline-data.md) | 质量与性能基线数据（来自 `data/` 下的测量结果） |
| [data/](./data/) | 不可再生的测量数据：标注集与各任务实测结果（JSON） |

项目总入口与快速上手见仓库根目录 [README.md](../README.md)；面向自动化协作的约束与命令见
根目录 [agent.md](../agent.md)。

## 阅读顺序建议

- **第一次用**：根 README → [configuration.md](./configuration.md)（填 Key）→ [troubleshooting.md](./troubleshooting.md)
- **要改后端**：根 [agent.md](../agent.md) → [architecture.md](./architecture.md) → [data-contract-vnext.md](./data-contract-vnext.md)
- **要参与 2.0 合并**：[data-contract-2.0.md](./data-contract-2.0.md)（形态与缺口清单）→ [data-contract-vnext.md](./data-contract-vnext.md)
- **要接前端或写脚本**：[api.md](./api.md)
- **要打包发布**：[build.md](./build.md)
- **想知道现在做到什么程度、哪里还不稳**：[issues.md](./issues.md) + [baseline-data.md](./baseline-data.md)

## 文档约定

- 只写现状，不写改动记录；"为什么这么设计"只保留仍然成立的决策。
- 数值、默认值、端点、文件路径一律以代码为准；代码与文档冲突时改文档。
- 过程类材料（计划、复审、测量流水账）不入库，留在 git 历史里。
