---
doc_type: plan
status: draft
---

# 思考模式状态提示 + 总结模型探测补齐

| 字段 | 值 |
|---|---|
| **日期** | 2026-09-29 |
| **目标** | ①不点探测也能看出当前思考模式"默认 = 可能开启"的风险；②总结模型的思考模式获得与主模型同级的探测能力 |
| **范围** | `frontend/src/utils/settingsForm.ts`、`SettingsPage.vue`、`SummaryPage.vue`、`client.ts`、`backend/api/routes_settings.py` |
| **不做** | 不改后端 `{}` 的语义（仍 = 跟随端点默认）；不做保存时自动探测；不做 Anthropic budget_tokens UI |

---

## 0. 背景与现状复核

**事故源头**（agent.md §10.18，2026-08-26）：M3 的 chat/completions 端点
默认开 thinking，`thinking_mode = {}` = 跟随端点默认 = **开**——用户当时
以为是关，30K prompt 连续 6 次 230s 硬超时。

**10.19 已落地**（复核确认，比最初评估的缺口小）：

- `SettingsPage.vue:214-219`：4 预设下拉（`{}` 自动 / thinking:disabled / reasoning_effort:none / enable_thinking:false）
- `SettingsPage.vue:265-291`：`runProbeThinking` + 探测命中自动填入下拉（`:284`）+ 动态选项
- `SettingsPage.vue:398-405`：基线 thinking 痕迹 ⚠ details 面板（7 维分项）
- `routes_settings.py:67-84`：`POST /api/settings/probe-thinking`，但**固定探测 `config.api.model`**
- `SummaryPage.vue:262-264`：总结思考模式已是同款 4 预设下拉，**但无探测按钮**

**复核后真实缺口（本 plan 范围）**：

- **G1 无探测时的状态提示缺失**：`{}` 在两页分别显示"自动（默认）"
  （SettingsPage:215）/ "自动（跟随全局设置）"（SummaryPage:116）——
  不点探测按钮，用户看不到"默认 = 可能开 thinking"警示；显式禁用项
  也没有"✓ 已禁用"的正反馈。10.18 的"未来加固 ①"只完成了一半
- **G2 总结模型探测缺失**：`summary_model` 复用同一 base_url/api_key
  （architecture.md"总结专用模型"），完全具备探测条件，但端点没有
  model 参数、页面没有按钮——10.18 事故场景（重型总结任务卡死）恰恰
  最需要它

## 1. 设计决策

### D1：状态推断做成共享纯函数 `inferThinkingState`

`frontend/src/utils/settingsForm.ts`（`stringifyThinkingMode` 同处，:55）新增：

```ts
export type ThinkingState = 'disabled' | 'default' | 'custom'
export function inferThinkingState(tm: Record<string, unknown> | null | undefined): ThinkingState
```

判定规则（与后端 llm_client 的实际消费语义对齐）：

| 输入 | 返回 | UI 文案 |
|---|---|---|
| `{}` / undefined / null | `default` | ⚠ 跟随端点默认：部分模型（M3/GLM 等）的 chat/completions 端点默认开启思考链，大 prompt 可能长时间无响应——建议点「自动探测」 |
| `thinking.type === "disabled"` 或 `enable_thinking === false` 或 `reasoning_effort === "none"` | `disabled` | ✓ 已显式禁用思考链 |
| 其他任何非空 dict | `custom` | 已自定义（覆盖值原样显示，不做语义判断） |

- 纯函数 + vitest 直测，不碰组件
- 文案的"总结模型"变体由调用方传场景前缀，函数只管状态

### D2：probe 端点加可选 `model` 参数（不新增端点）

`routes_settings.py`：

- `ModelPreviewRequest` 加 `model: Optional[str] = None`
- `probe_thinking` 改用 `req.model or config.api.model`

向后兼容：旧前端不传 model 行为完全不变；`/preview/models` 不动
（模型列表接口与单模型探测语义无关）。

### D3：SummaryPage 探测复用 SettingsPage 的成熟模式，不抽共享组件

两页的"下拉 + 探测 + 命中自动应用"流程相似，但状态流不同：
SettingsPage 有 pruneEmptyStrings / 422 字段高亮；SummaryPage 有
600ms 防抖 + **版本号串行化的 GET→改→PUT**（`SummaryPage.vue:88-112`，
PUT 是全量替换，这是踩过坑的契约）。强行抽组件要同时伺候两套保存流，
收益低、回归风险高。

