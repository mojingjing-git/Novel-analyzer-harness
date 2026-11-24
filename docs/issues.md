# 已知问题与未决取舍

本页只记录**当前仍然成立**的问题。已修掉的不在这里出现。
每条给出判断依据（多数带 file:line），方便复核。

## 一、复检链路

### 复检判定只有一轮可审计数据，判定质量仍未定标

真实语料 16 个块（按标题切出 30 回）上跑 `codex/gpt-6-luna`、批 6、4 并发，共 8 次调用：
**SUPPORTED 19 / INSUFFICIENT 20 / DIFFERENT_ANSWER 1 / INVALID 6**（46 个候选，
`docs/data/MEASURE-CH16-LUNA.json` + `verdicts-20261101-luna.json`）。

19 条 SUPPORTED 逐条审计的结论：

- 0 条引用证据里没给出的章号（答案没跑出证据范围）；
- 独立盲读者（deepseek-v4.1-flash，只看"事项 + 证据段"，不给结论）两轮都认为
  "原文有明确交代"的只有 10 条；但人工复核争议样本时，`fs_013`、`fs_035` 的
  证据里**确实**有交代（如"刘后生疑，拷问寇珠。寇珠怀忠，触阶而死。"），
  是盲读者漏判——**"答不出"不等于"原文没有"**；
- 确认过判 1 条（`fs_040`）：答案写了"计划将展昭诓到陷空岛"，而证据 2877 字里
  没有"陷空岛"、没有"诓"、也没有"资助"。这类"用模型自身剧情记忆补全"的过判
  才是真正要挡的。

因此"SUPPORTED 19 条是不是太积极"目前**没有**充分证据：抽样只抓到 1 条确凿假阳性。
要把这件事变成定标数据，需要人工标注一批（至少 30 条）而不是再换模型重跑。

### 真实长书没有跑通过的基线

三侠五义 64 章、510806 字那一轮，32 次调用全部 usage 未知、`committed_blocks=0`、
知识库未生成——这是一次失败运行（`docs/data/MEASURE-E2E-2026-10-31.json`）。
合成语料（22 章）可以跑通，真实长书不行。

### 带证据复检（V3 / V2）现在怎么跑

阶段 2 默认走带证据复检 V3（`analysis.recheck_version = "v3"`），也可切换为 V2（`"v2"`），只看 `open`/`active` 且带锚点的条目，不受早期全书扫描休眠判死问题影响；判定通道由三件配置决定：

| 配置 | 作用 |
|---|---|
| `analysis.recheck_model` | 判定模型（留空 = 跟随 `api.model`） |
| `analysis.recheck_base_url` | 判定通道（留空 = 跟随 `api.base_url`） |
| `analysis.recheck_reasoning_effort` | 判定思考档位（默认 `low`） |

**判定模型与判定通道必须同源**：网关专用名（如 `codex/gpt-6-luna`）打到官方端点会被
以 `400 unknown model` 拒绝，而流式失败返回的是空回包。两处兜住这件事：

- `recheck_conflict_reason()`（`backend/services/final_summary/parsers.py`）在开跑之前阻断并记
  `PARTIAL` + 原因，不静默降级到主模型；
- 两处 `call_model` 不再吞 `success=False`，失败如实进 `failed_batches`；
- 收尾哨兵：`calls > 0` 但 `checked == 0` 时记 ERROR，并把首个原始回包留在
  明细的 `raw_sample` 里。

配套语义：V2 批大小 `recheck_v2_batch_size`（默认 6）、记账三态（已核对／终态未核对／还没核对，可重试的 `INVALID` 进补问轮）、逐条判定落盘（V3/V2 均写 `output/recheck_verdicts.json`）、"跑完但没全核对上"落 `PARTIAL` 而不是 `RUNNING`。

审计用的脚本：`scripts/audit_recheck_verdicts.py`——章号闭包（0 成本）、
专名闭包统计、盲读者复核、乱序复核。

### 假阳性的主要形态是"答非所问"，三个支点的实测判断

整本实测（《大奉打更人》，208 条 SUPPORTED）之后，三个支点的结论定了：

| 支点 | 实测 | 判断 |
|---|---|---|
| 1 答案实体闭包 | "答案引用证据窗口外章号"命中 11/208（5.3%），**人工逐条确认 11/11 都是答非所问** | **值得做**，0 成本、命中即人工复核。但它是"相关性"的代理指标，不是定义 |
| 2 盲读者复核 | 抽样 60 条：两轮都确认 8 条、两轮都否认 51 条；人工核 5 条否认样本，**4 条确为过判、1 条是漏判** | **不宜直接当自动降级门禁**（精确率约 80%，会误伤真 SUPPORTED）。弱于判定模型的复核方尤其危险——"答不出"≠"原文没有" |
| 3 扰动稳定性 | 乱序重问会让一部分输出直接变空，混了"格式稳定性"和"语义稳定性"两个变量 | 现有实验不足以证明语义不稳；要做得控制变量（同序重采样）|

