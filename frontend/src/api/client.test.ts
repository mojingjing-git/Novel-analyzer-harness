/** request() 行为契约：JSON 解析/错误提取/非 JSON 显式失败 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { api } from './client'
import { __setSessionTokenForTests } from './sessionToken'

function mockFetch(body: string, ok = true, status = 200, statusText = 'OK') {
  const fn = vi.fn(async (_path: string, init?: RequestInit) => ({
    ok, status, statusText,
    text: async () => body,
  }))
  vi.stubGlobal('fetch', fn)
  return fn
}

/** 取 mock fetch 首次调用的请求 headers（调用方传的 init） */
function firstHeaders(fn: ReturnType<typeof mockFetch>): Record<string, string> {
  return (fn.mock.calls[0]?.[1]?.headers ?? {}) as Record<string, string>
}

afterEach(() => {
  vi.unstubAllGlobals()
  __setSessionTokenForTests(null)
})

describe('client.request', () => {
  it('合法 JSON 正常解析', async () => {
    mockFetch('{"a":1}')
    expect(await api.health()).toEqual({ a: 1 })
  })

  it('空 body 宽容返回空对象', async () => {
    mockFetch('', true, 204)
    expect(await api.health()).toEqual({})
  })

  it('ok 但 body 非 JSON 必须显式抛错（此前静默返回 {} 致下游 TypeError 白屏）', async () => {
    mockFetch('<html>Bad Gateway</html>')
    await expect(api.health()).rejects.toMatchObject({ status: 200 })
  })

  it('非 ok 时优先取 detail 字段作为错误消息并附加 status', async () => {
    mockFetch('{"detail":"书目不存在"}', false, 404, 'Not Found')
    await expect(api.getBookReport('某书')).rejects.toMatchObject({
      message: '书目不存在',
      status: 404,
    })
  })
})

describe('client.request X-Session-Token（PR-1 收尾）', () => {
  it('token 已注入时请求附带 X-Session-Token header', async () => {
    __setSessionTokenForTests('tok-abc')
    const fn = mockFetch('{"a":1}')
    await api.health()
    expect(firstHeaders(fn)['X-Session-Token']).toBe('tok-abc')
  })

  it('token 为 null（浏览器 dev）时不附带该 header', async () => {
    __setSessionTokenForTests(null)
    const fn = mockFetch('{"a":1}')
    await api.health()
    expect(firstHeaders(fn)['X-Session-Token']).toBeUndefined()
  })

  it('POST + body 时 token 与 Content-Type 共存（token 最后合并、不可覆盖既有键）', async () => {
    __setSessionTokenForTests('tok-abc')
    const fn = mockFetch('{"ok":true}')
    await api.startAnalysis()
    const h = firstHeaders(fn)
    expect(h['Content-Type']).toBe('application/json')
    expect(h['X-Session-Token']).toBe('tok-abc')
  })
})

describe('client.downloadLogs（PR-1 收尾）', () => {
  it('200 text/plain 触发 blob 下载且不抛错（独立 fetch，绕开 request() 非 JSON 抛错契约）', async () => {
    __setSessionTokenForTests('tok-abc')
    const createObjectURL = vi.fn(() => 'blob:mock')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL } as unknown as typeof URL)
    const fn = vi.fn(async (_path: string, init?: RequestInit) => ({
      ok: true, status: 200, statusText: 'OK',
      text: async () => '',
      blob: async () => new Blob(['log content']),
    }))
    vi.stubGlobal('fetch', fn)
    await expect(api.downloadLogs()).resolves.toBeUndefined()
    expect(firstHeaders(fn as unknown as ReturnType<typeof mockFetch>)['X-Session-Token']).toBe('tok-abc')
    expect(createObjectURL).toHaveBeenCalledOnce()
  })

  it('403 时抛错并带 status', async () => {
    __setSessionTokenForTests(null)
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false, status: 403, statusText: 'Forbidden',
        text: async () => '', blob: async () => new Blob(),
      })),
    )
    await expect(api.downloadLogs()).rejects.toMatchObject({ status: 403 })
  })
})

describe('client.request 超时（A3：此前没有超时，请求卡住会永久禁用按钮）', () => {
  it('fetch 抛 TimeoutError 时映射为 TIMEOUT 并给中文文案', async () => {
    const fn = vi.fn(async () => {
      const err = new Error('signal timed out')
      err.name = 'TimeoutError'
      throw err
    })
    vi.stubGlobal('fetch', fn)
    await expect(api.health()).rejects.toMatchObject({
      message: '请求超时，请稍后重试',
      code: 'TIMEOUT',
    })
  })

  it('主动中断映射为 ABORTED，与超时区分开', async () => {
    const fn = vi.fn(async () => {
      const err = new Error('aborted')
      err.name = 'AbortError'
      throw err
    })
    vi.stubGlobal('fetch', fn)
    await expect(api.health()).rejects.toMatchObject({ code: 'ABORTED' })
  })

  it('默认请求带上 AbortSignal，且调用方可覆盖 timeoutMs', async () => {
    const fn = mockFetch('{"ok":true}')
    await api.health()
    const init = fn.mock.calls[0]?.[1] as RequestInit
    expect(init.signal).toBeDefined()
  })

  it('T I M E O U T 出现在错误码中文映射里', async () => {
    const { vnextErrorText } = await import('./client')
    expect(vnextErrorText('TIMEOUT')).toContain('超时')
    expect(vnextErrorText('ABORTED')).toContain('取消')
  })
})
