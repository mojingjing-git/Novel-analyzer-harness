<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, inject } from 'vue'
import { routerKey } from 'vue-router'
import BookSelector from '../components/BookSelector.vue'
import LogConsole, { type LogEntry } from '../components/LogConsole.vue'
import Icon from '../components/Icon.vue'
import LaneView from '../components/LaneView.vue'
import ThroughputChart from '../components/ThroughputChart.vue'
import { api, type SummaryStatus, type SummaryInflightLane, type StyleProfileDto, type RecheckVerdictsResponse } from '../api/client'
import { useProgressSocket, type ProgressMessage } from '../api/useProgressSocket'
import { renderMarkdownWithToc } from '../utils/markdown'
import {
  SUMMARY_PHASES, phaseLabelOf, phaseIndexOf, stepClassOf, overallPctOf,
  batchPctOf, elapsedTextOf, summaryTokenTextOf, showProgressOf,
  recheckAccountOf, recheckMetaTextOf, stepSubtextOf, stepProgressPctOf,
} from '../utils/summaryProgress'
import {
  applySummaryProgress, reconcileSummaryLanes,
  type SummaryLaneBlocks, type SummaryLaneFinished,
} from '../utils/summaryLanes'
import {
  upsertTokenDeltaBuffer, sampleRateHistory, aggregateRate as computeAggregateRate,
  isSummaryTokenDelta, RATE_HISTORY_MAX, type BlockTokenInfo,
} from '../utils/queueFeed'
import { inferThinkingState } from '../utils/settingsForm'
import { useLogStore } from '../composables/useLogStore'

// 进度计算本体（4 阶段加权等）抽到 utils/summaryProgress.ts，可独立 vitest；
// SUMMARY_PHASES 同时供模板渲染步骤条

// ===== 共享书目 =====
const bookId = ref('')

// ===== 风格量化概览与 Excel 导出 =====
const excelBusy = ref(false)
const excelError = ref('')
const excelNotice = ref('')
async function exportExcel() {
  if (!bookId.value) return
  excelBusy.value = true
  excelError.value = ''
  excelNotice.value = ''
  try {
    const res = await api.exportAggregateExcel(bookId.value)
    excelNotice.value = res.path ? `导出成功：${res.path}` : '导出成功'
  } catch (e) {
    excelError.value = (e as Error).message
  } finally {
    excelBusy.value = false
  }
}

const styleProfile = ref<StyleProfileDto | null>(null)
async function loadStyleProfile() {
  const bid = bookId.value
  if (!bid) { styleProfile.value = null; return }
  try {
    const res = await api.getStyleProfile(bid)
    if (bid !== bookId.value) return
    styleProfile.value = res
  } catch {
    if (bid === bookId.value) styleProfile.value = null
  }
}

const STYLE_METRIC_SPECS: { key: string; label: string; format: (v: number) => string }[] = [
  { key: 'avg_sentence_length', label: '平均句长', format: v => `${v.toFixed(1)} 字` },
  { key: 'dialogue_ratio', label: '对话占比', format: v => `${(v * 100).toFixed(1)}%` },
  { key: 'short_sentence_ratio', label: '短句占比', format: v => `${(v * 100).toFixed(1)}%` },
  { key: 'long_sentence_ratio', label: '长句占比', format: v => `${(v * 100).toFixed(1)}%` },
  // 后端 aggregate_stats 已对文言/口语词比乘以 100（百分比数值），禁止再次 * 100
  { key: 'literary_ratio', label: '文言词比', format: v => `${v.toFixed(2)}%` },
  { key: 'colloquial_ratio', label: '口语词比', format: v => `${v.toFixed(2)}%` },
  { key: 'sensory_density', label: '感官密度', format: v => `${v.toFixed(2)} /千字` },
  { key: 'metaphor_freq', label: '比喻频度', format: v => `${v.toFixed(2)} /千字` },
  { key: 'psychological_density', label: '心理描写', format: v => `${v.toFixed(2)} /千字` },
  { key: 'ttr', label: '用字丰富度', format: v => `${(v * 100).toFixed(1)}%` },
]

const styleMetrics = computed(() => {
  const stat = styleProfile.value?.statistical
  if (!stat) return []
  return STYLE_METRIC_SPECS
    .filter(s => typeof stat[s.key] === 'number' && Number.isFinite(stat[s.key]))
    .map(s => ({ key: s.key, label: s.label, formatted: s.format(stat[s.key]) }))
})

// ===== 最终总结（范围选择 + 设置页只读摘要；模型与并发统一前往设置页调整） =====
const router = inject(routerKey, null)
function goToSettings() {
  void router?.push('/settings')
}

const startCh = ref(1)
const endCh = ref(99999)

const cfgBatchSize = ref(30)
const cfgSummaryConcurrency = ref(0)
const cfgAnalysisConcurrency = ref(2)
const cfgRecheckConcurrency = ref(0)
const cfgSummaryModel = ref('')
const cfgMainModel = ref('')
const cfgRecheckModel = ref('')
const cfgSummaryThinking = ref<Record<string, unknown>>({})
const cfgMainThinking = ref<Record<string, unknown>>({})
let settingsSeq = 0

const effectiveSummaryModel = computed(() =>
  cfgSummaryModel.value.trim() || cfgMainModel.value.trim() || '未配置',
)
const summaryModelFollowsMain = computed(() => !cfgSummaryModel.value.trim())
const effectiveSummaryConcurrency = computed(() =>
  cfgSummaryConcurrency.value > 0
    ? cfgSummaryConcurrency.value
    : Math.max(1, cfgAnalysisConcurrency.value || 1),
)
const summaryConcurrencyFollowsMain = computed(() => cfgSummaryConcurrency.value <= 0)
const effectiveSummaryThinkingLabel = computed(() => {
  const own = cfgSummaryThinking.value
  const hasOwn = Object.keys(own).length > 0
  const effective = hasOwn ? own : cfgMainThinking.value
  const st = inferThinkingState(effective)
  const prefix = hasOwn ? '' : '跟随主模型 · '
  if (st === 'disabled') return `${prefix}已禁用思考`
  if (st === 'depth_low') return `${prefix}浅度思考 (low)`
  if (st === 'depth_medium') return `${prefix}中度思考 (medium)`
  if (st === 'depth_high') return `${prefix}深度思考 (high)`
  if (st === 'budget') return `${prefix}Token 预算`
  if (st === 'custom') return `${prefix}自定义参数`
  return `${prefix}端点默认`
})
const effectiveRecheckModel = computed(() =>
  cfgRecheckModel.value.trim() || cfgMainModel.value.trim() || '未配置',
)
const effectiveRecheckConcurrency = computed(() =>
  cfgRecheckConcurrency.value > 0
    ? cfgRecheckConcurrency.value
    : Math.max(1, cfgAnalysisConcurrency.value || 1),
)

