/**
 * API 层综合测试：覆盖 sessionToken、HTTP client 鉴权/错误展开、useProgressSocket 单例重连与主动关闭。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import {
  getSessionToken,
  __setSessionTokenForTests,
  initSessionToken,
} from '../api/sessionToken'
import { api } from '../api/client'
import {
  useProgressSocket,
  closeProgressSocket,
  type ProgressMessage,
} from '../api/useProgressSocket'

describe('sessionToken', () => {
  beforeEach(() => {
    __setSessionTokenForTests(null)
    delete (window as unknown as { pywebview?: unknown }).pywebview
  })

  afterEach(() => {
    __setSessionTokenForTests(null)
    delete (window as unknown as { pywebview?: unknown }).pywebview
  })

  it('无 pywebview 时保持 null，pywebview 就绪时拉取并写入内存单例', async () => {
    expect(getSessionToken()).toBeNull()
    initSessionToken()
    expect(getSessionToken()).toBeNull()

    ;(window as unknown as { pywebview?: unknown }).pywebview = {
      api: { get_session_token: async () => 'tok-pywebview-123' },
    }
    initSessionToken()
    await Promise.resolve()
    expect(getSessionToken()).toBe('tok-pywebview-123')
  })
})

describe('api client', () => {
  const fetchMock = vi.fn()

  function jsonResponse(body: unknown, status = 200) {
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: status === 200 ? 'OK' : 'Error',
      text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
    }
  }

  beforeEach(() => {
    __setSessionTokenForTests(null)
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    __setSessionTokenForTests(null)
    vi.unstubAllGlobals()
  })

  it('注入 X-Session-Token 且不可被调用方 headers 覆盖', async () => {
    __setSessionTokenForTests('sec-token')
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))
    await api.putSettings({})
    const [, init] = fetchMock.mock.calls[0]
    const headers = init.headers as Record<string, string>
    expect(headers['X-Session-Token']).toBe('sec-token')
    expect(headers['Content-Type']).toBe('application/json')

    await api.patchSettings({ analysis: { summary_batch_size: 20 } })
    const [patchUrl, patchInit] = fetchMock.mock.calls[1]
    expect(patchUrl).toBe('/api/settings')
    expect(patchInit.method).toBe('PATCH')
  })

  it('非 2xx 展开 FastAPI detail 字符串并保留 422 校验数组 detail', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: '未找到指定书籍' }, 404))
    await expect(api.getSettings()).rejects.toMatchObject({
      message: '未找到指定书籍',
      status: 404,
    })

    const detail = [
      { loc: ['body', 'api', 'timeout'], msg: 'Input should be greater than 0' },
    ]
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail }, 422))
    await expect(api.putSettings({})).rejects.toMatchObject({
      status: 422,
      detail,
    })
  })

  it('reviewForeshadowItem 对路径段做 URL 编码并以 POST 携带 X-Session-Token', async () => {
    __setSessionTokenForTests('sec-token')
    fetchMock.mockResolvedValueOnce(jsonResponse({
      book_id: '书 A',
      item: { id: 'fs#1', status: 'resolved', source_type: 'human_override', needs_review: false, resolution_quality: 'verified' },
      lifecycle_bars: { resolved_verified: 1 },
      counts: { total: 1, active: 0, resolved: 1, dormant: 0 },
    }))
    await api.reviewForeshadowItem('书 A', 'fs#1', { action: 'confirm_resolved', resolved_chapter: 18 })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/books/%E4%B9%A6%20A/ledger/fs%231/review')
    expect(init.method).toBe('POST')
    expect((init.headers as Record<string, string>)['X-Session-Token']).toBe('sec-token')
    expect(JSON.parse(init.body as string)).toEqual({ action: 'confirm_resolved', resolved_chapter: 18 })
  })
})

describe('useProgressSocket', () => {
  interface FakeSocket {
    url: string
    readyState: number
    onopen: (() => void) | null
    onclose: (() => void) | null
    onerror: (() => void) | null
    onmessage: ((ev: { data: string }) => void) | null
    close: ReturnType<typeof vi.fn>
  }

  let sockets: FakeSocket[] = []

  function installFakeWebSocket() {
    sockets = []
    class MockWebSocket {
      static OPEN = 1
      url: string
      readyState = 0
      onopen: (() => void) | null = null
      onclose: (() => void) | null = null
      onerror: (() => void) | null = null
      onmessage: ((ev: { data: string }) => void) | null = null
      close = vi.fn(() => {
        this.readyState = 3
        this.onclose?.()
      })
      constructor(url: string) {
        this.url = url
        sockets.push(this)
      }
    }
    vi.stubGlobal('WebSocket', MockWebSocket)
  }

  function mountHost(onMessage: (m: ProgressMessage) => void) {
    let exposed!: ReturnType<typeof useProgressSocket>
    const Host = defineComponent({
      setup() {
        exposed = useProgressSocket(onMessage)
        return () => h('div')
      },
    })
    const wrapper = mount(Host)
    return { wrapper, get state() { return exposed } }
  }

  beforeEach(() => {
    vi.useFakeTimers()
    installFakeWebSocket()
  })

  afterEach(() => {
    closeProgressSocket()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('分发业务消息并过滤 ping、指数退避重连、closeProgressSocket 主动关闭不产生孤儿重连', () => {
    const received: ProgressMessage[] = []
    const { wrapper, state } = mountHost((m) => received.push(m))

    expect(sockets).toHaveLength(1)
    sockets[0].onopen?.()
    expect(state.connected.value).toBe(true)

    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'ping' }) })
    sockets[0].onmessage?.({ data: JSON.stringify({ type: 'log', payload: { level: 'info', text: 'hi', source: 'business', category: 'analysis' } }) })
    expect(received).toHaveLength(1)

    // 异常断开触发 1s -> 2s 指数退避重连
    sockets[0].onclose?.()
    expect(state.connected.value).toBe(false)
    vi.advanceTimersByTime(1000)
    expect(sockets).toHaveLength(2)

    sockets[1].onclose?.()
    vi.advanceTimersByTime(1999)
    expect(sockets).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(sockets).toHaveLength(3)

    // 主动关闭不再触发重连
    closeProgressSocket()
    vi.advanceTimersByTime(60_000)
    expect(sockets).toHaveLength(3)
    wrapper.unmount()
  })
})
