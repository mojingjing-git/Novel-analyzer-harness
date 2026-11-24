/**
 * QueuePage WS 消息状态机的纯函数层（对应 QueuePage.vue 的 onMessage）
 *
 * 设计与 laneRegistry/summaryLanes 一致：所有函数接收当前状态（Map/数组）、
 * 返回新引用（Vue 响应式要求 Map 引用替换才触发更新），不做任何 IO / 组件耦合，
 * 可被 vitest 直接覆盖。语义要点：
 *   - block_done 必须清理 rateByBlock（否则聚合速率把已结束块的尾速算进去）
 *   - token 累计取 Math.max（非相加——后端 delta 是累计值不是增量）
 *   - finished FIFO 上限、发现流 200 条上限
 */
import { normalizeRange } from './laneRegistry'
import type {
  WSBlockStartPayload,
  WSBlockDonePayload,
  WSDiscoveryPayload,
  WSTokenDeltaPayload,
} from '../api/useProgressSocket'
import type { QueueItemDto, SessionTokenStatsResponse, TokenCategory } from '../api/client'

// ============ 车道数据结构（与 RunDashboard.vue 导出的接口结构兼容）============

export interface FeedActiveBlock {
  range: string
  startedAt: number
}
export interface FeedFinishedBlock {
  ok: boolean
  range: string
}
export interface FeedDiscoveryItem {
  id: number
  ts: number
  events: number
  foreshadows: string[]
  characters: string[]
  unresolved: string[]
  highlighted: boolean
}

export const FINISHED_BLOCKS_MAX = 50
export const RATE_HISTORY_MAX = 60
export const DISCOVERIES_MAX = 200

// ============ block_start / block_done：车道生命周期 ============

/** 块开始 → 活跃块登记。blockId ≤ 0 时原样返回（与组件原逻辑一致不重建） */
export function applyBlockStart(
  active: Map<number, FeedActiveBlock>,
  p: WSBlockStartPayload,
  nowMs: number,
): Map<number, FeedActiveBlock> {
  const blockId = Number(p.chapter ?? 0)
  if (blockId > 0) {
    const next = new Map(active)
    next.set(blockId, {
      range: normalizeRange(String(p.range ?? p.message ?? '')) || `block ${blockId}`,
      startedAt: Number(p.ts ?? nowMs / 1000) * 1000,
    })
    return next
  }
  return active
}

/** 块完成 → 移出活跃 + 清理速率 + 登记 finished（FIFO 上限）。三张 Map 引用一并返回 */
export function applyBlockDone(
  active: Map<number, FeedActiveBlock>,
  finished: Map<number, FeedFinishedBlock>,
  rateByBlock: Map<number, number>,
  p: WSBlockDonePayload,
): { active: Map<number, FeedActiveBlock>; finished: Map<number, FeedFinishedBlock>; rateByBlock: Map<number, number> } {
  const blockId = Number(p.chapter ?? 0)
  if (blockId <= 0) return { active, finished, rateByBlock }
  const nextActive = new Map(active)
  nextActive.delete(blockId)
  // 清理已完成 block 的速率，
  // 否则 aggregateRate 会把刚结束 block 的尾速算进"当前聚合"
  const nextRate = new Map(rateByBlock)
  nextRate.delete(blockId)
  const nextFinished = new Map(finished)
  nextFinished.set(blockId, {
    ok: Boolean(p.ok),
    range: normalizeRange(String(p.range ?? p.message ?? '')) || `block ${blockId}`,
  })
  // 限制大小：超过 FINISHED_BLOCKS_MAX 删最早的（Map 插入序）
  if (nextFinished.size > FINISHED_BLOCKS_MAX) {
    const firstKey = nextFinished.keys().next().value
    if (firstKey !== undefined) nextFinished.delete(firstKey)
  }
  return { active: nextActive, finished: nextFinished, rateByBlock: nextRate }
}

// ============ token_delta：块级累计 + 速率 ============