async function loadSummaryConfig() {
  const seq = ++settingsSeq
  try {
    const cfg = await api.getSettings()
    if (seq !== settingsSeq || !cfg) return
    cfgBatchSize.value = cfg.analysis?.summary_batch_size ?? 30
    cfgSummaryConcurrency.value = cfg.analysis?.summary_concurrency ?? 0
    cfgAnalysisConcurrency.value = cfg.analysis?.concurrency ?? 2
    cfgRecheckConcurrency.value = cfg.analysis?.recheck_concurrency ?? 0
    cfgSummaryModel.value = cfg.api?.summary_model ?? ''
    cfgMainModel.value = cfg.api?.model ?? ''
    cfgRecheckModel.value = cfg.analysis?.recheck_model ?? ''
    cfgSummaryThinking.value = cfg.api?.summary_thinking_mode ?? {}
    cfgMainThinking.value = cfg.api?.thinking_mode ?? {}
  } catch (e) {
    console.error('读取总结配置失败:', e)
  }
}

// ===== 主内容 3-Tab 与持久化复检明细 =====
type SummaryViewTab = 'report' | 'style' | 'recheck'
const activeTab = ref<SummaryViewTab>('report')
const recheckVerdicts = ref<RecheckVerdictsResponse | null>(null)
const recheckVerdictsLoading = ref(false)
const recheckTierFilter = ref<string>('all')

const VERDICT_TIER_LABELS: Record<string, string> = {
  RESOLVED_EXPLICIT: '显式回收',
  RESOLVED_IMPLICIT: '隐式回收',
  PARTIALLY_RESOLVED: '阶段性回收',
  UNRESOLVED: '未回收',
  SUPPORTED: '已确认回收',
  INSUFFICIENT: '证据不足',
  CONFLICT: '证据冲突',
  INVALID: '无效线索',
}

function verdictBadgeClass(tierOrVerdict: string): string {
  if (tierOrVerdict === 'RESOLVED_EXPLICIT' || tierOrVerdict === 'RESOLVED_IMPLICIT' || tierOrVerdict === 'SUPPORTED') {
    return 'glass-tinted-green'
  }
  if (tierOrVerdict === 'PARTIALLY_RESOLVED') return 'glass-tinted-blue'
  if (tierOrVerdict === 'CONFLICT' || tierOrVerdict === 'INVALID') return 'glass-tinted-red'
  return 'glass-tinted-orange'
}

const filteredVerdicts = computed(() => {
  const list = recheckVerdicts.value?.verdicts ?? []
  if (recheckTierFilter.value === 'all') return list
  return list.filter((v) => (v.tier || v.verdict) === recheckTierFilter.value)
})

const SEMANTIC_STYLE_LABELS: Record<string, string> = {
  narrative_rhythm: '叙事节奏',
  sentence_style: '句式风格',
  dialogue_style: '对话风格',
  tone: '整体基调',
  imagery: '意象特征',
  pacing: '推进节奏',
  perspective: '叙事视角',
}

const semanticStyleRows = computed(() => {
  const sem = styleProfile.value?.semantic
  if (!sem || typeof sem !== 'object') return []
  return Object.entries(sem)
    .filter(([, val]) => val !== null && val !== undefined && String(val).trim() !== '')
    .map(([key, val]) => ({
      key,
      label: SEMANTIC_STYLE_LABELS[key] || key,
      text: Array.isArray(val) ? val.join('、') : String(val),
    }))
})

async function loadRecheckVerdicts() {
  const bid = bookId.value
  if (!bid) { recheckVerdicts.value = null; return }
  recheckVerdictsLoading.value = true
  try {
    const res = await api.getRecheckVerdicts?.(bid)
    if (bid !== bookId.value) return
    recheckVerdicts.value = res ?? null
  } catch {
    if (bid === bookId.value) recheckVerdicts.value = null
  } finally {
    if (bid === bookId.value) recheckVerdictsLoading.value = false
  }
}
const summaryStatus = ref<SummaryStatus | null>(null)
const report = ref('')
const nowSec = ref(Date.now() / 1000)
let clockTimer: ReturnType<typeof setInterval> | null = null

function syncClock(isRunning: boolean) {
  if (isRunning && !clockTimer) {
    nowSec.value = Date.now() / 1000
    clockTimer = setInterval(() => {
      nowSec.value = Date.now() / 1000
    }, 1000)
  } else if (!isRunning && clockTimer) {
    clearInterval(clockTimer)
    clockTimer = null
  }
}

watch(() => Boolean(summaryStatus.value?.running), (isRunning) => {
  syncClock(isRunning)
}, { immediate: true })

// ===== 总结日志抽屉（业务+技术日志，按后端 category="summary" 过滤）=====
const { logs: allLogs, removeByCategory } = useLogStore()
const summaryLogs = computed(() =>
  (allLogs.value as (LogEntry & { category?: string })[]).filter(
    (l) => l.category === 'summary' && l.kind !== 'debug' && l.source !== 'block',
  ),
)
const drawerOpen = ref(false)
// 点击开始总结后自动展开；阶段完成（报告输出）后自动收起；可手动切换
watch(summaryStatus, (s) => {
  if (s?.phase === 'complete') {
    drawerOpen.value = false
    settingsOpen.value = false
  }
})

// ===== 报告优先展示、可折叠生成设置与目录导航 =====
const hasReport = computed(() => Boolean(report.value.trim()))
const settingsOpen = ref(false)
const settingsExpanded = computed(() => !hasReport.value || settingsOpen.value)
const parsedReport = computed(() => renderMarkdownWithToc(report.value))
const reportHtml = computed(() => parsedReport.value.html)
const tocHeadings = computed(() => parsedReport.value.headings)
const minHeadingLevel = computed(() => (
  tocHeadings.value.length ? Math.min(...tocHeadings.value.map(h => h.level)) : 1
))
const mobileTocOpen = ref(false)
const activeHeadingId = ref('')
const reportCardRef = ref<HTMLElement | null>(null)

watch(tocHeadings, (list) => {
  activeHeadingId.value = list[0]?.id || ''
  mobileTocOpen.value = false
})

function scrollToHeading(id: string) {
  activeHeadingId.value = id
  mobileTocOpen.value = false
  const el = reportCardRef.value?.querySelector<HTMLElement>('#' + id) ?? document.getElementById(id)
  el?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
}

// ===== 进度条（细化：4 阶段步骤 + 批次子进度 + 整体百分比 + 耗时）=====
const phaseLabel = computed(() => phaseLabelOf(summaryStatus.value?.phase))

const phaseIndex = computed(() => phaseIndexOf(summaryStatus.value?.phase))

function stepClass(i: number): string {
  return stepClassOf(phaseIndex.value, i)
}

const overallPct = computed(() => overallPctOf(summaryStatus.value))

const batchPct = computed(() => batchPctOf(summaryStatus.value))

const elapsedText = computed(() => elapsedTextOf(summaryStatus.value, nowSec.value))

const summaryTokenText = computed(() => summaryTokenTextOf(summaryStatus.value))

// 带证据复检（V3 / V2）的账：WS recheck_done 与 /api/summary/status 同步提供
const recheckAccount = computed(() => recheckAccountOf(summaryStatus.value?.recheck))
const recheckMetaText = computed(() => recheckMetaTextOf(summaryStatus.value))

const showProgress = computed(() => showProgressOf(summaryStatus.value))

