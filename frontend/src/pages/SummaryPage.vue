<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import BookSelector from '../components/BookSelector.vue'
import LogConsole, { type LogEntry } from '../components/LogConsole.vue'
import { api, type SummaryStatus } from '../api/client'
import { renderMarkdown } from '../utils/markdown'
import {
  SUMMARY_PHASES, phaseLabelOf, phaseIndexOf, stepClassOf, overallPctOf,
  batchPctOf, elapsedTextOf, summaryTokenTextOf, showProgressOf,
  dictToJsonString, jsonStringToDict,
} from '../utils/summaryProgress'
import {
  inferThinkingState, makeThinkingPresets, THINKING_ANTHROPIC_CAVEAT,
  type ThinkingState,
} from '../utils/settingsForm'
import { useLogStore } from '../composables/useLogStore'

// 进度计算本体（4 阶段加权等）抽到 utils/summaryProgress.ts，可独立 vitest；
// SUMMARY_PHASES 同时供模板渲染步骤条

// ===== 共享书目 =====
const bookId = ref('')

// ===== 区块一：数据聚合 =====
const aggBusy = ref(false)
const aggFiles = ref<{ name: string; size_kb: number }[]>([])
const aggContent = ref<unknown>(null)
const aggIsJson = ref(false)
const aggError = ref('')

async function runAggregate() {
  aggBusy.value = true
  aggError.value = ''
  try { await api.runAggregate(bookId.value, false); await loadAggFiles() }
  catch (e) { aggError.value = (e as Error).message }
  finally { aggBusy.value = false }
}

async function loadAggFiles() {
  const bid = bookId.value
  if (!bid) { aggFiles.value = []; return }
  try {
    const res = await api.getAggregateFiles(bid)
    if (bid !== bookId.value) return   // 已切书：丢弃过期响应
    aggFiles.value = res.files
  } catch (e) { console.error(e) }
}

async function viewAggFile(name: string) {
  const bid = bookId.value
  if (!bid) return
  try {
    const res = await api.getAggregateFile(bid, name)
    if (bid !== bookId.value) return   // 已切书：丢弃过期响应
    aggContent.value = res.content; aggIsJson.value = res.is_json
  } catch (e) {
    if (bid !== bookId.value) return   // 已切书：过期失败的错误不污染新书
    aggError.value = (e as Error).message
  }
}

const aggNotice = ref('')
async function exportExcel() {
  aggError.value = ''
  aggNotice.value = ''
  try { await api.exportAggregateExcel(bookId.value); aggNotice.value = '导出成功' } catch (e) { aggError.value = (e as Error).message }
}

// ===== 区块二：最终总结 =====
const startCh = ref(1)
const endCh = ref(99999)
// 0802 spec：批次/并发参数持久化到后端 config.json（取代 localStorage）
const batchSize = ref(30)
const concurrency = ref(2)
// 总结专用模型/思考覆盖：空=跟随全局设置
const summaryModel = ref('')
const summaryThinking = ref('{}')
const modelOptions = ref<string[]>([])
let paramsLoaded = false
let persistTimer: ReturnType<typeof setTimeout> | null = null
let persistVersion = 0
const paramSaveError = ref('')
watch(batchSize, () => { if (paramsLoaded) schedulePersist() })
watch(concurrency, () => { if (paramsLoaded) schedulePersist() })
watch(summaryModel, () => { if (paramsLoaded) schedulePersist() })
watch(summaryThinking, () => { if (paramsLoaded) schedulePersist() })

// 防抖 + 版本号串行化：连续修改只触发一次写入；写入时重新读服务器最新配置再全量回写，
// 避免多个并发 PUT 基于旧快照互相覆盖（旧实现的竞态：快速改两个参数会丢其中一个）。
function schedulePersist() {
  persistVersion++
  const myVersion = persistVersion
  if (persistTimer) clearTimeout(persistTimer)
  persistTimer = setTimeout(async () => {
    persistTimer = null
    await persistSummaryParams()
    if (myVersion !== persistVersion) schedulePersist() // 写入期间又有改动，补一轮
  }, 600)
}

