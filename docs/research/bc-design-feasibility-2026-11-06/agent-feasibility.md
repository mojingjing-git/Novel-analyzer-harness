# 四项小说分析器改进的代码可行性结论

检查日期：2026-11-06  
范围：只检查现有数据结构、主流程和契约测试入口；不评价现有分析质量，不打开小说或历史案例，不调用模型与外网，不读取 `config.json`。真实案例的具体判断以同目录 `validation-plan.md` 为主。

## 结论

四项均可落地，但现有能力并不等于新方案已经实现：

- `claims_v2` 能逐片段做证据关系和叙事框架校验，但片段由标点确定性切分，源码明确注明“不是语义原子”（`backend/common/claim_evidence_v2.py:19-41`）。
- `closure.py` 已有可复用的 `AND/OR/UNCERTAIN` 关闭逻辑和原文证据定位（`backend/common/closure.py:26-132`），但目前主要服务验收/审计，未成为运行时伏笔账本的一部分。
- foreshadow ledger 已保存单个 `question`、`answer_criteria`、原文锚点、最终状态和 `resolution_quality`，但没有逐章证据事件、语义子承诺、别名表或反向候选索引（`backend/utils/foreshadow_ledger.py:38-82`）。
- 当前 `recheck_v2` 是可靠的“旧问题向未来找证据”基线：从锚点开始，只搜索锚点之后的内容（`backend/services/recheck_v2.py:323-423`，`backend/core/evidence.py:269-360`）。没有“新揭晓反向找旧问题”的索引。
- 生产默认值与文档存在一个需注意的偏差：代码中 `recheck_v2_enabled=True`（`backend/config/settings.py:201`），而 C 按需审计默认关闭（`backend/config/settings.py:244-248`）。`agent.md:384` 关于 V2 默认关闭的描述已过时。

## 四项对比

