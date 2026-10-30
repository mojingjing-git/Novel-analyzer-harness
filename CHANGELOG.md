# 小说智能分析器 — 完整更新日志

---

## [Unreleased] - 2026-10-30（v2 S2 数据空间：novel-analyzer-vnext 格式门禁与来源模型）

### 新数据空间与格式门禁

- 新增 `backend/vnext/` 包（`constants` / `artifacts` / `fingerprint` / `project` / `blocks`）：新版本只认带 `format_id = "novel-analyzer-vnext"` 与 `schema_version = 1` 的产物
- 数据根 `workspace_vnext/`，运行时状态独立为 `queue_state_vnext.json` / `books_vnext.json` / `config_vnext.json`；**与旧 `workspace/` 完全隔离**：不打开、不覆盖、不删除、不归档、不写迁移脚本
- 缺格式标记一律 `UNSUPPORTED_ARTIFACT_FORMAT`（"此目录不属于本版本，请从原文创建新项目"）；**不猜字段、不回退解析、不用旧默认值补全**
- 校验顺序固定：顶层对象 → `format_id` → `schema_version` → 必需字段 → 取值域。先判格式族再判版本，避免把"走错门"报成"版本号不对"

### 身份与指纹

- `chapter_id = ch_ + sha256(book_id \x1f ordinal)[:12]`，**不含正文哈希**：正文改写只改 `content_hash`，否则"改一个字"会变成"换了一章"，`STALE_INPUT` 无法沿依赖传播
- 六个指纹集中定义：`content_hash` / `block_signature` / `dependency_fingerprint` / `task_key`（不含 `run_id`）/ `result_id` / `evidence_id`（12 hex）
- 规范 JSON = 键排序 + 无空白 + `ensure_ascii=False`；多段拼接用 `\x1f` 而非 `|`（后者会让 `a|b` 与 `a|`+`b` 撞同一哈希）
- 文本规范化版本化（`TEXT_NORMALIZATION_VERSION=1`，幂等）：去 BOM / 换行 LF / 删行尾空白但**保留行首缩进** / 连续空行压至多 1；不做全半角与简繁转换（会改变字符数，废掉已存偏移）

### 写入与并发

- `write_json_atomic`：同目录 `mkstemp` + 唯一名 + `fsync` + `os.replace`，Windows `PermissionError` 6 次固定退避；失败保留原文件并清理临时文件
- 刻意**不复用** `json_utils.safe_save_json`：它带 `json_repair` 兜底与"失败返回 default"，会把"没写进去"伪装成"写成功"
- `commit_manifest` 锁粒度是**书**（跨书不共享锁），锁内读→改→`revision+1`→原子写；版本不符抛 `RevisionConflict`，不重试不合并
- `verify_block` 按 manifest 重算签名，成员变 / 正文变 / 策略版本变三类都判失效

### 测试与文档

- 新增 5 个测试文件共 60 例（`test_vnext_constants_alignment` 4 / `test_vnext_artifacts` 15 / `test_vnext_fingerprint` 15 / `test_vnext_project` 15 / `test_vnext_blocks` 11）；全量 **962 → 1022 passed**
- 新增 `docs/data-contract-vnext.md`：格式标识与策略版本口径、目录布局、对象定义、十步校验顺序、六个指纹的精确输入、写入规则、错误码表，附录含旧产物实测清单、18 行读写入口矩阵、实现决定
- `pytest_isolation.py` 增加 vnext 隔离段（合成 `config_vnext.json` / `queue_state_vnext.json` / `books_vnext.json`，空但合法，不伪造产物）

---

## [Unreleased] - 2026-10-30（v2 S1 错误判定贯通：C-01 止血 + 结果对象化）

### C-01 止血：正文里的数字不再被当成厂商错误码

- **根因**：`classify_message` 用 `\b\d{3,4}\b` 扫任意文本，正文中的数字被当厂商码，判成 AUTH/BILLING/MODERATION_INPUT 且 `action=stop` → `llm_client` 首次即停 → `pipeline` 登记 `_fatal_blocks` → **该块永久失败、补跑也不重试**；命中 moderation 的情形还会被 `skip_moderation_blocked` 静默跳过
- **实测对照**（`classify_message` 层）：
  - `第 401 章 我叫王二`：`AUTH`/`text`/stop → `UNKNOWN`/`none`/`limited_retry`
  - `这批货赚了 1008 块`：`BILLING`/`structured`/stop → `UNKNOWN`/`none`/`limited_retry`
  - `总字数 1026 万，还在连载`：`MODERATION_INPUT`/`structured`/stop → `UNKNOWN`/`none`/`limited_retry`
- **分级规则**：只有可信通道（SDK 异常属性 / 错误 envelope / 真实 HTTP 状态）有资格产出 `stop`；文本通道（命名错误码与错误措辞）上限是 `limited_retry`/`backoff`/`retry_after`，永不为 stop；纯数字厂商码只留在 `BODY_CODE_MAP`（structured 独享）
- **识别能力未被削弱**：真实 `status_code=401` 仍是 `AUTH`/structured/stop 且 `attempts==1`；`code=rate_limit_error` 仍是 `RATE_LIMIT`/structured/retry_after

### 判定对象贯通（D-01）

- 新增 `backend/core/error_types.py`（422 行）：`ErrorVerdict` + `LLMResult` + 5 个共享枚举 + 显式 JSON DTO；最底层叶子模块，禁止反向 import
- `ClassVerdict` 由 2 元组扩为 `(cls, confidence, action)`，`action` 由唯一策略函数 `resolve_action()` 推导；新增 `is_permanent()`（= structured 且 stop）
- `chat` / `chat_with_retry` / `chat_auto` 返回 `LLMResult`；`StreamChunk` / `StreamResult` 增 `verdict`（流式块也携带完整契约，不只在终结结果上补齐）
- 四处重复的重试决策收敛为 `_decide_retry()`；`_error_action` 判据由 `RETRY_POLICY[cls]=="stop"` 改为 `is_permanent()`
- `ErrorClass` 收敛为单一定义（`error_types`），`error_taxonomy` 改为 re-export（两份同值 str-Enum 会让跨模块 `is` 静默为 False）
- `pipeline` 致命块登记改为**结构化优先、文本兜底**：有 `retry_info["verdict"]` 用 `ErrorVerdict.is_permanent`，否则回退文本判定（文本恒不永久，不会误登记）

### usage 语义与并发隔离

- `LLMResult.tokens` 为 `Optional`，拿不到 usage 时 `None` + `usage_status=UNKNOWN`；`(0,0)` 记账口径保留，但"未知"与"真实为零"由 `usage_status` 区分
- `block_done` 与 chapter `token_stats` 两个载荷都带 `usage_status`；停止早退（未发请求）显式标 `KNOWN`
- 新增 6 路并发用例：按消息 tag 分派六种输入、`await asyncio.sleep(0)` 强制交错、断言 `peak>=2`，并用"结构化 402 与文本 BILLING 同为 `billing` 但 `(confidence, attempts)` 不同"锁死不串味
- `_decide_retry` 加类型守卫：传 `ClassVerdict` 直接抛 `TypeError`（防止字段演进后静默降级成不 stop）

### 测试与其它

- 全量 **892 → 962**：新增 `test_error_types.py`(29) / `test_retry_decision_u2.py`(20) / `test_verdict_pipeline_u4b.py`(4) / `test_concurrency_and_usage_u6.py`(8)；`test_error_taxonomy.py` 37→45（4 条旧契约断言改名改写，均因断言的是"文本数字判 stop"这类误判行为）
- 验收：`python -m pytest -o addopts=` → **962 passed / exit 0**
- 顺带修：断点续跑跳过块原本上报 `"tokens": (0, 0)`（元组，前端按数字读会得 NaN），改为整数 `0` + `usage_status: "KNOWN"`
- **刻意未做**：不加 `__iter__` / 可解包兼容层，旧五元组一次性删除；U2 曾用的 `contextvars` 临时槽已在 U5 删除，结构化结论改由结果对象显式携带

---

## [Unreleased] - 2026-10-30（v2 S0 基线：测试环境噪声根治 + CI + 合成语料）

### 测试环境（S0-4）

- **根治 `NO_PROXY` 方括号导致的 74 个假失败**：本机 `NO_PROXY` 含方括号 IPv6 写法 `[::1]`，httpx 把它当 URL 解析，端口段 `:1]` 转 int 失败 → `httpx.InvalidURL: Invalid port: ':1]'`，在 `backend/core/llm_client.py:143` 的 `AsyncOpenAI(...)` 构造期抛出。凡构造 `AsyncOpenAI`/`AsyncAnthropic` 的用例整片失败（`test_anthropic_provider.py` 7 例、`test_summary_checkpoint.py` 6 例等），同环境实测 74 failed / 790 passed
- `pytest_isolation.sanitize_proxy_env()` 在 `install()` 内、任何 `backend.*` import 之前执行：把 no_proxy 中的 `[::1]` 改写为等价裸写法 `::1`，丢弃端口段不可解析的代理 URL。**只改本进程 `os.environ`，不触碰宿主全局环境**
- 新增 `assert_proxy_env_sane()`，由根 `conftest.py` 的 autouse fixture 双向校验
- 验收：原始未清理的代理环境下直接 `python -m pytest` → 892 用例全绿（此前 74 failed / 790 passed）
- 新增 `backend/tests/test_pytest_isolation_proxy.py`（21 例）：归一化单测、代理 URL 可解析性单测、进程内已净化断言、以及症状级回归（构造 `AsyncOpenAI`/`AsyncAnthropic` 不得抛异常）

### CI 与依赖（S0-2/S0-3/S0-6）

- 新增 `.github/workflows/ci.yml`：`backend` job 跑 Windows + Ubuntu 双平台矩阵（Windows 覆盖文件替换/路径/CRLF 行为），`frontend` job 跑 `npm ci` + vitest + `npm run build`（含 `vue-tsc`），`hygiene` job 拦截被误跟踪的 `config.json`/`.env`/`session.token`/`queue_state*.json`/`*.log`/`*.tmp`
- workflow 内显式声明**不带方括号**的 `NO_PROXY`，避免 CI 复现本机写法
- 新增 `requirements-dev.txt`：`pytest` / `pytest-asyncio` / `pytest-timeout` / `pytest-cov`
- CI 以 `-p pytest_timeout --timeout=180 --timeout-method=thread` 强制并发用例超时（此前 `agent.md` 记录该债已挂两个月）；本地不强制，避免耦合开发机插件

### 合成语料（S0-5）

