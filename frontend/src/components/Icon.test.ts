/**
 * Icon 契约测试：注册表渲染 / 未知名 fallback 到 info / 默认尺寸。
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import Icon from './Icon.vue'

describe('Icon 契约', () => {
  it('已知图标：渲染 SVG，默认 18px / strokeWidth 1.5', () => {
    const wrapper = mount(Icon, { props: { name: 'play' } })
    const svg = wrapper.find('svg')
    expect(svg.exists()).toBe(true)
    expect(svg.attributes('width')).toBe('18')
    expect(svg.attributes('stroke-width')).toBe('1.5')
    expect(svg.html()).toContain('<polygon')
  })

  it('未知名 → fallback 到 info 图标（圆环路径）', () => {
    const wrapper = mount(Icon, { props: { name: 'no_such_icon' } })
    expect(wrapper.find('svg').html()).toContain('cx="12" cy="12" r="10"')
  })

  it('自定义尺寸透传', () => {
    const wrapper = mount(Icon, { props: { name: 'x', size: 24, strokeWidth: 2 } })
    expect(wrapper.find('svg').attributes('width')).toBe('24')
    expect(wrapper.find('svg').attributes('stroke-width')).toBe('2')
  })
})
