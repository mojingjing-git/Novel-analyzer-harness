---
doc_type: plan
status: done
---

# 思考模式状态提示 + 总结模型探测补齐

| 字段 | 值 |
|---|---|
| **日期** | 2026-09-29（2026-09-30 按独立复审修订） |
| **目标** | ①不点探测也能看出当前思考模式"默认 = 可能开启"的风险；②总结模型的思考模式获得与主模型同级的探测能力 |
| **范围** | `frontend/src/utils/settingsForm.ts`、`SettingsPage.vue`、`SummaryPage.vue`、`client.ts`、`backend/api/routes_settings.py` |
| **不做** | 不改后端 `{}` 的语义（仍 = 跟随端点默认 / 总结场景回退全局）；不做保存时自动探测；不做 Anthropic budget_tokens UI |

---

## 0. 背景与现状复核

**事故源头**（agent.md §10.18，2026-08-26）：M3 的 chat/completions 端点
默认开 thinking，`thinking_mode = {}` = 跟随端点默认 = **开**——用户当时
以为是关，30K prompt 连续 6 次 230s 硬超时。

**10.19 已落地**（复审逐条核实属实）：

- `SettingsPage.vue:214-219`：4 预设下拉（`{}` 自动（默认） / thinking:disabled / reasoning_effort:none / enable_thinking:false）
- `SettingsPage.vue:265-291`：`runProbeThinking` + 探测命中自动填入下拉（`:284`）+ 动态选项
- `SettingsPage.vue:398-411`：基线 thinking 痕迹 ⚠ details 面板（7 维分项）
- `routes_settings.py:67-84`：`POST /api/settings/probe-thinking`，但**固定探测 `config.api.model`（:82）**
- `SummaryPage.vue:262-264`：总结思考模式已是同款 4 预设下拉（`{}` 标"自动（跟随全局设置）"），**但无探测按钮**

**语义前提（复审补核实，决定本文多处设计）**：总结侧的 `{}` 与主模型侧的
`{}` **语义不同**——`final_summary.py:558` 为
`thinking_mode = summary_thinking_mode or thinking_mode`：总结的 `{}`
（falsy）**回退到全局 thinking_mode**，而主模型的 `{}` 直接跟随端点默认。
这是两页首项标签不同的根本原因，不是文案漂移。

**复审后的真实缺口**：

- **G1**：两页均无"不点探测"的状态提示；`{}` 的"默认 = 可能开 thinking"
  风险与"已显式禁用"的正反馈都不可见（10.18 未来加固①只完成一半）
- **G2**：总结模型无探测；`summary_model` 复用同一 base_url/api_key，
  `llm_probe.probe_thinking_params(base_url, api_key, model, provider)`
  第 3 参就是 model，端点只差一个可选参数

## 1. 设计决策

### D1：状态推断做成共享纯函数 `inferThinkingState`

`frontend/src/utils/settingsForm.ts`（`stringifyThinkingMode:55` 同处；
**无现成状态推断函数可复用**，复审确认）新增：

```ts
export type ThinkingState = 'disabled' | 'default' | 'custom'
export function inferThinkingState(tm: Record<string, unknown> | null | undefined): ThinkingState
```

判定规则（覆盖探测动态选项可能写入的**全部五种**禁用语法——探测候选含
`chat_template_kwargs` / `reasoning.effort`，命中会自动填入下拉，
漏判会把"探测实测有效"的配置显示成 custom）：

| 输入 | 返回 | UI 文案（主模型场景） |
|---|---|---|
| `{}` / undefined / null | `default` | ⚠ 跟随端点默认：部分模型（M3/GLM 等）的 chat/completions 端点默认开启思考链，大 prompt 可能长时间无响应——建议点「自动探测」 |
| `thinking.type === "disabled"` ／ `enable_thinking === false` ／ `reasoning_effort === "none"` ／ `chat_template_kwargs?.enable_thinking === false` ／ `reasoning?.effort === "none"` | `disabled` | ✓ 已显式禁用思考链 |
| `thinking.type === "enabled"` | `custom`（信息性文案"已显式启用"可选，不做硬性区分） | 已自定义 |
| 其他任何非空 dict | `custom` | 已自定义 |

**Caveat（写入文案注记）**：provider=anthropic 时 llm_client 只放行 `thinking`
键（llm_client.py:209-211），`enable_thinking`/`reasoning_effort` 会被静默
丢弃、实际靠 Anthropic 端点默认（原生关）。probe 端点对 anthropic 已短路
返回 `thinking:disabled`（llm_probe.py:99-113）故写回值安全；`disabled`
文案加一行小字"Anthropic 协议下仅 thinking 键生效"。

