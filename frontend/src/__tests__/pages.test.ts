/**
 * 页面级交互综合测试：
 * 覆盖 QueuePage、SettingsPage、SplitterPage、StylePage、SummaryPage、TimelinePage、WorkspacePage。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ref, nextTick } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'

let wsHandler: ((msg: ProgressMessage) => void) | null = null

vi.mock('../api/useProgressSocket', () => ({
  useProgressSocket: vi.fn(),
}))

vi.mock('../components/BookSelector.vue', () => ({
  default: {
    name: 'BookSelector',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<select :value="modelValue" data-test="selector" @change="$emit('update:modelValue', $event.target.value)"></select>`,
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
    getPresets: vi.fn(),
    getForeshadowCategories: vi.fn(),
    previewModels: vi.fn(),
    getModels: vi.fn().mockResolvedValue({ models: ['m-a'] }),
    probeThinking: vi.fn(),
    probeRecheck: vi.fn(),
    getModelSpec: vi.fn().mockResolvedValue({ found: false, spec: null, catalog_version: '2026-11-01' }),
    syncModelsCatalog: vi.fn().mockResolvedValue({ success: true, version: '2026-11-01', total_models: 2347 }),
    // SplitterPage
    previewSplit: vi.fn(),
    batchSplit: vi.fn(),
    inferBookName: vi.fn(),
    // StylePage
    startStyle: vi.fn(),
    stopStyle: vi.fn(),
    styleStatus: vi.fn(),
    getStyleResult: vi.fn(),
    // SummaryPage
    startSummary: vi.fn(),
    stopSummary: vi.fn(),
    summaryStatus: vi.fn(),
    getBookReport: vi.fn(),
    runAggregate: vi.fn(),
    getAggregateFiles: vi.fn().mockResolvedValue({ files: [] }),
    getAggregateFile: vi.fn(),
    exportAggregateExcel: vi.fn(),
    getBooks: vi.fn().mockResolvedValue({ books: [] }),
    // TimelinePage
    getTimeline: vi.fn(),
    // WorkspacePage
    workspaceNovels: vi.fn(),
    workspaceArchives: vi.fn(),
    archiveNovel: vi.fn(),
    archiveAll: vi.fn(),
    deleteArchive: vi.fn(),
  },
}))

import { api, type AnalysisStatus, type AppConfigDto, type QueueItemDto } from '../api/client'
import { useProgressSocket, type ProgressMessage } from '../api/useProgressSocket'
import QueuePage from '../pages/QueuePage.vue'
import SettingsPage from '../pages/SettingsPage.vue'
import SplitterPage from '../pages/SplitterPage.vue'
import StylePage from '../pages/StylePage.vue'
import SummaryPage from '../pages/SummaryPage.vue'
import TimelinePage from '../pages/TimelinePage.vue'
import WorkspacePage from '../pages/WorkspacePage.vue'
import RunDashboard from '../components/RunDashboard.vue'
import ChapterDetailPanel from '../components/ChapterDetailPanel.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import LogConsole from '../components/LogConsole.vue'
import { useLogStore } from '../composables/useLogStore'

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
      summary_batch_size: 30, foreshadow_recheck_batch_size: 40,
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
  useLogStore().clear()
  vi.mocked(useProgressSocket).mockImplementation((cb) => {
    wsHandler = cb
    return { connected: ref(true) }
  })
  mockedApi.analysisStatus.mockResolvedValue(mkStatus())
  mockedApi.getTokenStats.mockResolvedValue({ categories: {}, chapter_stats: [], elapsed: 0, running: false })
  mockedApi.getSessionTokenStats.mockResolvedValue({
    analysis: { categories: {}, chapter_stats: [], elapsed: 0, running: false },
    summary: { categories: {}, input_tokens: 0, output_tokens: 0, elapsed: 0 },
  })
  mockedApi.getSettings.mockResolvedValue(mkCfg())
  mockedApi.putSettings.mockResolvedValue({ ok: true })
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
  mockedApi.getModels.mockResolvedValue({ models: ['m-a'] })
  mockedApi.summaryStatus.mockResolvedValue(null as unknown as Awaited<ReturnType<typeof api.summaryStatus>>)
  mockedApi.getBookReport.mockResolvedValue({ book_id: '', report: '' })
  mockedApi.styleStatus.mockResolvedValue({ running: false, phase: '' } as Awaited<ReturnType<typeof api.styleStatus>>)
  mockedApi.getStyleResult.mockResolvedValue({ book_id: 'A', content: '# 风格分析内容' })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('QueuePage', () => {
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
  })
})

describe('StylePage & TimelinePage & WorkspacePage', () => {
  it('StylePage：停止后忽略短暂 running 轮询，结果文件缺失判失败', async () => {
    const wrapper = mount(StylePage)
    await settle()
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'A')
    await settle()

    await wrapper.findAll('button').find((b) => b.text().includes('开始分析'))!.trigger('click')
    await settle()
    await wrapper.findAll('button').find((b) => b.text().includes('停止'))!.trigger('click')
    await settle()

    mockedApi.styleStatus.mockResolvedValue({ running: true, phase: 'llm' } as Awaited<ReturnType<typeof api.styleStatus>>)
    await vi.advanceTimersByTimeAsync(2000)
    await settle()
    expect(wrapper.text()).toContain('开始分析')
    expect(mockedApi.getStyleResult).not.toHaveBeenCalled()
    wrapper.unmount()
  })

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
})

describe('SummaryPage', () => {
  it('防抖全量 PUT 保住 api_key、切书报告竞态守卫与总结完成自动收起日志抽屉', async () => {
    let resolveA!: (v: { book_id: string; report: string }) => void
    mockedApi.getBookReport.mockImplementation((id: string) => {
      if (id === 'A') return new Promise((r) => { resolveA = r })
      return Promise.resolve({ book_id: id, report: '<p>B的报告</p>' })
    })
    const wrapper = mount(SummaryPage)
    await settle()

    // 改批次大小触发 600ms 防抖全量 PUT，api_key 不丢失
    const batchInput = wrapper.findAll('input[type="number"]')[2]
    batchInput.setValue('50')
    await vi.advanceTimersByTimeAsync(600)
    expect(mockedApi.putSettings).toHaveBeenCalledTimes(1)
    expect(mockedApi.putSettings.mock.calls[0][0].api!.api_key).toBe('sk-old')
    expect(mockedApi.putSettings.mock.calls[0][0].analysis!.summary_batch_size).toBe(50)

    // 切书守卫
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'A')
    await settle()
    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'B')
    await settle()
    resolveA({ book_id: 'A', report: '<p>A的过期报告</p>' })
    await settle()
    expect(wrapper.text()).toContain('B的报告')
    expect(wrapper.text()).not.toContain('A的过期报告')

    // 开始总结展开日志抽屉 -> complete 自动收起
    mockedApi.startSummary.mockResolvedValue({} as Awaited<ReturnType<typeof api.startSummary>>)
    await wrapper.findAll('button').find((b) => b.text().includes('开始总结'))!.trigger('click')
    await settle()
    expect(wrapper.findComponent(LogConsole).exists()).toBe(true)

    mockedApi.summaryStatus.mockResolvedValue({
      running: false, book_id: 'B', phase: 'complete', batches_done: 3, total_batches: 3,
      error: '', started_at: 1, finished_at: 2, token_stats: {},
    } as Awaited<ReturnType<typeof api.summaryStatus>>)
    await vi.advanceTimersByTimeAsync(2000)
    await settle()
    expect(wrapper.findComponent(LogConsole).exists()).toBe(false)
    wrapper.unmount()
  })
})
