/** LaneView 车道组件测试 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import LaneView from './LaneView.vue'

describe('LaneView 车道组件', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('占位：activeBlocks 有 2 个块时，渲染 concurrency=4 个 lane，前 2 个有内容后 2 个空', () => {
    const active = new Map([
      [1, { range: '第1-4章', startedAt: Date.now() }],
      [2, { range: '第5-8章', startedAt: Date.now() }],
    ])
    const wrapper = mount(LaneView, { props: { concurrency: 4, activeBlocks: active } })
    const rows = wrapper.findAll('.lane-row')
    expect(rows.length).toBe(4)
    expect(rows[0].classes()).toContain('lane-running')
    expect(rows[1].classes()).toContain('lane-running')
    expect(rows[2].classes()).toContain('lane-empty')
    expect(rows[3].classes()).toContain('lane-empty')
    // 范围文本
    expect(rows[0].text()).toContain('第1-4章')
    expect(rows[1].text()).toContain('第5-8章')
    expect(rows[2].text()).toContain('空槽')
  })

  it('释放：active 移出后移到 finishedBlocks（绿/红态，按 blockId 降序，最新完成在前）', () => {
    const active = new Map<number, { range: string; startedAt: number }>()
    const finished = new Map([
      [1, { ok: true, range: '第1-4章' }],
      [2, { ok: false, range: '第5-8章' }],
    ])
    const wrapper = mount(LaneView, { props: { concurrency: 2, activeBlocks: active, finishedBlocks: finished } })
    const rows = wrapper.findAll('.lane-row')
    // 实现按 blockId 降序：row0=block 2 (failed)，row1=block 1 (done)
    expect(rows[0].classes()).toContain('lane-failed')
    expect(rows[0].text()).toContain('✗')
    expect(rows[0].text()).toContain('第5-8章')
    expect(rows[1].classes()).toContain('lane-done')
    expect(rows[1].text()).toContain('✓')
    expect(rows[1].text()).toContain('第1-4章')
  })

  it('超上限：active 超过 concurrency 时，只渲染前 concurrency 个活跃块（不闪烁设计）', () => {
    const active = new Map([
      [1, { range: 'A', startedAt: 100 }],
      [2, { range: 'B', startedAt: 200 }],
      [3, { range: 'C', startedAt: 300 }],
    ])
    const wrapper = mount(LaneView, { props: { concurrency: 2, activeBlocks: active } })
    const rows = wrapper.findAll('.lane-row')
    expect(rows.length).toBe(2)  // 不扩容，并发槽位固定
    expect(rows[0].text()).toContain('A')
    expect(rows[1].text()).toContain('B')
  })

  it('重试同 blockId：active 重新占位前先清掉旧 finished（key 一致）', () => {
    // 同一 block_id 失败后重试：finished 仍存在 + 新的 active 同 id
    // LaneView 只显示 active（前 N 个），finished 在 N 之后占位
    const active = new Map([
      [5, { range: '第5-8章（重试）', startedAt: Date.now() }],
    ])
    const finished = new Map([
      [5, { ok: false, range: '第5-8章（首跑失败）' }],
    ])
    const wrapper = mount(LaneView, { props: { concurrency: 4, activeBlocks: active, finishedBlocks: finished } })
    const rows = wrapper.findAll('.lane-row')
    // active 排前（按 startedAt 升序），finished 排后补齐
    expect(rows[0].text()).toContain('重试')
    expect(rows[1].text()).toContain('首跑失败')
  })

  it('空数据：0 个 active + 0 个 finished 时，全部 lane 显示空槽', () => {
    const wrapper = mount(LaneView, { props: { concurrency: 3, activeBlocks: new Map(), finishedBlocks: new Map() } })
    const rows = wrapper.findAll('.lane-row')
    expect(rows.length).toBe(3)
    rows.forEach((r) => {
      expect(r.classes()).toContain('lane-empty')
      expect(r.text()).toContain('空槽')
    })
  })

  it('running 块进度条按 stallWarnSec 归一化（0-100%）', () => {
    vi.useFakeTimers()
    const now = Date.now()
    vi.setSystemTime(now)
    const active = new Map([
      [1, { range: '第1-4章', startedAt: now - 240 * 1000 }],  // 已跑 4 分钟
    ])
    const wrapper = mount(LaneView, {
      props: { concurrency: 1, activeBlocks: active, stallWarnSec: 480 },
    })
    const bar = wrapper.find('.lane-progress-bar')
    expect(bar.exists()).toBe(true)
    // 240/480 = 50%
    const width = bar.attributes('style') || ''
    expect(width).toContain('50%')
    vi.useRealTimers()
  })

  it('超 stallWarnSec 且无 token_delta → lane-stalled + ⚠ 标记', () => {
    vi.useFakeTimers()
    const now = Date.now()
    vi.setSystemTime(now)
    const active = new Map([
      [1, { range: '第1-4章', startedAt: now - 600 * 1000 }],  // 已跑 10 分钟
    ])
    // tokenByBlock 中 lastTokenAt 是 8 分钟前（>5 秒阈值）→ 卡死
    const tokenByBlock = new Map([
      [1, { outputTokens: 100, lastTokenAt: now - 480 * 1000 }],  // 8 分钟前最后更新
    ])
    const wrapper = mount(LaneView, {
      props: { concurrency: 1, activeBlocks: active, tokenByBlock, stallWarnSec: 480 },
    })
    const row = wrapper.find('.lane-row')
    expect(row.classes()).toContain('lane-stalled')
    // ⚠ 图标
    expect(wrapper.text()).toContain('⚠')
    vi.useRealTimers()
  })

  it('槽位锚定：块 1 完成后槽位就地保留完成态，块 2 稳定停留在 #2 槽位不发生窜动', async () => {
    const now = Date.now()
    const active = new Map([
      [1, { range: '第1-4章', startedAt: now - 2000 }],
      [2, { range: '第5-8章', startedAt: now - 1000 }],
    ])
    const finished = new Map<number, { ok: boolean; range: string }>()
    const wrapper = mount(LaneView, {
      props: { concurrency: 4, activeBlocks: active, finishedBlocks: finished },
    })

    let rows = wrapper.findAll('.lane-row')
    expect(rows[0].text()).toContain('第1-4章')
    expect(rows[1].text()).toContain('第5-8章')

    // 模拟块 1 完成：移出 active，加入 finished
    const nextActive = new Map([
      [2, { range: '第5-8章', startedAt: now - 1000 }],
    ])
    const nextFinished = new Map([
      [1, { ok: true, range: '第1-4章' }],
    ])
    await wrapper.setProps({ activeBlocks: nextActive, finishedBlocks: nextFinished })

    rows = wrapper.findAll('.lane-row')
    // 槽位 0 就地展示块 1 完成
    expect(rows[0].classes()).toContain('lane-done')
    expect(rows[0].text()).toContain('✓')
    expect(rows[0].text()).toContain('第1-4章')
    // 槽位 1 依旧是块 2（没有窜动到槽位 0）
    expect(rows[1].classes()).toContain('lane-running')
    expect(rows[1].text()).toContain('第5-8章')
  })

  it('动态吐字：近 1500ms 内有 token 更新触发 lane-streaming 并按 token 估算进度', () => {
    vi.useFakeTimers()
    const now = Date.now()
    vi.setSystemTime(now)
    const active = new Map([
      [1, { range: '第1-4章', startedAt: now - 10000 }],
    ])
    const tokenByBlock = new Map([
      [1, { outputTokens: 800, lastTokenAt: now - 500 }],
    ])
    const wrapper = mount(LaneView, {
      props: { concurrency: 1, activeBlocks: active, tokenByBlock },
    })
    const row = wrapper.find('.lane-row')
    expect(row.classes()).toContain('lane-streaming')
    expect(wrapper.find('.lane-streaming-dot').exists()).toBe(true)

    const bar = wrapper.find('.lane-progress-bar')
    // 800/1600 = 50% -> 0.05 + 0.5 * 0.93 = 0.515 (51.5%)
    const width = bar.attributes('style') || ''
    expect(width).toContain('51.5%')
    vi.useRealTimers()
  })

  it('点击穿透：点击有效块触发 select-block 事件，空槽不触发', async () => {
    const active = new Map([
      [1, { range: '第1-4章', startedAt: 100 }],
    ])
    const wrapper = mount(LaneView, {
      props: { concurrency: 2, activeBlocks: active },
    })
    const rows = wrapper.findAll('.lane-row')
    // 槽位 0 有效块
    await rows[0].trigger('click')
    expect(wrapper.emitted('select-block')).toBeTruthy()
    expect(wrapper.emitted('select-block')![0]).toEqual([1])

    // 槽位 1 空槽
    await rows[1].trigger('click')
    expect(wrapper.emitted('select-block')!.length).toBe(1)
  })

  it('思考状态感知：isThinking=true 时渲染 🧠 图标、lane-thinking 样式与思考字数标签', async () => {
    vi.useFakeTimers()
    const now = Date.now()
    vi.setSystemTime(now)
    const active = new Map([
      [1, { range: '第1-4章', startedAt: now - 5000 }],
    ])
    const tokenByBlock = new Map([
      [1, { outputTokens: 500, lastTokenAt: now - 300, isThinking: true }],
    ])
    const wrapper = mount(LaneView, {
      props: { concurrency: 1, activeBlocks: active, tokenByBlock },
    })

    const row = wrapper.find('.lane-row')
    expect(row.classes()).toContain('lane-thinking')
    expect(row.classes()).toContain('lane-streaming')

    // 状态列呈现 🧠 图标
    expect(wrapper.find('.lane-state').text()).toBe('🧠')

    // 吐字标签带 "思考" 前缀，并带有 is-thinking 类
    const tok = wrapper.find('.lane-tok')
    expect(tok.text()).toContain('思考 500tok')
    expect(tok.classes()).toContain('is-thinking')
    expect(wrapper.find('.lane-streaming-dot').classes()).toContain('is-thinking')

    // 进度条封顶估算：0.05 + (500/2000)*0.40 = 0.05 + 0.10 = 0.15 (15%)
    const bar = wrapper.find('.lane-progress-bar')
    const width = bar.attributes('style') || ''
    const pct = Number(width.match(/width:\s*([\d.]+)%/)?.[1])
    expect(pct).toBeCloseTo(15)

    // 思考结束切换为正常内容生成态
    const nextTokenByBlock = new Map([
      [1, { outputTokens: 800, lastTokenAt: now - 100, isThinking: false }],
    ])
    await wrapper.setProps({ tokenByBlock: nextTokenByBlock })
    expect(row.classes()).not.toContain('lane-thinking')
    expect(wrapper.find('.lane-state').text()).toBe('⏳')
    expect(wrapper.find('.lane-tok').text()).toBe('800tok')
    expect(wrapper.find('.lane-tok').classes()).not.toContain('is-thinking')

    vi.useRealTimers()
  })
})
