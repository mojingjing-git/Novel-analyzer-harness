/**
 * 运行概览车道注册表自愈（车道堆叠修复）
 *
 * 问题：activeBlocks 由 WS block_start/block_done 增量记账，通道有损
 * （服务端队满丢最旧、断线无重放），丢一条 done = 永久僵尸车道；
 * 且 done 本身有消费滞后（rolling 等待），"start未done" 稳态 ~2.5 倍并发数。
 *
 * 两个纯函数（可独立 vitest）：
 * - gcActiveBlocks：超窗无 token 的僵尸车道回收（兜底自愈）
 * - reconcileActiveBlocks：用后端 inflight_blocks（信号量真实在途，零丢失）整体纠偏
 */

/** 总结合成车道 id 下界（summaryLanes.ts 以 900000+ 起编，不受分析对账管辖） */
export const SYNTHETIC_LANE_ID_BASE = 900000

export interface ActiveBlockEntry {
  range: string
  startedAt: number  // ms
}

export interface InflightBlock {
  chapter: number    // block_id（块首章号，与 block_start 的 chapter 同一编号空间）
  range: string
  started_at: number // 秒（后端 time.time()）
}

/** tokenByBlock 中 GC 关心的最小字段 */
export interface TokenInfo {
  lastTokenAt: number  // ms
}

/**
 * 防御旧后端 / 直接调 API 时混入的人类可读日志文案。
 * 正常路径后端已发结构化 range，本函数仅做无副作用兜底。
 */
export function normalizeRange(raw: string): string {
  const text = String(raw ?? '').trim()
  if (!text) return ''
  return text
    .replace(/^\[(?:补跑|重试)\]\s*/, '')
    .replace(/^开始分析\s*/, '')
    .replace(/\.\.\.\s*$/, '')
    .replace(/分析(?:完成|失败|异常)$/, '')
    .replace(/\s*内容审核拦截，已跳过$/, '')
    .trim()
}

/**
 * GC 僵尸活跃车道。返回 kept（保留的 Map，可能为原引用）与 gcIds（被回收的 id）。
 *
 * 回收条件（跳过 ≥900000 的总结合成车道，它们的存活由 summaryLanes 自己管理）：
 * - A 真卡死：elapsed > stallWarnSec 且收到过 token_delta 且最后 token 距今 > stallWarnSec
 *   （要求 has token：非流式模式/首 token 前的块无 delta，不能据此判死）
 * - B 硬上限：elapsed > hardTtlMs 无条件回收
 */
export function gcActiveBlocks(
  active: Map<number, ActiveBlockEntry>,
  tokenByBlock: Map<number, TokenInfo>,
  nowMs: number,
  stallWarnSec: number,
  hardTtlMs: number = 30 * 60 * 1000,
): { kept: Map<number, ActiveBlockEntry>; gcIds: number[] } {
  const warnMs = stallWarnSec * 1000
  const gcIds: number[] = []
  for (const [id, info] of active) {
    if (id >= SYNTHETIC_LANE_ID_BASE) continue
    const elapsed = nowMs - info.startedAt
    if (elapsed <= warnMs) continue
    const tk = tokenByBlock.get(id)
    const noTokenFor = tk ? nowMs - tk.lastTokenAt : Infinity
    if (tk && noTokenFor > warnMs) {
      gcIds.push(id)
    } else if (elapsed > hardTtlMs) {
      gcIds.push(id)
    }
  }
  if (gcIds.length === 0) return { kept: active, gcIds }
  const kept = new Map(active)
  for (const id of gcIds) kept.delete(id)
  return { kept, gcIds }
}

/**
 * 用后端 inflight_blocks 对账：整体替换分析块（id < 900000），
 * 保留总结合成车道（≥900000）与刚登记的新块（age < graceMs，
 * 防「fetch 后 block_start 才落 Map」竞态导致车道闪烁）。
 * 无变化时返回原引用，避免多余的反应式更新。
 */
export function reconcileActiveBlocks(
  active: Map<number, ActiveBlockEntry>,
  inflight: InflightBlock[],
  nowMs: number,
  graceMs: number = 15000,
  concurrency?: number,
): Map<number, ActiveBlockEntry> {
  const next = new Map<number, ActiveBlockEntry>()
  for (const b of inflight) {
    if (b.chapter < SYNTHETIC_LANE_ID_BASE) {
      // round：服务端秒(浮点)×1000 的抖动会让「无变化」判断失效
      next.set(b.chapter, { range: b.range, startedAt: Math.round(b.started_at * 1000) })
    }
  }
  for (const [id, info] of active) {
    if (id >= SYNTHETIC_LANE_ID_BASE || nowMs - info.startedAt < graceMs) {
      next.set(id, info)
    }
  }
  // 防御最后一层：即便对账源或宽限期逻辑异常，分析车道也不能显示超过并发池
  const analysisEntries = [...next.entries()]
    .filter(([id]) => id < SYNTHETIC_LANE_ID_BASE)
    .sort((a, b) => a[1].startedAt - b[1].startedAt)
  if (concurrency && concurrency > 0 && analysisEntries.length > concurrency) {
    const keepIds = new Set(analysisEntries.slice(0, concurrency).map(([id]) => id))
    for (const [id] of analysisEntries) {
      if (!keepIds.has(id)) next.delete(id)
    }
  }
  // 无变化判断：键集合一致且 range/startedAt 全等
  if (next.size === active.size) {
    let same = true
    for (const [id, info] of next) {
      const old = active.get(id)
      if (!old || old.range !== info.range || old.startedAt !== info.startedAt) {
        same = false
        break
      }
    }
    if (same) return active
  }
  return next
}
