/**
 * SettingsPage 表单处理的纯函数层（从组件 save()/分类勾选/思考模式 原样抽取）。
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

// ============ 思考模式状态推断（2026-09-30，M3 事故教训 agent.md 10.18）============

export type ThinkingState = 'disabled' | 'default' | 'custom'

/**
 * 从 thinking_mode dict 推断当前状态（SettingsPage / SummaryPage 下拉旁的状态提示）。
 *
 * 覆盖五种禁用语法：三种顶层预设 + 探测动态选项可能写入的两种嵌套写法
 * （chat_template_kwargs.enable_thinking / reasoning.effort，llm_probe 候选）——
 * 漏判会把"探测实测有效"的配置显示成 custom，G1 的禁用正反馈就落空了。
 *
 * `{}` 的语义按调用方区分：主模型 = 跟随端点默认（llm_client 不注入 extra_body），
 * 总结 = 回退全局 thinking_mode（final_summary.py 的 `or` 回退链）——"default"
 * 是否危险由调用方文案表达，本函数只管状态。
 */
export function inferThinkingState(tm: Record<string, unknown> | null | undefined): ThinkingState {
  if (!tm || Object.keys(tm).length === 0) return 'default'
  const isObj = (v: unknown): v is Record<string, unknown> =>
    !!v && typeof v === 'object' && !Array.isArray(v)
  const thinking = tm.thinking
  if (isObj(thinking) && thinking.type === 'disabled') return 'disabled'
  if (tm.enable_thinking === false) return 'disabled'
  if (tm.reasoning_effort === 'none') return 'disabled'
  const ctk = tm.chat_template_kwargs
  if (isObj(ctk) && ctk.enable_thinking === false) return 'disabled'
  const reasoning = tm.reasoning
  if (isObj(reasoning) && reasoning.effort === 'none') return 'disabled'
  return 'custom'
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
