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

### 解析失败的判定被记成"未核对"，也不再补问

`backend/vnext/resolution.py` 的 `cached_as_terminal` 对任何 `INVALID` 都返回 False，
于是 `count_verdicts`（`:649`）把这类行计入 `not_checked`，`invalid` 与
`invalid_by_reason` 恒为 0。16 块实测 46 条里有 6 条是 `INVALID`
（4 条 `FOREIGN_ID`、2 条 `QUOTE_NOT_FOUND`），报告上全写成"未核对"，看不出
是"模型答错了格式"还是"根本没查"。

同一个原因还让它们不会被补问：`backend/services/recheck_v2.py:311-325` 的 `_consume`
只要拿到 payload 就把候选记为 done（`retried_batches=0`），而 `FOREIGN_ID`
（模型引用了别条候选的 evidence_id）本来最可能通过单独补问救回来。

### 复检提示词曾经贴着上下文上限

16 章那轮 12 次调用全部被上下文长度拒绝（`prompt is too long: 100001 tokens > 100000`），
60 个候选一个都没核对上——原因是测量脚本只把**整本正文**传进 `book_text`，
一个候选的证据包就是全书，批 6 条等于把同一本书塞进提示词六遍。

走生产路径（`chapters=` 有界取窗）后同一批数据单候选提示词约 2.8k–4.7k 字，
8 次调用即跑完，不再撞上限。这条现在**不再是缺陷**，留在这里是因为"证据包按候选裁剪"
正是它当年暴露出来的方向。

### 真实长书没有跑通过的基线

三侠五义 64 章、510806 字那一轮，32 次调用全部 usage 未知、`committed_blocks=0`、
知识库未生成——这是一次失败运行（`docs/data/MEASURE-E2E-2026-10-31.json`）。
合成语料（22 章）可以跑通，真实长书不行。

### 复检提示词曾经贴着上下文上限

见上一节：根因是测量脚本只传整本正文，不是证据包设计本身。有界取窗上线后，同一批
数据单候选提示词约 2.8k–4.7k 字，不再有 `CONTEXT_LENGTH` 拒绝。

### 休眠判定让全书复检永远启动不了

现有账本里 dormant 占多数、active 为 0，而
`backend/services/final_summary.py:1488` 在"没有 active 伏笔"时直接返回——休眠判死
事实上让全书复检不会开始。新的带证据复检（`backend/services/recheck_v2.py`）默认关闭
（`backend/config/settings.py:187` 的 `recheck_v2_enabled=False`），所以**当前实际生效
的仍是旧的无阻断路径**。

vnext 侧的 `backend/vnext/foreshadow.py` 用 `cutoff_view(cutoff_ordinal=)` 取代了休眠
判死，但 vnext 还没有接入生产路由（见下文"vnext 未接线"）。

### 账户级熔断两条管线都没有

欠费或账号整体不可用时，整轮任务会继续往下派发、继续烧钱。vnext 里
`backend/vnext/breaker.py` 已经按"只认结构化的账户级错误 + 粘性跳闸"写好，但**没有任何
调用方**；1.x 侧只有 `backend/core/pipeline.py:755` 登记致命块供补跑剔除，不做熔断。

## 二、数据层语义

以下几条在 1.x 生产路径上仍然成立，且互相牵连：1.x 的滚动摘要与人物状态只有一个
`last_updated_chapter` 标量，没有"快照上界排他"的概念，因此**越早补跑的块既进不了
摘要，又会被后面高章号的结果覆盖**。三者应一起改，不宜分次修。

| 问题 | 位置 | 表现 |
|---|---|---|
| 滚动摘要按标量上界过滤 | `backend/core/pipeline.py:609`、`:918-941` | 补跑的旧块被排除在摘要之外 |
| 人物状态覆写不受顺序保护 | `backend/core/memory_state.py:386`（`:328-341` 只护住 timeline） | 后到的高章号状态盖掉先到的 |
| 已验证事实没有来源 | `backend/core/memory_state.py:369-372` | `verified_facts` 直接原样入账，无法溯源 |
| 同章共现直接写成关系 | `backend/core/memory_state.py:388-394` | 同一章出现就判定两人有关系 |
| 知识库没有版本字段 | `backend/models/knowledge.py:14-31` | 无法判断快照是哪个版本的规则生成的 |
| 知识库两个 dict 不裁剪 | `backend/core/memory_state.py:409-418` | `_trim_kb` 只裁 7 个 list，两个 dict 无上限 |

