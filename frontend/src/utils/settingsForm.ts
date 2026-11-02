/**
 * SettingsPage 表单处理的纯函数层（对应组件的 save()/分类勾选/思考模式）。
 * 语义锁定：空字符串剪除（v-model.number 清空残留）、422 校验错误按 loc 末元素映射红框。
 */

/** 递归删除空字符串字段（语义即"恢复该项默认"）。数组与 null 不递归、不删除 */
export function pruneEmptyStrings(obj: Record<string, unknown>): void {
  for (const k of Object.keys(obj)) {
    const v = obj[k]
    if (v === '') { delete obj[k]; continue }
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      pruneEmptyStrings(v as Record<string, unknown>)
    }
  }
}

export interface ValidationItem {
  loc?: (string | number)[]
  msg?: string
}

/**
 * 422 校验错误 → 字段级红框映射。
 * FastAPI detail 为数组，取 loc 末元素作字段键；无 loc 记 '?'；无 msg 用默认文案。
 */
export function mapFieldErrors(
  detail: unknown,
): { fieldErrors: Record<string, string>; msgs: string[] } {
  const fieldErrors: Record<string, string> = {}
  const msgs: string[] = []
  for (const item of (Array.isArray(detail) ? detail : []) as ValidationItem[]) {
    const leaf = item.loc && item.loc.length ? String(item.loc[item.loc.length - 1]) : '?'
    const m = item.msg || '校验未通过'
    fieldErrors[leaf] = m
    msgs.push(`${leaf}: ${m}`)
  }
  return { fieldErrors, msgs }
}

// ============ 伏笔分类勾选 ============

/** 勾选/取消一个分类，返回新数组（Set 去重语义与组件一致） */
export function toggleCategoryIn(list: string[] | undefined, name: string): string[] {
  const set = new Set(list ?? [])
  if (set.has(name)) set.delete(name); else set.add(name)
  return Array.from(set)
}

/** 清空全部时的兜底：保留"其他" */
export const CATEGORY_CLEAR_FALLBACK = ['其他']

// ============ 思考模式 JSON 字符串 ↔ dict ============

/** 空 dict / 缺失 → '{}'（表示"自动/跟随默认"）；非空原样序列化 */
export function stringifyThinkingMode(tm: Record<string, unknown> | undefined | null): string {
  if (!tm || Object.keys(tm).length === 0) return '{}'
  return JSON.stringify(tm)
}

/** 解析思考模式选项值；坏 JSON → null（调用方保持原值不变，与 try/catch 空捕获语义一致） */
export function parseThinkingMode(val: string): Record<string, unknown> | null {
  if (val === '{}') return {}
  try { return JSON.parse(val) } catch { return null }
}

// ============ 思考模式状态推断 ============

export type ThinkingState =
  | 'disabled'
  | 'default'
  | 'depth_low'
  | 'depth_medium'
  | 'depth_high'
  | 'budget'
  | 'custom'

/**
 * 从 thinking_mode dict 推断当前状态（SettingsPage / SummaryPage 下拉旁的状态提示）。
 *
 * 核心逻辑：深度优先（Depth Overrides Disable）——
 * 若配置了思考深度（low/medium/high/budget），优先提示受控深度，避免用户误解。
 *
 * `{}` 的语义按调用方区分：主模型 = 跟随端点默认（llm_client 不注入 extra_body），
 * 总结 = 回退全局 thinking_mode（final_summary.py 的 `or` 回退链）——"default"
 */
export function inferThinkingState(tm: Record<string, unknown> | null | undefined): ThinkingState {
  if (!tm || Object.keys(tm).length === 0) return 'default'
  const isObj = (v: unknown): v is Record<string, unknown> =>
    !!v && typeof v === 'object' && !Array.isArray(v)

  // 1. 深度优先识别（用户强调：思考深度优先度高，防崩塌）
  if (tm.reasoning_effort === 'low') return 'depth_low'
  if (tm.reasoning_effort === 'medium') return 'depth_medium'
  if (tm.reasoning_effort === 'high' || tm.reasoning_effort === 'max') return 'depth_high'

  const thinking = tm.thinking
  if (isObj(thinking)) {
    if (thinking.type === 'enabled' && typeof thinking.budget_tokens === 'number') {
      return 'budget'
    }
    if (thinking.type === 'disabled') return 'disabled'
  }

  // 2. 禁用模式识别
  if (tm.enable_thinking === false) return 'disabled'
  if (tm.reasoning_effort === 'none') return 'disabled'
  const ctk = tm.chat_template_kwargs
  if (isObj(ctk) && ctk.enable_thinking === false) return 'disabled'
  const reasoning = tm.reasoning
  if (isObj(reasoning)) {
    if (reasoning.effort === 'none') return 'disabled'
    if (reasoning.effort === 'low') return 'depth_low'
    if (reasoning.effort === 'medium') return 'depth_medium'
    if (reasoning.effort === 'high' || reasoning.effort === 'max') return 'depth_high'
  }

  return 'custom'
}

/** 思考深度预设选项（高优先级，避免完全关闭导致输出质量崩塌） */
export const THINKING_DEPTH_PRESETS: { value: string; label: string }[] = [
  { value: '', label: '默认（不限制深度 / 跟随模型）' },
  { value: 'low', label: '浅度思考 (low) - 推荐！极速防崩' },
  { value: 'medium', label: '中度思考 (medium) - 平衡质量' },
  { value: 'high', label: '深度思考 (high) - 深度推理' },
  { value: 'budget_1024', label: '预算限制: 1024 tok (Claude 3.7)' },
]

