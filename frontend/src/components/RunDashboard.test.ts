/** RunDashboard 容器测试 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import RunDashboard from './RunDashboard.vue'
import CountUp from './CountUp.vue'

describe('RunDashboard 仪表盘容器', () => {
  it('默认渲染：4 个聚合卡 + LaneView + DiscoveryFeed', () => {
    const wrapper = mount(RunDashboard, {
      props: { running: false, concurrency: 4 },
    })
    // 4 个聚合卡
    const cards = wrapper.findAll('.rd-card')
    expect(cards.length).toBe(4)
    expect(cards[0].text()).toContain('已完成')
    expect(cards[1].text()).toContain('ETA')
    expect(cards[2].text()).toContain('本会话输入')
    expect(cards[3].text()).toContain('本会话输出')
    // LaneView 子组件
    expect(wrapper.find('.lane-view').exists()).toBe(true)
    // DiscoveryFeed 子组件
    expect(wrapper.find('.discovery-feed').exists()).toBe(true)
  })

  it('数据透传：progress / sessionTokens 正确传到 CountUp 子组件', () => {
    const wrapper = mount(RunDashboard, {
      props: {
        running: true,
        progress: { current: 42, total: 100, eta: '5m30s' },
        sessionTokens: { input: 12345, output: 6789 },
      },
    })
    // 4 张聚合卡，但 ETA 用纯文本（不是 CountUp），所以只有 3 个 CountUp
    const countUps = wrapper.findAllComponents(CountUp)
    expect(countUps.length).toBe(3)
    expect(countUps[0].props('value')).toBe(42)        // 已完成
    expect(countUps[1].props('value')).toBe(12345)     // 输入 tokens
    expect(countUps[2].props('value')).toBe(6789)      // 输出 tokens
    // ETA 文本（纯文本 span）
    const cards = wrapper.findAll('.rd-card')
    expect(cards[1].text()).toContain('5m30s')
  })

  it('根节点套用 .glass-card Win11 风格栏位框', () => {
    // 保护：避免后续重构把栏位框误删，导致 RunDashboard 4 个区块失去容器感
    const wrapper = mount(RunDashboard, {
      props: { running: false, concurrency: 4 },
    })
    const root = wrapper.find('.run-dashboard')
    expect(root.exists()).toBe(true)
    expect(root.classes()).toContain('glass-card')
  })

  it('fmtNum 对小数四舍五入，去掉 CountUp 动画中间帧的浮点', async () => {
    // fmtNum('12.999759744828719') 必须 Math.round → "13"：
    // CountUp 动画 600ms 期间任意一帧的 display 都是浮点，被 String() 完整输出 18 位
    const wrapper = mount(RunDashboard, {
      props: {
        running: true,
        progress: { current: 12.999759744828719, total: 100, eta: '' },
      },
    })
    // 等待 CountUp 动画完成（默认 600ms）再断言
    await new Promise((r) => setTimeout(r, 700))
    const cards = wrapper.findAll('.rd-card')
    // 第一张卡 = 已完成
    expect(cards[0].text()).toContain('13')
    // 不应出现未取整的浮点输出
    expect(cards[0].text()).not.toContain('12.999')
  })

  it('aggregateRate 优先于 rateHistory 最后一项', () => {
    // 4 路并发时 rateHistory 末尾只是某个 block 的瞬时速率，aggregateRate
    // 才是 sum(活跃 block 速率) = 系统总吞吐。取 rateHistory[-1] 时，
    // 4 路并发下显示的是单 block 速率，不是聚合。
    const wrapper = mount(RunDashboard, {
      props: {
        running: true,
        rateHistory: [10, 12, 11],  // 末尾是单个 block 的速率 = 11
        aggregateRate: 44,           // 4 个活跃 block 各 11 tok/s，合计 44
      },
    })
    // 找实时输出速率的右侧大数字
    const rateValue = wrapper.find('.rd-rate-value')
    expect(rateValue.text()).toContain('44')
  })

  it('透传 LaneView 的 select-block 事件', async () => {
    const active = new Map([[3, { range: '第9-12章', startedAt: 100 }]])
    const wrapper = mount(RunDashboard, {
      props: { running: true, concurrency: 2, activeBlocks: active },
    })
    const laneRow = wrapper.find('.lane-row')
    await laneRow.trigger('click')
    expect(wrapper.emitted('select-block')).toBeTruthy()
    expect(wrapper.emitted('select-block')![0]).toEqual([3])
  })
})
