/**
 * SummaryPage 组件级测试：参数持久化的版本号串行化 + 全量 PUT 防 api_key 重置 +
 * paramsLoaded 门闩 + 切书守卫 + complete 收抽屉。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('../api/client', async () => ({
  api: {
    getSettings: vi.fn(),
    putSettings: vi.fn(),
    getModels: vi.fn(async () => ({ models: ['m-a'] })),
    startSummary: vi.fn(),
    stopSummary: vi.fn(),
    summaryStatus: vi.fn(),
    getBookReport: vi.fn(),
    runAggregate: vi.fn(),
    getAggregateFiles: vi.fn(async () => ({ files: [] })),
    getAggregateFile: vi.fn(),
    exportAggregateExcel: vi.fn(),
    getBooks: vi.fn(async () => ({ books: [] })),
  },
}))
// stub 成可控发射器（真实组件会自请求书目列表）
vi.mock('../components/BookSelector.vue', () => ({
  default: {
    name: 'BookSelector',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<select :value="modelValue" data-test="selector"
      @change="$emit('update:modelValue', $event.target.value)"></select>`,
  },
}))

import { api, type AppConfigDto } from '../api/client'
import SummaryPage from './SummaryPage.vue'
import LogConsole from '../components/LogConsole.vue'

const mockedApi = vi.mocked(api)

function mkCfg(): AppConfigDto {
  return {
    api: {
      base_url: 'https://x', api_key: 'sk-secret', model: 'm-a',
      summary_model: '', summary_thinking_mode: {},
    },
    analysis: { summary_batch_size: 30, summary_concurrency: 2 },
  } as unknown as AppConfigDto
}

/** 推进 fake timers 并排空微任务 */
const settle = () => vi.advanceTimersByTimeAsync(0)

async function mountPage() {
  const wrapper = mount(SummaryPage)
  await settle()
  return wrapper
}

async function pickBook(wrapper: ReturnType<typeof mount>, bookId: string) {
  wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', bookId)
  await settle()
}

beforeEach(() => {
  vi.useFakeTimers()
  mockedApi.getSettings.mockReset()
  mockedApi.putSettings.mockReset()
  mockedApi.summaryStatus.mockReset()
  mockedApi.getBookReport.mockReset()
  mockedApi.getSettings.mockResolvedValue(mkCfg())
  mockedApi.putSettings.mockResolvedValue({ ok: true })
  mockedApi.summaryStatus.mockResolvedValue(null as unknown as Awaited<ReturnType<typeof api.summaryStatus>>)
  mockedApi.getBookReport.mockResolvedValue({ book_id: '', report: '' })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('SummaryPage 总结参数持久化', () => {
  it('PUT 必须基于 GET 的完整配置整体回写——api_key 不得丢失（防全量替换重置密钥）', async () => {
    const wrapper = await mountPage()
    const batchInput = wrapper.findAll('input[type="number"]')[2]  // 批次大小
    batchInput.setValue('50')
    await vi.advanceTimersByTimeAsync(600)
    expect(mockedApi.putSettings).toHaveBeenCalledTimes(1)
    const payload = mockedApi.putSettings.mock.calls[0][0]
    expect(payload.api!.api_key).toBe('sk-secret')             // 核心：完整回写
    expect(payload.analysis!.summary_batch_size).toBe(50)
  })

  it('600ms 防抖：连续改两次只 PUT 一次，且带上最新值', async () => {
    const wrapper = await mountPage()
    const batchInput = wrapper.findAll('input[type="number"]')[2]
    batchInput.setValue('40')
    batchInput.setValue('50')
    await vi.advanceTimersByTimeAsync(599)
    expect(mockedApi.putSettings).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(mockedApi.putSettings).toHaveBeenCalledTimes(1)
    expect(mockedApi.putSettings.mock.calls[0][0].analysis!.summary_batch_size).toBe(50)
  })

  it('写入期间又有改动 → 补一轮写入（版本号串行化）', async () => {
    let releasePut!: (v: { ok: boolean }) => void
    mockedApi.putSettings.mockImplementation(
      () => new Promise<{ ok: boolean }>((resolve) => { releasePut = resolve }),
    )
    const wrapper = await mountPage()
    const batchInput = wrapper.findAll('input[type="number"]')[2]
    batchInput.setValue('50')
    await vi.advanceTimersByTimeAsync(600)   // 第一轮 PUT 挂起中
    expect(mockedApi.putSettings).toHaveBeenCalledTimes(1)
    batchInput.setValue('60')                // 写入期间再改 → 版本号 +1
    await vi.advanceTimersByTimeAsync(600)   // 第二轮防抖到点（第一轮还没返回）
    expect(mockedApi.putSettings.mock.calls.length).toBeGreaterThanOrEqual(2)
    releasePut({ ok: true })
    await vi.advanceTimersByTimeAsync(2000)  // 等补写轮完成
    const lastCall = mockedApi.putSettings.mock.calls[mockedApi.putSettings.mock.calls.length - 1][0]
    expect(lastCall.analysis!.summary_batch_size).toBe(60)  // 最终值不丢
  })

  it('paramsLoaded 门闩：初始回填服务端值不触发 PUT', async () => {
    mockedApi.getSettings.mockResolvedValue({
      ...mkCfg(),
      analysis: { summary_batch_size: 99, summary_concurrency: 7 },
    } as AppConfigDto)
    const wrapper = await mountPage()
    await vi.advanceTimersByTimeAsync(2000)  // 越过防抖窗口
    const inputs = wrapper.findAll('input[type="number"]')
    expect((inputs[2].element as HTMLInputElement).value).toBe('99')
    expect((inputs[3].element as HTMLInputElement).value).toBe('7')
    expect(mockedApi.putSettings).not.toHaveBeenCalled()
  })
})

describe('SummaryPage 切书与状态联动', () => {
  it('慢的 A 书报告不得挂到 B 名下（v-html 守卫）', async () => {
    let resolveA!: (v: { book_id: string; report: string }) => void
    mockedApi.getBookReport.mockImplementation((id: string) => {
      if (id === 'A') return new Promise((resolve) => { resolveA = resolve })
      return Promise.resolve({ book_id: id, report: '<p>B的报告</p>' })
    })
    const wrapper = await mountPage()
    await pickBook(wrapper, 'A')
    await pickBook(wrapper, 'B')
    expect(wrapper.text()).toContain('B的报告')
    resolveA({ book_id: 'A', report: '<p>A的过期报告</p>' })
    await settle()
    expect(wrapper.text()).not.toContain('A的过期报告')
  })

  it('阶段 complete → 总结日志抽屉自动收起', async () => {
    const wrapper = await mountPage()
    mockedApi.startSummary.mockResolvedValue({} as Awaited<ReturnType<typeof api.startSummary>>)
    await pickBook(wrapper, 'A')
    const startBtn = wrapper.findAll('button').find((b) => b.text().includes('开始总结'))
    await startBtn!.trigger('click')
    await settle()
    expect(wrapper.findComponent(LogConsole).exists()).toBe(true)   // 抽屉展开
    mockedApi.summaryStatus.mockResolvedValue({
      running: false, book_id: 'A', phase: 'complete', batches_done: 3, total_batches: 3,
      error: '', started_at: 1, finished_at: 2, token_stats: {},
    } as Awaited<ReturnType<typeof api.summaryStatus>>)
    await vi.advanceTimersByTimeAsync(2000)  // 触发轮询刷新状态
    await settle()
    expect(wrapper.findComponent(LogConsole).exists()).toBe(false)  // 抽屉收起
  })
})