function ensureStatus(book?: string): SummaryStatus {
  if (!summaryStatus.value) {
    summaryStatus.value = {
      running: true,
      book_id: book || bookId.value || '',
      phase: 'starting',
      batches_done: 0,
      total_batches: 0,
      error: '',
      started_at: Date.now() / 1000,
      finished_at: 0,
      token_stats: {},
    }
  }
  return summaryStatus.value
}

const summaryError = ref('')
const summaryActiveBlocks = ref<SummaryLaneBlocks>(new Map())
const summaryFinishedBlocks = ref<SummaryLaneFinished>(new Map())
const summaryTokenByBlock = ref<Map<number, BlockTokenInfo>>(new Map())
const summaryRateByBlock = ref<Map<number, number>>(new Map())
const summaryRateHistory = ref<number[]>([])
const liveConcurrency = ref<number | null>(null)

const SUMMARY_THROUGHPUT_INTERVAL_MS = 250
const SUMMARY_RATE_HISTORY_MAX = RATE_HISTORY_MAX * 4
const SUMMARY_LANE_FLUSH_TICKS = 4

const pendingSummaryTokenBuf = new Map<number, BlockTokenInfo>()
const pendingSummaryRateBuf = new Map<number, number>()
let hasPendingSummaryTokenFlush = false
let hasPendingSummaryRateFlush = false
let summaryLaneFlushTicks = 0
let summaryRateTimer: ReturnType<typeof setInterval> | null = null

const summaryAggregateRate = computed(() =>
  computeAggregateRate(summaryRateByBlock.value, summaryActiveBlocks.value),
)
const effectiveLaneConcurrency = computed(() => {
  const live = liveConcurrency.value
  if (typeof live === 'number' && Number.isFinite(live) && live > 0) {
    return Math.max(1, Math.round(live))
  }
  const st = summaryStatus.value?.concurrency
  if (typeof st === 'number' && Number.isFinite(st) && st > 0) {
    return Math.max(1, Math.round(st))
  }
  return effectiveSummaryConcurrency.value
})

function clearSummaryTokenState(blockId?: number) {
  if (typeof blockId === 'number') {
    pendingSummaryTokenBuf.delete(blockId)
    pendingSummaryRateBuf.delete(blockId)
    if (summaryTokenByBlock.value.has(blockId)) {
      const next = new Map(summaryTokenByBlock.value)
      next.delete(blockId)
      summaryTokenByBlock.value = next
    }
    if (summaryRateByBlock.value.has(blockId)) {
      const next = new Map(summaryRateByBlock.value)
      next.delete(blockId)
      summaryRateByBlock.value = next
    }
    return
  }
  pendingSummaryTokenBuf.clear()
  pendingSummaryRateBuf.clear()
  hasPendingSummaryTokenFlush = false
  hasPendingSummaryRateFlush = false
  summaryLaneFlushTicks = 0
  summaryTokenByBlock.value = new Map()
  summaryRateByBlock.value = new Map()
}

function archiveLeftoverLanes(ok: boolean, targetMap: SummaryLaneBlocks = summaryActiveBlocks.value) {
  if (targetMap.size === 0) return
  const nextFinished: SummaryLaneFinished = new Map(summaryFinishedBlocks.value)
  for (const [blockId, blk] of targetMap) {
    if (!nextFinished.has(blockId)) {
      nextFinished.set(blockId, { ok, range: blk.range })
    }
    clearSummaryTokenState(blockId)
  }
  while (nextFinished.size > 50) {
    const firstKey = nextFinished.keys().next().value
    if (firstKey === undefined) break
    nextFinished.delete(firstKey)
  }
  summaryFinishedBlocks.value = nextFinished
  if (targetMap === summaryActiveBlocks.value) {
    summaryActiveBlocks.value = new Map()
  }
}

function resetSummaryLanes() {
  summaryActiveBlocks.value = new Map()
  summaryFinishedBlocks.value = new Map()
  summaryRateHistory.value = []
  summaryLaneFlushTicks = 0
  clearSummaryTokenState()
}

function flushSummaryRateBuffer() {
  if (!hasPendingSummaryRateFlush) return
  summaryRateByBlock.value = new Map(pendingSummaryRateBuf)
  hasPendingSummaryRateFlush = false
}

function flushSummaryTokenBuffer() {
  if (!hasPendingSummaryTokenFlush) return
  summaryTokenByBlock.value = new Map(pendingSummaryTokenBuf)
  hasPendingSummaryTokenFlush = false
}

function startSummaryRateSampler() {
  if (summaryRateTimer) return
  summaryRateTimer = setInterval(() => {
    flushSummaryRateBuffer()
    if (summaryStatus.value?.running || summaryActiveBlocks.value.size > 0) {
      summaryRateHistory.value = sampleRateHistory(
        summaryRateHistory.value,
        summaryAggregateRate.value,
        0.22,
        SUMMARY_RATE_HISTORY_MAX,
      )
    }
    summaryLaneFlushTicks += 1
    if (summaryLaneFlushTicks >= SUMMARY_LANE_FLUSH_TICKS) {
      summaryLaneFlushTicks = 0
      flushSummaryTokenBuffer()
    }
  }, SUMMARY_THROUGHPUT_INTERVAL_MS)
}

function stopSummaryRateSampler() {
  if (!summaryRateTimer) return
  clearInterval(summaryRateTimer)
  summaryRateTimer = null
  summaryLaneFlushTicks = 0
}

async function startSummary() {
  summaryError.value = ''
  try {
    resetSummaryLanes()
    liveConcurrency.value = effectiveSummaryConcurrency.value
    if (summaryStatus.value) {
      summaryStatus.value = {
        ...summaryStatus.value,
        summary_done: 0,
        recon_done: 0,
        recheck_progress: null,
      }
    }
    await api.startSummary(bookId.value, startCh.value, endCh.value, 0, 0)
    drawerOpen.value = true // 开始总结 → 自动展开日志抽屉
    void refreshSummaryStatus()
  } catch (e) { summaryError.value = (e as Error).message }
}
async function stopSummary() { try { await api.stopSummary() } catch (e) { summaryError.value = (e as Error).message } }
async function refreshSummaryStatus() {
  try {
    const st = await api.summaryStatus()
    if (!st) return
    summaryStatus.value = st
    if (typeof st.concurrency === 'number' && Number.isFinite(st.concurrency) && st.concurrency > 0) {
      liveConcurrency.value = st.concurrency
    }
    const now = Date.now()
    const prevActive = summaryActiveBlocks.value
    if (st.running) {
      if (Array.isArray(st.inflight_lanes)) {
        const inflight: SummaryInflightLane[] = st.inflight_lanes
        const nextActive = reconcileSummaryLanes(prevActive, inflight)
        for (const [id, blk] of prevActive) {
          if (!nextActive.has(id) && now - blk.startedAt < 3000) {
            nextActive.set(id, blk)
          }
        }
        for (const id of prevActive.keys()) {
          if (!nextActive.has(id)) {
            clearSummaryTokenState(id)
          }
        }
        summaryActiveBlocks.value = nextActive
      }
    } else if (prevActive.size > 0) {
      archiveLeftoverLanes(st.phase === 'complete')
      clearSummaryTokenState()
    }
  } catch (e) { console.error(e) }
}
async function loadReport() {
  const bid = bookId.value
  if (!bid) return
  try {
    const res = await api.getBookReport(bid)
    if (bid !== bookId.value) return   // 已切书：A 书报告不得挂到 B 名下（v-html 渲染）
    report.value = res.report
    if (res.report && res.report.trim()) settingsOpen.value = false
  } catch (e) { console.error(e) }
}