**调用点转换**：`SummaryPage` 的 `summaryThinking` 是字符串 ref（:73），
调用形如 `inferThinkingState(jsonStringToDict(summaryThinking.value))`
（`jsonStringToDict` 已在 :10 import）；命中写回用 `dictToJsonString`。

### D2：probe 端点加可选 `model` 参数（不新增端点）

`routes_settings.py`：

- `ModelPreviewRequest` 加 `model: Optional[str] = None`（全仓仅此文件引用，
  `/preview/models` 与旧前端零影响；不传时 `req.model or config.api.model`
  逐字节等价现行为）
- `probe_thinking` 改用 `req.model or config.api.model`

### D3：SummaryPage 探测复刻 SettingsPage 模式；预设数组共享 value、首项标签按页传入

**不抽组件**（与 §6.6/10.16 useBookScope 暂缓先例一致）：两页保存流不同
（SettingsPage 有 pruneEmptyStrings/422 高亮；SummaryPage 有 600ms 防抖 +
版本号串行化 GET→改→PUT，`:88-112`），强行抽象回归风险大于收益。

**预设数组不能完全合并**（复审阻塞项）：两页首项 `{}` 的标签差异是语义性的
（见 §0 语义前提），统一标签会误导（给 SettingsPage 的 `{}` 标"跟随全局"
是错的——它自己就是全局）。方案：

- `settingsForm.ts` 导出 `THINKING_PRESET_VALUES`（value + 后三项技术标签
  单一来源）+ `makeThinkingPresets(autoLabel: string)`（首项标签由调用页传：
  SettingsPage 传 `'自动（跟随端点默认）'`，SummaryPage 传 `'自动（跟随全局设置）'`）
- 两页删本地数组改调用；后三项标签本就逐字相同，无行为变化

SummaryPage 新增 `runProbeThinking`：复刻 SettingsPage:265-291 结构，
差异两处——body 组装照 `:272-274` 的 trim 模式**条件包含**
`model: summaryModel.value`；命中写入 `summaryThinking`（经
`dictToJsonString`）。`summaryModel` 为空 → 按钮 disabled。

### D4：SummaryPage 的 `{}` 状态提示必须两级组合显示（防误报）

总结侧 `{}` 实际消费全局值（`final_summary.py:558` 的 `or` 回退链）。
若全局已显式禁用、总结留 `{}`（10.18 后的常见配置），单级提示
"⚠ 跟随端点默认可能开启"就是**假阳性**——恰是本 plan 要消灭的错误方向。

T2.2 的 `{}` 文案定为两级组合：

```
跟随全局设置（当前全局：{inferThinkingState(cfg.api.thinking_mode) 对应文案}）
```

`loadSummaryParams` 的响应里 `cfg.api.thinking_mode` 已可得（:124），无技术障碍。

## 2. 任务清单

| Phase | # | 文件 | 改动 | 验收 |
|---|---|---|---|---|
| 1 | T1.1 | `frontend/src/utils/settingsForm.ts` | `inferThinkingState`（五语法判定 + caveat 常量）+ `THINKING_PRESET_VALUES` + `makeThinkingPresets` | vitest 落 `settingsForm.test.ts`（已存在，紧随既有"思考模式 JSON 转换"describe 追加；**utils 层有 100% coverage 基线（10.29），新导出必须带测试**）：三态判定含五禁用语法、null/undefined、未知键、嵌套 dict |
| 1 | T1.2 | `SettingsPage.vue` / `SummaryPage.vue` | 本地预设数组改 `makeThinkingPresets(...)`（首项标签按页传入） | 两页下拉**value 集合不变**；后三项标签逐字不变；首项标签保持各自原文 |
| 2 | T2.1 | `SettingsPage.vue` | 下拉旁状态提示（v-if 按 inferThinkingState 三态；样式用 `glass-tinted-*` / `--win-*`，§9.4） | 选 `{}` 显示 ⚠；选五种禁用语法任一显示 ✓；custom 中性 |
| 2 | T2.2 | `SummaryPage.vue` | 同款提示，`{}` 用 D4 两级组合文案；含 Anthropic caveat | 全局禁用 + 总结 `{}` → 不出现 ⚠ 误报 |
| 3 | T3.1 | `backend/api/routes_settings.py` | `ModelPreviewRequest.model` + probe 使用 `req.model or config.api.model` | 新测试 |
| 3 | T3.2 | `backend/tests/test_settings_probe_route.py`（**新建**） | TestClient + patch `backend.core.llm_probe.probe_thinking_params`（routes_settings.py:71 是函数内 import，patch 模块属性即生效）：①model 透传断言；②不传回退主模型。不挂 `test_anthropic_provider.py`（那是 probe 函数级测试，职责不符） | pytest 绿 |
| 4 | T4.1 | `frontend/src/api/client.ts` | `probeThinking` body 类型加 `model?` | 类型检查过 |
| 4 | T4.2 | `SummaryPage.vue` + `SummaryPage.test.ts` | `runProbeThinking` + 按钮 + dynamicOptions + 命中自动写入 + probeError；**组件测试**：既有 `SummaryPage.test.ts` 的 `vi.mock('../api/client')` 工厂**缺 `probeThinking`，必须补 mock**（否则探测用例 `api.probeThinking is not a function`），断言 body 含 model、命中自动写入 summaryThinking | 组件测试 + 手动目验双保险 |
| 5 | T5 | 全量验证 + 文档 | pytest / vitest / vue-tsc + vite build；按 §9.6 更新 agent.md（新 10.x 条目，编号以执行日 agent.md 现状为准——10.29 已被占用） | 三绿（记录实跑数字） |