- 新增 `backend/tests/fixtures/vnext_corpus.py`：24 章确定性中文语料，覆盖 6 类边界——断号（第 17 章缺失）、多章块（块边界跨缺口）、同章先埋后收（第 12 章）、补跑缺口（第 9/22 章）、有互动与无互动角色（第 14 章对话 vs 第 15 章城东/城西各做各事）、引用歧义（第 19 章"他又提起那顶铜匣"——引用真实且词面相关，但语义不构成回收）
- 语料为**确定性生成**（无随机、无时间戳），同版本逐字节可重现；附 `CorpusTruth` 地面真值供断言
- 新增 `backend/tests/test_vnext_corpus_fixture.py`（7 例）自检上述边界齐全

### 基线归档（S0-1）

- 新增 `docs/BASELINE-2026-10-30.md`：工具链版本、代码规模（backend 141 文件 / 26,688 行；frontend 75 文件 / 11,763 行）、测试基线（892 收集用例）、配置字段清单（仅字段名）、产物基线、已知未达标项登记

### 说明

- 本批**不含行为修复**：C-01（分类器误报致永久失败）等全部功能缺陷留待 S1 及后续批次
- 旧 v1 产物（`workspace/`）未做任何迁移或修改；PLAN v2 `legacy_artifact_support: false`

---

## [Unreleased] - 2026-10-25（厂商错误码分类体系 error_taxonomy）

### 错误分类器（T1）

- 新增 `backend/core/error_taxonomy.py`：`ErrorClass` 11 类枚举 + `RETRY_POLICY` 决策矩阵 + `classify_exception(e)`（结构化通道：异常属性码 / body 深挖含 MiniMax `base_resp` / HTTP 状态兜底，400/404/422 不兜底）+ `classify_message(msg)`（字符串通道永不 None：码串扫描 structured 置信 + 类文本正则 text 置信）
- 码表覆盖 11 家厂商官方调研（OpenAI/Anthropic/DeepSeek/MiniMax/智谱/火山方舟/百炼/硅基/Kimi/OpenRouter + Ollama 文本兜底）；判定优先级写死：MODERATION_INPUT → OUTPUT → BILLING → CONTEXT_LENGTH → AUTH → BAD_REQUEST → RATE_LIMIT → SERVER → NETWORK → TIMEOUT；前插规则：百炼 `Throttling.*` 强制限流（覆盖 `insufficient_quota` 同名冲突）；条件规则：422 `unprocessable_entity_error` + 敏感 message → 输入审核（MiniMax Anthropic 皮肤，日志最大样本 ~5525 条），否则按参数错
- 审核**判侧**：码表逐条带 input/output（1026 输入 / 1027 输出 / 方舟 SensitiveContentDetected 输入），双侧码默认 OUTPUT；`refusal`、裸词码（safety/moderation）、智谱 1000/1302 系仅 body 通道，不进消息扫描（防误报）
- `moderation.py` 改转发层：`[MODERATION]` 标记契约（`mark_moderation`/`is_moderation_error`/`MODERATION_MARKER`）原样，pipeline/analyzer/前端零改动；旧码表 10 值全量吸收

### 接入修复（T2）

- **流式异常分类标记（原 bug 直接动机）**：`chat_stream` 的 except 块原本不做审核嗅探——MiniMax 1027（558 条）/Anthropic 皮肤 422 输入拦截（~5525 条）被当普通失败烧完整温度退火链（13-16 次计费调用/块，重试成功率恒为零）。现异常在场时即分类：审核打 `[MODERATION]`、RATE_LIMIT 顺手提取 Retry-After（流式此前从未做过）
- **重试循环按 RETRY_POLICY 决策**（流式/非流式 × 温度/退避 4 处同构）：stop 类（AUTH/BILLING/CONTEXT_LENGTH/BAD_REQUEST）首次命中即停并附可读提示（"API 余额不足，请充值后重试"/"建议调小 analysis.block_size"等）；审核 MODERATION_INPUT/structured 首次短路、text 置信维持连续 3 次（WAF 误判守卫语义不变）；`_is_429` 的 "429" 子串判定 6 处替换为 RATE_LIMIT 分类（"1429" 类误报不再加码退避）
- **pipeline 补跑 fatal 过滤（P2-2 最小方案②）**：stop 类失败块登记 `_fatal_blocks`、补跑轮剔除（全 fatal 则终止补跑）——欠费场景整场浪费从"剩余块数 × 4 次调用"降到"×1 次"；审核类不登记（`skip_moderation_blocked=False` 时用户明确要求补跑）
- `chat()` 非流式 APIError / anthropic.APIStatusError 分支对齐 `classify_exception`（后者原仅文本嗅探，Anthropic 结构化码由此进入）

### 行为变化与兼容

- 仅两类行为变化：①stop 类从"烧 N 次重试"变"停 1 次"（官方文档背书，省钱无正确性风险）；②structured/输入侧审核 1 次即短路（原 3 次）——text 置信阈值不变
- `[MODERATION]` 标记格式、pipeline skip 契约、FailureLogger JSON 格式均不变；回滚 revert llm_client/pipeline 接入点即可
- 新测试 `backend/tests/test_error_taxonomy.py` 37 例（api_failures.log 真实串 gold fixtures + P2-4 歧义样本 + 集成用例）；既有 test_moderation / test_llm_moderation_sniff / test_moderation_threshold 零改动通过
- plan：`docs/PLAN-provider-error-taxonomy-2026-09-30.md`（§8 独立复审 4 项 P2 + §9 二次复审 1 项 P2 修订并入正文后实施）

---

## [Unreleased] - 2026-10-15（第三批清理，Step0–R5 完成、Step6 暂停）

### 测试隔离基建（Step0）

- **问题**：pytest 与真实运行时共用 cwd/`NOVEL_ROOT`——`NOVEL_ROOT`/`LOG_FILE`/`CONFIG_FILE`/`QUEUE_STATE_FILE` 均在 import 时按 cwd 求值，跑测试可污染真实 `config.json` / `queue_state.json` / 日志 / `workspace/`；首轮"写后移除 handler"方案曾向真实 `api_failures.log` 写入 14KB（已逐字节还原）
- **改动**：新增根目录 `conftest.py` + `pytest_isolation.py`——在任何 `backend.*` import 前**写前隔离**：cwd 切会话临时根、`NOVEL_ROOT` 指向它、写合成 `config.json`、剥 `LLM_API_KEY`/`MINIMAX_API_KEY`/`NOVEL_ANALYZER_SESSION_TOKEN`、按 `pytest.ini` testpaths 建占位目录（pytest 按 cwd 解析 testpaths）；autouse fixture 逐用例断言隔离完整 + 重置单例/书目缓存
- **守卫**：`backend/tests/test_runtime_isolation.py`（进程内契约 + 独立子进程回归 + 正向对照——真实泄漏必被抓）；`test_archive_path_update.py` 补齐队列/书目还原并断言落盘目标在隔离根
- **验证**：全量运行后五个真实文件 SHA256 与基线一致；隔离原语独立成模块（根与 `tests/` 各有 conftest，`import conftest` 会随收集范围解析到不同模块）

### 死代码与协议修正（Step1）

- 删 `CharacterCardGenerator.long_context_insights` 无消费者读链（损坏的 `long_context_insights_aggregated.json` 会让有效角色详情 404）；写端与 Excel 消费者保留
- 恢复 `WSType.PING = "ping"` 常量（`ws.py` 心跳仍在发、前端仍识别，之前常量被误删却宣称类型全集）；注明「心跳是唯一不带 payload 的消息」例外，线上 JSON 不变
- 修 agent.md 两处失实：`networkx + pyvis` 标为历史依赖（无消费者）；测试示例 `test_pipeline.py`（不存在）→ `test_pipeline_inflight.py`
- 新测试：`test_character_card_generator.py` 16 例（真实生成器 + 真实路由）、`test_pipeline_callbacks_factory.py` 10 例（替换测试内复制闭包，改测真实 `make_pipeline_callbacks`）、`test_ws_protocol_ping.py` 5 例（以真实 `ws_progress` 为入口的运行时契约，变异检查全部被杀）

### 流式 Token 进度回调收敛（Step2）

- **问题**：`analyzer.py` / `final_summary.py::_make_on_progress` / `style_analyzer.py` 三处手写闭包的过滤/耗时/速率/广播主体相同，差异仅 context/session_id/unit_idx 来源
- **改动**：新增 `backend/core/stream_progress.py::make_stream_progress_callback`，三调用点接线；`_make_on_progress` 保留为薄适配器（总结回调仍在并发槽之后创建，源码顺序断言 + 变异检查锁定）
- **契约**：计时独立 `time.monotonic()` 不跨调用共享、累计值由 LLMClient 维护不二次累加、只广播 content/usage（reasoning/error 过滤）、`await broadcast_token_delta` 与节流入口不变；新增 `test_stream_progress.py` 21 例（含真实入口 + 真实重试链贯通用例）

### JSON 解析现状矩阵（Step3）

- **结论**：四处入口（analyzer 校验链 / `_parse_llm_dict` / `extract_foreshadow_json` / style）两两不等价（parse-first 与 extract-first 顺序相反、空 dict 行为分歧、summary 多 schema 转换）——**不强行抽取**，保留调用方重复
- 现状矩阵固化为 `test_json_parse_boundaries.py` 134 例（全部走真实入口；含 mock 生命周期防护用例）；全部 mock 改 `monkeypatch` 作用域管理（R3 曾发现直接赋值泄露污染后续用例）
- 唯一生产变动：删 `extract_foreshadow_json` 中不可达的 `isinstance(data, list)` 分支（`safe_parse_json` 只返回 dict/None）

### 名称规则与生命周期审计（Step4/Step5）

- 角色名后缀单一来源：`aggregate_utils.CHAR_NAME_SUFFIXES`（内容与顺序逐字节保留），`viz_service` 删重复字面量列表；聚合先 strip / 图谱后 strip 的边界差异保留并锁定
- 书名规则审计结论**不合并**：`infer_book_name` 与 `_clean_book_name` 21 样本 10 结果不同（文件名推断 vs 展示名清洗职责不同）；新增 `test_char_name_suffix_source.py` 20 例 + `test_book_name_rules.py` 98 例
- 生命周期审计结论**不建基类**：`SummaryService` 与 `LocationNormalizationService` 仅 4 小片段相同，其余全分歧；新增 `test_service_lifecycle_matrix.py` 26 例矩阵（重复 start/stop、互斥、token 只累计一次、三态等）

### 未做（Step6 暂停）