function onWsMessage(msg: ProgressMessage) {
  if (msg.type === 'token_delta') {
    if (!isSummaryTokenDelta(msg.payload)) return
    upsertTokenDeltaBuffer(pendingSummaryTokenBuf, pendingSummaryRateBuf, msg.payload, Date.now())
    hasPendingSummaryTokenFlush = true
    hasPendingSummaryRateFlush = true
    return
  }

  if (msg.type === 'summary_progress') {
    const p = msg.payload
    const cur = ensureStatus(p.book_id)
    const prevPhase = cur.phase
    const nextBookId = p.book_id ?? cur.book_id
    const nextTotal = p.total_batches ?? cur.total_batches
    const nextDone = p.batches_done ?? cur.batches_done
    const nextSummaryDone = p.summary_done ?? cur.summary_done
    const nextReconDone = p.recon_done ?? cur.recon_done
    let nextPhase = cur.phase
    let nextRecheck = cur.recheck
    let nextRecheckProgress = cur.recheck_progress ?? null
    if (p.type === 'phase' && p.phase) {
      nextPhase = p.phase
      nextRecheckProgress = null
    } else if (p.type === 'recheck_progress' && p.recheck_progress) {
      nextRecheckProgress = p.recheck_progress
    } else if (p.type === 'recheck_done' && p.recheck) {
      nextRecheck = p.recheck
      nextRecheckProgress = null
      void loadRecheckVerdicts()
    } else if (p.type === 'complete') {
      nextPhase = 'complete'
      nextRecheckProgress = null
    }
    if (typeof p.concurrency === 'number' && Number.isFinite(p.concurrency) && p.concurrency > 0) {
      liveConcurrency.value = p.concurrency
    }
    summaryStatus.value = {
      ...cur,
      running: p.type === 'complete' ? cur.running : true,
      book_id: nextBookId,
      phase: nextPhase,
      batches_done: nextDone,
      total_batches: nextTotal,
      summary_done: nextSummaryDone,
      recon_done: nextReconDone,
      recheck: nextRecheck,
      recheck_progress: nextRecheckProgress,
      concurrency: p.concurrency ?? cur.concurrency,
    }
    const prevActive = summaryActiveBlocks.value
    const laneRes = applySummaryProgress(p, prevActive, summaryFinishedBlocks.value)
    if (laneRes.changed) {
      for (const id of prevActive.keys()) {
        if (!laneRes.newActive.has(id)) {
          clearSummaryTokenState(id)
        }
      }
      summaryActiveBlocks.value = laneRes.newActive
      summaryFinishedBlocks.value = laneRes.newFinished
    }
    if (p.type === 'complete') {
      clearSummaryTokenState()
    }
    if (p.type === 'phase' && prevPhase === 'recheck' && nextPhase !== 'recheck') {
      void refreshSummaryStatus()
    }
    return
  }

  if (msg.type === 'token_stats') {
    const p = msg.payload
    if (p.source !== 'summary') return
    const cur = ensureStatus()
    const prevStats = cur.token_stats || {}
    const prevCat = prevStats[p.category] || { input_tokens: 0, output_tokens: 0 }
    summaryStatus.value = {
      ...cur,
      token_stats: {
        ...prevStats,
        [p.category]: {
          input_tokens: prevCat.input_tokens + (p.input_tokens || 0),
          output_tokens: prevCat.output_tokens + (p.output_tokens || 0),
        },
      },
    }
    return
  }

  if (msg.type === 'state_change') {
    const { state, detail } = msg.payload
    if (!['summary_running', 'summary_done', 'summary_stopped', 'summary_failed'].includes(state)) {
      return
    }
    const cur = ensureStatus()
    if (state === 'summary_running') {
      const wasTerminalOrIdle = ['idle', 'complete', 'failed', 'stopped'].includes(cur.phase)
      if (wasTerminalOrIdle) {
        resetSummaryLanes()
      }
      summaryStatus.value = {
        ...cur,
        running: true,
        phase: wasTerminalOrIdle ? 'starting' : cur.phase,
        batches_done: wasTerminalOrIdle ? 0 : cur.batches_done,
        total_batches: wasTerminalOrIdle ? 0 : cur.total_batches,
        summary_done: wasTerminalOrIdle ? 0 : cur.summary_done,
        recon_done: wasTerminalOrIdle ? 0 : cur.recon_done,
        recheck_progress: wasTerminalOrIdle ? null : cur.recheck_progress,
        error: '',
        started_at: wasTerminalOrIdle ? Date.now() / 1000 : (cur.started_at || Date.now() / 1000),
        finished_at: 0,
      }
      void refreshSummaryStatus()
    } else if (state === 'summary_done') {
      summaryStatus.value = {
        ...cur,
        running: false,
        phase: 'complete',
        recheck_progress: null,
        finished_at: Date.now() / 1000,
      }
      archiveLeftoverLanes(true)
      clearSummaryTokenState()
      drawerOpen.value = false
      settingsOpen.value = false
      void (async () => {
        await refreshSummaryStatus()
        if (!bookId.value || summaryStatus.value?.book_id === bookId.value) {
          await Promise.all([loadReport(), loadStyleProfile(), loadRecheckVerdicts()])
        }
      })()
    } else if (state === 'summary_stopped') {
      summaryStatus.value = {
        ...cur,
        running: false,
        phase: 'stopped',
        recheck_progress: null,
        finished_at: Date.now() / 1000,
      }
      archiveLeftoverLanes(false)
      clearSummaryTokenState()
      void refreshSummaryStatus()
    } else if (state === 'summary_failed') {
      summaryStatus.value = {
        ...cur,
        running: false,
        phase: 'failed',
        recheck_progress: null,
        error: detail || cur.error,
        finished_at: Date.now() / 1000,
      }
      archiveLeftoverLanes(false)
      clearSummaryTokenState()
      void refreshSummaryStatus()
    }
  }
}

const { connected } = useProgressSocket(onWsMessage)
watch(connected, (on, prev) => {
  if (on && prev === false) {
    void refreshSummaryStatus()
  }
})

// 切换书目时，自动刷新报告、文风量化指标与持久化复检明细
watch(bookId, async () => {
  excelError.value = ''
  excelNotice.value = ''
  styleProfile.value = null
  report.value = ''
  recheckVerdicts.value = null
  recheckTierFilter.value = 'all'
  if (!summaryStatus.value?.running) {
    resetSummaryLanes()
    liveConcurrency.value = null
  }
  await Promise.all([loadReport(), loadStyleProfile(), loadRecheckVerdicts()])
})

onMounted(() => {
  startSummaryRateSampler()
  loadSummaryConfig()
  refreshSummaryStatus()
  loadReport()
  loadStyleProfile()
  loadRecheckVerdicts()
})
onUnmounted(() => {
  settingsSeq++
  syncClock(false)
  stopSummaryRateSampler()
})
</script>

