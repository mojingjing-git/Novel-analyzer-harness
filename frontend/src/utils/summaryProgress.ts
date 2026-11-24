/**
 * SummaryPage 最终总结进度的纯函数层（对应组件的 computed）。
 * 语义锁定：4 阶段加权总百分比（batch 0.7 / recheck 0.12 / style 0.08 / report 0.1）、
 * 失败/停止按已完成卷进度近似、非批次阶段进行中计该阶段权重的一半。
 */
import type { SummaryStatus, RecheckLiveProgress } from '../api/client'

export const SUMMARY_PHASES = [
  { key: 'batch', label: '分卷分析 + 伏笔调和', weight: 0.7 },
  { key: 'recheck', label: '伏笔复检', weight: 0.12 },
  { key: 'style', label: '写作风格分析', weight: 0.08 },
  { key: 'report', label: '生成全书脉络报告', weight: 0.1 },
] as const

export const PHASE_LABELS: Record<string, string> = {
  starting: '准备中...',
  batch: '分卷分析 + 伏笔调和',
  recheck: '伏笔复检',
  style: '写作风格分析',
  report: '生成全书脉络报告',
  complete: '完成',
  stopped: '已停止',
  failed: '失败',
  idle: '空闲',
}

export function phaseLabelOf(phase: string | undefined): string {
  return PHASE_LABELS[phase || 'idle'] || '准备中'
}

/** 阶段序号：idle/starting → -1；complete → SUMMARY_PHASES.length；failed/stopped → -1（交给 banner 提示） */
export function phaseIndexOf(phase: string | undefined): number {
  if (!phase || phase === 'idle' || phase === 'starting') return -1
  if (phase === 'complete') return SUMMARY_PHASES.length // 全部完成
  const i = SUMMARY_PHASES.findIndex((x) => x.key === phase)
  if (i >= 0) return i
  return -1
}

export function stepClassOf(phaseIndex: number, i: number): string {
  if (phaseIndex > i) return 'done'
  if (phaseIndex === i) return 'active'
  return 'pending'
}

export function stage1FractionOf(s: SummaryStatus | null): number {
  if (!s || s.total_batches <= 0) return 0
  if (
    typeof s.summary_done === 'number'
    && typeof s.recon_done === 'number'
    && (s.summary_done > 0 || s.recon_done > 0 || s.batches_done === 0)
  ) {
    return Math.min(1, Math.max(0, (s.summary_done + s.recon_done) / (2 * s.total_batches)))
  }
  return Math.min(1, Math.max(0, s.batches_done / s.total_batches))
}

export function recheckPctOf(s: SummaryStatus | null): number | null {
  const rp = s?.recheck_progress
  if (!rp || typeof rp !== 'object' || !rp.stage || !(rp.stage_total > 0)) return null
  const frac = Math.min(1, Math.max(0, (rp.stage_done || 0) / rp.stage_total))
  const band = (lo: number, hi: number) => Math.round(lo + (hi - lo) * frac)
  switch (rp.stage) {
    case 'u1_index':
      return 2
    case 'u1_split':
      return band(0, 10)
    case 'u1_alias':
      return band(10, 18)
    case 'u1_route':
      return band(18, 40)
    case 'u3_hop': {
      const hop = rp.hop || 1
      if (hop === 2) return band(65, 76)
      if (hop >= 3) return band(76, 82)
      return band(40, 65)
    }
    case 'u4_audit':
      return band(82, 96)
    case 'u5_repair':
      return band(96, 100)
    case 'build_candidates':
      return band(0, 15)
    case 'recheck':
      return band(15, 100)
    default:
      return null
  }
}

export function recheckStageShortLabel(rp: RecheckLiveProgress): string {
  switch (rp.stage) {
    case 'u1_index':
      return '建索引'
    case 'u1_split':
      return '拆解'
    case 'u1_alias':
      return '别名'
    case 'u1_route':
      return '路由'
    case 'u3_hop':
      return (rp.hop || 1) === 1 ? '初判' : `深挖${rp.hop || 2}`
    case 'u4_audit':
      return '终审'
    case 'u5_repair':
      return '修复'
    case 'build_candidates':
      return '建证据'
    default:
      return '核对'
  }
}

function recheckStageFullLabel(rp: RecheckLiveProgress): string {
  switch (rp.stage) {
    case 'u1_index':
      return '索引准备'
    case 'u1_split':
      return '核心拆解'
    case 'u1_alias':
      return '别名词典'
    case 'u1_route':
      return '双向路由'
    case 'u3_hop': {
      const hop = rp.hop || 1
      return hop === 1 ? '初判 (1/3跳)' : `深挖 (${hop}/3跳)`
    }
    case 'u4_audit':
      return '四防终审'
    case 'u5_repair':
      return '引文修复'
    case 'build_candidates':
      return '构建证据'
    default:
      return '核对'
  }
}

