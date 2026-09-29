/**
 * BookSelector 组件契约测试（真实组件首次被测——各页面测试里它都是 stub）：
 * ?book= 查询参数预置 / 仅 modelValue 为空才回填 / change 事件转发 / 刷新按钮。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('../api/client', () => ({
  api: { listBooks: vi.fn() },
}))

import { api, type BookInfo } from '../api/client'
import BookSelector from './BookSelector.vue'

const mockedApi = vi.mocked(api)

const books: BookInfo[] = [
  { id: 'A', name: '《甲》', total_chapters: 10, has_report: true, has_aggregated: false },
  { id: 'B', name: '《乙》', total_chapters: 20, has_report: false, has_aggregated: true },
] as BookInfo[]

const settle = () => vi.advanceTimersByTimeAsync(0)

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  mockedApi.listBooks.mockResolvedValue(books)
  window.history.replaceState({}, '', '/')  // 清掉上一用例的查询参数
})

afterEach(() => {
  window.history.replaceState({}, '', '/')
  vi.useRealTimers()
})

describe('BookSelector 契约', () => {
  it('加载书目并渲染章节/聚合标记', async () => {
    const wrapper = mount(BookSelector, { props: { modelValue: '' } })
    await settle()
    const text = wrapper.text()
    expect(text).toContain('《甲》 (10 章)')
    expect(text).toContain('✓已总结')
    expect(text).toContain('✓已聚合')
  })

  it('?book= 预置：仅当 modelValue 为空且 id 存在于列表才 emit', async () => {
    window.history.replaceState({}, '', '/?book=B')
    const wrapper = mount(BookSelector, { props: { modelValue: '' } })
    await settle()
    expect(wrapper.emitted('update:modelValue')![0]).toEqual(['B'])

    // modelValue 已有值 → 不覆盖
    const wrapper2 = mount(BookSelector, { props: { modelValue: 'A' } })
    await settle()
    expect(wrapper2.emitted('update:modelValue')).toBeUndefined()
  })

  it('?book= 指向不存在的 id → 不 emit', async () => {
    window.history.replaceState({}, '', '/?book=NOPE')
    const wrapper = mount(BookSelector, { props: { modelValue: '' } })
    await settle()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('change 事件转发选中值', async () => {
    const wrapper = mount(BookSelector, { props: { modelValue: '' } })
    await settle()
    await wrapper.find('select').setValue('A')
    expect(wrapper.emitted('update:modelValue')![0]).toEqual(['A'])
  })

  it('刷新按钮重拉书目', async () => {
    const wrapper = mount(BookSelector, { props: { modelValue: '' } })
    await settle()
    expect(mockedApi.listBooks).toHaveBeenCalledTimes(1)
    await wrapper.findAll('button')[0].trigger('click')
    await settle()
    expect(mockedApi.listBooks).toHaveBeenCalledTimes(2)
  })
})
