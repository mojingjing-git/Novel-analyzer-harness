import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import AppLayout from './components/AppLayout.vue'
import QueuePage from './pages/QueuePage.vue'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    component: AppLayout,
    children: [
      { path: '', component: QueuePage },
      { path: 'settings', component: () => import('./pages/SettingsPage.vue') },
      { path: 'characters', name: 'characters', component: () => import('./pages/CharacterCardPage.vue') },
      { path: 'summary', component: () => import('./pages/SummaryPage.vue') },
      { path: 'splitter', component: () => import('./pages/SplitterPage.vue') },
      { path: 'workspace', component: () => import('./pages/WorkspacePage.vue') },
      { path: 'world', component: () => import('./pages/WorldStagePage.vue') },
      { path: 'stats', component: () => import('./pages/StatsPage.vue') },
      { path: 'timeline', component: () => import('./pages/TimelinePage.vue') },
      { path: 'graph', redirect: to => ({ path: '/characters', query: to.query }) },
      { path: 'map', redirect: to => ({ path: '/world', query: to.query }) },
      { path: 'style', redirect: to => ({ path: '/summary', query: to.query }) },
      { path: 'prompt', redirect: to => ({ path: '/stats', query: { ...to.query, tab: 'prompt' } }) },
    ],
  },
]

export const CHUNK_RELOAD_KEY = 'novel-analyzer-chunk-reload'
export const CHUNK_RELOAD_GUARD_MS = 10_000

const CHUNK_ERROR_PATTERNS = [
  /failed to fetch dynamically imported module/i,
  /importing a module script failed/i,
  /error loading dynamically imported module/i,
  /unable to preload css/i,
  /chunkloaderror/i,
]

export function isChunkLoadError(error: unknown): boolean {
  if (!error) return false
  const name = typeof error === 'object' ? String((error as { name?: unknown }).name ?? '') : ''
  if (/chunkloaderror/i.test(name)) return true
  const msg = error instanceof Error ? error.message : String(error)
  return CHUNK_ERROR_PATTERNS.some((re) => re.test(msg))
}

export interface ChunkRecoverOptions {
  storage?: Pick<Storage, 'getItem' | 'setItem'> | null
  navigate?: (url: string) => void
  now?: number
}

export function recoverFromChunkError(
  error: unknown,
  targetPath: string,
  options: ChunkRecoverOptions = {},
): boolean {
  if (!isChunkLoadError(error) || !targetPath) return false
  const now = options.now ?? Date.now()
  const storage = options.storage !== undefined
    ? options.storage
    : (typeof window !== 'undefined' ? window.sessionStorage : null)

  try {
    const raw = storage?.getItem(CHUNK_RELOAD_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as { path?: string; ts?: number }
      if (parsed.path === targetPath && typeof parsed.ts === 'number' && now - parsed.ts < CHUNK_RELOAD_GUARD_MS) {
        return false
      }
    }
    storage?.setItem(CHUNK_RELOAD_KEY, JSON.stringify({ path: targetPath, ts: now }))
  } catch {
    // 忽略受限环境下的 sessionStorage 异常
  }

  const navigate = options.navigate ?? ((url: string) => {
    if (typeof window !== 'undefined') window.location.assign(url)
  })
  navigate(targetPath)
  return true
}

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.onError((error, to) => {
  const fallbackPath = typeof window !== 'undefined'
    ? `${window.location.pathname}${window.location.search}${window.location.hash}`
    : '/'
  recoverFromChunkError(error, to?.fullPath || fallbackPath)
})
