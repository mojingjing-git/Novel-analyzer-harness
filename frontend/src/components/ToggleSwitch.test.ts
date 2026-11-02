/**
 * ToggleSwitch 组件单元测试：
 * 验证初始状态、点击切换、v-model 联动、禁用态阻断、无障碍 role="switch"
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ToggleSwitch from './ToggleSwitch.vue'

describe('ToggleSwitch 规范契约', () => {
  it('渲染基本结构与无障碍属性', () => {
    const wrapper = mount(ToggleSwitch, {
      props: { modelValue: false, label: '测试开关' },
    })
    const button = wrapper.find('button[role="switch"]')
    expect(button.exists()).toBe(true)
    expect(button.attributes('aria-checked')).toBe('false')
    expect(button.classes()).not.toContain('is-checked')
    expect(wrapper.text()).toContain('测试开关')
  })

  it('点击切换触发 update:modelValue 与 change 事件', async () => {
    const wrapper = mount(ToggleSwitch, {
      props: { modelValue: false },
    })
    const button = wrapper.find('button[role="switch"]')
    await button.trigger('click')

    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([true])
    expect(wrapper.emitted('change')?.[0]).toEqual([true])
  })

  it('当 modelValue 为 true 时具有 is-checked 类且 aria-checked 为 true', () => {
    const wrapper = mount(ToggleSwitch, {
      props: { modelValue: true },
    })
    const button = wrapper.find('button[role="switch"]')
    expect(button.classes()).toContain('is-checked')
    expect(button.attributes('aria-checked')).toBe('true')
  })

  it('禁用状态下点击不触发任何事件', async () => {
    const wrapper = mount(ToggleSwitch, {
      props: { modelValue: false, disabled: true },
    })
    const button = wrapper.find('button[role="switch"]')
    expect(button.attributes('disabled')).toBeDefined()
    await button.trigger('click')

    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(wrapper.emitted('change')).toBeUndefined()
  })
})
