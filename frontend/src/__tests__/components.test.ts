/**
 * 核心交互组件与状态存储综合测试：
 * 覆盖 DiscoveryFeed、LaneView、RunDashboard、BookSelector、ChapterDetailPanel、useLogStore。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'

vi.mock('../api/client', () => ({
  api: {
    listBooks: vi.fn(),
    getLatestChapter: vi.fn(),
    getChapterResult: vi.fn(),
  },
}))

import { api, type BookInfo } from '../api/client'
import DiscoveryFeed from '../components/DiscoveryFeed.vue'
import LaneView from '../components/LaneView.vue'
import RunDashboard, { type DiscoveryItem } from '../components/RunDashboard.vue'
import CountUp from '../components/CountUp.vue'
import BookSelector from '../components/BookSelector.vue'
import ChapterDetailPanel from '../components/ChapterDetailPanel.vue'

const mockedApi = vi.mocked(api)
const settle = () => vi.advanceTimersByTimeAsync(0)

function makeItem(overrides: Partial<DiscoveryItem> = {}): DiscoveryItem {
  return {
    id: 1,
    ts: Date.now(),
    events: 3,
    foreshadows: [],
    characters: [],
    unresolved: [],
    highlighted: false,
    ...overrides,
  }
}

describe('DiscoveryFeed', () => {
  it('空状态提示、列表裁剪、高亮样式与 hover 暂停滚动', async () => {
    const empty = mount(DiscoveryFeed)
    expect(empty.text()).toContain('尚无新发现')

    const items = [
      makeItem({ id: 1, foreshadows: ['古碑现世'], highlighted: true }),
      makeItem({ id: 2, characters: ['林动'], highlighted: false }),
    ]
    const wrapper = mount(DiscoveryFeed, { props: { items, maxItems: 1 } })
    const list = wrapper.findAll('.df-item')
    expect(list).toHaveLength(1)
    expect(list[0].text()).toContain('林动')

    const wrapper2 = mount(DiscoveryFeed, { props: { items } })
    expect(wrapper2.findAll('.df-item')[0].classes()).toContain('df-highlight')
    await wrapper2.find('.df-list').trigger('mouseenter')
    expect(wrapper2.text()).toContain('暂停滚动')
    await wrapper2.find('.df-list').trigger('mouseleave')
    expect(wrapper2.text()).not.toContain('暂停滚动')
  })
})

describe('LaneView', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('槽位锚定：块 1 完成后就地转完成态，块 2 停留在原槽位不窜动，点击有效块触发 select-block', async () => {
    const now = Date.now()
    const active = new Map([
      [1, { range: '第1-4章', startedAt: now - 2000 }],
      [2, { range: '第5-8章', startedAt: now - 1000 }],
    ])
    const wrapper = mount(LaneView, {
      props: { concurrency: 3, activeBlocks: active, finishedBlocks: new Map() },
    })
    let rows = wrapper.findAll('.lane-row')
    expect(rows).toHaveLength(3)
    expect(rows[0].classes()).toContain('lane-running')
    expect(rows[2].classes()).toContain('lane-empty')

    await wrapper.setProps({
      activeBlocks: new Map([[2, { range: '第5-8章', startedAt: now - 1000 }]]),
      finishedBlocks: new Map([[1, { ok: true, range: '第1-4章' }]]),
    })
    rows = wrapper.findAll('.lane-row')
    expect(rows[0].classes()).toContain('lane-done')
    expect(rows[0].text()).toContain('第1-4章')
    expect(rows[1].classes()).toContain('lane-running')
    expect(rows[1].text()).toContain('第5-8章')

    await rows[1].trigger('click')
    await rows[2].trigger('click') // 空槽不触发
    expect(wrapper.emitted('select-block')).toEqual([[2]])
  })

  it('卡死预警（lane-stalled）与思考/流式吐字状态（lane-thinking / lane-streaming）', async () => {
    const now = Date.now()
    vi.setSystemTime(now)
    const stalledWrapper = mount(LaneView, {
      props: {
        concurrency: 1,
        activeBlocks: new Map([[1, { range: '第1-4章', startedAt: now - 600_000 }]]),
        tokenByBlock: new Map([[1, { outputTokens: 100, lastTokenAt: now - 480_000 }]]),
        stallWarnSec: 480,
      },
    })
    expect(stalledWrapper.find('.lane-row').classes()).toContain('lane-stalled')

    const thinkingWrapper = mount(LaneView, {
      props: {
        concurrency: 1,
        activeBlocks: new Map([[1, { range: '第1-4章', startedAt: now - 5_000 }]]),
        tokenByBlock: new Map([[1, { outputTokens: 500, lastTokenAt: now - 300, isThinking: true }]]),
      },
    })
    expect(thinkingWrapper.find('.lane-row').classes()).toContain('lane-thinking')
    expect(thinkingWrapper.find('.lane-state').text()).toBe('🧠')
    expect(thinkingWrapper.find('.lane-tok').text()).toContain('思考 500tok')

    await thinkingWrapper.setProps({
      tokenByBlock: new Map([[1, { outputTokens: 800, lastTokenAt: now - 100, isThinking: false }]]),
    })
    expect(thinkingWrapper.find('.lane-row').classes()).not.toContain('lane-thinking')
    expect(thinkingWrapper.find('.lane-tok').text()).toBe('800tok')
  })
})

describe('RunDashboard', () => {
  it('渲染 4 张聚合卡、CountUp 数值取整、aggregateRate 优先级与 select-block 透传', async () => {
    const active = new Map([[3, { range: '第9-12章', startedAt: 100 }]])
    const wrapper = mount(RunDashboard, {
      props: {
        running: true,
        concurrency: 2,
        activeBlocks: active,
        progress: { current: 12.999, total: 100, eta: '5m30s' },
        sessionTokens: { input: 12345, output: 6789 },
        rateHistory: [10, 11],
        aggregateRate: 44,
      },
    })
    await nextTick()
    const cards = wrapper.findAll('.rd-card')
    expect(cards).toHaveLength(4)
    expect(cards[0].text()).toContain('13')
    expect(cards[0].text()).not.toContain('12.999')
    expect(cards[1].text()).toContain('5m30s')

    const countUps = wrapper.findAllComponents(CountUp)
    expect(countUps[1].props('value')).toBe(12345)
    expect(countUps[2].props('value')).toBe(6789)
    expect(wrapper.find('.rd-rate-value').text()).toContain('44')

    await wrapper.find('.lane-row').trigger('click')
    expect(wrapper.emitted('select-block')).toEqual([[3]])
  })
})

describe('BookSelector & ChapterDetailPanel', () => {
  const books: BookInfo[] = [
    { id: 'A', name: '《甲》', total_chapters: 10, has_report: true, has_aggregated: false },
    { id: 'B', name: '《乙》', total_chapters: 20, has_report: false, has_aggregated: true },
  ] as BookInfo[]

  type ChapterResp = Awaited<ReturnType<typeof api.getChapterResult>>

  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    mockedApi.listBooks.mockResolvedValue(books)
    mockedApi.getLatestChapter.mockResolvedValue({ book_id: 'A', latest: 12 })
    mockedApi.getChapterResult.mockResolvedValue({
      book_id: 'A',
      chapter: 12,
      data: { chapter_number: 12, block_size: 4, core_events: [{ event: '主角出场' }] },
    })
    window.history.replaceState({}, '', '/')
  })

  afterEach(() => {
    window.history.replaceState({}, '', '/')
    vi.useRealTimers()
  })

  it('BookSelector：加载书目、支持 ?book= URL 预置、下拉切换与手动刷新', async () => {
    window.history.replaceState({}, '', '/?book=B')
    const wrapper = mount(BookSelector, { props: { modelValue: '' } })
    await settle()
    expect(wrapper.text()).toContain('《甲》 (10 章)')
    expect(wrapper.emitted('update:modelValue')![0]).toEqual(['B'])

    await wrapper.find('select').setValue('A')
    expect(wrapper.emitted('update:modelValue')![1]).toEqual(['A'])

    await wrapper.findAll('button')[0].trigger('click')
    await settle()
    expect(mockedApi.listBooks).toHaveBeenCalledTimes(2)
  })

  it('ChapterDetailPanel：自动加载最新章、requestSeq 切书竞态守卫、章号钳位与 60s 空闲回退', async () => {
    let resolveA!: (v: ChapterResp) => void
    mockedApi.getChapterResult.mockImplementation((bid: string, ch: number) => {
      if (bid === 'A') return new Promise<ChapterResp>((r) => { resolveA = r })
      return Promise.resolve({ book_id: bid, chapter: ch, data: { core_events: [{ event: `${bid}的内容` }] } })
    })

    const wrapper = mount(ChapterDetailPanel, { props: { bookId: '', chapter: 0 } })
    await wrapper.setProps({ bookId: 'A' })
    await settle()
    await wrapper.setProps({ bookId: 'B' })
    await settle()
    resolveA({ book_id: 'A', chapter: 12, data: { core_events: [{ event: 'A书迟到响应' }] } })
    await settle()
    expect(wrapper.text()).toContain('B的内容')
    expect(wrapper.text()).not.toContain('A书迟到响应')

    // 章号钳位与 60s 空闲回退自动模式
    await wrapper.setProps({ chapter: -3 })
    expect(wrapper.emitted('update:chapter')!.at(-1)).toEqual([0])

    await wrapper.setProps({ chapter: 3 })
    await settle()
    const baseCount = (wrapper.emitted('update:chapter') ?? []).length
    await vi.advanceTimersByTimeAsync(60_000)
    expect((wrapper.emitted('update:chapter') ?? []).length).toBe(baseCount + 1)
    expect(wrapper.emitted('update:chapter')!.at(-1)).toEqual([0])
  })
})

describe('useLogStore', () => {
  const KEY = 'novel-analyzer-logs'

  beforeEach(() => {
    window.localStorage.clear()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('坏条目过滤、2000 条上限裁剪、节流落盘与按分类过滤/删除', async () => {
    window.localStorage.setItem(KEY, JSON.stringify([
      { id: 1, text: '2026-08-07 10:00:00 [INFO] backend.core.pipeline: ok' },
      { id: 'bad', text: 'x' },
    ]))
    vi.resetModules()
    const { useLogStore } = await import('../composables/useLogStore')
    const store = useLogStore()
    expect(store.logs.value).toHaveLength(1)
    expect(store.logs.value[0].source).toBe('python')

    store.add('分析日志', 'info', 'analysis')
    store.add('总结日志', 'info', 'summary')
    await vi.advanceTimersByTimeAsync(500)
    expect(JSON.parse(window.localStorage.getItem(KEY)!)).toHaveLength(3)

    expect(store.filter('summary')).toHaveLength(1)
    store.removeByCategory('analysis')
    expect(store.logs.value.some((l) => l.category === 'analysis')).toBe(false)
    store.clear()
    expect(window.localStorage.getItem(KEY)).toBeNull()
  })
})
