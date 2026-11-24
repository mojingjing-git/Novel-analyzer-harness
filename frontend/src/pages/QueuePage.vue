<script setup lang="ts">
import Icon from '../components/Icon.vue'
import { ref, computed, onMounted, onUnmounted, inject } from 'vue'
import { routerKey } from 'vue-router'
import LogConsole from '../components/LogConsole.vue'
import ChapterDetailPanel from '../components/ChapterDetailPanel.vue'
import BookSelector from '../components/BookSelector.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import RunDashboard, { type DiscoveryItem, type ActiveBlock, type FinishedBlock } from '../components/RunDashboard.vue'
import { applySummaryProgress } from '../utils/summaryLanes'
import { phaseLabelOf } from '../utils/summaryProgress'
import { gcActiveBlocks, reconcileActiveBlocks } from '../utils/laneRegistry'
import {
  applyBlockStart, applyBlockDone, upsertTokenDeltaBuffer, sampleRateHistory, appendDiscovery,
  aggregateRate as computeAggregateRate, summarizeSession, resolveDeleteIndex,
  pickRunningBlockSize, RATE_HISTORY_MAX, type BlockTokenInfo,
} from '../utils/queueFeed'
import { api, type AnalysisStatus, type SessionTokenStatsResponse } from '../api/client'
import { useProgressSocket, type ProgressMessage } from '../api/useProgressSocket'
import { useLogStore } from '../composables/useLogStore'

const router = inject(routerKey, null)

const status = ref<AnalysisStatus | null>(null)
const progress = ref({ current: 0, total: 0, eta: '' })
const sessionStats = ref<SessionTokenStatsResponse | null>(null)
const busy = ref(false)
const error = ref('')
const canStart = computed(() => !busy.value && !status.value?.running &&
  Boolean(status.value?.items.some(item => item.status === 'pending' || item.status === 'running')))
let analysisStartMessages: ProgressMessage[] | null = null
let disposed = false

// H17 (2026-08-26) 实时仪表盘：活跃块 / 已完成块 / 发现流
const activeBlocks = ref<Map<number, ActiveBlock>>(new Map())
const finishedBlocks = ref<Map<number, FinishedBlock>>(new Map())
const discoveries = ref<DiscoveryItem[]>([])
let discoveryId = 1
// 块级 token 累计（用于车道进度条 + 卡死预警）+ 60 秒 1s 平滑吞吐历史
const tokenByBlock = ref<Map<number, BlockTokenInfo>>(new Map())
// per-block 最新累计均速（key=blockId），用于汇总活跃 block 的系统总吞吐；
// block_done 时清理对应 blockId，避免把已结束 block 算进当前总速率。
const rateByBlock = ref<Map<number, number>>(new Map())
const rateHistory = ref<number[]>([])
// 非响应式缓冲：高频 token_delta 先写入普通 Map，仅首帧/满 1 秒由采样定时器统一提交响应式快照，降低大并发渲染开销
const pendingTokenByBlock = new Map<number, BlockTokenInfo>()
const pendingRateByBlock = new Map<number, number>()
let hasPendingTokenFlush = false
let lastTokenFlushAt = 0
let rateSampleTimer: ReturnType<typeof setInterval> | null = null

function flushTokenBuffer(nowMs: number) {
  if (!hasPendingTokenFlush) return
  tokenByBlock.value = new Map(pendingTokenByBlock)
  rateByBlock.value = new Map(pendingRateByBlock)
  hasPendingTokenFlush = false
  lastTokenFlushAt = nowMs
}

// 活跃 block 聚合速率（tok/s）= sum(活跃 block 的 rateByBlock)
const aggregateRate = computed(() => computeAggregateRate(rateByBlock.value, activeBlocks.value))
// 章节详情共享的书目与章节号（从独立的工具栏提升到此处，便于两个分栏内容框对齐）
const detailBookId = ref('')
const detailChapter = ref(0)
let pollTimer: ReturnType<typeof setInterval> | null = null

const { logs: storeLogs, add, clear } = useLogStore()
const logs = computed(() => storeLogs.value)
// 简化日志（过滤 debug + Python 技术日志 + 逐章块事件）的条数，用于标题展示；
// 与 LogConsole simplified 的过滤规则保持一致，避免"标题计数与实际显示不符"
const logsSimplifiedCount = computed(() => logs.value.filter((l) => l.kind !== 'debug' && l.source !== 'python' && l.source !== 'block').length)

