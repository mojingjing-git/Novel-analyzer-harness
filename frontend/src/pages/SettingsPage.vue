<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api, type AppConfigDto } from '../api/client'
import LogConsole from '../components/LogConsole.vue'
import { useLogStore } from '../composables/useLogStore'
import {
  pruneEmptyStrings, mapFieldErrors, toggleCategoryIn, CATEGORY_CLEAR_FALLBACK,
  stringifyThinkingMode, parseThinkingMode,
  inferThinkingState, makeThinkingPresets, THINKING_ANTHROPIC_CAVEAT,
  type ThinkingState,
} from '../utils/settingsForm'

// 设置项 tooltip 字典：key 与模板中 :title 表达式一一对应
// 字面太直白的字段（如"并发数""块大小""Checkpoint"）不列，避免噪音
const tooltips: Record<string, string> = {
  // API 配置
  api_preset: '从已保存的厂商预设中快速填入 base_url / model / api_key；切到预设会自动覆盖当前未保存的输入',
  base_url: 'LLM 服务的入口地址，例如 https://api.openai.com/v1 或厂商网关地址',
  api_key: '厂商颁发的访问密钥；本地工具明文存储，仅写入本机 config.json',
  model: '模型名。点输入框展开列表（按当前 base_url/api_key 探测），也可手填自定义模型',
  provider: '请求协议格式：\nauto = 按 base_url/API Key/模型名特征自动检测\nopenai = OpenAI 兼容 /chat/completions\nanthropic = Anthropic /v1/messages',
  max_tokens: '单次 LLM 请求的最大输出 token 上限；超出将被截断',
  timeout: '单次分析请求的 API 超时（秒）；超时后会进入重试链',
  summary_timeout: '最终总结（卷摘要+最终报告+伏笔 reconciliation/复检+风格提取）使用的 API 超时，建议比分析 Timeout 长（默认 600s）',
  json_mode: 'JSON 输出约束模式：\ndefault = 通用\nqwen = Qwen 系列需关 thinking\ndeepseek = DeepSeek V4\nglm47 = GLM-4.7',
  thinking_mode: '禁用/启用模型的思考链。\n点「自动探测」会让系统实测哪个参数有效',
  // 温度退火 & 重试
  temperature: '单次请求的初始温度；后续重试会逐步降温',
  temperature_step: '每轮退火重试时降低的温度（例：起始 0.7、步长 0.15 → 0.7 → 0.55 → 0.4 → ...）',
  temperature_max_retries: '温度退火最大重试轮数；用尽后再走退避',
  backoff_max_retries: '指数退避最大轮数；等待时长 min(2^N, 60s)；429 会额外 3 轮并尊重 Retry-After',
  // 分析配置
  concurrency: '并行分析的章节数。\n越大越快，但 token 峰值越高（受 API TPM/RPM 限制）',
  block_size: '每"块"包含的章节数。\n块越大单次请求 context 越长、token 消耗越大，但 KB 增量更稳定',
  max_arc_length: '主线（角色弧光）最大字数；超过会在合并阶段裁剪',
  max_arcs_in_prompt: '每章 Prompt 中携带的最近弧光条数；过多会稀释注意力',
  max_summaries_in_prompt: '每章 Prompt 中携带的近期章节摘要数；影响上下文长度与连续性',
  timeline_truncate: 'Prompt 中携带的时间线条数上限；超出按章节倒序截断',
  max_character_states: 'Prompt 中携带的最近角色状态条数',
  max_world_items: 'Prompt 中携带的世界观条目上限',
  max_foreshadow_entries: 'Prompt 中携带的伏笔网络条目上限；超过按综合权重截断',
  batch_summary_min_words: '每卷摘要的最低字数要求；不足会被 LLM 补写',
  final_report_min_words: '最终报告的最低字数要求',
  // 滚动总结参数
  rolling_early_chapters: '开篇多少章内不做滚动总结（让 KB 自然增长）',
  rolling_max_milestones: '滚动总结中里程碑条数上限；超出 FIFO 淘汰',
  rolling_max_momentum: '势头条目上限；超出触发归档压缩为里程碑',
  checkpoint_interval: '每处理多少块做一次 checkpoint 落盘；0=关闭',
  auto_archive: '分析完成后自动把工作区搬到「分析结果/」归档目录',
  auto_summary: '队列全部完成后自动启动最终总结',
  skip_moderation_blocked: '识别为内容审核拦截的章节：重试 1 次后跳过并标记，不再反复重试白烧成本',
  summary_concurrency: '总结阶段的并发数（卷摘要/伏笔复检/风格提取等共用）',
  summary_batch_size: '总结阶段的批次大小；每批含 N 卷',
}


