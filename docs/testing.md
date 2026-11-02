# 测试体系

本项目测试分 **5 档**（T0–T4，累积式：T0 ⊂ T1 ⊂ T2 ⊂ T3 ⊂ T4），统一由
`scripts/run_tests.py` 入口按验证强度选用。规则与清单集中在仓库根 `pytest_tiers.py`。

## 档位定义

| 档 | 内容 | 用例数 | 耗时（实测） | 典型场景 |
|---|---|---|---|---|
| T0 | 冒烟：backend 全模块自动导入（`test_smoke_imports.py`）+ 核心纯函数（JSON 容错、错误分类、伏笔账本、脱敏、编码等 21 个清单文件） | 561 | ~12s | 每改几行代码后的即时自检 |
| T1 | T0 + 默认档单测（`backend/tests/` 未列名文件，实测均 <1s） | 1698 | ~25s | 改完一个功能点 |
| T2 | T1 + 中速文件（0.9–2.5s：切分静默失败、隔离自检、生命周期矩阵、mock 流水线批处理）+ 顶层 `tests/`（切分边界 + Gutenberg 语料基线） | 1783 | ~53s 串行 / ~30s 并行 | 日常提交前的门禁 |
| T3 | T2 + 慢速文件（`test_streaming.py` 53s、`test_llm_mock.py` 21s，真实重试/退避链路）＝ pytest 全量 | 1823 | ~127s 串行 / ~90s 并行 | 宣布完成、提交推送 |
| T4 | T3 + 前端 vitest（33 个测试文件）＝ 全仓验证 | — | ~2min | 大重构、发布、跨栈改动 |

用例数以 `python scripts/run_tests.py --list-tiers` 实时为准；耗时随机器浮动。

## 命令

```bash
python scripts/run_tests.py --tier 1        # 按档位跑（累积式，含更低档）
python scripts/run_tests.py                 # 无参数 = T4 全仓验证
python scripts/run_tests.py --related       # 只跑与本次 git 改动相关的文件
python scripts/run_tests.py --related --tier 2
python scripts/run_tests.py --list-tiers    # 查看各档文件/用例分布
python scripts/run_tests.py --serial        # 串行（复现并行不稳时用）
python scripts/run_tests.py -- -k recheck   # 透传 pytest 参数
```

不带 `--tier` 直接跑 `python -m pytest` 等价于 T3（全量）；`python -m pytest --tier 2`
可直接按档过滤（选项由根 `conftest.py` 注册）。小档（T0/T1）默认串行——xdist 每个
worker 都要做一遍隔离安装与全量收集，用例少时并行反而慢（实测 T0 串行 12s vs 并行 42s）。

## 文件 → 档位规则

`pytest_tiers.py` 按 `SLOW_FILES`(T3) → `MEDIUM_FILES`(T2) → 顶层 `tests/`(T2) →
`SMOKE_FILES`(T0) 的顺序匹配，未命中一律落默认档 T1：

- **慢/中速清单是白名单**：只有实测超过 1s 的文件才进 `MEDIUM_FILES`。
- **新测试文件自动落 T1**，不需要登记；实测超过 1s 的文件必须移进 `MEDIUM_FILES`，
  否则 T1 会被悄悄拖慢。
- `backend/tests/test_tier_assignment.py` 守护清单：条目指向的文件必须存在、三张清单
  互斥、档位语义不被改动。清单改名后先跑 T0 即可发现死条目。

## 底层机制

- **写前隔离**（根 `conftest.py` + `pytest_isolation.py`）：任何 `backend.*` import 之前
  把 cwd 切到会话级隔离根，模块顶层实例化的日志/队列/配置文件全部落在临时目录；
  每条用例 autouse fixture 前后断言隔离完好并重置运行时单例。测试代码路径一律按
  `__file__` 绝对定位，不依赖 cwd。
- **SSL context 缓存**（`pytest_ssl_cache.py`）：httpx 每次建客户端要 3.8s 的
  `create_default_context`，缓存后全量测试从 ~750s 压到 ~110s。
- **对账**（`run_tests.py`）：跑前 `--collect-only` 数预期用例数、跑后核对 passed 数，
  防止"过滤条件写错漏掉整份文件却报全绿"。日志按 PID 落
  `.tmp_pytest_run_<pid>.log`，多个实例并发互不污染。
- **前端测试**：Vitest + @vue/test-utils + jsdom，`cd frontend && npm run test`，
  由 T4 自动编排（`--no-frontend` 可跳过）。

## 其他入口

- `scripts/pytest_related.py`：改动相关性选择的实现，`run_tests.py --related` 复用其
  `select()`；直接运行仍可独立使用（`--run` 执行，`--base` 指定比较基准）。
- `scripts/pytest_split.py -n 8`：按文件切组、各起独立进程的备选并行方案，不依赖
  xdist 插件（121–142s）。

## 维护约定

- 提交/推送前必须过一遍 T3 或 T4（`run_tests.py` 无参数即 T4），并在提交信息或
  PR 描述里写明跑的是哪一档。
- 测试数量以 `pytest --collect-only -q` 与 `npm run test:coverage` 实时为准。
- dev 依赖（pytest-xdist / pytest-timeout / pytest-cov）见 `requirements-dev.txt`，
  用 `uv pip install -r requirements-dev.txt` 安装（本仓库 .venv 由 uv 管理）。
