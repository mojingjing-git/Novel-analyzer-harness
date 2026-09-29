/**
 * queueFeed 纯函数测试：QueuePage WS 消息状态机（从 onMessage 抽取）的语义锁定。
 * 重点回归：H17 P3 V2（2026-08-26）block_done 清速率 / token 累计取最大 / FIFO 上限。
 */
import { describe, it, expect } from 'vitest'
import {
  applyBlockStart, applyBlockDone, applyTokenDelta, appendDiscovery,
  aggregateRate, fmt, fmtPercent, summarizeSession, resolveDeleteIndex,
  pickRunningBlockSize, FINISHED_BLOCKS_MAX, RATE_HISTORY_MAX, DISCOVERIES_MAX,
  type FeedActiveBlock, type FeedFinishedBlock, type FeedDiscoveryItem,
} from './queueFeed'
import type { QueueItemDto, SessionTokenStatsResponse } from '../api/client'

const NOW = 1_700_000_000_000

function mkItem(partial: Partial<QueueItemDto>): QueueItemDto {
  return {
    name: '', blocks_dir: '', workspace_dir: '', status: 'pending',
    total_chapters: 0, completed_chapters: 0, block_size: 1,
    start_time: 0, end_time: 0, error_message: '', archive_failed: false,
    ...partial,
  }
}

describe('queueFeed.applyBlockStart', () => {
  it('登记活跃块：结构化 range + ts(秒)×1000', () => {
    const next = applyBlockStart(new Map(), { chapter: 9, range: 'ch9-ch10', progress: 0, total: 0, ts: 1700000000 }, NOW)
    expect(next.get(9)).toEqual({ range: 'ch9-ch10', startedAt: 1_700_000_000_000 })
  })

  it('无结构化 range 时回退 message 并经 normalizeRange 剥人类文案', () => {
    // 注意 ?? 语义：range 为空串不回退（与线上行为一致），缺失才回退 message
    const p = { chapter: 5, message: '开始分析ch5-ch6...', progress: 0, total: 0, ts: 100 } as unknown as Parameters<typeof applyBlockStart>[1]
    const next = applyBlockStart(new Map(), p, NOW)
    expect(next.get(5)?.range).toBe('ch5-ch6')
  })

  it('range 为空串（非缺失）时 ?? 不回退 message → `block N` 兜底', () => {
    const next = applyBlockStart(new Map(), { chapter: 5, range: '', message: '开始分析ch5-ch6...', progress: 0, total: 0, ts: 100 }, NOW)
    expect(next.get(5)?.range).toBe('block 5')
  })

  it('range 与 message 全空 → `block N` 兜底文案', () => {
    const next = applyBlockStart(new Map(), { chapter: 7, range: '', progress: 0, total: 0, ts: 100 }, NOW)
    expect(next.get(7)?.range).toBe('block 7')
  })

  it('缺 ts 时 startedAt 回退为传入的 nowMs', () => {
    const next = applyBlockStart(new Map(), { chapter: 3, range: 'ch3', progress: 0, total: 0, ts: undefined as unknown as number }, NOW)
    expect(next.get(3)?.startedAt).toBe(NOW)
  })

  it('chapter ≤ 0 不登记且不重建 Map', () => {
    const active = new Map<number, FeedActiveBlock>([[1, { range: 'ch1', startedAt: 1 }]])
    const next = applyBlockStart(active, { chapter: 0, range: '', progress: 0, total: 0, ts: 1 }, NOW)
    expect(next).toBe(active)
    expect(next.size).toBe(1)
  })
})

describe('queueFeed.applyBlockDone', () => {
  it('移出活跃 + 登记 finished（ok/range）', () => {
    const active = new Map<number, FeedActiveBlock>([[9, { range: 'ch9-ch10', startedAt: 1 }]])
    const r = applyBlockDone(active, new Map(), new Map(), { chapter: 9, ok: true, range: 'ch9-ch10' })
    expect(r.active.has(9)).toBe(false)
    expect(r.finished.get(9)).toEqual({ ok: true, range: 'ch9-ch10' })
  })

  it('H17 P3 V2：done 后清理该块速率——聚合不再算已结束块的尾速', () => {
    const active = new Map<number, FeedActiveBlock>([[9, { range: 'r9', startedAt: 1 }]])
    const rate = new Map<number, number>([[9, 55]])
    const r = applyBlockDone(active, new Map(), rate, { chapter: 9, ok: true, range: 'r9' })
    expect(aggregateRate(r.rateByBlock, r.active)).toBe(0)
  })

  it(`FIFO：超过 ${FINISHED_BLOCKS_MAX} 条淘汰最早登记的块`, () => {
    let finished = new Map<number, FeedFinishedBlock>()
    for (let i = 1; i <= FINISHED_BLOCKS_MAX; i++) {
      finished = applyBlockDone(new Map(), finished, new Map(), { chapter: i, ok: true, range: `r${i}` }).finished
    }
    expect(finished.has(1)).toBe(true)
    finished = applyBlockDone(new Map(), finished, new Map(), { chapter: 999, ok: true, range: 'r999' }).finished
    expect(finished.size).toBe(FINISHED_BLOCKS_MAX)
    expect(finished.has(1)).toBe(false)   // 最早插入的块被淘汰
    expect(finished.has(2)).toBe(true)
    expect(finished.has(999)).toBe(true)
  })

  it('chapter ≤ 0 三张 Map 原样返回', () => {
    const active = new Map<number, FeedActiveBlock>()
    const finished = new Map<number, FeedFinishedBlock>()
    const rate = new Map<number, number>()
    const r = applyBlockDone(active, finished, rate, { chapter: 0, ok: true, range: '' })
    expect(r.active).toBe(active)
    expect(r.finished).toBe(finished)
    expect(r.rateByBlock).toBe(rate)
  })
})

