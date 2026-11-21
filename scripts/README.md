# Scripts 目录说明

本目录存放测试编排、工具、打捞验收与基线测量脚本。

## 1. 核心测试与工程工具

| 脚本 | 用途 |
|---|---|
| `run_tests.py` | 全仓统一测试入口（后端 `pytest` + 前端 `vitest`，支持 `--no-frontend` 与 pytest 参数透传） |
| `sync_models_dev.py` | 同步上游 models.dev 模型能力与定价目录到 `backend/data/models_dev.json` |
| `smoke_portable.py` | 便携桌面运行环境冒烟验证 |
| `e2e_real_book.py` | 真实小说端到端联调脚本 |

## 2. 伏笔打捞与自动验收

| 脚本 | 用途 |
|---|---|
| `run_salvage.py` | 伏笔打捞两段式管线（蒸馏匹配 + 固定窗门禁） |
| `acceptance_gate.py` | 打捞结果自动验收器（机械增量 + NLI 回路） |
| `acceptance_vnext.py` | vnext 验收入口 |
| `audit_recheck_verdicts.py` | 复检判词审计（章号闭包 / 盲读者 / 乱序） |
| `run_recheck_audit.py` | 旧回收审计脚本 |
| `check_anchor_identity.py` | 锚点物理单元定位四面计数 |
| `verify_recheck_fixtures.py` | 复检固定基线用例验证 |

## 3. 基线测量脚本

- `measure_c1_pipeline.py`、`measure_c2_annotations.py`、`measure_c3_sampling.py`
- `measure_full_book.py`、`measure_prompt_cost.py`
- `measure_recheck_v2.py`、`measure_recheck_v2_pipeline.py`
- `probe_recheck_one.py`、`probe_recheck_transport.py`、`profile_recheck_build.py`

## 4. 归档实验脚本（`experiments/bc/`）

`experiments/bc/` 集中收纳跨作品 B/C 评测与生产管线收尾实验脚本（40 个脚本，含 `bc_*`、`agent_probe.py`、`compare_pilot.py`），供回溯评测口径与运行专项契约测试使用。
