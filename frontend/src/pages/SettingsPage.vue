<script setup lang="ts">
import { ref, computed, watch, inject, onMounted, onUnmounted } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import { api, type AppConfigDto, type ModelSpecDto } from '../api/client'
import ToggleSwitch from '../components/ToggleSwitch.vue'
import {
  pruneEmptyStrings, mapFieldErrors, toggleCategoryIn, CATEGORY_CLEAR_FALLBACK,
  inferThinkingState, makeThinkingPresets, THINKING_DEPTH_PRESETS,
  composeThinkingMode, decomposeThinkingMode,
  THINKING_ANTHROPIC_CAVEAT,
  type ThinkingState,
} from '../utils/settingsForm'

// 设置项 tooltip 字典：key 与模板中 :title 表达式一一对应
const tooltips: Record<string, string> = {
  // 主 API 连接
  api_preset: '从已保存的厂商预设中快速填入 base_url / model / api_key；切到预设会自动覆盖当前未保存的输入',
  base_url: 'LLM 服务的入口地址，例如 https://api.openai.com/v1 或厂商网关地址',
  api_key: '厂商颁发的访问密钥；本地工具明文存储，仅写入本机 config.json',
  provider: '请求协议格式：\nauto = 按 base_url/API Key/模型名特征自动检测\nopenai = OpenAI 兼容 /chat/completions\nanthropic = Anthropic /v1/messages',
  // 模型调用与并发
  model: '章节分析（主调用）模型。点输入框展开列表（按当前 base_url/api_key 探测），也可手填自定义模型',
  thinking_depth: '控制模型思考深度（reasoning_effort/budget）。高优先级，浅度思考(low)可有效压制Token开销同时避免完全关闭思考导致输出崩塌',
  thinking_mode: '禁用/启用模型的思考链。仅在未设置思考深度时生效。\n点「自动探测」会让系统实测哪个参数有效',
  concurrency: '并行分析的章节块数。\n越大越快，但 token 峰值越高（受 API TPM/RPM 限制）',
  summary_model: '最终总结（卷摘要/脉络报告/写作风格/图谱）专用模型；留空 = 跟随主模型',
  summary_thinking: '最终总结思考控制；留空 = 跟随主思考配置',
  summary_concurrency: '最终总结并发数；0 = 严格跟随章节分析主并发',
  recheck_model: '伏笔复检（V3 / V2）专用模型；留空 = 跟随主模型',
  recheck_reasoning: '伏笔复检思考档位；留空 = 跟随主思考配置（仅 OpenAI 兼容端点生效）',
  recheck_concurrency: '伏笔复检并发数；0 = 严格跟随章节分析主并发',
  recheck_v3_gray_u4: 'V3 灰区终审（默认关闭）：开启后对多跳、非高置信或争议伏笔追加一轮 U4 终审，token 消耗约增加 33%',
  recheck_v3_mid_single_hop: 'V3 支线单跳省钱模式（默认关闭）：开启后中重要度支线伏笔缩窗至 3 个且跳过二三跳（高重要度主线仍保留 5 窗 3 跳），约再省 15%–25% token',
  // 运行与流程策略
  block_size: '每"块"包含的章节数。\n块越大单次请求 context 越长、token 消耗越大，但 KB 增量更稳定',
  summary_batch_size: '总结阶段的批次大小；每批含 N 卷',
  auto_archive: '分析完成后自动把工作区搬到「分析结果/」归档目录',
  auto_summary: '队列全部完成后自动启动最终总结',
  skip_moderation_blocked: '识别为内容审核拦截的章节：重试 1 次后跳过并标记，不再反复重试白烧成本',
  // 请求超时、JSON 模式与重试
  max_tokens: '单次 LLM 请求的最大输出 token 上限；超出将被截断',
  timeout: '单次分析与默认复检请求的 API 超时（秒）；超时后会进入重试链',
  summary_timeout: '最终总结（卷摘要/最终报告/风格提取/图谱）使用的 API 超时，建议比分析 Timeout 长（默认 600s）',
  json_mode: 'JSON 输出约束模式：\ndefault = 通用\nqwen = Qwen 系列需关 thinking\ndeepseek = DeepSeek V4\nglm47 = GLM-4.7',
  temperature: '单次请求的初始温度；后续重试会逐步降温',
  temperature_step: '每轮退火重试时降低的温度（例：起始 0.7、步长 0.15 → 0.7 → 0.55 → 0.4 → ...）',
  temperature_max_retries: '温度退火最大重试轮数；用尽后再走退避',
  backoff_max_retries: '指数退避最大轮数；等待时长 min(2^N, 60s)；429 会额外 3 轮并尊重 Retry-After',
  // 提示词上下文与滚动总结预算
  max_arc_length: '主线（角色弧光）最大字数；超过会在合并阶段裁剪',
  max_arcs_in_prompt: '每章 Prompt 中携带的最近弧光条数；过多会稀释注意力',
  max_summaries_in_prompt: '每章 Prompt 中携带的近期章节摘要数；影响上下文长度与连续性',
  timeline_truncate: 'Prompt 中携带的时间线条数上限；超出按章节倒序截断',
  max_character_states: 'Prompt 中携带的最近角色状态条数',
  max_world_items: 'Prompt 中携带的世界观条目上限',
  max_foreshadow_entries: 'Prompt 中携带的伏笔网络条目上限；超过按综合权重截断',
  batch_summary_min_words: '每卷摘要的最低字数要求；不足会被 LLM 补写',
  final_report_min_words: '最终报告的最低字数要求',
  rolling_early_chapters: '开篇多少章内不做滚动总结（让 KB 自然增长）',
  rolling_max_milestones: '滚动总结中里程碑条数上限；超出 FIFO 淘汰',
  rolling_max_momentum: '势头条目上限；超出触发归档压缩为里程碑',
  checkpoint_interval: '每处理多少块做一次 checkpoint 落盘；0=关闭',
}

const ADVANCED_FIELD_KEYS = new Set([
  'max_tokens', 'timeout', 'summary_timeout', 'json_mode',
  'temperature', 'temperature_step', 'temperature_max_retries', 'backoff_max_retries',
  'recheck_max_tokens', 'recheck_timeout', 'recheck_v2_batch_size',
  'max_arc_length', 'max_arcs_in_prompt', 'max_summaries_in_prompt', 'timeline_truncate',
  'max_character_states', 'max_world_items', 'max_foreshadow_entries',
  'batch_summary_min_words', 'final_report_min_words',
  'volume_compress_threshold', 'volume_compress_group',
  'rolling_early_chapters', 'rolling_max_milestones', 'rolling_max_momentum', 'checkpoint_interval',
  'foreshadow_min_importance', 'foreshadow_min_confidence',
  'max_foreshadow_catalog_high', 'max_foreshadow_catalog_mid', 'foreshadow_kept_categories',
])

const RECHECK_ENDPOINT_FIELD_KEYS = new Set([
  'recheck_base_url', 'recheck_api_key', 'recheck_provider', 'recheck_json_mode',
])

const config = ref<AppConfigDto | null>(null)
const loadError = ref('')
const presets = ref<Record<string, { base_url: string; model: string; api_key?: string }>>({})
const models = ref<string[]>([])
const saving = ref(false)
const saved = ref(false)
const savedSnapshot = ref<string | null>(null)
const configSnapshot = computed(() => JSON.stringify(config.value))
const hasUnsavedChanges = computed(() => savedSnapshot.value !== null && savedSnapshot.value !== configSnapshot.value)
let savedTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const showAdvanced = ref(false)
const showRecheckEndpoint = ref(false)

interface CategoryDef { name: string; description: string; examples: string[] }
const categoryDefs = ref<CategoryDef[]>([])
const keptSet = computed(() => new Set(config.value?.analysis.foreshadow_kept_categories ?? []))

// ===== 有效跟随只读提示 =====
const effectiveSummaryConc = computed(() => {
  const c = config.value?.analysis
  if (!c) return 1
  return c.summary_concurrency > 0 ? c.summary_concurrency : (c.concurrency || 1)
})
const effectiveRecheckConc = computed(() => {
  const c = config.value?.analysis
  if (!c) return 1
  return c.recheck_concurrency > 0 ? c.recheck_concurrency : (c.concurrency || 1)
})

// ===== 复检通道同源提示 =====
const recheckConflictHint = computed(() => {
  const a = config.value?.analysis
  if (!a) return ''
  const recheckModel = (a.recheck_model || '').trim()
  const recheckBase = (a.recheck_base_url || '').trim()
  const effectiveModel = (recheckModel || config.value?.api.model || '').trim()
  const effectiveBase = (recheckBase || config.value?.api.base_url || '').trim()
  if (effectiveBase.includes('minimaxi.com') && effectiveModel.includes('/')) {
    const src = recheckModel ? `判定模型 ${effectiveModel}` : `判定模型留空（跟随主模型 ${effectiveModel}）`
    return `⚠ ${src} 是网关专用名，当前判定通道 ${effectiveBase} 是官方端点——这个组合会在开跑时被阻断（400 unknown model）。请把判定通道改成网关地址，或改用官方端点支持的模型名。`
  }
  return ''
})