- `models/knowledge.py` 已确认缺陷：`_legacy_text` marker 在「旧层字段 → `from_dict` → `to_dict` → 再 `from_dict`」两轮往返后 `rolling_structured` 变 `{}`（旧滚动摘要静默丢失）——修复须先写失败回归，等范围讨论后单独授权
- 伏笔 batch 字段仍在写入；删字段/升 schema 需另立版本化迁移方案
- 全程未提交期间分步补丁存 `docs/patches/`（step0-r0 … step5-r5），plan/REPORT 见 `docs/PLAN-cleanup-batch-3-2026-10-14.md` / `docs/REPORT-cleanup-batch-3-2026-10-14.md`

---

## [Unreleased] - 2026-09-30

### 前端测试深度加强（84 → 225 用例）+ coverage 基建

- **问题**：前端 84 例集中在 utils 纯函数层，12 个页面只有 TimelinePage 有测试；
  QueuePage 的 onMessage 状态机、F-4 按回调分键防抖、P2 按名称删书，SummaryPage 的
  版本号串行化 PUT，SettingsPage 的 422 红框映射，StylePage 的 F-3 标志，
  ChapterDetailPanel 的 requestSeq 守卫等有修复史的高风险逻辑全部零自动化护栏，
  误删即静默回归
- **页面/组件覆盖**：页面 1/12 → 7/12（新增 Queue/Summary/Settings/Style/Splitter/Workspace），
  组件 3/13 → 10/13（新增 BookSelector 真实组件、ProgressBar、ConfirmDialog、LogConsole、
  ChapterValue、Icon、ChapterDetailPanel）；useLocale + router 冒烟补齐
- **纯函数抽取**（沿用 laneRegistry/summaryLanes 先例，行为零变化）：
  - `utils/queueFeed.ts`：block_start/done 车道生命周期（done 清速率、finished FIFO 50）、
    token_delta 累计取最大、发现流 200 条上限、会话汇总（命中率只算分析侧）、按名称解析删除索引
  - `utils/summaryProgress.ts`：4 阶段加权总百分比（0.7/0.12/0.08/0.1）、失败按卷进度近似
  - `utils/settingsForm.ts`：空串剪除（递归跳过数组）、422 detail 按 loc 末元素映射红框
  - `utils/splitterView.ts`：F-2 整体失败回滚 processing→pending、卷分组
  - `utils/workspaceView.ts`：fmtSize 阶梯 / fmtTime
- **顺手修 1 个真 bug**：SummaryPage `paramsLoaded` 门闩微任务时序缺陷——
  门闩在赋值后同步置位，但 Vue watcher flush 在下一个微任务，服务端总结参数
  ≠ 前端默认值时（如 batch_size=99）挂载即触发一次多余 PUT（GET+PUT 全量回写）；
  修复为置位前 `await nextTick()`，门闩测试锁定
- **基建**：`@vitest/coverage-v8@^4.1.11` dev 依赖 + `npm run test:coverage` 脚本 +
  vitest.config.ts coverage 段；基线：全仓 60.4% stmts / 55.2% branch，
  utils 层 98.5%（funcs 100%），未测页面（CharacterCard/Graph/Map/Stats/PromptPreview）已列明
- **测试**：+141 例（含 queueFeed 20 例、SummaryPage 版本号串行化/门闩/防 api_key 重置、
  SettingsPage 422/预设覆盖语义、SplitterPage pywebview 双通道/F-2 回滚、
  WorkspacePage 确认流、ChapterDetailPanel 切书竞态/60s 空闲回退等）
- **回归**：vitest 225 passed / `vue-tsc -b` 0 错 / `vite build` 通过；后端零改动

---

### PR-1 收尾：前端 X-Session-Token 注入（修桌面端 403 折中）

- **问题**：PR-1 的已知折中——`GET /api/analysis/logs` 受 session token 保护，
  但前端从不发送 header，桌面端 webview 调它必 403；且唯一消费点是
  QueuePage 的 `<a href>` 顶层导航（不走 fetch），即使注入 header 也修不到
- **改动**：
  - `desktop.py` Api 类加 `get_session_token()`：经 pywebview js_api 把 token
    传给前端（信任域前提：webview 只加载本地 dist 不渲染远程内容）
  - 新增 `frontend/src/api/sessionToken.ts`：token 模块级单例，模块加载 +
    `pywebviewready` 事件双路径就绪；仅存内存，不进 localStorage / 日志
  - `client.ts` `request()` 统一附加 header（合并顺序：Content-Type →
    调用方 headers → token 不可覆盖）；新增 `downloadLogs()`（独立 fetch +
    blob 下载，显式绕开 request() 对"200+非 JSON"的抛错契约）
  - QueuePage 下载按钮 `<a href>` → `<button @click>`，tooltip 同步
- **测试**：client.test.ts +5 / sessionToken.test.ts 5 例（含与 desktop.py
  方法名的契约一致性源码断言）/ test_desktop_pythonw.py +1（源码级，
  不 import desktop.py——顶层副作用会污染 pytest 进程）
- **e2e**：设 env 后无 header 403 / 正确 header 200 / 错 token 403 实测通过
- **plan**：`docs/PLAN-session-token-header-2026-09-29.md`（已过独立复审并
  按其 2 阻塞 + 5 重要项修订，见 §7）

---

### 思考模式状态提示 + 总结模型独立探测

- **问题**：M3 事故（agent.md 10.18）的"未来加固①"只完成一半——不点探测
  看不到 `{}` = "跟随端点默认（可能开启 thinking）"的风险；总结模型
  （summary_model）完全没有探测能力，而重型总结恰恰最容易撞思考链超时
- **改动**：
  - `settingsForm.ts` 新增 `inferThinkingState()`：覆盖五种禁用语法
    （3 顶层预设 + 探测动态选项的 2 种嵌套写法），三态判定供两页共享；
    `makeThinkingPresets()` 预设单一来源（value + 技术标签共享，首项 label
    按页传入——主模型 `{}` = 跟随端点默认，总结 `{}` = 回退全局，语义不同）
  - SettingsPage / SummaryPage 下拉旁新增常驻状态提示：`{}` → ⚠ 风险文案；
    显式禁用 → ✓ + Anthropic caveat（llm_client 对 Anthropic 只放行 thinking 键）
  - SummaryPage 的 `{}` 用两级组合显示：全局已禁用时显示中性"跟随全局
    （当前全局：✓ 已显式禁用）"，不再误报为开启
  - `routes_settings.py` `ModelPreviewRequest` 加可选 `model`：探测端点支持
    对总结模型独立探测（不传时行为与旧版逐字节等价）；SummaryPage 加
    「自动探测」按钮，命中自动填入下拉并触发持久化
- **测试**：settingsForm.test.ts +9（含嵌套写法与类型漂移防御）/
  SummaryPage.test.ts +4（含 mock 补 probeThinking——10.29 新增的组件测试
  工厂原本缺该方法）/ SettingsPage.test.ts +3 / test_settings_probe_route.py
  2 例（model 透传 + 空值回退）
- **plan**：`docs/PLAN-thinking-mode-status-probe-2026-09-29.md`（已过独立
  复审并按其 3 阻塞 + 3 重要项修订，见 §7）

---

## [Unreleased] - 2026-09-28

### 桌面模式无控制台启动（根治 taskkill 误杀）

- **问题**：`run_desktop.bat` 启动一个 tail PowerShell 日志窗口 + cmd 窗口，GUI 退出后
  `taskkill /FI "WINDOWTITLE eq NovelAnalyzer 运行日志" /T /F` 按窗口标题强杀进程树，
  在多项目开发机上存在误杀其他软件 cmd 窗口的风险；tail 窗口本身也已被 GUI 内
  WebSocket 日志面板取代，信息冗余
- **改动**：
  - `run_desktop.bat` 改用 `pythonw.exe`（venv 优先）通过 `start ""` 异步启动
    `desktop.py`，无控制台窗口、无 tail 窗口、无 taskkill；bat 自身立即 `exit /b 0`
  - `desktop.py` 顶部对 pythonw 下 `sys.stdout/stderr is None` 显式补 `os.devnull`，
    保证后续 `TextIOWrapper` / `_StderrTee` / `__getattr__` 代理不拿到 None
  - `_start_server` 删除内联 `log_config`（引用 `sys.stderr`），改 `log_config=None`，
    与 `backend/main.py` 一致；`uvicorn.error` propagate 到 root logger 由
    analyzer.log file handler 落盘（实测确认），仅把 `uvicorn.access` 压到 WARNING
  - 关键失败路径补原生 MessageBox：后端启动超时 / pywebview 缺失 / 单实例锁失败 /
    session token 失败，避免"双击没反应"
- **测试**：`backend/tests/test_desktop_pythonw.py` 锁定 None→devnull 处理与
  `log_config=None` 契约，防止回归
- **兼容**：`run_dev.bat` 开发模式不变（仍开两个 cmd 窗口）；exe 用户不走 bat，无影响

---

### 切章器支持 DOCX 输入

- **新增** `backend/utils/docx_extract.py`：`.docx` → 纯文本，**零第三方依赖**（`zipfile` + `xml.etree` 标准库）
- **入口归一化**：`splitter_service._detect_and_read` 是全链路唯一文本入口，按后缀分派即可支持双格式，**下游切章逻辑零改动**，`requirements.txt` 无需加依赖
- **核心洞察**：15 条章正则全带 `^` 锚点并配套 `len(s) <= 80` 整行闸门，其成立前提是「一行一个语义单元」。TXT 天然满足，DOCX 无换行符需显式重建 —— 本次改动本质是重建前提，而非重写规则
- **实现要点**：`<w:br/>` → `\n`；Heading/Title 样式段落强制独占一行；**递归 `iter('w:p')` 覆盖表格内正文**；异常四分诊（ZIP magic / `BadZipFile` / **`RuntimeError`=加密** / 空正文）统一转 `ValueError`
- **宿主与前端**：`desktop.py` 文件对话框增加 `.docx` 过滤器；`SplitterPage.vue` 的 `baseName()` 与 `<input accept>` 同步支持
- **测试**：`backend/tests/test_splitter_docx.py` 17 用例（TXT/DOCX 等价性 + `detected_pattern` 一致性 + 表格正文 + tab/br + 5 类异常 + 长书自动检测稳定性）
- **plan 已过独立复审**：v1 被挑出 3 阻塞 + 2 重要项并全部修订，详见 `docs/PLAN-docx-input-2026-09-28.md` §10
- **实现后再复审挑出 2 项阻塞并已修**：见下方「复审后修正」