<template>
  <div class="summary-page space-y-5">
    <!-- 顶部标题与高频操作条 -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <h2 class="section-title">最终总结</h2>
      <div class="flex items-center gap-2 flex-wrap">
        <button v-if="!summaryStatus?.running" @click="startSummary" :disabled="!bookId" class="glass-button glass-button-primary">
          <Icon name="play" :size="12" /> 开始总结
        </button>
        <button v-else @click="stopSummary" class="glass-button glass-button-danger">
          <Icon name="stop" :size="12" /> 停止
        </button>
        <button @click="loadReport" :disabled="!bookId" class="glass-button">刷新报告</button>
        <button @click="exportExcel" :disabled="excelBusy || !bookId" class="glass-button">{{ excelBusy ? '导出中...' : '导出 Excel' }}</button>
        <button
          v-if="hasReport"
          type="button"
          @click="settingsOpen = !settingsOpen"
          class="glass-button"
          :class="{ 'sp-btn-active': settingsExpanded }"
        >
          <Icon name="settings" :size="13" />
          {{ settingsExpanded ? '收起设置' : '生成设置' }}
        </button>
        <button
          type="button"
          @click="drawerOpen = !drawerOpen"
          class="glass-button"
          :class="{ 'sp-btn-active': drawerOpen }"
        >
          日志 ({{ summaryLogs.length }})
        </button>
      </div>
    </div>

    <BookSelector v-model="bookId" />

    <!-- 生成参数设置面板（仅保留章节范围，模型与并发统一前往设置页调整；始终使用 v-show 保活） -->
    <section v-show="settingsExpanded" class="glass-card sp-settings-panel p-4 space-y-3" data-test="settings-panel">
      <div class="flex items-center justify-between gap-2 pb-2" style="border-bottom: 1px solid var(--win-stroke)">
        <h3 class="font-semibold text-sm" style="letter-spacing: -0.01em">生成参数</h3>
        <button v-if="hasReport" type="button" @click="settingsOpen = false" class="glass-button btn-sm">收起</button>
      </div>

      <div class="sp-num-grid">
        <div class="sp-field">
          <label class="sp-label">起始章</label>
          <input v-model.number="startCh" :disabled="Boolean(summaryStatus?.running)" type="number" min="1" class="glass-input w-full min-w-0" />
        </div>
        <div class="sp-field">
          <label class="sp-label">结束章</label>
          <input v-model.number="endCh" :disabled="Boolean(summaryStatus?.running)" type="number" min="1" class="glass-input w-full min-w-0" />
        </div>
      </div>

      <div class="sp-readonly-bar glass-inset px-3 py-2.5 flex flex-wrap items-center justify-between gap-3">
        <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" style="color: var(--win-text-secondary)">
          <span>
            总结模型：<strong style="color: var(--win-text-primary)">{{ effectiveSummaryModel }}</strong>
            <span v-if="summaryModelFollowsMain" class="ml-1" style="color: var(--win-text-muted)">(跟随主模型)</span>
          </span>
          <span>
            思考：<strong style="color: var(--win-text-primary)">{{ effectiveSummaryThinkingLabel }}</strong>
          </span>
          <span>
            并发：<strong style="color: var(--win-text-primary)">{{ effectiveSummaryConcurrency }} 路</strong>
            <span v-if="summaryConcurrencyFollowsMain" class="ml-1" style="color: var(--win-text-muted)">(跟随主分析)</span>
          </span>
          <span>
            批次：<strong style="color: var(--win-text-primary)">{{ cfgBatchSize }} 章</strong>
          </span>
          <span>
            复检：<strong style="color: var(--win-text-primary)">{{ effectiveRecheckModel }}</strong>
            · {{ effectiveRecheckConcurrency }} 路
          </span>
        </div>
        <button type="button" @click="goToSettings" class="glass-button btn-sm shrink-0">前往设置调整</button>
      </div>
    </section>

    <!-- 提示信息与运行状态（按需显示） -->
    <p v-if="summaryError" class="glass-tinted-red px-3 py-2 rounded text-sm">{{ summaryError }}</p>
    <p v-if="excelError" class="glass-tinted-red px-3 py-2 rounded text-sm">{{ excelError }}</p>
    <p v-if="excelNotice" class="glass-tinted-green px-3 py-2 rounded text-sm">{{ excelNotice }}</p>

    <div v-if="showProgress" class="summary-progress">
      <div class="step-row">
        <div v-for="(ph, i) in SUMMARY_PHASES" :key="ph.key" class="step" :class="stepClass(i)">
          <div class="step-head">
            <span class="step-dot">{{ stepClass(i) === 'done' ? '✓' : (i + 1) }}</span>
            <span class="step-label">{{ ph.label }}</span>
          </div>
          <span
            v-if="stepSubtextOf(summaryStatus, ph.key, i, phaseIndex)"
            class="step-sub"
          >{{ stepSubtextOf(summaryStatus, ph.key, i, phaseIndex) }}</span>
          <div
            v-if="stepProgressPctOf(summaryStatus, ph.key, i, phaseIndex) !== null"
            class="step-mini-bar"
          >
            <div
              class="step-mini-fill"
              :style="{ width: stepProgressPctOf(summaryStatus, ph.key, i, phaseIndex) + '%' }"
            ></div>
          </div>
        </div>
      </div>

      <div class="bar"><div class="bar-fill" :style="{ width: overallPct + '%' }"></div></div>

      <div class="progress-meta">
        <span class="pm-phase">{{ phaseLabel }}</span>
        <span v-if="summaryStatus?.phase === 'batch'" class="pm-sub">
          卷 {{ summaryStatus.batches_done }}/{{ summaryStatus.total_batches }} · {{ batchPct }}%
          <template v-if="typeof summaryStatus.summary_done === 'number' && typeof summaryStatus.recon_done === 'number'">
            (摘要 {{ summaryStatus.summary_done }}/{{ summaryStatus.total_batches }} · 调和 {{ summaryStatus.recon_done }}/{{ summaryStatus.total_batches }})
          </template>
        </span>
        <span v-else-if="summaryStatus?.phase === 'recheck'" class="pm-sub">
          {{ recheckMetaText }}
        </span>
        <span v-else-if="summaryStatus?.phase === 'complete'" class="pm-sub pm-ok">✓ 全书脉络报告已生成</span>
        <span v-else-if="summaryStatus?.phase === 'failed'" class="pm-sub pm-warn">✗ 总结失败</span>
        <span v-else-if="summaryStatus?.phase === 'stopped'" class="pm-sub pm-warn">■ 已停止</span>
        <span
          v-if="summaryStatus?.running || summaryActiveBlocks.size > 0"
          class="pm-concurrency"
        >{{ summaryStatus?.phase === 'recheck' ? '复检并发' : '阶段并发' }} {{ effectiveLaneConcurrency }} 路</span>
        <span class="pm-time">⏱ {{ elapsedText }}</span>
      </div>
      <p v-if="summaryStatus?.error" class="pm-error">错误: {{ summaryStatus.error }}</p>
      <p v-else-if="summaryStatus?.phase === 'complete'" class="pm-done-note">
        共 {{ summaryStatus.total_batches }} 批 · 总耗时 {{ elapsedText }}
      </p>

      <div
        v-if="summaryStatus?.running || summaryActiveBlocks.size > 0 || summaryFinishedBlocks.size > 0 || summaryRateHistory.length > 1"
        class="summary-telemetry"
      >
        <ThroughputChart
          :rate-history="summaryRateHistory"
          :aggregate-rate="summaryAggregateRate"
          :running="Boolean(summaryStatus?.running)"
          :sample-interval-ms="250"
          title="总结输出吞吐"
        />
        <LaneView
          :concurrency="effectiveLaneConcurrency"
          :active-blocks="summaryActiveBlocks"
          :finished-blocks="summaryFinishedBlocks"
          :token-by-block="summaryTokenByBlock"
          :stall-warn-sec="600"
        />
      </div>
    </div>

    <div v-if="summaryTokenText" class="text-xs px-1" style="color: var(--win-text-secondary)">{{ summaryTokenText }}</div>

    <!-- 总结日志面板（按需展开） -->
    <div v-if="drawerOpen" class="glass-card p-3 space-y-2">
      <div class="flex items-center justify-between gap-2 pb-1.5" style="border-bottom: 1px solid var(--win-stroke)">
        <div class="flex items-center gap-2">
          <span class="text-sm font-medium" style="color: var(--win-text-primary)">总结日志</span>
          <span class="text-xs" style="color: var(--win-text-muted)">{{ summaryLogs.length }} 条</span>
        </div>
        <div class="flex items-center gap-2">
          <button
            v-if="summaryLogs.length"
            @click="removeByCategory('summary')"
            class="glass-button btn-sm"
            style="font-size: 11px"
          >清空</button>
          <button type="button" @click="drawerOpen = false" class="glass-button btn-sm" style="font-size: 11px">收起</button>
        </div>
      </div>
      <LogConsole :logs="summaryLogs" />
    </div>

    <!-- 主内容 3-Tab（严格使用 v-show 保活三个视图，兼顾风格/复检独立可读性与异步 DOM 状态） -->
    <div class="summary-tabs" role="tablist">
      <button
        type="button"
        role="tab"
        :aria-selected="activeTab === 'report'"
        class="glass-pill"
        :class="{ 'is-active': activeTab === 'report' }"
        @click="activeTab = 'report'"
      >脉络报告</button>
      <button
        type="button"
        role="tab"
        :aria-selected="activeTab === 'style'"
        class="glass-pill"
        :class="{ 'is-active': activeTab === 'style' }"
        @click="activeTab = 'style'"
      >
        写作风格
        <span v-if="styleProfile?.exists && styleMetrics.length" class="ml-1 opacity-75">({{ styleMetrics.length }})</span>
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="activeTab === 'recheck'"
        class="glass-pill"
        :class="{ 'is-active': activeTab === 'recheck' }"
        @click="activeTab = 'recheck'"
      >
        复检明细
        <span v-if="(recheckVerdicts?.verdicts?.length || recheckAccount?.checked)" class="ml-1 opacity-75">
          ({{ recheckVerdicts?.verdicts?.length || recheckAccount?.checked }})
        </span>
      </button>
    </div>

    <!-- Tab 1：脉络报告 -->
    <section v-show="activeTab === 'report'">
      <div v-if="hasReport" class="report-layout" :class="{ 'has-toc': tocHeadings.length > 0 }">
        <!-- 窄窗折叠目录栏 -->
        <div v-if="tocHeadings.length > 0" class="toc-mobile glass-panel">
          <button
            type="button"
            class="toc-mobile-trigger"
            @click="mobileTocOpen = !mobileTocOpen"
          >
            <span class="font-medium text-sm">目录（{{ tocHeadings.length }} 节）</span>
            <Icon name="arrow_down" :size="13" :style="{ transform: mobileTocOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }" />
          </button>
          <nav v-if="mobileTocOpen" class="toc-mobile-list" aria-label="报告目录">
            <button
              v-for="h in tocHeadings"
              :key="h.id"
              type="button"
              class="toc-link"
              :class="{ 'is-active': activeHeadingId === h.id }"
              :style="{ paddingLeft: `${8 + Math.max(0, h.level - minHeadingLevel) * 12}px` }"
              @click="scrollToHeading(h.id)"
            >{{ h.text }}</button>
          </nav>
        </div>

        <!-- 宽窗左侧粘性目录栏 -->
        <aside v-if="tocHeadings.length > 0" class="toc-sidebar glass-panel p-3" aria-label="报告目录">
          <div class="toc-title">目录（{{ tocHeadings.length }}）</div>
          <nav class="toc-nav">
            <button
              v-for="h in tocHeadings"
              :key="h.id"
              type="button"
              class="toc-link"
              :class="{ 'is-active': activeHeadingId === h.id }"
              :style="{ paddingLeft: `${8 + Math.max(0, h.level - minHeadingLevel) * 12}px` }"
              @click="scrollToHeading(h.id)"
            >{{ h.text }}</button>
          </nav>
        </aside>

        <!-- 正文阅读卡：限制舒适行宽 -->
        <article ref="reportCardRef" class="glass-card report-article p-5 sm:p-7">
          <div class="report-prose md-content" v-html="reportHtml"></div>
        </article>
      </div>
      <div v-else class="glass-panel text-center py-8 text-sm" style="color: var(--win-text-muted)">
        <template v-if="!bookId">请选择书目</template>
        <template v-else-if="summaryStatus?.running && summaryStatus?.book_id === bookId">总结进行中…</template>
        <template v-else>暂无总结报告</template>
      </div>
    </section>

    <!-- Tab 2：写作风格 -->
    <section v-show="activeTab === 'style'" class="space-y-4">
      <div v-if="styleProfile?.exists && (styleMetrics.length || semanticStyleRows.length)" class="glass-card style-section p-4 space-y-4">
        <div class="flex flex-wrap items-center justify-between gap-2 pb-2" style="border-bottom: 1px solid var(--win-stroke)">
          <h3 class="text-sm font-semibold" style="color: var(--win-text-primary)">写作风格量化指标</h3>
          <span v-if="styleProfile.sample_chapters?.length" class="text-xs" style="color: var(--win-text-muted)">
            采样 {{ styleProfile.sample_chapters.length }} 章
          </span>
        </div>
        <div v-if="styleMetrics.length" class="style-metrics-grid">
          <div v-for="m in styleMetrics" :key="m.key" class="glass-inset style-metric-tile">
            <div class="style-metric-label">{{ m.label }}</div>
            <div class="style-metric-value">{{ m.formatted }}</div>
          </div>
        </div>
        <div v-if="semanticStyleRows.length" class="space-y-2 pt-2" style="border-top: 1px solid var(--win-stroke)">
          <div class="text-xs font-medium" style="color: var(--win-text-secondary)">语义文风特征</div>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-2">
            <div v-for="row in semanticStyleRows" :key="row.key" class="glass-inset px-3 py-2 text-xs">
              <div class="font-medium mb-0.5" style="color: var(--win-text-secondary)">{{ row.label }}</div>
              <div style="color: var(--win-text-primary); line-height: 1.5">{{ row.text }}</div>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="glass-panel text-center py-8 text-sm" style="color: var(--win-text-muted)">
        {{ !bookId ? '请选择书目' : '暂无写作风格画像数据' }}
      </div>
    </section>

    <!-- Tab 3：复检明细（实时计数账 + 持久化逐条证据链） -->
    <section v-show="activeTab === 'recheck'" class="space-y-4">
      <div v-if="recheckAccount" class="glass-panel p-3 space-y-1">
        <div class="flex items-center gap-2 text-sm flex-wrap">
          <span class="font-medium" style="color: var(--win-text-primary)">伏笔复检（{{ recheckAccount.v3_tiers ? '带证据 V3' : '带证据 V2' }}）</span>
          <span
            class="text-xs px-2 py-0.5 rounded"
            :class="recheckAccount.status === 'COMPLETED' ? 'glass-tinted-green' : 'glass-tinted-orange'"
          >{{ recheckAccount.status || '—' }}</span>
          <span class="text-xs" style="color: var(--win-text-secondary)">{{ recheckAccount.calls }} 次调用</span>
        </div>
        <div v-if="recheckAccount.v3_tiers" class="text-xs" style="color: var(--win-text-secondary)">
          已核对 <b>{{ recheckAccount.checked }}</b> / {{ recheckAccount.candidates_total }} ·
          显式回收 {{ recheckAccount.v3_tiers.resolved_explicit }} ·
          隐式回收 {{ recheckAccount.v3_tiers.resolved_implicit }} ·
          阶段性回收 {{ recheckAccount.v3_tiers.partially_resolved }} ·
          未回收 {{ recheckAccount.v3_tiers.unresolved }} ·
          未核对 {{ recheckAccount.not_checked }}<template v-if="recheckAccount.dropped_no_anchor"> · 无证据未进候选 {{ recheckAccount.dropped_no_anchor }}</template>
        </div>
        <div v-else class="text-xs" style="color: var(--win-text-secondary)">
          已核对 <b>{{ recheckAccount.checked }}</b> / {{ recheckAccount.candidates_total }} ·
          回收 {{ recheckAccount.supported }} · 证据不足 {{ recheckAccount.insufficient }} ·
          冲突 {{ recheckAccount.conflict }}<template v-if="recheckAccount.invalid"> · 无效 {{ recheckAccount.invalid }}</template> ·
          未核对 {{ recheckAccount.not_checked }}<template v-if="recheckAccount.dropped_no_anchor"> · 无证据未进候选 {{ recheckAccount.dropped_no_anchor }}</template>
        </div>
        <p v-if="recheckAccount.status_reason" class="text-xs" style="color: var(--win-warning)">{{ recheckAccount.status_reason }}</p>
      </div>

      <div v-if="recheckVerdicts?.exists" class="glass-card p-4 space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2 pb-2" style="border-bottom: 1px solid var(--win-stroke)">
          <div class="flex flex-wrap items-center gap-2 text-xs">
            <span class="text-sm font-semibold" style="color: var(--win-text-primary)">
              复检判定明细（{{ recheckVerdicts.engine ? recheckVerdicts.engine.toUpperCase() : 'V3' }}）
            </span>
            <span class="glass-badge badge-blue">{{ recheckVerdicts.status || 'COMPLETED' }}</span>
            <span v-if="recheckVerdicts.model" style="color: var(--win-text-secondary)">模型：{{ recheckVerdicts.model }}</span>
            <span style="color: var(--win-text-muted)">共 {{ recheckVerdicts.verdicts.length }} / {{ recheckVerdicts.candidates_total }} 条</span>
          </div>
          <div class="flex flex-wrap gap-1.5">
            <button
              v-for="opt in [
                { key: 'all', label: '全部' },
                { key: 'RESOLVED_EXPLICIT', label: '显式回收' },
                { key: 'RESOLVED_IMPLICIT', label: '隐式回收' },
                { key: 'PARTIALLY_RESOLVED', label: '阶段性回收' },
                { key: 'UNRESOLVED', label: '未回收' },
              ]"
              :key="opt.key"
              type="button"
              class="glass-pill text-xs"
              :class="{ 'is-active': recheckTierFilter === opt.key }"
              @click="recheckTierFilter = opt.key"
            >{{ opt.label }}</button>
          </div>
        </div>

        <p v-if="recheckVerdicts.status_reason" class="text-xs glass-tinted-orange px-3 py-1.5 rounded">
          {{ recheckVerdicts.status_reason }}
        </p>

        <div v-if="filteredVerdicts.length === 0" class="text-xs py-6 text-center" style="color: var(--win-text-muted)">
          当前筛选下暂无判定记录
        </div>
        <div v-else class="space-y-2.5">
          <div v-for="(v, idx) in filteredVerdicts" :key="v.id || v.candidate_id || idx" class="verdict-card glass-inset p-3 space-y-1.5">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="text-xs font-mono" style="color: var(--win-text-muted)">{{ v.id || v.candidate_id }}</span>
                <span class="text-xs px-2 py-0.5 rounded font-medium" :class="verdictBadgeClass(v.tier || v.verdict)">
                  {{ VERDICT_TIER_LABELS[v.tier || v.verdict] || v.tier || v.verdict || '未知' }}
                </span>
                <span v-if="v.applied" class="status-tag badge-green" style="font-size: 11px">已写入账本</span>
                <span v-if="v.gate_demoted" class="status-tag badge-orange" style="font-size: 11px">门控降级</span>
                <span v-if="v.repaired" class="status-tag badge-blue" style="font-size: 11px">已修复</span>
              </div>
              <div class="flex items-center gap-2 text-xs" style="color: var(--win-text-muted)">
                <span v-if="v.first_seen_chapter">首见第{{ v.first_seen_chapter }}章</span>
                <span v-if="v.resolved_chapter">回收第{{ v.resolved_chapter }}章</span>
                <span v-if="v.hops_used">{{ v.hops_used }} 跳</span>
                <span v-if="v.importance" class="glass-badge">{{ v.importance }}</span>
              </div>
            </div>

            <div v-if="v.description || v.question" class="text-xs font-medium" style="color: var(--win-text-primary)">
              {{ v.description || v.question }}
            </div>
            <div v-if="v.answer" class="text-xs" style="color: var(--win-text-primary); line-height: 1.55">
              {{ v.answer }}
            </div>
            <blockquote v-if="v.quote" class="verdict-quote text-xs">
              「{{ v.quote }}」
            </blockquote>
            <div v-if="v.reason" class="text-xs" style="color: var(--win-text-secondary)">
              依据：{{ v.reason }}
            </div>
            <div v-if="v.gate_demoted && v.gate_reason" class="text-xs" style="color: var(--win-warning)">
              门控说明：{{ v.gate_reason }}
            </div>

            <details v-if="v.evidence_chain?.length" class="text-xs pt-1">
              <summary class="cursor-pointer select-none" style="color: var(--win-accent)">
                展开证据链（{{ v.evidence_chain.length }} 条）
              </summary>
              <div class="mt-1.5 space-y-1 pl-2" style="border-left: 2px solid var(--win-stroke)">
                <div v-for="(ev, eIdx) in v.evidence_chain" :key="eIdx" style="color: var(--win-text-secondary)">
                  <span v-if="ev.unit_id" style="color: var(--win-accent)">[单元 {{ ev.unit_id }}] </span>
                  <span v-if="ev.role" class="mr-1" style="color: var(--win-text-muted)">({{ ev.role }})</span>
                  <span v-if="ev.quote">「{{ ev.quote }}」</span>
                </div>
              </div>
            </details>
          </div>
        </div>
      </div>
      <div v-else-if="recheckVerdictsLoading" class="glass-panel text-center py-8 text-sm" style="color: var(--win-text-muted)">
        正在加载复检判定明细…
      </div>
      <div v-else-if="!recheckAccount" class="glass-panel text-center py-8 text-sm" style="color: var(--win-text-muted)">
        {{ !bookId ? '请选择书目' : '该书暂无复检判定明细' }}
      </div>
    </section>
  </div>