// ===== 复检判定通道：预设填充 + 通道探测（空白项回落当前表单主通道值） =====
const recheckProbeBusy = ref(false)
const recheckProbeError = ref('')
const recheckProbeResult = ref<Awaited<ReturnType<typeof api.probeRecheck>> | null>(null)

function onRecheckPreset(name: string) {
  const p = presets.value[name]
  if (p && config.value) {
    config.value.analysis.recheck_base_url = p.base_url
    config.value.analysis.recheck_model = p.model
    if (p.api_key) config.value.analysis.recheck_api_key = p.api_key
    showRecheckEndpoint.value = true
  }
}

async function runRecheckProbe() {
  const a = config.value?.analysis
  const mainApi = config.value?.api
  if (!a || !mainApi) return
  recheckProbeBusy.value = true
  recheckProbeError.value = ''
  recheckProbeResult.value = null
  try {
    const body: Record<string, string> = {}
    const effBase = (a.recheck_base_url || '').trim() || (mainApi.base_url || '').trim()
    const effKey = (a.recheck_api_key || '').trim() || (mainApi.api_key || '').trim()
    const effProv = (a.recheck_provider || '').trim() || (mainApi.provider || '').trim()
    const effModel = (a.recheck_model || '').trim() || (mainApi.model || '').trim()
    if (effBase) body.base_url = effBase
    if (effKey) body.api_key = effKey
    if (effProv) body.provider = effProv
    if (effModel) body.model = effModel
    recheckProbeResult.value = await api.probeRecheck(body)
  } catch (e) {
    recheckProbeError.value = e instanceof Error ? e.message : String(e)
  } finally {
    recheckProbeBusy.value = false
  }
}

// ===== 复检模型下拉（独立端点留空且未手动拉取时回 fallback 主通道列表） =====
const recheckModels = ref<string[]>([])
const showRecheckModelList = ref(false)
const recheckModelFilter = ref('')
const recheckModelBoxRef = ref<HTMLElement | null>(null)
const recheckModelsBusy = ref(false)
const recheckModelsError = ref('')

const recheckModelSourceList = computed(() => {
  if (recheckModels.value.length > 0) return recheckModels.value
  if (!(config.value?.analysis.recheck_base_url || '').trim()) return models.value
  return []
})

const filteredRecheckModels = computed(() => {
  const f = recheckModelFilter.value.trim().toLowerCase()
  const src = recheckModelSourceList.value
  if (!f) return src
  return src.filter((m: string) => m.toLowerCase().includes(f))
})

async function loadRecheckModels() {
  const a = config.value?.analysis
  const mainApi = config.value?.api
  if (!a || !mainApi || recheckModelsBusy.value) return
  recheckModelsBusy.value = true
  recheckModelsError.value = ''
  try {
    const body: Record<string, string> = {}
    const effBase = (a.recheck_base_url || '').trim() || (mainApi.base_url || '').trim()
    const effKey = (a.recheck_api_key || '').trim() || (mainApi.api_key || '').trim()
    const effProv = (a.recheck_provider || '').trim() || (mainApi.provider || '').trim()
    if (effBase) body.base_url = effBase
    if (effKey) body.api_key = effKey
    if (effProv) body.provider = effProv
    recheckModels.value = (await api.previewModels(body)).models
    showModelList.value = false
    showSummaryModelList.value = false
    showRecheckModelList.value = true
  } catch (e) {
    recheckModelsError.value = e instanceof Error ? e.message : String(e)
  } finally {
    recheckModelsBusy.value = false
  }
}

function pickRecheckModel(m: string) {
  if (config.value) config.value.analysis.recheck_model = m
  recheckModelFilter.value = ''
  recheckModelBoxRef.value?.querySelector('input')?.focus()
  showRecheckModelList.value = false
}

// ===== 伏笔分类 =====
function toggleCategory(name: string) {
  if (!config.value) return
  config.value.analysis.foreshadow_kept_categories = toggleCategoryIn(
    config.value.analysis.foreshadow_kept_categories, name)
}
function selectAllCategories() {
  if (!config.value) return
  config.value.analysis.foreshadow_kept_categories = categoryDefs.value.map(c => c.name)
}
function clearAllCategories() {
  if (!config.value) return
  config.value.analysis.foreshadow_kept_categories = [...CATEGORY_CLEAR_FALLBACK]
}
async function loadCategoryDefs() {
  try {
    const res = await api.getForeshadowCategories()
    categoryDefs.value = res.defs
  } catch (e) { console.error('加载伏笔分类失败', e) }
}

// ===== 主模型规格 & 总结模型规格 =====
const modelSpecResult = ref<{ model: string; spec: ModelSpecDto } | null>(null)
const modelSpec = computed(() => modelSpecResult.value?.model === config.value?.api.model.trim()
  ? modelSpecResult.value?.spec ?? null
  : null)
let modelSpecSeq = 0
const catalogVersion = ref('')
const catalogSyncing = ref(false)
const catalogSyncMsg = ref('')

async function fetchModelSpec(modelName?: string) {
  const m = (modelName ?? config.value?.api.model ?? '').trim()
  const seq = ++modelSpecSeq
  if (!m) {
    modelSpecResult.value = null
    return
  }
  const isCurrent = () => !disposed && seq === modelSpecSeq && m === config.value?.api.model.trim()
  try {
    const res = await api.getModelSpec?.(m)
    if (!isCurrent()) return
    modelSpecResult.value = res?.found && res.spec ? { model: m, spec: res.spec } : null
    if (res?.catalog_version) catalogVersion.value = res.catalog_version
  } catch {
    if (isCurrent()) modelSpecResult.value = null
  }
}

const summaryModelSpecResult = ref<{ model: string; spec: ModelSpecDto } | null>(null)
const summaryModelSpec = computed(() => {
  const sm = (config.value?.api.summary_model || '').trim()
  return sm && summaryModelSpecResult.value?.model === sm ? summaryModelSpecResult.value.spec : null
})
let summarySpecSeq = 0

async function fetchSummaryModelSpec(modelName?: string) {
  const m = (modelName ?? config.value?.api.summary_model ?? '').trim()
  const seq = ++summarySpecSeq
  if (!m) {
    summaryModelSpecResult.value = null
    return
  }
  const isCurrent = () => !disposed && seq === summarySpecSeq && m === (config.value?.api.summary_model || '').trim()
  try {
    const res = await api.getModelSpec?.(m)
    if (!isCurrent()) return
    summaryModelSpecResult.value = res?.found && res.spec ? { model: m, spec: res.spec } : null
    if (res?.catalog_version) catalogVersion.value = res.catalog_version
  } catch {
    if (isCurrent()) summaryModelSpecResult.value = null
  }
}

async function runSyncCatalog() {
  catalogSyncing.value = true
  catalogSyncMsg.value = ''
  try {
    const res = await api.syncModelsCatalog?.()
    if (res?.success) {
      catalogVersion.value = res.version ?? ''
      catalogSyncMsg.value = `同步成功 (共 ${res.total_models ?? 0} 款)`
      await Promise.all([fetchModelSpec(), fetchSummaryModelSpec()])
      setTimeout(() => { catalogSyncMsg.value = '' }, 3000)
    } else {
      catalogSyncMsg.value = `同步失败: ${res?.error || '未知错误'}`
    }
  } catch (e) {
    catalogSyncMsg.value = `同步失败: ${(e as Error).message}`
  } finally {
    catalogSyncing.value = false
  }
}

async function load() {
  loadError.value = ''
  try {
    config.value = await api.getSettings()
    savedSnapshot.value = configSnapshot.value
    presets.value = (await api.getPresets()).presets
    syncThinkingFromConfig()
    syncSummaryThinkingFromConfig()
    await Promise.all([fetchModelSpec(), fetchSummaryModelSpec()])
  } catch (e) {
    loadError.value = (e as Error).message || '无法连接后端服务'
  }
}

// ===== 主模型下拉 & 总结模型下拉 =====
const modelError = ref('')
async function loadModels() {
  const a = config.value?.api
  const body: { base_url?: string; api_key?: string; provider?: string } = {}
  if (a?.base_url?.trim()) body.base_url = a.base_url.trim()
  if (a?.api_key?.trim()) body.api_key = a.api_key.trim()
  if (a?.provider) body.provider = a.provider
  modelError.value = ''
  try { const res = await api.previewModels(body); models.value = res.models }
  catch (e) { modelError.value = '获取模型失败: ' + (e as Error).message }
}

const showModelList = ref(false)
const modelFilter = ref('')
const modelBoxRef = ref<HTMLElement | null>(null)

const filteredModels = computed(() => {
  const f = modelFilter.value.trim().toLowerCase()
  if (!f) return models.value
  return models.value.filter((m: string) => m.toLowerCase().includes(f))
})

async function openModelList() {
  showSummaryModelList.value = false
  showRecheckModelList.value = false
  showModelList.value = true
  if (models.value.length === 0) {
    await loadModels()
  }
}

function pickModel(m: string) {
  if (config.value) config.value.api.model = m
  modelFilter.value = ''
  modelBoxRef.value?.querySelector('input')?.focus()
  showModelList.value = false
  void fetchModelSpec(m)
}

const showSummaryModelList = ref(false)
const summaryModelFilter = ref('')
const summaryModelBoxRef = ref<HTMLElement | null>(null)

