// 全局选中书目状态（跨页面持久化到 localStorage）
import { ref, type Ref } from 'vue'

const STORAGE_KEY = 'novel-analyzer-selected-book'

let selectedBookRef: Ref<string> | null = null

function readStorage(): string {
  try {
    return window.localStorage?.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

function writeStorage(id: string): void {
  try {
    if (!window.localStorage) return
    if (id) window.localStorage.setItem(STORAGE_KEY, id)
    else window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // 忽略无 localStorage 环境的写入失败
  }
}

export function useSelectedBook(): {
  selectedBook: Ref<string>
  setSelectedBook: (id: string) => void
} {
  if (!selectedBookRef) {
    selectedBookRef = ref(readStorage())
  }

  function setSelectedBook(id: string): void {
    if (!selectedBookRef) {
      selectedBookRef = ref('')
    }
    selectedBookRef.value = id
    writeStorage(id)
  }

  return { selectedBook: selectedBookRef, setSelectedBook }
}

/** 仅限单元测试清理模块级单例与 localStorage */
export function resetSelectedBookForTest(): void {
  selectedBookRef = null
  writeStorage('')
}