</template>

<style scoped>
.summary-page {
  container-type: inline-size;
  min-width: 0;
}

.sp-btn-active {
  background: var(--win-accent-soft);
  border-color: var(--win-accent);
  color: var(--win-accent);
  font-weight: 500;
}

.sp-settings-panel {
  container-type: inline-size;
}
.sp-num-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 12px;
}
@container (max-width: 340px) {
  .sp-num-grid {
    grid-template-columns: 1fr;
  }
}

.sp-readonly-bar {
  border-radius: var(--win-radius-control);
}

.summary-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.verdict-card {
  border-radius: var(--win-radius-control);
}

.verdict-quote {
  margin: 0;
  padding: 4px 10px;
  border-left: 3px solid var(--win-accent);
  color: var(--win-text-secondary);
  background: var(--win-control-alt);
  border-radius: 0 var(--win-radius-control) var(--win-radius-control) 0;
}

.sp-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.sp-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--win-text-secondary);
}
.btn-sm {
  padding: 0 12px;
  white-space: nowrap;
}

/* ===== 报告主体与目录布局 ===== */
.report-layout {
  display: grid;
  grid-template-columns: 1fr;
  gap: 16px;
  align-items: start;
}
.toc-sidebar {
  display: none;
}
.toc-mobile {
  overflow: hidden;
}
.toc-mobile-trigger {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  background: transparent;
  border: none;
  color: var(--win-text-primary);
  cursor: pointer;
  font-family: inherit;
}
.toc-mobile-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 260px;
  overflow-y: auto;
  padding: 4px 8px 10px;
  border-top: 1px solid var(--win-stroke);
}
.toc-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--win-text-secondary);
  padding: 2px 8px 8px;
  margin-bottom: 6px;
  border-bottom: 1px solid var(--win-stroke);
}
.toc-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.toc-link {
  display: block;
  width: 100%;
  text-align: left;
  padding: 5px 8px;
  border-radius: var(--win-radius-control);
  border: none;
  background: transparent;
  color: var(--win-text-secondary);
  font-size: 12.5px;
  line-height: 1.4;
  font-family: inherit;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: background var(--win-duration-fast) var(--win-ease),
              color var(--win-duration-fast) var(--win-ease);
}
.toc-link:hover {
  background: var(--win-control-hover);
  color: var(--win-text-primary);
}
.toc-link.is-active {
  background: var(--win-accent-soft);
  color: var(--win-accent);
  font-weight: 600;
}

