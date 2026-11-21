/**
 * 前端工具函数与路由综合测试：
 * 覆盖 router、markdown、splitterView、laneRegistry、queueFeed、settingsForm、summaryLanes、summaryProgress。
 */
import { describe, it, expect } from 'vitest'
import { router } from '../router'
import { renderMarkdown } from '../utils/markdown'
import {
  baseName,
  appendFileUnique,
  rollbackProcessing,
  groupChaptersByVolume,
} from '../utils/splitterView'
import {
  gcActiveBlocks,
  normalizeRange,
  reconcileActiveBlocks,
  SYNTHETIC_LANE_ID_BASE,
  type ActiveBlockEntry,
} from '../utils/laneRegistry'
import {
  applyBlockStart,
  applyBlockDone,
  applyTokenDelta,
  appendDiscovery,
  resolvePayloadBlockId,
  aggregateRate,
  fmt,
  fmtPercent,
  summarizeSession,
  resolveDeleteIndex,
  pickRunningBlockSize,
  formatSessionCost,
  FINISHED_BLOCKS_MAX,
  type FeedActiveBlock,
  type FeedFinishedBlock,
} from '../utils/queueFeed'
import {
  pruneEmptyStrings,
  mapFieldErrors,
  toggleCategoryIn,
  inferThinkingState,
  composeThinkingMode,
  decomposeThinkingMode,
  makeThinkingPresets,
} from '../utils/settingsForm'
import { applySummaryProgress } from '../utils/summaryLanes'
import {
  SUMMARY_PHASES,
  overallPctOf,
  phaseIndexOf,
  stepClassOf,
  elapsedTextOf,
  recheckAccountOf,
} from '../utils/summaryProgress'
import type { QueueItemDto, SessionTokenStatsResponse, SummaryStatus } from '../api/client'

describe('router & basic utils', () => {
  it('注册全部核心子路由', () => {
    const paths = router.getRoutes().map((r) => r.path)
    for (const p of ['/', '/settings', '/summary', '/splitter', '/workspace', '/style', '/timeline', '/characters', '/stats', '/graph', '/map']) {
      expect(paths).toContain(p)
    }
  })

  it('renderMarkdown：支持粗体/表格/代码块并转义 HTML 标签与拦截危险协议', () => {
    expect(renderMarkdown('')).toBe('')
    const html = renderMarkdown('**加粗** <script>alert(1)</script> [危险](javascript:alert(2)) [安全](https://example.com)')
    expect(html).toContain('<strong>加粗</strong>')
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
    expect(html).toContain('href="#"')
    expect(html).toContain('href="https://example.com"')
  })

  it('splitterView：baseName、去重追加、失败回滚与按卷分组', () => {
    expect(baseName('D:\\小说\\《诡秘之主》.txt')).toBe('《诡秘之主》')
    expect(baseName('/var/books/test.docx')).toBe('test')

    const r1 = appendFileUnique([], 'D:\\a.txt')
    const r2 = appendFileUnique(r1.items, 'D:\\a.txt')
    expect(r2.added).toBe(false)
    expect(r2.items).toHaveLength(1)

    const rolled = rollbackProcessing([{ ...r1.items[0], status: 'processing' }])
    expect(rolled[0].status).toBe('pending')

    const groups = groupChaptersByVolume([
      { index: 1, title: 'c1', volume: '第一卷', word_count: 100 },
      { index: 2, title: 'c2', volume: '第一卷', word_count: 100 },
      { index: 3, title: 'c3', volume: '第二卷', word_count: 100 },
    ])
    expect(groups).toHaveLength(2)
    expect(groups[0].items).toHaveLength(2)
  })
})