vnext 侧对应能力都有（`exclusive_before` 快照、有序归约、`SourceRef` 溯源、共现与语义
关系分表、快照策略版本），但见"vnext 未接线"。

## 三、总结与产物

| 问题 | 位置 | 表现 |
|---|---|---|
| 总结 checkpoint 只覆盖阶段 1 | `backend/services/final_summary.py:1117-1233` | 阶段 2/3/4 中断后要整体重跑 |
| 卷摘要压缩是纯字符截断 | `backend/services/final_summary.py:1105` | 截断会切在半句上，摘要读起来断裂 |
| 聚合的 10 个子产物不是原子写 | `backend/utils/aggregate_utils.py`（约 10 处 `open(...,'w')+json.dump`） | 主聚合文件已原子化，子产物不是；写到一半中断会留下半个 JSON |
| 提示词上限有 4 项没接配置 | `backend/core/analyzer.py:73-81` 只传了 7 个上限 | `max_verified_facts` / `max_relationships` / `max_themes` / `max_content_chars` 无法配置 |
| 上下文裁剪来源有两套 | `backend/core/memory_state.py:414` 读配置 vs `backend/core/knowledge_base.py:309` 硬编码 | 同一条上限在不同路径下生效值不同 |

## 四、前端

| 问题 | 位置 | 表现 |
|---|---|---|
| 自研 Markdown 渲染器 + `v-html` | `frontend/src/pages/SummaryPage.vue:391`、`frontend/src/utils/markdown.ts` | 报告内容里的 HTML 不做转义，报告来自模型输出 |
| 设置保存是全量覆盖，没有版本号或乐观锁 | `frontend/src/pages/SummaryPage.vue:106-111`、`backend/api/routes_settings.py:104` | 两个窗口同时改配置，后写的整份覆盖前一份（写盘本身已是原子写） |

请求超时已经处理（`frontend/src/api/client.ts` 的 30 秒默认超时 + 重活 120–300 秒），
但总结状态仍是 2 秒轮询（`frontend/src/pages/SummaryPage.vue:200`）。

### vnext 页面与后端字段名不一致

vnext 前端页面按一套字段名读数据，后端返回的是另一套，页面上直接显示为空。

| 端点 | 后端返回 | 前端读取 | 页面表现 |
|---|---|---|---|
| `POST /api/vnext/projects` | `total_chapters`（`backend/vnext/vnext_service.py:297`） | `chapter_count`（`frontend/src/pages/VnextPage.vue:154`） | 显示"已创建《X》（undefined 章）" |
| `GET /api/vnext/books/{id}/blocks` | `state`（取值 `done` / `stale:<原因>` / `missing:<原因>`） | `block.status`（`frontend/src/pages/VnextDetailPage.vue:30-38`） | 块状态列为空 |
| `GET /api/vnext/queue` | 条目缺 `total_chapters` / `block_size` / `updated_at`（`backend/api/routes_vnext.py:92-105` 手工构造） | 声明了 `total_chapters`（`frontend/src/api/client.ts:364`） | 该字段缺失；POST / DELETE 路径的同一队列是有这些字段的 |

另两处较小的口径差异：`GET /api/style/status` 只返回 `{running, phase}`，前端类型声明了
`error`；`GET /api/vnext/books` 的 rejected 项多返回 `name` / `message`，前端只声明
`path` / `reason`（后者不影响显示）。

### 桌面端下载日志可能 403

`GET /api/analysis/logs` 受 `X-Session-Token` 保护（`backend/api/auth.py:21`）。前端已在
`client.ts` 里注入该 header，但 `backend/api/auth.py:30-34` 的注释仍写着"桌面端
pywebview 调受保护端点会被 403"。这一条**未实机验证**：桌面端是否仍会 403 需要装包
后确认。

