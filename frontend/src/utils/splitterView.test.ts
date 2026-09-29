/**
 * splitterView 纯函数测试：路径/扩展名处理、去重追加、F-2 失败回滚、卷分组。
 */
import { describe, it, expect } from 'vitest'
import {
  baseName, splitterStatusText, appendFileUnique, rollbackProcessing,
  groupChaptersByVolume, type SplitterFileItem,
} from './splitterView'

function mkFile(partial: Partial<SplitterFileItem>): SplitterFileItem {
  return { file_path: '', book_name: '', status: 'pending', chapters: 0, error: '', ...partial }
}

describe('splitterView.baseName', () => {
  it('反斜杠归一 + 剥 .txt/.docx 扩展名（大小写不敏感）', () => {
    expect(baseName('D:\\书\\《甲》.txt')).toBe('《甲》')
    expect(baseName('D:\\书\\《乙》.docx')).toBe('《乙》')
    expect(baseName('/x/y/《丙》.TXT')).toBe('《丙》')
    expect(baseName('无扩展名')).toBe('无扩展名')
  })
})

describe('splitterView.appendFileUnique', () => {
  it('新路径追加并自动取名；重复路径不追加', () => {
    let items: SplitterFileItem[] = []
    let r = appendFileUnique(items, 'D:\\a\\《甲》.txt')
    items = r.items
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ book_name: '《甲》', status: 'pending' })
    r = appendFileUnique(items, 'D:\\a\\《甲》.txt')
    expect(r.added).toBe(false)
    expect(r.items).toHaveLength(1)
  })
})

describe('splitterView.rollbackProcessing（F-2）', () => {
  it('整体失败：processing → pending；done/error 行不受影响', () => {
    const items = [
      mkFile({ file_path: 'a', status: 'processing' }),
      mkFile({ file_path: 'b', status: 'done', chapters: 99 }),
      mkFile({ file_path: 'c', status: 'error', error: 'x' }),
    ]
    const rolled = rollbackProcessing(items)
    expect(rolled.map((f) => f.status)).toEqual(['pending', 'done', 'error'])
  })

  it('返回新数组新对象，不原地修改', () => {
    const items = [mkFile({ file_path: 'a', status: 'processing' })]
    const rolled = rollbackProcessing(items)
    expect(rolled).not.toBe(items)
    expect(rolled[0]).not.toBe(items[0])
    expect(items[0].status).toBe('processing')
  })
})

describe('splitterView.groupChaptersByVolume', () => {
  it('相邻同卷合并；空卷归一组；卷变化切新组', () => {
    const groups = groupChaptersByVolume([
      { chapter: 1, title: 'a', volume: '第一卷' },
      { chapter: 2, title: 'b', volume: '第一卷' },
      { chapter: 3, title: 'c', volume: '' },
      { chapter: 4, title: 'd', volume: '第二卷' },
    ] as never)
    expect(groups.map((g) => g.volume)).toEqual(['第一卷', '', '第二卷'])
    expect(groups[0].items).toHaveLength(2)
  })

  it('空章节列表 → 空分组', () => {
    expect(groupChaptersByVolume([] as never)).toEqual([])
  })
})

describe('splitterView.statusText', () => {
  it('四状态中文映射', () => {
    expect(splitterStatusText('pending')).toBe('待处理')
    expect(splitterStatusText('processing')).toBe('处理中')
    expect(splitterStatusText('done')).toBe('完成')
    expect(splitterStatusText('error')).toBe('失败')
  })
})