| 方向 | 现有程度与缺口 | 最小方案 | 工作规模（定性） | 成本新增点 | 验证结果/未验证项 |
|---|---|---|---|---|---|
| 1. 复杂伏笔拆成语义子问题/承诺，分别维护未回答、部分回答、已回答 | **中低，契约可复用。** 当前每项只有一个 `question`、一个 `answer_criteria`；重审只有整题结果。`claims_v2` 的 `supported/partial/contradicted/insufficient` 可用于片段判定，但 `answer_units()` 按标点切分，不能生成或稳定标识用户认可的语义子目标。closure 的 branches 是关闭条件分支，不是独立子问题状态。 | 在 foreshadow 项下新增不可变的 `promises[]`，每项保存 `promise_id/text/source_quote/source_chapter/group_key`；另用 append-only 的 `promise_events[]` 记录每次证据后的 `unanswered/partial/answered/refuted`。父题关闭条件仍使用原 `answer_criteria` 和 `AND/OR`，子目标状态只展示进展，不能暗中改写父条件。 | 中。数据结构、提示词输出、状态投影和 UI/导出需一起改；无需重写重审协议。 | 新分析可在现有章节抽取调用中扩展输出，理论上不增加调用数，但会增加输出 token、解析失败和重试。旧结果回填必须调用模型；纯规则只能做不稳定的标点切分，不能宣称语义可靠。 | 离线探针证明一句无标点文本“推测 + 计划 + 结果未交代”仍只生成 1 个 `claims_v2` 单元。可复用关系判定已证实；语义子目标生成、父子条件实际隔离、旧结果回填尚未验证。`fs_006` 适合作为后续旁路案例；`fs_009` 要求原 OR 保持 OR。 |
| 2. 每章增量证据账本，区分人物猜测、已证实、被推翻、补充信息，并保留演变 | **低到中。** 当前 `KnowledgeBase.verified_facts` 只是去重后的字符串列表，并会按上限裁剪（`backend/models/knowledge.py:20-23`，`backend/core/knowledge_base.py:451-517`）；`ForeshadowItem.evidence_chapters` 只有章节号，`notes` 无固定结构。`recheck_verdicts.json` 是候选级重审明细，每候选当前终态会去重，并不是逐章事件历史（`backend/services/recheck_v2.py:180-216`）。`claims_v2` 的 attribution/world/modality/realization 可辅助区分归属和计划/完成，但没有“猜测/证实/推翻/补充”的账本事件。 | 新增版本化 append-only `claim_events.json` 或 `.jsonl`：`event_id, foreshadow_id, chapter, evidence_id, quote, subject, claim_text, kind, frame, observed_at`；用 `supersedes_event_id` 表达演变，最终状态由事件投影，禁止覆盖旧事件。 | 大。需要保证章内顺序、去重、证据定位和可重建投影；提示词、落盘、API/导出都会受影响。 | 新书若把分类并入现有章节抽取调用，可避免新增调用数，但 prompt/输出 token 增加。旧书不能可靠事后猜测归属，回填或补标需要额外模型调用；否则只能保留 `unknown`。 | 代码检查确认这是明确缺口。未做模型分类探针。高风险是复用摘要把“人物推测”误归为叙事事实；`game/fs_003` 的第 6 章“伊芙有了某种推测”应作为必须保留的归属边界。 |
| 3. 双向关联：旧问题向前找答案，新揭晓反向找旧问题，并支持别名/代称 | **部分具备。** 向前搜索已存在，且有原文 ID、精确引用、反向证据搜索和实体归并的保护。反向索引、开放问题别名表和“新事件→旧问题”候选流程不存在。`entity_key` 通常取 `motif_key`，无 motif 时退化为 `clue:<id>`（`backend/core/anchor.py:216-223`），不能承担人物代称或场景代称的别名解析。 | 在现有 forward recheck 外增加：开放问题别名表、轻量倒排索引、`candidate_links[]`。新证据先产生候选，再由现有重审契约核定同一指涉；候选链接分别标 `context_only/possible_answer/verified_answer`。所有“登记前证据”只可作背景，不得计入登记后回答。 | 中到大。主要是候选检索、别名生命周期、误连治理和持久化；现有证据验证器可直接复用。 | 反向候选检索本身可用 CPU 完成，无模型调用；别名抽取可并入章节调用。候选增多会把额外判断成本带入现有重审调用；未命中的候选不产生费用，低置信别名歧义可能需要额外调用。 | 离线探针确认 `chapter <= anchor_chapter` 会产生 `evidence_not_after_anchor`，但它是软信号而非硬拒绝，因此反向关联必须显式区分“登记前已知”和“登记后回收”。`hist/fs_011`（第 12 章登记、第 11 章计划证据）正是这个边界。未验证别名覆盖率与误连率。 |
| 4. 登记问题和关闭条件绑定登记时原文，标记模型擅自扩大的要求，冻结登记标准，禁止看结局后改题 | **规则层强，运行时冻结弱。** `validate_prereg_row()` 校验 branch、`AND/OR` 和 closable；`criteria_logic()`、`c_audit_selector` 用 `prereg.criteria_source == criteria` 做全串一致性检查（`backend/common/closure.py:26-78`，`backend/common/c_claim_audit_gate.py:95-102`，`backend/common/c_audit_selector.py:125-130`）。`ForeshadowItem` 已保存 `anchor_quote/anchor_chapter`。缺口是这些字段进入 ledger 后没有不可变的 registration snapshot、内容哈希或修订链；`evaluation_corpus.freeze_plan()` 只冻结评测语料/协议，不是每道题的运行时登记（`backend/common/evaluation_corpus.py:78-143`）。 | 新增 append-only `registration_snapshot`：保存当时 `question`、完整 `criteria_source`、closure/prereg、原文 quote/span/chapter、`registered_chapter`、协议版本和 content hash。登记后只追加 revision，不原位修改。重审、反向关联和报告必须引用 snapshot；缺快照的旧项目标为 `legacy_unfrozen`，不得声称已完成事前冻结。另做确定性对照：新增 requirement 若不在原文/冻结快照中则标 `model_expansion`，不计入关闭。 | 小到中。最核心的冻结和校验可用现有字段及 closure 实现；主要工作是接入主分析流程、旧账本迁移和导出。 | 不新增模型调用。`criteria_source` 相等、哈希和 branch 引用都可由本地代码校验。旧项目若无登记时原文，只能做带 `backfill` 标记的伪冻结，不能追溯性证明。 | 离线探针确认 OR 条件仍在单个分支满足时关闭；但给 criteria 追加“且结果必须被证实”后，`validate_prereg_row()` 对 branch 子集仍返回 true。只有调用点的 `criteria_source` 全串相等检查能阻止这种扩张。说明“直接复用 closure 就自动冻结”不成立，必须在流程层补硬约束。 |