#### 复审后修正（同日）

- **修 1（行为偏差）**：标题段落前后补空行的做法会让 DOCX 与 TXT 行结构不等价 —— 多出的空行被 `split_text` 保留进 `blocks/NNNN.txt`，而 `style_analyzer` 按「第 offset 个汉字」定位采样窗口（`style_analyzer.py:283`），偏移推后会让风格采样错位。改为**一个 `<w:p>` 输出一行、不注入空行**。已加 `test_blocks_content_matches_txt` 直接比对落盘正文
- **修 2（测试太弱）**：表格场景原断言只查「正文里有字」，实际单章时自动检测 `scores >= 5` 闸门过不了会静默回落单章且 `detected_pattern` 退化。改为 ≥6 章并要求 pattern 正确
- **顺带**：删除被证伪的 `enable_short_line_heuristic`，缩进回到 2 空格，行尾符还原为各文件 HEAD 约定，测试扩到 20 例
- **已知限制**：不支持加密 / 受 DRM 的 DOCX（显式报错）；不抽表格以外的结构（页眉页脚 / 批注 / 文本框）；不支持 EPUB / 旧 `.doc` / PDF

---

### 车道显示一致性 + 车道计数准确性修复

- **问题 1**：同一时刻车道面板混两种文案——WS `block_start` 发人类可读日志格式
  `开始分析ch1429-ch1432...`，REST `inflight_blocks` 对账发规范格式 `ch1381-ch1384`，
  15s 宽限期过后被对账覆盖，多车道并发时同屏可见两种格式
- **问题 2**：面板车道数可能超过并发池上限——WS 增量记账在 `block_done` 丢失
  （队满驱逐/断线）时虚高，且总结合成车道（≥900000）不占分析并发池
- **改动**：
  - 后端 `pipeline._emit` 六处 START/DONE/FAILED/SKIPPED 事件统一补结构化
    `range` + `block_size` 字段；`pipeline_events` 转发时 `range` 优先、message 兜底
    （`message` 保留原格式供日志控制台，向后兼容）
  - 前端 `laneRegistry.ts` 新增 `normalizeRange()`（剥前缀/尾巴的防御性纯函数）+
    `reconcileActiveBlocks` 增加 `concurrency` 参数：分析块超限时按 startedAt
    最早优先截断（防御最后一层）；QueuePage 对账 grace 15s → 5s
  - `QueuePage.onMessage` 的 block_start/done 改读 `p.range`
- **测试**：`test_pipeline_inflight.py` structured_range 两例 + `test_queue_service.py`
  range 优先用例 + `laneRegistry.test.ts` 11 → 18 例（normalizeRange 5 + 超限截断 2）
- **目验**（2026-09-30 真实队列）：活跃车道全程单一 `chN-chM` 格式，头部计数恒
  ≤ 并发值，失败块（MiniMax 1027 审核拦截 × 7 块 3 轮补跑）车道即时释放、跑完
  清零无僵尸；续跑补发 DONE 的 `block N` finished 占位为有意限速设计的已知兜底
- **plan**：`docs/PLAN-lane-display-concurrency-2026-09-28.md`（已归档 done，
  对账与目验明细见 `docs/PLAN-lane-display-closeout-2026-09-29.md`）

---

## [Unreleased] - 2026-09-10

### PR-1：API Key 日志泄露修复（D 方案鉴权）

- **`redact()` 工具函数**（`backend/core/llm_failure_logger.py`）：4 个正则覆盖 OpenAI sk-... / Anthropic sk-ant-... / Authorization Bearer / x-api-key header；**显式长度 20+ 避免误伤短随机串**；**fail-open**（异常时返回原文）
- **双层防御**：
  1. `FailureLogger.record_failure` 入口对 `error_message` 自动 redact
  2. `app.py` 新增 `_SensitiveFieldsFilter`（logging.Filter）注册到 `_file_handler`，对所有 `analyzer.log` 落盘 record 二次过滤
- **`llm_client.py` 9 处 `str(e)`** 全部走 `_redact(str(e))`（认证失败 / 请求超时 / API 错误 / 连接失败 / 未知错误 / 流式异常等 6 类异常路径）
- **D 方案鉴权**（`backend/api/auth.py` 新增）：`require_session_token` Depends，读 `NOVEL_ANALYZER_SESSION_TOKEN` 环境变量 + `X-Session-Token` header，`hmac.compare_digest` 防时序攻击；`/api/analysis/logs` 端点加 `dependencies=[Depends(require_session_token)]`
- **`desktop.py` 启动时** `secrets.token_urlsafe(32)` 生成 32 字节 token，写入 `session.token` + 设置环境变量；`_on_closed` 退出时清理
- **测试**：`test_redact.py` 14 case（含 3 个反向：短串不误伤、已脱敏不重复、Retry-After 不误伤）+ `test_session_token_auth.py` 4 e2e（开发模式放行 / 无 header 403 / 错 token 403 / 正确 token 200）
- **已知折中**：D 方案"桌面端零摩擦"需前端 `client.ts` 拦截 fetch 加 `X-Session-Token` header（独立 PR）；当前桌面 webview 调受保护端点会被 403
- **新约束**（写入 `agent.md` 9.1）：未来加新错误字段必须也走 `redact()`

### 文档：README 去 AI 味重写

- **README.md 简化至 150 行**（原 277 行）：去 AI 高频词（深度 / 一键 / 全流程 / 双保险 / 兜底 / 秒级 / 智能 / 成本优化 / 零数据丢失哲学 等）；统一术语"伏笔账本"（首次出现标注 `foreshadow_ledger.json`）；新增项目状态 / 已知限制 / 贡献 / 许可证 / 致谢 5 节；测试数字改写"约 427 用例（以 pytest --collect-only 实时输出为准）"
- **拆 4 个 docs**：
  - `docs/architecture.md`：核心机制（流水线 / LLM 客户端三重竞争 / 9 级 JSON 容错 / 50 类伏笔 / 4 阶段总结 / KV cache 布局 / 桌面双目录方案 / 完整目录结构）
  - `docs/configuration.md`：api/analysis 完整字段 + 4 个环境变量 + 8 家厂商预设
  - `docs/api.md`：60+ 端点表 + WebSocket 8 种消息类型
  - `docs/troubleshooting.md`：5 个日志文件 + 9 个常见问题详细版 + 性能调优速查
- **顺手修** `agent.md` 第 17 行测试数字 420 → 427（v3 plan 独立项发现的失真）
- **未动**：用户其他 session 10 个 M 改动（`backend/config/`、`backend/services/`、`frontend/` 等）由用户自己处理

### 许可证：添加 Apache 2.0
- 仓库根加 `LICENSE` 文件（标准 Apache 2.0 文本 ~200 行；Copyright 2026 mojingjing-git）
- README "许可证"段改写为"本项目使用 Apache License 2.0"，并引用 `LICENSE` 文件
- **协议选择理由**：多一层专利授权；大公司看到 Apache 更愿意集成；几乎所有"赚点小钱"路径（服务/咨询/捐赠）都不被协议影响
- **隐私**：Copyright 用 GitHub username 不暴露真名
- **状态**：仓库仍 private；用户"再优化优化"后转 public（届时 LICENSE 已在位）

### Docs：测试数字修正 + 架构图 Mermaid 化 + gitignore 兜底
- **测试数字**：README.md / agent.md 共 4 处"427/420/54" 失实数字统一改为"约 443/57"（2026-09-10 `pytest --collect-only` 实测）；保留 agent.md 第 1123 行"后端 420 passed"历史回归记录不动
- **架构图 Mermaid 化**：`docs/architecture.md` 新增 3 张流程图（数据流全景 / 章节分析流水线 / 最终总结 4 阶段），GitHub 原生渲染支持
- **gitignore 兜底**：增补 `session.token`（PR-1 引入的 32 字节 session token 缺兜底规则，桌面端启动时生成根目录文件）
- **Mermaid 节点**用 `<br>`（不带斜杠），兼容性更好；图 3 batch_size 标注"默认 40，config.json 当前配 80"
- **未动**：CHANGELOG 历史段 / agent.md 10.x 时序段 / `docs/PLAN-splitter-i18n-2026-09.md` / `docs/DELIVER-splitter-i18n-2026-09.md`（历史 plan/deliver 报告）；用户其他 session 10 个 M 改动

### v0.1.0 release：PyInstaller 单文件 .exe + 首次启动自动配置
- **首个 release 版本**（2026-09-12）— 配套 `dist/novel-analyzer.exe`（约 26 MB）单文件 Windows 桌面应用
- **PyInstaller 6.22.2 + hook-contrib 2026.7**：`desktop.spec` 完整配置（hiddenimports + excludes + datas），`build_exe.bat` 一键脚本
- **数据落 EXE 同级**（不走 `%APPDATA%`）：`backend/app.py` `backend/services/queue_manager.py` `desktop.py` 三处统一 `EXE_DIR`（可写）+ `BUNDLE_DIR`（只读，dist 嵌入位置）；删 `NOVEL_ROOT` 在 PyInstaller 模式下的绕路逻辑
- **首次启动行为**：`first_run_setup()` 自动创建 `workspace/` + 复制 `config.example.json` → `config.json` + 复制 `_welcome_workspace.md` → `workspace/README.md`；返回 `NEEDS_API_KEY` 触发 `window.evaluate_js("location.hash = '#/settings'")` 引导
- **WebView2 Runtime 检测**：`check_webview2()` 双保险（注册表 Edge Update Client + 路径 `C:\Program Files (x86)\Microsoft\EdgeWebView\Application`）；Win10 部分用户首次启动需下载 ~100MB runtime，给友好弹窗
- **`collect_submodules('backend')` 自动收集**：解决 `uvicorn.run("backend.app:app")` 字符串 import 被 PyInstaller 静态分析漏掉的问题（68 个 backend 子模块自动收集，排除 tests）
- **`.gitignore` 兜底**：增补 `build/` + `dist/`（PyInstaller 产物不入库，exe 单独走 `gh release`）
- **`config.example.json`**（新写）：真实 `config.json` 脱敏模板，`api_key="YOUR_API_KEY_HERE"`
- **`_welcome_workspace.md`**（新写）：workspace 目录的欢迎页（首次启动自动生成）
- **`docs/build.md`**（新写）：打包文档（spec 角色 / 关键设计 / 5 个常见问题 / 体积优化历史）
- **`build_exe.bat`**（新写）：一键打包脚本（清旧产物 + 前端 build + PyInstaller + 验证）
- **shutil 修 bug**：`desktop.py` 顶部加 `import shutil`（`first_run_setup()` 用了但漏 import）
- **Win11 透明背景**：pywebview 6.x 控件不支持透明背景色，自动降级纯色（不影响功能）
- **测试验证**：进程启动 12+ 秒稳定运行；workspace/ + config.json 自动创建；WebSocket `/ws/progress` accept + connection open；6 个依赖健康检查全部就绪
- **体积 26 MB**（含 Python runtime + 依赖 + 前端 dist + 配置模板）
- **范围外**：macOS / Linux / ARM64（v0.1.0 仅 Windows x64）；代码签名证书（v0.1.0 不上，避免 +30% 杀软误报）

