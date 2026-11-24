/**
 * 前端工具函数与路由综合测试：
 * 覆盖 router、markdown、splitterView、laneRegistry、queueFeed、settingsForm、summaryLanes、summaryProgress。
 */
import { describe, it, expect, vi } from 'vitest'
import { router, isChunkLoadError, recoverFromChunkError, CHUNK_RELOAD_GUARD_MS } from '../router'
import { renderMarkdown, renderMarkdownWithToc } from '../utils/markdown'
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
  upsertTokenDeltaBuffer,
  sampleRateHistory,
  calcChartCeiling,
  calcLogChartCeiling,
  buildLogScaleTicks,
  buildSmoothAreaPaths,
  appendDiscovery,
  resolvePayloadBlockId,
  isSummaryTokenDelta,
  aggregateRate,
  fmt,
  fmtPercent,
  summarizeSession,
  resolveDeleteIndex,
  pickRunningBlockSize,
  formatSessionCost,
  FINISHED_BLOCKS_MAX,
  RATE_HISTORY_MAX,
  type BlockTokenInfo,
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
import {
  ADVANCED_GROUP_IDS,
  ALL_ADVANCED_FIELD_KEYS,
  buildDefaultAppConfig,
  DEFAULT_SETTINGS_SCHEMA,
  isFieldModified,
  resetFieldToDefault,
  setConfigValueByPath,
} from '../utils/settingsSchema'
import { applySummaryProgress, reconcileSummaryLanes } from '../utils/summaryLanes'
import {
  SUMMARY_PHASES,
  overallPctOf,
  phaseIndexOf,
  stepClassOf,
  elapsedTextOf,
  recheckAccountOf,
  recheckPctOf,
  recheckMetaTextOf,
  stepSubtextOf,
  stepProgressPctOf,
  summaryTokenTextOf,
} from '../utils/summaryProgress'
import { stripArchiveTimestamp, suggestRestoreName } from '../utils/workspaceView'
import type { QueueItemDto, SessionTokenStatsResponse, SummaryStatus } from '../api/client'