async function persistSummaryParams() {
  try {
    paramSaveError.value = ''
    // 注意：PUT /api/settings 是全量替换（缺失字段回退默认值），
    // 必须基于完整配置修改后整体写回，否则会重置 api_key 等其它配置。
    const cfg = await api.getSettings()
    cfg.analysis.summary_batch_size = batchSize.value
    cfg.analysis.summary_concurrency = concurrency.value
    cfg.api.summary_model = summaryModel.value
    cfg.api.summary_thinking_mode = jsonStringToDict(summaryThinking.value)
    await api.putSettings(cfg)
  } catch (e) {
    // 分析运行中 PUT 会返回 409：显式提示，避免用户以为改成功
    paramSaveError.value = '保存总结参数失败: ' + (e as Error).message
  }
}

// 思考控制选项（JSON 字符串 ↔ dict 的转换在 utils/summaryProgress，按参数名区分，不按厂商模型命名）
// 预设 value/技术标签与 SettingsPage 同源（settingsForm.makeThinkingPresets）；
// 首项 label 必须按页传入：总结侧 `{}` 经 final_summary 的 `or` 链回退全局 thinking_mode，
// 与主模型侧"直接跟随端点默认"语义不同
const THINKING_OPTIONS = makeThinkingPresets('自动（跟随全局设置）')

// 状态提示（不点探测也可见）：总结 `{}` 的实际效果取决于全局值，
// 两级组合显示防误报——全局已显式禁用时不再报"可能开启"（M3 事故教训 10.18）
const globalThinking = ref<Record<string, unknown> | undefined>(undefined)
const summaryThinkingState = computed<ThinkingState>(() =>
  inferThinkingState(jsonStringToDict(summaryThinking.value)))
const globalThinkingState = computed<ThinkingState>(() => inferThinkingState(globalThinking.value))
const globalThinkingLabel = computed(() => {
  if (globalThinkingState.value === 'disabled') return '✓ 已显式禁用'
  if (globalThinkingState.value === 'default') return '⚠ 跟随端点默认（可能开启思考链）'
  return '已自定义'
})

// 自动探测禁用思考参数（发微请求实测当前端点认哪个参数；对 summary_model 独立探测）
const probeBusy = ref(false)
const probeError = ref('')
const dynamicThinkingOptions = ref<{ value: string; label: string }[]>([])

async function runProbeThinking() {
  probeBusy.value = true
  probeError.value = ''
  const body: { base_url?: string; api_key?: string; provider?: string; model?: string } = {}
  // 照 SettingsPage 模式按 trim 条件包含；model 用总结专用模型（复用同一 base_url/api_key）
  const cfg = await api.getSettings().catch(() => null)
  if (cfg?.api.base_url?.trim()) body.base_url = cfg.api.base_url.trim()
  if (cfg?.api.api_key?.trim()) body.api_key = cfg.api.api_key.trim()
  if (cfg?.api.provider) body.provider = cfg.api.provider
  if (summaryModel.value.trim()) body.model = summaryModel.value.trim()
  try {
    const res = await api.probeThinking(body)
    dynamicThinkingOptions.value = []
    if (res.best?.thinking_mode) {
      const s = JSON.stringify(res.best.thinking_mode)
      if (!THINKING_OPTIONS.some((o) => o.value === s)) {
        dynamicThinkingOptions.value = [{ value: s, label: `探测命中: ${res.best.param || s}` }]
      }
      // 命中自动写入（与 SettingsPage 一致）；summaryThinking 是字符串 ref，经 dict 转换回写
      summaryThinking.value = dictToJsonString(res.best.thinking_mode)
      if (paramsLoaded) schedulePersist()
    }
  } catch (e) {
    probeError.value = '探测失败: ' + (e as Error).message
  } finally {
    probeBusy.value = false
  }
}