## 五、vnext 未接线

vnext 是与 1.x 并存的第二套数据层，**尚未替换生产路径**：

- `backend/api/routes_vnext.py:196` 明确写着"本版没有分析流水线"；
- `run_analysis` 目前只被测试调用（`backend/tests/test_vnext_runner_analysis.py:19`）；
- `backend/vnext/breaker.py` 没有任何调用方。

因此 vnext 里那些"已经实现"的机制（排他快照、有序归约、`SourceRef` 溯源、
`PromptLimits`、原子写、`cutoff_view`）**不能**算作 1.x 缺陷已修——两条管线现在是并存的。

## 六、打包与工程卫生

| 问题 | 位置 | 表现 |
|---|---|---|
| onefile 下配置路径指向解包目录 | `desktop.py` 里 `cfg_path = Path(__file__).resolve().parent / "config.json"` | 打包后 `__file__` 在 `_MEIPASS` 里，配置读写位置不对 |
| spec 里 pytest_asyncio 既收又排 | `desktop.spec:60` 的 hiddenimports 含 `pytest_asyncio`，`:78` 的 excludes 也含它 | 自相矛盾，以 excludes 为准时隐藏导入白写 |
| `.gitignore` 里的裸 `v` | `.gitignore:53` | 任何名为 `v` 的路径都会被忽略（`git check-ignore backend/v` 命中）；新建这类目录会静默不入库 |
| `.gitignore` 里的裸 `1/` | `.gitignore:25` | 同类风险 |

## 七、覆盖范围

- 充分验证过的只有中文长篇。英文与多语言混合文本只做过切分可靠性验证，没有端到端实测。
- 桌面端只在 Windows 上验证，macOS / Linux 未做桌面端打包。
- 前端测试覆盖组件与逻辑，端到端（真实后端联调）没有覆盖。
- 输出 JSON 的 schema 仍在演化，跨版本升级后依赖旧字段的脚本需要适配。

## 八、已知取舍

**密钥脱敏是 fail-open 的。** 可能含 key 的错误日志走 `llm_failure_logger.redact()`，
`app.py` 的 `_SensitiveFieldsFilter` 对落盘 `analyzer.log` 的记录做二次过滤；但 redact
抛异常时返回原文——宁可漏脱敏也不丢日志。新增错误字段若不走 redact，就可能漏脱敏。
这是本地单用户工具的取舍：日志可用性优先于绝对不泄露。需要更强保证时应改 fail-close。

**`api.max_retries` 是死配置。** 它只用于失败日志里标注"第 n/m 次尝试"，不参与任何
重试判定；真正的重试由 `temperature_max_retries` 与 `backoff_max_retries` 控制。
配置项清单见 [configuration.md](./configuration.md)。

**`gui.language` 目前没有消费点。** 后端只是把它存进 `config.json`，不影响任何行为。

**`config.example.json` 已落后于代码默认值。** 它缺 `streaming_enabled`、`anchor_*`、
`recheck_*` 等字段，且 `temperature`、`auto_archive`、`summary_batch_size` 等值与代码
默认值不一致。以 [configuration.md](./configuration.md) 为准。

## 九、待复核项

以下是怀疑存在、但尚未在当前代码上复现确认的问题。复现后应移入上面的正式条目
或直接删除。

| 现象 | 怀疑点 |
|---|---|
| 并发分析时多数块被判为 `STALE_DEPENDENCY`（`backend/vnext/results.py:396`），导致重跑反复作废 | 依赖指纹在并发写入下的计算是否稳定 |
| `llm_client` 在拿不到 usage 时把消耗记为 0 或 `None`，使 token 统计失真 | usage 未知时的上报路径 |
| 风格分析快照在并发下存在边界不一致 | 快照写入与读取的时序 |
| 端口锁在启动瞬间存在竞态窗口 | 进程级单实例锁的抢锁时机 |