const config = ref<AppConfigDto | null>(null)
const loadError = ref('')
const presets = ref<Record<string, { base_url: string; model: string; api_key?: string }>>({})
const models = ref<string[]>([])
const saving = ref(false)
const saved = ref(false)

interface CategoryDef { name: string; description: string; examples: string[] }
const categoryDefs = ref<CategoryDef[]>([])

const keptSet = computed(() => new Set(config.value?.analysis.foreshadow_kept_categories ?? []))

function toggleCategory(name: string) {
  if (!config.value) return
  // 增删逻辑在 utils/settingsForm.toggleCategoryIn（可独立 vitest）
  config.value.analysis.foreshadow_kept_categories = toggleCategoryIn(
    config.value.analysis.foreshadow_kept_categories, name)
}

function selectAllCategories() {
  if (!config.value) return
  config.value.analysis.foreshadow_kept_categories = categoryDefs.value.map(c => c.name)
}

function clearAllCategories() {
  if (!config.value) return
  // 保留"其他"作为兜底
  config.value.analysis.foreshadow_kept_categories = [...CATEGORY_CLEAR_FALLBACK]
}

async function loadCategoryDefs() {
  try {
    const res = await api.getForeshadowCategories()
    categoryDefs.value = res.defs
  } catch (e) { console.error('加载伏笔分类失败', e) }
}

async function load() {
  loadError.value = ''
  try {
    config.value = await api.getSettings()
    presets.value = (await api.getPresets()).presets
  } catch (e) {
    // P3：此前静默失败导致 v-if="config" 整页空白且无重试入口
    loadError.value = (e as Error).message || '无法连接后端服务'
  }
}

const modelError = ref('')
async function loadModels() {
  // 用表单当前（未保存）的 base_url/api_key 查模型：填完即可获取，无需先保存；
  // 空字段不传，由后端回退到已保存配置
  const a = config.value?.api
  const body: { base_url?: string; api_key?: string; provider?: string } = {}
  if (a?.base_url?.trim()) body.base_url = a.base_url.trim()
  if (a?.api_key?.trim()) body.api_key = a.api_key.trim()
  if (a?.provider) body.provider = a.provider
  modelError.value = ''
  try { const res = await api.previewModels(body); models.value = res.models }
  catch (e) { modelError.value = '获取模型失败: ' + (e as Error).message }
}

// 自定义模型下拉：点击即展开全部，可输入筛选，也允许手填自定义模型
const showModelList = ref(false)
const modelFilter = ref('')
const modelBoxRef = ref<HTMLElement | null>(null)

const filteredModels = computed(() => {
  const f = modelFilter.value.trim().toLowerCase()
  if (!f) return models.value
  return models.value.filter((m: string) => m.toLowerCase().includes(f))
})

async function openModelList() {
  showModelList.value = true
  if (models.value.length === 0) {
    await loadModels()
  }
}

function pickModel(m: string) {
  if (config.value) config.value.api.model = m
  modelFilter.value = ''
  showModelList.value = false
}

function onModelBoxBlur() {
  // 延迟收起，避免点击选项时先触发 blur 把列表收掉
  setTimeout(() => { showModelList.value = false }, 120)
}

