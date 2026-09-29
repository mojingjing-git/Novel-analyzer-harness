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