---

## 阶段总览

| 阶段 | 时间 | 版本形态 | 技术栈 | 存档位置 |
|---|---|---|---|---|
| 一、雏形期 | 2026-03-03 ~ 03-18 | 单文件脚本 → 四合一工作台 | tkinter + OpenAI SDK（Qwen 系） | `E:\AI\小说` |
| 二、PyQt6 桌面版 | 2026-04-15 ~ 06-01 | PyQt6 桌面应用（Git 化） | PyQt6 + openai SDK | `F:\tool\novel_analyzer` |
| 三、PySide6 迭代期 | 2026-06-14 ~ 07-29 | v0.5 → v0.9.x → 多分支 → Web 原型 | PySide6 → FastAPI+React | `F:\AI\微型集群` |
| 四、Web 正式版 | 2026-08-01 ~ 至今 | FastAPI + Vue3 + pywebview | FastAPI + Vue3 + Tailwind | `F:\AI\小说分析器` |

---

## 阶段一：雏形期（2026-03-03 ~ 03-18）`E:\AI\小说`

### 2026-03-03 ｜ 项目源头：单块 LLM 分析雏形（`1/` 目录）

- 首个脚本 `run.py`（404 行 GUI 版）与更原始的纯命令行版本（`无明显问题会自动重置版.txt`，174 行）
- 核心思想从第一天起就没变过：**把小说按块喂给 LLM，强制输出 JSON 结构化分析（核心事件/人物弧光/伏笔/逻辑漏洞/跨块呼应/知识更新），用 `knowledge.json` 做跨块记忆**
- 本地 **LM Studio qwen3-8b** 推理，OpenAI 兼容接口；每 20 块触发主线压缩
- 实际分析《死人经》405 块；`compressed_arcs` 里残留 `<think>` 思考痕迹（早期清洗 bug 见证）

### 2026-03-05 ｜ 多模型分支与聚合闭环

- **`deepseek1` / `ver.1-20260304`**：同一脚本（MD5 相同）交给 DeepSeek 重写整理，API 转向阿里云 DashScope（qwen3-coder-flash）；新增**长上下文模式**与「洞察」输出（跨章模式/伏笔网络/节奏）
- **`kimi1`**：Kimi 独立实现版，新增**聚合闭环**：分析 → 聚合报告（AnalysisAggregator）→ 精简（JSON Optimizer Pro）→ LLM 输入；`0.0.1聚合.py` 指纹去重合并工具
- 本地 LM Studio 版（qwen3.5-9b）与云端版并行验证

### 2026-03-07 ｜ 三小件工具 + ver.1.1 系列

- **`ver.1.1-20260307`**：长上下文模式正式化（max_arc_length=2000）；`ver1.1-fork`（本地 9B 对比测试）；**`ver1.1-fork2`：结构化世界观记忆**——初始化并回写 10 个知识库字段（world_building/long_term_arcs/foreshadowing_network/character_relationships/thematic_elements/pacing_tracker），从"只记时间线+摘要"升级为结构化记忆
- **`拆书小软件`**：不调 LLM 的 txt 章节切分工具（多编码检测 GBK/Big5、13 种章节关键词、按目标字数分块）
- **`聚合去重小软件`**：按块号合并去重、角色关系推导、Markdown 报告
- **`角色卡小软件`**：从聚合结果提取全部角色 → 事件/弧光/关系统计 → 标准 Markdown 角色卡 + 索引（实战：《三侠五义》300+ 角色卡）

### 2026-03-08 ~ 03-18 ｜ 四合一工作台（雏形期收官）

- **`完整工作流/release-1.0.0`**：分割 → 拆解（分析）→ 汇总 → 人物卡四件套流水线
- **`工作台v1.0.0`**：单文件四合一统一工作台（1995/2422 行，NovelSplitter/NovelAnalyzer/AnalysisMerger/RoleCardGenerator 集成进一个 tkinter 界面）
- **`工作台v1.0.1`**（3/18 最后修改）：配置切到本地 unsloth/qwen3.5-9b，雏形期结束

**本阶段定型的核心机制（沿用至今）**：OpenAI 兼容接口、JSON 强约束 prompt、温度退火重试（0.1→0.05→0.0）、knowledge.json 跨块记忆、思考链剥离。

---

## 阶段二：PyQt6 桌面版（2026-04-15 ~ 06-01）`F:\tool\novel_analyzer`

### 2026-04-15 ~ 04-28 ｜ 从脚本到正式应用

- 4/15：《三侠五义》121 章 txt 就绪；4/18 首次运行（E 盘），LM Studio 本地模型，串行逐章分析 55 章，两阶段重试机制上线
- 4/22：`RETRY_MECHANISM.md`（温度退火 0.1→0.05→0.0 + 指数退避 2^n 秒）、命令行聚合 CLI
- 4/25-27：小说切分工具 CLI → **PyQt6 GUI 版**（`cuttttttt*.py`，编码探测/分词）
- 4/28：**正式定型**——requirements、main.py、README、应用图标、PyInstaller spec（`novel_analyzer.spec`，应用名"小说智能分析器"）；完成 64 章聚合 + characters.xlsx 导出；项目从 E 盘迁至 F 盘

### 2026-05-07 ｜ Git 化与首轮大修

- 建立 git 仓库（`.gitignore` 排除 workspace/日志/产物）
- **首提交即修复 14 个 bug**：GUI 闪退、数据反序列化、路径硬编码等（`f78048e`）

### 2026-05-16 ｜ 功能爆发周（git 可考）

- **block_size 功能**：多章合并为一个分析块（每块 N 章），块 ID 取首章号
- **伏笔回收机制**：`active_foreshadowing` 带编号注入 prompt，`resolved_foreshadowing` 显式回收，回收后不再截断增长
- 评审反馈修复：子串匹配阈值调优（10→6 字符）、去魔法数、补 debug 日志

### 2026-05-17 ｜ 最终总结页上线

- **FinalSummaryPage**：批量 LLM 分析全书脉络（分卷摘要 + 全书报告），配套修复：`json_mode=default` 强制（DeepSeek json_object 模式坑）、报告/卷摘要自动保存、章节按数值排序
- **并行模式实绩**：半小时完成《三侠五义》全书 121 章分析（串行预热 + 分片并行 + 断点续跑 + 失败 3 轮补跑）
- GUI 五个主标签页成型：分析结果 / 数据聚合 / 角色卡生成器 / 最终总结 / 统计面板；顶部 Prompt 预览、小说切分、工作区管理、风格分析入口
- 12 维度数据聚合 + Excel 多 Sheet 导出 + 角色卡（HTML/MD/TXT）

### 2026-05-27 ~ 06-01 ｜ 创作辅助转型（未提交的开发）

- 核心模块二次大改：KB 截断上限、llm_client、analyzer、json_utils 等 8 文件（**未提交 git**）
- **`blueprint_generator.py`**：全书结构蓝图生成（GUI）
- **`workspace_manager.py`**：多书归档管理器（GUI）
- **`style_analyzer.py` v2**：代码统计 + LLM 七维评分（叙事距离/句法节奏/感官密度/词汇层级/比喻策略/情感表达/对话比重）
- **`batch_style_v2.py` + `show_comparison.py`**：5 本书批量风格对比（三侠五义/圣墟/大王饶命/永夜君王/超神机械师）
- **skills/ 创作工作流技能**：拆书 → 蓝图 → 世界观 → 角色映射 → 新书生成 → 验证（从"分析工具"向"创作辅助平台"演进）

---

## 阶段三：PySide6 迭代期（2026-06-14 ~ 07-29）`F:\AI\微型集群`

> 技术栈统一迁移到 **PySide6**，多分支并行开发；此阶段确立了最终总结 4 阶段、伏笔账本、滚动总结、内存化、队列、Web 化等全部关键能力。

### v0.5 稳定版 ｜ 基础分析器定型

- "六路上下文注入"（故事历史/前情摘要/时间线/角色状态/世界观/伏笔网络）
- 分片队列并发（按并发数分桶）、断点续跑、失败补跑 3 轮、温度退火、思考链过滤
- 最终总结分批 + 汇总、数据聚合/Excel/角色卡/风格提取（单层 LLM 七维评分）
- 产物三件套格式：`chapter_N_result.json` + `chapter_N_data.json` + `chapter_N_report.md`（含冗余写盘，后续优化点）

### v0.6 ｜ 测试体系 + 伏笔账本

- **"九路上下文注入"**：新增角色关系/已验证事实/主题元素
- **tests/ 测试体系**（11 个测试文件）
- **`utils/foreshadow_ledger.py` 伏笔账本**（active/resolved/dormant 状态机）与伏笔审计报告
- 滚动总结以"批次 Rolling"初版形态出现；最终总结并发批次设计文档（06-16）
- 吞吐率/剩余时间预测、完整 API 配置

### dev 主干（git 05-07 ~ 06-24）｜ v0.7 → v0.9.0 → v0.9.1

| 日期 | 里程碑 |
|---|---|
| 06-17 | 并发批处理 + 伏笔账本 |
| **06-18** | **批次 Rolling Summary + KV Cache 优化**（prompt 分层：早期固定 + 近期滚动） |
| **06-20** | **v0.9.0 性能优化 + 结构化滚动总结 + 全量测试覆盖**；v0.9.1 修复 10 个 bug |
| 06-21 | **风格提取两层架构**（14 项统计硬指标 + 8 维 LLM 语义） |
| 06-22~24 | 设置界面完善（双主题 one_dark/one_light） |

### 07-07 ｜ UI 分支：win11 风格

- **Fluent Design 双主题**（themes/fluent.qss + fluent-dark.qss）
- **伏笔账本图形页**（gui/foreshadow_page.py）、设置对话框（滚动总结触发章数）、模型查询弹窗
- 毛玻璃效果原型（liquid_glass_demo.html）

### 07-16 ｜ 桌面版打包交付

