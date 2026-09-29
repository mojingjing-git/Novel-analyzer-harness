/**
 * settingsForm 纯函数测试：空串剪除 / 422 字段映射 / 分类勾选 / 思考模式 JSON 转换。
 */
import { describe, it, expect } from 'vitest'
import {
  pruneEmptyStrings, mapFieldErrors, toggleCategoryIn,
  CATEGORY_CLEAR_FALLBACK, stringifyThinkingMode, parseThinkingMode,
} from './settingsForm'

describe('settingsForm.pruneEmptyStrings（P2：v-model.number 清空残留）', () => {
  it('递归删除空字符串字段；数值 0 / false 不误删', () => {
    const cfg = {
      api: { base_url: 'https://x', summary_model: '', timeout: 0, provider: '' },
      analysis: { auto_archive: false, note: '' },
      kept: '保留非空',
    }
    pruneEmptyStrings(cfg)
    expect(cfg.api).toEqual({ base_url: 'https://x', timeout: 0 })
    expect(cfg.analysis).toEqual({ auto_archive: false })
    expect(cfg.kept).toBe('保留非空')
  })

  it('数组只作值不递归，数组内空串保留', () => {
    const cfg = { analysis: { encoding_priority: ['', 'utf-8'] }, junk: '' }
    pruneEmptyStrings(cfg)
    expect(cfg.analysis.encoding_priority).toEqual(['', 'utf-8'])
    expect('junk' in cfg).toBe(false)
  })

  it('null 字段不删除（只有空串语义是恢复默认）', () => {
    const cfg = { api: { thinking_mode: null } }
    pruneEmptyStrings(cfg as unknown as Record<string, unknown>)
    expect('thinking_mode' in cfg.api).toBe(true)
  })
})

describe('settingsForm.mapFieldErrors（422 → 红框字段映射）', () => {
  it('取 loc 末元素作字段键，msg 缺失给默认文案', () => {
    const { fieldErrors, msgs } = mapFieldErrors([
      { loc: ['body', 'api', 'base_url'], msg: 'invalid url' },
      { loc: ['body', 'analysis', 'concurrency'] },
    ])
    expect(fieldErrors).toEqual({
      base_url: 'invalid url',
      concurrency: '校验未通过',
    })
    expect(msgs).toEqual(['base_url: invalid url', 'concurrency: 校验未通过'])
  })

  it('无 loc → \'?\'；detail 非数组 → 空映射', () => {
    expect(mapFieldErrors([{ msg: 'x' }]).fieldErrors).toEqual({ '?': 'x' })
    expect(mapFieldErrors('weird')).toEqual({ fieldErrors: {}, msgs: [] })
  })
})

describe('settingsForm 分类勾选', () => {
  it('toggleCategoryIn：增删互逆，Set 去重', () => {
    expect(toggleCategoryIn(['人物', '情节'], '设定')).toEqual(['人物', '情节', '设定'])
    expect(toggleCategoryIn(['人物', '情节'], '人物')).toEqual(['情节'])
    expect(toggleCategoryIn(undefined, '人物')).toEqual(['人物'])
    expect(toggleCategoryIn(['人物', '人物'], '人物')).toEqual([])
  })

  it('清空兜底保留「其他」', () => {
    expect(CATEGORY_CLEAR_FALLBACK).toEqual(['其他'])
  })
})

describe('settingsForm 思考模式 JSON 转换', () => {
  it('stringifyThinkingMode：空/缺失 → \'{}\'（自动）', () => {
    expect(stringifyThinkingMode(undefined)).toBe('{}')
    expect(stringifyThinkingMode({})).toBe('{}')
    expect(stringifyThinkingMode({ thinking: { type: 'disabled' } })).toBe('{"thinking":{"type":"disabled"}}')
  })

  it('parseThinkingMode：\'{}\' → 空 dict；坏 JSON → null（调用方保持原值）', () => {
    expect(parseThinkingMode('{}')).toEqual({})
    expect(parseThinkingMode('{"enable_thinking":false}')).toEqual({ enable_thinking: false })
    expect(parseThinkingMode('broken')).toBeNull()
  })
})
