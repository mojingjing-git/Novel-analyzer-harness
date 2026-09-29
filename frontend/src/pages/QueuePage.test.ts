/**
 * QueuePage 组件级测试：WS 状态机分发（H17）/ F-4 按回调分键防抖 / P2 按名称删书 / M-3 进度清零。
 * WS 回调经 vi.mock ../api/useProgressSocket 捕获后直接喂消息；时间敏感统一 fake timers。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref, nextTick } from 'vue'

let wsHandler: ((msg: ProgressMessage) => void) | null = null

vi.mock('../api/useProgressSocket', () => ({
  useProgressSocket: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: {
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
  },
}))

import { api } from '../api/client'
import { useProgressSocket, type ProgressMessage } from '../api/useProgressSocket'
import QueuePage from './QueuePage.vue'
import RunDashboard from '../components/RunDashboard.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { useLogStore } from '../composables/useLogStore'
import type { AnalysisStatus, QueueItemDto } from '../api/client'

const mockedApi = vi.mocked(api)

function mkItem(partial: Partial<QueueItemDto>): QueueItemDto {
  return {
    name: '', blocks_dir: '', workspace_dir: '', status: 'pending',
    total_chapters: 0, completed_chapters: 0, block_size: 1,
    start_time: 0, end_time: 0, error_message: '', archive_failed: false,
    ...partial,
  }
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

/** 推进 fake timers 同时排空微任务（挂载/喂消息后的统一 flush 手法） */
const settle = () => vi.advanceTimersByTimeAsync(0)

async function mountPage() {
  const wrapper = mount(QueuePage, {
    global: { directives: { tooltip: {} } },
  })
  await settle()
  return wrapper
}

function feed(msg: ProgressMessage) {
  wsHandler?.(msg)
}

function dashProps(wrapper: ReturnType<typeof mount>) {
  return wrapper.findComponent(RunDashboard).props() as unknown as {
    activeBlocks: Map<number, { range: string; startedAt: number }>
    finishedBlocks: Map<number, { ok: boolean; range: string }>
    tokenByBlock: Map<number, { outputTokens: number; lastTokenAt: number }>
    discoveries: Array<{ id: number; highlighted: boolean; events: number }>
    progress: { current: number; total: number; eta: string }
    aggregateRate: number
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  // 逐个 mockReset（不用 resetAllMocks：避免清掉 useProgressSocket 捕获器实现）
  for (const key of Object.keys(mockedApi)) {
    (mockedApi[key as keyof typeof mockedApi] as unknown as { mockReset: () => void }).mockReset()
  }
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
})

describe('QueuePage WS 状态机（H17）', () => {
  it('block_start → 活跃块登记并透传给 RunDashboard', async () => {
    const wrapper = await mountPage()
    feed({ type: 'block_start', payload: { chapter: 9, range: 'ch9-ch10', progress: 0, total: 0, ts: 100 } })
    await nextTick()
    const p = dashProps(wrapper)
    expect(p.activeBlocks.get(9)?.range).toBe('ch9-ch10')
  })

  it('block_done → 移出活跃 + 登记 finished + 清理该块速率（H17 P3 V2）', async () => {
    const wrapper = await mountPage()
    feed({ type: 'block_start', payload: { chapter: 9, range: 'ch9-ch10', progress: 0, total: 0, ts: 100 } })
    feed({ type: 'token_delta', payload: { context: 'analysis', session_id: 's', unit_idx: 9, delta: { output_tokens: 100, rate_tokens_per_sec: 55 }, timestamp: 0 } })
    await nextTick()
    expect(dashProps(wrapper).aggregateRate).toBe(55)
    feed({ type: 'block_done', payload: { chapter: 9, ok: true, range: 'ch9-ch10' } })
    await nextTick()
    const p = dashProps(wrapper)
    expect(p.activeBlocks.has(9)).toBe(false)
    expect(p.finishedBlocks.get(9)?.ok).toBe(true)
    // 修复语义：done 后聚合速率归零，尾速不再算进"当前聚合"
    expect(p.aggregateRate).toBe(0)
  })

  it('token_delta 同块累计取最大非相加；聚合 = 活跃块速率之和', async () => {
    const wrapper = await mountPage()
    feed({ type: 'block_start', payload: { chapter: 1, range: 'ch1', progress: 0, total: 0, ts: 1 } })
    feed({ type: 'block_start', payload: { chapter: 2, range: 'ch2', progress: 0, total: 0, ts: 1 } })
    feed({ type: 'token_delta', payload: { context: 'analysis', session_id: 's', unit_idx: 1, delta: { output_tokens: 100, rate_tokens_per_sec: 20 }, timestamp: 0 } })
    feed({ type: 'token_delta', payload: { context: 'analysis', session_id: 's', unit_idx: 1, delta: { output_tokens: 50, rate_tokens_per_sec: 20 }, timestamp: 0 } })
    feed({ type: 'token_delta', payload: { context: 'analysis', session_id: 's', unit_idx: 2, delta: { output_tokens: 10, rate_tokens_per_sec: 30 }, timestamp: 0 } })
    await nextTick()
    const p = dashProps(wrapper)
    expect(p.tokenByBlock.get(1)?.outputTokens).toBe(100)  // max(100,50)
    expect(p.aggregateRate).toBe(50)                        // 20 + 30
  })

  it('discovery → 发现流追加，新伏笔置 highlighted', async () => {
    const wrapper = await mountPage()
    feed({ type: 'discovery', payload: { events: 3, foreshadows: ['玄铁令'], characters: [], unresolved: [] } })
    await nextTick()
    const p = dashProps(wrapper)
    expect(p.discoveries[0]).toMatchObject({ events: 3, highlighted: true })
  })

  it('summary_progress → 顶部横幅提示 + 合成总结车道（≥900000）', async () => {
    const wrapper = await mountPage()
    feed({ type: 'summary_progress', payload: { type: 'phase', phase: 'batch', total_batches: 3, batches_done: 0 } })
    await nextTick()
    expect(wrapper.text()).toContain('最终总结进行中')
    const p = dashProps(wrapper)
    expect(p.activeBlocks.size).toBe(3)
    for (const id of p.activeBlocks.keys()) expect(id).toBeGreaterThanOrEqual(900000)
  })
})

