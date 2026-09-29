/** 车道注册表自愈纯函数测试（车道堆叠修复） */
import { describe, it, expect } from 'vitest'
import {
  gcActiveBlocks,
  normalizeRange,
  reconcileActiveBlocks,
  SYNTHETIC_LANE_ID_BASE,
  type ActiveBlockEntry,
  type InflightBlock,
  type TokenInfo,
} from './laneRegistry'

const MIN = 60 * 1000

function activeOf(entries: Array<[number, number]>): Map<number, ActiveBlockEntry> {
  // entries: [id, startedAgeMs] → startedAt = now - age
  const now = Date.now()
  return new Map(entries.map(([id, age]) => [id, { range: `block ${id}`, startedAt: now - age }]))
}

function tokensOf(entries: Array<[number, number]>): Map<number, TokenInfo> {
  // entries: [id, lastTokenAgeMs]
  const now = Date.now()
  return new Map(entries.map(([id, age]) => [id, { outputTokens: 0, lastTokenAt: now - age }]))
}

describe('normalizeRange', () => {
  it('保留规范短格式', () => {
    expect(normalizeRange('ch1381-ch1384')).toBe('ch1381-ch1384')
  })

  it('剥掉开始前缀和省略号', () => {
    expect(normalizeRange('开始分析ch1429-ch1432...')).toBe('ch1429-ch1432')
  })

  it('剥掉补跑前缀', () => {
    expect(normalizeRange('[补跑] 开始分析ch9-ch10...')).toBe('ch9-ch10')
  })

  it('剥掉完成、失败、异常尾巴', () => {
    expect(normalizeRange('ch9-ch10分析完成')).toBe('ch9-ch10')
    expect(normalizeRange('ch9-ch10分析失败')).toBe('ch9-ch10')
    expect(normalizeRange('ch9-ch10分析异常')).toBe('ch9-ch10')
  })

  it('剥掉内容审核拦截尾巴', () => {
    expect(normalizeRange('ch9-ch10 内容审核拦截，已跳过')).toBe('ch9-ch10')
  })
})

describe('gcActiveBlocks', () => {
  const WARN = 480 // 秒

  it('回收：超预警窗且最后 token 也超窗的僵尸车道', () => {
    const now = Date.now()
    const active = activeOf([[100, 10 * MIN]]) // 10 分钟前 start
    const tokens = tokensOf([[100, 9 * MIN]]) // 9 分钟前最后 token
    const { kept, gcIds } = gcActiveBlocks(active, tokens, now, WARN)
    expect(gcIds).toEqual([100])
    expect(kept.size).toBe(0)
  })

  it('保留：超预警窗但 token 还在更新的活块（慢块不误杀）', () => {
    const now = Date.now()
    const active = activeOf([[100, 10 * MIN]])
    const tokens = tokensOf([[100, 3 * 1000]]) // 3 秒前还有 token
    const { kept, gcIds } = gcActiveBlocks(active, tokens, now, WARN)
    expect(gcIds).toEqual([])
    expect(kept.size).toBe(1)
  })

  it('保留：未收到过 token_delta 的块不按条件A回收（非流式/首token前）', () => {
    const now = Date.now()
    const active = activeOf([[100, 10 * MIN]])
    const { kept, gcIds } = gcActiveBlocks(active, new Map(), now, WARN)
    expect(gcIds).toEqual([])
    expect(kept.size).toBe(1)
  })

  it('硬上限：无论有无 token，超 hardTtl 一律回收', () => {
    const now = Date.now()
    const active = activeOf([[100, 31 * MIN]])
    const tokens = tokensOf([[100, 1000]]) // token 活跃但 31 分钟没完成
    const { gcIds } = gcActiveBlocks(active, tokens, now, WARN)
    expect(gcIds).toEqual([100])
  })

  it('跳过总结合成车道（≥900000），永不 GC', () => {
    const now = Date.now()
    const id = SYNTHETIC_LANE_ID_BASE + 1
    const active = activeOf([[id, 60 * MIN]])
    const { kept, gcIds } = gcActiveBlocks(active, new Map(), now, WARN)
    expect(gcIds).toEqual([])
    expect(kept.size).toBe(1)
  })

  it('无回收时返回原引用（不触发多余反应式更新）', () => {
    const active = activeOf([[100, 1000]])
    const { kept } = gcActiveBlocks(active, new Map(), Date.now(), WARN)
    expect(kept).toBe(active)
  })
})