describe('queueFeed.applyTokenDelta', () => {
  const delta = (output: number, rate: number) => ({ output_tokens: output, rate_tokens_per_sec: rate })
  const deltaMsg = (unitIdx: number, output: number, rate: number) => ({
    context: 'analysis', session_id: 's', unit_idx: unitIdx, delta: delta(output, rate), timestamp: 0,
  })

  it('同块累计取最大而非相加（后端 delta 是累计值）', () => {
    let tokens = new Map<number, { outputTokens: number; lastTokenAt: number }>()
    let rate = new Map<number, number>()
    ;({ tokenByBlock: tokens, rateByBlock: rate } = applyTokenDelta(tokens, rate, [], deltaMsg(4, 100, 20), NOW))
    ;({ tokenByBlock: tokens } = applyTokenDelta(tokens, rate, [], deltaMsg(4, 50, 20), NOW))
    expect(tokens.get(4)?.outputTokens).toBe(100)
  })

  it('rate ≤ 0 也写入 rateByBlock（反映该块当前不输出）', () => {
    const r = applyTokenDelta(new Map(), new Map(), [], deltaMsg(2, 0, 0), NOW)
    expect(r.rateByBlock.get(2)).toBe(0)
  })

  it(`rate > 0 追加 sparkline 历史并截到最近 ${RATE_HISTORY_MAX} 条；rate ≤ 0 不追加`, () => {
    let history: number[] = []
    for (let i = 1; i <= RATE_HISTORY_MAX + 5; i++) {
      ;({ rateHistory: history } = applyTokenDelta(new Map(), new Map(), history, deltaMsg(1, 1, i), NOW))
    }
    expect(history.length).toBe(RATE_HISTORY_MAX)
    expect(history[0]).toBe(6)          // 前 5 条被淘汰
    expect(history[history.length - 1]).toBe(RATE_HISTORY_MAX + 5)
    const r = applyTokenDelta(new Map(), new Map(), history, deltaMsg(1, 1, 0), NOW)
    expect(r.rateHistory).toBe(history) // 引用不变 = 未追加
  })

  it('unit_idx ≤ 0 不更新块级 Map，但 rateHistory 逻辑独立生效', () => {
    const tokens = new Map<number, { outputTokens: number; lastTokenAt: number }>()
    const r = applyTokenDelta(tokens, new Map(), [], deltaMsg(-1, 10, 5), NOW)
    expect(r.tokenByBlock.size).toBe(0)
    expect(r.rateHistory).toEqual([5])
  })
})

describe('queueFeed.appendDiscovery', () => {
  it('追加 + id 自增；新伏笔/新人物非空 → highlighted', () => {
    const r1 = appendDiscovery([], { events: 3, foreshadows: ['剑'], characters: [], unresolved: [] }, 1, NOW)
    expect(r1.items[0]).toMatchObject({ id: 1, events: 3, foreshadows: ['剑'], highlighted: true })
    expect(r1.nextId).toBe(2)
    const r2 = appendDiscovery(r1.items, { events: 0, foreshadows: [], characters: ['张三'], unresolved: ['?'] }, r1.nextId, NOW)
    expect(r2.items[1]).toMatchObject({ id: 2, highlighted: true })
    const r3 = appendDiscovery(r2.items, { events: 1, foreshadows: [], characters: [], unresolved: ['?'] }, r2.nextId, NOW)
    expect(r3.items[2].highlighted).toBe(false)
  })

  it(`发现流上限 ${DISCOVERIES_MAX} 条：淘汰最早`, () => {
    let items: FeedDiscoveryItem[] = []
    let nextId = 1
    for (let i = 0; i <= DISCOVERIES_MAX; i++) {
      ;({ items, nextId } = appendDiscovery(items, { events: i, foreshadows: [], characters: [], unresolved: [] }, nextId, NOW))
    }
    expect(items.length).toBe(DISCOVERIES_MAX)
    expect(items[0].events).toBe(1)
    expect(items[items.length - 1].events).toBe(DISCOVERIES_MAX)
  })
})

