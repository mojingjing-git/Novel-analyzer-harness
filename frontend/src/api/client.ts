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

export interface RerunMeta {
  book_id: string
  chapters: number[]
  total: number
  overrides?: Record<string, unknown>
}

export interface AnalysisStatus {
  running: boolean
  mode?: 'idle' | 'queue' | 'rerun'
  rerun?: RerunMeta | null
  queue: QueueProgress
  items: QueueItemDto[]
  workspace_dir: string
  // 后端 status 接口给出两个独立字段：concurrency = 并发路数，
  // block_size = 批大小（单批最大章数）。两者不可混用。
  concurrency?: number
  block_size?: number
  // 运行概览车道对账源：pipeline 并发窗口内的真实在途批
  inflight_blocks?: Array<{ chapter: number; range: string; started_at: number }>
}

export interface RerunOverrides {
  model?: string
  reasoning_effort?: 'none' | 'low' | 'medium' | 'high' | 'xhigh' | 'max' | ''
  temperature?: number
  block_size?: number
  concurrency?: number
}

export interface RerunChaptersRequest {
  book_id: string
  chapters: string | number[]
  overrides?: RerunOverrides
  update_rolling?: boolean
}

export interface RerunChaptersResponse {
  ok: boolean
  book_id: string
  chapters: number[]
  total: number
}

export interface TokenCategory {
  input_tokens: number
  output_tokens: number
}

