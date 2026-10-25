/** sessionToken 模块行为 + 与 desktop.py js_api 方法名的契约（PR-1 收尾） */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { __setSessionTokenForTests, getSessionToken, initSessionToken } from './sessionToken'

// 契约一致性（T3.3）：js_api 方法名打错只会静默得到 null → 403 复现且无任何报错，
// 用源码级断言把两侧方法名锁死。
// 注意用 import.meta.dirname（vitest 提供的真实磁盘路径）而非 import.meta.url——
// jsdom 环境下后者是 http:// 假 URL，fileURLToPath 会抛 "scheme file" 错。
const apiDir = import.meta.dirname
const desktopSrc = readFileSync(join(apiDir, '../../../desktop.py'), 'utf-8')

beforeEach(() => __setSessionTokenForTests(null))

afterEach(() => {
  vi.unstubAllGlobals()
  delete (window as unknown as Record<string, unknown>).pywebview
})

describe('sessionToken', () => {
  it('init：pywebview 不存在（浏览器 dev）时保持 null', () => {
    initSessionToken()
    expect(getSessionToken()).toBeNull()
  })

  it('init：pywebview 已就绪时立即取回 token', async () => {
    const get_session_token = vi.fn(async () => 'tok-now')
    ;(window as unknown as Record<string, unknown>).pywebview = {
      api: { get_session_token },
    }
    initSessionToken()
    await vi.waitFor(() => expect(getSessionToken()).toBe('tok-now'))
  })

  it('pywebviewready 事件兜底：晚到的 token 也能取到', async () => {
    const get_session_token = vi.fn(async () => 'tok-late')
    ;(window as unknown as Record<string, unknown>).pywebview = {
      api: { get_session_token },
    }
    window.dispatchEvent(new Event('pywebviewready'))
    await vi.waitFor(() => expect(getSessionToken()).toBe('tok-late'))
  })

  it('pywebview 存在但 js_api 抛错时静默保持 null（不阻塞请求）', async () => {
    const get_session_token = vi.fn(async () => {
      throw new Error('not ready')
    })
    ;(window as unknown as Record<string, unknown>).pywebview = {
      api: { get_session_token },
    }
    initSessionToken()
    await vi.waitFor(() => expect(get_session_token).toHaveBeenCalled())
    expect(getSessionToken()).toBeNull()
  })

  it('契约一致性：调用的 js_api 方法名与 desktop.py Api 定义同名', () => {
    expect(desktopSrc).toContain('def get_session_token(self)')
    const st = readFileSync(join(apiDir, 'sessionToken.ts'), 'utf-8')
    expect(st).toContain('get_session_token')
  })
})
