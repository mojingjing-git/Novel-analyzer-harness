<script setup lang="ts">
import { ref, computed, watch, inject, onMounted, onUnmounted } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import { api, type AppConfigDto, type ModelSpecDto } from '../api/client'
import SettingsApiConnectionCard from '../components/settings/SettingsApiConnectionCard.vue'
import SettingsStrategyCard from '../components/settings/SettingsStrategyCard.vue'
import SettingsModelMatrixCard from '../components/settings/SettingsModelMatrixCard.vue'
import SettingsAdvancedSection from '../components/settings/SettingsAdvancedSection.vue'
import {
  pruneEmptyStrings, mapFieldErrors, toggleCategoryIn, CATEGORY_CLEAR_FALLBACK,
  inferThinkingState, makeThinkingPresets,
  composeThinkingMode, decomposeThinkingMode,
  type ThinkingState,
} from '../utils/settingsForm'
import {
  ADVANCED_FIELD_KEYS,
  RECHECK_ENDPOINT_FIELD_KEYS,
  type CategoryDef,
} from '../utils/settingsTooltips'

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

// ===== 复检判定通道：预设填充 + 通道探测 =====
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

onMounted(() => {
  loadCategoryDefs()
  load()
})

onUnmounted(() => {
  disposed = true
  clearTimeout(savedTimer)
})
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

    <div class="settings-cards-grid">
      <SettingsApiConnectionCard
        :config="config"
        :presets="presets"
        :catalogVersion="catalogVersion"
        :catalogSyncing="catalogSyncing"
        :catalogSyncMsg="catalogSyncMsg"
        :errCls="errCls"
        @sync-catalog="runSyncCatalog"
        @preset="onPreset"
      />

      <SettingsStrategyCard
        :config="config"
        :errCls="errCls"
      />

      <SettingsModelMatrixCard
        :config="config"
        :presets="presets"
        :models="models"
        :modelError="modelError"
        :loadModels="loadModels"
        :modelSpec="modelSpec"
        :summaryModelSpec="summaryModelSpec"
        :effectiveSummaryConc="effectiveSummaryConc"
        :effectiveRecheckConc="effectiveRecheckConc"
        :recheckConflictHint="recheckConflictHint"
        v-model:showRecheckEndpoint="showRecheckEndpoint"
        :thinkingModes="thinkingModes"
        :dynamicThinkingOptions="dynamicThinkingOptions"
        :thinkingState="thinkingState"
        :thinkingDepth="thinkingDepth"
        :thinkingDisableStr="thinkingDisableStr"
        :probeBusy="probeBusy"
        :probeResult="probeResult"
        :probeError="probeError"
        :summaryThinkingModes="summaryThinkingModes"
        :dynamicSummaryThinkingOptions="dynamicSummaryThinkingOptions"
        :summaryThinkingState="summaryThinkingState"
        :summaryThinkingDepth="summaryThinkingDepth"
        :summaryThinkingDisableStr="summaryThinkingDisableStr"
        :summaryProbeBusy="summaryProbeBusy"
        :summaryProbeResult="summaryProbeResult"
        :summaryProbeError="summaryProbeError"
        :recheckProbeBusy="recheckProbeBusy"
        :recheckProbeResult="recheckProbeResult"
        :recheckProbeError="recheckProbeError"
        :errCls="errCls"
        @fetch-model-spec="fetchModelSpec"
        @fetch-summary-model-spec="fetchSummaryModelSpec"
        @thinking-depth-change="onThinkingDepthChange"
        @thinking-disable-change="onThinkingDisableChange"
        @summary-thinking-depth-change="onSummaryThinkingDepthChange"
        @summary-thinking-disable-change="onSummaryThinkingDisableChange"
        @probe-thinking="runProbeThinking"
        @probe-summary-thinking="runSummaryProbeThinking"
        @recheck-preset="onRecheckPreset"
        @probe-recheck="runRecheckProbe"
      />
    </div>

    <SettingsAdvancedSection
      :show="showAdvanced"
      :config="config"
      :categoryDefs="categoryDefs"
      :keptSet="keptSet"
      :errCls="errCls"
      @toggle-category="toggleCategory"
      @select-all-categories="selectAllCategories"
      @clear-all-categories="clearAllCategories"
    />

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
@container (min-width: 960px) {
  .settings-cards-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .settings-span-2 {
    grid-column: span 2;
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
.btn-lg {
  padding: 0 20px;
}
</style>
