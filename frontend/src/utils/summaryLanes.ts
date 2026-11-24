// 最终总结阶段 → LaneView 车道状态管理
// 支持两类消息驱动：
// 1. 真实并发槽打点事件：lane_start / lane_done（后端获得并发槽后真实入槽、释放前离槽）
// 2. 兼容回退（real_lanes 未开启时的旧消息格式）：由 phase / batch_done 合成阶段块
//
// 车道 ID 区间（避开分析阶段 1-10000 章节 ID）：
//   900001-900999 : 分卷摘要（summary_phase_1）
//   905001-905999 : 伏笔调和（summary_phase_1_recon）
//   910001-919999 : 伏笔复检（summary_phase_2）
//   920001        : 写作风格分析（summary_phase_3）
//   930001-930003 : 最终报告 / 人物图谱 / 世界图谱（summary_phase_4）

import type { WSSummaryProgressPayload } from '../api/useProgressSocket'
import type { SummaryInflightLane } from '../api/client'

const SUMMARY_FINISHED_MAX = 50

export interface SummaryLaneBlock {
  range: string
  startedAt: number
}

export type SummaryLaneBlocks = Map<number, SummaryLaneBlock>

export type SummaryLaneFinished = Map<number, { ok: boolean; range: string }>

function trimFinished(finished: SummaryLaneFinished) {
  while (finished.size > SUMMARY_FINISHED_MAX) {
    const firstKey = finished.keys().next().value
    if (firstKey === undefined) break
    finished.delete(firstKey)
  }
}

function belongsToPhase(laneId: number, phase: string): boolean {
  if (phase === 'batch') return (laneId >= 900000 && laneId < 910000) || laneId === 920001
  if (phase === 'recheck') return laneId >= 910000 && laneId < 920000
  if (phase === 'style') return laneId === 920001
  if (phase === 'report') return laneId >= 930000
  return false
}

/**
 * 用后端 /api/summary/status 的 inflight_lanes 快照对账在途总结车道。
 * 无变化时返回原 Map 引用，避免多余渲染。
 */