## 最小数据与兼容方案

最稳妥的兼容方式是先增加版本化 sidecar，而不是一次性重写 `foreshadow_ledger.json`：

- `foreshadow_registry.json`：不可变登记快照与修订链，服务方向 4。
- `semantic_promises.json`：子承诺定义与状态事件，服务方向 1。可后续并入 ledger schema v7。
- `claim_events.jsonl`：逐章、append-only 证据演化，服务方向 2。
- `association_aliases.json` 与 `candidate_links.jsonl`：别名和双向候选链接，服务方向 3。

现有 `ForeshadowItem.from_dict()` 会过滤未知字段（`backend/utils/foreshadow_ledger.py:66-71`）；因此新字段若直接塞进旧 JSON 会被静默丢弃。需要显式 schema 升级或 sidecar，且新写入器应同时保留旧字段供旧 UI 读取。旧项目只可建立 `derived/backfilled/legacy_unfrozen` 视图，不应改写历史结果。

所有新事件都应以 `foreshadow_id + registration_id + chapter + evidence_id + content_hash` 去重。父题关闭与子目标进展应分开保存：例如 `fs_009` 的 OR 和 `fs_006` 的多项 criteria 必须保持登记的布尔结构，子目标拆分不得把 OR 改成 AND。

## 主要风险

1. **证据归属污染：** 摘要压缩后，人物推测可能变成叙述事实；必须有来源引文和 attribution 字段。
2. **事后改题：** 只校验 branch 是否是 criteria 子串不够，必须校验完整 criteria、锚点和登记时间。
3. **登记前泄漏：** 对于反向关联，登记前原文只能提供背景，不能作为“问题被登记后又答上”的证据。当前时间检查是软信号，接入新流程时应改为确定性的分类。
4. **模型扩大要求：** 新增条件必须与冻结的原文条件逐项对照，未落原文的要求只记作 `model_expansion`。
5. **历史兼容：** 旧项目缺原始登记时刻，不能追溯性证明“未看结局前冻结”；只能显式标注为回填视图。

## 离线验证

运行方式：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python docs/research/bc-design-feasibility-2026-11-06/agent_probe.py
```

探针只导入本地纯 Python 契约，不读取配置、小说或历史结果，不调用网络。结果：

- 一句包含“推测、计划、结果未交代”的无标点答案仍被 `answer_units()` 当作 1 个单元；证明 `claims_v2` 不能替代语义子目标。
- OR prereg 在只命中一个分支时正确关闭；同一 prereg 在 criteria 末尾追加要求后，`validate_prereg_row()` 仍可返回 true；证明必须保留调用点的 `criteria_source` 全串冻结检查。
- 第 11 章证据对第 12 章锚点产生 `evidence_not_after_anchor` 软标记，第 13 章不产生；证明时序识别已有，但“登记前已知不得计作回收”仍需新流程层硬规则。
- 旧 ledger 加载会把未知的新字段过滤掉；证明新历史数据必须走显式 schema 或 sidecar。

未验证：语义子目标质量、分类准确率、别名召回/误连、逐章事件的去重与顺序、在真实运行中的成本。以上需要 `validation-plan.md` 的案例旁路验证。

## 优先级建议

**最值得先做方向 4。** 它规模最小、无需新增模型调用，并能用已有 closure/prereg/原文锚点快速接到产品路径；同时它为方向 1 的子承诺和方向 3 的反向关联提供共同的登记时间、冻结条件和来源边界。若不先固定“当时的问题与关闭条件”，后续拆题和反向找答案都可能把事后信息误算成事前承诺。

建议紧接方向 4 做方向 1，并在同一 schema 设计中一起明确父题布尔逻辑与子目标状态。方向 2 收益大但实现和数据治理最重；方向 3 应建立在冻结登记和别名表之后。
