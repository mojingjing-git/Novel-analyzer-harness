/**
 * QueuePage WS 消息状态机的纯函数层（从 QueuePage.vue onMessage 原样抽取）
 *
 * 设计与 laneRegistry/summaryLanes 一致：所有函数接收当前状态（Map/数组）、
 * 返回新引用（Vue 响应式要求 Map 引用替换才触发更新），不做任何 IO / 组件耦合，
 * 可被 vitest 直接覆盖。语义锁定 2026-08-26 H17 Phase 3 V2 的一批修复：
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
export const RATE_HISTORY_MAX = 30
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
  // H17 P3 V2 修复（2026-08-26）：清理已完成 block 的速率，
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

/** 实时 token 增量 → 块级累计（取最大非相加）+ per-block 最新速率 + 全局 sparkline 历史 */
export function applyTokenDelta(
  tokenByBlock: Map<number, { outputTokens: number; lastTokenAt: number }>,
  rateByBlock: Map<number, number>,
  rateHistory: number[],
  p: WSTokenDeltaPayload,
  nowMs: number,
): { tokenByBlock: Map<number, { outputTokens: number; lastTokenAt: number }>; rateByBlock: Map<number, number>; rateHistory: number[] } {
  const d = p.delta
  const blockId = Number(p.unit_idx ?? -1)
  const outTokens = Number(d.output_tokens ?? 0)
  const rate = Number(d.rate_tokens_per_sec ?? 0)
  const nextTokens = new Map(tokenByBlock)
  const nextRate = new Map(rateByBlock)
  if (blockId > 0) {
    const cur = nextTokens.get(blockId) || { outputTokens: 0, lastTokenAt: 0 }
    cur.outputTokens = Math.max(cur.outputTokens, outTokens)  // 累计取最大
    cur.lastTokenAt = nowMs
    nextTokens.set(blockId, cur)
    // 注意：rate 可能 < 0 / = 0（停止 / 出错），也照更新以反映"该 block 当前不输出"
    nextRate.set(blockId, rate)
  }
  let nextHistory = rateHistory
  if (rate > 0) {
    nextHistory = [...rateHistory, rate].slice(-RATE_HISTORY_MAX)
  }
  return { tokenByBlock: nextTokens, rateByBlock: nextRate, rateHistory: nextHistory }
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
} {
  const a = sessionStats?.analysis
  const s = sessionStats?.summary
  const input = (catTotal(a?.categories, 'input_tokens') || 0) + (s?.input_tokens || 0)
  const output = (catTotal(a?.categories, 'output_tokens') || 0) + (s?.output_tokens || 0)
  const cached = a?.cached_tokens || 0
  const aInput = catTotal(a?.categories, 'input_tokens') || 0
  const hitRate = aInput + cached > 0 ? (cached / (aInput + cached)) * 100 : 0
  return { input, output, cached, hitRate, total: input + output + cached }
}

// ============ 队列操作 ============

/**
 * P2 修复：删除时以名称实时解析索引，避免轮询/WS 重排后按旧索引删错书。
 * 找不到返回 -1（调用方只 warn 不删）。
 */
export function resolveDeleteIndex(items: QueueItemDto[] | undefined, name: string): number {
  return items?.findIndex((i) => i.name === name) ?? -1
}

/** 多书混排修复：进度换算取正在运行那一项的 block_size，而非首项 */
export function pickRunningBlockSize(running: boolean, items: QueueItemDto[] | undefined): number {
  if (!running || !items?.length) return 1
  const runningItem = items.find((i) => i.status === 'running') || items[0]
  return runningItem?.block_size || 1
}