@container (min-width: 920px) {
  .report-layout.has-toc {
    grid-template-columns: 220px minmax(0, 1fr);
  }
  .toc-mobile {
    display: none;
  }
  .toc-sidebar {
    display: block;
    position: sticky;
    top: 0;
    max-height: calc(100vh - 140px);
    overflow-y: auto;
  }
}

.report-article {
  min-width: 0;
}
.report-prose {
  max-width: 72ch;
  margin: 0 auto;
  font-size: 14.5px;
  line-height: 1.78;
}
.report-prose :deep(.md-h1),
.report-prose :deep(.md-h2),
.report-prose :deep(.md-h3) {
  scroll-margin-top: 16px;
}

/* ===== 写作风格量化概览（平铺小格，无嵌套阴影） ===== */
.style-section {
  container-type: inline-size;
}
.style-metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(128px, 1fr));
  gap: 8px;
}
.style-metric-tile {
  padding: 10px 12px;
}
.style-metric-label {
  font-size: 12px;
  color: var(--win-text-muted);
}
.style-metric-value {
  font-size: 15px;
  font-weight: 600;
  color: var(--win-text-primary);
  margin-top: 2px;
  font-variant-numeric: tabular-nums;
}

/* ===== 细化进度条（4 阶段粒度）===== */
.summary-progress {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  border-radius: var(--win-radius-container);
  background: var(--win-layer);
  border: 1px solid var(--win-stroke);
}