describe('QueuePage F-4 防抖刷新（按回调分键）', () => {
  it('多次 block_done 合并为一次 REST 刷新；token_stats 的刷新不被吞', async () => {
    const wrapper = await mountPage()
    const statusCalls = mockedApi.analysisStatus.mock.calls.length  // 挂载时 1 次
    feed({ type: 'block_done', payload: { chapter: 1, ok: true, range: 'ch1' } })
    feed({ type: 'block_done', payload: { chapter: 2, ok: true, range: 'ch2' } })
    feed({ type: 'block_done', payload: { chapter: 3, ok: true, range: 'ch3' } })
    feed({ type: 'token_stats', payload: { category: 'analysis', input_tokens: 1, output_tokens: 1 } })
    await nextTick()
    expect(mockedApi.analysisStatus.mock.calls.length).toBe(statusCalls)      // 500ms 内不发
    await vi.advanceTimersByTimeAsync(500)
    expect(mockedApi.analysisStatus.mock.calls.length).toBe(statusCalls + 1)  // 3 → 1 合并
    expect(mockedApi.getTokenStats).toHaveBeenCalledTimes(1)                  // 另一键独立触发
  })

  it('卸载后轮询与 pending 防抖全部清空，不再发请求', async () => {
    const wrapper = await mountPage()
    feed({ type: 'block_done', payload: { chapter: 1, ok: true, range: 'ch1' } })  // 挂一个未到期的防抖
    const calls = mockedApi.analysisStatus.mock.calls.length
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(20_000)  // 覆盖 4 个轮询周期 + 防抖到期
    expect(mockedApi.analysisStatus.mock.calls.length).toBe(calls)
  })
})

describe('QueuePage 删除确认（P2 按名称解析索引）', () => {
  const bookA = mkItem({ name: '《A》', status: 'done' })
  const bookB = mkItem({ name: '《B》', status: 'done' })

  it('确认时按名称实时解析：轮询重排后删的是重排后的索引', async () => {
    mockedApi.analysisStatus.mockResolvedValue(mkStatus({ items: [bookA, bookB] }))
    const wrapper = await mountPage()
    const delBtns = wrapper.findAll('tbody button').filter((b) => b.text().trim() === '删除')
    await delBtns[1].trigger('click')  // 点第二行《B》
    await settle()
    const dialog = wrapper.findComponent(ConfirmDialog)
    expect(dialog.exists()).toBe(true)
    // 文案里书名号双重包裹（item.name 自带《》，模板再包一层）——既有 UX
    expect(wrapper.text()).toContain('确定要删除《《B》》吗')
    // 模拟轮询重排：B 换到第 0 位（fake timers 推进 5s 触发轮询刷新）
    mockedApi.analysisStatus.mockResolvedValue(mkStatus({ items: [bookB, bookA] }))
    await vi.advanceTimersByTimeAsync(5_000)
    await dialog.vm.$emit('confirm')
    await settle()
    expect(mockedApi.deleteBook).toHaveBeenCalledWith(0)
  })

  it('目标书已被移除 → 不调 deleteBook，仅记警告日志', async () => {
    useLogStore().clear()
    mockedApi.analysisStatus.mockResolvedValue(mkStatus({ items: [bookA] }))
    const wrapper = await mountPage()
    const delBtns = wrapper.findAll('tbody button').filter((b) => b.text().trim() === '删除')
    await delBtns[0].trigger('click')  // 点《A》
    await settle()
    // 下一轮轮询时 A 已不在队列
    mockedApi.analysisStatus.mockResolvedValue(mkStatus({ items: [bookB] }))
    await vi.advanceTimersByTimeAsync(5_000)
    await wrapper.findComponent(ConfirmDialog).vm.$emit('confirm')
    await settle()
    expect(mockedApi.deleteBook).not.toHaveBeenCalled()
    // warn 文案同样双重书名号，断言用与书名无关的尾部子串
    expect(useLogStore().logs.value.some((l) => l.text.includes('可能已被移除'))).toBe(true)
  })
})

describe('QueuePage 进度语义', () => {
  it('M-3：开始分析先清零上一轮进度残留', async () => {
    const wrapper = await mountPage()
    feed({ type: 'progress', payload: { current: 5, total: 10, eta: '1m' } })
    await nextTick()
    expect(dashProps(wrapper).progress).toMatchObject({ current: 5, total: 10 })
    const startBtn = wrapper.findAll('button').find((b) => b.text().includes('开始分析'))
    await startBtn!.trigger('click')
    await settle()
    expect(mockedApi.startAnalysis).toHaveBeenCalledTimes(1)
    expect(dashProps(wrapper).progress).toMatchObject({ current: 0, total: 0 })
  })

  it('运行中刷新页面：REST 状态兜底回填进度条（防凭空消失）', async () => {
    mockedApi.analysisStatus.mockResolvedValue(mkStatus({
      running: true,
      queue: { total_items: 1, completed_items: 0, current_item: '《A》', current_progress: 10, current_total: 100 },
    }))
    const wrapper = await mountPage()  // 挂载即 refresh → 回填
    expect(dashProps(wrapper).progress).toMatchObject({ current: 10, total: 100 })
  })
})