真正要挡的是**答非所问**：模型拿着证据窗口里的一段话去回答另一个问题。它不是编造，
引文是真的，所以词面相似度、专名闭包都抓不住；章号越界只是它的副产品。

要全自动判别，可行的形态是：**同款或更强模型做盲答复核**（复核方不弱于判定方），
把"判定方说已回收、复核方说没交代"降级为待复核；以及 0 成本的章号闭包做前置筛。
这两条都还没有实现，等决策。

### 43 条无锚点的旧回收无法自动复审

`llm_judged` 的已回收条目里 43 条没有可用锚点引文，证据复检进不去，
只能等锚点迁移（D-1 的 `anchor_file_index`/`anchor_content_hash`）或通过人工复核页面处置。

## 二、数据层语义

以下几条在生产路径上仍然成立：

| 问题 | 位置 | 表现 |
|---|---|---|
| 滚动摘要按标量上界过滤 | `backend/core/pipeline.py` | 补跑历史块时通过提示词标注避免 `current_context` 倒退，但推进进度仍受单向最大章号标量约束 |
| 已验证事实没有来源 | `backend/core/memory_state.py` | `verified_facts` 直接原样入账，无法溯源 |
| 同章共现直接写成关系 | `backend/core/memory_state.py` | 逐章阶段同一章出现即判定两人有关系（全书图谱在 Stage 4 另行洗出语义关系） |
| 知识库没有版本字段 | `backend/models/knowledge.py:14-31` | 无法判断快照是哪个版本的规则生成的 |

## 三、总结与产物

| 问题 | 位置 | 表现 |
|---|---|---|
| 卷摘要压缩是纯字符截断 | `backend/services/final_summary/checkpoint_mixin.py` | 截断会切在半句上，摘要读起来断裂 |

## 四、前端

| 问题 | 位置 | 表现 |
|---|---|---|
| 设置保存是全量覆盖，没有版本号或乐观锁 | `frontend/src/pages/SettingsPage.vue`、`backend/api/routes_settings.py` | 两个窗口同时改配置，后写的整份覆盖前一份（写盘本身已是原子写） |

请求超时已经处理（`frontend/src/api/client.ts` 的 30 秒默认超时 + 重活 120–300 秒），总结与分析进度均通过 `/ws/progress` 实时推送。

### 桌面端下载日志可能 403

`GET /api/analysis/logs` 受 `X-Session-Token` 保护（`backend/api/auth.py:21`）。前端已在
`client.ts` 里注入该 header，但 `backend/api/auth.py:30-34` 的注释仍写着"桌面端
pywebview 调受保护端点会被 403"。这一条**未实机验证**：桌面端是否仍会 403 需要装包
后确认。

## 五、覆盖范围

- 充分验证过的只有中文长篇。英文与多语言混合文本只做过切分可靠性验证，没有端到端实测。
- 桌面端只在 Windows 上验证，macOS / Linux 未做桌面端打包。
- 前端测试覆盖组件与逻辑，端到端（真实后端联调）没有覆盖。
- 输出 JSON 的 schema 仍在演化，跨版本升级后依赖旧字段的脚本需要适配。

## 六、已知取舍

**密钥脱敏是 fail-open 的。** 可能含 key 的错误日志走 `llm_failure_logger.redact()`，
`app.py` 的 `_SensitiveFieldsFilter` 对落盘 `analyzer.log` 的记录做二次过滤；但 redact
抛异常时返回原文——宁可漏脱敏也不丢日志。新增错误字段若不走 redact，就可能漏脱敏。
这是本地单用户工具的取舍：日志可用性优先于绝对不泄露。需要更强保证时应改 fail-close。

**`api.max_retries` 是死配置。** 它只用于失败日志里标注"第 n/m 次尝试"，不参与任何
重试判定；真正的重试由 `temperature_max_retries` 与 `backoff_max_retries` 控制。
配置项清单见 [configuration.md](./configuration.md)。

**`gui.language` 目前没有消费点。** 后端只是把它存进 `config.json`，不影响任何行为。

## 七、待复核项

以下是怀疑存在、但尚未在当前代码上复现确认的问题。复现后应移入上面的正式条目
或直接删除。

| 现象 | 怀疑点 |
|---|---|
| `llm_client` 在拿不到 usage 时把消耗记为 0 或 `None`，使 token 统计失真 | usage 未知时的上报路径 |
| 风格分析快照在并发下存在边界不一致 | 快照写入与读取的时序 |