// 最终总结进度反馈（后端通过 WS 广播 summary_progress，此前 QueuePage 完全忽略）
const summaryHint = ref('')
let summaryHideTimer: ReturnType<typeof setTimeout> | null = null
function showSummaryHint(text: string) {
  summaryHint.value = text
  if (summaryHideTimer) clearTimeout(summaryHideTimer)
  summaryHideTimer = setTimeout(() => { summaryHint.value = '' }, 10000)
}

// F-4：WS 高频事件（block_done/token_stats 每块 1 次）合并为防抖刷新，
// 否则大书分析期间每块触发 2 次全量 REST 请求（2000 块 = 4000 请求）
// P2：按回调分键防抖——共享单 timer 时 token_stats 会吞掉先排队的 refresh
const pendingRefreshTimers = new Map<() => Promise<void>, ReturnType<typeof setTimeout>>()
function scheduleRefresh(fn: () => Promise<void>) {
  const prev = pendingRefreshTimers.get(fn)
  if (prev) clearTimeout(prev)
  pendingRefreshTimers.set(fn, setTimeout(() => {
    pendingRefreshTimers.delete(fn)
    void fn()
  }, 500))
}

function onMessage(msg: ProgressMessage) {
  if (analysisStartMessages !== null && (
    msg.type === 'progress' || msg.type === 'block_start' || msg.type === 'block_done' ||
    (msg.type === 'token_delta' && msg.payload.context === 'analysis')
  )) {
    analysisStartMessages.push(msg)
    return
  }

  switch (msg.type) {
    case 'progress':
      progress.value = {
        current: msg.payload.current,
        total: msg.payload.total,
        eta: msg.payload.eta || '',
      }
      break
    case 'block_start': {
      // 块开始 → 活跃块登记（状态机本体抽到 utils/queueFeed.ts，可独立 vitest）
      activeBlocks.value = applyBlockStart(activeBlocks.value, msg.payload, Date.now())
      break
    }
    case 'block_done': {
      // 块完成 → 移出活跃 + 清理速率与非响应式缓冲 + 登记 finished（FIFO 上限在 queueFeed 内）
      const doneBlockId = Number(msg.payload.chapter ?? 0)
      if (doneBlockId > 0) {
        pendingTokenByBlock.delete(doneBlockId)
        pendingRateByBlock.delete(doneBlockId)
      }
      const r = applyBlockDone(activeBlocks.value, finishedBlocks.value, rateByBlock.value, msg.payload)
      activeBlocks.value = r.active
      finishedBlocks.value = r.finished
      rateByBlock.value = r.rateByBlock
      if (doneBlockId > 0) {
        const nextTokens = new Map(tokenByBlock.value)
        nextTokens.delete(doneBlockId)
        tokenByBlock.value = nextTokens
      }
      if (r.active.size === 0) {
        hasPendingTokenFlush = false
        lastTokenFlushAt = Date.now()
      }
      scheduleRefresh(refresh)
      break
    }
    case 'discovery': {
      // 发现流 append-only（200 条上限在 queueFeed 内）
      const r = appendDiscovery(discoveries.value, msg.payload, discoveryId, Date.now())
      discoveries.value = r.items
      discoveryId = r.nextId
      break
    }
    case 'token_delta': {
      // 实时 token 增量先入普通 Map 缓冲；仅首帧或间隔满 1 秒时由采样循环统一提交响应式状态
      const now = Date.now()
      upsertTokenDeltaBuffer(pendingTokenByBlock, pendingRateByBlock, msg.payload, now)
      hasPendingTokenFlush = true
      if (now - lastTokenFlushAt >= 1000) {
        flushTokenBuffer(now)
      }
      break
    }
    case 'state_change': scheduleRefresh(refresh); break
    case 'token_stats': scheduleRefresh(refreshSessionStats); break
    case 'summary_progress': {
      const p = msg.payload
      const phase = String(p.phase ?? '')
      const phLabel = phaseLabelOf(phase)
      const bd = Number(p.batches_done ?? 0)
      const tb = Number(p.total_batches ?? 0)
      const extra = p.message ? `（${String(p.message)}）` : ''
      showSummaryHint(`最终总结进行中${phLabel ? '：' + phLabel : ''} · 卷${bd}/${tb}${extra} — 详见「最终总结」页`)

      // 总结车道状态维护（支持 lane_start/lane_done 与旧版合成回退）
      const result = applySummaryProgress(p, activeBlocks.value, finishedBlocks.value)
      if (result.changed) {
        activeBlocks.value = result.newActive
        finishedBlocks.value = result.newFinished
      }
      const ptype = String(p.type ?? '')
      const doneLaneId = Number(p.lane_id ?? 0)
      if (ptype === 'lane_done' && doneLaneId > 0) {
        pendingTokenByBlock.delete(doneLaneId)
        pendingRateByBlock.delete(doneLaneId)
        if (tokenByBlock.value.has(doneLaneId)) {
          const nextTk = new Map(tokenByBlock.value)
          nextTk.delete(doneLaneId)
          tokenByBlock.value = nextTk
        }
        if (rateByBlock.value.has(doneLaneId)) {
          const nextRt = new Map(rateByBlock.value)
          nextRt.delete(doneLaneId)
          rateByBlock.value = nextRt
        }
      } else if (ptype === 'complete' || (ptype === 'status' && typeof p.message === 'string' && p.message.includes('已停止'))) {
        let tkChanged = false
        const nextTk = new Map(tokenByBlock.value)
        const nextRt = new Map(rateByBlock.value)
        for (const k of [...pendingTokenByBlock.keys()]) if (k >= 900000) pendingTokenByBlock.delete(k)
        for (const k of [...pendingRateByBlock.keys()]) if (k >= 900000) pendingRateByBlock.delete(k)
        for (const k of [...nextTk.keys()]) if (k >= 900000) { nextTk.delete(k); tkChanged = true }
        for (const k of [...nextRt.keys()]) if (k >= 900000) { nextRt.delete(k); tkChanged = true }
        if (tkChanged) {
          tokenByBlock.value = nextTk
          rateByBlock.value = nextRt
        }
      }
      break
    }
  }
}