async function loadSummaryParams() {
  try {
    const cfg = await api.getSettings()
    // 门闩在赋值期间必须保持关闭：watcher 的 flush 发生在下一个微任务，
    // 若在赋值后同步置位 paramsLoaded，watcher 会把「初始回填」误判为用户改动，
    // 服务端值 ≠ 默认值时挂载即触发一次多余的 PUT
    batchSize.value = cfg.analysis.summary_batch_size ?? 30
    concurrency.value = cfg.analysis.summary_concurrency ?? 2
    summaryModel.value = cfg.api.summary_model ?? ''
    summaryThinking.value = dictToJsonString(cfg.api.summary_thinking_mode)
    globalThinking.value = cfg.api.thinking_mode  // 供 `{}` 两级组合状态提示判定实际效果
    // 当前模型不在列表时补一项（支持列表接口没返回的自定义模型）
    if (summaryModel.value && !modelOptions.value.includes(summaryModel.value)) {
      modelOptions.value.push(summaryModel.value)
    }
    await nextTick()  // 等本轮 watcher flush 跑完（此时门闩仍关 → 全部短路）
  } catch (e) { console.error('读取总结参数失败:', e) }
  paramsLoaded = true
}
async function loadModelOptions() {
  try {
    const res = await api.getModels()
    modelOptions.value = res.models ?? []
    if (summaryModel.value && !modelOptions.value.includes(summaryModel.value)) {
      modelOptions.value.push(summaryModel.value)
    }
  } catch (e) { console.error('获取模型列表失败:', e) }
}
const summaryStatus = ref<SummaryStatus | null>(null)
const report = ref('')
let pollTimer: ReturnType<typeof setInterval> | null = null

// ===== 总结日志抽屉（业务+技术日志，按后端 category="summary" 过滤）=====
const { logs: allLogs, removeByCategory } = useLogStore()
const summaryLogs = computed(() =>
  (allLogs.value as (LogEntry & { category?: string })[]).filter(
    (l) => l.category === 'summary' && l.kind !== 'debug' && l.source !== 'block',
  ),
)
const drawerOpen = ref(false)
// 点击开始总结后自动展开；阶段完成（报告输出）后自动收起；可手动切换
watch(summaryStatus, (s) => {
  if (s?.phase === 'complete') drawerOpen.value = false
})

const reportHtml = computed(() => renderMarkdown(report.value))

// ===== 进度条（细化：4 阶段步骤 + 批次子进度 + 整体百分比 + 耗时）=====
const phaseLabel = computed(() => phaseLabelOf(summaryStatus.value?.phase))

const phaseIndex = computed(() => phaseIndexOf(summaryStatus.value?.phase))

function stepClass(i: number): string {
  return stepClassOf(phaseIndex.value, i)
}

const overallPct = computed(() => overallPctOf(summaryStatus.value))

const batchPct = computed(() => batchPctOf(summaryStatus.value))

const elapsedText = computed(() => elapsedTextOf(summaryStatus.value, Date.now() / 1000))

const summaryTokenText = computed(() => summaryTokenTextOf(summaryStatus.value))

const showProgress = computed(() => showProgressOf(summaryStatus.value))

const summaryError = ref('')
async function startSummary() {
  summaryError.value = ''
  try {
    await api.startSummary(bookId.value, startCh.value, endCh.value, batchSize.value, concurrency.value)
    drawerOpen.value = true // 开始总结 → 自动展开日志抽屉
  } catch (e) { summaryError.value = (e as Error).message }
}
async function stopSummary() { try { await api.stopSummary() } catch (e) { summaryError.value = (e as Error).message } }
async function refreshSummaryStatus() { try { summaryStatus.value = await api.summaryStatus() } catch (e) { console.error(e) } }
async function loadReport() {
  const bid = bookId.value
  if (!bid) return
  try {
    const res = await api.getBookReport(bid)
    if (bid !== bookId.value) return   // 已切书：A 书报告不得挂到 B 名下（v-html 渲染）
    report.value = res.report
  } catch (e) { console.error(e) }
}

