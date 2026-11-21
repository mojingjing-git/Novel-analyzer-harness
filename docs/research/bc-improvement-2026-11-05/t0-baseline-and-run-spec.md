# M0 / T0 基线与运行规格冻结清单（2026-11-05）

本文件对应 [BC改进任务书.md](file:///F:/AI/01_项目/小说分析器/docs/BC改进任务书.md) 中的 **T0｜冻结基线与评测范围（M0，P0）** 交付物。机读规格见同目录 [t0-baseline-and-run-spec.json](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/t0-baseline-and-run-spec.json)，曝光登记表见 [t0-exposure-registry.json](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/t0-exposure-registry.json)，语料计划见 [corpus-plan.json](file:///F:/AI/01_项目/小说分析器/docs/research/bc-improvement-2026-11-05/corpus-plan.json)。

## 1. 基线版本与核心哈希

- **Git HEAD**: `68e1292900bca3250e26c213cdac5cf26aad4b19`
- **Tier 0 基线测试**: `580/580 passed`
- **冻结语料 Manifest ID**: `54e4061f92b95dce68d02dd570df26767949c0feb658ac4907f1a57263118592`
- **语料 Bundle 路径**: `workspace/bc-evaluation/bc-improvement-20261105/corpus`（已执行 `freeze`，`novel_text_exported = false`）

### 核心代码与提示词 SHA-256

| 模块 / 提示词 | 路径 / 标识 | SHA-256 |
|---|---|---|
| 章节提取分析器 | [backend/core/analyzer.py](file:///F:/AI/01_项目/小说分析器/backend/core/analyzer.py) | `c4dbdb10f8da0c001344ec7868647c82931fb1acc333644b5cd26f3160244f3d` |
| 分析流水线 | [backend/core/pipeline.py](file:///F:/AI/01_项目/小说分析器/backend/core/pipeline.py) | `17489ce1806b3792cf80f76a7bc5d27c4c4e1ee8e512c077f2b133db5efa1b35` |
| 提示词构建器 | [backend/core/prompt_builder.py](file:///F:/AI/01_项目/小说分析器/backend/core/prompt_builder.py) | `ca52c82207d9464bbc7e169152b87089e80264b72f470da880a39c3e254fbc63` |
| 知识库与记忆裁剪 | [backend/core/knowledge_base.py](file:///F:/AI/01_项目/小说分析器/backend/core/knowledge_base.py) | `cabd44580037afd01ba5be9efe3d8a3de5aaa4d25a2f02a03f845c65ed1d4197` |
| 原文证据检索 | [backend/core/evidence.py](file:///F:/AI/01_项目/小说分析器/backend/core/evidence.py) | `d47ffdaee1516e0fd09d1f0ef2c48face71bbaa37951d55364f69a9b2c0f3fa0` |
| 锚点与章节跨度 | [backend/core/anchor.py](file:///F:/AI/01_项目/小说分析器/backend/core/anchor.py) | `a60194ce1c9103bf450820ecd408f2d5ff69b8dbb94f20027e4dbef71c9cc6cb` |
| 总结与伏笔总表 | [backend/services/final_summary.py](file:///F:/AI/01_项目/小说分析器/backend/services/final_summary.py) | `293624a4a7388a4fe3a02b87ef47f45ccd30f93bb81437dfbede0959ac79a7f7` |
| 伏笔复检 V2 | [backend/services/recheck_v2.py](file:///F:/AI/01_项目/小说分析器/backend/services/recheck_v2.py) | `6e77fdd6645bbe1f09c6ff36230f6572a37b4825503ea4f03f2a209229875fe5` |
| 伏笔去重工具 | [backend/utils/text_utils.py](file:///F:/AI/01_项目/小说分析器/backend/utils/text_utils.py) | `9a959de96b9063a3d1c4c832850125be4f5fc8b0c42f6729ea30e7d2d39d3520` |
| 复检裁决核 | [backend/vnext/resolution.py](file:///F:/AI/01_项目/小说分析器/backend/vnext/resolution.py) | `01b065a6983c97a06d75542def0d75dedc0ae3cb5d83f74b37feda7496fc9aa7` |
| 声明级证据核验 V2 | [backend/common/claim_evidence_v2.py](file:///F:/AI/01_项目/小说分析器/backend/common/claim_evidence_v2.py) | `77e7c2884a53f2358a8f91773ec4c8b40a32adf5890f1d99078bb0c436eee7d5` |
| `PROMPT_TEMPLATE` | `recheck_v2.PROMPT_TEMPLATE` | `9ea347987201b4832d7b68904adbcfa362e8396154b55bf4dda86e11084e4377` |
| `ITEM_TEMPLATE` | `recheck_v2.ITEM_TEMPLATE` | `81371c37ba9e1b440792585ad1d70cfa438807e1cd090b7ac3752edecc6bc206` |

## 2. 实际运行模型与网关参数（不含密钥）

- **章节采集模型 (`api`)**:
  - `base_url`: `http://127.0.0.1:3425/v1`
  - `model`: `zcode/GLM-5.3-Flash`
  - `temperature`: `0.1`, `max_tokens`: `65536`, `thinking_mode`: `{"reasoning_effort": "low"}`
  - `block_size`: `4`
- **复检与验收模型 (`analysis.recheck_*`)**:
  - `recheck_base_url`: `http://127.0.0.1:3425/v1`
  - `recheck_model`: `minimax-cn/MiniMax-M3.1-Flash`
  - `recheck_reasoning_effort`: `"low"`, `recheck_max_tokens`: `32768`, `recheck_v2_batch_size`: `8`

## 3. 作品分层与曝光隔离表

### 3.1 历史已曝光作品（共 11 部，严禁选入留出集）

1. **生产/调试工作区作品（3 部）**：`道诡异仙`、`大奉打更人`、`三侠五义`。
   - 用途限制：仅用于 T1 存量遗漏回放分析、T3 存量对照及 T4 候选抽样辅助；严禁重切分生产目录，严禁将其定向样本外推为全书准确率。
2. **前序跨作品评测已曝光/已消费作品（8 部）**：`大国重工`、`超级能源强国`、`全职高手`、`王者时刻`、`回到过去变成猫`、`我家老婆来自一千年前`、`唐砖`、`绍宋`。
   - 用途限制：前序验证集与已开启的留出集均已登记为 `previously_exposed_titles`，本轮不再充当未见验证或留出集。

### 3.2 本轮新选未曝光作品（共 8 部，覆盖 4 个预设题材组）

抽样参数在读取任何正文前统一固定为 `seed="bc-improvement-20261105-v1"`, `windows_per_work=4`, `window_chars=12000`（每部 4 个等距分层确定性窗口、每窗口 12,000 字，足以容纳跨章埋设与回收对照）：

| Work ID | 作品名称 | 划分 (`split`) | 预设题材组 (`topic_hint`) | 编码 | 字符数 | 源文件 SHA-256 | 初始状态 |
|---|---|---|---|---|---:|---|---|
| `ind_dev` | 《学霸的黑科技系统》 | `development` | `industrial_scifi` | `gb18030` | 5,045,926 | `012045f0dd022ee2...` | `metadata_only` |
| `game_dev` | 《亏成首富从游戏开始》 | `development` | `gaming_esports` | `gb18030` | 6,019,913 | `b96ce9a036fa3911...` | `metadata_only` |
| `urban_dev` | 《大医凌然》 | `development` | `urban_daily` | `gb18030` | 3,875,405 | `017d0a381c1096b9...` | `metadata_only` |
| `hist_dev` | 《秦吏》 | `development` | `history_court` | `gb18030` | 3,685,230 | `24e4c745ae5f4d17...` | `metadata_only` |
| `ind_holdout` | 《超神机械师》 | `holdout` | `industrial_scifi` | `gb18030` | 5,680,476 | `d25d4b65a198c1b6...` | `metadata_only` |
| `game_holdout` | 《网游之近战法师》 | `holdout` | `gaming_esports` | `gb18030` | 3,850,542 | `57980df4affcd640...` | `metadata_only` |
| `urban_holdout` | 《我真没想重生啊》 | `holdout` | `urban_daily` | `gb18030` | 3,422,137 | `7647dfae4dcad291...` | `metadata_only` |
| `hist_holdout` | 《宰执天下》 | `holdout` | `history_court` | `utf-16` | 7,889,601 | `437e191022e1a5c7...` | `metadata_only` |

## 4. 调用预算分配与硬护栏

- **全局硬上限**：逻辑调用数 $\le 300$ 次，包含重试的实际请求尝试数 $\le 400$ 次，最大并发数 $\le 2$。
- **阶段预算切分**：
  1. **M0 / T0（基线冻结）**：`0` 次调用（已完成）。
  2. **M1 / T1 + T2（遗漏追踪与开发集原文优先评测集）**：预留 $\le 48$ 次逻辑调用（存量回放 `0` 调用；4 部开发集抽样窗口基线采集与复检 $\le 48$ 次）。
  3. **M2 / T3（B 类主要根因修复与开发集对照）**：预留 $\le 64$ 次逻辑调用（同窗口修复前后配对对照）。
  4. **M3 / T4（C 类按需审核评估）**：预留 $\le 96$ 次逻辑调用（在 $\ge 80$ 条真实候选上对比 `current`、全量 `claims_v2`、按需 `claims_v2`）。
  5. **M4（留出集一次性终验）**：预留 $\le 80$ 次逻辑调用（4 部留出集在代码、提示词、参数、盲读参考标签全部冻结后一次性运行），余 `12` 次作为安全缓冲。

## 5. 锁定验收口径表

本轮严格锁定 [BC改进任务书.md](file:///F:/AI/01_项目/小说分析器/docs/BC改进任务书.md) 第四节全部指标口径：
- **B1 遗漏可定位率**：$\ge 90\%$ 遗漏样本可定位到具体阶段与原因码；关闭追踪开关时业务结果零变化。
- **B2 评测代表性**：留出集 $\ge 4$ 部作品、覆盖 $\ge 4$ 个预设题材组、$\ge 80$ 条可裁决参考观察（每部 $\ge 10$ 条），分清**登记项**与**回答项**，先盲读原文并冻结标签后再看被测输出。
- **B3 遗漏修复有效性**：召回提升以无支持误通过不增加为前提；至少 $75\%$ 被测作品持平或改善；单作品召回退化 $\le 5$ 个百分点。
- **B4 成本与稳定性**：同口径对比下总调用或总用时增幅 $\le 25\% \sim 30\%$；可重试/格式失败率 $\le 5\%$。
- **C1 按需审核有效性**：在 $\ge 80$ 条真实候选上，误放行率不高于全量 `claims_v2` 超过 $3$ 个百分点，且 `claims_v2` 调用次数相对全量下降 $\ge 40\%$；单独报告“未触发但实际误通过”条数。