const { connected } = useProgressSocket(onMessage)

// 车道注册表自愈（车道堆叠修复）：随 refresh()（5s 轮询 + block_done 防抖）执行
// 1) GC：elapsed 超预警窗且无 token 更新的僵尸车道回收（WS 丢 done 的兜底），
//    并同步清理 tokenByBlock/rateByBlock 与非响应式缓冲
// 2) 对账：用后端 inflight_blocks（信号量真实在途）整体纠偏分析块，
//    保留总结合成车道（≥900000）与 5s 内新登记块（防 fetch/start 竞态闪烁）
function syncLaneRegistry() {
  if (!status.value?.running) return
  const now = Date.now()
  const { kept, gcIds } = gcActiveBlocks(
    activeBlocks.value, tokenByBlock.value, now, STALL_WARN_SEC)
  if (gcIds.length > 0) {
    for (const id of gcIds) {
      tokenByBlock.value.delete(id)
      rateByBlock.value.delete(id)
      pendingTokenByBlock.delete(id)
      pendingRateByBlock.delete(id)
    }
    tokenByBlock.value = new Map(tokenByBlock.value)
    rateByBlock.value = new Map(rateByBlock.value)
    console.warn('[车道GC] 回收僵尸车道:', gcIds)
  }
  const inflight = status.value?.inflight_blocks
  if (inflight) {
    activeBlocks.value = reconcileActiveBlocks(
      kept, inflight, Date.now(), 5000, currentConcurrency.value)
  } else if (gcIds.length > 0) {
    activeBlocks.value = kept
  }
}

async function refresh() {
  try {
    const s = await api.analysisStatus()
    if (disposed) return
    status.value = s
    syncLaneRegistry()
    // 刷新/重连时 WS 进度可能尚未到达，用 REST 状态兜底填充进度条，
    // 避免运行中刷新页面后进度条凭空消失（等待下一个 block_done 才出现）
    if (s?.running && progress.value.total === 0 && s.queue?.current_progress != null && s.queue.current_total) {
      progress.value = { current: s.queue.current_progress, total: s.queue.current_total, eta: '' }
    }
    await refreshSessionStats()
  } catch (e) { console.error(e) }
}
// 本次窗口会话累计（分析+总结，内存态跨书累计；重启清零）
async function refreshSessionStats() { try { sessionStats.value = await api.getSessionTokenStats() } catch (e) { console.error(e) } }
// 本次窗口会话的总量与命中率（命中率只算分析侧）——计算本体在 utils/queueFeed.summarizeSession
const sessionTotal = computed(() => summarizeSession(sessionStats.value))

// 费用 chip：models.dev 挂牌价估算（后端算好）。未定价模型在 tooltip 里点名，
// 避免"少算了但看起来算全了"的错觉
const sessionCostTooltip = computed(() => {
  const cost = sessionStats.value?.cost
  if (!cost) return ''
  const base = '费用 = 按 models.dev 挂牌价估算（输入/输出/缓存分别计价）；'
    + '网关订阅或官方账单的实付可能与此不同'
  const ver = cost.catalog_version ? `；牌价目录 ${cost.catalog_version}` : ''
  const un = cost.unpriced?.length ? `；未计价模型（牌价库查不到，不计入）：${cost.unpriced.join('、')}` : ''
  return base + ver + un
})