export interface BlockTokenInfo {
  outputTokens: number
  lastTokenAt: number
  isThinking?: boolean
}

/** 是否属于最终总结阶段的流式 token_delta */
export function isSummaryTokenDelta(p: WSTokenDeltaPayload): boolean {
  return Boolean(p.context && p.context.startsWith('summary_phase_'))
}

/**
 * 转换 token_delta 的 blockId：
 * - context="analysis": 直接使用 unit_idx 章节号
 * - context="summary_phase_*": 映射到 900000+ 总结车道 ID，与 summaryLanes.ts 契约对齐
 */
export function resolvePayloadBlockId(p: WSTokenDeltaPayload): number {
  const context = p.context ?? 'analysis'
  const rawIdx = Number(p.unit_idx ?? -1)

  if (context === 'analysis') {
    return rawIdx
  }
  if (context === 'summary_phase_1') {
    return 900000 + (rawIdx > 0 ? rawIdx : 1)
  }
  if (context === 'summary_phase_1_recon') {
    return 905000 + (rawIdx > 0 ? rawIdx : 1)
  }
  if (context === 'summary_phase_2') {
    return 910000 + (rawIdx > 0 ? rawIdx : 1)
  }
  if (context === 'summary_phase_3') {
    return 920001
  }
  if (context === 'summary_phase_4') {
    return 930000 + (rawIdx > 0 ? rawIdx : 1)
  }
  return rawIdx
}

/** 向非响应式缓冲区写入单条 token_delta（供 1 秒统一批提交前收集最新状态） */
export function upsertTokenDeltaBuffer(
  tokenBuf: Map<number, BlockTokenInfo>,
  rateBuf: Map<number, number>,
  p: WSTokenDeltaPayload,
  nowMs: number,
): void {
  const d = p.delta
  const blockId = resolvePayloadBlockId(p)
  if (blockId < 0) return
  const outTokens = Number(d.output_tokens ?? 0)
  const rate = Number(d.rate_tokens_per_sec ?? 0)
  const isThinking = Boolean(d.is_thinking)
  const prev = tokenBuf.get(blockId)
  tokenBuf.set(blockId, {
    outputTokens: Math.max(prev?.outputTokens ?? 0, outTokens),
    lastTokenAt: nowMs,
    isThinking,
  })
  rateBuf.set(blockId, rate)
}

/** 实时 token 增量 → 块级累计（取最大非相加）+ per-block 最新速率；速率历史改由 1 秒采样生成 */
export function applyTokenDelta(
  tokenByBlock: Map<number, BlockTokenInfo>,
  rateByBlock: Map<number, number>,
  rateHistory: number[],
  p: WSTokenDeltaPayload,
  nowMs: number,
): { tokenByBlock: Map<number, BlockTokenInfo>; rateByBlock: Map<number, number>; rateHistory: number[] } {
  const nextTokens = new Map(tokenByBlock)
  const nextRate = new Map(rateByBlock)
  upsertTokenDeltaBuffer(nextTokens, nextRate, p, nowMs)
  return { tokenByBlock: nextTokens, rateByBlock: nextRate, rateHistory }
}

/** 1 秒定时采样全系统汇总吞吐：轻度 EMA 平滑（alpha=0.35）并保留最近 maxLen 个点 */
export function sampleRateHistory(
  history: number[],
  aggregateRateValue: number,
  alpha = 0.35,
  maxLen = RATE_HISTORY_MAX,
): number[] {
  const raw = Number.isFinite(aggregateRateValue) ? Math.max(0, aggregateRateValue) : 0
  if (history.length === 0) return [raw]
  const prev = history[history.length - 1]
  const next = prev * (1 - alpha) + raw * alpha
  return [...history, next].slice(-maxLen)
}