describe('router & basic utils', () => {
  it('注册全部核心子路由及旧路径查询参数保留重定向', () => {
    const routes = router.getRoutes()
    const paths = routes.map((r) => r.path)
    for (const p of ['/', '/settings', '/summary', '/splitter', '/workspace', '/world', '/timeline', '/characters', '/stats']) {
      expect(paths).toContain(p)
    }
    const evalRedirect = (from: string) => {
      const targetUrl = from.split('?')[0]
      const rec = routes.find((r) => r.path === targetUrl)
      expect(typeof rec?.redirect).toBe('function')
      const fn = rec!.redirect as (to: ReturnType<typeof router.resolve>) => unknown
      return fn(router.resolve(from))
    }
    expect(evalRedirect('/graph?book=A&id=张三')).toEqual({ path: '/characters', query: { book: 'A', id: '张三' } })
    expect(evalRedirect('/map?book=A')).toEqual({ path: '/world', query: { book: 'A' } })
    expect(evalRedirect('/style?book=A')).toEqual({ path: '/summary', query: { book: 'A' } })
    expect(evalRedirect('/prompt?book=A')).toEqual({ path: '/stats', query: { book: 'A', tab: 'prompt' } })
  })

  it('router 分包加载失败自愈：识别动态导入/CSS 预加载错误并防死循环重载', () => {
    expect(isChunkLoadError(new TypeError('Failed to fetch dynamically imported module: http://127.0.0.1/assets/CharacterCardPage-abc.js'))).toBe(true)
    expect(isChunkLoadError(new Error('Unable to preload CSS for /assets/WorldStagePage-abc.css'))).toBe(true)
    const chunkErr = new Error('loading chunk 12 failed')
    chunkErr.name = 'ChunkLoadError'
    expect(isChunkLoadError(chunkErr)).toBe(true)
    expect(isChunkLoadError(new Error('普通业务异常'))).toBe(false)

    const mem = new Map<string, string>()
    const storage = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => { mem.set(k, v) },
    }
    const navigate = vi.fn()

    // 首次失败触发重定向
    expect(recoverFromChunkError(
      new TypeError('Failed to fetch dynamically imported module'),
      '/characters',
      { storage, navigate, now: 1000 },
    )).toBe(true)
    expect(navigate).toHaveBeenCalledTimes(1)
    expect(navigate).toHaveBeenCalledWith('/characters')

    // 护栏窗口内同路径不重复触发，防止死循环
    expect(recoverFromChunkError(
      new TypeError('Failed to fetch dynamically imported module'),
      '/characters',
      { storage, navigate, now: 1000 + CHUNK_RELOAD_GUARD_MS - 100 },
    )).toBe(false)
    expect(navigate).toHaveBeenCalledTimes(1)

    // 换目标路径或超过护栏窗口后允许再次自愈
    expect(recoverFromChunkError(
      new TypeError('Failed to fetch dynamically imported module'),
      '/world',
      { storage, navigate, now: 1000 + CHUNK_RELOAD_GUARD_MS - 100 },
    )).toBe(true)
    expect(recoverFromChunkError(
      new TypeError('Failed to fetch dynamically imported module'),
      '/world',
      { storage, navigate, now: 1000 + CHUNK_RELOAD_GUARD_MS * 2 },
    )).toBe(true)
    expect(navigate).toHaveBeenCalledTimes(3)

    // 非分包错误不触发重载
    expect(recoverFromChunkError(new Error('other error'), '/timeline', { storage, navigate, now: 50000 })).toBe(false)
    expect(navigate).toHaveBeenCalledTimes(3)
  })

  it('renderMarkdown：支持粗体/表格(.md-table-wrap)/代码块并转义 HTML 标签与拦截危险协议', () => {
    expect(renderMarkdown('')).toBe('')
    const html = renderMarkdown('**加粗** <script>alert(1)</script> [危险](javascript:alert(2)) [安全](https://example.com)')
    expect(html).toContain('<strong>加粗</strong>')
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
    expect(html).toContain('href="#"')
    expect(html).toContain('href="https://example.com"')

    const tableHtml = renderMarkdown('| 人物 | 定位 |\n| --- | --- |\n| 李火旺 | 主角 |')
    expect(tableHtml).toContain('<div class="md-table-wrap"><table class="md-table">')
  })

  it('renderMarkdownWithToc：提取 h1-h3 目录、剥离行内格式、注入确定性锚点且忽略 h4', () => {
    const md = [
      '# **总纲** `[v1]`',
      '正文段落',
      '## [阶段一](https://example.com) *初入*',
      '### 子节 ~~旧~~',
      '#### 细节四级',
    ].join('\n')
    const res = renderMarkdownWithToc(md)
    expect(res.headings).toEqual([
      { id: 'md-h-1', level: 1, text: '总纲 [v1]' },
      { id: 'md-h-2', level: 2, text: '阶段一 初入' },
      { id: 'md-h-3', level: 3, text: '子节 旧' },
    ])
    expect(res.html).toContain('<h1 id="md-h-1" class="md-h1">')
    expect(res.html).toContain('<h2 id="md-h-2" class="md-h2">')
    expect(res.html).toContain('<h3 id="md-h-3" class="md-h3">')
    expect(res.html).toContain('<h4 class="md-h4">')
    expect(res.html).not.toContain('id="md-h-4"')
  })

  it('summaryTokenTextOf：包含全书图谱分类标签', () => {
    const txt = summaryTokenTextOf({
      running: false,
      book_id: 'A',
      phase: 'complete',
      batches_done: 1,
      total_batches: 1,
      error: '',
      started_at: 0,
      finished_at: 0,
      token_stats: { atlas: { input_tokens: 120, output_tokens: 80 } },
    })
    expect(txt).toContain('全书图谱 入120 出80')
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
    expect(r.rateHistory).toEqual([])

    const pStyle = {
      context: 'summary_phase_3', session_id: 'b', unit_idx: 0,
      delta: { output_tokens: 300, rate_tokens_per_sec: 15, is_thinking: true }, timestamp: NOW,
    }
    expect(resolvePayloadBlockId(pStyle)).toBe(920001)
    const rStyle = applyTokenDelta(new Map(), new Map(), [], pStyle, NOW)
    expect(rStyle.tokenByBlock.get(920001)?.isThinking).toBe(true)

    const pRecon = {
      context: 'summary_phase_1_recon', session_id: 'b', unit_idx: 2,
      delta: { output_tokens: 80, rate_tokens_per_sec: 10 }, timestamp: NOW,
    }
    expect(resolvePayloadBlockId(pRecon)).toBe(905002)

    expect(resolvePayloadBlockId({
      context: 'summary_phase_4', session_id: 'b', unit_idx: 0,
      delta: { output_tokens: 10, rate_tokens_per_sec: 1 }, timestamp: NOW,
    })).toBe(930001)
    expect(resolvePayloadBlockId({
      context: 'summary_phase_4', session_id: 'b', unit_idx: 2,
      delta: { output_tokens: 10, rate_tokens_per_sec: 1 }, timestamp: NOW,
    })).toBe(930002)
    expect(resolvePayloadBlockId({
      context: 'summary_phase_4', session_id: 'b', unit_idx: 3,
      delta: { output_tokens: 10, rate_tokens_per_sec: 1 }, timestamp: NOW,
    })).toBe(930003)

    expect(isSummaryTokenDelta(pStyle)).toBe(true)
    expect(isSummaryTokenDelta(pRecon)).toBe(true)
    expect(isSummaryTokenDelta({
      context: 'summary_phase_4', session_id: 'b', unit_idx: 1,
      delta: { output_tokens: 10, rate_tokens_per_sec: 1 }, timestamp: NOW,
    })).toBe(true)
    expect(isSummaryTokenDelta({
      context: 'analysis', session_id: 'a', unit_idx: 1,
      delta: { output_tokens: 10, rate_tokens_per_sec: 1 }, timestamp: NOW,
    })).toBe(false)
    expect(isSummaryTokenDelta({
      session_id: 'a', unit_idx: 1,
      delta: { output_tokens: 10, rate_tokens_per_sec: 1 }, timestamp: NOW,
    } as Parameters<typeof isSummaryTokenDelta>[0])).toBe(false)
  })

  it('upsertTokenDeltaBuffer、sampleRateHistory、calcChartCeiling 与 buildSmoothAreaPaths', () => {
    const tokBuf = new Map<number, BlockTokenInfo>()
    const rateBuf = new Map<number, number>()
    upsertTokenDeltaBuffer(tokBuf, rateBuf, {
      context: 'analysis', session_id: 's', unit_idx: 1,
      delta: { output_tokens: 120, rate_tokens_per_sec: 45, is_thinking: true }, timestamp: NOW,
    }, NOW)
    upsertTokenDeltaBuffer(tokBuf, rateBuf, {
      context: 'analysis', session_id: 's', unit_idx: 1,
      delta: { output_tokens: 90, rate_tokens_per_sec: 52, is_thinking: false }, timestamp: NOW + 200,
    }, NOW + 200)
    expect(tokBuf.get(1)?.outputTokens).toBe(120)
    expect(tokBuf.get(1)?.isThinking).toBe(false)
    expect(rateBuf.get(1)).toBe(52)

    expect(sampleRateHistory([], -5)).toEqual([0])
    const s1 = sampleRateHistory([], 100)
    const s2 = sampleRateHistory(s1, 200)
    expect(s1).toEqual([100])
    expect(s2[1]).toBeCloseTo(135, 5)
    let capped: number[] = []
    for (let i = 0; i < RATE_HISTORY_MAX + 10; i++) capped = sampleRateHistory(capped, 50)
    expect(capped).toHaveLength(RATE_HISTORY_MAX)

    expect(calcChartCeiling([10, 30])).toBe(50)
    expect(calcChartCeiling([80])).toBe(100)
    expect(calcChartCeiling([180])).toBe(200)
    expect(calcChartCeiling([420])).toBe(500)
    expect(calcChartCeiling([1100])).toBe(1250)

    expect(buildSmoothAreaPaths([10], 400, 56, 100)).toEqual({ line: '', area: '', points: [] })
    const paths = buildSmoothAreaPaths([20, 60, 40], 400, 56, 100)
    expect(paths.points).toHaveLength(3)
    expect(paths.line).toMatch(/^M .* C .* C /)
    expect(paths.area).toContain('Z')

    // 对数刻度辅助：上限、Y 轴单调映射与刻度标签
    expect(calcLogChartCeiling([0, 45])).toBe(100)
    expect(calcLogChartCeiling([50, 320])).toBe(1000)
    expect(calcLogChartCeiling([1200, 3800])).toBe(10000)
    expect(calcLogChartCeiling([25000])).toBe(100000)

    const logTicks = buildLogScaleTicks(1000, 56)
    expect(logTicks.map((t) => t.label)).toEqual(['1k', '100', '10', '0'])
    expect(logTicks[0].y).toBeLessThan(logTicks[1].y)
    expect(logTicks[1].y).toBeLessThan(logTicks[2].y)
    expect(logTicks[2].y).toBeLessThan(logTicks[3].y)

    const logPaths = buildSmoothAreaPaths([0, 10, 100, 1000], 400, 56, 1000, 'log')
    expect(logPaths.points[0].y).toBeCloseTo(54, 4)
    expect(logPaths.points[0].y).toBeGreaterThan(logPaths.points[1].y)
    expect(logPaths.points[1].y).toBeGreaterThan(logPaths.points[2].y)
    expect(logPaths.points[2].y).toBeGreaterThan(logPaths.points[3].y)
    expect(logPaths.points[3].y).toBeCloseTo(3, 4)
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
    const v2Acc = recheckAccountOf({
      status: 'OK', calls: 10, coverage: { checked: 20, supported: 5 },
    })
    expect(v2Acc?.supported).toBe(5)
    expect(v2Acc?.v3_tiers).toBeNull()

    const v3Acc = recheckAccountOf({
      status: 'COMPLETED',
      calls: 4,
      coverage: {
        checked: 8,
        candidates_total: 8,
        v3_tiers: { RESOLVED_EXPLICIT: 3, RESOLVED_IMPLICIT: 2, PARTIALLY_RESOLVED: 1, UNRESOLVED: 2 },
      },
    })
    expect(v3Acc?.v3_tiers).toEqual({
      resolved_explicit: 3,
      resolved_implicit: 2,
      partially_resolved: 1,
      unresolved: 2,
    })

    const sBatch = mk({ phase: 'batch', batches_done: 1, total_batches: 3, summary_done: 2, recon_done: 1 })
    expect(overallPctOf(sBatch)).toBe(35)
    expect(stepSubtextOf(sBatch, 'batch', 0, 0)).toBe('摘要 2/3 · 调和 1/3')
    expect(stepProgressPctOf(sBatch, 'batch', 0, 0)).toBe(50)

    const sRoute = mk({
      phase: 'recheck',
      recheck_progress: { stage: 'u1_route', stage_done: 5, stage_total: 10, remaining: 5, items_total: 20, unit: '批' },
    })
    expect(recheckPctOf(sRoute)).toBe(29)
    expect(recheckMetaTextOf(sRoute)).toBe('双向路由 5/10 批 · 剩余 5 批（共 20 条待复检）')

    const sAudit = mk({
      phase: 'recheck',
      recheck_progress: { stage: 'u4_audit', stage_done: 10, stage_total: 10, remaining: 0, items_total: 10, unit: '条' },
    })
    expect(recheckPctOf(sAudit)).toBe(96)
    expect(overallPctOf(sAudit)).toBe(82)

    const sDone = mk({
      phase: 'complete',
      total_batches: 3,
      recheck: { status: 'COMPLETED', calls: 4, coverage: { checked: 8, candidates_total: 10 } },
    })
    expect(stepSubtextOf(sDone, 'batch', 0, 4)).toBe('3/3 卷')
    expect(stepSubtextOf(sDone, 'recheck', 1, 4)).toBe('已检 8/10 条')
    expect(stepProgressPctOf(mk({ phase: 'style' }), 'style', 2, 2)).toBeNull()
  })

  it('workspaceView：剥离归档时间戳并生成无冲突还原目录名', () => {
    expect(stripArchiveTimestamp('《剑来》-20261122-0315')).toBe('《剑来》')
    expect(stripArchiveTimestamp('《剑来》')).toBe('《剑来》')
    expect(suggestRestoreName('《剑来》-20261122-0315', ['《太白》'])).toBe('《剑来》')
    expect(suggestRestoreName('《剑来》-20261122-0315', ['《剑来》', '《剑来》-2'])).toBe('《剑来》-3')
    expect(suggestRestoreName('《剑来》', [], ['《剑来》'])).toBe('《剑来》-2')
  })

  it('summaryLanes：real_lanes lane_start/lane_done 驱动与 HTTP in-flight 状态对账', () => {
    let active = new Map()
    let finished = new Map()

    // 启动真实车道 900001 与并发风格分析 920001
    let res = applySummaryProgress({
      type: 'lane_start',
      lane_id: 900001,
      label: '卷1 批次摘要 · 1-10章',
      ts: 1000,
    }, active, finished)
    expect(res.changed).toBe(true)
    active = res.newActive
    finished = res.newFinished
    expect(active.get(900001)?.range).toBe('卷1 批次摘要 · 1-10章')

    res = applySummaryProgress({
      type: 'lane_start',
      lane_id: 920001,
      label: '文风指标提取',
      ts: 1100,
    }, active, finished)
    active = res.newActive
    finished = res.newFinished
    expect(active.has(920001)).toBe(true)

    // real_lanes phase="batch" 不应误清并发中的 920001 风格车道
    res = applySummaryProgress({
      type: 'phase',
      phase: 'batch',
      real_lanes: true,
      concurrency: 4,
    }, active, finished)
    active = res.newActive
    expect(active.has(900001)).toBe(true)
    expect(active.has(920001)).toBe(true)

    // lane_done 移入 finished
    res = applySummaryProgress({
      type: 'lane_done',
      lane_id: 900001,
      ok: true,
      ts: 2000,
    }, active, finished)
    active = res.newActive
    finished = res.newFinished
    expect(active.has(900001)).toBe(false)
    expect(finished.get(900001)).toEqual({ ok: true, range: '卷1 批次摘要 · 1-10章' })

    // reconcileSummaryLanes 与后端 inflight_lanes 对账
    const nowSec = Date.now() / 1000
    const reconciled = reconcileSummaryLanes(active, [
      { id: 920001, label: '文风指标提取（更新）', phase: 'style', started_at: nowSec - 2 },
      { id: 910001, label: '伏笔证据检索 #1', phase: 'recheck', started_at: nowSec - 1 },
    ])
    expect(reconciled.get(920001)?.range).toBe('文风指标提取（更新）')
    expect(reconciled.get(910001)?.range).toBe('伏笔证据检索 #1')

    // 当 inflight_lanes 不包含旧车道时移除
    const emptyReconciled = reconcileSummaryLanes(
      new Map([[999001, { range: '过期块', startedAt: Date.now() - 10_000 }]]),
      [],
    )
    expect(emptyReconciled.has(999001)).toBe(false)
  })
})