const filteredSummaryModels = computed(() => {
  const f = summaryModelFilter.value.trim().toLowerCase()
  if (!f) return models.value
  return models.value.filter((m: string) => m.toLowerCase().includes(f))
})

async function openSummaryModelList() {
  showModelList.value = false
  showRecheckModelList.value = false
  showSummaryModelList.value = true
  if (models.value.length === 0) {
    await loadModels()
  }
}

function pickSummaryModel(m: string) {
  if (config.value) config.value.api.summary_model = m
  summaryModelFilter.value = ''
  summaryModelBoxRef.value?.querySelector('input')?.focus()
  showSummaryModelList.value = false
  void fetchSummaryModelSpec(m)
}

function openRecheckModelList() {
  showModelList.value = false
  showSummaryModelList.value = false
  showRecheckModelList.value = true
}

function leftModelBox(event: FocusEvent): boolean {
  const box = event.currentTarget as HTMLElement
  return !(event.relatedTarget instanceof Node && box.contains(event.relatedTarget))
}

function onDocClick(e: MouseEvent) {
  const target = e.target as Node
  if (modelBoxRef.value && !modelBoxRef.value.contains(target)) {
    showModelList.value = false
  }
  if (summaryModelBoxRef.value && !summaryModelBoxRef.value.contains(target)) {
    showSummaryModelList.value = false
  }
  if (recheckModelBoxRef.value && !recheckModelBoxRef.value.contains(target)) {
    showRecheckModelList.value = false
  }
}
onMounted(() => {
  document.addEventListener('click', onDocClick)
  loadCategoryDefs()
})
onUnmounted(() => {
  disposed = true
  clearTimeout(savedTimer)
  document.removeEventListener('click', onDocClick)
})

function onPreset(name: string) {
  const p = presets.value[name]
  if (p && config.value) {
    config.value.api.base_url = p.base_url
    config.value.api.model = p.model
    if (p.api_key) config.value.api.api_key = p.api_key
    void fetchModelSpec(p.model)
  }
}

const fieldErrors = ref<Record<string, string>>({})
const saveError = ref('')

async function save() {
  if (!config.value || saving.value) return
  saving.value = true
  saved.value = false
  clearTimeout(savedTimer)
  fieldErrors.value = {}
  saveError.value = ''
  const submittedSnapshot = configSnapshot.value
  try {
    const payload = JSON.parse(submittedSnapshot) as Record<string, unknown>
    pruneEmptyStrings(payload)
    const updated = await api.putSettings(payload as Partial<AppConfigDto>)
    if (disposed) return
    if (configSnapshot.value === submittedSnapshot) {
      if (updated?.api && updated?.analysis) {
        config.value = updated
        syncThinkingFromConfig()
        syncSummaryThinkingFromConfig()
      }
      savedSnapshot.value = configSnapshot.value
      saved.value = true
      savedTimer = setTimeout(() => { saved.value = false }, 2000)
    } else {
      savedSnapshot.value = submittedSnapshot
    }
  } catch (e) {
    if (disposed) return
    const err = e as Error & { status?: number; detail?: unknown }
    if (err.status === 422 && Array.isArray(err.detail)) {
      const { fieldErrors: fe, msgs } = mapFieldErrors(err.detail)
      fieldErrors.value = fe
      if (Object.keys(fe).some((k) => ADVANCED_FIELD_KEYS.has(k))) {
        showAdvanced.value = true
      }
      if (Object.keys(fe).some((k) => RECHECK_ENDPOINT_FIELD_KEYS.has(k))) {
        showRecheckEndpoint.value = true
      }
      saveError.value = '保存失败（参数校验未通过）：\n' + msgs.join('\n')
    } else {
      saveError.value = '保存失败: ' + err.message
    }
  }
  finally { if (!disposed) saving.value = false }
}

function errCls(field: string): string {
  return fieldErrors.value[field] ? 'input-invalid' : ''
}

function discardChanges() {
  if (savedSnapshot.value === null || saving.value) return
  try {
    const restored = JSON.parse(savedSnapshot.value) as AppConfigDto
    config.value = restored
    fieldErrors.value = {}
    saveError.value = ''
    saved.value = false
    syncThinkingFromConfig()
    syncSummaryThinkingFromConfig()
    void Promise.all([fetchModelSpec(), fetchSummaryModelSpec()])
  } catch (e) {
    console.error('还原草稿失败', e)
  }
}

const jsonModes = [
  { value: 'default', label: 'default - 通用' },
  { value: 'qwen', label: 'qwen - Qwen系列' },
  { value: 'deepseek', label: 'deepseek - DeepSeek V4' },
  { value: 'glm47', label: 'glm47 - GLM-4.7' },
]

// ===== 主模型思考控制 =====
const thinkingModes = makeThinkingPresets('自动（跟随端点默认）')
const thinkingState = computed<ThinkingState>(() => inferThinkingState(config.value?.api.thinking_mode))
const thinkingDepth = ref('')
const thinkingDisableStr = ref('{}')
const dynamicThinkingOptions = ref<{ value: string; label: string }[]>([])

function syncThinkingFromConfig() {
  if (!config.value) return
  const tm = config.value.api.thinking_mode
  const dec = decomposeThinkingMode(tm)
  thinkingDepth.value = dec.depth
  if (dec.depth) {
    thinkingDisableStr.value = '{}'
  } else if (tm && Object.keys(tm).length > 0) {
    const s = JSON.stringify(tm)
    thinkingDisableStr.value = s
    if (!thinkingModes.some((o) => o.value === s) && !dynamicThinkingOptions.value.some((o) => o.value === s)) {
      dynamicThinkingOptions.value.push({ value: s, label: `已保存配置: ${s}` })
    }
  } else {
    thinkingDisableStr.value = '{}'
  }
}

function onThinkingDepthChange(val: string) {
  thinkingDepth.value = val
  if (!config.value) return
  config.value.api.thinking_mode = composeThinkingMode(val, '', thinkingDisableStr.value)
}

function onThinkingDisableChange(val: string) {
  thinkingDisableStr.value = val
  if (!config.value) return
  config.value.api.thinking_mode = composeThinkingMode(thinkingDepth.value, '', val)
}

// ===== 总结模型思考控制 =====
const summaryThinkingModes = makeThinkingPresets('跟随主思考配置')
const summaryThinkingState = computed<ThinkingState>(() => inferThinkingState(config.value?.api.summary_thinking_mode))
const summaryThinkingDepth = ref('')
const summaryThinkingDisableStr = ref('{}')
const dynamicSummaryThinkingOptions = ref<{ value: string; label: string }[]>([])

function syncSummaryThinkingFromConfig() {
  if (!config.value) return
  const tm = config.value.api.summary_thinking_mode
  const dec = decomposeThinkingMode(tm)
  summaryThinkingDepth.value = dec.depth
  if (dec.depth) {
    summaryThinkingDisableStr.value = '{}'
  } else if (tm && Object.keys(tm).length > 0) {
    const s = JSON.stringify(tm)
    summaryThinkingDisableStr.value = s
    if (!summaryThinkingModes.some((o) => o.value === s) && !dynamicSummaryThinkingOptions.value.some((o) => o.value === s)) {
      dynamicSummaryThinkingOptions.value.push({ value: s, label: `已保存配置: ${s}` })
    }
  } else {
    summaryThinkingDisableStr.value = '{}'
  }
}

function onSummaryThinkingDepthChange(val: string) {
  summaryThinkingDepth.value = val
  if (!config.value) return
  config.value.api.summary_thinking_mode = composeThinkingMode(val, '', summaryThinkingDisableStr.value)
}

function onSummaryThinkingDisableChange(val: string) {
  summaryThinkingDisableStr.value = val
  if (!config.value) return
  config.value.api.summary_thinking_mode = composeThinkingMode(summaryThinkingDepth.value, '', val)
}

const probeBusy = ref(false)
const probeResult = ref<Awaited<ReturnType<typeof api.probeThinking>> | null>(null)
const probeError = ref('')

const summaryProbeBusy = ref(false)
const summaryProbeResult = ref<Awaited<ReturnType<typeof api.probeThinking>> | null>(null)
const summaryProbeError = ref('')

const PROBE_DETECTOR_LABELS: Record<string, string> = {
  reasoning_content: 'message.reasoning_content',
  reasoning_details: 'message.reasoning_details',
  think_tags: 'content 含 <think>',
  thinking_tags: 'content 含 <thinking>',
  reasoning_tag: 'content 含 <reasoning>',
  usage_reasoning_tokens: 'usage.reasoning_tokens>0',
  anthropic_thinking_blocks: 'content list + type=thinking',
}