/** 阶梯化图表刻度上限（避免 Y 轴随瞬时微小波动剧烈跳变） */
export function calcChartCeiling(values: number[]): number {
  const peak = values.reduce((m, v) => (Number.isFinite(v) && v > m ? v : m), 0)
  if (peak <= 50) return 50
  if (peak <= 100) return 100
  if (peak <= 250) return Math.ceil(peak / 50) * 50
  if (peak <= 1000) return Math.ceil(peak / 100) * 100
  return Math.ceil(peak / 250) * 250
}

export interface LogScaleTick {
  value: number
  label: string
  y: number
  yPct: number
}

/** 对数坐标刻度上限（10 的幂次：100 / 1000 / 10000 / ...） */
export function calcLogChartCeiling(values: number[]): number {
  const peak = values.reduce((m, v) => (Number.isFinite(v) && v > m ? v : m), 0)
  if (peak <= 100) return 100
  if (peak <= 1000) return 1000
  if (peak <= 10000) return 10000
  return Math.pow(10, Math.ceil(Math.log10(peak)))
}

/** 将速率值映射为图表 SVG 纵坐标（支持线性与 log10(1 + v) 对数映射） */
export function mapRateToChartY(
  value: number,
  ceiling: number,
  height: number,
  scale: 'linear' | 'log' = 'linear',
): number {
  const safeCeil = Math.max(ceiling, 1)
  const usableH = Math.max(height - 5, 1)
  const val = Number.isFinite(value) ? Math.max(0, value) : 0
  const ratio = scale === 'log'
    ? Math.min(1, Math.log10(1 + val) / Math.log10(1 + safeCeil))
    : Math.min(1, val / safeCeil)
  return Math.max(2, Math.min(height - 2, height - 2 - ratio * usableH))
}

export interface SmoothChartPoint {
  x: number
  y: number
  value: number
}

