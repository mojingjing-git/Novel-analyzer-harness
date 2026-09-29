/**
 * WorkspacePage 组件级测试：统一确认弹窗流——先关弹窗再执行 action，
 * 失败落 error；删除归档传 danger 标记。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('../api/client', () => ({
  api: {
    workspaceNovels: vi.fn(),
    workspaceArchives: vi.fn(),
    archiveNovel: vi.fn(),
    archiveAll: vi.fn(),
    deleteArchive: vi.fn(),
  },
}))

import { api } from '../api/client'
import WorkspacePage from './WorkspacePage.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const mockedApi = vi.mocked(api)

const settle = () => vi.advanceTimersByTimeAsync(0)

async function mountPage() {
  const wrapper = mount(WorkspacePage)
  await settle()
  return wrapper
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  mockedApi.workspaceNovels.mockResolvedValue({
    novels: [{ name: '《甲》', blocks_count: 221, dir_size: 14 * 1024 * 1024, dir_mtime: 1700000000 }],
  } as Awaited<ReturnType<typeof api.workspaceNovels>>)
  mockedApi.workspaceArchives.mockResolvedValue({
    archives: [{ name: '《旧书》', total_size: 1024, dir_mtime: 1700000000 }],
  } as Awaited<ReturnType<typeof api.workspaceArchives>>)
  mockedApi.archiveNovel.mockResolvedValue({} as Awaited<ReturnType<typeof api.archiveNovel>>)
  mockedApi.deleteArchive.mockResolvedValue({} as Awaited<ReturnType<typeof api.deleteArchive>>)
  mockedApi.archiveAll.mockResolvedValue({} as Awaited<ReturnType<typeof api.archiveAll>>)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('WorkspacePage 归档确认流', () => {
  it('归档：确认后调 API 并刷新列表', async () => {
    const wrapper = await mountPage()
    // 行内按钮文本是「归档」，顶部的「批量归档」也含该词——必须精确匹配
    await wrapper.findAll('button').find((b) => b.text().trim() === '归档')!.trigger('click')
    await settle()
    const dialog = wrapper.findComponent(ConfirmDialog)
    expect(dialog.exists()).toBe(true)
    // 文案双重书名号（name 自带《》，模板再包一层）——既有 UX
    expect(wrapper.text()).toContain('归档《《甲》》?')
    await dialog.vm.$emit('confirm')
    await settle()
    expect(mockedApi.archiveNovel).toHaveBeenCalledWith('《甲》')
    expect(wrapper.findComponent(ConfirmDialog).exists()).toBe(false)  // 弹窗先关
    expect(mockedApi.workspaceNovels).toHaveBeenCalledTimes(2)          // action 后刷新
  })

  it('取消：不执行任何操作', async () => {
    const wrapper = await mountPage()
    await wrapper.findAll('button').find((b) => b.text().trim() === '归档')!.trigger('click')
    await settle()
    await wrapper.findComponent(ConfirmDialog).vm.$emit('cancel')
    await settle()
    expect(mockedApi.archiveNovel).not.toHaveBeenCalled()
    expect(wrapper.findComponent(ConfirmDialog).exists()).toBe(false)
  })

  it('删除归档：danger 弹窗 + 不可恢复提示', async () => {
    const wrapper = await mountPage()
    const delBtn = wrapper.findAll('button').find((b) => b.text().includes('删除'))
    await delBtn!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('此操作不可恢复')
    const dialog = wrapper.findComponent(ConfirmDialog)
    expect(dialog.props('danger')).toBe(true)
    await dialog.vm.$emit('confirm')
    await settle()
    expect(mockedApi.deleteArchive).toHaveBeenCalledWith('《旧书》')
  })

  it('action 失败 → 错误落到页面错误条，不静默', async () => {
    mockedApi.archiveNovel.mockRejectedValue(new Error('目录被占用'))
    const wrapper = await mountPage()
    await wrapper.findAll('button').find((b) => b.text().trim() === '归档')!.trigger('click')
    await settle()
    await wrapper.findComponent(ConfirmDialog).vm.$emit('confirm')
    await settle()
    expect(wrapper.text()).toContain('目录被占用')
  })
})