function onDocClick(e: MouseEvent) {
  if (modelBoxRef.value && !modelBoxRef.value.contains(e.target as Node)) {
    showModelList.value = false
  }
}
onMounted(() => {
  document.addEventListener('click', onDocClick)
  loadCategoryDefs()
})
onUnmounted(() => {
  // AppLayout 按 route.path 强制重挂载，必须移除监听，否则每次访问设置页都泄漏一个全局监听器
  document.removeEventListener('click', onDocClick)
})

function onPreset(name: string) {
  const p = presets.value[name]
  if (p && config.value) {
    config.value.api.base_url = p.base_url
    config.value.api.model = p.model
    if (p.api_key) config.value.api.api_key = p.api_key
  }
}

const fieldErrors = ref<Record<string, string>>({})
const saveError = ref('')

async function save() {
  if (!config.value) return
  saving.value = true
  fieldErrors.value = {}
  saveError.value = ''
  try {
    // P2：数值输入清空后 v-model.number 保留 ''；后端虽能兜底回落默认，
    // 但静默改值不可预期——提交前剪除空字符串字段，语义即“恢复该项默认”
    // （剪除逻辑在 utils/settingsForm.pruneEmptyStrings，可独立 vitest）
    pruneEmptyStrings(config.value as unknown as Record<string, unknown>)
    await api.putSettings(config.value)
    saved.value = true
    setTimeout(() => saved.value = false, 2000)
  } catch (e) {
    const err = e as Error & { status?: number; detail?: unknown }
    if (err.status === 422 && Array.isArray(err.detail)) {
      // 422 映射逻辑在 utils/settingsForm.mapFieldErrors（loc 末元素 → 红框字段）
      const { fieldErrors: fe, msgs } = mapFieldErrors(err.detail)
      fieldErrors.value = fe
      saveError.value = '保存失败（参数校验未通过）：\n' + msgs.join('\n')
    } else {
      saveError.value = '保存失败: ' + err.message
    }
  }
  finally { saving.value = false }
}

// 根据字段名返回校验出错的 class（用于输入框红框高亮）
function errCls(field: string): string {
  return fieldErrors.value[field] ? 'input-invalid' : ''
}

const jsonModes = [
  { value: 'default', label: 'default - 通用' },
  { value: 'qwen', label: 'qwen - Qwen系列' },
  { value: 'deepseek', label: 'deepseek - DeepSeek V4' },
  { value: 'glm47', label: 'glm47 - GLM-4.7' },
]

// 运行日志（完整，含调试信息）—— 与全局日志存储共享同一单例
const { logs: storeLogs, clear: clearLogs } = useLogStore()

// 预设 value/技术标签来自 settingsForm 单一来源；首项 label 按页传入——
// 本页的 `{}` = 直接跟随端点默认（SummaryPage 的 `{}` 是回退全局，语义不同）
const thinkingModes = makeThinkingPresets('自动（跟随端点默认）')

// 状态提示（不点探测也可见）：`{}` = 可能开启思考链 ⚠，显式禁用 = ✓（M3 事故教训 10.18）
const thinkingState = computed<ThinkingState>(() => inferThinkingState(config.value?.api.thinking_mode))

function getThinkingMode(): string {
  if (!config.value) return '{}'
  return stringifyThinkingMode(config.value.api.thinking_mode)
}

function setThinkingMode(val: string) {
  if (!config.value) return
  const parsed = parseThinkingMode(val)
  if (parsed) config.value.api.thinking_mode = parsed  // 坏 JSON 保持原值（parseThinkingMode 返回 null）
}

// 自动探测禁用思考参数（发微请求实测当前端点认哪个参数）
const probeBusy = ref(false)
const probeResult = ref<{
  results: {
    param: string
    thinking_mode: Record<string, unknown> | null
    reasoning_chars: number
    content_chars: number
    think_tag_chars: number
    reasoning_token_count: number
    worked: boolean | null
    error: string
    detection_breakdown: Record<string, boolean>
  }[]
  best: { thinking_mode: Record<string, unknown>; param: string } | null
  default_thinks: boolean
  default_detection_breakdown: Record<string, boolean>
  note: string
} | null>(null)
const probeError = ref('')
const dynamicThinkingOptions = ref<{ value: string; label: string }[]>([])

