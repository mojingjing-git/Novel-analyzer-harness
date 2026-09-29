/**
 * ConfirmDialog 契约测试：默认文案 / danger 样式 / 遮罩点击取消 / confirm·cancel 事件。
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ConfirmDialog from './ConfirmDialog.vue'

describe('ConfirmDialog 契约', () => {
  it('默认文案（取消/确认）与 message 渲染', () => {
    const wrapper = mount(ConfirmDialog, { props: { message: '确定吗？' } })
    expect(wrapper.text()).toContain('确定吗？')
    const btns = wrapper.findAll('button')
    expect(btns.map((b) => b.text())).toEqual(['取消', '确认'])
  })

  it('自定义 title / confirmText / cancelText', () => {
    const wrapper = mount(ConfirmDialog, {
      props: { title: '删除小说', message: 'm', confirmText: '删除', cancelText: '再想想' },
    })
    expect(wrapper.text()).toContain('删除小说')
    expect(wrapper.findAll('button').map((b) => b.text())).toEqual(['再想想', '删除'])
  })

  it('danger → 确认按钮切换 danger 样式', () => {
    const normal = mount(ConfirmDialog, { props: { message: 'm' } })
    expect(normal.findAll('button')[1].classes()).toContain('glass-button-primary')
    const danger = mount(ConfirmDialog, { props: { message: 'm', danger: true } })
    expect(danger.findAll('button')[1].classes()).toContain('glass-button-danger')
  })

  it('点确认/取消按钮 → 对应事件', async () => {
    const wrapper = mount(ConfirmDialog, { props: { message: 'm' } })
    await wrapper.findAll('button')[0].trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)
    await wrapper.findAll('button')[1].trigger('click')
    expect(wrapper.emitted('confirm')).toHaveLength(1)
  })

  it('点遮罩空白（.self）→ cancel；点卡片本体不关', async () => {
    const wrapper = mount(ConfirmDialog, { props: { message: 'm' } })
    await wrapper.find('.confirm-card').trigger('click')
    expect(wrapper.emitted('cancel')).toBeUndefined()
    await wrapper.find('.confirm-mask').trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)
  })
})
