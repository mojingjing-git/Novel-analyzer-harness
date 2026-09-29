/**
 * LogConsole 契约测试。重点：simplified 过滤规则（debug/python/block 三排除）
 * ——它与 QueuePage 的 logsSimplifiedCount 是双重实现，此测试把契约钉死，
 * 任何一侧改动导致计数与显示不符会在此处先红。
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import LogConsole, { type LogEntry } from './LogConsole.vue'

function entry(partial: Partial<LogEntry> & { id: number }): LogEntry {
  return { text: `t${partial.id}`, kind: 'info', ...partial } as LogEntry
}

const mixed: LogEntry[] = [
  entry({ id: 1, kind: 'info', source: 'business' }),    // 保留
  entry({ id: 2, kind: 'debug', source: 'business' }),   // 排除：debug 级
  entry({ id: 3, kind: 'info', source: 'python' }),      // 排除：技术日志
  entry({ id: 4, kind: 'info', source: 'block' }),       // 排除：逐章事件
  entry({ id: 5, kind: 'error', source: 'local' }),      // 保留
  entry({ id: 6, kind: 'state' }),                       // 保留（无 source）
]

describe('LogConsole simplified 过滤契约', () => {
  it('simplified：过滤 debug + python + block 三类，其余保留', () => {
    const wrapper = mount(LogConsole, { props: { logs: mixed, simplified: true } })
    const rows = wrapper.findAll('.log-row')
    expect(rows.map((r) => r.text())).toEqual(['t1', 't5', 't6'])
  })

  it('完整模式不过滤', () => {
    const wrapper = mount(LogConsole, { props: { logs: mixed } })
    expect(wrapper.findAll('.log-row')).toHaveLength(6)
  })

  it('空日志 → 空态提示', () => {
    const wrapper = mount(LogConsole, { props: { logs: [] } })
    expect(wrapper.text()).toContain('等待日志...')
  })

  it('日志增长自动滚到底部', async () => {
    const wrapper = mount(LogConsole, { props: { logs: [entry({ id: 1 })] } })
    const el = wrapper.find('.log-console').element as HTMLElement
    // jsdom 不布局，scrollHeight/scrollTop 恒 0——只验证 watcher 把 scrollTop 写成 scrollHeight
    Object.defineProperty(el, 'scrollHeight', { configurable: true, value: 500 })
    await wrapper.setProps({ logs: [entry({ id: 1 }), entry({ id: 2 })] })
    await wrapper.vm.$nextTick()
    expect(el.scrollTop).toBe(500)
  })
})
