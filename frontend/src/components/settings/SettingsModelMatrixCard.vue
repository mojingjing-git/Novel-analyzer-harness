<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api, type AppConfigDto, type ModelSpecDto } from '../../api/client'
import {
  THINKING_DEPTH_PRESETS,
  THINKING_ANTHROPIC_CAVEAT,
  type ThinkingState,
} from '../../utils/settingsForm'
import { tooltips, PROBE_DETECTOR_LABELS } from '../../utils/settingsTooltips'

const props = defineProps<{
  config: AppConfigDto
  presets: Record<string, { base_url: string; model: string; api_key?: string }>
  models: string[]
  modelError: string
  loadModels: () => Promise<void>
  modelSpec: ModelSpecDto | null
  summaryModelSpec: ModelSpecDto | null
  effectiveSummaryConc: number
  effectiveRecheckConc: number
  recheckConflictHint: string
  showRecheckEndpoint: boolean
  thinkingModes: { value: string; label: string }[]
  dynamicThinkingOptions: { value: string; label: string }[]
  thinkingState: ThinkingState
  thinkingDepth: string
  thinkingDisableStr: string
  probeBusy: boolean
  probeResult: Awaited<ReturnType<typeof api.probeThinking>> | null
  probeError: string
  summaryThinkingModes: { value: string; label: string }[]
  dynamicSummaryThinkingOptions: { value: string; label: string }[]
  summaryThinkingState: ThinkingState
  summaryThinkingDepth: string
  summaryThinkingDisableStr: string
  summaryProbeBusy: boolean
  summaryProbeResult: Awaited<ReturnType<typeof api.probeThinking>> | null
  summaryProbeError: string
  recheckProbeBusy: boolean
  recheckProbeResult: Awaited<ReturnType<typeof api.probeRecheck>> | null
  recheckProbeError: string
  errCls: (field: string) => string
}>()

const emit = defineEmits<{
  'update:showRecheckEndpoint': [value: boolean]
  'fetch-model-spec': [modelName?: string]
  'fetch-summary-model-spec': [modelName?: string]
  'thinking-depth-change': [val: string]
  'thinking-disable-change': [val: string]
  'summary-thinking-depth-change': [val: string]
  'summary-thinking-disable-change': [val: string]
  'probe-thinking': []
  'probe-summary-thinking': []
  'recheck-preset': [name: string]
  'probe-recheck': []
}>()

// ===== 主模型下拉 =====
const showModelList = ref(false)
const modelFilter = ref('')
const modelBoxRef = ref<HTMLElement | null>(null)

const filteredModels = computed(() => {
  const f = modelFilter.value.trim().toLowerCase()
  if (!f) return props.models
  return props.models.filter((m: string) => m.toLowerCase().includes(f))
})

async function openModelList() {
  showSummaryModelList.value = false
  showRecheckModelList.value = false
  showModelList.value = true
  if (props.models.length === 0) {
    await props.loadModels()
  }
}

function pickModel(m: string) {
  props.config.api.model = m
  modelFilter.value = ''
  modelBoxRef.value?.querySelector('input')?.focus()
  showModelList.value = false
  emit('fetch-model-spec', m)
}

// ===== 总结模型下拉 =====
const showSummaryModelList = ref(false)
const summaryModelFilter = ref('')
const summaryModelBoxRef = ref<HTMLElement | null>(null)

const filteredSummaryModels = computed(() => {
  const f = summaryModelFilter.value.trim().toLowerCase()
  if (!f) return props.models
  return props.models.filter((m: string) => m.toLowerCase().includes(f))
})

async function openSummaryModelList() {
  showModelList.value = false
  showRecheckModelList.value = false
  showSummaryModelList.value = true
  if (props.models.length === 0) {
    await props.loadModels()
  }
}

function pickSummaryModel(m: string) {
  props.config.api.summary_model = m
  summaryModelFilter.value = ''
  summaryModelBoxRef.value?.querySelector('input')?.focus()
  showSummaryModelList.value = false
  emit('fetch-summary-model-spec', m)
}

// ===== 复检模型下拉 =====
const recheckModels = ref<string[]>([])
const showRecheckModelList = ref(false)
const recheckModelFilter = ref('')
const recheckModelBoxRef = ref<HTMLElement | null>(null)
const recheckModelsBusy = ref(false)
const recheckModelsError = ref('')

const recheckModelSourceList = computed(() => {
  if (recheckModels.value.length > 0) return recheckModels.value
  if (!(props.config.analysis.recheck_base_url || '').trim()) return props.models
  return []
})

const filteredRecheckModels = computed(() => {
  const f = recheckModelFilter.value.trim().toLowerCase()
  const src = recheckModelSourceList.value
  if (!f) return src
  return src.filter((m: string) => m.toLowerCase().includes(f))
})