async function runProbeThinking() {
  if (!config.value) return
  probeBusy.value = true
  probeError.value = ''
  probeResult.value = null
  const a = config.value.api
  const body: { base_url?: string; api_key?: string; provider?: string; model?: string } = {}
  if (a.base_url?.trim()) body.base_url = a.base_url.trim()
  if (a.api_key?.trim()) body.api_key = a.api_key.trim()
  if (a.provider) body.provider = a.provider
  if (a.model?.trim()) body.model = a.model.trim()
  try {
    const res = await api.probeThinking(body)
    probeResult.value = res
    dynamicThinkingOptions.value = []
    if (res.best?.thinking_mode) {
      const s = JSON.stringify(res.best.thinking_mode)
      const dec = decomposeThinkingMode(res.best.thinking_mode)
      if (dec.depth) {
        thinkingDepth.value = dec.depth
        thinkingDisableStr.value = '{}'
        config.value.api.thinking_mode = res.best.thinking_mode
      } else {
        thinkingDepth.value = ''
        if (!thinkingModes.some((o) => o.value === s)) {
          dynamicThinkingOptions.value = [{ value: s, label: `探测命中: ${res.best.param || s}` }]
        }
        thinkingDisableStr.value = s
        config.value.api.thinking_mode = res.best.thinking_mode
      }
    }
  } catch (e) {
    probeError.value = '探测失败: ' + (e as Error).message
  } finally {
    probeBusy.value = false
  }
}

async function runSummaryProbeThinking() {
  if (!config.value) return
  const sm = (config.value.api.summary_model || '').trim()
  if (!sm) return
  summaryProbeBusy.value = true
  summaryProbeError.value = ''
  summaryProbeResult.value = null
  const a = config.value.api
  const body: { base_url?: string; api_key?: string; provider?: string; model?: string } = { model: sm }
  if (a.base_url?.trim()) body.base_url = a.base_url.trim()
  if (a.api_key?.trim()) body.api_key = a.api_key.trim()
  if (a.provider) body.provider = a.provider
  try {
    const res = await api.probeThinking(body)
    summaryProbeResult.value = res
    dynamicSummaryThinkingOptions.value = []
    if (res.best?.thinking_mode) {
      const s = JSON.stringify(res.best.thinking_mode)
      const dec = decomposeThinkingMode(res.best.thinking_mode)
      if (dec.depth) {
        summaryThinkingDepth.value = dec.depth
        summaryThinkingDisableStr.value = '{}'
        config.value.api.summary_thinking_mode = res.best.thinking_mode
      } else {
        summaryThinkingDepth.value = ''
        if (!summaryThinkingModes.some((o) => o.value === s)) {
          dynamicSummaryThinkingOptions.value = [{ value: s, label: `探测命中: ${res.best.param || s}` }]
        }
        summaryThinkingDisableStr.value = s
        config.value.api.summary_thinking_mode = res.best.thinking_mode
      }
    }
  } catch (e) {
    summaryProbeError.value = '探测失败: ' + (e as Error).message
  } finally {
    summaryProbeBusy.value = false
  }
}

// ===== 旧直达兼容：/settings?section=prompt 自动跳转到 /stats?tab=prompt =====
const route = inject(routeLocationKey, null)
const router = inject(routerKey, null)
watch(() => route?.query?.section, (sec) => {
  if (sec === 'prompt' && router && route) {
    const { section: _drop, ...rest } = route.query
    void router.replace({ path: '/stats', query: { ...rest, tab: 'prompt' } })
  }
}, { immediate: true })

onMounted(load)
</script>

