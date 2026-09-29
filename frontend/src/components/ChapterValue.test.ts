/**
 * ChapterValue 契约测试：递归渲染（标量 null → '—'、对象键中文标签、
 * 顶层对象数组卡片化）——章节详情面板的展示基石。
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ChapterValue from './ChapterValue.vue'

describe('ChapterValue 契约', () => {
  it('标量：null → —，字符串/数字原样', () => {
    expect(mount(ChapterValue, { props: { value: null } }).text()).toBe('—')
    expect(mount(ChapterValue, { props: { value: '文本' } }).text()).toBe('文本')
    expect(mount(ChapterValue, { props: { value: 42 } }).text()).toBe('42')
  })

  it('对象：已知键出中文标签，未知键原样', () => {
    const wrapper = mount(ChapterValue, {
      props: { value: { event: '夺剑', characters: '张三', unknown_key: 'x' } },
    })
    const text = wrapper.text()
    expect(text).toContain('事件')
    expect(text).toContain('涉及人物')
    expect(text).toContain('unknown_key')
    expect(text).toContain('夺剑')
  })

  it('对象数组在顶层渲染为卡片（cv-cards），元素递归为键值对', () => {
    const wrapper = mount(ChapterValue, {
      props: {
        value: [{ id: 1, event: '事件A' }, { id: 2, event: '事件B' }],
        depth: 0,
      },
    })
    expect(wrapper.findAll('.cv-card')).toHaveLength(2)
    expect(wrapper.find('.cv-cards').exists()).toBe(true)
    expect(wrapper.text()).toContain('事件A')
  })

  it('标量数组渲染为普通列表（非卡片）', () => {
    const wrapper = mount(ChapterValue, { props: { value: ['甲', '乙'], depth: 0 } })
    expect(wrapper.findAll('.cv-item')).toHaveLength(2)
    expect(wrapper.find('.cv-cards').exists()).toBe(false)
  })

  it('嵌套对象递归：字段值是对象时继续展开', () => {
    const wrapper = mount(ChapterValue, {
      props: { value: { cross_block: { summary: '衔接', unresolved_questions: ['?'] } } },
    })
    expect(wrapper.text()).toContain('小结')
    expect(wrapper.text()).toContain('未解问题')
  })
})
