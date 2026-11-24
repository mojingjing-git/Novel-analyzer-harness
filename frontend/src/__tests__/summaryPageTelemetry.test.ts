import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { ref, nextTick } from 'vue'
import type { ProgressMessage } from '../api/useProgressSocket'

let wsHandler: ((msg: ProgressMessage) => void) | null = null

const mockApi = vi.hoisted(() => ({
  listBooks: vi.fn(),
  getSettings: vi.fn(),
  putSettings: vi.fn(),
  patchSettings: vi.fn(),
  getModels: vi.fn(),
  getPresets: vi.fn(),
  getModelSpec: vi.fn(),
  probeRecheck: vi.fn(),
  summaryStatus: vi.fn(),
  startSummary: vi.fn(),
  stopSummary: vi.fn(),
  getBookReport: vi.fn(),
  getStyleProfile: vi.fn(),
  getRecheckVerdicts: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../api/useProgressSocket', () => ({
  useProgressSocket: (cb: (msg: ProgressMessage) => void) => {
    wsHandler = cb
    return { connected: ref(true), reconnect: vi.fn() }
  },
}))

import SummaryPage from '../pages/SummaryPage.vue'

describe('SummaryPage — 并发车道与吞吐监控隔离', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-23T08:00:00Z'))
    wsHandler = null
    mockApi.listBooks.mockResolvedValue([
      { book_id: 'B1', name: '测试书目', chapter_count: 20, status: 'done' },
    ])
    mockApi.getSettings.mockResolvedValue({
      api: { base_url: 'http://127.0.0.1:9', api_key: 'k', model: 'm-main', provider: 'openai' },
      analysis: {
        concurrency: 5,
        summary_concurrency: 3,
        summary_batch_size: 10,
        v3_concurrency: 6,
      },
      gui: {},
    })
    mockApi.getModels.mockResolvedValue({ models: ['m-main'] })
    mockApi.getPresets.mockResolvedValue({ presets: {} })
    mockApi.getBookReport.mockResolvedValue({ book_id: 'B1', report: '' })
    mockApi.getStyleProfile.mockResolvedValue({ exists: false })
    mockApi.getRecheckVerdicts.mockResolvedValue({
      book_id: 'B1', exists: false, engine: '', status: '', status_reason: '',
      model: '', generated_at: '', coverage: {}, candidates_total: 0, verdicts: [],
    })
    mockApi.summaryStatus.mockResolvedValue({
      running: true,
      book_id: 'B1',
      phase: 'batch',
      batches_done: 0,
      total_batches: 3,
      concurrency: 3,
      inflight_lanes: [],
      started_at: Date.now() / 1000,
      finished_at: 0,
      error: '',
      token_stats: {},
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('独立展示 summary_concurrency、随 Stage 2 切换至 6 路复检并发并恢复，且 1s 缓冲忽略 analysis token_delta', async () => {
    const wrapper = mount(SummaryPage)
    await flushPromises()
    await nextTick()

    // 初始运行态采用 summary_concurrency=3，而非 analysis.concurrency=5；空转期显示 0 / 3 运行中
    expect(wrapper.find('.pm-concurrency').text()).toBe('阶段并发 3 路')
    expect(wrapper.find('.lane-sub').text()).toContain('0 / 3 运行中')
    expect(wrapper.find('.lane-idle').exists()).toBe(true)

    expect(wsHandler).not.toBeNull()

    // 1. Stage 1 启动真实车道 900001
    wsHandler!({
      type: 'summary_progress',
      payload: {
        type: 'lane_start',
        book_id: 'B1',
        lane_id: 900001,
        label: '卷1 批次摘要',
        concurrency: 3,
        ts: Date.now() / 1000,
      },
    })
    await nextTick()
    expect(wrapper.findAll('.lane-row')).toHaveLength(3)
    expect(wrapper.find('.lane-row .lane-range').text()).toBe('卷1 批次摘要')

    // 2. 收到 summary_phase_1 的 token_delta：吞吐 250ms 刷新且用对数刻度尺，车道保持 1000ms 刷新
    wsHandler!({
      type: 'token_delta',
      payload: {
        context: 'summary_phase_1',
        session_id: 'B1',
        unit_idx: 1,
        delta: { output_tokens: 400, rate_tokens_per_sec: 50, is_thinking: false },
        timestamp: Date.now(),
      },
    })
    await nextTick()
    // 0ms 时二者均尚未刷新
    expect(wrapper.find('.lane-tok').text()).toBe('')

    // 推进 2 次 250ms 周期（共 500ms）：吞吐率完成 220ms CountUp 达 50 tok/s 且渲染 2 点对数刻度曲线，但车道仍保持 1000ms 节奏未刷新
    await vi.advanceTimersByTimeAsync(250)
    await vi.advanceTimersByTimeAsync(250)
    expect(wrapper.find('.rd-rate-value').text()).toBe('50 tok/s')
    expect(wrapper.find('.rd-sparkline-svg').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('刻度上限')
    expect(wrapper.findAll('.rd-log-label').map((n) => n.text())).toEqual(['100', '10', '0'])
    expect(wrapper.find('.lane-tok').text()).toBe('')

    // 再推进 500ms（到达第 4 个 250ms 周期 = 1000ms）触发车道缓冲刷新，再推进 950ms 完成线性计数动画
    await vi.advanceTimersByTimeAsync(500)
    await vi.advanceTimersByTimeAsync(950)
    expect(wrapper.find('.lane-tok').text()).toBe('400 tok')
    expect(wrapper.find('.summary-telemetry').text()).toContain('总结输出吞吐')

    // 3. 收到 context='analysis' 的高额 token_delta（包括 unit_idx=900001）应被完全忽略
    wsHandler!({
      type: 'token_delta',
      payload: {
        context: 'analysis',
        session_id: 'other-analysis',
        unit_idx: 900001,
        delta: { output_tokens: 9999, rate_tokens_per_sec: 999, is_thinking: true },
        timestamp: Date.now(),
      },
    })
    await vi.advanceTimersByTimeAsync(2000)
    expect(wrapper.find('.lane-tok').text()).toBe('400 tok')
    expect(wrapper.find('.rd-rate-value').text()).toBe('50 tok/s')

    // 完成 900001
    wsHandler!({
      type: 'summary_progress',
      payload: {
        type: 'lane_done',
        book_id: 'B1',
        lane_id: 900001,
        ok: true,
        ts: Date.now() / 1000,
      },
    })
    await nextTick()

    // 4. Stage 2 伏笔复检切换到独立并发 6 路
    wsHandler!({
      type: 'summary_progress',
      payload: {
        type: 'phase',
        book_id: 'B1',
        phase: 'recheck',
        concurrency: 6,
        real_lanes: true,
      },
    })
    await nextTick()
    expect(wrapper.find('.pm-concurrency').text()).toBe('复检并发 6 路')
    expect(wrapper.findAll('.lane-row')).toHaveLength(6)

    // 5. 进入 Stage 4 恢复为 summary_concurrency=3 路
    wsHandler!({
      type: 'summary_progress',
      payload: {
        type: 'phase',
        book_id: 'B1',
        phase: 'report',
        concurrency: 3,
        real_lanes: true,
      },
    })
    await nextTick()
    expect(wrapper.find('.pm-concurrency').text()).toBe('阶段并发 3 路')
    expect(wrapper.findAll('.lane-row')).toHaveLength(3)

    wrapper.unmount()
  })

  it('summary_concurrency 为 0 时回退主分析 concurrency 展示并发车道并传 (0, 0) 给 startSummary', async () => {
    mockApi.getSettings.mockResolvedValueOnce({
      api: { base_url: 'http://127.0.0.1:9', api_key: 'k', model: 'm-main', provider: 'openai' },
      analysis: {
        concurrency: 7,
        summary_concurrency: 0,
        summary_batch_size: 25,
        recheck_concurrency: 0,
      },
      gui: {},
    })
    mockApi.summaryStatus
      .mockResolvedValueOnce({
        running: false,
        book_id: 'B1',
        phase: 'idle',
        batches_done: 0,
        total_batches: 0,
        started_at: 0,
        finished_at: 0,
        error: '',
        token_stats: {},
      })
      .mockResolvedValueOnce({
        running: true,
        book_id: 'B1',
        phase: 'batch',
        batches_done: 0,
        total_batches: 3,
        concurrency: 0,
        inflight_lanes: [],
        started_at: Date.now() / 1000,
        finished_at: 0,
        error: '',
        token_stats: {},
      })
    mockApi.startSummary.mockResolvedValueOnce({})

    const wrapper = mount(SummaryPage)
    await flushPromises()
    await nextTick()

    wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', 'B1')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('7 路')
    expect(wrapper.text()).toContain('(跟随主分析)')
    await wrapper.findAll('button').find((b) => b.text().includes('开始总结'))!.trigger('click')
    await flushPromises()
    await nextTick()
    expect(mockApi.startSummary).toHaveBeenCalledWith('B1', 1, 99999, 0, 0)
    expect(wrapper.find('.pm-concurrency').text()).toBe('阶段并发 7 路')
    expect(wrapper.find('.lane-sub').text()).toContain('0 / 7 运行中')

    wsHandler!({
      type: 'summary_progress',
      payload: {
        type: 'lane_start',
        book_id: 'B1',
        lane_id: 900001,
        label: '卷1 批次摘要',
        ts: Date.now() / 1000,
      },
    })
    await nextTick()
    expect(wrapper.findAll('.lane-row')).toHaveLength(7)

    wrapper.unmount()
  })

  it('阶段卡片实时显示分卷摘要/调和子进度与伏笔复检剩余条数', async () => {
    mockApi.getSettings.mockResolvedValueOnce({
      api: { base_url: 'http://127.0.0.1:9', api_key: 'k', model: 'm-main', provider: 'openai' },
      analysis: {
        concurrency: 4,
        summary_concurrency: 3,
        summary_batch_size: 20,
        recheck_concurrency: 4,
      },
      gui: {},
    })
    mockApi.summaryStatus.mockResolvedValueOnce({
      running: true,
      book_id: 'B1',
      phase: 'batch',
      batches_done: 0,
      total_batches: 3,
      summary_done: 0,
      recon_done: 0,
      concurrency: 3,
      inflight_lanes: [],
      started_at: Date.now() / 1000,
      finished_at: 0,
      error: '',
      token_stats: {},
    })

    const wrapper = mount(SummaryPage)
    await flushPromises()
    await nextTick()

    // 1. Stage 1：收到 stage1_progress，Step 1 卡片与 .progress-meta 同时展示摘要/调和子进度
    wsHandler!({
      type: 'summary_progress',
      payload: {
        type: 'stage1_progress',
        book_id: 'B1',
        summary_done: 2,
        recon_done: 1,
        batches_done: 1,
        total_batches: 3,
      },
    })
    await nextTick()

    const steps1 = wrapper.findAll('.step-row .step')
    expect(steps1[0].find('.step-sub').text()).toBe('摘要 2/3 · 调和 1/3')
    expect(steps1[0].find('.step-mini-fill').attributes('style')).toContain('50%')
    expect(wrapper.find('.progress-meta .pm-sub').text()).toContain('摘要 2/3 · 调和 1/3')

    // 2. 进入 Stage 2：收到 recheck_progress (初判 4/10 · 剩6条 -> 终审 7/10 · 剩3条)
    wsHandler!({
      type: 'summary_progress',
      payload: {
        type: 'phase',
        book_id: 'B1',
        phase: 'recheck',
        concurrency: 4,
        real_lanes: true,
      },
    })
    await nextTick()
    expect(wrapper.find('.progress-meta .pm-sub').text()).toContain('准备复检索引')

    wsHandler!({
      type: 'summary_progress',
      payload: {
        type: 'recheck_progress',
        book_id: 'B1',
        recheck_progress: {
          engine: 'v3',
          stage: 'u3_hop',
          hop: 1,
          stage_done: 4,
          stage_total: 10,
          remaining: 6,
          items_total: 10,
          checked: 4,
          calls: 2,
          unit: '条',
        },
      },
    })
    await nextTick()

    const steps2 = wrapper.findAll('.step-row .step')
    expect(steps2[0].find('.step-sub').text()).toBe('3/3 卷')
    expect(steps2[1].find('.step-sub').text()).toBe('初判 4/10 · 剩6条')
    expect(steps2[1].find('.step-mini-fill').attributes('style')).toContain('50%')
    expect(wrapper.find('.progress-meta .pm-sub').text()).toBe('初判 (1/3跳) 4/10 条 · 剩余 6 条')

    wsHandler!({
      type: 'summary_progress',
      payload: {
        type: 'recheck_progress',
        book_id: 'B1',
        recheck_progress: {
          engine: 'v3',
          stage: 'u4_audit',
          hop: 0,
          stage_done: 7,
          stage_total: 10,
          remaining: 3,
          items_total: 10,
          checked: 7,
          calls: 5,
          unit: '条',
        },
      },
    })
    await nextTick()

    expect(wrapper.findAll('.step-row .step')[1].find('.step-sub').text()).toBe('终审 7/10 · 剩3条')
    expect(wrapper.find('.progress-meta .pm-sub').text()).toBe('四防终审 7/10 条 · 剩余 3 条')

    // 3. 复检完成进入风格分析：Step 2 显示已检汇总，Step 3 显示分析中
    wsHandler!({
      type: 'summary_progress',
      payload: {
        type: 'recheck_done',
        book_id: 'B1',
        recheck: {
          status: 'COMPLETED',
          calls: 6,
          coverage: { checked: 8, candidates_total: 10, not_checked: 2 },
        },
      },
    })
    wsHandler!({
      type: 'summary_progress',
      payload: {
        type: 'phase',
        book_id: 'B1',
        phase: 'style',
        concurrency: 3,
        real_lanes: true,
      },
    })
    await nextTick()

    const steps3 = wrapper.findAll('.step-row .step')
    expect(steps3[1].find('.step-sub').text()).toBe('已检 8/10 条')
    expect(steps3[2].find('.step-sub').text()).toBe('分析中…')

    wrapper.unmount()
  })
})