<template>
  <div v-if="loadError" class="glass-card p-6 text-center space-y-3">
    <p class="text-sm" style="color: var(--win-danger)">设置加载失败：{{ loadError }}</p>
    <button class="glass-button" @click="load">重试</button>
  </div>
  <div class="max-w-6xl space-y-5" v-else-if="config">
    <div>
      <h2 class="section-title">设置</h2>
    </div>

    <!-- 核心区：主 API 连接 + 运行与流程策略 + 模型调用与并发（全展开功能分区） -->
    <div class="settings-cards-grid">
      <!-- 分区 1：主 API 连接 -->
      <div class="glass-card settings-card p-4 space-y-3">
        <div class="flex items-center justify-between gap-2 flex-wrap pb-2" style="border-bottom: 1px solid var(--win-stroke)">
          <h3 class="font-semibold" style="letter-spacing: -0.01em">主 API 连接</h3>
          <div class="flex items-center gap-2 flex-wrap">
            <span v-if="catalogSyncMsg" class="text-xs" :style="{ color: catalogSyncMsg.includes('失败') ? 'var(--win-danger)' : 'var(--win-success)' }">
              {{ catalogSyncMsg }}
            </span>
            <span v-else-if="catalogVersion" class="text-xs" style="color: var(--win-text-muted)">
              模型库: {{ catalogVersion }}
            </span>
            <button @click="runSyncCatalog" :disabled="catalogSyncing" class="glass-button btn-sm" style="font-size: 11px" title="从 models.dev 远程更新最新的模型规格与定价数据">
              {{ catalogSyncing ? '同步中...' : '更新模型库' }}
            </button>
          </div>
        </div>
        <div class="sf-row">
          <label class="sf-label" v-tooltip="tooltips.api_preset">API 预设:</label>
          <div class="sf-control">
            <select @change="onPreset(($event.target as HTMLSelectElement).value)" class="glass-select flex-1 min-w-0">
              <option value="">选择预设...</option>
              <option v-for="(_, name) in presets" :key="name" :value="name">{{ name }}</option>
            </select>
          </div>
        </div>
        <div class="sf-row">
          <label class="sf-label" v-tooltip="tooltips.base_url">Base URL:</label>
          <div class="sf-control">
            <input v-model="config.api.base_url" :class="errCls('base_url')" class="glass-input flex-1 min-w-0" />
          </div>
        </div>
        <div class="sf-row">
          <label class="sf-label" v-tooltip="tooltips.api_key">API Key:</label>
          <div class="sf-control">
            <input v-model="config.api.api_key" :class="errCls('api_key')" type="password" class="glass-input flex-1 min-w-0" />
          </div>
        </div>
        <div class="sf-row">
          <label class="sf-label" v-tooltip="tooltips.provider">协议格式:</label>
          <div class="sf-control">
            <select v-model="config.api.provider" class="glass-select flex-1 min-w-0">
              <option value="auto">auto（自动检测）</option>
              <option value="openai">openai（OpenAI 兼容）</option>
              <option value="anthropic">anthropic（/v1/messages）</option>
            </select>
          </div>
        </div>
      </div>

      <!-- 分区 2：运行与流程策略 -->
      <div class="glass-card settings-card p-4 space-y-3">
        <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">运行与流程策略</h3>
        <div class="sf-num-grid">
          <div class="sf-num-row">
            <label v-tooltip="tooltips.block_size">块大小 (章/块):</label>
            <input v-model.number="config.analysis.block_size" :class="errCls('block_size')" type="number" min="1" max="10" class="glass-input sf-num-input" />
          </div>
          <div class="sf-num-row">
            <label v-tooltip="tooltips.summary_batch_size">总结批次大小:</label>
            <input v-model.number="config.analysis.summary_batch_size" :class="errCls('summary_batch_size')" type="number" min="5" class="glass-input sf-num-input" />
          </div>
        </div>
        <div class="sf-row">
          <label class="sf-label" title="V3 采用全书双向路由、多跳侦探深挖与四防对称终审；V2 采用单跳严格核对">复检引擎:</label>
          <div class="sf-control">
            <select
              :value="config.analysis.recheck_version || 'v3'"
              @change="config.analysis.recheck_version = ($event.target as HTMLSelectElement).value"
              :class="errCls('recheck_version')"
              class="glass-select flex-1 min-w-0"
            >
              <option value="v3">V3 多跳深挖与对称终审（推荐·高召回零人工）</option>
              <option value="v2">V2 单跳严格布尔核对（兼容模式）</option>
            </select>
          </div>
        </div>
        <div class="sf-num-row">
          <label title="一次复检最多发多少次调用；超出记「未核对」，可续跑">复检调用上限:</label>
          <input v-model.number="config.analysis.recheck_max_calls" :class="errCls('recheck_max_calls')" type="number" min="1" class="glass-input sf-num-input" />
        </div>
        <div class="space-y-3 pt-1">
          <template v-if="(config.analysis.recheck_version || 'v3') === 'v3'">
            <div v-tooltip="tooltips.recheck_v3_gray_u4">
              <ToggleSwitch
                :modelValue="Boolean(config.analysis.recheck_v3_gray_u4)"
                @update:modelValue="config.analysis.recheck_v3_gray_u4 = $event"
                label="V3 灰区终审 U4（约 +33% token）"
              />
            </div>
            <div v-tooltip="tooltips.recheck_v3_mid_single_hop">
              <ToggleSwitch
                :modelValue="Boolean(config.analysis.recheck_v3_mid_single_hop)"
                @update:modelValue="config.analysis.recheck_v3_mid_single_hop = $event"
                label="V3 支线单跳省钱（再省约 15%–25% token）"
              />
            </div>
          </template>
          <div v-tooltip="tooltips.auto_archive">
            <ToggleSwitch v-model="config.analysis.auto_archive" label="自动归档" />
          </div>
          <div v-tooltip="tooltips.auto_summary">
            <ToggleSwitch v-model="config.analysis.auto_summary" label="队列完成后自动总结" />
          </div>
          <div v-tooltip="tooltips.skip_moderation_blocked">
            <ToggleSwitch v-model="config.analysis.skip_moderation_blocked" label="跳过内容审核拦截章节" />
          </div>
        </div>
      </div>

      <!-- 分区 3：模型调用与并发（默认不折叠，通栏统一管控所有阶段） -->
      <div
        class="glass-card card-allow-dropdown settings-card settings-span-2 p-4 space-y-4"
        :class="{ 'is-dropdown-open': showModelList || showSummaryModelList || showRecheckModelList }"
      >
        <div class="flex items-center justify-between gap-2 flex-wrap pb-2" style="border-bottom: 1px solid var(--win-stroke)">
          <div>
            <h3 class="font-semibold" style="letter-spacing: -0.01em">模型调用与并发</h3>
            <p class="text-xs mt-0.5" style="color: var(--win-text-secondary)">
              次级阶段留空模型/思考或并发设为 0 时，统一严格跟随「章节分析（主调用）」配置
            </p>
          </div>
          <button @click="loadModels" class="glass-button btn-sm shrink-0">刷新模型列表</button>
        </div>

        <p v-if="modelError" class="glass-tinted-red px-3 py-2 rounded text-xs">{{ modelError }}</p>

        <!-- 行 1：章节分析（主调用） -->
        <section class="glass-inset p-3.5 space-y-2.5">
          <div class="flex items-center justify-between gap-2 flex-wrap">
            <span class="text-sm font-semibold" style="color: var(--win-text-primary)">1. 章节分析（主调用）</span>
            <span class="text-xs" style="color: var(--win-text-muted)">作为次级阶段未单独指定时的默认基准</span>
          </div>
          <div class="mc-row-grid">
            <div class="mc-field mc-field-wide">
              <label class="mc-label" v-tooltip="tooltips.model">主模型</label>
              <div class="relative flex-1 min-w-0" ref="modelBoxRef" @focusout="leftModelBox($event) && (showModelList = false)">
                <input
                  v-model="config.api.model"
                  :class="errCls('model')"
                  @focus="openModelList"
                  @click="openModelList"
                  @input="showModelList = true; modelFilter = config?.api.model || ''"
                  @blur="fetchModelSpec()"
                  class="glass-input w-full"
                  placeholder="点击选择或输入自定义模型名"
                />
                <div
                  v-if="showModelList"
                  @mousedown.prevent
                  class="model-dropdown absolute z-30 mt-1 w-full max-h-60 overflow-auto p-1 space-y-0.5 glass-elevated"
                >
                  <div v-if="filteredModels.length === 0" class="px-2 py-1.5 text-xs" style="color: var(--win-text-muted)">
                    无匹配模型，以上方输入值为准
                  </div>
                  <button
                    v-for="m in filteredModels"
                    :key="m"
                    type="button"
                    @click="pickModel(m)"
                    class="model-option block w-full text-left px-2 py-1.5 rounded-md text-sm"
                    style="color: var(--win-text-primary)"
                  >{{ m }}</button>
                </div>
              </div>
            </div>

            <div class="mc-field">
              <label class="mc-label" v-tooltip="tooltips.thinking_depth">思考深度</label>
              <select :value="thinkingDepth" @change="onThinkingDepthChange(($event.target as HTMLSelectElement).value)" class="glass-select w-full min-w-0">
                <option v-for="d in THINKING_DEPTH_PRESETS" :key="d.value" :value="d.value">{{ d.label }}</option>
              </select>
            </div>

            <div class="mc-field">
              <label class="mc-label" v-tooltip="tooltips.thinking_mode">
                完全禁用
                <span v-if="thinkingDepth" class="text-xs ml-1" style="color: var(--win-text-muted)">(深度接管)</span>
              </label>
              <select
                :value="thinkingDisableStr"
                @change="onThinkingDisableChange(($event.target as HTMLSelectElement).value)"
                :disabled="Boolean(thinkingDepth)"
                class="glass-select w-full min-w-0"
                :title="thinkingDepth ? '已启用思考深度控制，禁用开关被高优先级覆盖' : '仅在未指定思考深度时生效'"
              >
                <option v-for="m in [...thinkingModes, ...dynamicThinkingOptions]" :key="m.value" :value="m.value">{{ m.label }}</option>
              </select>
            </div>

            <div class="mc-field mc-conc-field">
              <label class="mc-label" v-tooltip="tooltips.concurrency">分析并发</label>
              <input v-model.number="config.analysis.concurrency" :class="errCls('concurrency')" type="number" min="1" max="20" class="glass-input w-full" />
            </div>

            <div class="mc-action-cell">
              <button @click="runProbeThinking" :disabled="probeBusy || !config?.api.model" class="glass-button btn-sm shrink-0" title="对当前主模型发几次微请求，实测有效思考控制/深度参数，命中后自动应用">
                {{ probeBusy ? '探测中...' : '自动探测' }}
              </button>
            </div>
          </div>

          <div v-if="modelSpec" class="text-xs px-2.5 py-1 rounded glass-tinted-blue flex items-center gap-2 flex-wrap" style="color: var(--win-accent, #0078d4)">
            <span>💡 models.dev 规范：{{ modelSpec.name }}</span>
            <span v-if="modelSpec.reasoning">
              · 支持思考控制（{{ modelSpec.reasoning_type === 'effort' ? '档位: ' + (modelSpec.effort_values?.join('/') || 'low/medium/high') : (modelSpec.reasoning_type === 'budget_tokens' ? 'Token 预算' : '开/关') }}）
            </span>
            <span v-else style="color: var(--win-text-secondary)">· 原生无思考链</span>
            <span v-if="modelSpec.limit?.context">· 上下文 {{ (modelSpec.limit.context / 1000).toFixed(0) }}k</span>
            <span v-if="modelSpec.cost?.input">· 费率 ${{ modelSpec.cost.input }}/MIn</span>
          </div>

          <div v-if="thinkingState === 'depth_low'" class="text-xs glass-tinted-blue px-2.5 py-1 rounded" style="color: var(--win-accent, #0078d4)">
            ⚡ 浅度思考模式 (low)：有效抑制思考膨胀与耗时，保留必要推理，推荐小说分析使用
          </div>
          <div v-else-if="thinkingState === 'depth_medium'" class="text-xs glass-tinted-blue px-2.5 py-1 rounded" style="color: var(--win-accent, #0078d4)">
            ⚡ 中度思考模式 (medium)：平衡思考耗时与长篇逻辑推理深度
          </div>
          <div v-else-if="thinkingState === 'depth_high'" class="text-xs glass-tinted-blue px-2.5 py-1 rounded" style="color: var(--win-accent, #0078d4)">
            ⚡ 深度思考模式 (high)：完整展开长程链式思考，耗时与 Token 开销较大
          </div>
          <div v-else-if="thinkingState === 'budget'" class="text-xs glass-tinted-blue px-2.5 py-1 rounded" style="color: var(--win-accent, #0078d4)">
            ⚡ 思考 Token 预算限制模式：在指定预算内进行思考控制
          </div>
          <div v-else-if="thinkingState === 'disabled'" class="text-xs glass-tinted-green px-2.5 py-1 rounded">
            ✓ 已显式禁用思考链<span class="ml-1" style="color: var(--win-text-secondary)">（{{ THINKING_ANTHROPIC_CAVEAT }}）</span>
          </div>
          <div v-else-if="thinkingState === 'default'" class="text-xs glass-tinted-red px-2.5 py-1 rounded" style="color: var(--win-warning)">
            ⚠ 跟随端点默认：部分模型（M3/GLM 等）的 chat/completions 端点默认开启思考链，大 prompt 可能长时间无响应——建议点「自动探测」
          </div>
          <div v-else-if="thinkingState === 'custom'" class="text-xs glass-tinted-blue px-2.5 py-1 rounded">
            ℹ 已配置自定义思考参数
          </div>

          <div v-if="probeResult" class="space-y-1.5 text-xs">
            <p v-if="probeResult.note" :class="probeResult.best || !probeResult.default_thinks ? 'glass-tinted-green px-2 py-1 rounded' : 'glass-tinted-red px-2 py-1 rounded'">
              {{ probeResult.note }}
            </p>
            <details v-if="probeResult.default_detection_breakdown && Object.values(probeResult.default_detection_breakdown).some(v => v)" class="glass-inset px-2 py-1">
              <summary style="cursor: pointer; color: var(--win-warning)">
                ⚠ 基线检测到 thinking 痕迹（点击展开分项详情）
              </summary>
              <div class="mt-1 space-y-0.5 pl-3">
                <div v-for="(hit, key) in probeResult.default_detection_breakdown" :key="key" class="flex gap-2">
                  <span v-if="hit" style="color: var(--win-warning)">●</span>
                  <span v-else style="color: var(--win-text-muted)">○</span>
                  <span :style="hit ? 'color: var(--win-text-primary)' : 'color: var(--win-text-muted)'">
                    {{ PROBE_DETECTOR_LABELS[key] || key }}
                  </span>
                </div>
              </div>
            </details>
            <div v-for="r in probeResult.results" :key="r.param" class="flex gap-2 items-center flex-wrap">
              <span class="w-40 shrink-0" style="color: var(--win-text-secondary)">{{ r.param }}</span>
              <span v-if="r.worked === true" style="color: var(--win-success)">✓ 有效</span>
              <span v-else-if="r.worked === false" style="color: var(--win-danger)">✗ 无效</span>
              <span v-else style="color: var(--win-text-muted)">基线</span>
              <span style="color: var(--win-text-muted)">
                思考 {{ r.reasoning_chars }}字 / 内容 {{ r.content_chars }}字
                <template v-if="r.think_tag_chars > 0"> / 标签 {{ r.think_tag_chars }}字</template>
                <template v-if="r.reasoning_token_count > 0"> / tokens {{ r.reasoning_token_count }}</template>
              </span>
              <span v-if="r.error" style="color: var(--win-danger)">{{ r.error }}</span>
            </div>
          </div>
          <p v-if="probeError" class="glass-tinted-red px-3 py-2 rounded text-xs">{{ probeError }}</p>
        </section>

        <!-- 行 2：最终总结 -->
        <section class="glass-inset p-3.5 space-y-2.5">
          <div class="flex items-center justify-between gap-2 flex-wrap">
            <span class="text-sm font-semibold" style="color: var(--win-text-primary)">2. 最终总结（卷摘要 / 脉络报告 / 写作风格 / 图谱）</span>
            <span class="text-xs" style="color: var(--win-text-muted)">
              当前生效：{{ (config.api.summary_model || '').trim() || `跟随主模型 (${config.api.model || '未填'})` }} · {{ effectiveSummaryConc }} 路并发
            </span>
          </div>
          <div class="mc-row-grid">
            <div class="mc-field mc-field-wide">
              <label class="mc-label" v-tooltip="tooltips.summary_model">总结模型</label>
              <div class="relative flex-1 min-w-0" ref="summaryModelBoxRef" @focusout="leftModelBox($event) && (showSummaryModelList = false)">
                <input
                  v-model="config.api.summary_model"
                  @focus="openSummaryModelList"
                  @click="openSummaryModelList"
                  @input="showSummaryModelList = true; summaryModelFilter = config?.api.summary_model || ''"
                  @blur="fetchSummaryModelSpec()"
                  class="glass-input w-full"
                  placeholder="留空 = 跟随主模型（点击选择或手填）"
                />
                <div
                  v-if="showSummaryModelList"
                  @mousedown.prevent
                  class="model-dropdown absolute z-30 mt-1 w-full max-h-60 overflow-auto p-1 space-y-0.5 glass-elevated"
                >
                  <div v-if="filteredSummaryModels.length === 0" class="px-2 py-1.5 text-xs" style="color: var(--win-text-muted)">
                    无匹配模型（留空跟随主模型）
                  </div>
                  <button
                    v-for="m in filteredSummaryModels"
                    :key="m"
                    type="button"
                    @click="pickSummaryModel(m)"
                    class="model-option block w-full text-left px-2 py-1.5 rounded-md text-sm"
                    style="color: var(--win-text-primary)"
                  >{{ m }}</button>
                </div>
              </div>
            </div>

            <div class="mc-field">
              <label class="mc-label" v-tooltip="tooltips.summary_thinking">思考深度</label>
              <select :value="summaryThinkingDepth" @change="onSummaryThinkingDepthChange(($event.target as HTMLSelectElement).value)" class="glass-select w-full min-w-0">
                <option v-for="d in THINKING_DEPTH_PRESETS" :key="d.value" :value="d.value">{{ d.label }}</option>
              </select>
            </div>

            <div class="mc-field">
              <label class="mc-label" v-tooltip="tooltips.summary_thinking">
                思考控制
                <span v-if="summaryThinkingDepth" class="text-xs ml-1" style="color: var(--win-text-muted)">(深度接管)</span>
              </label>
              <select
                :value="summaryThinkingDisableStr"
                @change="onSummaryThinkingDisableChange(($event.target as HTMLSelectElement).value)"
                :disabled="Boolean(summaryThinkingDepth)"
                class="glass-select w-full min-w-0"
              >
                <option v-for="m in [...summaryThinkingModes, ...dynamicSummaryThinkingOptions]" :key="m.value" :value="m.value">{{ m.label }}</option>
              </select>
            </div>

            <div class="mc-field mc-conc-field">
              <label class="mc-label" v-tooltip="tooltips.summary_concurrency">总结并发 (0=跟随)</label>
              <input v-model.number="config.analysis.summary_concurrency" :class="errCls('summary_concurrency')" type="number" min="0" max="20" class="glass-input w-full" />
            </div>

            <div class="mc-action-cell">
              <button
                @click="runSummaryProbeThinking"
                :disabled="summaryProbeBusy || !(config?.api.summary_model || '').trim()"
                class="glass-button btn-sm shrink-0"
                :title="(config?.api.summary_model || '').trim() ? '对总结专用模型发微请求实测思考参数' : '请先指定独立的总结模型（留空时沿用主模型探测结果）'"
              >
                {{ summaryProbeBusy ? '探测中...' : '探测总结模型' }}
              </button>
            </div>
          </div>

          <div v-if="summaryModelSpec" class="text-xs px-2.5 py-1 rounded glass-tinted-blue flex items-center gap-2 flex-wrap" style="color: var(--win-accent)">
            <span>💡 models.dev 规范：{{ summaryModelSpec.name }}</span>
            <span v-if="summaryModelSpec.reasoning">
              · 支持思考控制（{{ summaryModelSpec.reasoning_type === 'effort' ? '档位: ' + (summaryModelSpec.effort_values?.join('/') || 'low/medium/high') : (summaryModelSpec.reasoning_type === 'budget_tokens' ? 'Token 预算' : '开/关') }}）
            </span>
            <span v-else style="color: var(--win-text-secondary)">· 原生无思考链</span>
            <span v-if="summaryModelSpec.limit?.context">· 上下文 {{ (summaryModelSpec.limit.context / 1000).toFixed(0) }}k</span>
            <span v-if="summaryModelSpec.cost?.input">· 费率 ${{ summaryModelSpec.cost.input }}/MIn</span>
          </div>

          <div v-if="summaryThinkingState !== 'default'" class="text-xs px-2.5 py-1 rounded" :class="summaryThinkingState === 'disabled' ? 'glass-tinted-green' : 'glass-tinted-blue'">
            <template v-if="summaryThinkingState === 'disabled'">✓ 总结已显式禁用思考链</template>
            <template v-else-if="summaryThinkingState === 'depth_low'">⚡ 总结浅度思考 (low)</template>
            <template v-else-if="summaryThinkingState === 'depth_medium'">⚡ 总结中度思考 (medium)</template>
            <template v-else-if="summaryThinkingState === 'depth_high'">⚡ 总结深度思考 (high)</template>
            <template v-else-if="summaryThinkingState === 'budget'">⚡ 总结思考 Token 预算模式</template>
            <template v-else>ℹ 总结已配置自定义思考参数</template>
          </div>
          <div v-if="summaryProbeResult?.note" class="text-xs px-2.5 py-1 rounded" :class="summaryProbeResult.best || !summaryProbeResult.default_thinks ? 'glass-tinted-green' : 'glass-tinted-orange'">
            {{ summaryProbeResult.note }}
          </div>
          <p v-if="summaryProbeError" class="glass-tinted-red px-3 py-2 rounded text-xs">{{ summaryProbeError }}</p>
        </section>

        <!-- 行 3：伏笔复检（含可按需展开的独立端点/Key） -->
        <section class="glass-inset p-3.5 space-y-2.5">
          <div class="flex items-center justify-between gap-2 flex-wrap">
            <span class="text-sm font-semibold" style="color: var(--win-text-primary)">3. 伏笔复检（V3 / V2）</span>
            <span class="text-xs" style="color: var(--win-text-muted)">
              当前生效：{{ (config.analysis.recheck_model || '').trim() || `跟随主模型 (${config.api.model || '未填'})` }} · {{ effectiveRecheckConc }} 路并发
            </span>
          </div>
          <div class="mc-row-grid">
            <div class="mc-field mc-field-wide">
              <label class="mc-label" v-tooltip="tooltips.recheck_model">复检模型</label>
              <div class="relative flex-1 min-w-0" ref="recheckModelBoxRef" @focusout="leftModelBox($event) && (showRecheckModelList = false)">
                <input
                  v-model="config.analysis.recheck_model"
                  :class="errCls('recheck_model')"
                  type="text"
                  class="glass-input w-full"
                  placeholder="留空 = 跟随主模型"
                  @focus="openRecheckModelList"
                  @click="openRecheckModelList"
                  @input="showRecheckModelList = true; recheckModelFilter = config?.analysis.recheck_model || ''"
                />
                <div
                  v-if="showRecheckModelList"
                  @mousedown.prevent
                  class="model-dropdown absolute z-30 mt-1 w-full max-h-60 overflow-auto p-1 space-y-0.5 glass-elevated"
                >
                  <div v-if="filteredRecheckModels.length === 0" class="px-2 py-1.5 text-xs" style="color: var(--win-text-muted)">
                    展开「独立端点」点击获取模型，或直接输入模型名
                  </div>
                  <button
                    v-for="m in filteredRecheckModels"
                    :key="m"
                    type="button"
                    @click="pickRecheckModel(m)"
                    class="model-option block w-full text-left px-2 py-1.5 rounded-md text-sm"
                    style="color: var(--win-text-primary)"
                  >{{ m }}</button>
                </div>
              </div>
            </div>

            <div class="mc-field">
              <label class="mc-label" v-tooltip="tooltips.recheck_reasoning" title="思考档位仅对 OpenAI 兼容端点生效；Anthropic 协议请通过独立端点或保持跟随">思考档位 (OpenAI)</label>
              <select v-model="config.analysis.recheck_reasoning_effort" class="glass-select w-full min-w-0">
                <option value="">跟随主思考配置</option>
                <option value="low">low（推荐·压低耗时）</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
              </select>
            </div>

            <div class="mc-field mc-conc-field">
              <label class="mc-label" v-tooltip="tooltips.recheck_concurrency">复检并发 (0=跟随)</label>
              <input v-model.number="config.analysis.recheck_concurrency" :class="errCls('recheck_concurrency')" type="number" min="0" max="20" class="glass-input w-full" />
            </div>

            <div class="mc-action-cell flex items-center gap-2 flex-wrap">
              <button @click="runRecheckProbe" :disabled="recheckProbeBusy" class="glass-button btn-sm shrink-0" title="对判定通道发几次微请求：验证模型/端点/Key 组合可用">
                {{ recheckProbeBusy ? '探测中...' : '探测判定通道' }}
              </button>
              <button type="button" @click="showRecheckEndpoint = !showRecheckEndpoint" class="glass-button btn-sm shrink-0">
                {{ showRecheckEndpoint ? '收起独立端点' : '独立端点 / Key' }}
              </button>
            </div>
          </div>

          <div v-if="recheckProbeError" class="text-xs glass-tinted-red px-2 py-1 rounded" style="color: var(--win-danger)">{{ recheckProbeError }}</div>
          <div v-if="recheckProbeResult" class="text-xs space-y-1">
            <p :class="recheckProbeResult.best ? 'glass-tinted-green px-2 py-1 rounded' : 'glass-tinted-orange px-2 py-1 rounded'">
              {{ recheckProbeResult.note || (recheckProbeResult.best ? '判定通道可用' : '通道可达，但未探测到可用的思考控制参数') }}
            </p>
            <p v-if="recheckProbeResult.best" style="color: var(--win-text-secondary)">
              命中参数：{{ recheckProbeResult.best.param }}
            </p>
          </div>
          <p v-if="recheckConflictHint" class="text-xs glass-tinted-red px-2 py-1 rounded" style="color: var(--win-danger)">{{ recheckConflictHint }}</p>

          <!-- 按需展开：复检独立端点与认证（严格 v-show 保活 422 高亮） -->
          <div v-show="showRecheckEndpoint" class="pt-2.5 mt-1 space-y-2.5" style="border-top: 1px dashed var(--win-stroke)">
            <div class="flex items-center justify-between gap-2 flex-wrap">
              <span class="text-xs font-medium" style="color: var(--win-text-secondary)">跨网关/换厂商时填下面字段（留空 = 跟随主连接）</span>
              <button @click="loadRecheckModels" :disabled="recheckModelsBusy" class="glass-button btn-sm shrink-0">
                {{ recheckModelsBusy ? '获取中...' : '从判定端点获取模型' }}
              </button>
            </div>
            <p v-if="recheckModelsError" class="text-xs glass-tinted-red px-2 py-1 rounded" style="color: var(--win-danger)">{{ recheckModelsError }}</p>
            <div class="sf-channel-grid">
              <div class="sf-row">
                <label class="sf-label" title="选一个厂商预设，自动填下面的端点/Key/模型">判定预设:</label>
                <div class="sf-control">
                  <select @change="onRecheckPreset(($event.target as HTMLSelectElement).value)" class="glass-select flex-1 min-w-0">
                    <option value="">选择预设...</option>
                    <option v-for="(_, name) in presets" :key="name" :value="name">{{ name }}</option>
                  </select>
                </div>
              </div>
              <div class="sf-row">
                <label class="sf-label" title="判定请求打到哪个端点；留空 = 跟随主通道">判定 Base URL:</label>
                <div class="sf-control">
                  <input v-model="config.analysis.recheck_base_url" :class="errCls('recheck_base_url')" type="text" class="glass-input flex-1 min-w-0" placeholder="留空 = 跟随主通道" />
                </div>
              </div>
              <div class="sf-row">
                <label class="sf-label" title="判定通道的 API Key；留空 = 跟随主 Key">判定 API Key:</label>
                <div class="sf-control">
                  <input v-model="config.analysis.recheck_api_key" :class="errCls('recheck_api_key')" type="password" class="glass-input flex-1 min-w-0" placeholder="留空 = 跟随主 Key" />
                </div>
              </div>
              <div class="sf-row">
                <label class="sf-label" title="判定请求的协议格式；跟随主配置 = 原样继承">协议格式:</label>
                <div class="sf-control">
                  <select v-model="config.analysis.recheck_provider" :class="errCls('recheck_provider')" class="glass-select flex-1 min-w-0">
                    <option value="">跟随主配置</option>
                    <option value="auto">auto（按判定端点检测）</option>
                    <option value="openai">openai（OpenAI 兼容）</option>
                    <option value="anthropic">anthropic（/v1/messages）</option>
                  </select>
                </div>
              </div>
              <div class="sf-row">
                <label class="sf-label" title="判定请求的 JSON 输出约束；复检默认要求 JSON 数组">JSON 模式:</label>
                <div class="sf-control">
                  <select v-model="config.analysis.recheck_json_mode" :class="errCls('recheck_json_mode')" class="glass-select flex-1 min-w-0">
                    <option value="">默认 (推荐·array)</option>
                    <option value="default">default - 通用</option>
                    <option value="qwen">qwen - Qwen系列</option>
                    <option value="deepseek">deepseek - DeepSeek V4</option>
                    <option value="glm47">glm47 - GLM-4.7</option>
                  </select>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>

    <!-- 高级区（严格使用 v-show 保持 DOM 常驻与 422 校验高亮可见，精简为 3 大纯配置分组） -->
    <div v-show="showAdvanced" class="settings-cards-grid">
      <!-- 高级分组 1：请求超时、JSON 模式与退火重试 -->
      <div class="glass-card settings-card p-4 space-y-3">
        <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">请求超时、JSON 模式与退火重试</h3>
        <div class="sf-num-grid">
          <div class="sf-num-row"><label v-tooltip="tooltips.max_tokens">Max Tokens:</label><input v-model.number="config.api.max_tokens" :class="errCls('max_tokens')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.timeout">分析 Timeout (s):</label><input v-model.number="config.api.timeout" :class="errCls('timeout')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.summary_timeout">总结 Timeout (s):</label><input v-model.number="config.api.summary_timeout" :class="errCls('summary_timeout')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.json_mode">JSON 模式:</label>
            <select v-model="config.api.json_mode" :class="errCls('json_mode')" class="glass-select sf-json-select">
              <option v-for="m in jsonModes" :key="m.value" :value="m.value">{{ m.label }}</option>
            </select>
          </div>
          <div class="sf-num-row"><label v-tooltip="tooltips.temperature">初始温度:</label><input v-model.number="config.api.temperature" :class="errCls('temperature')" type="number" step="0.01" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.temperature_step">温度步长:</label><input v-model.number="config.api.temperature_step" :class="errCls('temperature_step')" type="number" step="0.01" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.temperature_max_retries">退火重试:</label><input v-model.number="config.api.temperature_max_retries" :class="errCls('temperature_max_retries')" type="number" min="1" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.backoff_max_retries">退避重试:</label><input v-model.number="config.api.backoff_max_retries" :class="errCls('backoff_max_retries')" type="number" min="0" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label title="单次复检调用的输出上限（token）">复检 Max Tokens:</label><input v-model.number="config.analysis.recheck_max_tokens" :class="errCls('recheck_max_tokens')" type="number" min="1024" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label title="单次复检调用的超时（秒）；0 = 跟随分析 Timeout">复检 Timeout (s):</label><input v-model.number="config.analysis.recheck_timeout" :class="errCls('recheck_timeout')" type="number" min="0" class="glass-input sf-num-input" /></div>
          <div v-if="(config.analysis.recheck_version || 'v3') === 'v2'" class="sf-num-row">
            <label title="仅 V2 引擎生效：每次调用同时判定几条候选">V2 判定批大小:</label>
            <input v-model.number="config.analysis.recheck_v2_batch_size" :class="errCls('recheck_v2_batch_size')" type="number" min="1" max="20" class="glass-input sf-num-input" />
          </div>
        </div>
      </div>

      <!-- 高级分组 2：提示词上下文与滚动总结预算 -->
      <div class="glass-card settings-card p-4 space-y-3">
        <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">提示词上下文与滚动总结预算</h3>
        <div class="sf-num-grid">
          <div class="sf-num-row"><label v-tooltip="tooltips.rolling_early_chapters">触发章数:</label><input v-model.number="config.analysis.rolling_early_chapters" :class="errCls('rolling_early_chapters')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.rolling_max_milestones">里程碑上限:</label><input v-model.number="config.analysis.rolling_max_milestones" :class="errCls('rolling_max_milestones')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.rolling_max_momentum">势头上限:</label><input v-model.number="config.analysis.rolling_max_momentum" :class="errCls('rolling_max_momentum')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.checkpoint_interval">Checkpoint:</label><input v-model.number="config.analysis.checkpoint_interval" :class="errCls('checkpoint_interval')" type="number" min="0" max="100" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.max_arc_length">主线最大字数:</label><input v-model.number="config.analysis.max_arc_length" :class="errCls('max_arc_length')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.max_arcs_in_prompt">Prompt 主线条数:</label><input v-model.number="config.analysis.max_arcs_in_prompt" :class="errCls('max_arcs_in_prompt')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.max_summaries_in_prompt">Prompt 摘要数:</label><input v-model.number="config.analysis.max_summaries_in_prompt" :class="errCls('max_summaries_in_prompt')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.timeline_truncate">时间线截断:</label><input v-model.number="config.analysis.timeline_truncate" :class="errCls('timeline_truncate')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.max_character_states">角色状态数:</label><input v-model.number="config.analysis.max_character_states" :class="errCls('max_character_states')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.max_world_items">世界观条数:</label><input v-model.number="config.analysis.max_world_items" :class="errCls('max_world_items')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.max_foreshadow_entries">伏笔网络条数:</label><input v-model.number="config.analysis.max_foreshadow_entries" :class="errCls('max_foreshadow_entries')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.batch_summary_min_words">分卷摘要字数:</label><input v-model.number="config.analysis.batch_summary_min_words" :class="errCls('batch_summary_min_words')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label v-tooltip="tooltips.final_report_min_words">最终报告字数:</label><input v-model.number="config.analysis.final_report_min_words" :class="errCls('final_report_min_words')" type="number" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label title="卷摘要拼接总字符超过此阈值时触发分层压缩">压缩阈值 (字符):</label><input v-model.number="config.analysis.volume_compress_threshold" :class="errCls('volume_compress_threshold')" type="number" min="10000" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label title="每 N 卷为一组（首尾各 1 组保留全文，中间组截断到 1/2）">压缩组大小 (卷):</label><input v-model.number="config.analysis.volume_compress_group" :class="errCls('volume_compress_group')" type="number" min="1" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label title="伏笔最低保留重要度：低/中/高">伏笔最低重要度:</label>
            <select v-model="config.analysis.foreshadow_min_importance" class="glass-select sf-num-input"><option>低</option><option>中</option><option>高</option></select>
          </div>
          <div class="sf-num-row"><label title="伏笔最低保留置信度：低/中/高">伏笔最低置信度:</label>
            <select v-model="config.analysis.foreshadow_min_confidence" class="glass-select sf-num-input"><option>低</option><option>中</option><option>高</option></select>
          </div>
          <div class="sf-num-row"><label title="importance=高 的伏笔最多保留多少（-1=无上限）">高 importance 上限:</label><input v-model.number="config.analysis.max_foreshadow_catalog_high" type="number" min="-1" class="glass-input sf-num-input" /></div>
          <div class="sf-num-row"><label title="importance=中 的伏笔最多保留多少">中 importance 上限:</label><input v-model.number="config.analysis.max_foreshadow_catalog_mid" type="number" min="0" class="glass-input sf-num-input" /></div>
        </div>
      </div>

      <!-- 高级分组 3：伏笔分类（功能类别） -->
      <div v-if="categoryDefs.length" class="glass-card settings-card settings-span-2 p-4 space-y-3">
        <div class="flex items-center justify-between pb-2" style="border-bottom: 1px solid var(--win-stroke)">
          <h3 class="font-semibold" style="letter-spacing: -0.01em">伏笔分类（功能类别）</h3>
          <div class="flex gap-2">
            <button @click="selectAllCategories" class="glass-pill">全选</button>
            <button @click="clearAllCategories" class="glass-pill" title="清空 = 只保留「其他」兜底">清空</button>
          </div>
        </div>
        <p class="text-xs" style="color: var(--win-text-secondary)">
          已选 {{ keptSet.size }} / {{ categoryDefs.length }}
        </p>
        <div class="sf-cat-grid">
          <label
            v-for="cat in categoryDefs"
            :key="cat.name"
            class="category-chip flex items-start gap-2 text-sm cursor-pointer p-1.5"
            :class="{ 'is-checked': keptSet.has(cat.name) }"
            v-tooltip="cat.description + (cat.examples.length ? '\n\n典型示例: ' + cat.examples.join(', ') : '')"
          >
            <input
              type="checkbox"
              :checked="keptSet.has(cat.name)"
              @change="toggleCategory(cat.name)"
              style="margin-top: 3px"
            />
            <span style="color: var(--win-text-primary)">{{ cat.name }}</span>
          </label>
        </div>
      </div>
    </div>

    <!-- 粘性保存操作栏 -->
    <div class="settings-sticky-bar space-y-2.5">
      <div v-if="saveError" class="glass-tinted-red px-3 py-2 rounded space-y-1 max-h-32 overflow-y-auto">
        <p class="text-sm font-medium" style="color: var(--win-danger)">保存失败</p>
        <pre class="text-xs whitespace-pre-wrap" style="color: var(--win-danger)">{{ saveError }}</pre>
      </div>
      <div class="flex gap-2.5 items-center flex-wrap">
        <button @click="save" :disabled="saving" class="glass-button glass-button-primary btn-lg">
          {{ saving ? '保存中...' : '保存设置' }}
        </button>
        <button v-if="hasUnsavedChanges && !saving" type="button" @click="discardChanges" class="glass-button">
          放弃更改
        </button>
        <Transition name="modal">
          <span v-if="saved && !hasUnsavedChanges" class="glass-badge badge-green" style="font-size: 12px" role="status">已保存</span>
        </Transition>
        <span v-if="!saving && hasUnsavedChanges" class="glass-badge badge-orange" style="font-size: 12px" role="status">有未保存的修改</span>
        <button type="button" @click="showAdvanced = !showAdvanced" class="glass-button ml-auto">
          {{ showAdvanced ? '收起高级参数（3 大分组）' : '展开高级参数（3 大分组）' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-cards-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 20px;
}
.settings-card {
  container-type: inline-size;
  min-width: 0;
}
@container (min-width: 960px) {
  .settings-cards-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .settings-span-2 {
    grid-column: span 2;
  }
}

/* 模型调用与并发矩阵行：宽屏多列横排，窄屏自动折行 */
.mc-row-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px 12px;
  align-items: end;
}
.mc-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.mc-label {
  font-size: 12px;
  color: var(--win-text-secondary);
}
.mc-action-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
@container (min-width: 640px) {
  .mc-row-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .mc-field-wide {
    grid-column: span 2;
  }
}
@container (min-width: 960px) {
  .mc-row-grid {
    grid-template-columns: minmax(220px, 2fr) minmax(150px, 1.2fr) minmax(150px, 1.2fr) 115px auto;
  }
  .mc-field-wide {
    grid-column: span 1;
  }
}

