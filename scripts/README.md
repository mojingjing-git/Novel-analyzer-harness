# Scripts 目录说明

本目录存放测试编排、工具、打捞验收与基线测量脚本；调用模型或配置时统一读取 `runtime/config.json`（由 `backend.config.paths.runtime_path` 解析）。

## 1. 核心测试与工程工具

| 脚本 | 用途 |
|---|---|
| `run_tests.py` | 全仓统一测试入口（后端 `pytest` + 前端 `vitest`，支持 `--no-frontend` 与 pytest 参数透传） |
| `sync_models_dev.py` | 同步上游 models.dev 模型能力与定价目录到 `backend/data/models_dev.json` |
| `smoke_portable.py` | 便携桌面运行环境冒烟验证 |

## 2. 伏笔打捞与自动验收

| 脚本 | 用途 |
|---|---|
| `run_salvage.py` | 伏笔打捞两段式管线（蒸馏匹配 + 固定窗门禁） |
| `acceptance_gate.py` | 打捞结果自动验收器（机械增量 + NLI 回路） |
| `audit_recheck_verdicts.py` | 复检判词审计（章号闭包 / 盲读者 / 乱序） |
| `run_recheck_audit.py` | 旧回收审计脚本 |
| `check_anchor_identity.py` | 锚点物理单元定位四面计数 |
| `verify_recheck_fixtures.py` | 复检固定基线用例验证 |

## 3. 基线测量脚本

- `measure_prompt_cost.py`
- `measure_recheck_v2.py`、`measure_recheck_v2_pipeline.py`
- `probe_recheck_one.py`、`probe_recheck_transport.py`、`profile_recheck_build.py`
