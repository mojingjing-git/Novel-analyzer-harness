/**
 * 核心交互组件与状态存储综合测试：
 * 覆盖 DiscoveryFeed、LaneView、RunDashboard、BookSelector、ChapterDetailPanel、useLogStore。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { defineComponent, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'

vi.mock('../api/client', () => ({
  api: {
    listBooks: vi.fn(),
    getLatestChapter: vi.fn(),
    getChapterResult: vi.fn(),
    getModels: vi.fn().mockResolvedValue({ models: ['gpt-4o', 'MiniMax-M3.1'] }),
  },
}))

import { api, type BookInfo, type ConfigProfilesResponse } from '../api/client'
import DiscoveryFeed from '../components/DiscoveryFeed.vue'
import LaneView from '../components/LaneView.vue'
import RunDashboard, { type DiscoveryItem } from '../components/RunDashboard.vue'
import CountUp from '../components/CountUp.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import BookSelector from '../components/BookSelector.vue'
import ChapterDetailPanel from '../components/ChapterDetailPanel.vue'
import SettingsProfilesCard from '../components/settings/SettingsProfilesCard.vue'
import RerunChaptersDialog from '../components/RerunChaptersDialog.vue'
import { resetSelectedBookForTest } from '../composables/useSelectedBook'

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
    expect(rows[0].find('.lane-state').text()).toBe('等待首字')
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
    expect(thinkingWrapper.find('.lane-state').text()).toBe('思考')
    await vi.advanceTimersByTimeAsync(1000)
    expect(thinkingWrapper.find('.lane-tok').text()).toContain('思考 500 tok')

    await thinkingWrapper.setProps({
      tokenByBlock: new Map([[1, { outputTokens: 800, lastTokenAt: now - 100, isThinking: false }]]),
    })
    expect(thinkingWrapper.find('.lane-row').classes()).not.toContain('lane-thinking')
    expect(thinkingWrapper.find('.lane-state').text()).toBe('输出')
    // 线性 950ms 动画中途应处于 500..800 之间
    await vi.advanceTimersByTimeAsync(475)
    const midMatch = thinkingWrapper.find('.lane-tok').text().match(/(\d+)\s+tok/)
    expect(midMatch).not.toBeNull()
    const midVal = Number(midMatch![1])
    expect(midVal).toBeGreaterThan(500)
    expect(midVal).toBeLessThan(800)

    await vi.advanceTimersByTimeAsync(600)
    expect(thinkingWrapper.find('.lane-tok').text()).toBe('800 tok')

    // 同一物理槽位切换为新 blockId 时通过 :key 重新挂载，不应从 800 倒数
    await thinkingWrapper.setProps({
      activeBlocks: new Map([[2, { range: '第5-8章', startedAt: now }]]),
      tokenByBlock: new Map([[2, { outputTokens: 120, lastTokenAt: now, isThinking: false }]]),
    })
    const switchedInitial = Number(thinkingWrapper.find('.lane-tok').text().match(/(\d+)\s+tok/)?.[1] ?? -1)
    expect(switchedInitial).toBeLessThanOrEqual(120)
    await vi.advanceTimersByTimeAsync(1000)
    expect(thinkingWrapper.find('.lane-tok').text()).toBe('120 tok')
    thinkingWrapper.unmount()

    // 空闲时收起空槽，显示单行提示
    const idleWrapper = mount(LaneView, {
      props: {
        concurrency: 5,
        activeBlocks: new Map(),
        finishedBlocks: new Map(),
      },
    })
    expect(idleWrapper.find('.lane-idle').exists()).toBe(true)
    expect(idleWrapper.findAll('.lane-row')).toHaveLength(0)
  })
})

describe('RunDashboard', () => {
  it('渲染 4 张聚合卡、CountUp 数值取整、平滑吞吐图与 select-block 透传', async () => {
    const active = new Map([[3, { range: '第9-12章', startedAt: 100 }]])
    const wrapper = mount(RunDashboard, {
      props: {
        running: true,
        concurrency: 2,
        activeBlocks: active,
        progress: { current: 12.999, total: 100, eta: '' },
        sessionTokens: { input: 12345, output: 6789 },
        rateHistory: [10, 44],
        aggregateRate: 44,
      },
    })
    await nextTick()
    const cards = wrapper.findAll('.rd-card')
    expect(cards).toHaveLength(4)
    expect(cards[0].text()).toContain('13')
    expect(cards[0].text()).toContain('/ 100')
    expect(cards[0].text()).not.toContain('12.999')
    expect(cards[1].text()).toContain('—')

    // 即使传入 blockSize: 8，已完成卡片也不再把章节数乘以 blockSize
    await wrapper.setProps({ blockSize: 8 })
    await nextTick()
    expect(cards[0].text()).not.toContain('800')

    const countUps = wrapper.findAllComponents(CountUp)
    expect(countUps[1].props('value')).toBe(12345)
    expect(countUps[2].props('value')).toBe(6789)
    expect(wrapper.find('.rd-rate-value').text()).toContain('44')

    const svg = wrapper.find('.rd-sparkline-svg')
    expect(svg.exists()).toBe(true)
    expect(svg.attributes('viewBox')).toBe('0 0 400 56')
    expect(svg.findAll('path')).toHaveLength(2)
    expect(wrapper.text()).not.toContain('刻度上限')
    expect(wrapper.findAll('.rd-log-label').map((n) => n.text())).toEqual(['100', '10', '0'])

    await wrapper.find('.lane-row').trigger('click')
    expect(wrapper.emitted('select-block')).toEqual([[3]])

    await wrapper.setProps({ running: false, activeBlocks: new Map(), aggregateRate: 0 })
    expect(wrapper.find('.rd-rate-value').text()).toBe('0 tok/s')
    expect(wrapper.find('.rd-sparkline-svg').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('ConfirmDialog', () => {
  it('转移焦点、限制 Tab 循环并在 Escape 取消后归还焦点', async () => {
    const wrapper = mount(defineComponent({
      components: { ConfirmDialog },
      setup: () => ({ open: ref(false) }),
      template: `
        <button type="button" @click="open = true">打开</button>
        <ConfirmDialog v-if="open" title="删除小说" message="移入回收站" @cancel="open = false" />
      `,
    }), { attachTo: document.body })
    try {
      const trigger = wrapper.get('button')
      trigger.element.focus()
      await trigger.trigger('click')
      const dialog = wrapper.get('[role="dialog"]')
      const [cancel, confirm] = dialog.findAll('button')
      expect(document.activeElement).toBe(cancel.element)
      expect(dialog.attributes('aria-labelledby')).toBe(dialog.get('h3').attributes('id'))
      await cancel.trigger('keydown', { key: 'Tab', shiftKey: true })
      expect(document.activeElement).toBe(confirm.element)
      await confirm.trigger('keydown', { key: 'Tab' })
      expect(document.activeElement).toBe(cancel.element)
      await cancel.trigger('keydown', { key: 'Escape' })
      await nextTick()
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
      expect(document.activeElement).toBe(trigger.element)

      await trigger.trigger('click')
      const backdrop = wrapper.get('.confirm-mask')
      const press = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
      backdrop.element.dispatchEvent(press)
      expect(press.defaultPrevented).toBe(true)
      await backdrop.trigger('click')
      await nextTick()
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
      expect(document.activeElement).toBe(trigger.element)
    } finally {
      wrapper.unmount()
    }
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
    window.localStorage.clear()
    resetSelectedBookForTest()
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
    wrapper.unmount()
  })

  it('BookSelector：跨实例全局持久化、不存在书目清理、手动刷新不回退 ?book= 且 syncGlobal=false 隔离', async () => {
    // 1. 首个实例选中 B，跨实例挂载应自动恢复 B
    const w1 = mount(BookSelector, { props: { modelValue: '' } })
    await settle()
    await w1.find('select').setValue('B')
    w1.unmount()

    const w2 = mount(BookSelector, { props: { modelValue: '' } })
    await settle()
    expect(w2.emitted('update:modelValue')![0]).toEqual(['B'])
    w2.unmount()

    // 2. 书目不存在时自动清理脏 ID，不向父级发射失效书名
    mockedApi.listBooks.mockResolvedValueOnce([books[0]])
    const w3 = mount(BookSelector, { props: { modelValue: '' } })
    await settle()
    expect(w3.emitted('update:modelValue')).toBeUndefined()
    expect(window.localStorage.getItem('novel-analyzer-selected-book')).toBeNull()
    w3.unmount()

    // 3. 手动点刷新不再重吃 ?book= 覆盖当前空白；syncGlobal=false 不污染全局
    window.history.replaceState({}, '', '/?book=B')
    const isolated = mount(BookSelector, { props: { modelValue: '', syncGlobal: false } })
    await settle()
    expect(isolated.emitted('update:modelValue')![0]).toEqual(['B'])
    expect(window.localStorage.getItem('novel-analyzer-selected-book')).toBeNull()
    await isolated.find('select').setValue('A')
    expect(window.localStorage.getItem('novel-analyzer-selected-book')).toBeNull()
    await isolated.setProps({ modelValue: '' })
    await isolated.findAll('button')[0].trigger('click')
    await settle()
    expect(isolated.emitted('update:modelValue')!.filter((e) => e[0] === 'B')).toHaveLength(1)
    isolated.unmount()
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

  it('ChapterDetailPanel & ChapterValue：隐藏内部元数据字段并映射嵌套中文标签', async () => {
    mockedApi.getChapterResult.mockResolvedValue({
      book_id: 'A',
      chapter: 5,
      data: {
        chapter_number: 5,
        block_size: 4,
        source_chapters: [5, 6, 7],
        anchor_stats: { total: 1, located: 1, missing: 0 },
        core_events: [{ id: 1, event: '突破', characters: '张三', function: '升级', importance: '高' }],
        character_arcs: [{ name: '张三', surface_action: '出关', inner_motivation: '复仇', change_delta: '提升', driver: '信念' }],
        foreshadowing: [{ clue: '玉佩', type: '伏笔', implication: '身世', confidence: '高', importance: '中', question: '何人所留', answer_criteria: '说明来历', lead: false }],
      },
    })

    const wrapper = mount(ChapterDetailPanel, { props: { bookId: '', chapter: 5 } })
    await wrapper.setProps({ bookId: 'A' })
    await settle()
    const text = wrapper.text()
    expect(text).not.toContain('anchor_stats')
    expect(text).not.toContain('source_chapters')
    expect(text).toContain('共 4 个文本块')
    expect(text).toContain('重要度')
    expect(text).toContain('变化幅度')
    expect(text).toContain('驱动因素')
    expect(text).toContain('待解问题')
    expect(text).toContain('回收判据')
    expect(text).toContain('纯线索')

    // block_size=1（新单章产物）不展示冗余的“文本块”徽标
    mockedApi.getChapterResult.mockResolvedValueOnce({
      book_id: 'A',
      chapter: 6,
      data: { chapter_number: 6, block_size: 1, source_chapters: [6], core_events: [] },
    })
    await wrapper.setProps({ chapter: 6 })
    await settle()
    expect(wrapper.text()).not.toContain('文本块')
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

describe('SettingsProfilesCard', () => {
  function mkSampleProfiles(): ConfigProfilesResponse {
    const baseSummary = {
      model: '', summary_model: '', recheck_model: '',
      block_size: 2, concurrency: 8, summary_concurrency: 6, recheck_concurrency: 6,
      summary_batch_size: 45, recheck_version: 'v3', recheck_v3_mode: 'wide',
      recheck_v3_pool_max_chars: 180000, recheck_v3_batch_size: 20, recheck_v3_max_hops: 2,
      volume_compress_threshold: 180000,
    }
    return {
      active_slot_id: 'builtin_256k',
      last_applied_slot_id: 'builtin_256k',
      is_modified_from_last: false,
      store_corrupted: false,
      slots: [
        { id: 'builtin_128k', kind: 'builtin', slot_index: 1, name: '128K 兼容保底', description: '128K', badge: '128K', recommended: false, populated: true, is_active: false, matches_current: false, include_credentials: false, has_api_key: false, has_recheck_api_key: false, updated_at: null, summary: { ...baseSummary, concurrency: 6, recheck_v3_pool_max_chars: 75000, recheck_v3_batch_size: 10 }, managed_field_count: 37 },
        { id: 'builtin_256k', kind: 'builtin', slot_index: 2, name: '256K 甜点推荐', description: '256K', badge: '256K · 推荐', recommended: true, populated: true, is_active: true, matches_current: true, include_credentials: false, has_api_key: false, has_recheck_api_key: false, updated_at: null, summary: baseSummary, managed_field_count: 37 },
        { id: 'builtin_512k', kind: 'builtin', slot_index: 3, name: '512K 超长巨著', description: '512K', badge: '512K', recommended: false, populated: true, is_active: false, matches_current: false, include_credentials: false, has_api_key: false, has_recheck_api_key: false, updated_at: null, summary: { ...baseSummary, concurrency: 10, recheck_v3_pool_max_chars: 360000, recheck_v3_batch_size: 32 }, managed_field_count: 37 },
        { id: 'user_1', kind: 'user', slot_index: 4, name: '主力方案', description: 'M3.1', badge: '自定义 1', recommended: false, populated: true, is_active: false, matches_current: false, include_credentials: true, has_api_key: true, has_recheck_api_key: false, updated_at: '2026-11-25T05:00:00+08:00', summary: { ...baseSummary, base_url: 'https://api.test/v1', model: 'MiniMax-M3.1' }, managed_field_count: 120 },
        { id: 'user_2', kind: 'user', slot_index: 5, name: '自定义槽 2', description: '', badge: '空槽 2', recommended: false, populated: false, is_active: false, matches_current: false, include_credentials: false, has_api_key: false, has_recheck_api_key: false, updated_at: null, summary: null, managed_field_count: 0 },
        { id: 'user_3', kind: 'user', slot_index: 6, name: '自定义槽 3', description: '', badge: '空槽 3', recommended: false, populated: false, is_active: false, matches_current: false, include_credentials: false, has_api_key: false, has_recheck_api_key: false, updated_at: null, summary: null, managed_field_count: 0 },
      ],
    }
  }

  it('渲染 6 槽（3 内置 + 3 用户）、无 role="status"/select/保存按钮，并支持存入弹窗与 Escape 关闭', async () => {
    const wrapper = mount(SettingsProfilesCard, {
      attachTo: document.body,
      props: {
        profiles: mkSampleProfiles(),
        busy: false,
        hasUnsavedChanges: false,
        notice: '',
        warnings: [],
        error: '',
        saveModalError: '',
      },
    })

    expect(wrapper.findAll('[data-profile-slot]')).toHaveLength(6)
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
    expect(wrapper.find('select').exists()).toBe(false)
    expect(wrapper.findAll('button').some((b) => b.text().includes('保存'))).toBe(false)
    expect(wrapper.get('[data-profile-apply="builtin_256k"]').text()).toBe('已生效')
    expect((wrapper.get('[data-profile-apply="builtin_256k"]').element as HTMLButtonElement).disabled).toBe(true)

    // 打开空槽 user_2 存入弹窗
    const saveBtn = wrapper.get<HTMLButtonElement>('[data-profile-save="user_2"]')
    saveBtn.element.focus()
    await saveBtn.trigger('click')
    await nextTick()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)

    // Escape 关闭并归还焦点
    await wrapper.get('[role="dialog"]').trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(saveBtn.element)

    // 重新打开并提交表单 -> emit('save')
    await saveBtn.trigger('click')
    await nextTick()
    await wrapper.get<HTMLInputElement>('#profile-slot-name').setValue('新方案')
    await wrapper.get<HTMLInputElement>('#profile-slot-desc').setValue('用于长篇测试')
    await wrapper.get('form[role="dialog"]').trigger('submit')
    expect(wrapper.emitted('save')?.[0]).toEqual([{
      slotId: 'user_2',
      name: '新方案',
      description: '用于长篇测试',
      include_credentials: false,
    }])

    // 后端返回校验错误时保持弹窗可见并展示 saveModalError
    await wrapper.setProps({ saveModalError: '校验未通过' })
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
    expect(wrapper.get('[role="dialog"]').text()).toContain('校验未通过')

    wrapper.unmount()
  })
})

describe('RerunChaptersDialog & ChapterDetailPanel rerun actions', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('校验章号范围并打包临时覆盖参数，且详情头部支持触发重跑', async () => {
    const dlg = mount(RerunChaptersDialog, {
      props: {
        bookId: '测书',
        totalChapters: 50,
        hasReport: true,
        initialChapters: '37-40, 5',
        currentChapter: 5,
      },
    })
    await nextTick()
    expect(dlg.text()).toContain('将重跑 5 章：5, 37, 38, 39, 40')
    expect(dlg.text()).toContain('不会自动重算')

    await dlg.find('.adv-toggle').trigger('click')
    const selects = dlg.findAll('select')
    await selects[0].setValue('high')
    const submitBtn = dlg.findAll('button').find((b) => b.text().includes('开始重跑'))!
    await submitBtn.trigger('click')

    const emitted = dlg.emitted('confirm')?.[0]?.[0] as Record<string, unknown>
    expect(emitted.book_id).toBe('测书')
    expect(emitted.chapters).toBe('37-40, 5')
    expect((emitted.overrides as Record<string, unknown>).reasoning_effort).toBe('high')

    mockedApi.getLatestChapter.mockResolvedValue({ book_id: 'A', latest: 6 })
    mockedApi.getChapterResult.mockResolvedValue({
      book_id: 'A',
      chapter: 6,
      data: { chapter_number: 6, block_size: 1, source_chapters: [6], core_events: [] },
    })
    const panel = mount(ChapterDetailPanel, { props: { bookId: 'A', chapter: 6 } })
    await settle()
    const rerunOne = panel.findAll('button').find((b) => b.text().includes('重跑本章'))
    expect(rerunOne).toBeTruthy()
    await rerunOne!.trigger('click')
    expect(panel.emitted('rerun-chapter')?.[0]).toEqual([6])
  })
})