const showLog = ref(false)
const dashboardSessionMeta = computed(() => {
  if (!sessionStats.value) return null
  return {
    cached: sessionTotal.value.cached,
    hitRate: sessionTotal.value.hitRate,
    total: sessionTotal.value.total,
    cost: sessionTotal.value.cost,
    costTooltip: sessionCostTooltip.value,
  }
})

async function withBusy(fn: () => Promise<void>, label: string) {
  busy.value = true; error.value = ''
  try {
    await fn()
    if (!disposed) await refresh()
  } catch (e) {
    if (disposed) return
    error.value = `${label}: ${(e as Error).message}`
    add(error.value, 'error', 'analysis')
  } finally {
    if (!disposed) busy.value = false
  }
}

function handleStart() {
  if (!canStart.value) return
  void withBusy(async () => {
    const messages: ProgressMessage[] = []
    analysisStartMessages = messages
    try {
      await api.startAnalysis()
      if (analysisStartMessages !== messages) return
    } finally {
      analysisStartMessages = null
    }

    // 接受启动后重置统计，再应用先于 HTTP 响应到达的进度消息。
    progress.value = { current: 0, total: 0, eta: '' }
    pendingTokenByBlock.clear()
    pendingRateByBlock.clear()
    hasPendingTokenFlush = false
    lastTokenFlushAt = 0
    tokenByBlock.value = new Map()
    rateByBlock.value = new Map()
    rateHistory.value = []
    for (const message of messages) onMessage(message)
    add('分析已启动', 'info', 'analysis')
  }, '启动分析')
}
function handleStop() { withBusy(async () => { await api.stopAnalysis(); add('分析已停止', 'warn', 'analysis') }, '停止分析') }

// PR-1 收尾（2026-09-30）：日志端点受 session token 保护且返回 text/plain，
// <a href> 顶层导航带不了 header 也渲染不了下载，改走 api.downloadLogs()（fetch + blob）
async function handleDownloadLogs() {
  try {
    await api.downloadLogs()
    add('日志已开始下载', 'info', 'analysis')
  } catch (e) {
    error.value = `下载日志: ${(e as Error).message}`
    add(error.value, 'error', 'analysis')
  }
}
function handleScanWorkspace() { withBusy(async () => { const res = await api.scanWorkspace(); add(`扫描完成，新增 ${res.added} 本小说`, 'info', 'analysis') }, '扫描工作区') }
function handleRemove(index: number) { withBusy(async () => { await api.removeQueueItem(index); add(`已移出队列项 #${index}`, 'info', 'analysis') }, '移出队列') }
function handleMoveUp(index: number) { withBusy(async () => { await api.moveQueueItemUp(index) }, '上移') }
function handleMoveDown(index: number) { withBusy(async () => { await api.moveQueueItemDown(index) }, '下移') }
function handleReset(index: number) { withBusy(async () => { await api.resetQueueItem(index); add(`已重置队列项 #${index} 为待处理`, 'info', 'analysis') }, '重跑') }
function handleClear() { withBusy(async () => { await api.clearQueue(); add('队列已清空', 'info', 'analysis') }, '清空队列') }

// 删除确认：用玻璃弹窗替代浏览器原生 confirm()
function handleDelete(index: number) {
  const it = status.value?.items?.[index]
  if (!it) return
  deleteTargetName.value = it.name
  deleteTarget.value = index   // 仅作弹窗开关信号，确认时以名称实时解析索引
}

const deleteTarget = ref<number | null>(null)
const deleteTargetName = ref('')

function confirmDelete() {
  const name = deleteTargetName.value
  deleteTarget.value = null
  if (!name) return
  withBusy(async () => {
    // P2：以名称实时解析索引，避免轮询/WS 重排后按旧索引删错书（解析逻辑在 utils/queueFeed）
    const idx = resolveDeleteIndex(status.value?.items, name)
    if (idx < 0) { add(`未找到《${name}》，可能已被移除`, 'warn', 'analysis'); return }
    await api.deleteBook(idx)
    add(`已删除《${name}》并移入回收站`, 'warn', 'analysis')
  }, '删除小说')
}

const statusBadgeClass: Record<string, string> = {
  pending: 'badge-gray', running: 'badge-blue', done: 'badge-green',
  failed: 'badge-red', skipped: 'badge-gray',
}

const statusLabels: Record<string, string> = {
  pending: '等待中', running: '运行中', done: '已完成', failed: '失败', skipped: '已跳过',
}

