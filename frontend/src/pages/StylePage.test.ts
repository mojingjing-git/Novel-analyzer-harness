/**
 * StylePage 组件级测试：F-3 stopRequested 标志（停止后轮询不回跳 running）、
 * 结果文件缺失=失败非完成、onMounted 恢复轮询、无书早退。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('../api/client', () => ({
  api: {
    startStyle: vi.fn(),
    stopStyle: vi.fn(),
    styleStatus: vi.fn(),
    getStyleResult: vi.fn(),
  },
}))
vi.mock('../api/useProgressSocket', () => ({
  useProgressSocket: vi.fn(() => ({ connected: { value: true } })),
}))
vi.mock('../components/BookSelector.vue', () => ({
  default: {
    name: 'BookSelector',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<select :value="modelValue" @change="$emit('update:modelValue', $event.target.value)"></select>`,
  },
}))

import { api } from '../api/client'
import StylePage from './StylePage.vue'
import { useLogStore } from '../composables/useLogStore'

const mockedApi = vi.mocked(api)

const settle = () => vi.advanceTimersByTimeAsync(0)

// 每个用例挂载后统一卸载：清掉轮询 interval，防止跨用例计 poll 次数
let activeWrapper: ReturnType<typeof mount> | null = null
async function mountPage() {
  activeWrapper = mount(StylePage)
  await settle()
  return activeWrapper
}

async function pickBook(wrapper: ReturnType<typeof mount>, id: string) {
  wrapper.findComponent({ name: 'BookSelector' }).vm.$emit('update:modelValue', id)
  await settle()
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()          // 调用计数跨用例隔离
  useLogStore().clear()       // 日志是模块级单例，跨用例会互相污染断言
  mockedApi.styleStatus.mockResolvedValue({ running: false, phase: '' } as Awaited<ReturnType<typeof api.styleStatus>>)
  mockedApi.startStyle.mockResolvedValue({} as Awaited<ReturnType<typeof api.startStyle>>)
  mockedApi.stopStyle.mockResolvedValue({} as Awaited<ReturnType<typeof api.stopStyle>>)
  mockedApi.getStyleResult.mockResolvedValue({ book_id: 'A', content: '# 风格分析内容' })
})

afterEach(() => {
  activeWrapper?.unmount()
  activeWrapper = null
  vi.useRealTimers()
})

describe('StylePage 轮询状态机（F-3）', () => {
  it('F-3：停止后后端仍短暂报 running → 不得把 running 置回 true，也不读结果', async () => {
    const wrapper = await mountPage()
    await pickBook(wrapper, 'A')
    await wrapper.findAll('button').find((b) => b.text().includes('开始分析'))!.trigger('click')
    await settle()
    expect(mockedApi.startStyle).toHaveBeenCalled()

    await wrapper.findAll('button').find((b) => b.text().includes('停止'))!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('开始分析')  // running=false 回到可开始态

    // 下一轮轮询：后端仍报 running（短暂窗口）→ 不回跳、不读结果
    mockedApi.styleStatus.mockResolvedValue({ running: true, phase: 'llm_extract' } as Awaited<ReturnType<typeof api.styleStatus>>)
    await vi.advanceTimersByTimeAsync(2000)
    await settle()
    expect(wrapper.text()).toContain('开始分析')                       // 未回跳"运行中"
    expect(mockedApi.getStyleResult).not.toHaveBeenCalled()            // 不读取结果
  })

  it('轮询到完成：拉结果展示并记「风格分析完成」', async () => {
    const wrapper = await mountPage()
    await pickBook(wrapper, 'A')
    await wrapper.findAll('button').find((b) => b.text().includes('开始分析'))!.trigger('click')
    mockedApi.styleStatus.mockResolvedValue({ running: false, phase: 'done' } as Awaited<ReturnType<typeof api.styleStatus>>)
    await vi.advanceTimersByTimeAsync(2000)
    await settle()
    expect(wrapper.text()).toContain('# 风格分析内容')
    expect(wrapper.text()).not.toContain('风格分析失败')
  })

  it('F-3 第二处：结束但结果文件不存在 = 失败（不说"完成"）', async () => {
    const wrapper = await mountPage()
    await pickBook(wrapper, 'A')
    await wrapper.findAll('button').find((b) => b.text().includes('开始分析'))!.trigger('click')
    mockedApi.styleStatus.mockResolvedValue({ running: false, phase: 'done' } as Awaited<ReturnType<typeof api.styleStatus>>)
    mockedApi.getStyleResult.mockRejectedValue(new Error('404'))
    await vi.advanceTimersByTimeAsync(2000)
    await settle()
    expect(wrapper.text()).toContain('风格分析失败（未生成结果文件）')
    expect(wrapper.text()).not.toContain('风格分析完成')
  })

  it('onMounted：后端已在运行 → 恢复 running 并接续轮询', async () => {
    mockedApi.styleStatus.mockResolvedValue({ running: true, phase: 'computing_stats' } as Awaited<ReturnType<typeof api.styleStatus>>)
    await mountPage()
    expect(mockedApi.styleStatus).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2000)
    expect(mockedApi.styleStatus).toHaveBeenCalledTimes(2)  // 轮询已接上
  })

  it('无书时「开始分析」禁用（start 早退的 UI 门）', async () => {
    const wrapper = await mountPage()
    const btn = wrapper.findAll('button').find((b) => b.text().includes('开始分析'))!
    expect(btn.attributes('disabled')).toBeDefined()
    expect(mockedApi.startStyle).not.toHaveBeenCalled()
  })
})
