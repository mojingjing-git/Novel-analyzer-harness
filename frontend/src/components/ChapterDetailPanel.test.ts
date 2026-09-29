/**
 * ChapterDetailPanel 组件级测试：requestSeq 竞态守卫（P2：早退也失效在飞请求）、
 * 章号规范化、60s 空闲回退、latest 轮询门控、sections 元键过滤。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('../api/client', () => ({
  api: {
    getLatestChapter: vi.fn(),
    getChapterResult: vi.fn(),
  },
}))

import { api } from '../api/client'
import ChapterDetailPanel from './ChapterDetailPanel.vue'

const mockedApi = vi.mocked(api)

const settle = () => vi.advanceTimersByTimeAsync(0)

type ChapterResp = Awaited<ReturnType<typeof api.getChapterResult>>

function mkResult(bookId = 'A', chapter = 12): ChapterResp {
  return {
    book_id: bookId, chapter,
    data: {
      chapter_number: chapter, block_size: 4,
      core_events: [{ event: '主角出场' }],
      custom_field: '原样展示的未知键',
    },
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()  // mock 调用计数跨用例隔离
  mockedApi.getLatestChapter.mockResolvedValue({ book_id: 'A', latest: 12 })
  mockedApi.getChapterResult.mockResolvedValue(mkResult())
})

afterEach(() => {
  vi.useRealTimers()
})

describe('ChapterDetailPanel 自动模式与加载', () => {
  it('选书后拉最新章 → 自动加载第 12 章，标记「自动 · 最新章节」', async () => {
    const wrapper = mount(ChapterDetailPanel, { props: { bookId: '', chapter: 0 } })
    await wrapper.setProps({ bookId: 'A' })
    await settle()
    expect(wrapper.text()).toContain('第 12 章')
    expect(wrapper.text()).toContain('自动 · 最新章节')
    expect(wrapper.text()).toContain('核心事件')
  })

  it('sections 过滤元键（chapter_number/block_size），未知键原样展示', async () => {
    const wrapper = mount(ChapterDetailPanel, { props: { bookId: '', chapter: 0 } })
    await wrapper.setProps({ bookId: 'A' })
    await settle()
    const text = wrapper.text()
    expect(text).toContain('原样展示的未知键')
    expect(text).not.toContain('chapter_number')
    expect(text).not.toContain('block_size')
  })
})

describe('ChapterDetailPanel requestSeq 竞态守卫（P2）', () => {
  it('切书瞬间早退路径失效在飞请求：旧书迟到响应不得写回', async () => {
    let resolveA!: (v: ChapterResp) => void
    mockedApi.getChapterResult.mockImplementation((bid: string, ch: number) => {
      if (bid === 'A') return new Promise<ChapterResp>((resolve) => { resolveA = resolve })  // A 的加载挂起
      return Promise.resolve({
        book_id: bid, chapter: ch,
        data: { core_events: [{ event: `${bid}的内容` }] },
      })
    })
    const wrapper = mount(ChapterDetailPanel, { props: { bookId: '', chapter: 0 } })
    await wrapper.setProps({ bookId: 'A' })
    await settle()
    expect(wrapper.text()).toContain('加载中...')
    // 切书瞬间：latest 尚为 0 → loadChapter 走早退路径（seq++ 失效 A 的在飞请求）
    await wrapper.setProps({ bookId: 'B' })
    await settle()
    resolveA({ book_id: 'A', chapter: 12, data: { core_events: [{ event: 'A书的迟到响应' }] } })  // 迟到响应
    await settle()
    expect(wrapper.text()).toContain('B的内容')
    expect(wrapper.text()).not.toContain('A书的迟到响应')
  })

  it('切书重置：旧书 result 清空后加载新书', async () => {
    mockedApi.getChapterResult.mockImplementation((bid: string, ch: number) =>
      Promise.resolve(bid === 'A'
        ? { book_id: bid, chapter: ch, data: { core_events: [{ event: 'A书内容' }] } }
        : { book_id: bid, chapter: ch, data: { core_events: [{ event: 'B书内容' }] } }))
    const wrapper = mount(ChapterDetailPanel, { props: { bookId: '', chapter: 0 } })
    await wrapper.setProps({ bookId: 'A' })
    await settle()
    expect(wrapper.text()).toContain('A书内容')
    await wrapper.setProps({ bookId: 'B' })
    await settle()
    expect(wrapper.text()).toContain('B书内容')
    expect(wrapper.text()).not.toContain('A书内容')
    expect(mockedApi.getLatestChapter).toHaveBeenCalledWith('B')
  })
})

describe('ChapterDetailPanel 章号规范化与空闲回退', () => {
  it('负数/小数/NaN → 钳位为 ≥0 整数并反向 emit 修正', async () => {
    const wrapper = mount(ChapterDetailPanel, { props: { bookId: '', chapter: 0 } })
    await wrapper.setProps({ chapter: -3 })
    expect(wrapper.emitted('update:chapter')![0]).toEqual([0])
    await wrapper.setProps({ chapter: 5.7 })
    expect(wrapper.emitted('update:chapter')!.at(-1)).toEqual([5])
    await wrapper.setProps({ chapter: Number.NaN })
    expect(wrapper.emitted('update:chapter')!.at(-1)).toEqual([0])
  })

  it('手动指定 60s 无活动 → 自动回到最新章节（emit 0）', async () => {
    const wrapper = mount(ChapterDetailPanel, { props: { bookId: '', chapter: 0 } })
    await wrapper.setProps({ bookId: 'A', chapter: 3 })
    await settle()
    const baseline = (wrapper.emitted('update:chapter') ?? []).length
    await vi.advanceTimersByTimeAsync(60_000)
    const all = wrapper.emitted('update:chapter') ?? []
    expect(all.length).toBe(baseline + 1)
    expect(all.at(-1)).toEqual([0])
  })

  it('窗口活动重置空闲计时：60s 内有 click 则不回退', async () => {
    const wrapper = mount(ChapterDetailPanel, { props: { bookId: '', chapter: 0 } })
    await wrapper.setProps({ bookId: 'A', chapter: 3 })
    await settle()
    const baseline = (wrapper.emitted('update:chapter') ?? []).length
    await vi.advanceTimersByTimeAsync(45_000)
    window.dispatchEvent(new Event('click'))
    await vi.advanceTimersByTimeAsync(45_000)  // 距上次活动仅 45s
    expect((wrapper.emitted('update:chapter') ?? []).length).toBe(baseline)
  })

  it('手动指定时 5s latest 轮询跳过；自动模式才刷新', async () => {
    const wrapper = mount(ChapterDetailPanel, { props: { bookId: '', chapter: 0 } })
    await wrapper.setProps({ bookId: 'A', chapter: 3 })  // 手动
    await settle()
    const callsManual = mockedApi.getLatestChapter.mock.calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(mockedApi.getLatestChapter.mock.calls.length).toBe(callsManual)  // 不刷新

    await wrapper.setProps({ chapter: 0 })  // 回到自动
    await settle()
    const base = mockedApi.getLatestChapter.mock.calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    expect(mockedApi.getLatestChapter.mock.calls.length).toBeGreaterThan(base)
  })
})