// 检测维度中文名（与后端 _THINKING_DETECTORS 对应）
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
  const body: { base_url?: string; api_key?: string; provider?: string } = {}
  if (a.base_url?.trim()) body.base_url = a.base_url.trim()
  if (a.api_key?.trim()) body.api_key = a.api_key.trim()
  if (a.provider) body.provider = a.provider
  try {
    const res = await api.probeThinking(body)
    probeResult.value = res
    dynamicThinkingOptions.value = []
    if (res.best?.thinking_mode) {
      const s = JSON.stringify(res.best.thinking_mode)
      if (!thinkingModes.some((o) => o.value === s)) {
        dynamicThinkingOptions.value = [{ value: s, label: `探测命中: ${res.best.param || s}` }]
      }
      setThinkingMode(s)
    }
  } catch (e) {
    probeError.value = '探测失败: ' + (e as Error).message
  } finally {
    probeBusy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-if="loadError" class="glass-card p-6 text-center space-y-3">
    <p class="text-sm" style="color: var(--win-danger)">设置加载失败：{{ loadError }}</p>
    <button class="glass-button" @click="load">重试</button>
  </div>
  <div class="space-y-6 max-w-3xl" v-else-if="config">
    <div>
      <h2 class="section-title">设置</h2>
      <p class="section-subtitle">API、分析、知识库限制等参数</p>
    </div>

    <div class="glass-card p-4 space-y-3">
      <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">API 配置</h3>
      <div class="flex items-center gap-2">
        <label class="w-32 text-sm" style="color: var(--win-text-secondary)" v-tooltip="tooltips.api_preset">API 预设:</label>
        <select @change="onPreset(($event.target as HTMLSelectElement).value)" class="glass-input flex-1">
          <option value="">选择预设...</option>
          <option v-for="(_, name) in presets" :key="name" :value="name">{{ name }}</option>
        </select>
      </div>
      <div class="flex items-center gap-2">
        <label class="w-32 text-sm" style="color: var(--win-text-secondary)" v-tooltip="tooltips.base_url">Base URL:</label>
        <input v-model="config.api.base_url" :class="errCls('base_url')" class="glass-input flex-1" />
      </div>
      <div class="flex items-center gap-2">
        <label class="w-32 text-sm" style="color: var(--win-text-secondary)" v-tooltip="tooltips.api_key">API Key:</label>
        <input v-model="config.api.api_key" :class="errCls('api_key')" type="password" class="glass-input flex-1" />
      </div>
      <div class="flex items-center gap-2">
        <label class="w-32 text-sm shrink-0" style="color: var(--win-text-secondary)" v-tooltip="tooltips.model">模型:</label>
        <div class="relative flex-1" ref="modelBoxRef">
          <input
            v-model="config.api.model"
            :class="errCls('model')"
            @focus="openModelList"
            @click="openModelList"
            @input="showModelList = true; modelFilter = config?.api.model || ''"
            @blur="onModelBoxBlur"
            class="glass-input w-full"
            placeholder="点击选择或输入自定义模型名"
          />
          <div
            v-if="showModelList"
            class="absolute z-20 mt-1 w-full max-h-60 overflow-auto rounded-lg border p-1 space-y-0.5"
            style="background: var(--win-layer); border: 1px solid var(--win-stroke); box-shadow: var(--win-shadow-flyout); border-radius: 8px"
          >
            <div v-if="filteredModels.length === 0" class="px-2 py-1.5 text-xs" style="color: var(--win-text-disabled)">
              无匹配模型，以上方输入值为准
            </div>
            <button
              v-for="m in filteredModels"
              :key="m"
              type="button"
              @mousedown.prevent="pickModel(m)"
              class="model-option block w-full text-left px-2 py-1.5 rounded-md text-sm"
              style="color: var(--win-text-primary)"
            >{{ m }}</button>
          </div>
        </div>
        <button @click="loadModels" class="glass-button btn-sm">获取模型</button>
      </div>
      <div class="flex items-center gap-2">
        <label class="w-32 text-sm shrink-0" style="color: var(--win-text-secondary)" v-tooltip="tooltips.provider">协议格式:</label>
        <select v-model="config.api.provider" class="glass-input flex-1">
          <option value="auto">auto（自动检测）</option>
          <option value="openai">openai（OpenAI 兼容）</option>
          <option value="anthropic">anthropic（/v1/messages）</option>
        </select>
      </div>
      <p v-if="modelError" class="glass-tinted-red px-3 py-2 rounded text-xs">{{ modelError }}</p>
      <div class="flex items-center gap-2">
        <label class="w-32 text-sm" style="color: var(--win-text-secondary)" v-tooltip="tooltips.max_tokens">Max Tokens:</label>
        <input v-model.number="config.api.max_tokens" :class="errCls('max_tokens')" type="number" class="glass-input" style="width: 130px" />
      </div>
      <div class="flex items-center gap-2">
        <label class="w-32 text-sm" style="color: var(--win-text-secondary)" v-tooltip="tooltips.timeout">分析 Timeout (s):</label>
        <input v-model.number="config.api.timeout" :class="errCls('timeout')" type="number" class="glass-input" style="width: 110px" />
      </div>
      <div class="flex items-center gap-2">
        <label class="w-32 text-sm" style="color: var(--win-text-secondary)" v-tooltip="tooltips.summary_timeout">总结 Timeout (s):</label>
        <input v-model.number="config.api.summary_timeout" :class="errCls('summary_timeout')" type="number" class="glass-input" style="width: 110px" />
      </div>
      <div class="flex items-center gap-2">
        <label class="w-32 text-sm" style="color: var(--win-text-secondary)" v-tooltip="tooltips.json_mode">JSON 模式:</label>
        <select v-model="config.api.json_mode" class="glass-input flex-1">
          <option v-for="m in jsonModes" :key="m.value" :value="m.value">{{ m.label }}</option>
        </select>
      </div>
      <div class="flex items-center gap-2">
        <label class="w-32 text-sm" style="color: var(--win-text-secondary)" v-tooltip="tooltips.thinking_mode">思考模式:</label>
        <select :value="getThinkingMode()" @change="setThinkingMode(($event.target as HTMLSelectElement).value)" class="glass-input flex-1">
          <option v-for="m in [...thinkingModes, ...dynamicThinkingOptions]" :key="m.value" :value="m.value">{{ m.label }}</option>
        </select>
        <button @click="runProbeThinking" :disabled="probeBusy || !config?.api.model" class="glass-button btn-sm" title="对当前端点发几次微请求，实测哪个禁用思考参数有效，命中后自动填入上方下拉">
          {{ probeBusy ? '探测中...' : '自动探测' }}
        </button>
      </div>
      <!-- 状态提示：不点探测也可见（M3 事故 10.18——`{}` 是跟随端点默认，部分端点默认开 thinking） -->
      <div v-if="thinkingState === 'default'" class="ml-32 text-xs glass-tinted-red px-2 py-1 rounded" style="color: var(--win-warning)">
        ⚠ 跟随端点默认：部分模型（M3/GLM 等）的 chat/completions 端点默认开启思考链，大 prompt 可能长时间无响应——建议点「自动探测」
      </div>
      <div v-else-if="thinkingState === 'disabled'" class="ml-32 text-xs glass-tinted-green px-2 py-1 rounded">
        ✓ 已显式禁用思考链<span class="ml-1" style="color: var(--win-text-secondary)">（{{ THINKING_ANTHROPIC_CAVEAT }}）</span>
      </div>
      <div v-if="probeResult" class="ml-32 space-y-2 text-xs">
        <p v-if="probeResult.note" :class="probeResult.best || !probeResult.default_thinks ? 'glass-tinted-green px-2 py-1 rounded' : 'glass-tinted-red px-2 py-1 rounded'">
          {{ probeResult.note }}
        </p>
        <!-- 基线检测详情：展开式 7 维度列表 -->
        <details v-if="probeResult.default_detection_breakdown && Object.values(probeResult.default_detection_breakdown).some(v => v)" class="px-2 py-1 rounded" style="background: var(--win-layer-soft, var(--win-layer))">
          <summary style="cursor: pointer; color: var(--win-warning)">
            ⚠ 基线检测到 thinking 痕迹（点击展开分项详情）
          </summary>
          <div class="mt-1 space-y-0.5 pl-3">
            <div v-for="(hit, key) in probeResult.default_detection_breakdown" :key="key" class="flex gap-2">
              <span v-if="hit" style="color: var(--win-warning)">●</span>
              <span v-else style="color: var(--win-text-disabled)">○</span>
              <span :style="hit ? 'color: var(--win-text-primary)' : 'color: var(--win-text-disabled)'">
                {{ PROBE_DETECTOR_LABELS[key] || key }}
              </span>
            </div>
          </div>
        </details>
        <div v-for="r in probeResult.results" :key="r.param" class="flex gap-2 items-center flex-wrap">
          <span class="w-40 shrink-0" style="color: var(--win-text-secondary)">{{ r.param }}</span>
          <span v-if="r.worked === true" style="color: var(--win-success)">✓ 有效</span>
          <span v-else-if="r.worked === false" style="color: var(--win-danger)">✗ 无效</span>
          <span v-else style="color: var(--win-text-disabled)">基线</span>
          <span style="color: var(--win-text-disabled)">
            思考 {{ r.reasoning_chars }}字 / 内容 {{ r.content_chars }}字
            <template v-if="r.think_tag_chars > 0"> / 标签 {{ r.think_tag_chars }}字</template>
            <template v-if="r.reasoning_token_count > 0"> / tokens {{ r.reasoning_token_count }}</template>
          </span>
          <span v-if="r.error" style="color: var(--win-danger)">{{ r.error }}</span>
        </div>
      </div>
      <p v-if="probeError" class="ml-32 glass-tinted-red px-3 py-2 rounded text-xs">{{ probeError }}</p>
    </div>

    <div class="glass-card p-4 space-y-3">
      <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">温度退火 & 重试</h3>
      <div class="grid grid-cols-2 gap-3">
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.temperature">初始温度:</label><input v-model.number="config.api.temperature" :class="errCls('temperature')" type="number" step="0.01" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.temperature_step">温度步长:</label><input v-model.number="config.api.temperature_step" :class="errCls('temperature_step')" type="number" step="0.01" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.temperature_max_retries">退火重试:</label><input v-model.number="config.api.temperature_max_retries" :class="errCls('temperature_max_retries')" type="number" min="1" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.backoff_max_retries">退避重试:</label><input v-model.number="config.api.backoff_max_retries" :class="errCls('backoff_max_retries')" type="number" min="0" class="glass-input" style="width: 110px" /></div>
      </div>
    </div>

    <div class="glass-card p-4 space-y-3">
      <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">分析配置</h3>
      <div class="grid grid-cols-2 gap-3">
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.concurrency">并发数:</label><input v-model.number="config.analysis.concurrency" type="number" min="1" max="20" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.block_size">块大小 (章/块):</label><input v-model.number="config.analysis.block_size" type="number" min="1" max="10" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.max_arc_length">主线最大字数:</label><input v-model.number="config.analysis.max_arc_length" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.max_arcs_in_prompt">Prompt 主线条数:</label><input v-model.number="config.analysis.max_arcs_in_prompt" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.max_summaries_in_prompt">Prompt 摘要数:</label><input v-model.number="config.analysis.max_summaries_in_prompt" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.timeline_truncate">时间线截断:</label><input v-model.number="config.analysis.timeline_truncate" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.max_character_states">角色状态数:</label><input v-model.number="config.analysis.max_character_states" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.max_world_items">世界观条数:</label><input v-model.number="config.analysis.max_world_items" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.max_foreshadow_entries">伏笔网络条数:</label><input v-model.number="config.analysis.max_foreshadow_entries" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.batch_summary_min_words">分卷摘要字数:</label><input v-model.number="config.analysis.batch_summary_min_words" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.final_report_min_words">最终报告字数:</label><input v-model.number="config.analysis.final_report_min_words" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" title="卷摘要拼接总字符超过此阈值时触发分层压缩（首尾各 1 组保留全文，中间组截断到 1/2）">压缩阈值 (字符):</label><input v-model.number="config.analysis.volume_compress_threshold" type="number" min="10000" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" title="每 N 卷为一组（首尾各 1 组保留全文，中间组截断到 1/2）">压缩组大小 (卷):</label><input v-model.number="config.analysis.volume_compress_group" type="number" min="1" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" title="伏笔最低保留重要度：低/中/高；过滤阶段会丢弃低于该等级的所有伏笔">伏笔最低重要度:</label>
          <select v-model="config.analysis.foreshadow_min_importance" class="glass-input" style="width: 110px"><option>低</option><option>中</option><option>高</option></select>
        </div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" title="伏笔最低保留置信度：低/中/高；importance 已收一道，confidence 兜底">伏笔最低置信度:</label>
          <select v-model="config.analysis.foreshadow_min_confidence" class="glass-input" style="width: 110px"><option>低</option><option>中</option><option>高</option></select>
        </div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" title="importance=高 的伏笔最多保留多少（-1=无上限）">高 importance 上限:</label><input v-model.number="config.analysis.max_foreshadow_catalog_high" type="number" min="-1" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" title="importance=中 的伏笔最多保留多少（按综合权重排序截断）">中 importance 上限:</label><input v-model.number="config.analysis.max_foreshadow_catalog_mid" type="number" min="0" class="glass-input" style="width: 110px" /></div>
      </div>
    </div>

    <div v-if="categoryDefs.length" class="glass-card p-4 space-y-3">
      <div class="flex items-center justify-between pb-2" style="border-bottom: 1px solid var(--win-stroke)">
        <h3 class="font-semibold" style="letter-spacing: -0.01em">伏笔分类（功能类别）</h3>
        <div class="flex gap-2">
          <button @click="selectAllCategories" class="glass-pill">全选</button>
          <button @click="clearAllCategories" class="glass-pill" title="清空 = 只保留「其他」兜底">清空</button>
        </div>
      </div>
      <p class="text-xs" style="color: var(--win-text-secondary)">
        LLM 抽取伏笔时会自由产出 type 字符串（可能 100+ 种），系统会用 LLM 归一化到下列 50 个功能类别之一。
        未勾选的类别会被过滤掉。建议至少保留「其他」作为兜底。已选 {{ keptSet.size }} / {{ categoryDefs.length }}。
      </p>
      <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
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

    <div class="glass-card p-4 space-y-3">
      <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">滚动总结参数</h3>
      <div class="grid grid-cols-2 gap-3">
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.rolling_early_chapters">触发章数:</label><input v-model.number="config.analysis.rolling_early_chapters" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.rolling_max_milestones">里程碑上限:</label><input v-model.number="config.analysis.rolling_max_milestones" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.rolling_max_momentum">势头上限:</label><input v-model.number="config.analysis.rolling_max_momentum" type="number" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.checkpoint_interval">Checkpoint:</label><input v-model.number="config.analysis.checkpoint_interval" type="number" min="0" max="100" class="glass-input" style="width: 110px" /></div>
      </div>
      <label class="flex items-center gap-2 text-sm cursor-pointer" v-tooltip="tooltips.auto_archive">
        <input v-model="config.analysis.auto_archive" type="checkbox" />
        自动归档
      </label>
      <label class="flex items-center gap-2 text-sm cursor-pointer" v-tooltip="tooltips.auto_summary">
        <input v-model="config.analysis.auto_summary" type="checkbox" />
        队列完成后自动总结
      </label>
      <label class="flex items-center gap-2 text-sm cursor-pointer" v-tooltip="tooltips.skip_moderation_blocked">
        <input v-model="config.analysis.skip_moderation_blocked" type="checkbox" />
        跳过内容审核拦截章节
      </label>
      <div class="grid grid-cols-2 gap-3">
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.summary_concurrency">总结并发数:</label><input v-model.number="config.analysis.summary_concurrency" type="number" min="1" max="20" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" v-tooltip="tooltips.summary_batch_size">总结批次大小:</label><input v-model.number="config.analysis.summary_batch_size" type="number" min="5" class="glass-input" style="width: 110px" /></div>
        <div class="flex items-center gap-2"><label class="text-sm flex-1" style="color: var(--win-text-secondary)" title="全书伏笔复检时每次 LLM 调用携带的活跃伏笔数；调大可减少调用次数省钱，单批过大可能稀释注意力降低判断质量">复检批大小:</label><input v-model.number="config.analysis.foreshadow_recheck_batch_size" type="number" min="10" max="200" class="glass-input" style="width: 110px" /></div>
      </div>
    </div>

    <div class="flex gap-3 items-center">
      <button @click="save" :disabled="saving" class="glass-button glass-button-primary btn-lg">
        {{ saving ? '保存中...' : '保存设置' }}
      </button>
      <Transition name="modal">
        <span v-if="saved" class="glass-badge badge-green" style="font-size: 12px"> 已保存</span>
      </Transition>
    </div>
    <div v-if="saveError" class="glass-card p-3 space-y-1" style="border-color: var(--win-danger)">
      <p class="text-sm font-medium" style="color: var(--win-danger)">保存失败</p>
      <pre class="text-xs whitespace-pre-wrap" style="color: var(--win-danger)">{{ saveError }}</pre>
    </div>

    <!-- 运行日志（完整）：存放真日志，分析队列页只展示简化版 -->
    <div class="glass-card p-4 space-y-3">
      <div class="flex items-center justify-between">
        <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em; flex: 1">运行日志（完整）</h3>
        <button @click="clearLogs" class="glass-button btn-danger-link">清空日志</button>
      </div>
      <p class="text-xs" style="color: var(--win-text-disabled)">
        这是完整的运行日志（含调试信息），跨页面与会话保留。分析队列页面仅显示过滤掉调试信息的简化版。
      </p>
      <LogConsole :logs="storeLogs" class="settings-log" />
    </div>
  </div>
</template>

<style scoped>
/* Win11 输入校验态：危险红 + 1px 焦点环 */
.input-invalid {
  border-color: var(--win-danger) !important;
  box-shadow: 0 0 0 1px var(--win-danger);
}

/* 模型下拉项：4px 圆角 + hover token */
.model-option {
  border-radius: 4px;
  transition: background var(--win-duration-fast) var(--win-ease);
}
.model-option:hover {
  background: var(--win-control-hover);
}

/* 伏笔分类 chip：Win11 轻量筛选 chip */
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

/* ===== 按钮尺寸修饰 ===== */
/* 紧凑按钮：覆盖 .glass-button 的 padding 0 16px → 0 12px，高度仍由基类 32px 控制 */
.btn-sm {
  padding: 0 12px;
  white-space: nowrap;
}
/* 主操作按钮：32px 高 + 略宽 padding */
.btn-lg {
  padding: 0 20px;
}
/* 文字危险按钮：保留 32px 高，仅文字红 */
.btn-danger-link {
  color: var(--win-danger);
  font-size: 12px;
  padding: 0 12px;
}
</style>