.card-allow-dropdown {
  overflow: visible !important;
  position: relative;
  z-index: 2;
}
.card-allow-dropdown.is-dropdown-open {
  z-index: 35;
}

.sf-row {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
  min-width: 0;
}
.sf-label {
  font-size: 14px;
  color: var(--win-text-secondary);
  min-width: 0;
}
.sf-control {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
  width: 100%;
}
.sf-control > input,
.sf-control > select,
.sf-control > .relative {
  flex: 1 1 160px;
  min-width: 0;
}
@container (min-width: 500px) {
  .sf-row {
    display: grid;
    grid-template-columns: 112px minmax(0, 1fr);
    align-items: center;
    gap: 8px 12px;
  }
}

.sf-num-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px 16px;
}
.sf-num-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}
.sf-num-row label {
  font-size: 14px;
  color: var(--win-text-secondary);
  min-width: 0;
  flex: 1;
}
.sf-num-input {
  width: 110px;
  flex-shrink: 0;
}
.sf-json-select {
  width: 140px;
  max-width: 55%;
  flex-shrink: 0;
}
@container (min-width: 460px) {
  .sf-num-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.sf-channel-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 12px;
}
@container (min-width: 820px) {
  .sf-channel-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.sf-cat-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
@container (min-width: 640px) {
  .sf-cat-grid {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
}

.settings-sticky-bar {
  position: sticky;
  bottom: 0;
  z-index: 25;
  background: var(--win-layer);
  border: 1px solid var(--win-stroke-strong);
  border-radius: var(--win-radius-container);
  box-shadow: var(--win-shadow-tooltip);
  padding: 12px 16px;
}

.input-invalid {
  border-color: var(--win-danger) !important;
  box-shadow: 0 0 0 1px var(--win-danger);
}

.model-dropdown {
  max-height: 240px;
  overflow-x: hidden;
  overflow-y: auto !important;
  overscroll-behavior: contain;
  z-index: 30;
}
.model-dropdown::-webkit-scrollbar-thumb {
  background: var(--win-text-muted);
  border-radius: 5px;
  border: 3px solid transparent;
  background-clip: content-box;
}
.model-dropdown::-webkit-scrollbar-thumb:hover {
  background: var(--win-text-secondary);
}

.model-option {
  border-radius: 4px;
  transition: background var(--win-duration-fast) var(--win-ease);
}
.model-option:hover {
  background: var(--win-control-hover);
}

.category-chip {
  border: 1px solid var(--win-stroke);
  border-radius: var(--win-radius-control);
  transition: background var(--win-duration-fast) var(--win-ease),
              border-color var(--win-duration-fast) var(--win-ease);
}
.category-chip.is-checked {
  background: var(--win-accent-soft);
  border-color: var(--win-accent);
}

.btn-sm {
  padding: 0 12px;
  white-space: nowrap;
}
.btn-lg {
  padding: 0 20px;
}
</style>
