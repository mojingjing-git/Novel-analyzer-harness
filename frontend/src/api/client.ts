// API 客户端 — fetch 封装 + 全部 TS 类型定义

import { getSessionToken, initSessionToken } from './sessionToken'

export interface QueueItemDto {
  name: string
  blocks_dir: string
  workspace_dir: string
  status: string
  total_chapters: number
  completed_chapters: number
  block_size: number
  start_time: number
  end_time: number
  error_message: string
  archive_failed: boolean
}

export interface QueueProgress {
  total_items: number
  completed_items: number
  current_item: string
  current_progress: number
  current_total: number
}

export interface AnalysisStatus {
  running: boolean
  queue: QueueProgress
  items: QueueItemDto[]
  workspace_dir: string
  // H17 P3 V2 修复（2026-08-26）：后端 status 接口加 concurrency / block_size
  // 修复前 LaneView 用 block_size（每块几章）当并发数显示「N/4」错误
  // 实际配置 concurrency=8 时后端是 8 路并发，UI 标 4
  concurrency?: number
  block_size?: number
  // 运行概览车道对账源：pipeline 信号量窗口内的真实在途块（车道堆叠修复）
  inflight_blocks?: Array<{ chapter: number; range: string; started_at: number }>
}

export interface TokenCategory {
  input_tokens: number
  output_tokens: number
}

export interface ChapterStat {
  chapter: number
  elapsed: number
  input_tokens: number
  output_tokens: number
  retries?: number
  failed_tokens?: number
}

export interface TokenStatsResponse {
  categories: Record<string, TokenCategory>
  chapter_stats: ChapterStat[]
  elapsed: number
  running: boolean
  total_retries?: number
  total_failed_tokens?: number
  cached_tokens?: number
}

export interface SessionTokenStatsResponse {
  analysis: TokenStatsResponse
  summary: {
    categories: Record<string, TokenCategory>
    input_tokens: number
    output_tokens: number
    elapsed: number
  }
}

export interface ProbeThinkingItem {
  param: string
  thinking_mode: Record<string, unknown> | null
  reasoning_chars: number
  content_chars: number
  think_tag_chars: number
  reasoning_token_count: number
  worked: boolean | null
  error: string
  detection_breakdown: Record<string, boolean>
}

export interface ProbeThinkingResult {
  results: ProbeThinkingItem[]
  best: { thinking_mode: Record<string, unknown>; param: string } | null
  default_thinks: boolean
  default_detection_breakdown: Record<string, boolean>
  note: string
}

export interface AppConfigDto {
  api: {
    base_url: string
    api_key: string
    model: string
    max_tokens: number
    timeout: number
    summary_timeout: number
    max_retries: number
    json_mode: string
    temperature: number
    temperature_step: number
    temperature_max_retries: number
    backoff_max_retries: number
    thinking_mode: Record<string, unknown>
    summary_model: string
    summary_thinking_mode: Record<string, unknown>
    provider: string
  }
  analysis: {
    max_arc_length: number
    concurrency: number
    block_size: number
    encoding_priority: string[]
    max_arcs_in_prompt: number
    max_summaries_in_prompt: number
    timeline_truncate: number
    max_character_states: number
    max_world_items: number
    max_foreshadow_entries: number
    foreshadow_kept_categories: string[]
    foreshadow_min_importance: string
    foreshadow_min_confidence: string
    max_foreshadow_catalog_high: number
    max_foreshadow_catalog_mid: number
    batch_summary_min_words: number
    final_report_min_words: number
    rolling_early_chapters: number
    rolling_max_milestones: number
    rolling_max_momentum: number
    checkpoint_interval: number
    auto_archive: boolean
    auto_summary: boolean
    summary_concurrency: number
    summary_batch_size: number
    foreshadow_recheck_batch_size: number
    skip_moderation_blocked: boolean
    max_compressed_arcs: number
    max_recent_summaries: number
    max_pacing_tracker: number
    max_foreshadow_network: number
    max_world_building: number
    max_verified_facts: number
    max_long_term_arcs: number
    max_thematic_elements: number
    volume_compress_threshold: number
    volume_compress_group: number
  }
  gui: {
    language?: string  // i18n 接口预留（2026-09-02 L2）
  }
  working_directory: string | null
  knowledge_file: string
  workspace_dir: string
  [key: string]: unknown
}

export interface WorkspaceNovel {
  name: string
  blocks_count: number
  result_count: number
  dir_size: number
  dir_mtime: number
}

export interface WorkspaceArchive {
  name: string
  file_count: number
  total_size: number
  dir_mtime: number
}

export interface BookInfo {
  id: string
  name: string
  source: 'workspace' | 'archive'
  total_chapters: number
  has_report?: boolean
  has_aggregated?: boolean
}

