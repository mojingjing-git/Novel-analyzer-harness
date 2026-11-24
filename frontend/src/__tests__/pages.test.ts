/**
 * 页面级交互综合测试：
 * 覆盖 QueuePage、SettingsPage、SplitterPage、SummaryPage、TimelinePage、WorkspacePage、StatsPage、CharacterCardPage、WorldStagePage。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ref, nextTick } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'

const { echartsSetOption } = vi.hoisted(() => ({
  echartsSetOption: vi.fn(),
}))

let wsHandler: ((msg: ProgressMessage) => void) | null = null

vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: vi.fn((el: HTMLElement) => ({
    setOption: echartsSetOption,
    on: vi.fn(),
    off: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
    getDom: () => el,
  })),
}))
vi.mock('echarts/charts', () => ({ GraphChart: {}, TreeChart: {} }))
vi.mock('echarts/components', () => ({ TooltipComponent: {}, LegendComponent: {} }))
vi.mock('echarts/renderers', () => ({ SVGRenderer: {} }))

vi.mock('../api/useProgressSocket', () => ({
  useProgressSocket: vi.fn(),
}))

vi.mock('../components/BookSelector.vue', () => ({
  default: {
    name: 'BookSelector',
    props: ['modelValue', 'syncGlobal'],
    emits: ['update:modelValue'],
    template: `<select :value="modelValue" :data-sync-global="String(syncGlobal ?? true)" data-test="selector" @change="$emit('update:modelValue', $event.target.value)"></select>`,
  },
}))

vi.mock('../api/client', () => ({
  api: {
    // QueuePage & ChapterDetailPanel
    analysisStatus: vi.fn(),
    getTokenStats: vi.fn(),
    getSessionTokenStats: vi.fn(),
    startAnalysis: vi.fn(),
    stopAnalysis: vi.fn(),
    scanWorkspace: vi.fn(),
    removeQueueItem: vi.fn(),
    moveQueueItemUp: vi.fn(),
    moveQueueItemDown: vi.fn(),
    resetQueueItem: vi.fn(),
    clearQueue: vi.fn(),
    deleteBook: vi.fn(),
    listBooks: vi.fn().mockResolvedValue([]),
    getLatestChapter: vi.fn().mockResolvedValue({ book_id: 'A', latest: 0 }),
    getChapterResult: vi.fn(),
    // SettingsPage & SummaryPage
    getSettings: vi.fn(),
    putSettings: vi.fn(),
    patchSettings: vi.fn(),
    getPresets: vi.fn(),
    getForeshadowCategories: vi.fn(),
    previewModels: vi.fn(),
    getModels: vi.fn().mockResolvedValue({ models: ['m-a'] }),
    probeThinking: vi.fn(),
    probeRecheck: vi.fn(),
    getModelSpec: vi.fn().mockResolvedValue({ found: false, spec: null, catalog_version: '2026-11-01' }),
    syncModelsCatalog: vi.fn().mockResolvedValue({ success: true, version: '2026-11-01', total_models: 2347 }),
    promptPreview: vi.fn(),
    // SplitterPage
    previewSplit: vi.fn(),
    batchSplit: vi.fn(),
    inferBookName: vi.fn(),
    // SummaryPage & CharacterCardPage & WorldStagePage
    startSummary: vi.fn(),
    stopSummary: vi.fn(),
    summaryStatus: vi.fn(),
    getBookReport: vi.fn(),
    getStyleProfile: vi.fn(),
    getRecheckVerdicts: vi.fn(),
    exportAggregateExcel: vi.fn(),
    getBooks: vi.fn().mockResolvedValue({ books: [] }),
    getBookCharacters: vi.fn().mockResolvedValue({ book_id: 'A', characters: [] }),
    getCharacterCard: vi.fn(),
    getAtlas: vi.fn(),
    // TimelinePage
    getTimeline: vi.fn(),
    getForeshadowRanking: vi.fn(),
    // StatsPage
    getUsage: vi.fn(),
    clearUsage: vi.fn(),
    getBookTokenStats: vi.fn(),
    // WorkspacePage
    workspaceNovels: vi.fn(),
    workspaceArchives: vi.fn(),
    archiveNovel: vi.fn(),
    archiveAll: vi.fn(),
    deleteArchive: vi.fn(),
    restoreArchiveBlocks: vi.fn(),
  },
}))

import { api, type AnalysisStatus, type AppConfigDto, type QueueItemDto } from '../api/client'
import { useProgressSocket, type ProgressMessage } from '../api/useProgressSocket'
import QueuePage from '../pages/QueuePage.vue'
import SettingsPage from '../pages/SettingsPage.vue'
import SplitterPage from '../pages/SplitterPage.vue'
import SummaryPage from '../pages/SummaryPage.vue'
import TimelinePage from '../pages/TimelinePage.vue'
import WorkspacePage from '../pages/WorkspacePage.vue'
import CharacterCardPage from '../pages/CharacterCardPage.vue'
import WorldStagePage from '../pages/WorldStagePage.vue'
import StatsPage from '../pages/StatsPage.vue'
import { router as appRouter } from '../router'
import RunDashboard from '../components/RunDashboard.vue'
import ChapterDetailPanel from '../components/ChapterDetailPanel.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import LogConsole from '../components/LogConsole.vue'
import { useLogStore } from '../composables/useLogStore'
import { resetSelectedBookForTest } from '../composables/useSelectedBook'

const mockedApi = vi.mocked(api)
const settle = () => vi.advanceTimersByTimeAsync(0)

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
      summary_batch_size: 30,
      skip_moderation_blocked: true, max_compressed_arcs: 1, max_recent_summaries: 1,
      max_pacing_tracker: 1, max_foreshadow_network: 1, max_world_building: 1,
      max_verified_facts: 1, max_long_term_arcs: 1, max_thematic_elements: 1,
      volume_compress_threshold: 1, volume_compress_group: 1,
    },
    gui: { language: 'zh-CN' },
  } as unknown as AppConfigDto
}

function mkStatus(partial: Partial<AnalysisStatus> = {}): AnalysisStatus {
  return {
    running: false,
    queue: { total_items: 0, completed_items: 0, current_item: '', current_progress: 0, current_total: 0 },
    items: [],
    workspace_dir: '',
    ...partial,
  }
}

function mkItem(partial: Partial<QueueItemDto>): QueueItemDto {
  return {
    name: '', blocks_dir: '', workspace_dir: '', status: 'pending',
    total_chapters: 0, completed_chapters: 0, block_size: 1,
    start_time: 0, end_time: 0, error_message: '', archive_failed: false,
    ...partial,
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  window.localStorage.clear()
  resetSelectedBookForTest()
  useLogStore().clear()
  vi.mocked(useProgressSocket).mockImplementation((cb) => {
    wsHandler = cb
    return { connected: ref(true) }
  })
  mockedApi.analysisStatus.mockResolvedValue(mkStatus())
  mockedApi.getForeshadowRanking.mockResolvedValue({ book_id: 'A', total: 0, ranking: [] })
  mockedApi.getTokenStats.mockResolvedValue({ categories: {}, chapter_stats: [], elapsed: 0, running: false })
  mockedApi.getSessionTokenStats.mockResolvedValue({
    analysis: { categories: {}, chapter_stats: [], elapsed: 0, running: false },
    summary: { categories: {}, input_tokens: 0, output_tokens: 0, elapsed: 0 },
  })
  mockedApi.getSettings.mockResolvedValue(mkCfg())
  mockedApi.putSettings.mockResolvedValue(mkCfg())
  mockedApi.patchSettings.mockResolvedValue(mkCfg())
  mockedApi.getPresets.mockResolvedValue({
    presets: {
      厂商A: { base_url: 'https://a/v1', model: 'model-a', api_key: 'sk-a' },
      厂商B: { base_url: 'https://b/v1', model: 'model-b', api_key: '' },
    },
  })
  mockedApi.getForeshadowCategories.mockResolvedValue({
    schema_version: 1, fallback: 'builtin',
    defs: [{ name: '人物', description: '', examples: [] }],
    kept: ['人物'], min_importance: '中', min_confidence: '中',
  })
  mockedApi.getModelSpec.mockResolvedValue({ found: false, spec: null, catalog_version: '2026-11-01' })
  mockedApi.syncModelsCatalog.mockResolvedValue({ success: true, version: '2026-11-01', total_models: 2347 })
  echartsSetOption.mockClear()
  mockedApi.getModels.mockResolvedValue({ models: ['m-a'] })
  mockedApi.summaryStatus.mockResolvedValue(null as unknown as Awaited<ReturnType<typeof api.summaryStatus>>)
  mockedApi.getBookReport.mockResolvedValue({ book_id: '', report: '' })
  mockedApi.getStyleProfile.mockResolvedValue({ book_id: 'A', exists: false, statistical: null, semantic: null, sample_chapters: [] })
  mockedApi.getRecheckVerdicts.mockResolvedValue({
    book_id: 'A', exists: false, engine: '', status: '', status_reason: '',
    model: '', generated_at: '', coverage: {}, candidates_total: 0, verdicts: [],
  })
  mockedApi.getAtlas.mockResolvedValue({
    book_id: 'A', exists: false, schema_version: 1, generated_at: '', model: '',
    characters: [], relationships: [], stages: [], world_tree: [],
  })
  mockedApi.promptPreview.mockResolvedValue({
    book_id: 'A', chapter: 1, chapter_label: '第1章', chapter_total_chars: 1000,
    chapter_truncated: false, system_prompt: 'sys', user_prompt: 'usr',
    system_len: 3, user_len: 3, total_len: 6, params: {},
  })
  mockedApi.exportAggregateExcel.mockResolvedValue({ ok: true, path: 'output/test.xlsx' })
  mockedApi.getUsage.mockResolvedValue({
    version: 1, updated_at: null,
    per_model: [{ model: 'm-a', in: 100, out: 50, cached: 0, usd: 0.01 }],
    categories: { chapter: { in: 100, out: 50 } },
    category_models: { chapter: ['m-a'] },
    chapters_done: 1, retries: 0, failed_tokens: 0, elapsed_seconds: 10,
    total_usd: 0.01, unpriced: [], catalog_version: '2026-11-01',
  })
  mockedApi.clearUsage.mockResolvedValue({ success: true })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('QueuePage', () => {
  it('队列未加载、为空、或只含终态时禁止启动，待处理项启用启动', async () => {
    const wrapper = mount(QueuePage, { global: { directives: { tooltip: {} } } })
    const start = () => wrapper.findAll('button').find(b => b.text().trim() === '开始分析')!
    expect(start().element.disabled).toBe(true)
    await settle()
    expect(start().element.disabled).toBe(true)
    await start().trigger('click')

    mockedApi.analysisStatus.mockResolvedValue(mkStatus({ items: ['done', 'failed', 'skipped'].map(status => mkItem({ status })) }))
    await vi.advanceTimersByTimeAsync(5000)
    expect(start().element.disabled).toBe(true)
    await start().trigger('click')
    expect(mockedApi.startAnalysis).not.toHaveBeenCalled()

    mockedApi.analysisStatus.mockResolvedValue(mkStatus({ items: [mkItem({ status: 'pending' })] }))
    await vi.advanceTimersByTimeAsync(5000)
    expect(start().element.disabled).toBe(false)
    wrapper.unmount()
  })

  it.each([true, false])('启动返回 %s 时正确处理抢先到达的分析事件', async (accepted) => {
    mockedApi.analysisStatus.mockResolvedValue(mkStatus({ items: [mkItem({ status: 'pending' })] }))
    let succeed!: (data: { ok: boolean }) => void
    let fail!: (error: Error) => void
    mockedApi.startAnalysis.mockImplementationOnce(() => new Promise((resolve, reject) => { succeed = resolve; fail = reject }))
    const wrapper = mount(QueuePage, { global: { directives: { tooltip: {} } } })
    const dashboard = () => {
      const props = wrapper.getComponent(RunDashboard).props()
      return { progress: props.progress!, rateHistory: props.rateHistory!, activeBlocks: props.activeBlocks!, finishedBlocks: props.finishedBlocks!, tokenByBlock: props.tokenByBlock! }
    }
    const sendStart = (chapter: number) => wsHandler?.({ type: 'block_start', payload: { chapter, range: `ch${chapter}`, progress: 0, total: 0, ts: 100 } })
    const sendToken = (chapter: number) => wsHandler?.({ type: 'token_delta', payload: { context: 'analysis', session_id: 's', unit_idx: chapter, delta: { output_tokens: 50, rate_tokens_per_sec: 40 }, timestamp: 0 } })
    const sendDone = (chapter: number) => wsHandler?.({ type: 'block_done', payload: { chapter, ok: true, range: `ch${chapter}` } })
    await settle()
    sendStart(9)
    sendToken(9)
    await vi.advanceTimersByTimeAsync(1000)
    sendDone(9)
    wsHandler?.({ type: 'progress', payload: { current: 9, total: 10, eta: '' } })
    await nextTick()
    expect(dashboard().rateHistory).toEqual([40])

    await wrapper.findAll('button').find(b => b.text().trim() === '开始分析')!.trigger('click')
    wsHandler?.({ type: 'progress', payload: { current: 1, total: 20, eta: '约 1 分钟' } })
    sendStart(1)
    sendToken(1)
    sendDone(1)
    sendStart(2)
    sendToken(2)
    await nextTick()
    expect(dashboard().progress.current).toBe(9)
    expect(dashboard().rateHistory).toEqual([40])
    expect(dashboard().activeBlocks.has(2)).toBe(false)

    if (accepted) succeed({ ok: true })
    else fail(new Error('最终总结正在运行'))
    await settle()

    expect(dashboard().progress.current).toBe(accepted ? 1 : 9)
    expect(dashboard().rateHistory).toEqual(accepted ? [] : [40])
    expect(dashboard().finishedBlocks.has(1)).toBe(accepted)
    expect(dashboard().activeBlocks.has(1)).toBe(false)
    expect(dashboard().activeBlocks.has(2)).toBe(accepted)
    expect(dashboard().tokenByBlock.has(1)).toBe(false)
    await vi.advanceTimersByTimeAsync(1000)
    expect(dashboard().tokenByBlock.has(2)).toBe(accepted)
    if (!accepted) expect(wrapper.text()).toContain('最终总结正在运行')
    wrapper.unmount()
  })

  it('WS 状态机（block_start/token_delta/block_done/summary_progress）与防抖合并刷新', async () => {
    const wrapper = mount(QueuePage, { global: { directives: { tooltip: {} } } })
    await settle()
    const baseCalls = mockedApi.analysisStatus.mock.calls.length

    wsHandler?.({ type: 'block_start', payload: { chapter: 9, range: 'ch9-ch10', progress: 0, total: 0, ts: 100 } })
    wsHandler?.({ type: 'token_delta', payload: { context: 'analysis', session_id: 's', unit_idx: 9, delta: { output_tokens: 100, rate_tokens_per_sec: 55 }, timestamp: 0 } })
    await nextTick()
    let dash = wrapper.findComponent(RunDashboard).props() as Record<string, unknown>
    expect(dash.aggregateRate).toBe(55)

    wsHandler?.({ type: 'block_done', payload: { chapter: 9, ok: true, range: 'ch9-ch10' } })
    wsHandler?.({ type: 'block_done', payload: { chapter: 10, ok: true, range: 'ch10-ch11' } })
    await nextTick()
    dash = wrapper.findComponent(RunDashboard).props() as Record<string, unknown>
    expect(dash.aggregateRate).toBe(0)

    await vi.advanceTimersByTimeAsync(500)
    expect(mockedApi.analysisStatus.mock.calls.length).toBe(baseCalls + 1)
    wrapper.unmount()
  })

  it('1 秒非响应式缓冲与平滑总速率定时采样', async () => {
    const wrapper = mount(QueuePage, { global: { directives: { tooltip: {} } } })
    await settle()

    wsHandler?.({ type: 'block_start', payload: { chapter: 9, range: 'ch9-ch10', progress: 0, total: 0, ts: 100 } })
    // 首帧立即提交，rateHistory 不由单块消息直接追加
    wsHandler?.({ type: 'token_delta', payload: { context: 'analysis', session_id: 's', unit_idx: 9, delta: { output_tokens: 50, rate_tokens_per_sec: 40 }, timestamp: 0 } })
    await nextTick()
    let dash = wrapper.findComponent(RunDashboard).props() as Record<string, unknown>
    expect(dash.aggregateRate).toBe(40)
    expect(dash.rateHistory).toEqual([])

    // 同 1 秒窗内的多路并发增量暂存缓冲，不即时引发 RunDashboard rateHistory 抖动
    wsHandler?.({ type: 'block_start', payload: { chapter: 10, range: 'ch11-ch12', progress: 0, total: 0, ts: 100 } })
    wsHandler?.({ type: 'token_delta', payload: { context: 'analysis', session_id: 's', unit_idx: 10, delta: { output_tokens: 80, rate_tokens_per_sec: 60 }, timestamp: 0 } })
    await nextTick()
    dash = wrapper.findComponent(RunDashboard).props() as Record<string, unknown>
    expect(dash.aggregateRate).toBe(40)
    expect(dash.rateHistory).toEqual([])

    // 满 1 秒定时器统一提交并压入平滑后的全系统总速率样本
    await vi.advanceTimersByTimeAsync(1000)
    dash = wrapper.findComponent(RunDashboard).props() as Record<string, unknown>
    expect(dash.aggregateRate).toBe(100)
    expect(dash.rateHistory).toEqual([100])

    wsHandler?.({ type: 'token_delta', payload: { context: 'analysis', session_id: 's', unit_idx: 9, delta: { output_tokens: 90, rate_tokens_per_sec: 20 }, timestamp: 0 } })
    await vi.advanceTimersByTimeAsync(1000)
    dash = wrapper.findComponent(RunDashboard).props() as Record<string, unknown>
    expect(dash.aggregateRate).toBe(80)
    expect((dash.rateHistory as number[])[1]).toBeCloseTo(93, 5)
    wrapper.unmount()
  })

  it('按书名实时解析删除索引（防重排删错书）与点击车道块切换章节详情 Tab', async () => {
    const bookA = mkItem({ name: '《A》', status: 'done' })
    const bookB = mkItem({ name: '《B》', status: 'done' })
    mockedApi.analysisStatus.mockResolvedValue(mkStatus({ items: [bookA, bookB] }))
    const wrapper = mount(QueuePage, { global: { directives: { tooltip: {} } } })
    await settle()

    const delBtns = wrapper.findAll('tbody button').filter((b) => b.text().trim() === '删除')
    await delBtns[1].trigger('click') // 点击《B》
    await settle()
    mockedApi.analysisStatus.mockResolvedValue(mkStatus({ items: [bookB, bookA] }))
    await vi.advanceTimersByTimeAsync(5_000) // 轮询重排后 B 移到索引 0
    await wrapper.findComponent(ConfirmDialog).vm.$emit('confirm')
    await settle()
    expect(mockedApi.deleteBook).toHaveBeenCalledWith(0)

    await wrapper.findComponent(RunDashboard).vm.$emit('select-block', 5)
    await nextTick()
    expect(wrapper.findComponent(ChapterDetailPanel).props('chapter')).toBe(5)
    wrapper.unmount()
  })

  it('QueuePage：默认收起实时日志与重复用量栏，点击实时日志切换展开/收起', async () => {
    const wrapper = mount(QueuePage, { global: { directives: { tooltip: {} } } })
    await settle()
    expect(wrapper.findComponent(LogConsole).exists()).toBe(false)
    expect(wrapper.find('.total-card').exists()).toBe(false)

    const logBtn = wrapper.findAll('button').find((b) => b.text().includes('实时日志'))!
    await logBtn.trigger('click')
    await nextTick()
    expect(wrapper.findComponent(LogConsole).exists()).toBe(true)

    await logBtn.trigger('click')
    await nextTick()
    expect(wrapper.findComponent(LogConsole).exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('SettingsPage', () => {
  it('422 校验错误红框映射、数值清空剪除、思考模式自动探测与预设覆盖语义', async () => {
    mockedApi.putSettings.mockRejectedValueOnce(Object.assign(new Error('参数校验失败'), {
      status: 422,
      detail: [{ loc: ['body', 'api', 'base_url'], msg: 'invalid url' }],
    }))
    const wrapper = mount(SettingsPage, { global: { directives: { tooltip: {} } } })
    await settle()

    // 422 红框
    await wrapper.findAll('button').find((b) => b.text().includes('保存'))!.trigger('click')
    await settle()
    expect(wrapper.find('input.input-invalid').exists()).toBe(true)
    expect(wrapper.text()).toContain('base_url: invalid url')

    // 预设切换：厂商B 空密钥不覆盖原密钥，厂商A 覆盖
    const presetSelect = wrapper.findAll('select')[0]
    await presetSelect.setValue('厂商B')
    expect((wrapper.find('input[type="password"]').element as HTMLInputElement).value).toBe('sk-old')
    await presetSelect.setValue('厂商A')
    expect((wrapper.find('input[type="password"]').element as HTMLInputElement).value).toBe('sk-a')

    // 思考模式探测命中浅度思考
    mockedApi.probeThinking.mockResolvedValue({
      results: [],
      best: { thinking_mode: { reasoning_effort: 'low' }, param: 'reasoning_effort: low' },
      default_thinks: true,
      default_detection_breakdown: {},
      model_type: 'always_on_reasoning',
      note: 'MiniMax',
    } as unknown as Awaited<ReturnType<typeof api.probeThinking>>)
    await wrapper.findAll('button').find((b) => b.text().includes('自动探测'))!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('浅度思考模式 (low)')
    wrapper.unmount()
  })

  it('SettingsPage：保存不破坏空字符串状态且高级字段 422 自动展开折叠区', async () => {
    const cfg = mkCfg()
    cfg.analysis.recheck_model = ''
    cfg.analysis.recheck_base_url = ''
    cfg.analysis.recheck_api_key = ''
    cfg.analysis.recheck_provider = ''
    mockedApi.getSettings.mockResolvedValueOnce(cfg)

    const wrapper = mount(SettingsPage, { global: { directives: { tooltip: {} } } })
    await settle()

    // 初始高级区折叠（v-show=false）
    expect(wrapper.text()).toContain('展开高级参数（3 大分组）')
    await wrapper.findAll('button').find((b) => b.text().includes('保存设置'))!.trigger('click')
    await settle()

    // 原有 config 字符串不被原地 delete，探测复检不抛 TypeError
    mockedApi.probeRecheck.mockResolvedValueOnce({ results: [], best: null, default_thinks: false, default_detection_breakdown: {}, note: 'ok' })
    await wrapper.findAll('button').find((b) => b.text().includes('探测判定通道'))!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('ok')

    // 高级字段 422 自动展开高级区并高亮红框
    mockedApi.putSettings.mockRejectedValueOnce(Object.assign(new Error('校验失败'), {
      status: 422,
      detail: [{ loc: ['body', 'api', 'timeout'], msg: '必须大于 0' }],
    }))
    await wrapper.findAll('button').find((b) => b.text().includes('保存设置'))!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('收起高级参数（3 大分组）')
    expect(wrapper.find('input.input-invalid').exists()).toBe(true)
    wrapper.unmount()
  })

  it('SettingsPage：保存响应保留提交后输入，第二次保存提交新值', async () => {
    let finishSave!: (config: AppConfigDto) => void
    mockedApi.putSettings.mockImplementationOnce(() => new Promise(resolve => { finishSave = resolve }))
    const wrapper = mount(SettingsPage, { global: { directives: { tooltip: {} } } })
    await settle()
    const input = wrapper.get<HTMLInputElement>('input[placeholder="点击选择或输入自定义模型名"]')
    const save = () => wrapper.findAll('button').find(b => b.text().includes('保存设置'))!
    await input.setValue('first-draft')
    await save().trigger('click')
    await input.setValue('typed-while-saving')
    const response = mkCfg()
    response.api.model = 'first-draft'
    finishSave(response)
    await settle()
    expect(input.element.value).toBe('typed-while-saving')
    expect(wrapper.text()).toContain('有未保存的修改')
    expect(wrapper.text()).not.toContain('已保存')

    const second = mkCfg()
    second.api.model = 'typed-while-saving'
    mockedApi.putSettings.mockResolvedValueOnce(second)
    await save().trigger('click')
    await settle()
    expect(mockedApi.putSettings).toHaveBeenLastCalledWith(expect.objectContaining({ api: expect.objectContaining({ model: 'typed-while-saving' }) }))
    expect(wrapper.get('[role="status"]').text()).toBe('已保存')
    wrapper.unmount()
  })

  it('SettingsPage：旧模型规格慢响应不会覆盖当前模型，也不显示已被清空模型的规格', async () => {
    type SpecResponse = Awaited<ReturnType<typeof api.getModelSpec>>
    const spec = (name: string): SpecResponse => ({ found: true, catalog_version: 'test', spec: {
      id: name, name, family: 'test', reasoning: false, reasoning_type: '',
      effort_values: [], recommended_mode: {}, limit: {}, cost: {},
    } })
    let resolveOld!: (value: SpecResponse) => void
    mockedApi.getModelSpec.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    const wrapper = mount(SettingsPage, { global: { directives: { tooltip: {} } } })
    await settle()
    const modelInput = wrapper.get('input[placeholder="点击选择或输入自定义模型名"]')
    mockedApi.getModelSpec.mockResolvedValueOnce(spec('新模型规格'))
    await modelInput.setValue('new-model')
    await modelInput.trigger('blur')
    await settle()
    expect(wrapper.text()).toContain('新模型规格')
    resolveOld(spec('旧模型规格'))
    await settle()
    expect(wrapper.text()).not.toContain('旧模型规格')
    expect(wrapper.text()).toContain('新模型规格')
    await modelInput.setValue('')
    expect(wrapper.text()).not.toContain('新模型规格')
    wrapper.unmount()
  })

  it('SettingsPage：模型下拉支持焦点移出/返回、按钮激活和滚动条操作', async () => {
    mockedApi.previewModels.mockResolvedValueOnce({ models: ['m-a', 'm-b', 'm-c'] })
    const wrapper = mount(SettingsPage, {
      attachTo: document.body,
      global: { directives: { tooltip: {} } },
    })
    await settle()

    const modelInput = wrapper.find('input[placeholder="点击选择或输入自定义模型名"]')
    await modelInput.trigger('focus')
    await settle()
    const dropdown = wrapper.find('.model-dropdown')
    expect(dropdown.exists()).toBe(true)

    await dropdown.trigger('mousedown')
    expect(wrapper.find('.model-dropdown').exists()).toBe(true)

    const input = modelInput.element as HTMLInputElement
    input.focus()
    await nextTick()
    const option = wrapper.findAll<HTMLButtonElement>('.model-option').find(button => button.text() === 'm-b')!
    option.element.focus()
    await vi.advanceTimersByTimeAsync(150)
    expect(wrapper.find('.model-dropdown').exists()).toBe(true)
    await option.trigger('click')
    expect(input.value).toBe('m-b')
    expect(document.activeElement).toBe(input)
    expect(wrapper.find('.model-dropdown').exists()).toBe(false)

    await modelInput.trigger('click')
    input.blur()
    input.focus()
    await vi.advanceTimersByTimeAsync(150)
    expect(wrapper.find('.model-dropdown').exists()).toBe(true)
    document.body.click()
    await nextTick()
    expect(wrapper.find('.model-dropdown').exists()).toBe(false)
    wrapper.unmount()
  })

  it('SettingsPage：修改后显示「放弃更改」，点击可还原草稿并清除未保存提示', async () => {
    const wrapper = mount(SettingsPage, { global: { directives: { tooltip: {} } } })
    await settle()
    expect(wrapper.findAll('button').find((b) => b.text().trim() === '放弃更改')).toBeUndefined()

    const modelInput = wrapper.get<HTMLInputElement>('input[placeholder="点击选择或输入自定义模型名"]')
    await modelInput.setValue('draft-changed')
    await nextTick()
    expect(wrapper.text()).toContain('有未保存的修改')

    const discardBtn = wrapper.findAll('button').find((b) => b.text().trim() === '放弃更改')!
    expect(discardBtn).toBeDefined()
    await discardBtn.trigger('click')
    await nextTick()
    expect(modelInput.element.value).toBe('m-a')
    expect(wrapper.text()).not.toContain('有未保存的修改')
    expect(wrapper.findAll('button').find((b) => b.text().trim() === '放弃更改')).toBeUndefined()
    wrapper.unmount()
  })

  it('SettingsPage：统一模型调用与并发分区默认展开、summary_concurrency 支持 0 跟随主分析', async () => {
    const cfg = mkCfg()
    cfg.analysis.summary_concurrency = 0
    cfg.analysis.recheck_concurrency = 0
    mockedApi.getSettings.mockResolvedValueOnce(cfg)

    const wrapper = mount(SettingsPage, { global: { directives: { tooltip: {} } } })
    await settle()

    const txt = wrapper.text()
    expect(txt).toContain('模型调用与并发')
    expect(txt).toContain('1. 章节分析（主调用）')
    expect(txt).toContain('2. 最终总结')
    expect(txt).toContain('伏笔复检')
    expect(txt).toContain('总结并发 (0=跟随)')

    await wrapper.findAll('button').find((b) => b.text().includes('保存设置'))!.trigger('click')
    await settle()
    expect(mockedApi.putSettings).toHaveBeenCalledWith(expect.objectContaining({
      analysis: expect.objectContaining({ summary_concurrency: 0, recheck_concurrency: 0 }),
    }))
    wrapper.unmount()
  })
})

describe('SplitterPage', () => {
  afterEach(() => {
    delete (window as unknown as { pywebview?: unknown }).pywebview
  })

  it('pywebview 去重添加、批量切分整体失败回滚与串行推断书名容错', async () => {
    const picker = vi.fn(async () => ['D:\\a\\《甲》.txt', 'D:\\a\\《甲》.txt', 'D:\\a\\《乙》.txt'])
    ;(window as unknown as { pywebview?: unknown }).pywebview = { api: { pick_files: picker } }
    const wrapper = mount(SplitterPage)
    await wrapper.findAll('button').find((b) => b.text().includes('添加文件'))!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('批量切分（2 本）')

    mockedApi.batchSplit.mockRejectedValueOnce(new Error('后端崩了'))
    await wrapper.findAll('button').find((b) => b.text().includes('批量切分'))!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('后端崩了')
    expect(wrapper.text()).not.toContain('处理中')

    mockedApi.inferBookName.mockImplementation((p: string) =>
      p.includes('《乙》') ? Promise.reject(new Error('失败')) : Promise.resolve({ book_name: '《新书名》' }),
    )
    await wrapper.findAll('button').find((b) => b.text().includes('推断书名'))!.trigger('click')
    await settle()
    const inputs = wrapper.findAll('input[placeholder="书名"]')
    expect((inputs[0].element as HTMLInputElement).value).toBe('《新书名》')
    expect((inputs[1].element as HTMLInputElement).value).toBe('《乙》')
    wrapper.unmount()
  })

  it('SplitterPage：ToggleSwitch 控件与批量切分成功后一键加入分析队列', async () => {
    const picker = vi.fn(async () => ['D:\\a\\《甲》.txt'])
    ;(window as unknown as { pywebview?: unknown }).pywebview = { api: { pick_files: picker } }
    mockedApi.batchSplit.mockResolvedValueOnce({
      total: 1,
      success: 1,
      fail: 0,
      results: [{ file: 'D:\\a\\《甲》.txt', book: '《甲》', total_chapters: 12, ok: true, error: '' }],
    })
    mockedApi.scanWorkspace.mockResolvedValueOnce({ added: 1 })

    const wrapper = mount(SplitterPage)
    const switches = wrapper.findAll('button[role="switch"]')
    expect(switches).toHaveLength(3)
    expect((switches[2].element as HTMLButtonElement).disabled).toBe(true)

    await wrapper.findAll('button').find((b) => b.text().includes('添加文件'))!.trigger('click')
    await settle()
    await wrapper.findAll('button').find((b) => b.text().includes('批量切分'))!.trigger('click')
    await settle()

    const goQueueBtn = wrapper.findAll('button').find((b) => b.text().includes('加入分析队列并前往'))!
    expect(goQueueBtn).toBeDefined()
    await goQueueBtn.trigger('click')
    await settle()
    expect(mockedApi.scanWorkspace).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})

describe('TimelinePage & WorkspacePage', () => {
  it('TimelinePage：切书竞态守卫防止旧书慢响应覆盖新书数据', async () => {
    type TimelineResp = Awaited<ReturnType<typeof api.getTimeline>>
    let resolveA!: (v: TimelineResp) => void
    mockedApi.getTimeline.mockImplementation((id: string) => {
      if (id === 'A') return new Promise<TimelineResp>((r) => { resolveA = r })
      return Promise.resolve({
        events: [{ chapter: 2, event: 'B书的关键事件', characters: '', function: '', importance: '高' }],
        foreshadows: [],
      })
    })
    const wrapper = mount(TimelinePage)
    expect(wrapper.find('h2').text()).toBe('事件与伏笔')
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'A')
    await flushPromises()
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'B')
    await flushPromises()
    resolveA({ events: [{ chapter: 1, event: 'A书过期事件', characters: '', function: '' }], foreshadows: [] })
    await flushPromises()
    expect(wrapper.text()).toContain('B书的关键事件')
    expect(wrapper.text()).not.toContain('A书过期事件')
    wrapper.unmount()
  })

  it('TimelinePage：切书立即移除旧数据，当前请求可重试，排行缺账本单独显示', async () => {
    let failTimeline!: (error: Error) => void
    let failRanking!: (error: Error) => void
    mockedApi.getTimeline
      .mockResolvedValueOnce({ events: [{ chapter: 1, event: 'A书旧事件', characters: '', function: '' }], foreshadows: [] })
      .mockImplementationOnce(() => new Promise((_, reject) => { failTimeline = reject }))
      .mockResolvedValueOnce({ events: [{ chapter: 2, event: 'B书新事件', characters: '', function: '' }], foreshadows: [] })
    mockedApi.getForeshadowRanking
      .mockResolvedValueOnce({ book_id: 'A', total: 1, ranking: [{
        id: 'fs1', description: 'A书旧排行', composite_score: 40, importance: '高', confidence: 0.8,
        first_seen_chapter: 1, last_seen_chapter: 2, span: 1, evidence_count: 2, status: 'active',
        evidence_chapters: [1, 2], resolution: null, resolved_chapter: null,
      }] })
      .mockImplementationOnce(() => new Promise((_, reject) => { failRanking = reject }))
    const wrapper = mount(TimelinePage)
    const select = wrapper.findComponent({ name: 'BookSelector' })
    const tab = (label: string) => wrapper.findAll('button').find(button => button.text().includes(label))!
    select.vm.$emit('update:modelValue', 'A')
    await flushPromises()
    expect(wrapper.text()).toContain('A书旧事件')
    await tab('伏笔排行').trigger('click')
    expect(wrapper.text()).toContain('A书旧排行')

    select.vm.$emit('update:modelValue', 'B')
    await nextTick()
    expect(wrapper.text()).not.toContain('A书旧排行')
    expect(wrapper.get('[role="status"]').text()).toContain('正在加载伏笔排行')
    failRanking(Object.assign(new Error('伏笔账本不存在，请先运行最终总结'), { status: 404 }))
    await flushPromises()
    expect(wrapper.text()).toContain('伏笔账本不存在')
    await tab('事件时间线').trigger('click')
    expect(wrapper.text()).not.toContain('A书旧事件')
    expect(wrapper.get('[role="status"]').text()).toContain('正在加载时间线')
    failTimeline(new Error('读取中断'))
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('读取中断')
    await tab('重试').trigger('click')
    await flushPromises()
    expect(mockedApi.getTimeline).toHaveBeenLastCalledWith('B')
    expect(wrapper.text()).toContain('B书新事件')
    wrapper.unmount()
  })

  it('TimelinePage：取消最后一个分类后结果为空，重新全选恢复结果', async () => {
    mockedApi.getTimeline.mockResolvedValueOnce({ events: [], foreshadows: [{ chapter: 1, clue: '伏笔一', type: 'plant', category: '人物', confidence: 0.8 }] })
    const wrapper = mount(TimelinePage)
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'A')
    await flushPromises()
    const button = (label: string) => wrapper.findAll('button').find(b => b.text().startsWith(label))!
    await button('伏笔时间线').trigger('click')
    expect(wrapper.text()).toContain('伏笔一')
    await button('分类筛选').trigger('click')
    await button('清空').trigger('click')
    expect(wrapper.text()).toContain('未选择伏笔分类')
    expect(wrapper.text()).not.toContain('伏笔一')
    await button('全选').trigger('click')
    expect(wrapper.text()).toContain('伏笔一')
    wrapper.unmount()
  })

  it('TimelinePage：伏笔排行支持状态筛选且展示回收章节、质量标记与回收说明', async () => {
    mockedApi.getTimeline.mockResolvedValueOnce({ events: [], foreshadows: [] })
    mockedApi.getForeshadowRanking.mockResolvedValueOnce({
      book_id: 'A',
      total: 2,
      ranking: [
        {
          id: 'fs_r', description: '铜镜之谜', composite_score: 360, importance: '高',
          confidence: 0.9, status: 'resolved', first_seen_chapter: 2, last_seen_chapter: 20,
          span: 18, evidence_count: 4, evidence_chapters: [2, 20],
          resolution: '第20章揭示镜内封印为上古妖魂。', resolved_chapter: 20,
          resolution_quality: 'verified', needs_review: false,
        },
        {
          id: 'fs_a', description: '玄铁令牌', composite_score: 210, importance: '中',
          confidence: 0.7, status: 'active', first_seen_chapter: 5, last_seen_chapter: 10,
          span: 5, evidence_count: 2, evidence_chapters: [5, 10],
          resolution: null, resolved_chapter: null,
          needs_review: true,
        },
      ],
    })

    const wrapper = mount(TimelinePage)
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'A')
    await flushPromises()

    await wrapper.findAll('button').find((b) => b.text().includes('伏笔排行'))!.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('铜镜之谜')
    expect(wrapper.text()).toContain('回收于第20章')
    expect(wrapper.text()).toContain('证据已核实')
    expect(wrapper.text()).toContain('第20章揭示镜内封印为上古妖魂。')
    expect(wrapper.text()).toContain('建议复审')

    await wrapper.findAll('button').find((b) => b.text().trim() === '已回收')!.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('铜镜之谜')
    expect(wrapper.text()).not.toContain('玄铁令牌')

    await wrapper.findAll('button').find((b) => b.text().trim() === '休眠')!.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('当前状态筛选下暂无伏笔')
    wrapper.unmount()
  })

  it('WorkspacePage：归档与删除确认弹窗流及错误提示', async () => {
    mockedApi.workspaceNovels.mockResolvedValue({
      novels: [{ name: '《甲》', blocks_count: 10, dir_size: 1024, dir_mtime: 1700000000 }],
    } as Awaited<ReturnType<typeof api.workspaceNovels>>)
    mockedApi.workspaceArchives.mockResolvedValue({
      archives: [{ name: '《旧书》', total_size: 1024, dir_mtime: 1700000000 }],
    } as Awaited<ReturnType<typeof api.workspaceArchives>>)
    mockedApi.archiveNovel.mockRejectedValueOnce(new Error('目录被占用'))

    const wrapper = mount(WorkspacePage)
    await settle()
    await wrapper.findAll('button').find((b) => b.text().trim() === '归档')!.trigger('click')
    await settle()
    await wrapper.findComponent(ConfirmDialog).vm.$emit('confirm')
    await settle()
    expect(wrapper.text()).toContain('目录被占用')
    wrapper.unmount()
  })

  it('WorkspacePage：还原归档 blocks 切分、去时间戳预填、409 弹窗内联报错与自定义后缀还原成功', async () => {
    mockedApi.workspaceNovels.mockResolvedValue({
      novels: [{ name: '《甲》', blocks_count: 10, result_count: 10, dir_size: 1024, dir_mtime: 1700000000 }],
    })
    mockedApi.workspaceArchives.mockResolvedValue({
      archives: [
        { name: '《旧书》-20261122-0300', file_count: 12, total_size: 2048, dir_mtime: 1700000000, has_blocks: true, blocks_count: 12 },
        { name: '无切分归档', file_count: 1, total_size: 256, dir_mtime: 1700000000, has_blocks: false, blocks_count: 0 },
      ],
    })
    const wrapper = mount(WorkspacePage)
    await settle()

    const restoreBtns = wrapper.findAll('button').filter((b) => b.text().trim() === '还原切分')
    expect(restoreBtns).toHaveLength(2)
    expect((restoreBtns[1].element as HTMLButtonElement).disabled).toBe(true)

    await restoreBtns[0].trigger('click')
    await nextTick()
    const targetInput = wrapper.find('input[placeholder="输入工作区目录名"]')
    expect((targetInput.element as HTMLInputElement).value).toBe('《旧书》')

    mockedApi.restoreArchiveBlocks.mockRejectedValueOnce(new Error('工作区已存在同名目录'))
    await wrapper.findAll('button').find((b) => b.text().trim() === '确认还原')!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('工作区已存在同名目录')

    await targetInput.setValue('《旧书》-dsv4')
    mockedApi.restoreArchiveBlocks.mockResolvedValueOnce({ ok: true, novel_name: '《旧书》-dsv4', blocks_count: 12 })
    await wrapper.findAll('button').find((b) => b.text().trim() === '确认还原')!.trigger('click')
    await settle()
    expect(mockedApi.restoreArchiveBlocks).toHaveBeenLastCalledWith('《旧书》-20261122-0300', '《旧书》-dsv4')
    expect(wrapper.text()).toContain('已将「《旧书》-20261122-0300」的 12 章切分还原至 workspace/《旧书》-dsv4')
    expect(wrapper.findAll('button').some((b) => b.text().includes('前往分析队列'))).toBe(true)
    wrapper.unmount()
  })
})

describe('SummaryPage', () => {
  it('设置面板只读收口、切书报告竞态守卫、总结完成自动收起日志抽屉与复检明细 Tab', async () => {
    let resolveA!: (v: { book_id: string; report: string }) => void
    mockedApi.getBookReport.mockImplementation((id: string) => {
      if (id === 'A') return new Promise((r) => { resolveA = r })
      return Promise.resolve({ book_id: id, report: '<p>B的报告</p>' })
    })
    mockedApi.getRecheckVerdicts.mockResolvedValue({
      book_id: 'B',
      exists: true,
      engine: 'v3',
      status: 'COMPLETED',
      status_reason: '',
      model: 'm-recheck',
      generated_at: '2026-11-23T12:00:00Z',
      coverage: { checked: 2, candidates_total: 2 },
      candidates_total: 2,
      verdicts: [
        {
          id: 'fs_1', candidate_id: 'c1', verdict: 'RESOLVED_EXPLICIT', tier: 'RESOLVED_EXPLICIT',
          unresolved_type: '', answer: '第12章交待了钥匙用途。', quote: '钥匙拧开了暗格。', reason: '明确回应',
          applied: '已回收', resolved_chapter: '12', hops_used: 1,
          evidence_chain: [{ unit_id: 12, quote: '钥匙拧开了暗格。', role: 'payoff' }],
          gate_demoted: false, gate_reason: '', repaired: false,
          description: '青铜钥匙', question: '钥匙作何用？', first_seen_chapter: 1, importance: '高',
        },
        {
          id: 'fs_2', candidate_id: 'c2', verdict: 'UNRESOLVED', tier: 'UNRESOLVED',
          unresolved_type: '', answer: '暂未见交代。', quote: '', reason: '无后续线索',
          applied: '', resolved_chapter: '', hops_used: 3,
          evidence_chain: [], gate_demoted: false, gate_reason: '', repaired: false,
          description: '黑袍人身份', question: '黑袍人是谁？', first_seen_chapter: 2, importance: '中',
        },
      ],
    })

    const wrapper = mount(SummaryPage)
    await settle()

    // 设置面板已收口为 2 个章节范围输入 + 只读摘要条 + 前往设置按钮
    const panel = wrapper.get('[data-test="settings-panel"]')
    expect(panel.findAll('input[type="number"]')).toHaveLength(2)
    expect(panel.text()).toContain('m-a')
    expect(panel.text()).toContain('30 章')
    expect(panel.text()).toContain('前往设置调整')

    // 切书守卫
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'A')
    await settle()
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'B')
    await settle()
    resolveA({ book_id: 'A', report: '<p>A的过期报告</p>' })
    await settle()
    expect(wrapper.text()).toContain('B的报告')
    expect(wrapper.text()).not.toContain('A的过期报告')

    // 开始总结展开日志抽屉 -> 传 (0, 0) 由后端读取设置页参数 -> WS 实时推进 / V3 四态
    mockedApi.startSummary.mockResolvedValue({} as Awaited<ReturnType<typeof api.startSummary>>)
    await wrapper.findAll('button').find((b) => b.text().includes('开始总结'))!.trigger('click')
    await settle()
    expect(mockedApi.startSummary).toHaveBeenCalledWith('B', 1, 99999, 0, 0)
    expect(wrapper.findComponent(LogConsole).exists()).toBe(true)

    wsHandler?.({
      type: 'summary_progress',
      payload: { type: 'phase', phase: 'batch', total_batches: 3, book_id: 'B', batches_done: 0 },
    })
    wsHandler?.({
      type: 'token_stats',
      payload: { category: 'summary', input_tokens: 100, output_tokens: 50, source: 'summary' },
    })
    // 队列分析 token_stats 不混入总结页
    wsHandler?.({
      type: 'token_stats',
      payload: { category: 'chapter', input_tokens: 999, output_tokens: 999 },
    })
    wsHandler?.({
      type: 'summary_progress',
      payload: {
        type: 'recheck_done',
        book_id: 'B',
        recheck: {
          status: 'COMPLETED',
          calls: 4,
          coverage: {
            checked: 8,
            candidates_total: 8,
            not_checked: 0,
            v3_tiers: { RESOLVED_EXPLICIT: 3, RESOLVED_IMPLICIT: 2, PARTIALLY_RESOLVED: 1, UNRESOLVED: 2 },
          },
        },
      },
    })
    await nextTick()
    expect(wrapper.text()).toContain('显式回收 3')
    expect(wrapper.text()).toContain('隐式回收 2')
    expect(wrapper.text()).toContain('阶段性回收 1')
    expect(wrapper.text()).toContain('未回收 2')
    expect(wrapper.text()).toContain('入100 出50')
    expect(wrapper.text()).not.toContain('999')

    // 切换复检明细 Tab 验证逐条证据与层级过滤
    await wrapper.findAll('button[role="tab"]').find((b) => b.text().includes('复检明细'))!.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('青铜钥匙')
    expect(wrapper.text()).toContain('[单元 12]')
    expect(wrapper.text()).toContain('黑袍人身份')
    await wrapper.findAll('button').find((b) => b.text().trim() === '未回收')!.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('黑袍人身份')
    expect(wrapper.text()).not.toContain('第12章交待了钥匙用途。')

    mockedApi.getBookReport.mockClear()
    mockedApi.summaryStatus.mockResolvedValue({
      running: false, book_id: 'B', phase: 'complete', batches_done: 3, total_batches: 3,
      error: '', started_at: 1, finished_at: 2, token_stats: {},
    } as Awaited<ReturnType<typeof api.summaryStatus>>)
    wsHandler?.({ type: 'state_change', payload: { state: 'summary_done', detail: '总结完成: B' } })
    await settle()
    expect(wrapper.findComponent(LogConsole).exists()).toBe(false)
    expect(mockedApi.getBookReport).toHaveBeenCalledWith('B')
    wrapper.unmount()
  })

  it('SummaryPage：写作风格量化卡片正确格式化 literary_ratio（不重复乘100）并支持直接导出 Excel', async () => {
    mockedApi.getStyleProfile.mockResolvedValue({
      book_id: 'A',
      exists: true,
      statistical: {
        avg_sentence_length: 23.4,
        dialogue_ratio: 0.352,
        short_sentence_ratio: 0.42,
        literary_ratio: 1.85,
        colloquial_ratio: 0.65,
      },
      semantic: { narrative_rhythm: '快节奏推进' },
      sample_chapters: [1, 10],
    })

    const wrapper = mount(SummaryPage)
    await settle()
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'A')
    await settle()

    const txt = wrapper.text()
    expect(txt).toContain('23.4 字')
    expect(txt).toContain('35.2%')
    expect(txt).toContain('1.85%')
    expect(txt).toContain('0.65%')
    expect(txt).not.toContain('185%')

    await wrapper.findAll('button').find((b) => b.text().includes('导出 Excel'))!.trigger('click')
    await settle()
    expect(mockedApi.exportAggregateExcel).toHaveBeenCalledWith('A')
    wrapper.unmount()
  })

  it('SummaryPage：有报告时默认折叠生成设置、展示目录与限宽正文，并支持展开设置与目录跳转', async () => {
    mockedApi.getBookReport.mockResolvedValue({
      book_id: 'A',
      report: '# **总纲**\n\n内容一\n\n## 第一卷\n\n内容二',
    })
    const wrapper = mount(SummaryPage)
    await settle()
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'A')
    await settle()

    const settingsPanel = wrapper.get('[data-test="settings-panel"]')
    expect((settingsPanel.element as HTMLElement).style.display).toBe('none')
    expect(wrapper.find('.report-prose').exists()).toBe(true)
    expect(wrapper.find('#md-h-1').exists()).toBe(true)
    expect(wrapper.find('#md-h-2').exists()).toBe(true)

    const tocLink = wrapper.findAll('aside .toc-link').find((b) => b.text() === '第一卷')!
    expect(tocLink).toBeDefined()
    await tocLink.trigger('click')
    expect(tocLink.classes()).toContain('is-active')

    await wrapper.findAll('button').find((b) => b.text().includes('生成设置'))!.trigger('click')
    await nextTick()
    expect((settingsPanel.element as HTMLElement).style.display).not.toBe('none')
    wrapper.unmount()
  })
})

describe('StatsPage 5-Tab Hub & /prompt Redirect', () => {
  it('StatsPage：5-Tab 收口，按书历史与 Prompt 预览按 v-if 懒挂载且 BookSelector syncGlobal=false，支持 /prompt 重定向', async () => {
    const promptRec = appRouter.getRoutes().find((r) => r.path === '/prompt')
    expect(typeof promptRec?.redirect).toBe('function')
    const redirectFn = promptRec!.redirect as (to: ReturnType<typeof appRouter.resolve>) => unknown
    expect(redirectFn(appRouter.resolve('/prompt?book=A'))).toEqual({
      path: '/stats',
      query: { book: 'A', tab: 'prompt' },
    })

    const wrapper = mount(StatsPage)
    await settle()

    // 默认在「账本概览」Tab：不挂载按书历史/Prompt 的 BookSelector
    expect(wrapper.findComponent({ name: 'BookSelector' }).exists()).toBe(false)

    // 切至「实时日志」Tab
    await wrapper.findAll('button[role="tab"]').find((b) => b.text().includes('实时日志'))!.trigger('click')
    await nextTick()
    expect(wrapper.findComponent(LogConsole).exists()).toBe(true)

    // 切至「Prompt 预览」Tab：BookSelector syncGlobal=false 且支持预览
    await wrapper.findAll('button[role="tab"]').find((b) => b.text().includes('Prompt 预览'))!.trigger('click')
    await nextTick()

    const bs = wrapper.findComponent({ name: 'BookSelector' })
    expect(bs.exists()).toBe(true)
    expect(bs.props('syncGlobal')).toBe(false)

    bs.vm.$emit('update:modelValue', 'A')
    await nextTick()
    await wrapper.findAll('button').find((b) => b.text().includes('生成预览'))!.trigger('click')
    await settle()
    expect(mockedApi.promptPreview).toHaveBeenCalledWith('A', 0, 1)
    expect(wrapper.text()).toContain('System Prompt')
    wrapper.unmount()
  })

  it('StatsPage：ConfirmDialog 清零替代原生弹窗', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockImplementation(() => true)
    try {
      const statsWrapper = mount(StatsPage)
      await settle()
      await statsWrapper.findAll('button').find((b) => b.text().includes('清零账本'))!.trigger('click')
      await nextTick()
      expect(confirmSpy).not.toHaveBeenCalled()
      expect(statsWrapper.findComponent(ConfirmDialog).exists()).toBe(true)
      await statsWrapper.findComponent(ConfirmDialog).vm.$emit('confirm')
      await settle()
      expect(mockedApi.clearUsage).toHaveBeenCalledTimes(1)
      statsWrapper.unmount()
    } finally {
      confirmSpy.mockRestore()
    }
  })
})

describe('Routes, CharacterCardPage & WorldStagePage', () => {
  it('router 包含 characters 命名路由，CharacterCardPage 消费 ?book&id 并支持别名搜索与语义抽屉', async () => {
    const resolved = appRouter.resolve({ name: 'characters', query: { book: 'A', id: '张三' } })
    expect(resolved.path).toBe('/characters')

    mockedApi.getAtlas.mockResolvedValue({
      book_id: 'A',
      exists: true,
      schema_version: 1,
      generated_at: '2026-11-23',
      model: 'm-a',
      characters: [
        {
          canonical_name: '张三',
          aliases: ['张三丰'],
          faction: '武当',
          role_tier: 'protagonist',
          first_chapter: 1,
          identity_summary: '测试人物',
          arc_milestones: [{ chapter_range: '1-5', milestone: '悟道' }],
        },
        {
          canonical_name: '李四',
          aliases: [],
          faction: '武当',
          role_tier: 'major_ally',
          first_chapter: 2,
          identity_summary: '同行伙伴',
          arc_milestones: [],
        },
      ],
      relationships: [
        {
          source: '张三',
          target: '李四',
          relation_tag: '同门',
          category: 'ally',
          evolution: '稳固',
          evidence_chapters: [1],
        },
      ],
      stages: [],
      world_tree: [],
    })
    mockedApi.getBookCharacters.mockResolvedValue({
      book_id: 'A',
      characters: [{ name: '张三', aliases: ['张三丰'], faction: '武当', in_atlas: true, total_events: 3, chapters_count: 2 }],
    })
    mockedApi.getCharacterCard.mockResolvedValue({
      book_id: 'A',
      character: {
        name: '张三',
        aliases: ['张三丰'],
        faction: '武当',
        role_tier: 'protagonist',
        identity_summary: '测试人物',
        arc_milestones: [{ chapter_range: '1-5', milestone: '悟道' }],
        semantic_relationships: [{ peer: '李四', direction: 'out', relation_tag: '同门', category: 'ally', evolution: '稳固', evidence_chapters: [1] }],
        first_appearance: 1,
        chapters: [1, 2],
        total_events: 3,
        arcs: [],
        events: [],
        states: {},
        relationships: {},
      },
    })

    const testRouter = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/characters', name: 'characters', component: CharacterCardPage },
        { path: '/summary', component: { template: '<div />' } },
      ],
    })
    await testRouter.push({ name: 'characters', query: { book: 'A', id: '张三' } })
    await testRouter.isReady()

    const wrapper = mount(CharacterCardPage, { global: { plugins: [testRouter] } })
    await settle()
    expect(mockedApi.getBookCharacters).toHaveBeenCalledWith('A')
    expect(mockedApi.getCharacterCard).toHaveBeenCalledWith('A', '张三')
    expect(echartsSetOption).toHaveBeenCalled()
    expect(wrapper.text()).toContain('张三丰')
    expect(wrapper.text()).toContain('悟道')

    await wrapper.findAll('button').find((b) => b.text().includes('全部角色索引'))!.trigger('click')
    await nextTick()
    const search = wrapper.find('input[placeholder*="搜索角色名"]')
    await search.setValue('张三丰')
    expect(wrapper.text()).toContain('张三')

    // 详情卡 2-Tab 切换（概览与关系 | 分章轨迹）
    await wrapper.findAll('button[role="tab"]').find((b) => b.text().includes('分章轨迹'))!.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('出场章节')
    wrapper.unmount()

    // 概览区块全空且轨迹有数据时，viewCard 自动切至「分章轨迹」
    mockedApi.getBookCharacters.mockResolvedValueOnce({
      book_id: 'A',
      characters: [{ name: '李四', aliases: [], faction: '武当', in_atlas: true, total_events: 1, chapters_count: 2 }],
    })
    await testRouter.push({ name: 'characters', query: { book: 'A', id: '李四' } })
    await testRouter.isReady()
    mockedApi.getCharacterCard.mockResolvedValueOnce({
      book_id: 'A',
      character: {
        name: '李四', aliases: [], faction: '武当', role_tier: 'major_ally', identity_summary: '',
        arc_milestones: [], semantic_relationships: [], first_appearance: 2, chapters: [2, 3],
        total_events: 1,
        arcs: [{ chapter: 2, surface_action: '下山送信', inner_motivation: '忠义', change_delta: '+1', driver: '师命' }],
        events: [], states: {}, relationships: {},
      },
    })
    const wrapper2 = mount(CharacterCardPage, { global: { plugins: [testRouter] } })
    await settle()
    const trajTab = wrapper2.findAll('button[role="tab"]').find((b) => b.text().includes('分章轨迹'))!
    expect(trajTab.attributes('aria-selected')).toBe('true')
    expect(wrapper2.text()).toContain('下山送信')
    wrapper2.unmount()
  })

  it('WorldStagePage：未生成图谱展示引导，多世界根默认包裹虚拟根节点且点击 Pill 聚焦单世界', async () => {
    const testRouter = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/world', component: WorldStagePage },
        { path: '/summary', component: { template: '<div />' } },
      ],
    })
    await testRouter.push('/world')
    await testRouter.isReady()

    const wrapper = mount(WorldStagePage, { global: { plugins: [testRouter] } })
    await settle()

    // Case 1: 未生成图谱
    mockedApi.getAtlas.mockResolvedValueOnce({
      book_id: 'A', exists: false, schema_version: 1, generated_at: '', model: '',
      characters: [], relationships: [], stages: [], world_tree: [],
    })
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'A')
    await settle()
    expect(wrapper.text()).toContain('前往最终总结')

    // Case 2: 多世界根 -> 虚拟根 + 单世界 Pill 切换
    mockedApi.getAtlas.mockResolvedValueOnce({
      book_id: 'B',
      exists: true,
      schema_version: 1,
      generated_at: '2026-11-23',
      model: 'm-a',
      characters: [],
      relationships: [],
      stages: [{
        stage_no: 1,
        title: '凡界初行',
        start_chapter: 1,
        end_chapter: 50,
        world_realm: '下界',
        primary_location: '青云村',
        key_sub_locations: ['后山'],
        active_factions: ['青云门'],
        core_conflict: '宗门试炼',
        map_switch_reason: '飞升上界',
      }],
      world_tree: [
        { name: '下界', node_type: 'world', summary: '凡人界', children: [] },
        { name: '上界', node_type: 'world', summary: '仙界', children: [] },
      ],
    })
    echartsSetOption.mockClear()
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'B')
    await settle()

    expect(wrapper.text()).toContain('凡界初行')
    expect(wrapper.text()).toContain('飞升上界')
    expect(wrapper.text()).toContain('全部世界 (2)')
    const virtualOpt = echartsSetOption.mock.calls.at(-1)?.[0] as { series: [{ data: [{ name: string; node_type: string; children: unknown[] }] }] }
    expect(virtualOpt.series[0].data[0].name).toBe('B')
    expect(virtualOpt.series[0].data[0].node_type).toBe('virtual')
    expect(virtualOpt.series[0].data[0].children).toHaveLength(2)

    await wrapper.findAll('button.world-pill').find((b) => b.text() === '上界')!.trigger('click')
    await nextTick()
    const focusedOpt = echartsSetOption.mock.calls.at(-1)?.[0] as { series: [{ data: [{ name: string }] }] }
    expect(focusedOpt.series[0].data[0].name).toBe('上界')
    wrapper.unmount()
  })

  it('CharacterCardPage 适配视图/小节点缩放与 Escape 关闭抽屉；WorldStagePage 阶段按章升序时间轴与适配视图', async () => {
    const testRouter = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/characters', name: 'characters', component: CharacterCardPage },
        { path: '/world', component: WorldStagePage },
        { path: '/summary', component: { template: '<div />' } },
      ],
    })
    await testRouter.push({ name: 'characters', query: { book: 'A', id: '张三' } })
    await testRouter.isReady()

    mockedApi.getAtlas.mockResolvedValue({
      book_id: 'A',
      exists: true,
      schema_version: 1,
      generated_at: '2026-11-23',
      model: 'm-a',
      characters: [
        { canonical_name: '张三', aliases: [], faction: '武当', role_tier: 'protagonist', first_chapter: 1, identity_summary: '', arc_milestones: [] },
        { canonical_name: '李四', aliases: [], faction: '武当', role_tier: 'major_ally', first_chapter: 2, identity_summary: '', arc_milestones: [] },
      ],
      relationships: [],
      stages: [
        { stage_no: 1, title: '后期篇章', start_chapter: 51, end_chapter: 100, world_realm: '上界', primary_location: '天宫', key_sub_locations: [], active_factions: [], core_conflict: '飞升冲突', map_switch_reason: '' },
        { stage_no: 2, title: '序幕初章', start_chapter: 1, end_chapter: 50, world_realm: '下界', primary_location: '村落', key_sub_locations: [], active_factions: [], core_conflict: '初入修行', map_switch_reason: '破界飞升' },
      ],
      world_tree: [{ name: '全界', node_type: 'world', summary: '', children: [] }],
    })
    mockedApi.getBookCharacters.mockResolvedValue({
      book_id: 'A',
      characters: [{ name: '张三', aliases: [], faction: '武当', in_atlas: true, total_events: 1, chapters_count: 1 }],
    })
    mockedApi.getCharacterCard.mockResolvedValue({
      book_id: 'A',
      character: {
        name: '张三', aliases: [], faction: '武当', role_tier: 'protagonist', identity_summary: '',
        arc_milestones: [], semantic_relationships: [], first_appearance: 1, chapters: [1],
        total_events: 1, arcs: [], events: [], states: {}, relationships: {},
      },
    })

    echartsSetOption.mockClear()
    const charWrapper = mount(CharacterCardPage, { global: { plugins: [testRouter] } })
    await settle()
    const graphOpt = echartsSetOption.mock.calls.at(-1)?.[0] as { series: [{ zoom: number; force: { repulsion: number } }] }
    expect(graphOpt.series[0].zoom).toBeGreaterThan(1)
    expect(graphOpt.series[0].force.repulsion).toBeLessThanOrEqual(250)

    const beforeFitCalls = echartsSetOption.mock.calls.length
    await charWrapper.findAll('button').find((b) => b.text().trim() === '适配视图')!.trigger('click')
    expect(echartsSetOption.mock.calls.length).toBeGreaterThan(beforeFitCalls)

    expect(charWrapper.find('.char-detail-pane').exists()).toBe(true)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(charWrapper.find('.char-detail-pane').exists()).toBe(false)
    charWrapper.unmount()

    await testRouter.push('/world')
    await testRouter.isReady()
    const worldWrapper = mount(WorldStagePage, { global: { plugins: [testRouter] } })
    await settle()
    echartsSetOption.mockClear()
    worldWrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'A')
    await settle()

    const rows = worldWrapper.findAll('.stage-row')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('序幕初章')
    expect(rows[1].text()).toContain('后期篇章')

    await worldWrapper.findAll('button').find((b) => b.text().trim() === '适配视图')!.trigger('click')
    expect(echartsSetOption).toHaveBeenCalled()
    worldWrapper.unmount()
  })
})