describe('laneRegistry', () => {
  const MIN = 60 * 1000

  it('normalizeRange 剥除前缀、状态后缀与审核拦截文案', () => {
    expect(normalizeRange('开始分析ch1429-ch1432...')).toBe('ch1429-ch1432')
    expect(normalizeRange('[补跑] 开始分析ch9-ch10...')).toBe('ch9-ch10')
    expect(normalizeRange('ch9-ch10分析完成')).toBe('ch9-ch10')
    expect(normalizeRange('ch9-ch10 内容审核拦截，已跳过')).toBe('ch9-ch10')
  })

  it('gcActiveBlocks：回收超时无 token 僵尸块，保留活跃 token 块与总结合成车道', () => {
    const now = Date.now()
    const syntheticId = SYNTHETIC_LANE_ID_BASE + 1
    const active = new Map<number, ActiveBlockEntry>([
      [100, { range: 'b100', startedAt: now - 10 * MIN }],
      [200, { range: 'b200', startedAt: now - 10 * MIN }],
      [syntheticId, { range: 'summary', startedAt: now - 60 * MIN }],
    ])
    const tokens = new Map([
      [100, { outputTokens: 10, lastTokenAt: now - 9 * MIN }],
      [200, { outputTokens: 10, lastTokenAt: now - 3000 }],
    ])
    const { kept, gcIds } = gcActiveBlocks(active, tokens, now, 480)
    expect(gcIds).toEqual([100])
    expect(kept.has(200)).toBe(true)
    expect(kept.has(syntheticId)).toBe(true)
  })

  it('reconcileActiveBlocks：按 inflight 对账、保留宽限期内新块与合成块、按并发上限截断', () => {
    const now = Date.now()
    const synthetic = SYNTHETIC_LANE_ID_BASE + 1
    const active = new Map<number, ActiveBlockEntry>([
      [100, { range: 'stale', startedAt: now - 60_000 }],
      [200, { range: 'ch200', startedAt: now - 60_000 }],
      [300, { range: 'ch300', startedAt: now - 30_000 }],
      [400, { range: 'grace', startedAt: now - 5_000 }],
      [synthetic, { range: 'summary', startedAt: now - 5_000 }],
    ])
    const inflight = [
      { chapter: 200, range: '开始分析ch200...', started_at: now / 1000 - 60 },
      { chapter: 300, range: '开始分析ch300...', started_at: now / 1000 - 30 },
    ]
    const next = reconcileActiveBlocks(active, inflight, now, 15_000, 2)
    expect([...next.keys()].sort((a, b) => a - b)).toEqual([200, 300, synthetic])
  })
})

describe('queueFeed', () => {
  const NOW = 1_700_000_000_000

  it('applyBlockStart / applyBlockDone / aggregateRate 完整生命周期', () => {
    const active = applyBlockStart(new Map<number, FeedActiveBlock>(), {
      chapter: 9, range: 'ch9-ch10', progress: 0, total: 0, ts: 1700000000,
    }, NOW)
    expect(active.get(9)).toEqual({ range: 'ch9-ch10', startedAt: NOW })

    const rate = new Map([[9, 55]])
    expect(aggregateRate(rate, active)).toBe(55)

    const done = applyBlockDone(active, new Map<number, FeedFinishedBlock>(), rate, {
      chapter: 9, ok: true, range: 'ch9-ch10',
    })
    expect(done.active.has(9)).toBe(false)
    expect(done.finished.get(9)).toEqual({ ok: true, range: 'ch9-ch10' })
    expect(aggregateRate(done.rateByBlock, done.active)).toBe(0)
  })

  it('applyBlockDone 超出 FINISHED_BLOCKS_MAX 时按 FIFO 淘汰最早块', () => {
    let finished = new Map<number, FeedFinishedBlock>()
    for (let i = 1; i <= FINISHED_BLOCKS_MAX + 1; i++) {
      finished = applyBlockDone(new Map(), finished, new Map(), { chapter: i, ok: true, range: `r${i}` }).finished
    }
    expect(finished.size).toBe(FINISHED_BLOCKS_MAX)
    expect(finished.has(1)).toBe(false)
    expect(finished.has(FINISHED_BLOCKS_MAX + 1)).toBe(true)
  })

  it('applyTokenDelta：同块取最大累计值、总结阶段合成 ID 映射与 is_thinking 透传', () => {
    const p1 = { context: 'analysis', session_id: 's', unit_idx: 4, delta: { output_tokens: 100, rate_tokens_per_sec: 20 }, timestamp: 0 }
    const p2 = { context: 'analysis', session_id: 's', unit_idx: 4, delta: { output_tokens: 50, rate_tokens_per_sec: 20 }, timestamp: 0 }
    let r = applyTokenDelta(new Map(), new Map(), [], p1, NOW)
    r = applyTokenDelta(r.tokenByBlock, r.rateByBlock, r.rateHistory, p2, NOW)
    expect(r.tokenByBlock.get(4)?.outputTokens).toBe(100)

    const pStyle = {
      context: 'summary_phase_3', session_id: 'b', unit_idx: 0,
      delta: { output_tokens: 300, rate_tokens_per_sec: 15, is_thinking: true }, timestamp: NOW,
    }
    expect(resolvePayloadBlockId(pStyle)).toBe(920001)
    const rStyle = applyTokenDelta(new Map(), new Map(), [], pStyle, NOW)
    expect(rStyle.tokenByBlock.get(920001)?.isThinking).toBe(true)
  })

  it('appendDiscovery、summarizeSession、resolveDeleteIndex、pickRunningBlockSize、formatSessionCost', () => {
    const d = appendDiscovery([], { events: 2, foreshadows: ['古剑'], characters: [], unresolved: [] }, 1, NOW)
    expect(d.items[0].highlighted).toBe(true)

    const stats: SessionTokenStatsResponse = {
      analysis: { categories: { ch: { input_tokens: 70, output_tokens: 30 } }, cached_tokens: 30, chapter_stats: [], elapsed: 1, running: false },
      summary: { categories: {}, input_tokens: 10, output_tokens: 5, elapsed: 1 },
      cost: { usd: 0.321, unpriced: [] },
    }
    const s = summarizeSession(stats)
    expect(s.total).toBe(70 + 30 + 10 + 5 + 30)
    expect(s.hitRate).toBeCloseTo(30, 5)
    expect(formatSessionCost(s.cost)).toBe('~$0.32')
    expect(formatSessionCost({ usd: null, unpriced: ['x'] })).toBe('未定价')
    expect(fmt(1500)).toBe('1.5K')
    expect(fmtPercent(12.34)).toBe('12.3%')

    const items = [
      { name: '《A》', block_size: 4, status: 'done' },
      { name: '《B》', block_size: 8, status: 'running' },
    ] as QueueItemDto[]
    expect(resolveDeleteIndex(items, '《B》')).toBe(1)
    expect(pickRunningBlockSize(true, items)).toBe(8)
  })
})