.step-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}
@container (max-width: 560px) {
  .step-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
.step {
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  min-height: 44px;
  padding: 6px 10px;
  border-radius: var(--win-radius-control);
  font-size: 12px;
  border: 1px solid var(--win-stroke);
  background: var(--win-control-alt);
  color: var(--win-text-muted);
  transition: background var(--win-duration-fast) var(--win-ease), border-color var(--win-duration-fast) var(--win-ease), color var(--win-duration-fast) var(--win-ease);
}
.step-head {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  max-width: 100%;
  min-width: 0;
}
.step-sub {
  font-size: 11px;
  line-height: 1.25;
  opacity: 0.9;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}
.step-mini-bar {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 2px;
  background: transparent;
}
.step-mini-fill {
  height: 100%;
  background: var(--win-accent);
  transition: width 0.35s var(--win-ease);
}
.step-dot {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
  background: var(--win-control-hover);
  color: var(--win-text-secondary);
}
.step-label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.step.active {
  background: var(--win-accent-soft);
  border-color: var(--win-accent);
  color: var(--win-text-primary);
}
.step.active .step-dot {
  background: var(--win-accent-fill);
  color: var(--win-accent-fg);
}
.step.done {
  background: var(--win-success-bg);
  border-color: var(--win-success);
  color: var(--win-success);
}
.step.done .step-dot {
  background: var(--win-success);
  color: var(--win-success-fg);
}

.bar {
  height: 4px;
  border-radius: var(--win-radius-pill);
  background: var(--win-control-alt);
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  border-radius: var(--win-radius-pill);
  background: var(--win-accent);
  transition: width 0.45s var(--win-ease);
}

.progress-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--win-text-secondary);
}
.pm-phase {
  font-weight: 600;
  color: var(--win-text-primary);
}
.pm-sub {
  color: var(--win-text-secondary);
}
.pm-ok {
  color: var(--win-success);
}
.pm-warn {
  color: var(--win-danger);
}
.pm-time {
  margin-left: auto;
  color: var(--win-text-muted);
}
.pm-concurrency {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  border-radius: var(--win-radius-pill);
  background: var(--win-accent-soft);
  color: var(--win-accent);
  font-size: 11px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.pm-error {
  font-size: 12px;
  color: var(--win-danger);
}
.pm-done-note {
  font-size: 12px;
  color: var(--win-text-secondary);
}
.summary-telemetry {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 2px;
}
</style>
