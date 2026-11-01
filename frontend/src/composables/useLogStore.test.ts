/** useLogStore 守护测试：单例隔离用 vi.resetModules + 动态导入 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const KEY = 'novel-analyzer-logs'

function seed(entries: unknown[]) {
  window.localStorage.setItem(KEY, JSON.stringify(entries))
}

async function freshStore() {
  vi.resetModules()
  const mod = await import('./useLogStore')
  return mod.useLogStore()
}

beforeEach(() => {
  window.localStorage.clear()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useLogStore', () => {
  it('坏条目被过滤且 nextId 不被污染', async () => {
    seed([{ id: 1, text: '好条目' }, { id: 'bad', text: 'x' }, { text: '无id' }])
    const store = await freshStore()
    expect(store.logs.value).toHaveLength(1)
    store.add('新日志')
    expect(store.logs.value.at(-1)!.id).toBe(2)
    expect(store.logs.value.at(-1)!.text).toBe('新日志')
  })

  it('加载即裁剪到 2000 条上限', async () => {
    seed(Array.from({ length: 2050 }, (_, i) => ({ id: i + 1, text: `t${i}` })))
    const store = await freshStore()
    expect(store.logs.value).toHaveLength(2000)
    expect(store.logs.value[0].id).toBe(51)
  })

  it('migrateSource 给旧 Python 日志补 source', async () => {
    seed([{ id: 1, text: '2026-08-07 10:00:00 [INFO] backend.core.pipeline: xxx' }])
    const store = await freshStore()
    expect(store.logs.value[0].source).toBe('python')
  })

  it('add 超限裁剪 + 节流持久化合并写入', async () => {
    seed(Array.from({ length: 1999 }, (_, i) => ({ id: i + 1, text: `t${i}` })))
    const store = await freshStore()
    store.add('第一条')
    store.add('第二条')
    expect(store.logs.value).toHaveLength(2000)
    // watch 回调走微任务队列：用 Async 推进先冲掉 nextTick 再跑节流定时器
    await vi.advanceTimersByTimeAsync(500)
    const saved = JSON.parse(window.localStorage.getItem(KEY)!)
    expect(saved).toHaveLength(2000)
    expect(saved.at(-1).text).toBe('第二条')
  })

  it('clear 清空内存并移除存储键', async () => {
    seed([{ id: 1, text: 'a' }])
    const store = await freshStore()
    store.clear()
    expect(store.logs.value).toHaveLength(0)
    expect(window.localStorage.getItem(KEY)).toBeNull()
  })

  it('filter/removeByCategory 按 category 生效', async () => {
    const store = await freshStore()
    store.add('分析日志', 'info', 'analysis')
    store.add('总结日志', 'info', 'summary')
    expect(store.filter('summary')).toHaveLength(1)
    store.removeByCategory('analysis')
    expect(store.logs.value).toHaveLength(1)
    expect(store.logs.value[0].category).toBe('summary')
  })
})