describe('settingsForm', () => {
  it('pruneEmptyStrings、mapFieldErrors、toggleCategoryIn', () => {
    const cfg = { api: { base_url: 'https://x', summary_model: '', timeout: 0 }, analysis: { auto_archive: false } }
    pruneEmptyStrings(cfg)
    expect(cfg.api).toEqual({ base_url: 'https://x', timeout: 0 })

    const { fieldErrors } = mapFieldErrors([{ loc: ['body', 'api', 'base_url'], msg: 'invalid url' }])
    expect(fieldErrors).toEqual({ base_url: 'invalid url' })
    expect(toggleCategoryIn(['人物', '情节'], '人物')).toEqual(['情节'])
  })

  it('inferThinkingState 与 composeThinkingMode / decomposeThinkingMode 深度优先合成', () => {
    expect(inferThinkingState({})).toBe('default')
    expect(inferThinkingState({ reasoning_effort: 'low' })).toBe('depth_low')
    expect(inferThinkingState({ chat_template_kwargs: { enable_thinking: false } })).toBe('disabled')

    expect(composeThinkingMode('low', 'thinking_disabled')).toEqual({ reasoning_effort: 'low' })
    expect(composeThinkingMode('', 'thinking_disabled')).toEqual({ thinking: { type: 'disabled' } })
    expect(decomposeThinkingMode({ reasoning_effort: 'low' })).toEqual({
      depth: 'low', disableParam: '', isCustom: false,
    })
    expect(makeThinkingPresets('自动')[0].label).toBe('自动')
  })
})

describe('summaryLanes & summaryProgress', () => {
  it('applySummaryProgress：合成各阶段块、batch_done 移入 finished、阶段切换与停止清理', () => {
    const init = applySummaryProgress({ type: 'phase', phase: 'batch', total_batches: 3 }, new Map(), new Map())
    expect(init.newActive.size).toBe(3)
    expect(init.newActive.get(900001)?.range).toBe('卷1/3')

    const done1 = applySummaryProgress(
      { type: 'batch_done', batch: 1, total_batches: 3 },
      init.newActive, init.newFinished,
    )
    expect(done1.newActive.has(900001)).toBe(false)
    expect(done1.newFinished.get(900001)?.ok).toBe(true)

    const stopped = applySummaryProgress({ type: 'status', message: '已停止' }, done1.newActive, done1.newFinished)
    expect(stopped.newActive.size).toBe(0)
    expect(stopped.newFinished.get(900002)?.ok).toBe(false)
  })

  it('summaryProgress：4 阶段加权进度、耗时格式化与复检结果卡提取', () => {
    const totalWeight = SUMMARY_PHASES.reduce((s, p) => s + p.weight, 0)
    expect(totalWeight).toBeCloseTo(1, 10)

    const mk = (partial: Partial<SummaryStatus>): SummaryStatus => ({
      running: false, book_id: '', phase: 'idle', batches_done: 0,
      total_batches: 0, error: '', started_at: 0, finished_at: 0, token_stats: {},
      ...partial,
    })
    expect(overallPctOf(mk({ phase: 'batch', batches_done: 5, total_batches: 10 }))).toBe(35)
    expect(overallPctOf(mk({ phase: 'recheck' }))).toBe(76)
    expect(overallPctOf(mk({ phase: 'complete' }))).toBe(100)
    expect(stepClassOf(phaseIndexOf('recheck'), 0)).toBe('done')
    expect(elapsedTextOf(mk({ started_at: 1000, finished_at: 1125 }), 9999)).toBe('2分5秒')

    expect(recheckAccountOf({})).toBeNull()
    expect(recheckAccountOf({
      status: 'OK', calls: 10, coverage: { checked: 20, supported: 5 },
    })?.supported).toBe(5)
  })
})
