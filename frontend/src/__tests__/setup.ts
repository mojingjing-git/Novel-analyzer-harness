// Node 26 内置的试验性 localStorage（未提供 --localstorage-file 时 getItem 等方法为 undefined）
// 会覆盖 jsdom 的 window.localStorage；此处在缺失或不可用时补齐轻量内存 Storage 实现。
function createMemoryStorage(): Storage {
  let store: Record<string, string> = {}
  return {
    get length() {
      return Object.keys(store).length
    },
    key(index: number): string | null {
      const keys = Object.keys(store)
      return index >= 0 && index < keys.length ? keys[index] : null
    },
    getItem(key: string): string | null {
      return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null
    },
    setItem(key: string, value: string): void {
      store[String(key)] = String(value)
    },
    removeItem(key: string): void {
      delete store[key]
    },
    clear(): void {
      store = {}
    },
  }
}

function ensureStorage(name: 'localStorage' | 'sessionStorage') {
  const win = typeof window !== 'undefined' ? window : undefined
  const current = win ? (win as unknown as Record<string, unknown>)[name] : (globalThis as unknown as Record<string, unknown>)[name]
  const valid = Boolean(
    current &&
      typeof (current as Storage).getItem === 'function' &&
      typeof (current as Storage).setItem === 'function' &&
      typeof (current as Storage).clear === 'function',
  )
  if (!valid) {
    const impl = createMemoryStorage()
    if (win) {
      Object.defineProperty(win, name, { value: impl, configurable: true, writable: true })
    }
    Object.defineProperty(globalThis, name, { value: impl, configurable: true, writable: true })
  }
}

ensureStorage('localStorage')
ensureStorage('sessionStorage')
