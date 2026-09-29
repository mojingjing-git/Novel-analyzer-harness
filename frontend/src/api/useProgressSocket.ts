// WebSocket 进度推送 composable（单例模式）
// 全应用共享一个 WebSocket 连接，避免页面切换时断开重连丢失消息
import { ref, onUnmounted, type Ref } from 'vue'

// ============ WS 事件协议（与 backend/ws_events.py 镜像，wire 契约见该文件）============

export interface WSLogPayload {
  level: 'info' | 'warn' | 'error'
  text: string
  source: 'business' | 'python'
  category: string
}

export interface WSProgressPayload {
  current: number
  total: number
  eta: string
}

export interface WSBlockStartPayload {
  chapter: number   // block_id（块起始章号）
  range: string     // 后端保证为 chN-chM 结构化格式；message 仅供日志
  message?: string  // 旧后端无结构化 range 时的兜底
  progress: number
  total: number
  ts: number        // 服务端 epoch 秒
}

export interface WSBlockDonePayload {
  chapter: number
  ok: boolean
  range: string     // 后端保证为 chN-chM 结构化格式；message 仅供日志
  message?: string  // 旧后端无结构化 range 时的兜底
  elapsed?: number | null
  tokens?: number | number[] | null  // 续跑补发路径为 (0,0) 元组 → JSON 数组
}

export interface WSDiscoveryPayload {
  events: number
  foreshadows: string[]
  characters: string[]
  unresolved: string[]
}

export interface WSTokenStatsPayload {
  category: string
  input_tokens: number
  output_tokens: number
  current_tokens?: number
  source?: 'summary'
}

export interface WSTokenDeltaPayload {
  context: string
  session_id: string
  unit_idx: number
  delta: { output_tokens: number; rate_tokens_per_sec: number; elapsed_sec?: number }
  timestamp: number
}

export interface WSStateChangePayload {
  state: string  // 12 值全集见 backend/ws_events.py 的 WSState
  detail: string
}

/** 最终总结进度（final_summary._emit_progress 透传 + book_id/batches_done 注入）。
 *  子类型 status/phase/batch_done/batch_failed/ledger_updated/complete 字段互异，故全 optional */
export interface WSSummaryProgressPayload {
  type: string
  phase?: string
  phase_label?: string
  batch?: number
  total_batches?: number
  batches_done?: number
  elapsed?: number
  message?: string
  book_id?: string
  counts?: Record<string, number>
}

/** 地点归一化进度（后端有广播、前端当前以 REST 轮询为准，此类型备用） */
export interface WSLocationNormProgressPayload {
  type: string
  phase?: string
  batch_idx?: number
  error?: string
  message?: string
  book_id?: string
  batches_done?: number
}

export type ProgressMessage =
  | { type: 'log'; payload: WSLogPayload }
  | { type: 'progress'; payload: WSProgressPayload }
  | { type: 'block_start'; payload: WSBlockStartPayload }
  | { type: 'block_done'; payload: WSBlockDonePayload }
  | { type: 'discovery'; payload: WSDiscoveryPayload }
  | { type: 'token_stats'; payload: WSTokenStatsPayload }
  | { type: 'token_delta'; payload: WSTokenDeltaPayload }
  | { type: 'state_change'; payload: WSStateChangePayload }
  | { type: 'summary_progress'; payload: WSSummaryProgressPayload }
  | { type: 'location_normalization_progress'; payload: WSLocationNormProgressPayload }
  | { type: 'ping' }

export interface LogEntry {
  id: number
  text: string
  kind: 'info' | 'warn' | 'error' | 'state' | 'debug'
  category?: string  // analysis / style / splitter / aggregate / summary ...
  source?: string    // python=Python logging转发的技术日志 / business=业务消息 / block=逐章完成事件 / local=前端操作 / event=状态事件
}

// ============ 单例状态 ============
let sharedWs: WebSocket | null = null
const sharedConnected = ref(false)
const allSubscribers = new Set<(msg: ProgressMessage) => void>()
let retry = 0
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let isInitialized = false
let manualClose = false  // 主动关闭标记：避免 closeProgressSocket 后 onclose 仍触发重连产生孤儿 WS

function connect() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = `${proto}//${window.location.host}/ws/progress`
  try {
    sharedWs = new WebSocket(url)
  } catch (e) {
    console.error('[WS] 创建连接失败:', e)
    scheduleReconnect()
    return
  }

  sharedWs.onopen = () => {
    sharedConnected.value = true
    retry = 0
    console.log('[WS] 已连接')
  }

  sharedWs.onmessage = (event) => {
    try {
      const msg: ProgressMessage = JSON.parse(event.data)
      if (msg.type !== 'ping') {
        // 分发给所有订阅者
        for (const sub of allSubscribers) {
          try {
            sub(msg)
          } catch (e) {
            console.error('[WS] 订阅者处理失败:', e)
          }
        }
      }
    } catch (e) {
      console.error('[WS] 解析消息失败:', e)
    }
  }

  sharedWs.onclose = () => {
    sharedConnected.value = false
    if (manualClose) {
      // 主动关闭（closeProgressSocket）后不再重连，否则会生成无人能清理的孤儿 WS
      manualClose = false
      console.log('[WS] 连接已主动关闭')
      return
    }
    console.log('[WS] 连接断开，重试中...')
    scheduleReconnect()
  }

  sharedWs.onerror = () => {
    sharedWs?.close()
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return  // 已有重连任务
  const delay = Math.min(1000 * Math.pow(2, retry), 10000)
  retry++
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    connect()
  }, delay)
}

/** 初始化全局 WS（在 App 启动时调用一次） */
export function initProgressSocket() {
  if (!isInitialized) {
    isInitialized = true
    connect()
  }
}

export function useProgressSocket(onMessage: (msg: ProgressMessage) => void): { connected: Ref<boolean> } {
  // 首次调用时初始化 WS
  if (!isInitialized) {
    initProgressSocket()
  }
  // 注册订阅者（页面卸载时移除）
  allSubscribers.add(onMessage)
  onUnmounted(() => {
    allSubscribers.delete(onMessage)
  })
  return { connected: sharedConnected }
}

/** 关闭 WS（测试或卸载时使用） */
export function closeProgressSocket() {
  manualClose = true
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (sharedWs) {
    sharedWs.close()
    sharedWs = null
  }
  sharedConnected.value = false
  isInitialized = false
}