// 进度换算取正在运行那一项的 block_size（多书混排时首项未必是运行中）——逻辑在 utils/queueFeed
const currentBlockSize = computed(() => pickRunningBlockSize(Boolean(status.value?.running), status.value?.items))

// H17 P3 V2 修复（2026-08-26）：并发块数（不是每块几章！）
// 修复前 RunDashboard 接收的是 currentBlockSize，导致「并发车道 N/4」实际是 N/每块4章
// 配置 concurrency=8 时后端 8 路并发但 UI 标 4。后端 status 已加 concurrency 字段
const currentConcurrency = computed(() => status.value?.concurrency || 1)

// H17 (2026-08-26) 左面板 tab：默认「运行概览」（H17 Phase 3 决策：分析器进度信息优先）
// 用户手动切换后本会话记忆（与 plan 决策 2 一致）
const currentTab = ref<'overview' | 'detail'>('overview')
const userSwitchedTab = ref(false)
function switchTab(tab: 'overview' | 'detail') {
  currentTab.value = tab
  userSwitchedTab.value = true
}

function onSelectLaneBlock(blockId: number) {
  detailChapter.value = blockId
  switchTab('detail')
}

// 分栏高度动态自适应与手动拉伸手柄
const splitGridRef = ref<HTMLElement | null>(null)
const customSplitHeight = ref<number | null>((() => {
  try {
    if (typeof localStorage !== 'undefined' && typeof localStorage.getItem === 'function') {
      const saved = localStorage.getItem('novel_analyzer_queue_split_height')
      if (saved) {
        const val = Number(saved)
        if (!isNaN(val) && val >= 520) return val
      }
    }
  } catch {
    // 忽略受限环境下的存储读取异常
  }
  return null
})())
let isSplitResizing = false
let splitStartY = 0
let splitStartHeight = 0

function onResizeBarPointerDown(e: PointerEvent) {
  if (e.button !== 0 || !splitGridRef.value) return
  const bar = e.currentTarget as HTMLElement
  bar.setPointerCapture(e.pointerId)
  isSplitResizing = false
  splitStartY = e.clientY
  splitStartHeight = splitGridRef.value.getBoundingClientRect().height

  const onPointerMove = (ev: PointerEvent) => {
    const deltaY = ev.clientY - splitStartY
    if (!isSplitResizing && Math.abs(deltaY) > 3) {
      isSplitResizing = true
    }
    if (isSplitResizing) {
      const newHeight = Math.max(520, Math.min(window.innerHeight * 0.9, splitStartHeight + deltaY))
      customSplitHeight.value = Math.round(newHeight)
    }
  }

  const onPointerUp = (ev: PointerEvent) => {
    bar.releasePointerCapture(ev.pointerId)
    bar.removeEventListener('pointermove', onPointerMove)
    bar.removeEventListener('pointerup', onPointerUp)
    bar.removeEventListener('pointercancel', onPointerUp)
    if (customSplitHeight.value) {
      localStorage.setItem('novel_analyzer_queue_split_height', String(customSplitHeight.value))
    }
    isSplitResizing = false
  }

  bar.addEventListener('pointermove', onPointerMove)
  bar.addEventListener('pointerup', onPointerUp)
  bar.addEventListener('pointercancel', onPointerUp)
}

function onResetSplitHeight() {
  customSplitHeight.value = null
  localStorage.removeItem('novel_analyzer_queue_split_height')
}

const STALL_WARN_SEC = 480

// 设置项 tooltip 字典：只覆盖"非一眼能看出"的字段；状态/操作按钮/进度文字不加
const tooltips: Record<string, string> = {
  // 工具栏
  scan_workspace: '扫描工作区，将已切分的小说加入队列',
  download_logs: '下载完整运行日志（含调试信息与 Python 技术日志）',
  // 章节输入
  chapter_input: '0 或留空 = 自动跟踪最新章节；输入数字 = 跳到指定章节',
  // Session token 累计 5 项
  session_input: '本次窗口会话累计输入 tokens = 系统提示 + 用户消息总和；分析 + 总结两段',
  session_output: '本次窗口会话累计输出 tokens = LLM 实际生成的内容',
  session_cached: '本次窗口会话累计 KV cache 命中 tokens（prompt 重复片段命中部分不计费但占带宽）',
  session_hit_rate: '命中率 = 命中缓存 ÷ (输入 + 命中缓存)；仅统计分析侧（总结侧未记录缓存命中）',
  session_total: '总消耗 = 输入 + 输出 + 命中缓存；这是实际向 API 发送/接收的 token 总量',
}