describe('settingsSchema', () => {
  it('131 项配置元模型与 11 个分组零遗漏、零重复且默认值辅助函数闭环', () => {
    expect(DEFAULT_SETTINGS_SCHEMA.total_fields).toBe(131)
    expect(DEFAULT_SETTINGS_SCHEMA.fields.length).toBe(131)
    expect(DEFAULT_SETTINGS_SCHEMA.groups.length).toBe(11)
    expect(ADVANCED_GROUP_IDS.length).toBe(7)
    expect(ALL_ADVANCED_FIELD_KEYS.size).toBe(106)

    const groupIds = new Set(DEFAULT_SETTINGS_SCHEMA.groups.map((g) => g.id))
    const pathSet = new Set<string>()
    for (const f of DEFAULT_SETTINGS_SCHEMA.fields) {
      expect(groupIds.has(f.group)).toBe(true)
      expect(pathSet.has(f.path)).toBe(false)
      pathSet.add(f.path)
    }

    const cfg = buildDefaultAppConfig()
    const poolSpec = DEFAULT_SETTINGS_SCHEMA.fields.find((f) => f.path === 'analysis.recheck_v3_pool_max_chars')!
    expect(isFieldModified(cfg, poolSpec)).toBe(false)
    setConfigValueByPath(cfg, poolSpec, 88888)
    expect(isFieldModified(cfg, poolSpec)).toBe(true)
    resetFieldToDefault(cfg, poolSpec)
    expect(isFieldModified(cfg, poolSpec)).toBe(false)
  })
})
