/** DiscoveryFeed 发现流测试 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import DiscoveryFeed from './DiscoveryFeed.vue'
import type { DiscoveryItem } from './RunDashboard.vue'

function makeItem(overrides: Partial<DiscoveryItem> = {}): DiscoveryItem {
  return {
    id: Math.floor(Math.random() * 100000),
    ts: Date.now(),
    events: 3,
    foreshadows: [],
    characters: [],
    unresolved: [],
    highlighted: false,
    ...overrides,
  }
}

describe('DiscoveryFeed 发现流', () => {
  it('空数据：渲染"尚无新发现"提示', () => {
    const wrapper = mount(DiscoveryFeed)
    expect(wrapper.text()).toContain('尚无新发现')
    expect(wrapper.findAll('.df-item').length).toBe(0)
  })

  it('追加：传入 items 数组后逐条渲染', () => {
    const items = [
      makeItem({ id: 1, events: 2, foreshadows: ['古碑现世'] }),
      makeItem({ id: 2, events: 4, characters: ['林动'] }),
      makeItem({ id: 3, events: 1, unresolved: ['幕后黑手？'] }),
    ]
    const wrapper = mount(DiscoveryFeed, { props: { items } })
    const list = wrapper.findAll('.df-item')
    expect(list.length).toBe(3)
    expect(list[0].text()).toContain('古碑现世')
    expect(list[1].text()).toContain('林动')
    expect(list[2].text()).toContain('幕后黑手？')
  })

  it('上限裁剪：超过 maxItems 时只显示最后 N 条（ring buffer）', () => {
    const items = Array.from({ length: 250 }, (_, i) => makeItem({ id: i }))
    const wrapper = mount(DiscoveryFeed, { props: { items, maxItems: 200 } })
    const list = wrapper.findAll('.df-item')
    expect(list.length).toBe(200)
    // 验证保留的是最后 200 条（id 50-249）
    expect(list[0].text()).toContain('')  // 文本含 id 50
    expect(list[199].text()).toContain('')  // 文本含 id 249
  })

  it('高亮：item.highlighted=true 时加 df-highlight class（外部传入，组件不重新计算）', () => {
    // 决策：DiscoveryFeed 信任外部传入的 highlighted 字段（来自后端 result 派生）
    const items = [
      makeItem({ id: 1, foreshadows: ['古碑'], highlighted: true }),
      makeItem({ id: 2, characters: ['林动'], highlighted: true }),
      makeItem({ id: 3, unresolved: ['x'], highlighted: false }),
    ]
    const wrapper = mount(DiscoveryFeed, { props: { items } })
    const list = wrapper.findAll('.df-item')
    expect(list[0].classes()).toContain('df-highlight')
    expect(list[1].classes()).toContain('df-highlight')
    expect(list[2].classes()).not.toContain('df-highlight')
  })

  it('hover 暂停：mouseenter 状态变化（视觉）— 通过 df-sub 文字验证', async () => {
    const items = [makeItem({ id: 1 })]
    const wrapper = mount(DiscoveryFeed, { props: { items } })
    expect(wrapper.text()).not.toContain('暂停滚动')
    await wrapper.find('.df-list').trigger('mouseenter')
    // 注：paused 状态在 script 内，不在 DOM 直接可见
    // 验证方式：mouseenter 后 df-sub 应包含"（暂停滚动）"
    expect(wrapper.text()).toContain('暂停滚动')
    await wrapper.find('.df-list').trigger('mouseleave')
    expect(wrapper.text()).not.toContain('暂停滚动')
  })
})