export interface ChapterStat {
  chapter: number
  range?: string
  chapters_count?: number
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

export interface SessionCostEstimate {
  usd: number | null
  unpriced: string[]
  priced_tokens?: number
  total_tokens?: number
  catalog_version?: string
}

export interface UsageModelRow {
  model: string
  in: number
  out: number
  cached: number
  usd: number | null
}

/** 全局累计账本（持久化 usage_state.json，跨进程累积） */
export interface UsageLedgerResponse {
  version: number
  updated_at: string | null
  per_model: UsageModelRow[]
  categories: Record<string, { in: number; out: number }>
  category_models: Record<string, string[]>
  chapters_done: number
  retries: number
  failed_tokens: number
  elapsed_seconds: number
  total_usd: number | null
  unpriced: string[]
  catalog_version: string
}

export interface SessionTokenStatsResponse {
  analysis: TokenStatsResponse
  summary: {
    categories: Record<string, TokenCategory>
    input_tokens: number
    output_tokens: number
    elapsed: number
  }
  cost?: SessionCostEstimate | null
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

export interface ModelSpecDto {
  id: string
  name: string
  family: string
  reasoning: boolean
  reasoning_type: string
  is_always_on?: boolean
  effort_values: string[]
  recommended_mode: Record<string, unknown>
  limit: { context?: number; output?: number }
  cost: { input?: number; output?: number; reasoning?: number; cache_read?: number; cache_write?: number }
  provider?: string
}

export interface ModelSpecResponse {
  found: boolean
  spec: ModelSpecDto | null
  catalog_version: string
}

export interface SyncModelsResponse {
  success: boolean
  version?: string
  total_models?: number
  error?: string
}

export type ConfigFieldType =
  | 'bool'
  | 'dict'
  | 'float'
  | 'int'
  | 'list_str'
  | 'optional_str'
  | 'str'

export interface ConfigGroupSchemaDto {
  id: string
  title: string
  description: string
  ui_placement: 'core' | 'recheck_endpoint' | 'advanced'
}

export interface ConfigFieldSchemaDto {
  path: string
  section: 'api' | 'analysis' | 'gui' | 'root'
  key: string
  field_type: ConfigFieldType
  default: unknown
  group: string
  label: string
  description: string
  min_val: number | null
  max_val: number | null
  step: number | null
  choices: string[]
  sensitive: boolean
  is_url: boolean
  status: 'active' | 'reserved' | 'legacy'
  ui_placement: 'core' | 'recheck_endpoint' | 'advanced'
}

export interface ConfigSchemaDto {
  total_fields: number
  groups: ConfigGroupSchemaDto[]
  fields: ConfigFieldSchemaDto[]
}

export interface ConfigProfileSummaryDto {
  base_url?: string
  model: string
  summary_model: string
  recheck_model: string
  block_size: number
  concurrency: number
  summary_concurrency: number
  recheck_concurrency: number
  summary_batch_size: number
  recheck_version: string
  recheck_v3_mode: string
  recheck_v3_pool_max_chars: number
  recheck_v3_batch_size: number
  recheck_v3_max_hops: number
  volume_compress_threshold: number
}

export interface ConfigProfileSlotDto {
  id: string
  kind: 'builtin' | 'user'
  slot_index: number
  name: string
  description: string
  badge: string
  recommended: boolean
  populated: boolean
  is_active: boolean
  matches_current: boolean
  include_credentials: boolean
  has_api_key: boolean
  has_recheck_api_key: boolean
  updated_at: string | null
  summary: ConfigProfileSummaryDto | null
  managed_field_count: number
}

export interface ConfigProfilesResponse {
  active_slot_id: string | null
  last_applied_slot_id: string | null
  is_modified_from_last: boolean
  store_corrupted: boolean
  slots: ConfigProfileSlotDto[]
}

export interface SaveProfilePayload {
  name?: string
  description?: string
  include_credentials?: boolean
  config?: {
    api: Record<string, unknown>
    analysis: Record<string, unknown>
  }
}

export interface SaveProfileResponse {
  slot: ConfigProfileSlotDto
  warnings: string[]
  profiles: ConfigProfilesResponse
}

export interface ClearProfileResponse {
  cleared: string
  profiles: ConfigProfilesResponse
}

export interface ApplyProfileResponse {
  applied_slot_id: string
  config: AppConfigDto
  warnings: string[]
  profiles: ConfigProfilesResponse
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
    sdk_max_retries?: number
    json_mode: string
    temperature: number
    temperature_step: number
    temperature_max_retries: number
    backoff_max_retries: number
    thinking_mode: Record<string, unknown>
    reasoning_effort?: string
    summary_model: string
    summary_thinking_mode: Record<string, unknown>
    provider: string
    streaming_enabled?: boolean
    temp_retry_wait_base?: number
    rate_limit_wait_base?: number
    rate_limit_wait_cap?: number
    rate_limit_min_backoff_retries?: number
    backoff_wait_cap?: number
    hard_timeout_margin?: number
    moderation_short_circuit_hits?: number
    breaker_open_after?: number
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
    max_relationships?: number
    max_themes?: number
    max_content_chars?: number
    max_verified_facts_in_prompt?: number
    foreshadow_kept_categories: string[]
    foreshadow_min_importance: string
    foreshadow_min_confidence: string
    max_foreshadow_catalog_high: number
    max_foreshadow_catalog_mid: number
    batch_summary_min_words: number
    final_report_min_words: number
    volume_compress_threshold: number
    volume_compress_group: number
    volume_compress_ratio?: number
    rolling_early_chapters: number
    rolling_update_interval?: number
    rolling_summary_max_tokens?: number
    rolling_summary_max_retries?: number
    rolling_summary_temperature?: number
    rolling_max_milestones: number
    rolling_max_momentum: number
    rolling_momentum_window?: number
    rolling_archive_trigger_count?: number
    rolling_max_paradigm_layers?: number
    rolling_max_causal_chains?: number
    rolling_archive_max_tokens?: number
    checkpoint_interval: number
    failed_block_retry_rounds?: number
    auto_archive: boolean
    auto_summary: boolean
    summary_concurrency: number
    summary_batch_size: number
    dormant_chapter_threshold?: number
    foreshadow_min_clue_chars?: number
    foreshadow_dedup_keyword_overlap?: number
    foreshadow_dedup_similarity?: number
    atlas_max_tokens?: number
    atlas_max_characters?: number
    atlas_max_locations?: number
    style_sample_chapters?: number
    style_sample_chars?: number
    style_max_tokens?: number
    style_temperature?: number
    anchor_quote_enabled: boolean
    anchor_min_chars: number
    recheck_max_calls: number
    recheck_max_tokens: number
    recheck_concurrency: number
    recheck_version?: string
    recheck_v3_mode?: string
    recheck_v3_batch_size?: number
    recheck_v3_pool_max_chars?: number
    recheck_v3_prompt_hard_cap_chars?: number
    recheck_v3_u1_superchunk_chars?: number
    recheck_v3_u1_split_batch_size?: number
    recheck_v3_u1_route_q_batch?: number
    recheck_v3_chapter_chars?: number
    recheck_v3_max_post_windows?: number
    recheck_v3_max_pre_windows?: number
    recheck_v3_hop2_new_cap?: number
    recheck_v3_hop3_new_cap?: number
    recheck_v3_max_hops?: number
    recheck_v3_max_audit_windows?: number
    recheck_v3_pre_lookback_units?: number
    recheck_v3_nms_cluster_radius?: number
    recheck_v3_nms_max_per_cluster?: number
    recheck_v2_batch_size: number
    recheck_v2_window_radius?: number
    recheck_v2_max_windows?: number
    recheck_v2_retry_rounds?: number
    recheck_v3_gray_u4?: boolean
    recheck_v3_mid_single_hop?: boolean
    recheck_model: string
    recheck_base_url: string
    recheck_api_key: string
    recheck_provider: string
    recheck_timeout: number
    recheck_json_mode: string
    recheck_reasoning_effort: string
    skip_moderation_blocked: boolean
    max_compressed_arcs: number
    max_recent_summaries: number
    max_pacing_tracker: number
    max_foreshadow_network: number
    max_world_building: number
    max_verified_facts: number
    max_long_term_arcs: number
    max_thematic_elements: number
    max_kb_character_states?: number
    max_kb_character_relationships?: number
  }
  gui: {
    language?: string  // i18n 接口预留
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
  has_blocks?: boolean
  blocks_count?: number
}

export interface BookInfo {
  id: string
  name: string
  source?: 'workspace' | 'archive'
  total_chapters: number
  has_report?: boolean
  has_aggregated?: boolean
  has_atlas?: boolean
}

export interface StyleProfileDto {
  book_id: string
  exists: boolean
  statistical: Record<string, number> | null
  semantic: Record<string, string> | null
  sample_chapters: number[]
}

export interface AtlasArcMilestone {
  chapter_range: string
  milestone: string
}

export interface AtlasCharacter {
  canonical_name: string
  aliases: string[]
  faction: string
  role_tier: 'protagonist' | 'major_ally' | 'major_antagonist' | 'key_support' | 'hidden_power' | string
  first_chapter: number
  identity_summary: string
  arc_milestones: AtlasArcMilestone[]
}

export interface AtlasRelationship {
  source: string
  target: string
  relation_tag: string
  category: 'ally' | 'hostile' | 'mentor' | 'romance' | 'faction' | 'complex' | string
  evolution: string
  evidence_chapters: number[]
}

export interface AtlasStage {
  stage_no: number
  title: string
  start_chapter: number
  end_chapter: number
  world_realm: string
  primary_location: string
  key_sub_locations: string[]
  active_factions: string[]
  core_conflict: string
  map_switch_reason: string
}

export interface AtlasTreeNode {
  name: string
  node_type: 'world' | 'region' | 'faction' | 'landmark' | string
  summary: string
  children: AtlasTreeNode[]
}

export interface StoryAtlasDto {
  book_id: string
  exists: boolean
  schema_version: number
  generated_at: string
  model: string
  characters: AtlasCharacter[]
  relationships: AtlasRelationship[]
  stages: AtlasStage[]
  world_tree: AtlasTreeNode[]
}

export interface CharacterListItem {
  name: string
  aliases?: string[]
  faction?: string
  role_tier?: string
  identity_summary?: string
  in_atlas?: boolean
  first_appearance?: number | null
  total_events: number
  chapters_count: number
}

export interface CharacterSemanticRel {
  peer: string
  direction: 'out' | 'in' | string
  relation_tag: string
  category: string
  evolution: string
  evidence_chapters: number[]
}

export interface CharacterCardData {
  name: string
  first_appearance: number | null
  chapters: number[]
  total_events: number
  aliases?: string[]
  faction?: string
  role_tier?: string
  identity_summary?: string
  arc_milestones?: AtlasArcMilestone[]
  semantic_relationships?: CharacterSemanticRel[]
  arcs: { chapter: number; surface_action: string; inner_motivation: string; change_delta: string; driver: string }[]
  events: { chapter: number; event: string; function?: string }[]
  states: Record<string, string>
  relationships: Record<string, { event_count: number; first_encounter: number }>
}

export interface SummaryInflightLane {
  id: number
  label: string
  phase: string
  started_at: number
}

export interface RecheckLiveProgress {
  engine?: string
  stage: string
  hop?: number
  stage_done: number
  stage_total: number
  remaining: number
  items_total?: number
  checked?: number
  calls?: number
  unit?: string
}

export interface SummaryStatus {
  running: boolean
  book_id: string
  phase: string
  batches_done: number
  total_batches: number
  summary_done?: number
  recon_done?: number
  error: string
  started_at: number
  finished_at: number
  concurrency?: number
  inflight_lanes?: SummaryInflightLane[]
  token_stats: Record<string, TokenCategory>
  recheck_progress?: RecheckLiveProgress | null
  /** 带证据复检（V2）的账：status/coverage/verdicts_file；未跑过为空对象 */
  recheck?: Record<string, unknown>
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
  needs_review?: boolean
  resolution_quality?: string
  source_type?: string
}

export type ForeshadowReviewAction = 'confirm_resolved' | 'reject_to_active' | 'mark_dormant'

export interface ForeshadowReviewPayload {
  action: ForeshadowReviewAction
  resolution?: string
  resolved_chapter?: number
  note?: string
}

export interface ForeshadowReviewResponse {
  book_id: string
  item: ForeshadowRankItem & { confidence: number; notes?: string[]; source_type: string }
  lifecycle_bars: Record<string, number>
  counts: { total: number; active: number; resolved: number; dormant: number }
}

export interface RecheckEvidenceDto {
  unit_id: number
  offset?: number
  quote: string
  role?: string
}

export interface RecheckVerdictItemDto {
  id: string
  candidate_id: string
  verdict: string
  tier: string
  unresolved_type: string
  answer: string
  quote: string
  reason: string
  applied: string
  resolved_chapter: string
  hops_used: number
  evidence_chain: RecheckEvidenceDto[]
  gate_demoted: boolean
  gate_reason: string
  repaired: boolean
  description: string
  question: string
  first_seen_chapter: number | null
  importance: string
  ledger_status?: string
  ledger_needs_review?: boolean
  ledger_resolution_quality?: string
  ledger_source_type?: string
  ledger_resolution?: string | null
  ledger_resolved_chapter?: number | null
}

export interface RecheckVerdictsResponse {
  book_id: string
  exists: boolean
  engine: string
  status: string
  status_reason: string
  model: string
  generated_at: string
  coverage: Record<string, unknown>
  candidates_total: number
  verdicts: RecheckVerdictItemDto[]
}

/**
 * 默认超时。之前所有请求都没有超时：后端一旦卡住（等模型、等磁盘），
 * 前端按钮会永久处于"请求中"禁用态，用户只能刷新页面。
 * 30s 覆盖常规读写；整本切章/预览/聚合/导出这类重活按调用传 `timeoutMs` 覆盖。
 */
const DEFAULT_TIMEOUT_MS = 30_000

/** `timeoutMs` 是本客户端自己的字段，不进 fetch。 */
type RequestOptions = RequestInit & { timeoutMs?: number }

async function request<T>(path: string, options?: RequestOptions): Promise<T> {
  // 受保护端点需 X-Session-Token header。
  // 合并顺序：Content-Type 默认 → 调用方 headers 覆盖 → token 最后（不可被覆盖）。
  // 注意 options 里的 headers 必须先展开再合并，整体 ...options 会把 headers 键整个替换。
  const { headers: extraHeaders, timeoutMs, ...rest } = options ?? {}
  const t = getSessionToken()
  // 调用方已给 signal 就尊重它（可能来自组件卸载），否则自己造一个超时信号。
  const signal = rest.signal ?? AbortSignal.timeout(timeoutMs ?? DEFAULT_TIMEOUT_MS)
  let res: Response
  try {
    res = await fetch(path, {
      ...rest,
      signal,
      headers: {
        'Content-Type': 'application/json',
        ...extraHeaders,
        ...(t ? { 'X-Session-Token': t } : {}),
      },
    })
  } catch (raw) {
    // 超时与主动中断都在这里：区分开，界面文案不同（超时=稍后重试；中断=正常取消）。
    const name = (raw as { name?: string } | null)?.name
    const err = new Error(name === 'TimeoutError' ? '请求超时，请稍后重试' : '请求已中断')
    const tagged = err as Error & { code?: string; detail?: unknown }
    tagged.code = name === 'TimeoutError' ? 'TIMEOUT' : 'ABORTED'
    tagged.detail = { code: tagged.code }
    throw err
  }
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
  // ok 但 body 非 JSON 时显式失败走统一错误提示——静默返回 {} 会让上游在深层字段上
  // TypeError 白屏且无从排查。
  // 空 body 仍宽容返回 {}（为 204 类端点留余地）。
  if (data === null && text) {
    const err = new Error(`响应不是有效 JSON (${res.status})`)
    ;(err as Error & { status?: number }).status = res.status
    throw err
  }
  return (data ?? {}) as T
}

/** 路径段编码：书名/文件名含 # ? % 等时不至于把 URL 截断到错误资源 */
function seg(s: string | number): string {
  return encodeURIComponent(String(s))
}

export const api = {
  // 基础
  health: () => request<{ status: string }>('/api/health'),

  // 设置
  getSettings: () => request<AppConfigDto>('/api/settings'),
  putSettings: (config: Partial<AppConfigDto>) =>
    request<AppConfigDto>('/api/settings', { method: 'PUT', body: JSON.stringify(config) }),
  patchSettings: (patch: {
    api?: Partial<AppConfigDto['api']>
    analysis?: Partial<AppConfigDto['analysis']>
    gui?: Partial<AppConfigDto['gui']>
  }) =>
    request<AppConfigDto>('/api/settings', { method: 'PATCH', body: JSON.stringify(patch) }),
  getPresets: () => request<{ presets: Record<string, { base_url: string; api_key: string; model: string }> }>('/api/settings/presets'),
  getSettingsSchema: () => request<ConfigSchemaDto>('/api/settings/schema'),
  getSettingsProfiles: () => request<ConfigProfilesResponse>('/api/settings/profiles'),
  saveSettingsProfile: (slotId: string, payload: SaveProfilePayload) =>
    request<SaveProfileResponse>(`/api/settings/profiles/${seg(slotId)}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  clearSettingsProfile: (slotId: string) =>
    request<ClearProfileResponse>(`/api/settings/profiles/${seg(slotId)}`, {
      method: 'DELETE',
    }),
  applySettingsProfile: (slotId: string) =>
    request<ApplyProfileResponse>(`/api/settings/profiles/${seg(slotId)}/apply`, {
      method: 'POST',
    }),
  getModels: () => request<{ models: string[] }>('/api/settings/models'),
  previewModels: (params: { base_url?: string; api_key?: string; provider?: string }) =>
    request<{ models: string[] }>('/api/settings/preview/models', { method: 'POST', body: JSON.stringify(params) }),
  probeThinking: (params: { base_url?: string; api_key?: string; provider?: string; model?: string }) =>
    request<ProbeThinkingResult>('/api/settings/probe-thinking', { method: 'POST', body: JSON.stringify(params) }),
  /** 探测复检判定通道（三级回退：请求字段 > 复检专用通道 > 主配置） */
  probeRecheck: (params: { base_url?: string; api_key?: string; provider?: string; model?: string }) =>
    request<ProbeThinkingResult>('/api/settings/probe-recheck', { method: 'POST', body: JSON.stringify(params) }),
  getModelSpec: (model: string) =>
    request<ModelSpecResponse>(`/api/settings/model-spec?model=${encodeURIComponent(model)}`),
  syncModelsCatalog: () =>
    request<SyncModelsResponse>('/api/settings/models/sync', { method: 'POST' }),

  // 分析控制
  startAnalysis: () => request<{ ok: boolean }>('/api/analysis/start', { method: 'POST' }),
  stopAnalysis: () => request<{ ok: boolean }>('/api/analysis/stop', { method: 'POST' }),
  rerunChapters: (payload: RerunChaptersRequest) =>
    request<RerunChaptersResponse>('/api/analysis/rerun', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  analysisStatus: () => request<AnalysisStatus>('/api/analysis/status'),
  getTokenStats: () => request<TokenStatsResponse>('/api/analysis/token_stats'),
  getSessionTokenStats: () => request<SessionTokenStatsResponse>('/api/analysis/token_stats/session'),
  getUsage: () => request<UsageLedgerResponse>('/api/analysis/usage'),
  clearUsage: () => request<{ success: boolean }>('/api/analysis/usage/clear', { method: 'POST' }),
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
  getStyleProfile: (book_id: string) => request<StyleProfileDto>(`/api/books/${seg(book_id)}/style_profile`),
  getRecheckVerdicts: (book_id: string) => request<RecheckVerdictsResponse>(`/api/books/${seg(book_id)}/recheck_verdicts`),
  getBookCharacters: (book_id: string) =>
    request<{ book_id: string; characters: CharacterListItem[] }>(`/api/books/${seg(book_id)}/characters`, { timeoutMs: 120_000 }),
  getCharacterCard: (book_id: string, name: string) =>
    request<{ book_id: string; character: CharacterCardData }>(`/api/books/${seg(book_id)}/characters/${seg(name)}`),

  // 最终总结
  startSummary: (book_id: string, start_chapter: number, end_chapter: number, batch_size: number = 0, concurrency: number = 0) =>
    request<{ ok: boolean }>('/api/summary/start', {
      method: 'POST',
      body: JSON.stringify({ book_id, start_chapter, end_chapter, batch_size, concurrency }),
    }),
  stopSummary: () => request<{ ok: boolean }>('/api/summary/stop', { method: 'POST' }),
  summaryStatus: () => request<SummaryStatus>('/api/summary/status'),

  // 切分
  previewSplit: (body: Record<string, unknown>) =>
    request<SplitterPreview>('/api/splitter/preview', {
      method: 'POST',
      body: JSON.stringify(body),
      timeoutMs: 120_000,
    }),
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
  restoreArchiveBlocks: (archive_name: string, target_name: string = '') =>
    request<{ ok: boolean; novel_name: string; blocks_count: number }>('/api/workspace/restore_blocks', {
      method: 'POST',
      body: JSON.stringify({ archive_name, target_name }),
      timeoutMs: 120_000,
    }),

  // Prompt
  promptPreview: (book_id: string, max_chars: number = 0, chapter: number = 1) =>
    request<{ book_id: string; chapter: number; chapter_label: string; chapter_total_chars: number; chapter_truncated: boolean; system_prompt: string; user_prompt: string; system_len: number; user_len: number; total_len: number; params: Record<string, unknown> }>('/api/prompt/preview', { method: 'POST', body: JSON.stringify({ book_id, max_chars, chapter }),
    timeoutMs: 120_000 }),

  // 可视化
  getTimeline: (book_id: string) => request<{ events: unknown[]; foreshadows: unknown[]; category_map_loaded?: boolean }>(`/api/viz/timeline/${seg(book_id)}`),
  getAtlas: (book_id: string) => request<StoryAtlasDto>(`/api/viz/atlas/${seg(book_id)}`),
  getForeshadowRanking: (book_id: string) => request<{
    book_id: string
    total: number
    ranking: ForeshadowRankItem[]
    lifecycle_bars?: Record<string, number>
  }>(`/api/books/${seg(book_id)}/foreshadow_ranking`),
  reviewForeshadowItem: (book_id: string, item_id: string, body: ForeshadowReviewPayload) =>
    request<ForeshadowReviewResponse>(`/api/books/${seg(book_id)}/ledger/${seg(item_id)}/review`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getForeshadowCategories: () => request<{
    schema_version: number
    fallback: string
    defs: { name: string; description: string; examples: string[] }[]
    kept: string[]
    min_importance: string
    min_confidence: string
  }>('/api/foreshadow/categories'),

  // 聚合导出（后端自动触发指纹缓存）
  exportAggregateExcel: (book_id: string) =>
    request<{ ok: boolean; path: string }>(`/api/aggregate/${seg(book_id)}/excel`, {
      method: 'POST',
      timeoutMs: 300_000,
    }),

  // 下载运行日志。受保护端点且返回 text/plain——不复用 request()
  // 的 JSON 解析契约（其对 200 + 非 JSON 显式抛错），独立 fetch + blob。
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

// 模块加载即尝试取 token；浏览器环境拿不到则保持 null（不附加 header）
initSessionToken()