- **构建版 0.0.1**：PyInstaller 成品「小说智能分析器.exe」（qwen2.5-72b-instruct-1m / DashScope）

### 07-21 ｜ 内存化 IO 优化（关键性能里程碑）

- 设计文档：《MemoryState 全内存 IO 优化设计》（实测每章 IO 开销 30-50s，目标 <1ms）
- **`core/memory_state.py`**：全部 AnalysisResult + KnowledgeBase 常驻内存，threading.Lock 并发安全，checkpoint_interval（默认 5 批）落盘，KB 快照子集缓存，删除 data.json 冗余写
- **流式并发池**替代批次 barrier 同步（提交：`feat: MemoryState 全内存 IO 优化 + 流式并发池`）

### 07-26 ~ 07-29 ｜ 多书队列 + Web 化（架构转型）

- **`core/queue_manager.py`**：多小说顺序分析队列（QueueItem 状态机 pending/running/done/failed/skipped、queue_state.json 持久化、check_api 预检）；实跑《卡徒》514 章（block_size=2）
- 07-28：内嵌 Web 工程副本 + 打包成品 NovelAnalyzerWeb.exe
- **07-29 `novel_analyzer_web_dev`**：**Web 版诞生** —— FastAPI（app.py 工厂 + 8 组路由 + ws.py WebSocket + progress_hub 进度广播）+ **React 19 + TypeScript + Vite** 前端（14 个页面）+ pywebview 桌面壳 + PyInstaller spec

---

## 阶段四：Web 正式版（2026-08-01 ~ 至今）`F:\AI\小说分析器`

> 当前版本。从 web_dev 快照起步：前端从 React 重写为 **Vue 3 + Tailwind 4 + TypeScript**（体积更小、与桌面壳集成更顺），后端在 FastAPI 架构上持续演进。

### 08-01 ｜ Git 初始化 + 畸形 JSON 修复 + 重试成本可见

- `init: 小说分析器代码快照`（含设计文档），GitHub Actions CI 配置
- **json 漏引号定向修复** + `parse_json_robust` 诊断信息（8 级容错链：直接解析 → 截尾 → 去注释 → 漏引号修复 → json5 → ast → json-repair → 单引号转换）
- **重试成本可见**：llm_client 累计 total_attempts → analyzer → pipeline → token_stats → 统计面板展示重试次数与失败 token 成本（"API 已扣费不能归零"哲学）；重试归因改按调用统计（并发准确）、耗时冻结、失败章节计入统计
- 12 个提交完成本批（设计 → 计划 → 实现 → 修正闭环）

### 08-02 ｜ 队列自动总结 + 书目标记（spec：auto-summary-and-book-marks）

- **队列完成后自动总结**：所有书分析完成后逐本串行执行最终总结（`auto_summary` 配置 + `allow_during_analysis` 护栏——仅自动总结可绕开"分析期间禁手动总结"）
- 总结批次/并发参数持久化到 config.json（取代 localStorage）
- 书目标记：书目状态摘要（has_report/has_ledger/has_audit/has_aggregated）、工作区扫描自动发现新书

### 08-07 ｜ 性能与正确性优化批（QUA/PERF/P0 系列）

- **P0-1 知识库快照隔离**：KB 快照子集缓存，低章号块读不到未来章节知识（并发正确性）
- **P0-2 休眠判定统一收尾**：批次并发使 last_seen_batch 非单调 → 全部批次完成后按批次升序统一 reconcile
- **P0-4 断号目录**：块划分按实际存在的章号（容错缺失章）
- **PERF-2 伏笔去重倒排索引**：关键词倒排 + SequenceMatcher ≥0.6 精筛
- **QUA-1 卷摘要压缩**：最终报告卷摘要超阈值（8 万字符）分层压缩（首尾组保留全文、中间组截半）
- **PERF-1 风格统计单次正则**：8 组词表合并单条正则一次 finditer（避免约 120 次全文扫描）
- 测试补全：快照缓存不泄漏未来章节、断号目录、休眠判定、去重倒排语义

### 08-09 ｜ 伏笔 50 类分类体系（治本）

- **50 类功能分类定义**（`FORESHADOW_CATEGORY_DEFS`，8 组 50 类，schema 版本号管理，兜底"其他"）
- 先落地 **LLM 归一化兜底版**（实测教训：单批 >100 条质量退化、思考链吃光 max_tokens、每批落盘防崩溃丢进度），《北宋穿越指南》497 章全量验证：1663 种 type 全量映射、0 幻觉、700 条伏笔总表、2786 条时间线全分类
- **随后改为源头约束主线**：分析 prompt 内嵌 50 类表（恒定文本注入 system 前缀，KV cache 友好），`type` 字段禁止自创；归一化版作为分支备份（`F:\AI\小说分析器 - 归一化兜底版`）
- 旧书靠 per-book `foreshadow_type_map.json`（含 schema 校验）被动兜底，零 LLM 调用
- 配套：伏笔总表三维过滤（重要度/置信度/类别）+ 分层截断（高 500/中 200）；时间线 50 类 chip 筛选；设置页类别勾选；`GET /api/foreshadow/categories`

### 08-09 ｜ 总结阶段独立模型选择（应对大模型延迟问题）

- 诊断：M2.7 思考模型在 50-110K 字符大 prompt 上延迟 5-15 分钟，630s 硬超时卡在延迟中位数 → 大量请求被误杀（实测记录：M2.7 官方 issue 报告平均响应 ~650s；**非安全审核**——涉敏错误码 1026/1027 会秒回错误响应，而全部失败都是"API 无响应"）
- **`summary_model` + `summary_thinking_mode`**：总结页独立模型下拉（复用同一 base_url/api_key，重型任务切 M3 等）+ 思考模式三档（自动 / mimo·GLM·M3 禁用思考 / DeepSeek·Qwen 禁用思考），持久化到 config.json
- **实测效果**：切 `deepseek-v4-flash` 后最终报告 3 分 21 秒一次通过（167K tokens），对比 M2.7 卡 630s×3 轮仍失败
- `summary_timeout` 默认 600s 与 max_tokens 告警阈值（64K→65537）微调

### 08-09 ｜ 最终总结断点续跑补全

- 阶段 1/4 卷摘要断点此前已有（volume_N.md 一完成即写 + recon_N.json 失败不写 → 重启只补调和 + manifest 校验 batch_size/ch 范围）
- **新增阶段 2/4 复检续跑**：每完成一批复检立即落盘 ledger（进 `_lock` 防并发写竞争），崩溃/停止后重启只对仍 active 的伏笔复检，已回收的不重复付费
- 配套：README 完整文档

### 08-24 ｜ 双批代码审计 + 14 个 P1 修复

- 第一批 5 路并行审计报 66 条 → 第二批 5 路对抗复核（53 确认 / 12 部分成立降级 / 1 驳回），落地全部 14 个 P1：滚动总结 KB 快照注入分析 prompt、pipeline rolling 容错、llm_client APIError 嗅探防崩穿重试链、final_summary 断点 results 指纹失效 + stop 级联取消风格任务、归一化全空判败不落盘/空批熔断/缓存短路要求非空、delete_book 显式路径防同名误删、切分保存运行护栏 + 暂存-交换原子写、settings from_dict 数值强转 + 损坏 config 修复前拒绝 save 保 API Key、前端 GraphPage ECharts 死 DOM 自动重绑 + Timeline/CharacterCard/Summary 三处切书守卫
- 逐项明细见计划文档：已归档到 `_trash_2026-09-10/superpowers_plans/2026-08-24-p1-bug-fixes.md`

### 08-24 ｜ P2/P3 划算项批量修复（S+A 两档 19 项）

- 后端 10 处：pipeline 快照构建线程池化 + failed 按章号范围过滤 + 势头 join 容错；models 字符串数组字段整串收编 + validate 嵌套对象类型检查；json 修复链全策略 dict 守卫 + safe_save_json 进程内唯一 tmp 名；聚合主文件改 safe_save_json 原子写；queue put_queue 畸形条目跳过 + 停止后不再广播假 done；final_summary run() 阶段段 try-finally 兜底回收 style_task；workspace 回收站 aborted 视为失败 + 归档回滚失败记录滞留路径；excel 零 sheet 明确报错 + 单元格公式注入消毒；moderation 瞬时误判多给一轮温度尝试；prompt_builder 章节正文超限尾部保序截断
- 前端 9 处：graph tooltip 转义 + loadData 切书快照守卫；summary viewAggFile 切书快照守卫；chapter loadChapter 早退分支失效在飞响应；map 归一化结束沿 getMap 补 try-catch；pywebviewready 监听 + rAF 卸载取消 + 日志 id 过滤；queue-page 定时器分键清理 + 删除确认改名称快照；api client 路径参数统一 encodeURIComponent；markdown 链接占位保护防强调正则污染 href；settings 提交前剪除空字符串数值字段
- 验证：后端全量 pytest 226 passed（本批新增约 15 用例），前端 vue-tsc + vite build 通过
- **未动**：KB 近邻指纹、切分窄化算法、协议探测回退等高成本项；逐项明细见计划文档：已归档到 `_trash_2026-09-10/superpowers_plans/2026-08-24-p2p3-batch-fixes.md`

### 08-24 ｜ 深度修复批次（G1-G7，大改值得七项）

- 上批「未动」的三项高成本项全部落地：KB 近邻指纹（G2）、切分窄化算法（G6）、协议探测回退（G5），外加四个审计高优先项
- 后端 9 文件：memory_state 审核拦截 skipped 标记随 checkpoint 持久化 + 成功落盘清理陈旧标记 + pipeline 续跑过滤已 skipped 块（重启不再重付 LLM 重试链费用）；knowledge_base 近邻增量校验 ≤best_limit 基线区间文件指纹、失效即全量重建，命中产物返回副本断开对象别名（陈旧 KB 不再固化进最终 knowledge.json）；style_analyzer `token_sink` 经 final_summary 接入 runner 统计（style 分类恒零盲区消除）；book_service `get_book_path` miss 改后台单飞刷新并立即返回 None（冷启动保留一次阻塞刷新，消除事件循环秒级冻结）；queue_service 预检加跨协议探针自动纠正 provider 误判（OpenAI 网关上的 claude-* 不再整书 404）；splitter 次级章节格式按比额门槛并入切分（绝对得分 ≥2 且 ≥ 主导的 15%，卷章混排书不再吞章、高频编号列表仍被挡住）；text_utils 编码检测改采样罚分择优（U+FFFD×8/控制字符×4/PUA×3 除以样本长度取最低罚分，Big5 繁体书不再坠入 gb18030 乱码，`detect_and_decode`/`detect_encoding` 两入口预检统一）
- 新增测试文件 7 个：test_memory_state_skipped / test_kb_fingerprint / test_style_tokens / test_book_service_async_refresh / test_provider_heal / test_splitter_mixed_formats / test_text_encoding
- 验证：后端全量 pytest 251 passed（本批新增约 24 用例），前端 vue-tsc + vite build 通过

