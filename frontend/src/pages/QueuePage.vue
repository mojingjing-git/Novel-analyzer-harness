<script setup lang="ts">
import Icon from '../components/Icon.vue'
import { ref, computed, onMounted, onUnmounted } from 'vue'
import LogConsole from '../components/LogConsole.vue'
import ChapterDetailPanel from '../components/ChapterDetailPanel.vue'
import BookSelector from '../components/BookSelector.vue'
import ProgressBar from '../components/ProgressBar.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import CountUp from '../components/CountUp.vue'
import RunDashboard, { type DiscoveryItem, type ActiveBlock, type FinishedBlock } from '../components/RunDashboard.vue'
import { applySummaryProgress } from '../utils/summaryLanes'
import { gcActiveBlocks, reconcileActiveBlocks } from '../utils/laneRegistry'
import {
  applyBlockStart, applyBlockDone, applyTokenDelta, appendDiscovery,
  aggregateRate as computeAggregateRate, summarizeSession, resolveDeleteIndex,
  pickRunningBlockSize, fmt, fmtPercent,
} from '../utils/queueFeed'
import { api, type AnalysisStatus, type TokenStatsResponse, type SessionTokenStatsResponse } from '../api/client'
import { useProgressSocket, type ProgressMessage } from '../api/useProgressSocket'
import { useLogStore } from '../composables/useLogStore'

const status = ref<AnalysisStatus | null>(null)
const progress = ref({ current: 0, total: 0, eta: '' })
const tokens = ref<TokenStatsResponse | null>(null)
const sessionStats = ref<SessionTokenStatsResponse | null>(null)
const busy = ref(false)
const error = ref('')

// H17 (2026-08-26) 实时仪表盘：活跃块 / 已完成块 / 发现流
const activeBlocks = ref<Map<number, ActiveBlock>>(new Map())
const finishedBlocks = ref<Map<number, FinishedBlock>>(new Map())
const discoveries = ref<DiscoveryItem[]>([])
let discoveryId = 1
// H17 Phase 3 V2：块级 token 累计（用于车道进度条 + 卡死预警）+ 速率 sparkline 历史
const tokenByBlock = ref<Map<number, { outputTokens: number; lastTokenAt: number }>>(new Map())
// H17 P3 V2 修复（2026-08-26）：per-block 最新速率（key=blockId），
// 用作「当前所有活跃 block 的聚合速率」基础。修复前 currentRate 是
// rateHistory 最后一项 = 某个 block 瞬时速率，4 路并发时显示是单 block。
// block_done 时清理对应 blockId 的速率，避免聚合时把已结束 block 的尾速算进去。
const rateByBlock = ref<Map<number, number>>(new Map())
const rateHistory = ref<number[]>([])
// H17 P3 V2：活跃 block 聚合速率（tok/s）= sum(活跃 block 的 rateByBlock)
// 计算本体抽到 utils/queueFeed.ts（可独立 vitest），此处仅做响应式包装
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
  switch (msg.type) {
    case 'progress':
      progress.value = {
        current: msg.payload.current,
        total: msg.payload.total,
        eta: msg.payload.eta || '',
      }
      break
    case 'block_start': {
      // H17: 块开始 → 活跃块登记（状态机本体抽到 utils/queueFeed.ts，可独立 vitest）
      activeBlocks.value = applyBlockStart(activeBlocks.value, msg.payload, Date.now())
      break
    }
    case 'block_done': {
      // H17: 块完成 → 移出活跃 + 清理速率 + 登记 finished（FIFO 上限在 queueFeed 内）
      const r = applyBlockDone(activeBlocks.value, finishedBlocks.value, rateByBlock.value, msg.payload)
      activeBlocks.value = r.active
      finishedBlocks.value = r.finished
      rateByBlock.value = r.rateByBlock
      scheduleRefresh(refresh)
      break
    }
    case 'discovery': {
      // H17: 发现流 append-only（200 条上限在 queueFeed 内）
      const r = appendDiscovery(discoveries.value, msg.payload, discoveryId, Date.now())
      discoveries.value = r.items
      discoveryId = r.nextId
      break
    }
    case 'token_delta': {
      // H17 Phase 3 V2：实时 token 增量 → 块级累计 + 速率 sparkline（queueFeed.applyTokenDelta）
      const r = applyTokenDelta(tokenByBlock.value, rateByBlock.value, rateHistory.value, msg.payload, Date.now())
      tokenByBlock.value = r.tokenByBlock
      rateByBlock.value = r.rateByBlock
      rateHistory.value = r.rateHistory
      break
    }
    case 'state_change': scheduleRefresh(refresh); break
    case 'token_stats': scheduleRefresh(refreshTokens); break
    case 'summary_progress': {
      // 最终总结阶段的进度事件：原版只更新顶部 hint 横幅，但 LaneView 显示的还是
      // 分析阶段的残留（ch1669-ch1648 之类）。H17 P3 V2 修复（2026-08-26）：把
      // summary 事件合成为 block_start/block_done 喂给现有 activeBlocks/finishedBlocks，
      // LaneView 就能用同一组件显示最终总结阶段的 LLM 调用。
      // 合成逻辑全部抽到 utils/summaryLanes.ts（可独立 vitest）。
      const p = msg.payload
      const phase = String(p.phase ?? '')
      const phaseLabel: Record<string, string> = {
        batch: '分卷分析+伏笔调和', recheck: '全书伏笔复检',
        style: '写作风格分析', report: '生成全书脉络报告', complete: '已完成',
      }
      const phLabel = phaseLabel[phase] || phase
      const bd = Number(p.batches_done ?? 0)
      const tb = Number(p.total_batches ?? 0)
      const extra = p.message ? `（${String(p.message)}）` : ''
      showSummaryHint(`最终总结进行中${phLabel ? '：' + phLabel : ''} · 卷${bd}/${tb}${extra} — 详见「最终总结」页`)

      // 合成 LaneView 兼容的 active/finished 块（phase/batch_done/complete 三种事件类型）
      const result = applySummaryProgress(p, activeBlocks.value, finishedBlocks.value)
      if (result.changed) {
        activeBlocks.value = result.newActive
        finishedBlocks.value = result.newFinished
      }
      break
    }
  }
}