describe('reconcileActiveBlocks', () => {
  const now = Date.now()

  function inflightOf(entries: Array<[number, number]>): InflightBlock[] {
    // entries: [chapter, startedAgeSec]
    return entries.map(([chapter, ageSec]) => ({
      chapter, range: `开始分析ch${chapter}...`, started_at: now / 1000 - ageSec,
    }))
  }

  it('用 inflight 整体替换分析块（僵尸被清除）', () => {
    const active = activeOf([[100, 30 * MIN], [200, 60 * 1000]])
    const next = reconcileActiveBlocks(active, inflightOf([[200, 30]]), now)
    expect([...next.keys()]).toEqual([200])
    expect(next.get(200)!.range).toContain('ch200')
  })

  it('保留总结合成车道（≥900000）', () => {
    const syntheticId = SYNTHETIC_LANE_ID_BASE + 5
    const active = activeOf([[syntheticId, 5 * MIN]])
    const next = reconcileActiveBlocks(active, inflightOf([[200, 30]]), now)
    expect(next.get(syntheticId)).toBeDefined()
    expect(next.get(200)).toBeDefined()
  })

  it('保留 15s 内新登记块（防 fetch→start 竞态闪烁）', () => {
    const active = activeOf([[300, 5 * 1000]]) // 5 秒前 start，REST 快照尚未包含
    const next = reconcileActiveBlocks(active, inflightOf([]), now)
    expect(next.get(300)).toBeDefined()
  })

  it('超过宽限期的失联块被清除', () => {
    const active = activeOf([[300, 60 * 1000]]) // 1 分钟前 start，不在 inflight
    const next = reconcileActiveBlocks(active, inflightOf([]), now)
    expect(next.size).toBe(0)
  })

  it('无变化时返回原引用', () => {
    // active 与 inflight 必须同源构造（服务端秒×1000），否则浮点抖动导致不等
    const serverTs = now / 1000 - 30
    const active = new Map([[200, { range: '开始分析ch200...', startedAt: Math.round(serverTs * 1000) }]])
    const inflight: InflightBlock[] = [{ chapter: 200, range: '开始分析ch200...', started_at: serverTs }]
    const next = reconcileActiveBlocks(active, inflight, now)
    expect(next).toBe(active)
  })

  it('分析块超过并发上限时按最早开始优先截断', () => {
    const synthetic = SYNTHETIC_LANE_ID_BASE + 1
    // 600 是宽限期内 WS 记账异常；200/300/400 来自并发池，按最早开始保留
    const active = new Map<number, ActiveBlockEntry>([
      [300, { range: 'ch300', startedAt: now - 30 * 1000 }],
      [200, { range: 'ch200', startedAt: now - 60 * 1000 }],
      [400, { range: 'ch400', startedAt: now - 10 * 1000 }],
      [600, { range: 'ch600', startedAt: now - 5 * 1000 }],
      [synthetic, { range: 'summary', startedAt: now - 5 * 1000 }],
    ])
    const inflight = inflightOf([[200, 60], [300, 30], [400, 10]])
    const next = reconcileActiveBlocks(active, inflight, now, 15000, 3)
    expect([...next.keys()].sort((a, b) => a - b)).toEqual([200, 300, 400, synthetic])
  })

  it('未超过并发上限时不截断', () => {
    const active = activeOf([])
    const next = reconcileActiveBlocks(active, inflightOf([[200, 60], [300, 30]]), now, 15000, 4)
    expect(next.size).toBe(2)
  })
})