### 08-24 ｜ 前端 vitest 基建 + P3 清扫

- 测试基建：引入 vitest + jsdom + @vue/test-utils 与独立 vitest.config.ts（markdown 自运行脚本迁入 npm test，新增 useLogStore/useProgressSocket/TimelinePage/client 4 个守护测试文件，全量 34 用例）；P3 修复 3 处：client.ts 非 JSON 的 200 响应显式抛错不再静默返回空对象、viewAggFile 过期失败不再污染新书 aggError、GraphPage 占位项清除上一本书的 loading 残留
- 验证：npm test 全量 34 用例通过（vitest+jsdom），vue-tsc + vite build 通过；后端零改动

### 未提交清单（当前工作区）

- ~~08-02 之后的所有里程碑（自动总结/优化批/伏笔 50 类/模型选择/复检续跑）尚未 git 提交~~ 已全部正式提交（08-27 批次，至 `46bc715`），本节作废
- ~~系统备份：`F:\AI\小说分析器 - 归一化兜底版`（08-09，伏笔归一化分支完整快照）~~ 已于 2026-08-31 删除（连同 `小说分析器 - 副本`，共 690MB）：基线 cae160a 在主仓历史内、两份备份的全部改动文件均已被后续正式实现覆盖，验证后清理；`代码版本/`（4GB，git 化之前的手动版本档案）与 `数据产物/`（2.3GB，分析结果归档）保留

---

## 2026-08-26 队列运行中实时仪表盘（H17 / S1）

- **后端（queue_service.py:700 on_progress）**：
  - 新增 `block_start` WS 消息（status=start，带 chapter/range/progress/total/ts）
  - `block_done` 补 `range` 字段
  - 新增 `discovery` 消息（status=done + result 存在）：core_events 数 / foreshadows 截 24 字 / 新人物（首次登场 + 停用词过滤）/ unresolved 截 24 字
  - 顶层 helper `_extract_discovery(result, seen_characters)` 纯函数好测
  - 类级 `_seen_characters: set` 跨块持久（Open Question：续跑后全量"首次"）
- **前端（3 组件 + QueuePage 集成）**：
  - `LaneView.vue`：并发车道状态机（占位/释放/失败态/超上限/重试同 id）
  - `DiscoveryFeed.vue`：ring buffer 200，hover 暂停滚动，新伏笔/新人物紫色高亮（#534AB7）
  - `RunDashboard.vue`：4 卡聚合（已完成/ETA/本会话输入输出）+ LaneView + DiscoveryFeed
  - `QueuePage.vue` 左 split-col tab 化：「运行概览」/「章节详情」；默认 running→概览，否则详情；用户手动切换本会话记忆
  - `useProgressSocket` ProgressMessage type 联合加 `'block_start' | 'discovery'`
- **测试**：后端 +5 / 前端 +12（LaneView 5 / DiscoveryFeed 5 / RunDashboard 2）；全量 293 + 47 passed / 0 回归；vue-tsc 0 错；vite build 0 错
- **分支**：全程在 `h16-recovery`（feature/streaming-tokens 名字被并发 watcher 针对）
- **不做**：流式 content_delta 输出 / 跨块伏笔去重 / 车道进度条+卡死预警（V2 升级，依赖 H16 Phase 3 业务接入）

## 2026-08-26 日志轮转彻底修复（H15）

- **`desktop.py` crash.log 轮转 bug**：`_StderrTee` 用独立 `open("a")` 句柄写 `crash.log`，与同文件 `RotatingFileHandler` 句柄并存，Windows 下 `os.rename` 因 `ERROR_SHARING_VIOLATION` 失败 → 异常被 `logging.handleError` 写回 stderr（即 crash.log）→ 形成「永不轮转 + 永不增长上限」反馈环。**实测** crash.log 60MB 无 `.1` 备份
- **`llm_client.py` FailureLogger 无大小上限**：原 `open("a")` 裸追加，24h-unlink 仅在 `__init__` 触发一次；长跑不重启文件只增不减
- **修复**：`_StderrTee` 改走 `crash_logger` 的 `RotatingFileHandler`（统一流/锁/轮转），`FailureLogger` 改用 `RotatingFileHandler(10MB × 3)`，取消 24h-unlink
- **统一策略**：所有 log 单文件 ≤10MB，备份 3 份（30MB 总占用上限）
- **回归**：255 pytest 全过，行为契约零变动；轮转备份在桌面端重启后首次到 10MB 时自动出现
- **未动**：`%LOCALAPPDATA%\NovelAnalyzer\run.log` / `build.log`（不在工程内）

---

## 2026-08-31 运行概览车道堆叠修复（车道注册表自愈）

- **症状**：长程运行后「并发车道 57 / 8 运行中」极夸张堆叠；原有功能无影响。WS 挂测试客户端三轮实测定位：真实 LLM 并发恒等于配置值（信号量从未超卖），纯前端车道记账腐烂
- **根因（三层叠加）**：
  1. 记账口径：`activeBlocks` =「start 未 done」，但 done 由 as_completed 消费循环补发（worker 已释放槽位）且每 8 块阻塞等 rolling 总结 1-3 分钟 → 稳态虚高 ~2.5 倍（实测峰值 20/8）
  2. 通道有损：ProgressHub 每连接队列 maxsize=1000 满了丢最旧 + 断线无重放 + 前端无对账 → 丢一条 done = 永久僵尸（只能刷新页面清零）
  3. 触发点：断点续跑补发洪峰（916 块 ~2700 条消息必打满队列）+ LLM 失败风暴日志（实测 21:51-21:52 两分钟 73 次尝试失败）+ rolling 等待
  - 附带排除并堵上两条隐藏泄漏路径：skipped（审核拦截）原先不转发 block_done；worker 异常兜底固定发 chapter:0 被前端 `blockId > 0` 检查忽略
- **修复**（subagent 复审通过并按其 4 条修正落地）：
  - `pipeline.py`：`_analyze_one_block` try/finally 登记 `_inflight_blocks`（预热/并发/补跑三路径单点覆盖，信号量真实在途 ≤ concurrency）+ `inflight_blocks()` 快照；`_worker`/`analyze_block_with_progress` 异常兜底带原 block_id 发 failed + 补 `state.add_failed`；续跑补发每 50 条 sleep(0)
  - `queue_service.py`：`status()` 暴露 `inflight_blocks`（getattr 兜底 `__new__` 单测实例）；`skipped` 也转发 block_done(ok=false)
  - `progress_hub.py`：队满分级驱逐——先丢 token_delta（高频可再生）→ 再丢 log → 保 block_start/block_done 等低频关键事件
  - 前端新增 `utils/laneRegistry.ts` 纯函数：`gcActiveBlocks`（超预警窗无 token / 30min 硬上限回收僵尸，跳过 ≥900000 总结合成车道 + has-token 门控防误杀）+ `reconcileActiveBlocks`（inflight 整体纠偏，保留合成车道与 15s 宽限新块，无变化返回原引用）；`QueuePage.syncLaneRegistry()` 挂 `refresh()`（5s 轮询 + block_done 防抖），GC 同步清理 tokenByBlock/rateByBlock；client.ts `AnalysisStatus` 加 `inflight_blocks` 类型
- **回归**：后端 420 passed（+6：hub 分级驱逐 3 / pipeline 在途+异常 3，54 文件）；前端 vitest 77 passed（+11 laneRegistry，11 文件）；vue-tsc 0 错；vite build 通过
- **未做**：rolling 等待与消费循环解耦（消除 done 延迟基线，行为改动大，先观察自愈效果再定）

## 2026-08-31 queue_service 职责拆分（1003 行 → 4 模块）

- **`queue_manager.py`（408 行）**：QueueItem/QueueManager（状态机/并行扫描/API 预检/持久化）独立；`check_api` 的 `LLMClient` 随迁，test_provider_heal 8 处 patch 字符串同步迁移（含 :61 整类重绑定）
- **`analysis_stats.py`（119 行）**：新类 AnalysisStats 收敛 service 的 7 个散属性（token 五件套/起止时刻），方法 reset/record_chapter/accumulate_token_stats/baseline/freeze/session_snapshot/book_consumption；**不持有 pipeline**——运行中实时 KV 命中由 service 传参
- **`pipeline_events.py`（167 行）**：`make_pipeline_callbacks(hub, item, stats, seen_characters)` 工厂替代 `_run_one_item` 的两个嵌套闭包（捕获面经复审确认恰好完整）；discovery 提取/ETA 格式化随迁
- **`queue_service.py`（406 行）**：只留 AnalysisService 编排 + get_service，符号名与文件名不动（summary_service 反向导入方向不变）；`_record_chapter_stat` 退化为测试拦截垫片
- 拆分前 subagent 对照代码复审，吸收 3 处修正（routes_analysis 生产导入不能破 / AnalysisStats 补 freeze+baseline / 每步绑定测试改造）；三步各自提交后全量 pytest 420 passed，`from backend.app import app` 导入冒烟通过
- **未动**：自动总结/归档编排（与分析-总结互斥锁纠缠，等有功能需求时再拆）

## 2026-08-31 WS 事件协议类型化（前后端，wire 格式零变更）

- **后端 `backend/ws_events.py`（新，协议唯一权威定义）**：`WSType`(11 值)/`WSState`(12 值)/`PipelineStatus`(7 值) 三个 StrEnum + 8 类 payload 契约 + 7 个纯同步工厂（log_event/progress_event/block_start_event/block_done_event/discovery_event/state_change_event/token_delta_event——HubLogHandler 同步线程可调）
- **后端接线**：pipeline.py 12 处 `_emit` status 常量化；pipeline_events.py 比较与发布改工厂（含删 `_record_chapter_stat` 之外的旧发现：progress_hub.block_done() 便捷方法全仓无调用方且 payload 形状与翻译层不一致，已删除并在 ws_events.block_done_event 注释留痕）；queue_service(6)/summary_service(4+2)/location_normalization_service(5+1)/app.py(2) 全部常量化；progress_hub.state_change 签名收紧为 WSState
- **前端**：useProgressSocket 定义 10 类 payload 接口 + 可辨识联合 ProgressMessage（ping 无 payload）；QueuePage 删 5 处 `as unknown as` 强转（switch 自动窄化，只删强转不改 `??` 兜底语义）；App.vue 删 7 处 as 强转；summaryLanes 入参类型化 `WSSummaryProgressPayload`（字段全 optional 兼容测试 fixture）
- **验证**：后端 420 passed + 导入冒烟；前端 vitest 77 passed + vue-tsc 0 错 + vite build 通过
- **收益**：状态漏发/字段拼错不再静默通过（类型层拦截），WS 协议有了唯一权威文档（ws_events.py 即协议文档）

