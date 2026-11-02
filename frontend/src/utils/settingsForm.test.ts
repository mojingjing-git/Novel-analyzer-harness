/**
 * settingsForm 纯函数测试：空串剪除 / 422 字段映射 / 分类勾选 / 思考模式 JSON 转换。
 */
import { describe, it, expect } from 'vitest'
import {
  pruneEmptyStrings, mapFieldErrors, toggleCategoryIn,
  CATEGORY_CLEAR_FALLBACK, stringifyThinkingMode, parseThinkingMode,
  inferThinkingState, makeThinkingPresets, THINKING_ANTHROPIC_CAVEAT,
  composeThinkingMode, decomposeThinkingMode, THINKING_DEPTH_PRESETS,
} from './settingsForm'

describe('settingsForm.pruneEmptyStrings：v-model.number 清空残留', () => {
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

describe('settingsForm.inferThinkingState（思考模式状态提示）', () => {
  it('`{}` / 缺失 → default（是否危险由调用方文案区分）', () => {
    expect(inferThinkingState(undefined)).toBe('default')
    expect(inferThinkingState(null)).toBe('default')
    expect(inferThinkingState({})).toBe('default')
  })

  it('深度优先：reasoning_effort low/medium/high/max 与 budget → 识别为对应深度状态', () => {
    expect(inferThinkingState({ reasoning_effort: 'low' })).toBe('depth_low')
    expect(inferThinkingState({ reasoning_effort: 'medium' })).toBe('depth_medium')
    expect(inferThinkingState({ reasoning_effort: 'high' })).toBe('depth_high')
    expect(inferThinkingState({ reasoning_effort: 'max' })).toBe('depth_high')
    expect(inferThinkingState({ thinking: { type: 'enabled', budget_tokens: 1024 } })).toBe('budget')
  })

  it('三种顶层禁用语法 → disabled', () => {
    expect(inferThinkingState({ thinking: { type: 'disabled' } })).toBe('disabled')
    expect(inferThinkingState({ enable_thinking: false })).toBe('disabled')
    expect(inferThinkingState({ reasoning_effort: 'none' })).toBe('disabled')
  })

  it('探测动态选项的两种嵌套禁用写法 → disabled（漏判会把实测有效配置显示成 custom）', () => {
    expect(inferThinkingState({ chat_template_kwargs: { enable_thinking: false } })).toBe('disabled')
    expect(inferThinkingState({ reasoning: { effort: 'none' } })).toBe('disabled')
  })

  it('显式启用 / 未知自定义 → custom（不做语义判断）', () => {
    expect(inferThinkingState({ some_vendor_key: 'x' })).toBe('custom')
  })

  it('防御：嵌套字段类型漂移不崩（LLM 输出可能是标量）', () => {
    expect(inferThinkingState({ thinking: 'disabled' })).toBe('custom')
    expect(inferThinkingState({ chat_template_kwargs: 'enable_thinking' })).toBe('custom')
    expect(inferThinkingState({ reasoning: null })).toBe('custom')
  })
})

describe('settingsForm.composeThinkingMode 与 decomposeThinkingMode（双开关合成引擎与优先级）', () => {
  it('深度优先（Depth Overrides Disable）：只要指定了深度，绝对不输出 disabled 避免质量崩塌', () => {
    // 即使 disableParam 选了 thinking_disabled，只要 depth 是 low，就输出 reasoning_effort: low
    const res = composeThinkingMode('low', 'thinking_disabled')
    expect(res).toEqual({ reasoning_effort: 'low' })
    expect('thinking' in res).toBe(false)
  })

  it('深度为默认时，禁用开关生效', () => {
    expect(composeThinkingMode('', 'thinking_disabled')).toEqual({ thinking: { type: 'disabled' } })
    expect(composeThinkingMode('', 'reasoning_effort_none')).toEqual({ reasoning_effort: 'none' })
    expect(composeThinkingMode('', 'enable_thinking_false')).toEqual({ enable_thinking: false })
  })

  it('深度与禁用皆为默认时，返回空对象 {}', () => {
    expect(composeThinkingMode('', '')).toEqual({})
  })

  it('decomposeThinkingMode 正确反解深度与禁用状态', () => {
    expect(decomposeThinkingMode({ reasoning_effort: 'low' })).toEqual({
      depth: 'low', disableParam: '', isCustom: false,
    })
    expect(decomposeThinkingMode({ thinking: { type: 'disabled' } })).toEqual({
      depth: '', disableParam: 'thinking_disabled', isCustom: false,
    })
    expect(decomposeThinkingMode({})).toEqual({
      depth: '', disableParam: '', isCustom: false,
    })
    expect(decomposeThinkingMode({ thinking: { type: 'enabled', budget_tokens: 1024 } })).toEqual({
      depth: 'budget_1024', disableParam: '', isCustom: false,
    })
    expect(decomposeThinkingMode({ other_key: 123 })).toEqual({
      depth: '', disableParam: '', isCustom: true,
    })
  })
})

describe('settingsForm.makeThinkingPresets（两页共享 value，首项 label 按页传入）', () => {
  it('value 集合与技术标签固定；首项 label 来自调用方（主模型/总结语义不同不能统一）', () => {
    const main = makeThinkingPresets('自动（跟随端点默认）')
    const summary = makeThinkingPresets('自动（跟随全局设置）')
    expect(main.map((o) => o.value)).toEqual([
      '{}',
      '{"thinking":{"type":"disabled"}}',
      '{"reasoning_effort":"none"}',
      '{"enable_thinking":false}',
    ])
    expect(main.slice(1)).toEqual(summary.slice(1))
    expect(main[0]).toEqual({ value: '{}', label: '自动（跟随端点默认）' })
    expect(summary[0]).toEqual({ value: '{}', label: '自动（跟随全局设置）' })
  })

  it('Anthropic caveat 常量存在（禁用文案附加行）', () => {
    expect(THINKING_ANTHROPIC_CAVEAT).toContain('Anthropic')
  })
})
