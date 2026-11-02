/**
 * SettingsPage 组件级测试：保存 422 红框映射 / 空串剪除提交 / 探测状态机 / 预设覆盖语义。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('../api/client', () => ({
  api: {
    getSettings: vi.fn(),
    putSettings: vi.fn(),
    getPresets: vi.fn(),
    getForeshadowCategories: vi.fn(),
    previewModels: vi.fn(),
    probeThinking: vi.fn(),
    getModelSpec: vi.fn().mockResolvedValue({ found: false, spec: null, catalog_version: '2026-11-01' }),
    syncModelsCatalog: vi.fn().mockResolvedValue({ success: true, version: '2026-11-01', total_models: 2347 }),
  },
}))

import { api, type AppConfigDto } from '../api/client'
import SettingsPage from './SettingsPage.vue'

const mockedApi = vi.mocked(api)

function mkCfg(): AppConfigDto {
  return {
    api: {
      base_url: 'https://x', api_key: 'sk-old', model: 'm-a',
      max_tokens: 100, timeout: 300, summary_timeout: 600, max_retries: 5,
      json_mode: 'default', temperature: 0.1, temperature_step: 0.1,
      temperature_max_retries: 3, backoff_max_retries: 1,
      thinking_mode: {}, summary_model: '', summary_thinking_mode: {}, provider: 'auto',
    },
    analysis: {
      max_arc_length: 100, concurrency: 4, block_size: 4, encoding_priority: [],
      max_arcs_in_prompt: 5, max_summaries_in_prompt: 5, timeline_truncate: 10,
      max_character_states: 10, max_world_items: 10, max_foreshadow_entries: 10,
      foreshadow_kept_categories: ['人物', '情节'], foreshadow_min_importance: '中',
      foreshadow_min_confidence: '中', max_foreshadow_catalog_high: 500,
      max_foreshadow_catalog_mid: 200, batch_summary_min_words: 2000,
      final_report_min_words: 5000, rolling_early_chapters: 100,
      rolling_max_milestones: 20, rolling_max_momentum: 15, checkpoint_interval: 5,
      auto_archive: false, auto_summary: false, summary_concurrency: 2,
      summary_batch_size: 30, foreshadow_recheck_batch_size: 40,
      skip_moderation_blocked: true, max_compressed_arcs: 1, max_recent_summaries: 1,
      max_pacing_tracker: 1, max_foreshadow_network: 1, max_world_building: 1,
      max_verified_facts: 1, max_long_term_arcs: 1, max_thematic_elements: 1,
      volume_compress_threshold: 1, volume_compress_group: 1,
    },
    gui: { language: 'zh-CN' },
  } as unknown as AppConfigDto
}

const settle = () => vi.advanceTimersByTimeAsync(0)

async function mountPage() {
  const wrapper = mount(SettingsPage, {
    global: { directives: { tooltip: {} } },
  })
  await settle()
  return wrapper
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()  // 清上一用例的 mock.calls（putSettings 断言按 calls[0] 取值）
  mockedApi.getSettings.mockResolvedValue(mkCfg())
  mockedApi.putSettings.mockResolvedValue({ ok: true })
  mockedApi.getPresets.mockResolvedValue({
    presets: {
      厂商A: { base_url: 'https://a/v1', model: 'model-a', api_key: 'sk-a' },
      // 空串 api_key 视为"预设不带密钥"（falsy → 不覆盖现有密钥，与生产一致）
      厂商B: { base_url: 'https://b/v1', model: 'model-b', api_key: '' },
    },
  })
  mockedApi.getForeshadowCategories.mockResolvedValue({
    schema_version: 1, fallback: 'builtin',
    defs: [
      { name: '人物', description: '', examples: [] },
      { name: '情节', description: '', examples: [] },
    ],
    kept: ['人物', '情节'], min_importance: '中', min_confidence: '中',
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('SettingsPage 保存（422 校验错误）', () => {
  it('422 → loc 末元素字段红框高亮 + 聚合错误文案', async () => {
    mockedApi.putSettings.mockRejectedValue(Object.assign(new Error('参数校验失败'), {
      status: 422,
      detail: [{ loc: ['body', 'api', 'base_url'], msg: 'invalid url' }],
    }))
    const wrapper = await mountPage()
    await wrapper.findAll('button').find((b) => b.text().includes('保存'))!.trigger('click')
    await settle()
    expect(wrapper.find('input.input-invalid').exists()).toBe(true)
    expect(wrapper.text()).toContain('base_url: invalid url')
  })

  it('非 422 错误 → 通用错误文案，无字段红框', async () => {
    mockedApi.putSettings.mockRejectedValue(Object.assign(new Error('网络炸了'), {}))
    const wrapper = await mountPage()
    await wrapper.findAll('button').find((b) => b.text().includes('保存'))!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('保存失败: 网络炸了')
    expect(wrapper.find('input.input-invalid').exists()).toBe(false)
  })

  it('数值输入清空后提交，空串字段被剪除（不静默改值）', async () => {
    const wrapper = await mountPage()
    // timeout 是 v-model.number：清空 → ''，提交前必须剪除该键
    const timeoutInput = wrapper.findAll('input[type="number"]').find((i) => (i.element as HTMLInputElement).value === '300')
    timeoutInput!.setValue('')
    await wrapper.findAll('button').find((b) => b.text().includes('保存'))!.trigger('click')
    await settle()
    const payload = mockedApi.putSettings.mock.calls[0][0] as unknown as Record<string, unknown>
    expect('timeout' in (payload as { api: Record<string, unknown> }).api).toBe(false)
  })
})

describe('SettingsPage 思考模式自动探测', () => {
  it('best 命中静态列表外的参数 → 注入动态选项并应用', async () => {
    mockedApi.probeThinking.mockResolvedValue({
      results: [],
      best: { thinking_mode: { chat_template_kwargs: { enable_thinking: false } }, param: 'chat_template_kwargs' },
      default_thinks: true,
      default_detection_breakdown: {},
      note: '',
    } as Awaited<ReturnType<typeof api.probeThinking>>)
    const wrapper = await mountPage()
    const probeBtn = wrapper.findAll('button').find((b) => b.text().includes('自动探测'))
    await probeBtn!.trigger('click')
    await settle()
    // 动态选项注入（静态 4 项之外新增）
    expect(wrapper.text()).toContain('探测命中: chat_template_kwargs')
    expect(wrapper.text()).not.toContain('探测中...')  // busy 复位
  })

  it('best 命中思考深度参数（如 MiniMax M3.1 low）→ 自动切到浅度思考并展示青色徽标', async () => {
    mockedApi.probeThinking.mockResolvedValue({
      results: [],
      best: { thinking_mode: { reasoning_effort: 'low' }, param: 'reasoning_effort: low' },
      default_thinks: true,
      default_detection_breakdown: { usage_reasoning_tokens: true },
      model_type: 'always_on_reasoning',
      note: 'MiniMax M3.1 Flash 常开思考模型',
    } as unknown as Awaited<ReturnType<typeof api.probeThinking>>)
    const wrapper = await mountPage()
    const probeBtn = wrapper.findAll('button').find((b) => b.text().includes('自动探测'))
    await probeBtn!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('浅度思考模式 (low)')
    expect(wrapper.text()).toContain('(已由思考深度接管)')
  })

  it('探测失败 → probeBusy 复位并显示错误，旧结果清空', async () => {
    mockedApi.probeThinking.mockRejectedValue(new Error('端点超时'))
    const wrapper = await mountPage()
    await wrapper.findAll('button').find((b) => b.text().includes('自动探测'))!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('探测失败: 端点超时')
    expect(wrapper.text()).not.toContain('探测中...')
  })
})

describe('SettingsPage 思考模式状态提示：不点探测也可见', () => {
  it('`{}` → 显示"跟随端点默认可能开启"警示', async () => {
    const wrapper = await mountPage()  // mkCfg thinking_mode = {}
    expect(wrapper.text()).toContain('跟随端点默认')
    expect(wrapper.text()).toContain('建议点「自动探测」')
  })

  it('显式禁用语法 → 显示 ✓ 正反馈 + Anthropic caveat；警示消失', async () => {
    mockedApi.getSettings.mockResolvedValue({
      ...mkCfg(),
      api: { ...mkCfg().api, thinking_mode: { enable_thinking: false } },
    } as AppConfigDto)
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('已显式禁用思考链')
    expect(wrapper.text()).toContain('Anthropic')
    expect(wrapper.text()).not.toContain('建议点「自动探测」')
  })

  it('探测动态选项的嵌套禁用写法 → 同样识别为 ✓（inferThinkingState 五语法）', async () => {
    mockedApi.getSettings.mockResolvedValue({
      ...mkCfg(),
      api: { ...mkCfg().api, thinking_mode: { chat_template_kwargs: { enable_thinking: false } } },
    } as AppConfigDto)
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('已显式禁用思考链')
  })

  it('浅度思考配置 → 显示 ⚡ 浅度思考徽标并提示有效抑制思考膨胀', async () => {
    mockedApi.getSettings.mockResolvedValue({
      ...mkCfg(),
      api: { ...mkCfg().api, thinking_mode: { reasoning_effort: 'low' } },
    } as AppConfigDto)
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('浅度思考模式 (low)')
    expect(wrapper.text()).toContain('有效抑制思考膨胀')
  })
})

describe('SettingsPage API 预设覆盖语义', () => {
  it('预设只在自带 api_key 时覆盖密钥；base_url/model 恒覆盖', async () => {
    const wrapper = await mountPage()
    const presetSelect = wrapper.findAll('select')[0]
    // 先切不带 api_key 的厂商B：base_url 覆盖，密钥保持 sk-old
    await presetSelect.setValue('厂商B')
    expect((wrapper.findAll('input')[0].element as HTMLInputElement).value).toBe('https://b/v1')
    const apiKeyInput = wrapper.find('input[type="password"]')
    expect((apiKeyInput.element as HTMLInputElement).value).toBe('sk-old')
    // 再切自带 api_key 的厂商A：密钥被覆盖为 sk-a
    await presetSelect.setValue('厂商A')
    expect((wrapper.findAll('input')[0].element as HTMLInputElement).value).toBe('https://a/v1')
    expect((apiKeyInput.element as HTMLInputElement).value).toBe('sk-a')
  })
})

describe('SettingsPage models.dev 规格与自更新', () => {
  it('匹配到 models.dev 规范时展示提示徽标（含思考控制与上下文上限）', async () => {
    mockedApi.getModelSpec.mockResolvedValue({
      found: true,
      spec: {
        id: 'minimax/minimax-m3.1-flash',
        name: 'MiniMax M3.1 Flash',
        family: 'minimax',
        reasoning: true,
        reasoning_type: 'effort',
        effort_values: ['low', 'medium', 'high'],
        recommended_mode: { reasoning_effort: 'low' },
        limit: { context: 1000000, output: 512000 },
        cost: { input: 0.15, output: 0.6 },
      },
      catalog_version: '2026-11-01',
    })
    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('models.dev 规范：MiniMax M3.1 Flash')
    expect(wrapper.text()).toContain('支持思考控制（档位: low/medium/high）')
    expect(wrapper.text()).toContain('上下文 1000k')
  })

  it('点击更新模型库按钮触发 syncModelsCatalog 并展示同步成功', async () => {
    mockedApi.syncModelsCatalog.mockResolvedValue({
      success: true,
      version: '2026-11-02',
      total_models: 2400,
    })
    const wrapper = await mountPage()
    const syncBtn = wrapper.findAll('button').find((b) => b.text().includes('更新模型库'))
    expect(syncBtn).toBeDefined()
    await syncBtn!.trigger('click')
    await settle()
    expect(mockedApi.syncModelsCatalog).toHaveBeenCalled()
    expect(wrapper.text()).toContain('同步成功 (共 2400 款)')
  })
})
