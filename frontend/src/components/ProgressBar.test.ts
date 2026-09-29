/**
 * ProgressBar 契约测试：百分比钳位、块→章换算（blockSize≤1 不显示）、ETA 展示。
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ProgressBar from './ProgressBar.vue'

describe('ProgressBar 契约', () => {
  it('常规进度：百分比与条宽', () => {
    const wrapper = mount(ProgressBar, { props: { current: 50, total: 200 } })
    expect(wrapper.text()).toContain('50/200')
    expect(wrapper.find('.glass-progress-fill').attributes('style')).toContain('25%')
  })

  it('total ≤ 0 → 0%；current 超出 total → 钳位 100%', () => {
    const zero = mount(ProgressBar, { props: { current: 10, total: 0 } })
    expect(zero.find('.glass-progress-fill').attributes('style')).toContain('0%')
    const over = mount(ProgressBar, { props: { current: 500, total: 200 } })
    expect(over.find('.glass-progress-fill').attributes('style')).toContain('100%')
  })

  it('blockSize ≤ 1 不显示章换算；>1 显示 current×bs / total×bs', () => {
    const none = mount(ProgressBar, { props: { current: 3, total: 10, blockSize: 1 } })
    expect(none.text()).not.toContain('章')
    const withBs = mount(ProgressBar, { props: { current: 3, total: 10, blockSize: 4 } })
    expect(withBs.text()).toContain('(12/40 章)')
  })

  it('自定义 label 与 eta', () => {
    const wrapper = mount(ProgressBar, { props: { current: 1, total: 2, label: '进度', eta: '5分' } })
    expect(wrapper.text()).toContain('进度: 1/2')
    expect(wrapper.text()).toContain('预计剩余: 5分')
  })
})