/** 根据等距时间点序列生成三次贝塞尔平滑路径与面积闭合路径 */
export function buildSmoothAreaPaths(
  values: number[],
  width: number,
  height: number,
  ceiling: number,
  scale: 'linear' | 'log' = 'linear',
): { line: string; area: string; points: SmoothChartPoint[] } {
  if (values.length < 2 || width <= 0 || height <= 0) {
    return { line: '', area: '', points: [] }
  }
  const safeCeil = Math.max(ceiling, 1)
  const points: SmoothChartPoint[] = values.map((v, i) => {
    const val = Number.isFinite(v) ? Math.max(0, v) : 0
    const x = (i / (values.length - 1)) * width
    const y = mapRateToChartY(val, safeCeil, height, scale)
    return { x, y, value: val }
  })

  let line = `M ${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[i]
    const p1 = points[i + 1]
    const cx = ((p0.x + p1.x) / 2).toFixed(1)
    line += ` C ${cx} ${p0.y.toFixed(1)}, ${cx} ${p1.y.toFixed(1)}, ${p1.x.toFixed(1)} ${p1.y.toFixed(1)}`
  }
  const area = `${line} L ${width.toFixed(1)} ${height.toFixed(1)} L 0 ${height.toFixed(1)} Z`
  return { line, area, points }
}

/** 生成对数 Y 轴刻度尺条目（自上而下最多 4 档） */
export function buildLogScaleTicks(ceiling: number, height: number): LogScaleTick[] {
  const safeCeil = Math.max(ceiling, 100)
  const tickValues = safeCeil <= 100
    ? [100, 10, 0]
    : safeCeil <= 1000
      ? [1000, 100, 10, 0]
      : [safeCeil, safeCeil / 10, safeCeil / 100, 0]
  return tickValues.map((v) => {
    const label = v >= 1000 ? `${v / 1000}k` : String(v)
    const y = mapRateToChartY(v, safeCeil, height, 'log')
    return {
      value: v,
      label,
      y,
      yPct: (y / height) * 100,
    }
  })
}

// ============ discovery：发现流 append-only ============

/** 发现流追加（200 条上限）。highlighted 由新伏笔/新人物非空派生 */
export function appendDiscovery(
  items: FeedDiscoveryItem[],
  p: WSDiscoveryPayload,
  nextId: number,
  nowMs: number,
): { items: FeedDiscoveryItem[]; nextId: number } {
  const item: FeedDiscoveryItem = {
    id: nextId,
    ts: nowMs,
    events: Number(p.events ?? 0),
    foreshadows: p.foreshadows ?? [],
    characters: p.characters ?? [],
    unresolved: p.unresolved ?? [],
    highlighted: (p.foreshadows?.length ?? 0) > 0 || (p.characters?.length ?? 0) > 0,
  }
  return { items: [...items, item].slice(-DISCOVERIES_MAX), nextId: nextId + 1 }
}

// ============ 派生值 ============

/** 活跃块聚合速率 = sum(活跃 block 的 rateByBlock)，已结束块不参与 */
export function aggregateRate(
  rateByBlock: Map<number, number>,
  activeBlocks: Map<number, unknown>,
): number {
  let total = 0
  for (const [blockId, rate] of rateByBlock.entries()) {
    if (activeBlocks.has(blockId)) {
      total += rate
    }
  }
  return total
}

/** token 数字格式化：0 → '0'，≥1e6 → M（2 位小数），≥1e3 → K（1 位小数） */
export function fmt(n: number): string {
  if (!n) return '0'
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return String(n)
}

export const fmtPercent = (n: number) => n.toFixed(1) + '%'

/** 分类小计加总为总量（分析/总结共用） */
export function catTotal(
  cats: Record<string, TokenCategory> | undefined,
  key: 'input_tokens' | 'output_tokens',
): number {
  if (!cats) return 0
  return Object.values(cats).reduce((s, c) => s + (c[key] || 0), 0)
}

/** 本次窗口会话总量（分析 + 总结）。命中率只基于分析侧计算（总结侧未记录缓存命中，混入会低估） */
export function summarizeSession(sessionStats: SessionTokenStatsResponse | null): {
  input: number; output: number; cached: number; hitRate: number; total: number
  cost: SessionTokenStatsResponse['cost'] | null
} {
  const a = sessionStats?.analysis
  const s = sessionStats?.summary
  const input = (catTotal(a?.categories, 'input_tokens') || 0) + (s?.input_tokens || 0)
  const output = (catTotal(a?.categories, 'output_tokens') || 0) + (s?.output_tokens || 0)
  const cached = a?.cached_tokens || 0
  const aInput = catTotal(a?.categories, 'input_tokens') || 0
  const hitRate = aInput + cached > 0 ? (cached / (aInput + cached)) * 100 : 0
  return { input, output, cached, hitRate, total: input + output + cached, cost: sessionStats?.cost ?? null }
}

/**
 * 费用 chip 文案：models.dev 挂牌价估算（后端算好，前端只格式化）。
 * usd=null 表示全部用量都未定价（自定义模型/网关特有命名）——
 * 显示"未定价"比误导性的 ~$0.00 诚实（AGY 复审 R-4）。
 */
export function formatSessionCost(
  cost: SessionTokenStatsResponse['cost'] | null | undefined,
): string {
  if (!cost) return ''
  if (cost.usd === null || cost.usd === undefined) return '未定价'
  return `~$${cost.usd.toFixed(2)}`
}

// ============ 队列操作 ============

/**
 * 删除时以名称实时解析索引，避免轮询/WS 重排后按旧索引删错书。
 * 找不到返回 -1（调用方只 warn 不删）。
 */
export function resolveDeleteIndex(items: QueueItemDto[] | undefined, name: string): number {
  return items?.findIndex((i) => i.name === name) ?? -1
}

/** 多书混排：进度换算取正在运行那一项的 block_size，而非首项 */
export function pickRunningBlockSize(running: boolean, items: QueueItemDto[] | undefined): number {
  if (!running || !items?.length) return 1
  const runningItem = items.find((i) => i.status === 'running') || items[0]
  return runningItem?.block_size || 1
}