onMounted(() => {
  refresh()
  pollTimer = setInterval(() => {
    void refresh()
  }, 5000)
  rateSampleTimer = setInterval(() => {
    const now = Date.now()
    flushTokenBuffer(now)
    const agg = computeAggregateRate(pendingRateByBlock, activeBlocks.value)
    if (activeBlocks.value.size > 0 || agg > 0) {
      rateHistory.value = sampleRateHistory(rateHistory.value, agg, 0.35, RATE_HISTORY_MAX)
    }
  }, 1000)
})

onUnmounted(() => {
  disposed = true
  analysisStartMessages = null
  if (pollTimer) clearInterval(pollTimer)
  if (rateSampleTimer) clearInterval(rateSampleTimer)
  if (summaryHideTimer) clearTimeout(summaryHideTimer)
  for (const t of pendingRefreshTimers.values()) clearTimeout(t)
  pendingRefreshTimers.clear()
})
</script>

<template>
  <div class="queue-page space-y-6">
    <div class="flex items-center justify-between gap-3 flex-wrap">
      <div>
        <h2 class="section-title">分析队列</h2>
      </div>
      <div class="flex gap-2">
        <button @click="handleScanWorkspace" :disabled="busy || status?.running" class="glass-button" v-tooltip="tooltips.scan_workspace">扫描工作区</button>
        <button v-if="!status?.running" @click="handleStart" :disabled="!canStart" class="glass-button glass-button-primary"><Icon name="play" :size="12" /> 开始分析</button>
        <button v-else @click="handleStop" :disabled="busy" class="glass-button glass-button-danger"><Icon name="stop" :size="12" /> 停止</button>
      </div>
    </div>

    <div v-if="error" class="glass-tinted-red px-4 py-2 rounded text-sm">{{ error }}</div>

    <div v-if="summaryHint" class="glass-tinted-blue px-4 py-2 rounded text-sm">
      {{ summaryHint }}
    </div>

    <div class="glass-card overflow-x-auto">
      <table class="glass-table">
        <thead>
          <tr>
            <th>书名</th>
            <th style="width: 96px">状态</th>
            <th style="width: 136px">进度</th>
            <th style="width: 210px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!status?.items?.length">
            <td colspan="4">
              <div class="empty-state">
                <span class="empty-icon"><Icon name="queue" :size="24" /></span>
                <span>{{ status ? '队列为空。切分小说后，扫描工作区即可加入队列。' : '尚未读取到队列状态' }}</span>
              </div>
            </td>
          </tr>
          <tr v-for="(item, idx) in status?.items || []" :key="idx">
            <td style="font-weight: 500">{{ item.name }}</td>
            <td>
              <span class="glass-badge" :class="statusBadgeClass[item.status] || 'badge-gray'">
                {{ statusLabels[item.status] || item.status }}
              </span>
            </td>
            <td style="color: var(--win-text-secondary)">
              <template v-if="item.block_size > 1">
                {{ item.completed_chapters }}/{{ Math.ceil(item.total_chapters / item.block_size) }} 块
                <span style="font-size: 11px; margin-left: 4px">({{ item.completed_chapters * item.block_size }}/{{ item.total_chapters }} 章)</span>
              </template>
              <template v-else>
                {{ item.completed_chapters }}/{{ item.total_chapters }}
                <span style="font-size: 11px; margin-left: 4px">章</span>
              </template>
            </td>
            <td>
              <div class="ops-row">
                <button
                  :class="['glass-button op-btn op-icon', { 'op-hidden': !(idx > 0 && !status?.running) }]"
                  @click="handleMoveUp(idx)"
                  :disabled="busy || status?.running || idx === 0"
                >
                  <Icon name="arrow_up" :size="12" />
                </button>
                <button
                  :class="['glass-button op-btn op-icon', { 'op-hidden': !(idx < (status?.items.length || 0) - 1 && !status?.running) }]"
                  @click="handleMoveDown(idx)"
                  :disabled="busy || status?.running || idx === (status?.items.length || 0) - 1"
                >
                  <Icon name="arrow_down" :size="12" />
                </button>
                <button
                  :class="['glass-button op-btn', { 'op-hidden': !(['done', 'failed', 'skipped'].includes(item.status) && !status?.running) }]"
                  @click="handleReset(idx)"
                  :disabled="busy || status?.running || !['done', 'failed', 'skipped'].includes(item.status)"
                >
                  重跑
                </button>
                <button
                  v-if="!status?.running && item.status !== 'running'"
                  @click="handleRemove(idx)"
                  class="glass-button op-btn"
                  :disabled="busy"
                >
                  移出队列
                </button>
                <button
                  v-if="!status?.running && item.status !== 'running'"
                  @click="handleDelete(idx)"
                  class="glass-button op-btn op-danger"
                  :disabled="busy"
                >
                  删除
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div
      ref="splitGridRef"
      class="split-grid"
      :class="{ 'has-log': showLog }"
      :style="customSplitHeight ? { height: customSplitHeight + 'px' } : {}"
    >
      <!-- 主栏：tab 切换（运行概览 | 章节详情），窄窗全宽，宽窗占主空间 -->
      <div class="split-col">
        <div class="col-head">
          <div class="head-main">
            <div class="left-tabs" role="tablist">
              <button
                :class="['left-tab', currentTab === 'overview' ? 'left-tab-active' : '']"
                @click="switchTab('overview')"
                role="tab"
                type="button"
              >运行概览</button>
              <button
                :class="['left-tab', currentTab === 'detail' ? 'left-tab-active' : '']"
                @click="switchTab('detail')"
                role="tab"
                type="button"
              >章节详情</button>
            </div>
            <span class="col-sub2">
              <template v-if="currentTab === 'overview'">
                {{ status?.running ? `运行中 · 车道 ${activeBlocks.size}` : '空闲' }}
              </template>
              <template v-else>
                {{ detailChapter > 0 ? `正在查看 第 ${detailChapter} 章` : '自动显示最新章节' }}
              </template>
            </span>
          </div>
          <div class="col-actions">
            <div class="cd-controls" v-if="currentTab === 'detail'">
              <BookSelector v-model="detailBookId" :sync-global="false" class="cd-book" />
              <input
                v-model.number="detailChapter"
                type="number"
                min="0"
                placeholder="留空=最新"
                class="glass-input dt-input"
                v-tooltip="tooltips.chapter_input"
              />
            </div>
            <span class="conn-state" style="font-size: 11px">
              <span class="dot" :class="connected ? 'on' : 'off'"></span>
              {{ connected ? '已连接' : '未连接' }}
            </span>
            <button
              type="button"
              @click="router?.push({ path: '/stats', query: { tab: 'session' } })"
              class="glass-button btn-head-sm"
            >
              逐章统计
            </button>
            <button
              type="button"
              @click="showLog = !showLog"
              class="glass-button btn-head-sm"
              :class="{ 'is-active-toggle': showLog }"
            >
              实时日志 ({{ logsSimplifiedCount }})
            </button>
            <button
              type="button"
              class="glass-button btn-head-sm"
              v-tooltip="tooltips.download_logs"
              @click="handleDownloadLogs"
            >
              下载日志
            </button>
          </div>
        </div>
        <RunDashboard
          v-if="currentTab === 'overview'"
          :running="Boolean(status?.running)"
          :concurrency="currentConcurrency"
          :progress="progress"
          :block-size="currentBlockSize"
          :session-tokens="sessionTotal"
          :session-meta="dashboardSessionMeta"
          :active-blocks="activeBlocks"
          :finished-blocks="finishedBlocks"
          :token-by-block="tokenByBlock"
          :rate-history="rateHistory"
          :aggregate-rate="aggregateRate"
          :stall-warn-sec="STALL_WARN_SEC"
          :discoveries="discoveries"
          class="split-detail"
          @select-block="onSelectLaneBlock"
        />
        <ChapterDetailPanel
          v-else
          :book-id="detailBookId"
          v-model:chapter="detailChapter"
          class="split-detail"
        />
      </div>

      <!-- 按需展开栏：简化实时日志 -->
      <div v-if="showLog" class="split-col split-col-log">
        <div class="col-head">
          <div class="head-main">
            <span class="col-title">
              实时日志（简）
              <span class="col-sub">({{ logsSimplifiedCount }} 条)</span>
            </span>
          </div>
          <div class="flex items-center gap-2">
            <button @click="clear" class="glass-button btn-head-sm">清空</button>
            <button type="button" @click="showLog = false" class="glass-button btn-head-sm">收起</button>
          </div>
        </div>
        <div class="split-log-wrapper">
          <LogConsole :logs="logs" :simplified="true" class="split-log" />
        </div>
      </div>
    </div>

    <!-- 分栏垂直高度调整条（支持拖拽拉高 / 双击重置） -->
    <div
      class="split-resize-bar"
      @pointerdown="onResizeBarPointerDown"
      @dblclick="onResetSplitHeight"
      :title="customSplitHeight ? '拖动调节高度 / 双击恢复默认自适应' : '拖动调节高度'"
    >
      <div class="split-resize-handle">
        <span class="split-resize-pill"></span>
      </div>
    </div>

    <div v-if="status?.items?.length && !status?.running" class="flex justify-end">
      <button @click="handleClear" :disabled="busy" class="glass-button" style="color: var(--win-danger)">清空队列</button>
    </div>

    <!-- 删除确认（玻璃弹窗） -->
    <ConfirmDialog
      v-if="deleteTarget !== null"
      title="删除小说"
      :message="`确定要删除《${deleteTargetName}》吗？文件将被移入系统回收站。`"
      confirm-text="删除"
      :danger="true"
      @confirm="confirmDelete"
      @cancel="deleteTarget = null"
    />
  </div>