async function loadRecheckModels() {
  const a = props.config.analysis
  const mainApi = props.config.api
  if (recheckModelsBusy.value) return
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

function openRecheckModelList() {
  showModelList.value = false
  showSummaryModelList.value = false
  showRecheckModelList.value = true
}

function pickRecheckModel(m: string) {
  props.config.analysis.recheck_model = m
  recheckModelFilter.value = ''
  recheckModelBoxRef.value?.querySelector('input')?.focus()
  showRecheckModelList.value = false
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
})
onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
})
</script>

<template>
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
              @input="showModelList = true; modelFilter = config.api.model || ''"
              @blur="emit('fetch-model-spec')"
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
          <select :value="thinkingDepth" @change="emit('thinking-depth-change', ($event.target as HTMLSelectElement).value)" class="glass-select w-full min-w-0">
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
            @change="emit('thinking-disable-change', ($event.target as HTMLSelectElement).value)"
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
          <button
            @click="emit('probe-thinking')"
            :disabled="probeBusy || !config.api.model"
            class="glass-button btn-sm shrink-0"
            title="对当前主模型发几次微请求，实测有效思考控制/深度参数，命中后自动应用"
          >
            {{ probeBusy ? '探测中...' : '自动探测' }}
          </button>
        </div>
      </div>

      <div v-if="modelSpec" class="text-xs px-2.5 py-1 rounded glass-tinted-blue flex items-center gap-2 flex-wrap" style="color: var(--win-accent)">
        <span>💡 models.dev 规范：{{ modelSpec.name }}</span>
        <span v-if="modelSpec.reasoning">
          · 支持思考控制（{{ modelSpec.reasoning_type === 'effort' ? '档位: ' + (modelSpec.effort_values?.join('/') || 'low/medium/high') : (modelSpec.reasoning_type === 'budget_tokens' ? 'Token 预算' : '开/关') }}）
        </span>
        <span v-else style="color: var(--win-text-secondary)">· 原生无思考链</span>
        <span v-if="modelSpec.limit?.context">· 上下文 {{ (modelSpec.limit.context / 1000).toFixed(0) }}k</span>
        <span v-if="modelSpec.cost?.input">· 费率 ${{ modelSpec.cost.input }}/MIn</span>
      </div>

      <div v-if="thinkingState === 'depth_low'" class="text-xs glass-tinted-blue px-2.5 py-1 rounded" style="color: var(--win-accent)">
        ⚡ 浅度思考模式 (low)：有效抑制思考膨胀与耗时，保留必要推理，推荐小说分析使用
      </div>
      <div v-else-if="thinkingState === 'depth_medium'" class="text-xs glass-tinted-blue px-2.5 py-1 rounded" style="color: var(--win-accent)">
        ⚡ 中度思考模式 (medium)：平衡思考耗时与长篇逻辑推理深度
      </div>
      <div v-else-if="thinkingState === 'depth_high'" class="text-xs glass-tinted-blue px-2.5 py-1 rounded" style="color: var(--win-accent)">
        ⚡ 深度思考模式 (high)：完整展开长程链式思考，耗时与 Token 开销较大
      </div>
      <div v-else-if="thinkingState === 'budget'" class="text-xs glass-tinted-blue px-2.5 py-1 rounded" style="color: var(--win-accent)">
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
                {{ PROBE_DETECTOR_LABELS[String(key)] || key }}
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
              @input="showSummaryModelList = true; summaryModelFilter = config.api.summary_model || ''"
              @blur="emit('fetch-summary-model-spec')"
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
          <select :value="summaryThinkingDepth" @change="emit('summary-thinking-depth-change', ($event.target as HTMLSelectElement).value)" class="glass-select w-full min-w-0">
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
            @change="emit('summary-thinking-disable-change', ($event.target as HTMLSelectElement).value)"
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
            @click="emit('probe-summary-thinking')"
            :disabled="summaryProbeBusy || !(config.api.summary_model || '').trim()"
            class="glass-button btn-sm shrink-0"
            :title="(config.api.summary_model || '').trim() ? '对总结专用模型发微请求实测思考参数' : '请先指定独立的总结模型（留空时沿用主模型探测结果）'"
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
              @input="showRecheckModelList = true; recheckModelFilter = config.analysis.recheck_model || ''"
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
          <button @click="emit('probe-recheck')" :disabled="recheckProbeBusy" class="glass-button btn-sm shrink-0" title="对判定通道发几次微请求：验证模型/端点/Key 组合可用">
            {{ recheckProbeBusy ? '探测中...' : '探测判定通道' }}
          </button>
          <button type="button" @click="emit('update:showRecheckEndpoint', !showRecheckEndpoint)" class="glass-button btn-sm shrink-0">
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
              <select @change="emit('recheck-preset', ($event.target as HTMLSelectElement).value)" class="glass-select flex-1 min-w-0">
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
</template>

<style scoped>
.settings-card {
  container-type: inline-size;
  min-width: 0;
}
.settings-span-2 {
  grid-column: span 1;
}
@container (min-width: 960px) {
  .settings-span-2 {
    grid-column: span 2;
  }
}
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
.btn-sm {
  padding: 0 12px;
  white-space: nowrap;
}
</style>
