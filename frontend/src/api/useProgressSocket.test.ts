/** useProgressSocket 守护测试：FakeWebSocket + 假时钟 + resetModules 隔离 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  url: string
  readyState = 0
  sent: string[] = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  constructor(url: string) { this.url = url; FakeWebSocket.instances.push(this) }
  send(data: string) { this.sent.push(data) }
  close() { this.readyState = 3; this.onclose?.() }
  open() { this.readyState = 1; this.onopen?.() }
  receive(obj: unknown) { this.onmessage?.({ data: JSON.stringify(obj) }) }
  drop() { this.onclose?.() }
}

async function freshModule() {
  vi.resetModules()
  return await import('./useProgressSocket')
}

async function inComponent<T>(fn: (m: typeof import('./useProgressSocket')) => T): Promise<T> {
  let out!: T
  mount({ setup() { out = fn(mCurrent!); return () => '<div/>' } })
  return out
}

let mCurrent: typeof import('./useProgressSocket') | null = null

beforeEach(async () => {
  vi.useFakeTimers()
  vi.stubGlobal('WebSocket', FakeWebSocket)
  FakeWebSocket.instances = []
  mCurrent = await freshModule()
})

afterEach(() => {
  mCurrent?.closeProgressSocket()
  vi.unstubAllGlobals()
  vi.useRealTimers()
  mCurrent = null
})

describe('useProgressSocket', () => {
  it('init 单例：两次初始化只建一条连接', async () => {
    const m = mCurrent!
    m.initProgressSocket()
    m.initProgressSocket()
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  it('非 ping 消息分发给订阅者，ping 被静默丢弃', async () => {
    const seen: string[] = []
    await inComponent(m2 => m2.useProgressSocket(msg => seen.push(msg.type)))
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].receive({ type: 'ping', payload: {} })
    FakeWebSocket.instances[0].receive({ type: 'block_done', payload: {} })
    expect(seen).toEqual(['block_done'])
  })

  it('订阅者抛异常不影响其他订阅者', async () => {
    const seen: string[] = []
    await inComponent(m2 => {
      m2.useProgressSocket(() => { throw new Error('炸了') })
      m2.useProgressSocket(msg => seen.push(msg.type))
    })
    FakeWebSocket.instances[0].open()
    FakeWebSocket.instances[0].receive({ type: 'progress', payload: {} })
    expect(seen).toEqual(['progress'])
  })

  it('自然掉线按指数退避重连：1s→2s，成功 open 后归零', async () => {
    const m = mCurrent!
    m.initProgressSocket()
    FakeWebSocket.instances[0].drop()                    // retry=0 → 1s
    await vi.advanceTimersByTimeAsync(999)
    expect(FakeWebSocket.instances).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(FakeWebSocket.instances).toHaveLength(2)
    FakeWebSocket.instances[1].drop()                    // retry=1 → 2s
    await vi.advanceTimersByTimeAsync(1999)
    expect(FakeWebSocket.instances).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    FakeWebSocket.instances[2].open()                    // 成功 → retry 归零
    FakeWebSocket.instances[2].drop()
    await vi.advanceTimersByTimeAsync(999)
    expect(FakeWebSocket.instances).toHaveLength(3)      // 又是 1s 档
    await vi.advanceTimersByTimeAsync(1)
    expect(FakeWebSocket.instances).toHaveLength(4)
  })

  it('重连延迟封顶 10s', async () => {
    const m = mCurrent!
    m.initProgressSocket()
    for (let i = 0; i < 6; i++) {
      FakeWebSocket.instances.at(-1)!.drop()
      await vi.advanceTimersByTimeAsync(10_000)
    }
    // 第 7 次 drop 后延迟应停在 10s：advance 9.9s 不出新连接
    FakeWebSocket.instances.at(-1)!.drop()
    await vi.advanceTimersByTimeAsync(9_900)
    const n = FakeWebSocket.instances.length
    await vi.advanceTimersByTimeAsync(100)
    expect(FakeWebSocket.instances.length).toBe(n + 1)
  })

  it('manualClose 粘滞：主动关闭后的自然掉线不再重连（现有语义锁定）', async () => {
    const m = mCurrent!
    m.initProgressSocket()
    m.closeProgressSocket()          // close() 同步触发首次 onclose：manualClose 被消费且不重连
    await vi.advanceTimersByTimeAsync(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)

    // 现状缺陷锁定（含粘滞行为本身）：二次置位时已无连接可消费标记，
    // 该标记会“粘”到下一条新连接上，使其自然掉线也不再重连；未来修复后此处应反转。
    m.closeProgressSocket()
    m.initProgressSocket()
    const n = FakeWebSocket.instances.length
    FakeWebSocket.instances.at(-1)!.drop()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(FakeWebSocket.instances).toHaveLength(n)
  })

  it('组件卸载自动移除订阅者', async () => {
    const m = mCurrent!
    const seen: string[] = []
    const w = mount({ setup() { m.useProgressSocket(msg => seen.push(msg.type)); return () => '<div/>' } })
    FakeWebSocket.instances[0].open()
    w.unmount()
    FakeWebSocket.instances[0].receive({ type: 'log', payload: {} })
    expect(seen).toEqual([])
  })
})
