/**
 * SummaryPage 最终总结进度的纯函数层（从组件 computed 原样抽取）。
 * 语义锁定：4 阶段加权总百分比（batch 0.7 / recheck 0.12 / style 0.08 / report 0.1）、
 * 失败/停止按已完成卷进度近似、非批次阶段进行中计该阶段权重的一半。
 */
import type { SummaryStatus } from '../api/client'

export const SUMMARY_PHASES = [
  { key: 'batch', label: '分卷分析 + 伏笔调和', weight: 0.7 },
  { key: 'recheck', label: '全书伏笔复检', weight: 0.12 },
  { key: 'style', label: '写作风格分析', weight: 0.08 },
  { key: 'report', label: '生成全书脉络报告', weight: 0.1 },
] as const

export const PHASE_LABELS: Record<string, string> = {
  starting: '准备中...',
  batch: '分卷分析 + 伏笔调和',
  recheck: '全书伏笔复检',
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

export function overallPctOf(s: SummaryStatus | null): number {
  if (!s) return 0
  if (s.phase === 'complete') return 100
  // 失败/停止：用已完成的卷进度近似（保住已取得的进度感）
  if ((s.phase === 'failed' || s.phase === 'stopped') && s.total_batches > 0) {
    return Math.round(SUMMARY_PHASES[0].weight * 100 * (s.batches_done / s.total_batches))
  }
  const i = SUMMARY_PHASES.findIndex((x) => x.key === s.phase)
  if (i < 0) return 0
  let pct = 0
  for (let k = 0; k < i; k++) pct += SUMMARY_PHASES[k].weight * 100
  if (s.phase === 'batch' && s.total_batches > 0) {
    pct += SUMMARY_PHASES[i].weight * 100 * (s.batches_done / s.total_batches)
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
  summary: '分卷摘要', reconciliation: '伏笔调和', recheck: '全书复检', style: '风格分析', final: '最终报告',
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