// 切换书目时，自动刷新两个区块
watch(bookId, async () => {
  aggFiles.value = []
  aggContent.value = null
  aggError.value = ''
  report.value = ''
  await loadAggFiles()
  await loadReport()
})

onMounted(() => { loadSummaryParams(); loadModelOptions(); refreshSummaryStatus(); loadAggFiles(); loadReport(); pollTimer = setInterval(refreshSummaryStatus, 2000) })
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<template>
  <div class="space-y-6">
    <div>
      <h2 class="section-title">聚合与总结</h2>
      <p class="section-subtitle">最终总结生成全书报告，数据聚合生成结构化结果（可二选一使用）</p>
    </div>

    <BookSelector v-model="bookId" />

    <!-- 区块一：最终总结（更常用，置顶） -->
    <section class="glass-card p-4 space-y-3">
      <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">最终总结</h3>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div class="flex items-center gap-2">
          <label class="text-sm shrink-0" style="color: var(--win-text-secondary)">起始章:</label>
          <input v-model.number="startCh" type="number" min="1" class="glass-input flex-1" />
        </div>
        <div class="flex items-center gap-2">
          <label class="text-sm shrink-0" style="color: var(--win-text-secondary)">结束章:</label>
          <input v-model.number="endCh" type="number" min="1" class="glass-input flex-1" />
        </div>
        <div class="flex items-center gap-2">
          <label class="text-sm shrink-0" style="color: var(--win-text-secondary)">批次大小:</label>
          <input v-model.number="batchSize" type="number" min="5" class="glass-input flex-1" />
        </div>
        <div class="flex items-center gap-2">
          <label class="text-sm shrink-0" style="color: var(--win-text-secondary)">并发数:</label>
          <input v-model.number="concurrency" type="number" min="1" max="20" class="glass-input flex-1" />
        </div>
      </div>

      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div class="flex items-center gap-2">
          <label class="text-sm shrink-0" style="color: var(--win-text-secondary)" title="最终总结专用模型，复用同一 base_url/api_key；空=跟随全局模型（重型任务可切 MiniMax-M3 等）">总结模型:</label>
          <select v-model="summaryModel" class="glass-input flex-1">
            <option value="">跟随全局设置</option>
            <option v-for="m in modelOptions" :key="m" :value="m">{{ m }}</option>
          </select>
        </div>
        <div class="flex items-center gap-2">
          <label class="text-sm shrink-0" style="color: var(--win-text-secondary)" title="总结专用思考控制，空=跟随全局；M3/mimo/GLM 用 thinking 参数，DeepSeek/Qwen 用 enable_thinking（M2.x 关不掉思考）">思考模式:</label>
          <select v-model="summaryThinking" class="glass-input flex-1">
            <option v-for="o in [...THINKING_OPTIONS, ...dynamicThinkingOptions]" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
          <button @click="runProbeThinking" :disabled="probeBusy || !summaryModel.trim()" class="glass-button btn-sm" title="对总结专用模型发几次微请求，实测哪个禁用思考参数有效，命中后自动填入上方下拉（复用全局 base_url/api_key）">
            {{ probeBusy ? '探测中...' : '自动探测' }}
          </button>
        </div>
      </div>
      <!-- 状态提示：`{}` 两级组合显示（总结 `{}` 回退全局——风险措辞仅当全局也是"默认"时出现） -->
      <div v-if="summaryThinkingState === 'default'" class="text-xs px-2 py-1 rounded" :class="globalThinkingState === 'default' ? 'glass-tinted-red' : 'glass-tinted-green'">
        <span v-if="globalThinkingState === 'default'" style="color: var(--win-warning)">
          ⚠ 跟随全局设置（当前全局：{{ globalThinkingLabel }}）——总结模型直接暴露在端点默认行为下
        </span>
        <span v-else>跟随全局设置（当前全局：{{ globalThinkingLabel }}）</span>
      </div>
      <div v-else-if="summaryThinkingState === 'disabled'" class="text-xs glass-tinted-green px-2 py-1 rounded">
        ✓ 已显式禁用思考链<span class="ml-1" style="color: var(--win-text-secondary)">（{{ THINKING_ANTHROPIC_CAVEAT }}）</span>
      </div>
      <div v-else-if="dynamicThinkingOptions.length" class="text-xs glass-tinted-green px-2 py-1 rounded">
        ✓ 探测命中并已应用：{{ dynamicThinkingOptions[0]?.label }}
      </div>
      <p v-if="probeError" class="glass-tinted-red px-3 py-2 rounded text-sm">{{ probeError }}</p>
      <p v-if="paramSaveError" class="glass-tinted-red px-3 py-2 rounded text-sm">{{ paramSaveError }}</p>

      <div class="flex gap-2">
        <button v-if="!summaryStatus?.running" @click="startSummary" :disabled="!bookId" class="glass-button glass-button-primary">开始总结</button>
        <button v-else @click="stopSummary" class="glass-button glass-button-danger">停止</button>
        <button @click="loadReport" :disabled="!bookId" class="glass-button">加载报告</button>
      </div>

      <p v-if="summaryError" class="glass-tinted-red px-3 py-2 rounded text-sm">{{ summaryError }}</p>

      <!-- 细化进度条：对齐旧版日志「最终总结」部分的 4 阶段粒度 -->
      <div v-if="showProgress" class="summary-progress">
        <div class="step-row">
          <div v-for="(ph, i) in SUMMARY_PHASES" :key="ph.key" class="step" :class="stepClass(i)">
            <span class="step-dot">{{ stepClass(i) === 'done' ? '✓' : (i + 1) }}</span>
            <span class="step-label">{{ ph.label }}</span>
          </div>
        </div>

        <div class="bar"><div class="bar-fill" :style="{ width: overallPct + '%' }"></div></div>

        <div class="progress-meta">
          <span class="pm-phase">{{ phaseLabel }}</span>
          <span v-if="summaryStatus?.phase === 'batch'" class="pm-sub">
            卷 {{ summaryStatus.batches_done }}/{{ summaryStatus.total_batches }} · {{ batchPct }}%
          </span>
          <span v-else-if="summaryStatus?.phase === 'complete'" class="pm-sub pm-ok">✓ 全书脉络报告已生成</span>
          <span v-else-if="summaryStatus?.phase === 'failed'" class="pm-sub pm-warn">✗ 总结失败</span>
          <span v-else-if="summaryStatus?.phase === 'stopped'" class="pm-sub pm-warn">■ 已停止</span>
          <span class="pm-time">⏱ {{ elapsedText }}</span>
        </div>
        <p v-if="summaryStatus?.error" class="pm-error">错误: {{ summaryStatus.error }}</p>
        <p v-else-if="summaryStatus?.phase === 'complete'" class="pm-done-note">
          共 {{ summaryStatus.total_batches }} 批 · 总耗时 {{ elapsedText }}
        </p>
      </div>

      <div v-if="summaryTokenText" class="text-xs" style="color: var(--win-text-secondary)">{{ summaryTokenText }}</div>

      <!-- 总结日志抽屉：阶段提示 + 技术明细（category=summary），报告区上方 -->
      <div class="glass-card">
        <div class="flex items-center gap-2 px-3 py-2 cursor-pointer select-none" @click="drawerOpen = !drawerOpen">
          <Icon name="arrow_down" :size="13" :style="{ transform: drawerOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }" />
          <span class="text-sm font-medium" style="color: var(--win-text-primary)">总结日志</span>
          <span class="text-xs" style="color: var(--win-text-disabled)">{{ summaryLogs.length }} 条</span>
          <span class="flex-1"></span>
          <button
            v-if="summaryLogs.length"
            @click.stop="removeByCategory('summary')"
            class="glass-button btn-sm"
            style="font-size: 11px"
          >清空</button>
        </div>
        <div v-if="drawerOpen" class="p-2">
          <LogConsole :logs="summaryLogs" />
        </div>
      </div>

      <div v-if="report" class="glass-card p-5 md-content" v-html="reportHtml"></div>
      <div v-if="!report" class="text-center py-8 text-sm" style="color: var(--win-text-disabled)">
        尚未加载报告。点击「加载报告」查看 final_summary_report.md
      </div>
    </section>

    <!-- 区块二：数据聚合 -->
    <section class="glass-card p-4 space-y-3">
      <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">数据聚合</h3>
      <div class="flex gap-2 flex-wrap">
        <button @click="runAggregate" :disabled="aggBusy || !bookId" class="glass-button glass-button-primary">开始聚合</button>
        <button @click="loadAggFiles" :disabled="!bookId" class="glass-button">刷新文件</button>
        <button @click="exportExcel" :disabled="!bookId" class="glass-button">导出Excel</button>
      </div>
      <p v-if="aggError" class="glass-tinted-red px-3 py-2 rounded text-sm">{{ aggError }}</p>
      <p v-if="aggNotice" class="glass-tinted-green px-3 py-2 rounded text-sm">{{ aggNotice }}</p>
      <div v-if="aggFiles.length" class="flex gap-2 flex-wrap">
        <button v-for="f in aggFiles" :key="f.name" @click="viewAggFile(f.name)" class="glass-button" style="font-size: 12px">{{ f.name }} ({{ f.size_kb }}KB)</button>
      </div>
      <pre v-if="aggContent" class="code-surface code-green p-4 overflow-auto max-h-96">{{ aggIsJson ? JSON.stringify(aggContent, null, 2) : aggContent }}</pre>
    </section>
  </div>