## 2026-08-31 llm_client 拆分（1761 行 → 4 模块，主类 1291 行）

- **`llm_failure_logger.py`（104 行）**：FailureLogger 类 + 进程级单例 + `_get_failure_logger()` 原样随迁（纯 stdlib；全仓仅 llm_client 内部使用，零测试改动）
- **`llm_probe.py`（277 行）**：`probe_thinking_params` 多模式思考检测 + `_THINKING_DETECTORS` + `_get_reasoning_tokens`（全库仅 probe 使用）转模块函数；LLMClient 删除该 staticmethod。调用方同步：routes_settings probe 端点、test_anthropic_provider 9 处（函数级导入/调用/AsyncOpenAI patch 字符串）、test_streaming 1 处（复审纠正：:563 的 patch 实为 probe 用途而非流式测试）
- **`llm_stream.py`（133 行）**：StreamChunk/StreamResult 数据类 + 三个解析纯函数（`parse_openai_stream_event`/`parse_anthropic_stream_event`/`parse_anthropic_response`，方法体无 self）转正；llm_client 三个调用点、analyzer 导入、final_summary 延迟导入、test_streaming 5 处 + test_anthropic_provider 2 处方法调用同步改写
- **llm_client.py 保留**：LLMClient 类 + chat/chat_with_retry/chat_stream_with_retry/chat_auto 重试核心（与 self 状态深度集成，机械拆分有行为风险——刻意不动）；AsyncOpenAI/AsyncAnthropic 导入必须留在本文件（流式路径的 patch 依赖）
- 拆分前 subagent 对照代码复审，吸收 2 处遗漏调用点；三步各自提交后全量 pytest 420 passed，`from backend.app import app` 导入冒烟通过
- **未动**：`moderation_hit_from_exception`（18 行，迁移价值低）、`_build_anthropic_payload`（读 self.config 保留为方法）、`detect_provider`（测试直接导入，留 llm_client）

## 2026-08-31 工作区清理

- 删除已被完全取代的两份整仓备份：`小说分析器 - 副本`（332MB）+ `小说分析器 - 归一化兜底版`（358MB）。删除前验证：基线提交 cae160a 在主仓历史内、两份备份全部改动/新增文件在主仓均有对应（内容已被后续正式实现覆盖）；`代码版本/`（git 化前手动版本档案）与 `数据产物/`（分析结果归档）保留
- 项目内清理：tmp_* 草稿 ×5、`crash.log.1`（62MB，H15 修复前的旧轮转备份）、`__pycache__`/`.pytest_cache`、前端构建残留（vite.config.js/.d.ts、tsbuildinfo ×2，均 gitignored 可再生）
- 删除 git 跟踪的死文件：4 张无引用的视觉验证截图（frontend/*.png）+ `.github/workflows/ci.yml`（仓库无远端，Actions 永远不会触发；均可从历史恢复）

## 2026-09-02 i18n 接口预留（L2）

- 后端 `GUIConfig.language` 字段（默认 `"zh-CN"`，写入 config.json）
- 前端 vue-i18n@^10 集成，空 locale 文件骨架
- `useLocale()` composable + `AppConfigDto.gui.language?` 类型
- **零翻译，零 UI 变化**；仅为未来 i18n 启用预留接口

## 2026-09-02 切章器国际化 Phase 2-3 修复（P1）

**目标**：把 `backend/services/splitter_service.py` 改成"包罗万象"切章器，覆盖 7 本 Project Gutenberg 外文书 + 4 类边界 case + 中文回归 0 破坏。

**Phase 2 核心修复**（T2.1a/b、T2.4、T2.5b、T2.6、T2.7）：
- **T2.1a 英文章节正则**：`CHAPTER_PATTERNS` 加 2 条——`[Cc]hapter\s+[IVXLCDM]+[\.\s:]?`（罗马数字）+ 裸 Roman 短篇（`[IVXLCDM]+\.\s+[A-Z][A-Z\s,'\u2019\-:]{2,80}$`，Sherlock 风格）；收紧原 `^\d+[\.、]\s*\S` 排除 Gutenberg license 的 `1.A.`/`1.B.`
- **T2.1b 章节号提取**：`_extract_chapter_num` 支持 Roman（`_roman_to_int`，含 `I` 单字符兜底）+ 英文数词（`_word_to_int`，含连字符/空格复合）
- **T2.4 卷归属**：`VOLUME_PATTERNS` 加 `Book the First/Second/...` + `Book [IVXLCDM]`（Dickens 风格）；`_normalize_volume_label` 同步把序数词映射为 Roman
- **T2.5b TOC 跳过**：`_strip_gutenberg_header` 在 START 后 100 行内识别 CONTENTS 块、跳过再找真章节；`_find_first_real_chapter_or_volume` 通过"12 行内 body 出现"判别 TOC vs 真章节
- **T2.5b 同号去重**：`_dedup_same_key` 保留字数最多的（TOC 残留 < 1000 字、正文 ≥ 5000 字）
- **T2.6 Gatsby 独行 Roman**：`裸罗马独行` 正则覆盖 `I`/`II`/`IX`/`X` 单独成行
- **T2.7 PG header 提取**：新增 `_extract_pg_header_meta` 识别 `Title:` / `Author:` 显式行（Dracula/Pride 等 7 本 metadata 全空 → 全填上）

**Phase 3 边界加固**（T3.1-T3.6）：
- 新增 `tests/test_splitter_edge_cases.py`（8 个 case 覆盖 4 类边界）：
  - 空文件 / 纯空白 → 1 章"全文"不报错
  - 单章节（"第1章 xxx" + 500 字）→ 1 章
  - 5 行诗集 / < 10 行短文 → 1 章"全文"
  - 20MB 单章 + `max_words=1M` → 自动拆为多章（`@pytest.mark.slow` + threading 超时保护）
- 顶层 `tests/conftest.py` 注册 `@pytest.mark.slow` marker

**Phase 4 已知限制**（T4.6 docstring 末尾追加）：
- Dracula 日记体无显式 Chapter 标记：仍可识别 `CHAPTER I.` ~ `CHAPTER XXVII.` 27 章 Roman
- Moby-Dick TOC 重复：`dedup_same_key` 兜底后剩余 1-2 条残留
- 裸罗马短篇正则要求"全大写 2-80 字符"：小写标题不识别
- pre-START 头 Title/Author 提取：只对 Project Gutenberg 文件有效

**回归结果**：
- baseline 7 本：7/7 章节数达标 + 7/7 metadata 含正确书名/作者
- 边界 8 case：8/8 通过
- 中文回归 16（mixed_formats 7 + atomic 2 + silent_failure 7）：16/16 通过
- `pytest backend/tests/`：421/421 通过（实际用例数 = 421；plan 写作"354"为 v2 计划时的过时数）
- **总改动**：splitter_service.py 5 处 + tests/test_splitter_edge_cases.py（新增）+ tests/conftest.py（新增）+ CHANGELOG.md（本条）

**未做**：
- 不动 Phase 1 产物（baseline 报告 / Gutenberg 下载脚本 / 测试 fixture）
- 不 git commit（用户未要求）
- 不重写整个 splitter_service.py，仅最小必要改动
- 日文片假名章节不识别（不在本次范围）

---

## 2026-09-04 小说书名推断合并

- splitter `save_to_workspace` 透传 `book_name` → `metadata.json.title`（修法 A，治本）
- `final_summary.detect_book_name._looks_valid_book_name` 拒含中文句末标点 / "作者/著" 的 title（兜底，对历史污染生效）
- 新增 `backend/scripts/fix_metadata_titles.py` 一次性修复脚本（已扫到 2 本污染 metadata.json）
- 新增 `backend/tests/test_book_name_inference.py`（4 个测试）
- 现状：现有 4146 章书已落盘数据**不受影响**；之前被错写 title 的 metadata.json 由兜底自动 fallback + 一次性脚本手动刷

---

## 核心机制演进主线

| 机制 | 雏形期（3月） | PyQt6/PySide6 期（4-7月） | Web 版（8月） |
|---|---|---|---|
| 分析引擎 | 单块顺序 + knowledge.json | 串行预热 + 分片/流式并发 + 失败 3 轮补跑 | 同上 + 快照隔离（并发正确性） |
| 记忆 | timeline + 压缩弧光 | 结构化滚动总结（里程碑/范式层/因果链/势头） | 同上 + 断号容错 + KV cache 前缀优化 |
| 伏笔 | 无 | 回收机制 → 伏笔账本（06-16/06-17） | **50 类分类体系**（源头约束+映射兜底）+ 总表过滤 |
| 最终总结 | 无 | 分批卷摘要+报告（05-17）→ 4 阶段（调和/复检/风格/报告） | 4 阶段 + **完整断点续跑** + 独立模型/思考选择 |
| 容错 | 温度退火 | + 指数退避 + 7 层 JSON 容错 + FailureLogger | + 漏引号定向修复 + 重试成本透明 |
| GUI/形态 | tkinter | PyQt6 → PySide6（Fluent 主题） | **FastAPI + Vue3 + pywebview** |
| 队列 | 无 | 无（单书）→ QueueManager（07-26） | 队列服务 + 自动总结 + 断点恢复 |

## 实际战绩（可复现的里程碑结果）

- 《三侠五义》121 章：5/17 并行模式半小时全书分析 + 最终总结报告（PyQt6 版）
- 5 本书批量风格对比（6/1）：三侠五义/圣墟/大王饶命/永夜君王/超神机械师
- 《卡徒》514 章队列分析（7 月底，block_size=2 断点续跑验证）
- 《北宋穿越指南》497 章（8/9）：伏笔 50 类归一化全量映射 1663 种 type、0 幻觉、时间线 2786 条
- 《白首妖师》500 章（8/9）：新约束 prompt 首战（48 种 type 全合法），最终报告经切换 deepseek-v4-flash 一次通过
- 归档书目：87+ 本（微型集群/分析结果），覆盖玄幻/武侠/都市/科幻等题材
