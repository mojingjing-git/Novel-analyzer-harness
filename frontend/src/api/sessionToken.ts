/**
 * 会话 token 单例（PR-1 收尾，2026-09-30）
 *
 * desktop.py 启动时生成 token 并设 NOVEL_ANALYZER_SESSION_TOKEN 环境变量；
 * 后端 require_session_token 校验请求的 X-Session-Token header。token 经
 * pywebview js_api（Api.get_session_token）传入，仅存模块内存——不进
 * localStorage、不打印日志。浏览器 dev 模式无 pywebview，保持 null，
 * client.ts 据此不附加 header（后端 env 未设时本就放行）。
 *
 * 就绪时机双保险：client.ts 模块加载时调一次 initSessionToken()；
 * pywebviewready 事件（每次页面加载会重发）再兜一次。
 */

let token: string | null = null

export function getSessionToken(): string | null {
  return token
}

/** 仅供测试注入（生产路径只有 initSessionToken 会写） */
export function __setSessionTokenForTests(t: string | null): void {
  token = t
}

function fetchFromPywebview(): void {
  const wv = (
    window as unknown as {
      pywebview?: { api?: { get_session_token?: () => Promise<unknown> } }
    }
  ).pywebview?.api
  if (!wv?.get_session_token) return
  wv
    .get_session_token()
    .then((t) => {
      if (typeof t === 'string' && t) token = t
    })
    .catch(() => {
      /* js_api 未就绪 / 浏览器环境：保持 null */
    })
}

/** client.ts 模块加载时调用；pywebview 已就绪则立即取到，否则由下方事件兜底 */
export function initSessionToken(): void {
  fetchFromPywebview()
}

if (typeof window !== 'undefined') {
  window.addEventListener('pywebviewready', fetchFromPywebview, { once: true })
}