采用：

- SummaryPage 新增 `runProbeThinking`（复刻 SettingsPage:265-291 结构，
  两处差异：body 多带 `model: summaryModel.value`；命中写入
  `summaryThinking` 而非 config.api.thinking_mode）
- `client.ts` 的 `probeThinking` body 类型加可选 `model?: string`
- 两页的 4 预设数组**提为 `settingsForm.ts` 单一来源**
  （`THINKING_PRESETS`），删除 SettingsPage:214-219 / SummaryPage:115-120
  两份本地副本——这是本 plan 唯一的既有代码重构点，机械替换

### D4：探测按钮防呆与 SettingsPage 对齐

- `summaryModel` 为空 → 按钮 disabled（SettingsPage 同款 `!config?.api.model` 条件）
- 探测中 busy 状态 + 失败 `probeError` 显示（同款）

## 2. 任务清单

| Phase | # | 文件 | 改动 | 验收 |
|---|---|---|---|---|
| 1 | T1.1 | `frontend/src/utils/settingsForm.ts` | `inferThinkingState` + `THINKING_PRESETS` 单一来源 | vitest：三态判定（含 null/undefined/未知键/嵌套 thinking dict） |
| 1 | T1.2 | `SettingsPage.vue` / `SummaryPage.vue` | 预设数组改引 `THINKING_PRESETS`（机械替换） | 两页下拉选项不变 |
| 2 | T2.1 | `SettingsPage.vue` | 下拉旁状态提示（v-if 按 inferThinkingState 三态） | 选 `{}` 显示 ⚠；选禁用项显示 ✓；custom 中性 |
| 2 | T2.2 | `SummaryPage.vue` | 同款状态提示（文案用总结场景变体） | 同上 |
| 3 | T3.1 | `backend/api/routes_settings.py` | `ModelPreviewRequest.model` + probe 使用 `req.model or config.api.model` | 新测试 |
| 3 | T3.2 | `backend/tests/` | +2 例：model 透传（mock `llm_probe.probe_thinking_params` 断言收到的 model）；不传 model 回退主模型 | pytest 绿 |
| 4 | T4.1 | `frontend/src/api/client.ts` | `probeThinking` body 加 `model?` | 类型检查过 |
| 4 | T4.2 | `SummaryPage.vue` | `runProbeThinking`（带 model）+ 按钮 + dynamicOptions + 命中自动写入 + probeError | 手动：对 M3 端点探测，note 正确、命中项自动进下拉 |
| 5 | T5 | 全量验证 | pytest / vitest / vue-tsc + vite build | 三绿 |

## 3. 验收标准

1. 两页选 `{}` 均出现"默认可能开启"警示；选任一禁用语法出现 ✓——
   **不点探测按钮也可见**（这是与 10.19 现状的区分点）
2. SummaryPage 可对 `summary_model` 独立探测；`summary_model` 为空时按钮禁用
3. 探测端点不传 model 时行为与改前逐字节一致（旧契约锁定）
4. 旧 `config.json`（thinking_mode 为 `{}` 或既有值）加载显示不回归
5. 三绿 + 手动目验记录

## 4. 兼容性与回滚

- 后端仅加可选字段，向后兼容；前端全部是展示层 + 一个既有纯函数文件
- 风险点唯一：T1.2 预设替换动了两页既有代码——机械替换、选项逐字不变，
  vitest + 目验双保险
- 回滚：按 Phase 反向 revert 即可，Phase 间无耦合（T3 可独立回滚）

## 5. 不做（显式排除）

- 不改 `{}` 的后端语义为"禁用"（会静默改变所有存量用户行为，且部分端点
  确实需要跟随默认——如已显式关思考的 Anthropic 协议端点）
- 不做"保存设置时自动探测"（微请求也是真实扣费，必须用户主动点）
- 不做 Anthropic `thinking.budget_tokens` 的结构化 UI（自定义 JSON 已可表达）
- 不给探测加结果缓存 / 历史记录（端点切换是低频操作）
- 不抽 ThinkingModeControl 共享组件（D3 论证）

## 6. 协调注意

另一会话正在加测试。本 plan 触碰 `settingsForm.ts`（若其已有测试需对账）、
`client.ts`，实施前先 `git status` 对账；SummaryPage 当前无测试文件，
T4.2 以手动目验为准，不新建组件测试（页面级，收益低）。