export function recheckMetaTextOf(s: SummaryStatus | null): string {
  const rp = s?.recheck_progress
  if (rp && typeof rp === 'object' && rp.stage_total > 0) {
    const u = rp.unit || '条'
    const label = recheckStageFullLabel(rp)
    if (u === '批' && (rp.items_total ?? 0) > 0) {
      return `${label} ${rp.stage_done}/${rp.stage_total} 批 · 剩余 ${rp.remaining} 批（共 ${rp.items_total} 条待复检）`
    }
    return `${label} ${rp.stage_done}/${rp.stage_total} ${u} · 剩余 ${rp.remaining} ${u}`
  }
  if (rp && typeof rp === 'object' && (rp.items_total ?? 0) > 0) {
    return `共 ${rp.items_total} 条待复检`
  }
  return '正在准备复检索引…'
}

export function stepSubtextOf(
  s: SummaryStatus | null,
  phaseKey: string,
  stepIndex: number,
  currentPhaseIndex: number,
): string {
  if (!s) return ''
  const isActive = currentPhaseIndex === stepIndex
  const isDone = currentPhaseIndex > stepIndex
  if (phaseKey === 'batch') {
    if (isActive && s.total_batches > 0) {
      if (typeof s.summary_done === 'number' && typeof s.recon_done === 'number') {
        return `摘要 ${s.summary_done}/${s.total_batches} · 调和 ${s.recon_done}/${s.total_batches}`
      }
      return `卷 ${s.batches_done}/${s.total_batches} · ${batchPctOf(s)}%`
    }
    if (isDone && s.total_batches > 0) {
      return `${s.total_batches}/${s.total_batches} 卷`
    }
    return ''
  }
  if (phaseKey === 'recheck') {
    if (isActive) {
      const rp = s.recheck_progress
      if (rp && typeof rp === 'object' && rp.stage_total > 0) {
        const shortLabel = recheckStageShortLabel(rp)
        const u = rp.unit || '条'
        return `${shortLabel} ${rp.stage_done}/${rp.stage_total} · 剩${rp.remaining}${u}`
      }
      if (rp && typeof rp === 'object' && (rp.items_total ?? 0) > 0) {
        return `待复检 ${rp.items_total} 条`
      }
      return '准备中…'
    }
    if (isDone) {
      const acct = recheckAccountOf(s.recheck)
      if (acct && (acct.candidates_total > 0 || acct.checked > 0)) {
        return `已检 ${acct.checked}/${acct.candidates_total || acct.checked} 条`
      }
    }
    return ''
  }
  if (phaseKey === 'style') {
    return isActive ? '分析中…' : ''
  }
  if (phaseKey === 'report') {
    return isActive ? '生成中…' : ''
  }
  return ''
}

export function stepProgressPctOf(
  s: SummaryStatus | null,
  phaseKey: string,
  stepIndex: number,
  currentPhaseIndex: number,
): number | null {
  if (!s || currentPhaseIndex !== stepIndex) return null
  if (phaseKey === 'batch' && s.total_batches > 0) {
    return Math.round(stage1FractionOf(s) * 100)
  }
  if (phaseKey === 'recheck') {
    return recheckPctOf(s)
  }
  return null
}

export function overallPctOf(s: SummaryStatus | null): number {
  if (!s) return 0
  if (s.phase === 'complete') return 100
  // 失败/停止：用已完成的卷进度近似（保住已取得的进度感）
  if ((s.phase === 'failed' || s.phase === 'stopped') && s.total_batches > 0) {
    return Math.round(SUMMARY_PHASES[0].weight * 100 * stage1FractionOf(s))
  }
  const i = SUMMARY_PHASES.findIndex((x) => x.key === s.phase)
  if (i < 0) return 0
  let pct = 0
  for (let k = 0; k < i; k++) pct += SUMMARY_PHASES[k].weight * 100
  if (s.phase === 'batch' && s.total_batches > 0) {
    pct += SUMMARY_PHASES[i].weight * 100 * stage1FractionOf(s)
  } else if (s.phase === 'recheck') {
    const rpct = recheckPctOf(s)
    pct += rpct !== null ? SUMMARY_PHASES[i].weight * rpct : SUMMARY_PHASES[i].weight * 50
  } else {
    pct += SUMMARY_PHASES[i].weight * 50 // 非批次阶段进行中，计该阶段权重的一半
  }
  return Math.min(100, Math.round(pct))
}

