/**
 * SplitterPage 组件级测试：F-2 整体失败回滚（防"处理中"永久卡死）、
 * pywebview/浏览器双通道文件添加、inferAll 串行容错。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('../api/client', () => ({
  api: {
    previewSplit: vi.fn(),
    batchSplit: vi.fn(),
    inferBookName: vi.fn(),
  },
}))

import { api } from '../api/client'
import SplitterPage from './SplitterPage.vue'

const mockedApi = vi.mocked(api)

const settle = () => vi.advanceTimersByTimeAsync(0)

type PywebviewStub = { api?: { pick_files?: () => Promise<string[] | null> } }

function setPywebview(stub: PywebviewStub | null) {
  if (stub === null) delete (window as unknown as { pywebview?: unknown }).pywebview
  else (window as unknown as { pywebview?: PywebviewStub }).pywebview = stub
}

async function addFilesViaPywebview(wrapper: ReturnType<typeof mount>, paths: string[]) {
  const picker = vi.fn(async () => paths)
  setPywebview({ api: { pick_files: picker } })
  await wrapper.findAll('button').find((b) => b.text().includes('添加文件'))!.trigger('click')
  await settle()
  setPywebview(null)
  return picker
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  setPywebview(null)
})

afterEach(() => {
  setPywebview(null)
  vi.useRealTimers()
})

describe('SplitterPage 文件添加双通道', () => {
  it('pywebview 存在 → 走原生对话框拿真实路径，按路径去重', async () => {
    const wrapper = mount(SplitterPage)
    const picker = await addFilesViaPywebview(wrapper, ['D:\\书\\《甲》.txt', 'D:\\书\\《甲》.txt'])
    expect(picker).toHaveBeenCalledTimes(1)
    const rows = wrapper.findAll('input.glass-input')
    expect(wrapper.text()).toContain('《甲》')
    expect(wrapper.text()).toContain('批量切分（1 本）')  // 重复路径只算一本
  })

  it('浏览器环境 → fallback 到隐藏 file input（不调 pick_files）', async () => {
    const wrapper = mount(SplitterPage)
    const input = wrapper.find('input[type="file"]')
    const clickSpy = vi.spyOn(input.element as HTMLInputElement, 'click').mockImplementation(() => {})
    await wrapper.findAll('button').find((b) => b.text().includes('添加文件'))!.trigger('click')
    await settle()
    expect(clickSpy).toHaveBeenCalled()
  })
})

describe('SplitterPage 批量切分（F-2）', () => {
  it('整体失败 → 全部行回滚"待处理"并显示错误，不永久卡"处理中"', async () => {
    mockedApi.batchSplit.mockRejectedValue(new Error('后端崩了'))
    const wrapper = mount(SplitterPage)
    await addFilesViaPywebview(wrapper, ['D:\\书\\《甲》.txt'])
    await wrapper.findAll('button').find((b) => b.text().includes('批量切分'))!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('后端崩了')
    expect(wrapper.text()).not.toContain('处理中')
    expect(wrapper.text()).toContain('待处理')
  })

  it('成功：按结果回写每行状态与章数', async () => {
    mockedApi.batchSplit.mockResolvedValue({
      total: 1, success: 1, fail: 0,
      results: [{ file: 'D:\\书\\《甲》.txt', book: '《甲》', total_chapters: 886, ok: true, error: '' }],
    } as Awaited<ReturnType<typeof api.batchSplit>>)
    const wrapper = mount(SplitterPage)
    await addFilesViaPywebview(wrapper, ['D:\\书\\《甲》.txt'])
    await wrapper.findAll('button').find((b) => b.text().includes('批量切分'))!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('完成')
    expect(wrapper.text()).toContain('886')
    expect(wrapper.text()).not.toContain('处理中')
  })

  it('部分失败：ok=false 行标"失败"并带错误文案', async () => {
    mockedApi.batchSplit.mockResolvedValue({
      total: 2, success: 1, fail: 1,
      results: [
        { file: 'D:\\a\\《甲》.txt', book: '《甲》', total_chapters: 10, ok: true, error: '' },
        { file: 'D:\\a\\《乙》.txt', book: '《乙》', total_chapters: 0, ok: false, error: '整本书切成一章' },
      ],
    } as Awaited<ReturnType<typeof api.batchSplit>>)
    const wrapper = mount(SplitterPage)
    await addFilesViaPywebview(wrapper, ['D:\\a\\《甲》.txt', 'D:\\a\\《乙》.txt'])
    await wrapper.findAll('button').find((b) => b.text().includes('批量切分'))!.trigger('click')
    await settle()
    expect(wrapper.text()).toContain('整本书切成一章')
    expect(wrapper.text()).toContain('失败')
  })
})

describe('SplitterPage 书名推断', () => {
  it('逐本串行推断；单本失败静默跳过不影响其余', async () => {
    mockedApi.inferBookName.mockImplementation((p: string): Promise<{ book_name: string }> =>
      p.includes('《乙》')
        ? Promise.reject(new Error('推断失败'))
        : Promise.resolve({ book_name: '《正确的书名》' }))
    const wrapper = mount(SplitterPage)
    await addFilesViaPywebview(wrapper, ['D:\\a\\《甲》.txt', 'D:\\a\\《乙》.txt'])
    await wrapper.findAll('button').find((b) => b.text().includes('推断书名'))!.trigger('click')
    await settle()
    expect(mockedApi.inferBookName).toHaveBeenCalledTimes(2)
    // 书名显示在可编辑 input 的 value 里（不在 text 节点）
    const nameInputs = wrapper.findAll('input[placeholder="书名"]')
    expect((nameInputs[0].element as HTMLInputElement).value).toBe('《正确的书名》')
    expect((nameInputs[1].element as HTMLInputElement).value).toBe('《乙》')  // 失败的保留原名
  })
})