</template>

<style scoped>
/* ===== 细化进度条（对齐旧版日志 4 阶段粒度）===== */
.summary-progress {
  display: flex;
  flex-direction: column;
  gap: 12px;
  /* 16px 16px 对齐 4 倍数（原 14px 16px 不合规） */
  padding: 16px;
  border-radius: var(--win-radius-container);
  background: var(--win-layer);
  border: 1px solid var(--win-stroke);
}

.step-row {
  display: flex;
  gap: 8px;
}
.step {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: 40px;
  padding: 0 8px;
  border-radius: var(--win-radius-control);
  font-size: 12px;
  border: 1px solid var(--win-stroke);
  background: var(--win-control-alt);
  color: var(--win-text-disabled);
  transition: background var(--win-duration-fast) var(--win-ease), border-color var(--win-duration-fast) var(--win-ease), color var(--win-duration-fast) var(--win-ease);
}
.step-dot {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
  background: var(--win-control-hover);
  color: var(--win-text-secondary);
}
.step-label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.step.active {
  background: var(--win-accent-soft);
  border-color: var(--win-accent);
  color: var(--win-text-primary);
}
.step.active .step-dot {
  background: var(--win-accent);
  color: #fff;
}
.step.done {
  background: var(--win-success-bg);
  border-color: var(--win-success);
  color: var(--win-success);
}
.step.done .step-dot {
  background: var(--win-success);
  color: #fff;
}

.bar {
  height: 4px;
  border-radius: var(--win-radius-pill);
  background: var(--win-control-alt);
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  border-radius: var(--win-radius-pill);
  background: var(--win-accent);
  transition: width 0.45s var(--win-ease);
}

.progress-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--win-text-secondary);
}
.pm-phase {
  font-weight: 600;
  color: var(--win-text-primary);
}
.pm-sub {
  color: var(--win-text-secondary);
}
.pm-ok {
  color: var(--win-success);
}
.pm-warn {
  color: var(--win-danger);
}
.pm-time {
  margin-left: auto;
  color: var(--win-text-disabled);
}
.pm-error {
  font-size: 12px;
  color: var(--win-danger);
}
.pm-done-note {
  font-size: 12px;
  color: var(--win-text-secondary);
}
</style>