export function batchPctOf(s: SummaryStatus | null): number {
  if (!s || !s.total_batches) return 0
  return Math.round((s.batches_done / s.total_batches) * 100)
}

/** 耗时文案：未开始 → ''；<60s → N秒；否则 N分M秒。nowSec 注入便于测试 */
export function elapsedTextOf(s: SummaryStatus | null, nowSec: number): string {
  if (!s || !s.started_at) return ''
  const end = s.finished_at || nowSec
  const secs = Math.max(0, Math.round(end - s.started_at))
  if (secs < 60) return `${secs}秒`
  return `${Math.floor(secs / 60)}分${secs % 60}秒`
}

const SUMMARY_TOKEN_LABELS: Record<string, string> = {
  summary: '分卷摘要', reconciliation: '伏笔调和', recheck: '伏笔复检', style: '风格分析', final: '最终报告', atlas: '全书图谱',
}

export function summaryTokenTextOf(s: SummaryStatus | null): string {
  const stats = s?.token_stats
  if (!stats) return ''
  const parts = Object.entries(SUMMARY_TOKEN_LABELS)
    .map(([k, label]) => {
      const v = stats[k]
      return v && (v.input_tokens || v.output_tokens) ? `${label} 入${v.input_tokens} 出${v.output_tokens}` : ''
    })
    .filter(Boolean)
  return parts.length ? `本次总结 token：${parts.join(' · ')}` : ''
}

export function showProgressOf(s: SummaryStatus | null): boolean {
  if (!s) return false
  if (s.running) return true
  return ['complete', 'failed', 'stopped'].includes(s.phase)
}

/** 思考控制 JSON 字符串 ↔ dict：空 dict → '{}'；坏 JSON 吞掉返回 {} */
export function dictToJsonString(v: Record<string, unknown> | undefined | null): string {
  if (!v || Object.keys(v).length === 0) return '{}'
  return JSON.stringify(v)
}

export function jsonStringToDict(s: string): Record<string, unknown> {
  try { return s === '{}' ? {} : JSON.parse(s) } catch { return {} }
}

// ============ 带证据复检（V3 / V2）的账：总结页结果卡 ============

export interface V3TierCounts {
  resolved_explicit: number
  resolved_implicit: number
  partially_resolved: number
  unresolved: number
}

export interface RecheckAccountView {
  status: string
  status_reason: string
  calls: number
  checked: number
  candidates_total: number
  supported: number
  insufficient: number
  conflict: number
  invalid: number
  not_checked: number
  dropped_no_anchor: number
  verdicts_file: string
  v3_tiers: V3TierCounts | null
}

/**
 * 从 summary/status 的 `recheck`（后端 outcome.summary() 的松散 dict）提取展示字段。
 * 防御式取值：后端升级/降级都不至于让页面崩——缺键按 0/空串处理；
 * `calls` 不存在视为"这轮没跑过复检"返回 null，不渲染结果卡。
 */
export function recheckAccountOf(r: Record<string, unknown> | undefined | null): RecheckAccountView | null {
  if (!r || typeof r !== 'object' || r.calls === undefined) return null
  const cov = (r.coverage && typeof r.coverage === 'object' ? r.coverage : {}) as Record<string, unknown>
  const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
  const str = (v: unknown): string => (typeof v === 'string' ? v : '')
  const rawTiers = cov.v3_tiers ?? r.v3_tiers
  const tiersObj = rawTiers && typeof rawTiers === 'object' ? (rawTiers as Record<string, unknown>) : null
  const v3_tiers: V3TierCounts | null = tiersObj
    ? {
        resolved_explicit: num(tiersObj.RESOLVED_EXPLICIT ?? tiersObj.resolved_explicit),
        resolved_implicit: num(tiersObj.RESOLVED_IMPLICIT ?? tiersObj.resolved_implicit),
        partially_resolved: num(tiersObj.PARTIALLY_RESOLVED ?? tiersObj.partially_resolved),
        unresolved: num(tiersObj.UNRESOLVED ?? tiersObj.unresolved),
      }
    : null
  return {
    status: str(r.status),
    status_reason: str(r.status_reason),
    calls: num(r.calls),
    checked: num(cov.checked),
    candidates_total: num(cov.candidates_total),
    supported: num(cov.supported),
    insufficient: num(cov.insufficient),
    conflict: num(cov.conflict),
    invalid: num(cov.invalid),
    not_checked: num(cov.not_checked),
    dropped_no_anchor: num(r.dropped_no_anchor),
    verdicts_file: str(r.verdicts_file),
    v3_tiers,
  }
}