const { connected } = useProgressSocket(onMessage)

// 车道注册表自愈（车道堆叠修复）：随 refresh()（5s 轮询 + block_done 防抖）执行
// 1) GC：elapsed 超预警窗且无 token 更新的僵尸车道回收（WS 丢 done 的兜底），
//    并同步清理 tokenByBlock/rateByBlock（同样只增不减的内存泄漏）
// 2) 对账：用后端 inflight_blocks（信号量真实在途）整体纠偏分析块，
//    保留总结合成车道（≥900000）与 15s 内新登记块（防 fetch/start 竞态闪烁）
function syncLaneRegistry() {
  if (!status.value?.running) return
  const now = Date.now()
  const { kept, gcIds } = gcActiveBlocks(
    activeBlocks.value, tokenByBlock.value, now, STALL_WARN_SEC)
  if (gcIds.length > 0) {
    for (const id of gcIds) {
      tokenByBlock.value.delete(id)
      rateByBlock.value.delete(id)
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
async function refreshTokens() { try { tokens.value = await api.getTokenStats() } catch (e) { console.error(e) } }
// 本次窗口会话累计（分析+总结，内存态跨书累计；重启清零）
async function refreshSessionStats() { try { sessionStats.value = await api.getSessionTokenStats() } catch (e) { console.error(e) } }
// 本次窗口会话的总量与命中率（命中率只算分析侧）——计算本体在 utils/queueFeed.summarizeSession
const sessionTotal = computed(() => summarizeSession(sessionStats.value))

async function withBusy(fn: () => Promise<void>, label: string) {
  busy.value = true; error.value = ''
  try { await fn(); await refresh() }
  catch (e) { error.value = `${label}: ${(e as Error).message}`; add(error.value, 'error', 'analysis') }
  finally { busy.value = false }
}

function handleStart() {
  withBusy(async () => {
    // 新分析开始即清零进度：否则上一轮 100% 残留到首条 WS 消息到达（M-3）
    progress.value = { current: 0, total: 0, eta: '' }
    await api.startAnalysis()
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

const STALL_WARN_SEC = 480

// 设置项 tooltip 字典：只覆盖"非一眼能看出"的字段；状态/操作按钮/进度文字不加
const tooltips: Record<string, string> = {
  // 工具栏
  scan_workspace: '扫描 working_directory 下的 .txt 文件，自动加入队列',
  download_logs: '下载完整运行日志（含调试信息与 Python 技术日志）',
  // 章节输入
  chapter_input: '0 或留空 = 自动跟踪最新章节；输入数字 = 跳到指定章节',
  // Session token 累计 5 项
  session_input: '本次窗口会话累计输入 tokens = 系统提示 + 用户消息总和；分析 + 总结两段',
  session_output: '本次窗口会话累计输出 tokens = LLM 实际生成的内容',
  session_cached: '本次窗口会话累计 KV cache 命中 tokens（prompt 重复片段命中部分不计费但占带宽）',
  session_hit_rate: '命中率 = 命中缓存 ÷ (输入 + 命中缓存)；仅统计分析侧（总结侧未记录缓存命中）',
  session_total: '总消耗 = 输入 + 输出 + 命中缓存；这是实际向 API 发送/接收的 token 总量',
  // 每章统计表表头
  col_elapsed: '这一章从发送到收到 LLM 响应的总耗时（秒），含网络 + 推理 + 重试',
  col_input_tokens: '这一章 LLM 请求的输入 token 总数（系统提示 + 用户消息）',
  col_output_tokens: '这一章 LLM 实际输出的 token 总数',
  col_tps: 'tokens per second：输出 token 数 ÷ 耗时，衡量单章处理速度',
}

onMounted(() => {
  refresh()
  pollTimer = setInterval(async () => {
    await refresh()
    if (status.value?.running) await refreshTokens()
  }, 5000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (summaryHideTimer) clearTimeout(summaryHideTimer)
  for (const t of pendingRefreshTimers.values()) clearTimeout(t)
  pendingRefreshTimers.clear()
})
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h2 class="section-title">分析队列</h2>
        <p class="section-subtitle">管理待分析的小说并启动批量流水线</p>
      </div>
      <div class="flex gap-2">
        <button @click="handleScanWorkspace" :disabled="busy || status?.running" class="glass-button" v-tooltip="tooltips.scan_workspace">扫描工作区</button>
        <button v-if="!status?.running" @click="handleStart" :disabled="busy" class="glass-button glass-button-primary"><Icon name="play" :size="12" /> 开始分析</button>
        <button v-else @click="handleStop" :disabled="busy" class="glass-button glass-button-danger"><Icon name="stop" :size="12" /> 停止</button>
      </div>
    </div>

    <div v-if="status?.running && progress.total > 0" class="glass-card p-4">
      <ProgressBar :current="progress.current" :total="progress.total" :eta="progress.eta" :block-size="currentBlockSize" label="进度" />
    </div>

    <div v-if="error" class="glass-tinted-red px-4 py-2 rounded text-sm">{{ error }}</div>

    <div v-if="summaryHint" class="glass-tinted-blue px-4 py-2 rounded text-sm">
      {{ summaryHint }}
    </div>

    <div class="glass-card">
      <table class="glass-table">
        <thead>
          <tr>
            <th>书名</th>
            <th style="width: 100px">状态</th>
            <th style="width: 140px">进度</th>
            <th style="width: 220px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!status?.items?.length">
            <td colspan="4">
              <div class="empty-state">
                <span class="empty-icon"><Icon name="queue" :size="24" /></span>
                <span>队列为空 — 扫描工作区或开始分析后，书目会出现在这里</span>
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
                <span style="font-size: 10px; margin-left: 4px">章</span>
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

    <div class="flex items-center justify-between flex-wrap gap-3">
      <div v-if="sessionStats" class="total-card">
        <span class="total-item" v-tooltip="tooltips.session_input">输入 <CountUp :value="sessionTotal.input" :format="fmt" /></span>
        <span class="total-item" v-tooltip="tooltips.session_output">输出 <CountUp :value="sessionTotal.output" :format="fmt" /></span>
        <span class="total-item" v-tooltip="tooltips.session_cached">命中缓存 <CountUp :value="sessionTotal.cached" :format="fmt" /></span>
        <span class="total-item" v-tooltip="tooltips.session_hit_rate">命中率 <CountUp :value="sessionTotal.hitRate" :format="fmtPercent" /></span>
        <span class="total-item" v-tooltip="tooltips.session_total">总消耗 <CountUp :value="sessionTotal.total" :format="fmt" /></span>
      </div>
      <button class="glass-button" style="font-size: 12px" v-tooltip="tooltips.download_logs" @click="handleDownloadLogs">下载日志</button>
    </div>

    <div class="split-grid">
      <!-- 左栏：tab 切换（运行概览 | 章节详情） -->
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
                {{ status?.running ? `运行中 · 车道 ${activeBlocks.size}` : '空闲 · 显示最近活动' }}
              </template>
              <template v-else>
                {{ detailChapter > 0 ? `正在查看 第 ${detailChapter} 章` : '自动显示最新章节' }}
              </template>
            </span>
          </div>
          <div class="cd-controls" v-if="currentTab === 'detail'">
            <BookSelector v-model="detailBookId" class="cd-book" />
            <input
              v-model.number="detailChapter"
              type="number"
              min="0"
              placeholder="留空=最新"
              class="glass-input dt-input"
              v-tooltip="tooltips.chapter_input"
            />
          </div>
        </div>
        <RunDashboard
          v-if="currentTab === 'overview'"
          :running="Boolean(status?.running)"
          :concurrency="currentConcurrency"
          :progress="progress"
          :session-tokens="sessionTotal"
          :active-blocks="activeBlocks"
          :finished-blocks="finishedBlocks"
          :token-by-block="tokenByBlock"
          :rate-history="rateHistory"
          :aggregate-rate="aggregateRate"
          :stall-warn-sec="STALL_WARN_SEC"
          :discoveries="discoveries"
          class="split-detail"
        />
        <ChapterDetailPanel
          v-else
          :book-id="detailBookId"
          v-model:chapter="detailChapter"
          class="split-detail"
        />
      </div>

      <!-- 右栏：简化日志（过滤 debug），撑满整列（腾出的空间由其填补） -->
      <div class="split-col">
        <div class="col-head">
          <div class="head-main">
            <span class="col-title">
              实时日志（简）
              <span class="col-sub">({{ logsSimplifiedCount }} 条)</span>
            </span>
            <span class="col-sub2">完整日志见「设置」页底部</span>
          </div>
          <div class="flex items-center gap-3">
            <span class="conn-state" style="font-size: 11px">
              <span class="dot" :class="connected ? 'on' : 'off'"></span>
              {{ connected ? '已连接' : '未连接' }}
            </span>
            <button @click="clear" class="glass-button" style="font-size: 12px">清空</button>
          </div>
        </div>
        <LogConsole :logs="logs" :simplified="true" class="split-log" />
      </div>
    </div>

    <div v-if="tokens?.chapter_stats?.length" class="glass-card">
      <div class="px-4 py-2.5 text-sm font-semibold" style="border-bottom: 1px solid var(--win-stroke)">
        每章统计
        <span style="color: var(--win-text-secondary); font-weight: 400">(最近 {{ Math.min(50, tokens.chapter_stats.length) }} 章 / 共 {{ tokens.chapter_stats.length }} 章)</span>
      </div>
      <table class="glass-table" style="border-radius: 0; border: none">
        <thead>
          <tr><th>章节</th><th v-tooltip="tooltips.col_elapsed">耗时(s)</th><th v-tooltip="tooltips.col_input_tokens">输入 Tokens</th><th v-tooltip="tooltips.col_output_tokens">输出 Tokens</th><th v-tooltip="tooltips.col_tps">t/s</th></tr>
        </thead>
        <tbody>
          <tr v-for="stat in tokens.chapter_stats.slice(-50)" :key="stat.chapter">
            <td>第 {{ stat.chapter }} 章</td>
            <td>{{ stat.elapsed?.toFixed(1) }}</td>
            <td>{{ stat.input_tokens }}</td>
            <td>{{ stat.output_tokens }}</td>
            <td>{{ stat.elapsed > 0 ? (stat.output_tokens / stat.elapsed).toFixed(1) : '-' }}</td>
          </tr>
        </tbody>
      </table>
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
/* 会话 token 累计信息条（分析+总结，内存态）：
   Win11 辅助信息栏风格——融入背景（--win-control-alt），与表格同款分隔；
   去掉 .glass-card 的强对比边框与阴影，避免视觉突兀 */
.total-card {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--win-text-secondary);
  flex-wrap: wrap;
  background: var(--win-control-alt);
  border: none;
  border-radius: var(--win-radius-control);
  box-shadow: none;
}
.total-item {
  white-space: nowrap;
}
.total-item b {
  color: var(--win-text-primary);
  font-weight: 500;
}

/* H17 (2026-08-26) 左面板 tab 切换（运行概览 | 章节详情） */
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
  color: var(--win-accent, #4f46e5);
  background: rgba(79, 70, 229, 0.10);
  border-color: rgba(79, 70, 229, 0.25);
}

/* 章节选择控件：位于左栏标题条内，与详情框同宽 */
.cd-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}
.cd-book {
  flex: 1;
  min-width: 0;
}
.dt-input {
  width: 110px;
  flex-shrink: 0;
}

/* 实时日志与章节详情各占一半宽度，等高且顶部对齐；工具栏移除后腾出的空间由日志整列撑满 */
.split-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  height: 64vh;
  min-height: 460px;
}
.split-col {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
/* 两个分栏使用同等高度的标题条，保证下方内容框上沿、下沿都对齐
   H17 P3 V2 修复（2026-08-26）：min-height 38 → 56，容纳 2 行内容（标题 14px + 副标 11px + 行距）
   修复前左栏 2 行（tabs + 副标）撑到 ~46px，右栏 1 行 + 按钮 ~32px，
   align-items:center 居中后导致左右两卡顶/底沿错位 ~5-8px */
.col-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
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
  color: var(--win-text-disabled);
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