/** 完全禁用思考预设选项（低优先级，仅在深度为默认时生效） */
export function makeThinkingDisablePresets(autoLabel: string): { value: string; label: string }[] {
  return [
    { value: '', label: autoLabel },
    { value: 'thinking_disabled', label: 'thinking: disabled (MiniMax/GLM)' },
    { value: 'reasoning_effort_none', label: 'reasoning_effort: none (OpenAI)' },
    { value: 'enable_thinking_false', label: 'enable_thinking: false (DeepSeek/Qwen)' },
    { value: 'chat_template_kwargs', label: 'chat_template_kwargs: false (vLLM)' },
  ]
}

/**
 * 两个开关合成最终 thinking_mode dict
 *
 * 核心原则：思考深度优先（Depth Overrides Disable）！
 * 当指定了思考深度（如 low）时，绝对不写入 disabled 参数，
 * 避免推理模型因强行关闭思考导致输出质量崩塌。
 */
export function composeThinkingMode(depth: string, disableParam: string, customJson?: string): Record<string, unknown> {
  if (customJson && customJson !== '{}') {
    try {
      const parsed = JSON.parse(customJson)
      if (parsed && typeof parsed === 'object') {
        if (!depth) return parsed
      }
    } catch {
      // ignore
    }
  }

  // 1. 深度优先
  if (depth === 'low') return { reasoning_effort: 'low' }
  if (depth === 'medium') return { reasoning_effort: 'medium' }
  if (depth === 'high') return { reasoning_effort: 'high' }
  if (depth === 'max') return { reasoning_effort: 'max' }
  if (depth === 'budget_1024') return { thinking: { type: 'enabled', budget_tokens: 1024 } }
  if (depth === 'budget_2048') return { thinking: { type: 'enabled', budget_tokens: 2048 } }

  // 2. 深度为默认时，读取禁用设置
  if (disableParam === 'thinking_disabled') return { thinking: { type: 'disabled' } }
  if (disableParam === 'reasoning_effort_none') return { reasoning_effort: 'none' }
  if (disableParam === 'enable_thinking_false') return { enable_thinking: false }
  if (disableParam === 'chat_template_kwargs') return { chat_template_kwargs: { enable_thinking: false } }

  return {}
}

/**
 * 从已有的 thinking_mode dict 反解回深度与禁用开关状态
 */
export function decomposeThinkingMode(tm: Record<string, unknown> | null | undefined): {
  depth: string
  disableParam: string
  isCustom: boolean
} {
  if (!tm || Object.keys(tm).length === 0) {
    return { depth: '', disableParam: '', isCustom: false }
  }
  const isObj = (v: unknown): v is Record<string, unknown> =>
    !!v && typeof v === 'object' && !Array.isArray(v)

  // 1. 检查深度
  if (tm.reasoning_effort === 'low') return { depth: 'low', disableParam: '', isCustom: false }
  if (tm.reasoning_effort === 'medium') return { depth: 'medium', disableParam: '', isCustom: false }
  if (tm.reasoning_effort === 'high') return { depth: 'high', disableParam: '', isCustom: false }
  if (tm.reasoning_effort === 'max') return { depth: 'max', disableParam: '', isCustom: false }

  const thinking = tm.thinking
  if (isObj(thinking)) {
    if (thinking.type === 'enabled' && thinking.budget_tokens === 1024) {
      return { depth: 'budget_1024', disableParam: '', isCustom: false }
    }
    if (thinking.type === 'enabled' && thinking.budget_tokens === 2048) {
      return { depth: 'budget_2048', disableParam: '', isCustom: false }
    }
    if (thinking.type === 'disabled') {
      return { depth: '', disableParam: 'thinking_disabled', isCustom: false }
    }
  }

  // 2. 检查禁用
  if (tm.reasoning_effort === 'none') return { depth: '', disableParam: 'reasoning_effort_none', isCustom: false }
  if (tm.enable_thinking === false) return { depth: '', disableParam: 'enable_thinking_false', isCustom: false }
  const ctk = tm.chat_template_kwargs
  if (isObj(ctk) && ctk.enable_thinking === false) {
    return { depth: '', disableParam: 'chat_template_kwargs', isCustom: false }
  }
  const reasoning = tm.reasoning
  if (isObj(reasoning)) {
    if (reasoning.effort === 'none') return { depth: '', disableParam: 'reasoning_effort_none', isCustom: false }
    if (reasoning.effort === 'low') return { depth: 'low', disableParam: '', isCustom: false }
    if (reasoning.effort === 'medium') return { depth: 'medium', disableParam: '', isCustom: false }
    if (reasoning.effort === 'high' || reasoning.effort === 'max') return { depth: 'high', disableParam: '', isCustom: false }
  }

  return { depth: '', disableParam: '', isCustom: true }
}

/**
 * 两页共享的思考模式预设：value + 技术标签单一来源。
 * 首项 label 必须按页传入——`{}` 在主模型侧（跟随端点默认）与总结侧
 * （跟随全局设置）语义不同，标签统一会误导（SettingsPage 自己就是全局）。
 */
export function makeThinkingPresets(autoLabel: string): { value: string; label: string }[] {
  return [
    { value: '{}', label: autoLabel },
    { value: '{"thinking":{"type":"disabled"}}', label: 'thinking: disabled' },
    { value: '{"reasoning_effort":"none"}', label: 'reasoning_effort: none' },
    { value: '{"enable_thinking":false}', label: 'enable_thinking: false' },
  ]
}

/** Anthropic 协议 caveat：llm_client 对 Anthropic 载荷只放行 thinking 键，
 * 其余禁用语法被静默丢弃、实际靠端点默认（Anthropic 原生默认关） */
export const THINKING_ANTHROPIC_CAVEAT = 'Anthropic 协议下仅 thinking 键生效，其余禁用语法会被静默忽略'