describe('queueFeed.aggregateRate', () => {
  it('只累加仍活跃的块速率', () => {
    const rate = new Map([[1, 10], [2, 20], [3, 40]])
    const active = new Map<number, FeedActiveBlock>([[1, { range: '', startedAt: 0 }], [3, { range: '', startedAt: 0 }]])
    expect(aggregateRate(rate, active)).toBe(50)  // 块 2 已结束不计
  })
})

describe('queueFeed.fmt / fmtPercent', () => {
  it('0 / 个位 / K / M 三档', () => {
    expect(fmt(0)).toBe('0')
    expect(fmt(999)).toBe('999')
    expect(fmt(1000)).toBe('1.0K')
    expect(fmt(123_456)).toBe('123.5K')
    expect(fmt(1_500_000)).toBe('1.50M')
    expect(fmtPercent(33.33)).toBe('33.3%')
  })
})

describe('queueFeed.summarizeSession', () => {
  const stats = (partial: {
    analysis?: Partial<SessionTokenStatsResponse['analysis']>
    summary?: Partial<SessionTokenStatsResponse['summary']>
  }): SessionTokenStatsResponse => ({
    analysis: { categories: {}, chapter_stats: [], elapsed: 0, running: false, ...partial.analysis },
    summary: { categories: {}, input_tokens: 0, output_tokens: 0, elapsed: 0, ...partial.summary },
  })

  it('总量 = 分析分类小计 + 总结；总消耗含 cached', () => {
    const s = summarizeSession(stats({
      analysis: {
        categories: { analysis: { input_tokens: 100, output_tokens: 50 } },
        cached_tokens: 30,
      },
      summary: { input_tokens: 10, output_tokens: 5 },
    }))
    expect(s.input).toBe(110)
    expect(s.output).toBe(55)
    expect(s.cached).toBe(30)
    expect(s.total).toBe(110 + 55 + 30)
  })

  it('命中率只基于分析侧（总结侧混入会低估命中率）', () => {
    const s = summarizeSession(stats({
      analysis: { categories: { a: { input_tokens: 70, output_tokens: 0 } }, cached_tokens: 30 },
      summary: { input_tokens: 9999, output_tokens: 0 },  // 总结侧不参与命中率
    }))
    expect(s.hitRate).toBeCloseTo(30, 5)
  })

  it('空统计 → 全 0，命中率不出现 NaN', () => {
    expect(summarizeSession(null)).toEqual({ input: 0, output: 0, cached: 0, hitRate: 0, total: 0 })
    expect(summarizeSession(stats({})).hitRate).toBe(0)
  })
})

describe('queueFeed.resolveDeleteIndex（P2：按名称实时解析防删错书）', () => {
  const items = [mkItem({ name: '《A》' }), mkItem({ name: '《B》' })]

  it('按名称命中返回当前索引', () => {
    expect(resolveDeleteIndex(items, '《B》')).toBe(1)
  })

  it('重排后名称对应的索引随之变化（不固定旧索引）', () => {
    const reordered = [items[1], items[0]]
    expect(resolveDeleteIndex(reordered, '《B》')).toBe(0)
  })

  it('找不到 / items 缺失 → -1', () => {
    expect(resolveDeleteIndex(items, '《C》')).toBe(-1)
    expect(resolveDeleteIndex(undefined, '《A》')).toBe(-1)
  })
})

describe('queueFeed.pickRunningBlockSize（多书混排取运行项）', () => {
  it('取 running 项的 block_size，而非首项', () => {
    const items = [mkItem({ name: 'A', block_size: 4, status: 'done' }), mkItem({ name: 'B', block_size: 8, status: 'running' })]
    expect(pickRunningBlockSize(true, items)).toBe(8)
  })

  it('无 running 项回退首项', () => {
    const items = [mkItem({ name: 'A', block_size: 4, status: 'done' })]
    expect(pickRunningBlockSize(true, items)).toBe(4)
  })

  it('未运行 / 空队列 / block_size 为 0 → 1', () => {
    expect(pickRunningBlockSize(false, [mkItem({ block_size: 4 })])).toBe(1)
    expect(pickRunningBlockSize(true, [])).toBe(1)
    expect(pickRunningBlockSize(true, [mkItem({ block_size: 0 })])).toBe(1)
  })
})
