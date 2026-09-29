/**
 * summaryProgress 纯函数测试：最终总结 4 阶段加权进度/阶段序号/耗时文案/思考参数 JSON 转换。
 * 权重锁定：batch 0.7 / recheck 0.12 / style 0.08 / report 0.1。
 */
import { describe, it, expect } from 'vitest'
import {
  SUMMARY_PHASES, phaseLabelOf, phaseIndexOf, stepClassOf, overallPctOf,
  batchPctOf, elapsedTextOf, summaryTokenTextOf, showProgressOf,
  dictToJsonString, jsonStringToDict,
} from './summaryProgress'
import type { SummaryStatus } from '../api/client'

function mkStatus(partial: Partial<SummaryStatus>): SummaryStatus {
  return {
    running: false, book_id: '', phase: 'idle', batches_done: 0,
    total_batches: 0, error: '', started_at: 0, finished_at: 0, token_stats: {},
    ...partial,
  }
}

describe('summaryProgress.overallPctOf（4 阶段加权）', () => {
  it('无状态 → 0；complete → 100', () => {
    expect(overallPctOf(null)).toBe(0)
    expect(overallPctOf(mkStatus({ phase: 'complete' }))).toBe(100)
  })

  it('batch 阶段按批次比例内插：5/10 批 → 35%', () => {
    expect(overallPctOf(mkStatus({ phase: 'batch', batches_done: 5, total_batches: 10 }))).toBe(35)
  })

  it('batch 完成进入 recheck：70 + 权重一半(6) = 76%', () => {
    expect(overallPctOf(mkStatus({ phase: 'recheck' }))).toBe(76)
  })

  it('非批次阶段（style/report）进行中计该阶段权重一半', () => {
    expect(overallPctOf(mkStatus({ phase: 'style' }))).toBe(86)   // 70+12+8/2
    expect(overallPctOf(mkStatus({ phase: 'report' }))).toBe(95)  // 70+12+8+10/2
  })

  it('failed/stopped：按已完成卷比例近似，保住已取得的进度感', () => {
    expect(overallPctOf(mkStatus({ phase: 'failed', batches_done: 5, total_batches: 10 }))).toBe(35)
    expect(overallPctOf(mkStatus({ phase: 'stopped', batches_done: 10, total_batches: 10 }))).toBe(70)
    expect(overallPctOf(mkStatus({ phase: 'failed', total_batches: 0 }))).toBe(0)
  })

  it('未知阶段 → 0', () => {
    expect(overallPctOf(mkStatus({ phase: 'what' }))).toBe(0)
  })

  it('权重表本身：四阶段权重和为 1（改权重必须显式改这里）', () => {
    const total = SUMMARY_PHASES.reduce((s, p) => s + p.weight, 0)
    expect(total).toBeCloseTo(1, 10)
  })
})

describe('summaryProgress.phaseIndexOf / stepClassOf / phaseLabelOf', () => {
  it('idle/starting/failed/stopped → -1；complete → 阶段数；具名阶段 → 序号', () => {
    expect(phaseIndexOf(undefined)).toBe(-1)
    expect(phaseIndexOf('idle')).toBe(-1)
    expect(phaseIndexOf('starting')).toBe(-1)
    expect(phaseIndexOf('complete')).toBe(SUMMARY_PHASES.length)
    expect(phaseIndexOf('batch')).toBe(0)
    expect(phaseIndexOf('recheck')).toBe(1)
    expect(phaseIndexOf('failed')).toBe(-1)
    expect(phaseIndexOf('stopped')).toBe(-1)
  })

  it('stepClassOf：之前 done / 当前 active / 之后 pending', () => {
    expect(stepClassOf(2, 0)).toBe('done')
    expect(stepClassOf(2, 2)).toBe('active')
    expect(stepClassOf(2, 3)).toBe('pending')
  })

  it('phaseLabelOf：已知阶段出中文，未知回退「准备中」', () => {
    expect(phaseLabelOf('batch')).toBe('分卷分析 + 伏笔调和')
    expect(phaseLabelOf(undefined)).toBe('空闲')
    expect(phaseLabelOf('what')).toBe('准备中')
  })
})

describe('summaryProgress.batchPctOf / elapsedTextOf / showProgressOf / summaryTokenTextOf', () => {
  it('batchPctOf：无批次 → 0；5/10 → 50%', () => {
    expect(batchPctOf(null)).toBe(0)
    expect(batchPctOf(mkStatus({ total_batches: 0 }))).toBe(0)
    expect(batchPctOf(mkStatus({ batches_done: 5, total_batches: 10 }))).toBe(50)
  })

  it('elapsedTextOf：未开始空串；<60s 秒；≥60s 分秒；finished_at 优先于 now', () => {
    expect(elapsedTextOf(null, 1000)).toBe('')
    expect(elapsedTextOf(mkStatus({ started_at: 0 }), 1000)).toBe('')
    expect(elapsedTextOf(mkStatus({ started_at: 1000 }), 1045)).toBe('45秒')
    expect(elapsedTextOf(mkStatus({ started_at: 1000 }), 1125)).toBe('2分5秒')
    expect(elapsedTextOf(mkStatus({ started_at: 1000, finished_at: 1060 }), 99999)).toBe('1分0秒')
  })

  it('showProgressOf：running 恒真；complete/failed/stopped 保留展示；进行中但未 running 隐藏', () => {
    expect(showProgressOf(null)).toBe(false)
    expect(showProgressOf(mkStatus({ running: true, phase: 'batch' }))).toBe(true)
    expect(showProgressOf(mkStatus({ phase: 'complete' }))).toBe(true)
    expect(showProgressOf(mkStatus({ phase: 'failed' }))).toBe(true)
    expect(showProgressOf(mkStatus({ phase: 'batch' }))).toBe(false)
  })

  it('summaryTokenTextOf：仅列有消耗的阶段；无统计 → 空串', () => {
    expect(summaryTokenTextOf(null)).toBe('')
    expect(summaryTokenTextOf(mkStatus({ token_stats: {} }))).toBe('')
    expect(summaryTokenTextOf(mkStatus({
      token_stats: {
        summary: { input_tokens: 100, output_tokens: 50 },
        reconciliation: { input_tokens: 0, output_tokens: 0 },
      },
    }))).toBe('本次总结 token：分卷摘要 入100 出50')
  })
})

describe('summaryProgress 思考参数 JSON 转换', () => {
  it('dictToJsonString：空/undefined → \'{}\'；非空原样序列化', () => {
    expect(dictToJsonString({})).toBe('{}')
    expect(dictToJsonString(undefined)).toBe('{}')
    expect(dictToJsonString({ thinking: { type: 'disabled' } })).toBe('{"thinking":{"type":"disabled"}}')
  })

  it('jsonStringToDict：\'{}\' 直通；坏 JSON 吞掉返回 {}', () => {
    expect(jsonStringToDict('{}')).toEqual({})
    expect(jsonStringToDict('{"enable_thinking":false}')).toEqual({ enable_thinking: false })
    expect(jsonStringToDict('not json')).toEqual({})
  })

  it('往返一致', () => {
    const d = { reasoning_effort: 'none' }
    expect(jsonStringToDict(dictToJsonString(d))).toEqual(d)
  })
})