export interface SummaryStatus {
  running: boolean
  book_id: string
  phase: string
  batches_done: number
  total_batches: number
  error: string
  started_at: number
  finished_at: number
  token_stats: Record<string, TokenCategory>
}

export interface LocationNormalizationStatus {
  running: boolean
  book_id: string
  phase: string
  batches_done: number
  total_batches: number
  error: string
  started_at: number
  finished_at: number
  token_stats: Record<string, unknown>
}

export interface MapDataResponse {
  book_id: string
  locations: unknown[]
  relationships: unknown[]
  needs_normalization: boolean
}

export interface SplitterPreview {
  chapters: { index: number; title: string; word_count: number; volume?: string | null; num?: number | null }[]
  total_chapters: number
  total_words: number
  total_volumes: number
  dedup_count: number
  detected_pattern: string
  pattern_name: string
  metadata: { title?: string; author?: string }
  stats: { avg: number; min: number; max: number; median: number }
  preview_count: number
}

export interface SplitterOption {
  pattern?: string
  mode?: 'auto' | 'custom'
  use_volume?: boolean
  min_words?: number
  merge_tiny?: boolean
  max_words?: number
  remove_ads?: boolean
  strip_whitespace?: boolean
}

export interface GraphNode {
  id: string
  name: string
  event_count: number
  chapters?: number[]
}

export interface GraphEdge {
  source: string
  target: string
  weight: number
}

export interface ForeshadowRankItem {
  id: string
  description: string
  composite_score: number
  importance: string
  confidence: number
  status: string
  first_seen_chapter: number
  last_seen_chapter: number
  span: number
  evidence_count: number
  evidence_chapters: number[]
  resolution: string | null
  resolved_chapter: number | null
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  // PR-1 收尾（2026-09-30）：受保护端点需 X-Session-Token header。
  // 合并顺序：Content-Type 默认 → 调用方 headers 覆盖 → token 最后（不可被覆盖）。
  // 注意 options 里的 headers 必须先展开再合并，整体 ...options 会把 headers 键整个替换。
  const { headers: extraHeaders, ...rest } = options ?? {}
  const t = getSessionToken()
  const res = await fetch(path, {
    ...rest,
    headers: {
      'Content-Type': 'application/json',
      ...extraHeaders,
      ...(t ? { 'X-Session-Token': t } : {}),
    },
  })
  // 关键：响应体只读取一次。之前先 res.json() 再 res.text() 会因为流已被消费而抛
  // “body stream already read”。现在统一读成文本，再按需要 JSON.parse。
  const text = await res.text()
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = null
  }
  if (!res.ok) {
    const detail =
      data && typeof data === 'object' && 'detail' in data
        ? (data as Record<string, unknown>).detail
        : undefined
    const message =
      typeof detail === 'string' && detail
        ? detail
        : text || `${res.status} ${res.statusText}`
    const err = new Error(message)
    ;(err as Error & { status?: number; detail?: unknown }).status = res.status
    ;(err as Error & { status?: number; detail?: unknown }).detail = detail
    throw err
  }
  // P3 收口（2026-08-24）：ok 但 body 非 JSON 此前静默返回 {}，
  // 上游在深层字段上 TypeError 白屏且无从排查；显式失败走统一错误提示。
  // 空 body 仍宽容返回 {}（为 204 类端点留余地）。
  if (data === null && text) {
    const err = new Error(`响应不是有效 JSON (${res.status})`)
    ;(err as Error & { status?: number }).status = res.status
    throw err
  }
  return (data ?? {}) as T
}

/** 路径段编码：书名/文件名含 # ? % 等时不至于把 URL 截断到错误资源（P2 审计） */
function seg(s: string | number): string {
  return encodeURIComponent(String(s))
}