</template>

<style scoped>
.queue-page {
  container-type: inline-size;
  min-width: 0;
}

/* 左面板 tab 切换（运行概览 | 章节详情） */
.left-tabs {
  display: flex;
  gap: 4px;
}
.left-tab {
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 500;
  color: var(--win-text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s;
}
.left-tab:hover {
  background: var(--win-control-hover, rgba(255,255,255,0.04));
  color: var(--win-text-primary);
}
.left-tab.left-tab-active {
  color: var(--win-accent);
  background: var(--win-accent-soft);
  border-color: color-mix(in srgb, var(--win-accent) 30%, transparent);
}

.col-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.btn-head-sm {
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
}
.btn-head-sm.is-active-toggle {
  background: var(--win-accent-soft);
  border-color: var(--win-accent);
  color: var(--win-accent);
  font-weight: 500;
}

/* 章节选择控件 */
.cd-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.cd-book {
  width: 180px;
  min-width: 120px;
}
.dt-input {
  width: 96px;
  flex-shrink: 0;
}

/* 概览默认全宽；日志按需展开：窄容器上下堆叠，宽容器给概览更多空间 */
.split-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  min-height: 460px;
}
.split-col {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.split-log-wrapper {
  height: 320px;
  display: flex;
  flex-direction: column;
}
@container (min-width: 1040px) {
  .split-grid.has-log {
    grid-template-columns: minmax(0, 1.65fr) minmax(300px, 0.85fr);
    min-height: 520px;
  }
  .split-grid.has-log .split-col-log {
    position: relative;
  }
  .split-grid.has-log .split-log-wrapper {
    position: absolute;
    top: 66px;
    left: 0;
    right: 0;
    bottom: 0;
    height: auto;
  }
}

/* 垂直调整手柄条 */
.split-resize-bar {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 12px;
  margin: -6px 0 10px;
  cursor: ns-resize;
  user-select: none;
  touch-action: none;
}
.split-resize-handle {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
}
.split-resize-pill {
  width: 36px;
  height: 4px;
  border-radius: 2px;
  background: var(--win-stroke);
  transition: background var(--win-duration-fast) var(--win-ease), width var(--win-duration-fast) var(--win-ease);
}
.split-resize-bar:hover .split-resize-pill {
  background: var(--win-text-muted);
  width: 48px;
}
.split-resize-bar:active .split-resize-pill {
  background: var(--win-accent);
}
.col-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 12px;
  min-height: 56px;
  margin-bottom: 10px;
}
.head-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.col-title {
  font-size: 14px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--win-text-primary);
}
.col-sub {
  color: var(--win-text-secondary);
  font-weight: 400;
  margin-left: 6px;
  font-size: 12px;
}
.col-sub2 {
  font-size: 11px;
  font-weight: 400;
  line-height: 1.3;
  color: var(--win-text-muted);
}
/* 让日志区撑满所在列（覆盖 LogConsole 自带的 max-height:320px） */
.split-log {
  flex: 1;
  min-height: 0;
  max-height: none !important;
  height: 100%;
}
/* 章节详情白框撑满所在列 */
.split-detail {
  flex: 1;
  min-height: 0;
}

/* 操作列：上移/下移/重跑 使用占位隐藏，保证「移出队列」和「删除」始终对齐 */
.ops-row {
  display: flex;
  align-items: center;
  gap: 6px;
  justify-content: flex-end;
}
/* Win11 列表行内操作按钮：统一 32px 高 / 12px padding，与基类 .glass-button 等高 */
.op-btn {
  height: 32px;
  padding: 0 12px;
  font-size: 13px;
  white-space: nowrap;
}
.op-icon {
  height: 32px;
  padding: 0 8px;
  min-width: 32px;
}
.op-hidden {
  visibility: hidden;
  pointer-events: none;
}
.op-danger {
  color: var(--win-danger);
}
</style>