## 3. 验收标准

1. 两页选 `{}` 均有风险提示、选任一**五种**禁用语法均有 ✓——不点探测按钮也可见
2. SummaryPage：全局禁用 + 总结 `{}` 时**不误报**（D4 两级组合生效）
3. SummaryPage 可对 `summary_model` 独立探测；为空时按钮禁用
4. probe 端点不传 model 时行为与改前逐字节一致（旧契约锁定）
5. 旧 `config.json`（thinking_mode 为 `{}` 或既有值）加载显示不回归
6. 三绿 + 手动目验记录

## 4. 兼容性与回滚

- 后端仅加可选字段，向后兼容；前端是展示层 + 一个既有纯函数文件
- 风险点唯一：T1.2 预设替换动两页既有代码——按 §9.2 用 `git diff -w` 核对
  真实改动，value 集合与后三项标签逐字比对
- 回滚：按 Phase 反向 revert，Phase 间无耦合（T3 可独立回滚）

## 5. 不做（显式排除）

- 不改 `{}` 的后端语义（主模型侧会静默改变存量用户行为；总结侧 `or` 回退链
  是既有契约）
- 不做"保存设置时自动探测"（微请求也是真实扣费）
- 不做 Anthropic `thinking.budget_tokens` 结构化 UI
- 不给探测加结果缓存 / 历史记录
- 不抽 ThinkingModeControl 共享组件（D3 论证）
- 不按 provider 动态切换提示体系（Anthropic caveat 以一行小字表达，够用）

## 6. 协调注意

另一会话的测试加强已落地（agent.md 10.29：84→225 用例 + coverage 基建，
新增 Queue/Summary/Settings/Style/Splitter/Workspace 六页组件测试）。
本 plan 的前提因此更新：SummaryPage/SettingsPage **已有组件测试**，
probe 相关改动必须同步补测试 mock 与用例；utils 新函数必须带测试
（coverage 基线）。实施前 `git status` + 对相关文件 `git diff` 对账。

---

## 7. 独立复审记录（2026-09-30）

复审结论：**修订后通过**。3 阻塞 + 3 重要 + 5 建议，阻塞/重要项已全部并入上文：

- 阻塞 1（disabled 三语法对探测动态选项不穷尽，验收 1 自相矛盾）→ D1 判定
  扩为五语法（补 `chat_template_kwargs.enable_thinking` / `reasoning.effort`），T1.1 用例同步
- 阻塞 2（预设数组合并会改首项标签，且标签差异是语义性的）→ D3 改为
  value/技术标签共享 + 首项标签按页传入
- 阻塞 3（SummaryPage `{}` 在全局已禁用时误报 ⚠）→ D4 两级组合文案
- 重要 1（另一会话已落地页面测试，"SummaryPage 无测试文件"前提失效；
  SummaryPage.test.ts 的 mock 缺 probeThinking）→ §0/§6/T4.2 更新
- 重要 2（后端测试落点未指名）→ T3.2 指名新建 test_settings_probe_route.py
- 重要 3（§9.5/§9.6/§9.4 对账缺失）→ 特此声明 §9.5 例外：probeThinking 加
  model 参数与 SummaryPage 新增 API 调用为任务本体要求的功能变更；T5 加
  agent.md 更新；T2.1 样式约束写入 §9.4 依据
- 建议采纳：S1 调用点转换写入 D1；S2 model 条件包含写入 D3；S3 Anthropic
  caveat 写入 D1；S4 显式启用文案作为可选项记入 D1；S5 `git diff -w` 写入 §4
