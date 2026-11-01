/** 最终总结阶段 → 合成 LaneView 兼容事件 测试 */
import { describe, it, expect } from 'vitest'
import { applySummaryProgress } from './summaryLanes'

describe('applySummaryProgress 把 summary_progress 翻译成 LaneView 块', () => {
  it('phase=batch + total_batches=12：合成 12 个卷分析块（id 900001-900012）', () => {
    const active = new Map()
    const finished = new Map()
    const r = applySummaryProgress(
      { type: 'phase', phase: 'batch', total_batches: 12, message: '分卷' },
      active, finished,
    )
    expect(r.changed).toBe(true)
    expect(r.newActive.size).toBe(12)
    expect(r.newActive.get(900001)?.range).toBe('卷1/12')
    expect(r.newActive.get(900012)?.range).toBe('卷12/12')
    // 起始时间应是同一时刻（一次性合成）
    const t1 = r.newActive.get(900001)!.startedAt
    const t12 = r.newActive.get(900012)!.startedAt
    expect(t12 - t1).toBeLessThan(50)  // 50ms 内
  })

  it('phase=recheck + total_batches=5：合成 5 个伏笔复检块（id 910001-910005）', () => {
    const active = new Map()
    const finished = new Map()
    const r = applySummaryProgress(
      { type: 'phase', phase: 'recheck', total_batches: 5 },
      active, finished,
    )
    expect(r.changed).toBe(true)
    expect(r.newActive.size).toBe(5)
    expect(r.newActive.get(910001)?.range).toBe('伏笔复检批1/5')
    expect(r.newActive.get(910005)?.range).toBe('伏笔复检批5/5')
  })

  it('phase=style / report：各合 1 个块（id 920001 / 930001）', () => {
    const a1 = applySummaryProgress({ type: 'phase', phase: 'style' }, new Map(), new Map())
    expect(a1.newActive.get(920001)?.range).toBe('写作风格分析')
    expect(a1.newActive.size).toBe(1)

    const a2 = applySummaryProgress({ type: 'phase', phase: 'report' }, new Map(), new Map())
    expect(a2.newActive.get(930001)?.range).toBe('生成全书脉络报告')
    expect(a2.newActive.size).toBe(1)
  })

  it('batch_done：把对应合成块从 active 移到 finished（ok=true，range 带 /total）', () => {
    // 先合成 batch 阶段
    let active = new Map()
    let finished = new Map()
    const init = applySummaryProgress(
      { type: 'phase', phase: 'batch', total_batches: 12 },
      active, finished,
    )
    active = init.newActive
    finished = init.newFinished

    // 第 3 卷完成
    const r = applySummaryProgress(
      { type: 'batch_done', batch: 3, total_batches: 12, message: '卷3 完成' },
      active, finished,
    )
    expect(r.changed).toBe(true)
    expect(r.newActive.has(900003)).toBe(false)
    expect(r.newFinished.get(900003)?.ok).toBe(true)
    expect(r.newFinished.get(900003)?.range).toBe('卷3/12')
    // 其他 11 卷还在 active
    expect(r.newActive.size).toBe(11)
  })

  it('batch_failed：合成块移到 finished 但 ok=false', () => {
    let active = new Map()
    let finished = new Map()
    const init = applySummaryProgress(
      { type: 'phase', phase: 'batch', total_batches: 12 },
      active, finished,
    )
    active = init.newActive
    finished = init.newFinished

    const r = applySummaryProgress(
      { type: 'batch_failed', batch: 4, message: '卷4 LLM失败' },
      active, finished,
    )
    expect(r.changed).toBe(true)
    expect(r.newFinished.get(900004)?.ok).toBe(false)
    expect(r.newActive.has(900004)).toBe(false)
  })

  it('阶段切换：上一阶段残留的合成块（id ≥ 900000）移到 finished 带 ✓', () => {
    // batch 阶段跑了一些
    let active = new Map()
    let finished = new Map()
    const batchInit = applySummaryProgress(
      { type: 'phase', phase: 'batch', total_batches: 12 },
      active, finished,
    )
    active = batchInit.newActive
    finished = batchInit.newFinished
    // 3 卷完成
    const done3 = applySummaryProgress(
      { type: 'batch_done', batch: 1, total_batches: 12 }, active, finished,
    )
    active = done3.newActive
    finished = done3.newFinished
    const done2 = applySummaryProgress(
      { type: 'batch_done', batch: 2, total_batches: 12 }, active, finished,
    )
    active = done2.newActive
    finished = done2.newFinished

    // style 阶段开始
    const styleInit = applySummaryProgress(
      { type: 'phase', phase: 'style' },
      active, finished,
    )
    // 残留的 10 个 batch 块应全部标 done
    expect(styleInit.changed).toBe(true)
    let batchInFinished = 0
    for (const [id, info] of styleInit.newFinished.entries()) {
      if (id >= 900001 && id <= 900012 && info.ok) batchInFinished++
    }
    expect(batchInFinished).toBe(12)  // 2 done by event + 10 swept = 12
    // active 只剩 style
    expect(styleInit.newActive.size).toBe(1)
    expect(styleInit.newActive.has(920001)).toBe(true)
  })

  it('分析阶段原 block_start（id 1-10000）不会被 sweep 影响', () => {
    // 模拟分析阶段残留
    const active = new Map([
      [1669, { range: 'ch1669', startedAt: Date.now() }],
    ] as Iterable<[number, { range: string; startedAt: number }]>)
    const finished = new Map()

    const r = applySummaryProgress(
      { type: 'phase', phase: 'batch', total_batches: 12 },
      active, finished,
    )
    // 1669（分析章节）不被 sweep
    expect(r.newActive.has(1669)).toBe(true)
    // 但 batch 阶段的合成块应该都在
    expect(r.newActive.get(900001)?.range).toBe('卷1/12')
  })

  it('complete：残留的 style/report 块兜底标 done', () => {
    let active = new Map()
    let finished = new Map()
    const init = applySummaryProgress({ type: 'phase', phase: 'style' }, active, finished)
    active = init.newActive
    finished = init.newFinished

    const r = applySummaryProgress({ type: 'complete', message: '总结完成' }, active, finished)
    expect(r.changed).toBe(true)
    expect(r.newActive.size).toBe(0)
    expect(r.newFinished.get(920001)?.ok).toBe(true)
  })

  it('未知 event type 不触发 changed（changed=false）', () => {
    const r = applySummaryProgress(
      { type: 'status', message: '加载中' },
      new Map(), new Map(),
    )
    expect(r.changed).toBe(false)
  })

  it('batch_done 但 batch=0 不会乱删（防御）', () => {
    const active = new Map([
      [900001, { range: '卷1/12', startedAt: Date.now() }],
    ] as Iterable<[number, { range: string; startedAt: number }]>)
    const r = applySummaryProgress(
      { type: 'batch_done', batch: 0, total_batches: 12 },
      active, new Map(),
    )
    expect(r.changed).toBe(false)
    expect(r.newActive.size).toBe(1)
  })

  it('batch_failed 保留 /totalBatches 后缀（range=卷X/N）', () => {
    let active = new Map()
    let finished = new Map()
    const init = applySummaryProgress(
      { type: 'phase', phase: 'batch', total_batches: 12 },
      active, finished,
    )
    active = init.newActive
    finished = init.newFinished
    const r = applySummaryProgress(
      { type: 'batch_failed', batch: 4, total_batches: 12, message: '卷4 失败' },
      active, finished,
    )
    expect(r.newFinished.get(900004)?.range).toBe('卷4/12')
    expect(r.newFinished.get(900004)?.ok).toBe(false)
  })

  it('batch_done 不传 total_batches 时退化为卷X（不抛错）', () => {
    let active = new Map()
    let finished = new Map()
    const init = applySummaryProgress(
      { type: 'phase', phase: 'batch', total_batches: 12 },
      active, finished,
    )
    active = init.newActive
    finished = init.newFinished
    const r = applySummaryProgress(
      { type: 'batch_done', batch: 5 },  // 缺 total_batches
      active, finished,
    )
    expect(r.newFinished.get(900005)?.range).toBe('卷5')
  })

  it('用户停止：type=status + message 含"已停止" → 残留合成块以 ok=false 移到 finished', () => {
    // 模拟 batch 阶段跑了一些，用户突然点停止
    let active = new Map()
    let finished = new Map()
    const init = applySummaryProgress(
      { type: 'phase', phase: 'batch', total_batches: 12 },
      active, finished,
    )
    active = init.newActive
    finished = init.newFinished

    const r = applySummaryProgress(
      { type: 'status', message: '已停止' },
      active, finished,
    )
    expect(r.changed).toBe(true)
    expect(r.newActive.size).toBe(0)
    // 12 个合成块应全部以 ok=false 移到 finished（不再转圈显示"运行中"）
    let stopCount = 0
    for (const info of r.newFinished.values()) {
      if (info.ok === false) stopCount++
    }
    expect(stopCount).toBe(12)
  })

  it('phase=recheck + total_batches=5 现在会合成 5 个块（修了死代码）', () => {
    const r = applySummaryProgress(
      { type: 'phase', phase: 'recheck', total_batches: 5 },
      new Map(), new Map(),
    )
    expect(r.changed).toBe(true)
    expect(r.newActive.size).toBe(5)
    expect(r.newActive.get(910001)?.range).toBe('伏笔复检批1/5')
    expect(r.newActive.get(910005)?.range).toBe('伏笔复检批5/5')
  })
})