export function reconcileSummaryLanes(
  active: SummaryLaneBlocks,
  inflight: SummaryInflightLane[] | undefined,
): SummaryLaneBlocks {
  const list = Array.isArray(inflight) ? inflight : []
  const next: SummaryLaneBlocks = new Map()
  for (const lane of list) {
    const id = Number(lane?.id ?? 0)
    if (id <= 0) continue
    next.set(id, {
      range: String(lane.label || `任务 #${id}`),
      startedAt: Math.round(Number(lane.started_at || 0) * 1000),
    })
  }
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

/**
 * 处理一条 summary_progress 事件，更新 active/finished Maps。
 * 注意：调用者负责保证返回的 Map 引用被替换为 new Map() 触发响应式。
 */
export function applySummaryProgress(
  p: WSSummaryProgressPayload,
  active: SummaryLaneBlocks,
  finished: SummaryLaneFinished,
): { changed: boolean; newActive: SummaryLaneBlocks; newFinished: SummaryLaneFinished } {
  const ptype = String(p.type ?? '')
  const phase = String(p.phase ?? '')
  const totalBatches = Number(p.total_batches ?? 0)
  const batchIdx = Number(p.batch ?? 0)

  // 真实并发槽入槽
  if (ptype === 'lane_start') {
    const laneId = Number(p.lane_id ?? 0)
    if (laneId > 0) {
      const next = new Map(active)
      const nextFinished = new Map(finished)
      nextFinished.delete(laneId)
      const tsMs = p.ts ? Math.round(Number(p.ts) * 1000) : Date.now()
      next.set(laneId, {
        range: String(p.label ?? `任务 #${laneId}`),
        startedAt: tsMs,
      })
      return { changed: true, newActive: next, newFinished: nextFinished }
    }
    return { changed: false, newActive: active, newFinished: finished }
  }

  // 真实并发槽离槽
  if (ptype === 'lane_done') {
    const laneId = Number(p.lane_id ?? 0)
    if (laneId > 0) {
      const next = new Map(active)
      const prev = next.get(laneId)
      next.delete(laneId)
      const nextFinished = new Map(finished)
      nextFinished.delete(laneId)
      nextFinished.set(laneId, {
        ok: p.ok !== false,
        range: String(p.label ?? prev?.range ?? `任务 #${laneId}`),
      })
      trimFinished(nextFinished)
      return { changed: true, newActive: next, newFinished: nextFinished }
    }
    return { changed: false, newActive: active, newFinished: finished }
  }

  // 阶段切换
  if (ptype === 'phase' && (phase === 'batch' || phase === 'recheck' || phase === 'style' || phase === 'report')) {
    if (p.real_lanes) {
      // 真实车道模式下仅清理上一阶段的残留条目，避免跨阶段僵尸块残留；实际车道由 lane_start/lane_done 驱动
      let moved = false
      const next = new Map(active)
      const nextFinished = new Map(finished)
      for (const [id, info] of next) {
        if (id >= 900000 && !belongsToPhase(id, phase)) {
          nextFinished.set(id, { ok: true, range: info.range })
          next.delete(id)
          moved = true
        }
      }
      if (!moved) return { changed: false, newActive: active, newFinished: finished }
      trimFinished(nextFinished)
      return { changed: true, newActive: next, newFinished: nextFinished }
    }

    const now = Date.now()
    const next = new Map(active)
    const nextFinished = new Map(finished)
    // 上一阶段残留的合成块（id ≥ 900000 是总结阶段合成块；分析阶段 < 900000 不动）
    for (const [id, info] of next) {
      if (id >= 900000) {
        nextFinished.set(id, { ok: true, range: info.range })
        next.delete(id)
      }
    }
    if (phase === 'batch' && totalBatches > 0) {
      for (let i = 1; i <= totalBatches; i++) {
        next.set(900000 + i, { range: `卷${i}/${totalBatches}`, startedAt: now })
      }
    } else if (phase === 'recheck' && totalBatches > 0) {
      for (let i = 1; i <= totalBatches; i++) {
        next.set(910000 + i, { range: `伏笔复检批${i}/${totalBatches}`, startedAt: now })
      }
    } else if (phase === 'style') {
      next.set(920001, { range: '写作风格分析', startedAt: now })
    } else if (phase === 'report') {
      next.set(930001, { range: '生成全书脉络报告', startedAt: now })
    }
    trimFinished(nextFinished)
    return { changed: true, newActive: next, newFinished: nextFinished }
  }

  // 兼容旧版 batch_done / batch_failed（real_lanes 下若未对应合成块则无动作）
  if (ptype === 'batch_done' || ptype === 'batch_failed') {
    if (batchIdx > 0) {
      const blockId = 900000 + batchIdx
      if (active.has(blockId)) {
        const next = new Map(active)
        const nextFinished = new Map(finished)
        next.delete(blockId)
        const totalLabel = totalBatches > 0 ? `${batchIdx}/${totalBatches}` : `${batchIdx}`
        nextFinished.set(blockId, {
          ok: ptype === 'batch_done',
          range: `卷${totalLabel}`,
        })
        trimFinished(nextFinished)
        return { changed: true, newActive: next, newFinished: nextFinished }
      }
    }
  }

  // 整个总结完成：兜底标 done
  if (ptype === 'complete') {
    if (active.size > 0) {
      const next = new Map(active)
      const nextFinished = new Map(finished)
      for (const [id, info] of next) {
        if (id >= 900000) {
          nextFinished.set(id, { ok: true, range: info.range })
          next.delete(id)
        }
      }
      trimFinished(nextFinished)
      return { changed: true, newActive: next, newFinished: nextFinished }
    }
  }

  // 用户主动停止：把残留 ≥900000 活动块标为中断
  if (ptype === 'status' && typeof p.message === 'string' && p.message.includes('已停止')) {
    if (active.size > 0) {
      const next = new Map(active)
      const nextFinished = new Map(finished)
      for (const [id, info] of next) {
        if (id >= 900000) {
          nextFinished.set(id, { ok: false, range: info.range })
          next.delete(id)
        }
      }
      trimFinished(nextFinished)
      return { changed: true, newActive: next, newFinished: nextFinished }
    }
  }

  return { changed: false, newActive: active, newFinished: finished }
}