export const api = {
  // 基础
  health: () => request<{ status: string }>('/api/health'),

  // 设置
  getSettings: () => request<AppConfigDto>('/api/settings'),
  putSettings: (config: Partial<AppConfigDto>) =>
    request<{ ok: boolean }>('/api/settings', { method: 'PUT', body: JSON.stringify(config) }),
  getPresets: () => request<{ presets: Record<string, { base_url: string; api_key: string; model: string }> }>('/api/settings/presets'),
  getModels: () => request<{ models: string[] }>('/api/settings/models'),
  previewModels: (params: { base_url?: string; api_key?: string; provider?: string }) =>
    request<{ models: string[] }>('/api/settings/preview/models', { method: 'POST', body: JSON.stringify(params) }),
  probeThinking: (params: { base_url?: string; api_key?: string; provider?: string; model?: string }) =>
    request<ProbeThinkingResult>('/api/settings/probe-thinking', { method: 'POST', body: JSON.stringify(params) }),

  // 分析控制
  startAnalysis: () => request<{ ok: boolean }>('/api/analysis/start', { method: 'POST' }),
  stopAnalysis: () => request<{ ok: boolean }>('/api/analysis/stop', { method: 'POST' }),
  analysisStatus: () => request<AnalysisStatus>('/api/analysis/status'),
  getTokenStats: () => request<TokenStatsResponse>('/api/analysis/token_stats'),
  getSessionTokenStats: () => request<SessionTokenStatsResponse>('/api/analysis/token_stats/session'),
  getBookTokenStats: (book_id: string) => request<{ book_id: string; analysis?: Record<string, unknown>; summary?: Record<string, unknown> }>(`/api/books/${seg(book_id)}/token_stats`),

  // 队列
  scanWorkspace: () => request<{ added: number }>('/api/queue/scan_workspace', { method: 'POST' }),
  removeQueueItem: (index: number) =>
    request<AnalysisStatus>('/api/queue/remove', { method: 'POST', body: JSON.stringify({ index }) }),
  moveQueueItemUp: (index: number) =>
    request<AnalysisStatus>('/api/queue/move_up', { method: 'POST', body: JSON.stringify({ index }) }),
  moveQueueItemDown: (index: number) =>
    request<AnalysisStatus>('/api/queue/move_down', { method: 'POST', body: JSON.stringify({ index }) }),
  deleteBook: (index: number) =>
    request<AnalysisStatus>('/api/queue/delete_book', { method: 'POST', body: JSON.stringify({ index }) }),
  resetQueueItem: (index: number) =>
    request<AnalysisStatus>('/api/queue/reset_item', { method: 'POST', body: JSON.stringify({ index }) }),
  clearQueue: () => request<AnalysisStatus>('/api/queue/clear', { method: 'POST' }),

  // 书目
  listBooks: () => request<BookInfo[]>('/api/books'),
  getChapterResult: (book_id: string, chapter: number) =>
    request<{ book_id: string; chapter: number; data: Record<string, unknown> }>(`/api/books/${seg(book_id)}/chapter/${seg(chapter)}`),
  getLatestChapter: (book_id: string) =>
    request<{ book_id: string; latest: number }>(`/api/books/${seg(book_id)}/latest_chapter`),
  getBookReport: (book_id: string) => request<{ book_id: string; report: string }>(`/api/books/${seg(book_id)}/report`),
  getBookCharacters: (book_id: string) =>
    request<{ book_id: string; characters: unknown[] }>(`/api/books/${seg(book_id)}/characters`),
  getCharacterCard: (book_id: string, name: string) =>
    request<{ book_id: string; character: unknown }>(`/api/books/${seg(book_id)}/characters/${seg(name)}`),

  // 最终总结
  startSummary: (book_id: string, start_chapter: number, end_chapter: number, batch_size: number, concurrency: number) =>
    request<{ ok: boolean }>('/api/summary/start', {
      method: 'POST',
      body: JSON.stringify({ book_id, start_chapter, end_chapter, batch_size, concurrency }),
    }),
  stopSummary: () => request<{ ok: boolean }>('/api/summary/stop', { method: 'POST' }),
  summaryStatus: () => request<SummaryStatus>('/api/summary/status'),

  startLocationNormalization: (book_id: string) =>
    request<{ ok: boolean }>('/api/location-normalization/start', {
      method: 'POST',
      body: JSON.stringify({ book_id }),
    }),
  stopLocationNormalization: () =>
    request<{ ok: boolean }>('/api/location-normalization/stop', { method: 'POST' }),
  locationNormalizationStatus: () =>
    request<LocationNormalizationStatus>('/api/location-normalization/status'),
  getLocationNormalizationResult: (book_id: string) =>
    request<{
      exists: boolean
      normalized_at?: string
      location_count?: number
      spatial_count?: number
    }>(`/api/location-normalization/result/${seg(book_id)}`),

  // 风格分析
  startStyle: (book_id: string, use_llm: boolean, limit: number) =>
    request<{ ok: boolean }>('/api/style/start', { method: 'POST', body: JSON.stringify({ book_id, use_llm, limit }) }),
  styleStatus: () => request<{ running: boolean; phase: string; error: string }>('/api/style/status'),
  stopStyle: () => request<{ ok: boolean }>('/api/style/stop', { method: 'POST' }),
  getStyleResult: (book_id: string) =>
    request<{ book_id: string; content: string }>(`/api/style/result/${seg(book_id)}`),

  // 切分
  previewSplit: (body: Record<string, unknown>) =>
    request<SplitterPreview>('/api/splitter/preview', { method: 'POST', body: JSON.stringify(body) }),
  inferBookName: (file_path: string) =>
    request<{ book_name: string }>('/api/splitter/infer_name', { method: 'POST', body: JSON.stringify({ file_path }) }),
  batchSplit: (body: {
    file_paths: string[]
    book_names: string[]
    mode: string
    pattern: string
    use_volume: boolean
    min_words: number
    merge_tiny: boolean
    max_words: number
    remove_ads: boolean
    strip_whitespace?: boolean
  }) =>
    request<{
      total: number
      success: number
      fail: number
      results: { file: string; book: string; total_chapters: number; ok: boolean; error: string }[]
    }>('/api/splitter/batch', { method: 'POST', body: JSON.stringify(body) }),

  // 工作区
  workspaceNovels: () => request<{ novels: WorkspaceNovel[] }>('/api/workspace/novels'),
  workspaceArchives: () => request<{ archives: WorkspaceArchive[] }>('/api/workspace/archives'),
  archiveNovel: (name: string) => request<{ ok: boolean }>('/api/workspace/archive', { method: 'POST', body: JSON.stringify({ novel_name: name }) }),
  archiveAll: () => request<{ ok: boolean; archived: number }>('/api/workspace/archive_all', { method: 'POST' }),
  deleteArchive: (name: string) => request<{ ok: boolean }>('/api/workspace/delete_archive', { method: 'POST', body: JSON.stringify({ archive_name: name }) }),

  // Prompt
  promptPreview: (book_id: string, max_chars: number = 0, chapter: number = 1) =>
    request<{ book_id: string; chapter: number; chapter_label: string; chapter_total_chars: number; chapter_truncated: boolean; system_prompt: string; user_prompt: string; system_len: number; user_len: number; total_len: number; params: Record<string, unknown> }>('/api/prompt/preview', { method: 'POST', body: JSON.stringify({ book_id, max_chars, chapter }) }),

  // 可视化
  getTimeline: (book_id: string) => request<{ events: unknown[]; foreshadows: unknown[]; category_map_loaded?: boolean }>(`/api/viz/timeline/${seg(book_id)}`),
  getForeshadowRanking: (book_id: string) => request<{
    book_id: string
    total: number
    ranking: ForeshadowRankItem[]
  }>(`/api/books/${seg(book_id)}/foreshadow_ranking`),
  getForeshadowCategories: () => request<{
    schema_version: number
    fallback: string
    defs: { name: string; description: string; examples: string[] }[]
    kept: string[]
    min_importance: string
    min_confidence: string
  }>('/api/foreshadow/categories'),
  getGraph: (book_id: string, params?: {
    chapter_start?: number
    chapter_end?: number
    min_edge_weight?: number
    max_nodes?: number
    min_node_count?: number
  }) => {
    const query = params
      ? '?' + Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null)
          .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
          .join('&')
      : ''
    return request<{
      nodes: GraphNode[]
      edges: GraphEdge[]
      total_characters: number
      total_edges: number
      filtered: Record<string, unknown>
      chapter_range: { min: number; max: number }
    }>(`/api/viz/graph/${seg(book_id)}${query}`)
  },
  getMap: (book_id: string) => request<{ locations: unknown[]; relationships: unknown[] }>(`/api/viz/map/${seg(book_id)}`),

  // 聚合
  runAggregate: (book_id: string, include_raw: boolean) =>
    request<{ ok: boolean; files: Record<string, string> }>('/api/aggregate/run', { method: 'POST', body: JSON.stringify({ book_id, include_raw }) }),
  getAggregateFiles: (book_id: string) =>
    request<{ files: { name: string; size_kb: number }[] }>(`/api/aggregate/${seg(book_id)}/files`),
  getAggregateFile: (book_id: string, name: string) =>
    request<{ content: unknown; is_json: boolean }>(`/api/aggregate/${seg(book_id)}/file/${seg(name)}`),
  exportAggregateExcel: (book_id: string) =>
    request<{ ok: boolean; path: string }>(`/api/aggregate/${seg(book_id)}/excel`, { method: 'POST' }),

  // 下载运行日志（PR-1 收尾）。受保护端点且返回 text/plain——不复用 request()
  // 的 JSON 解析契约（其 P3 规则对 200 + 非 JSON 显式抛错），独立 fetch + blob。
  downloadLogs: async (): Promise<void> => {
    const t = getSessionToken()
    const res = await fetch('/api/analysis/logs', {
      headers: t ? { 'X-Session-Token': t } : {},
    })
    if (!res.ok) {
      const err = new Error(`下载日志失败 (${res.status} ${res.statusText})`)
      ;(err as Error & { status?: number }).status = res.status
      throw err
    }
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'analyzer.log'
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  },
}

// PR-1 收尾：模块加载即尝试取 token；浏览器环境拿不到则保持 null（不附加 header）
initSessionToken()
