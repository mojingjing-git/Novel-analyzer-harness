<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed, watch, inject } from 'vue'
import { routerKey, routeLocationKey } from 'vue-router'
import { api, type TokenStatsResponse, type UsageLedgerResponse } from '../api/client'
import BookSelector from '../components/BookSelector.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import LogConsole from '../components/LogConsole.vue'
import { useLogStore } from '../composables/useLogStore'

type StatsTab = 'ledger' | 'session' | 'history' | 'logs' | 'prompt'
const VALID_TABS: StatsTab[] = ['ledger', 'session', 'history', 'logs', 'prompt']

const router = inject(routerKey, null)
const route = inject(routeLocationKey, null)

function parseTabQuery(raw: unknown): StatsTab {
  return typeof raw === 'string' && VALID_TABS.includes(raw as StatsTab) ? (raw as StatsTab) : 'ledger'
}
function parseBookQuery(raw: unknown): string {
  return typeof raw === 'string' ? raw.trim() : ''
}

const activeTab = ref<StatsTab>(parseTabQuery(route?.query?.tab))
const stats = ref<TokenStatsResponse | null>(null)
const usage = ref<UsageLedgerResponse | null>(null)
const running = ref(false)
const error = ref('')
const clearBusy = ref(false)
const showClearConfirm = ref(false)
let timer: ReturnType<typeof setInterval> | null = null
let disposed = false

const { logs: storeLogs, clear: clearLogs } = useLogStore()
const logs = computed(() => storeLogs.value)

function selectTab(tab: StatsTab) {
  activeTab.value = tab
  if (router) {
    void router.replace({ query: { ...(route?.query || {}), tab } })
  }
}

async function refresh() {
  try {
    // 运行状态与统计分离：结束后也能查看本次完整统计
    const status = await api.analysisStatus()
    if (disposed) return
    running.value = status?.running ?? false
    const nextStats = await api.getTokenStats()
    if (disposed) return
    stats.value = nextStats
    const nextUsage = await api.getUsage()
    if (disposed) return
    usage.value = nextUsage
    error.value = ''
  } catch (e) {
    // 失败显式提示，不再静默保留旧数据
    if (!disposed) error.value = '刷新统计失败: ' + (e as Error).message
  }
}

function handleClear() {
  showClearConfirm.value = true
}

async function confirmClear() {
  showClearConfirm.value = false
  clearBusy.value = true
  try {
    await api.clearUsage()
    if (disposed) return
    await refresh()
  } catch (e) {
    if (!disposed) error.value = '清零失败: ' + (e as Error).message
  } finally {
    if (!disposed) clearBusy.value = false
  }
}

// ============ 全局累计账本（持久化，跨进程累积） ============

const CATEGORY_LABELS: Record<string, string> = {
  chapter: '章节分析', rolling: '滚动总结', batch: '卷摘要', final: '最终报告',
  foreshadow: '伏笔分析', summary: '卷摘要（总结侧）', reconciliation: '对账',
  recheck: '复检', style: '文风', atlas: '全书图谱',
}

const ledgerRows = computed(() => {
  const cats = usage.value?.categories || {}
  const models = usage.value?.category_models || {}
  return Object.entries(cats).map(([key, val]) => ({
    key,
    label: CATEGORY_LABELS[key] || key,
    models: (models[key] || []).join('、') || '—',
    input: val.in || 0,
    output: val.out || 0,
  }))
})

const feeRows = computed(() => (usage.value?.per_model || []).map(r => ({
  ...r,
  usdLabel: r.usd === null || r.usd === undefined ? '—' : `~$${r.usd.toFixed(4)}`,
})))

const totalUsdLabel = computed(() => {
  const u = usage.value
  if (!u) return '—'
  if (u.total_usd === null || u.total_usd === undefined) return '未定价'
  return `~$${u.total_usd.toFixed(4)}`
})

const ledgerTotalIn = computed(() => ledgerRows.value.reduce((s, r) => s + r.input, 0))
const ledgerTotalOut = computed(() => ledgerRows.value.reduce((s, r) => s + r.output, 0))
const ledgerCached = computed(() => (usage.value?.per_model || []).reduce((s, r) => s + (r.cached || 0), 0))
const ledgerHitRate = computed(() => {
  const denom = ledgerCached.value + ledgerTotalIn.value
  return denom > 0 ? `${((ledgerCached.value / denom) * 100).toFixed(1)}%` : '—'
})
const hasLedgerData = computed(() => ledgerRows.value.length > 0)

// ============ 当前会话（分析服务内存态，进程重启清空） ============

const totalElapsed = computed(() => stats.value?.elapsed?.toFixed(0) || '0')
const recordedChaptersCount = computed(() =>
  (stats.value?.chapter_stats || []).reduce((sum, s) => sum + (s.chapters_count ?? 1), 0)
)

// ============ 按书历史（落盘 JSON） ============

const initialBook = parseBookQuery(route?.query?.book)
const historyBookId = ref(initialBook)
const historyData = ref<{ analysis?: any; summary?: any } | null>(null)
const historyError = ref('')
async function loadHistory() {
  if (!historyBookId.value) return
  historyError.value = ''
  historyData.value = null
  try {
    const res = await api.getBookTokenStats(historyBookId.value)
    if (!disposed) historyData.value = res
  } catch (e) {
    if (!disposed) historyError.value = (e as Error).message
  }
}

// ============ 日志下载 ============

async function handleDownloadLogs() {
  try {
    await api.downloadLogs()
  } catch (e) {
    if (!disposed) error.value = '下载日志失败: ' + (e as Error).message
  }
}

// ============ Prompt 预览（原 PromptPreviewPage 迁移） ============

const promptBookId = ref(initialBook)
const promptMaxChars = ref(0)
const promptChapter = ref(1)
const promptChapterLabel = ref('')
const promptSystem = ref('')
const promptUser = ref('')
const promptSysLen = ref(0)
const promptUsrLen = ref(0)
const promptTruncated = ref(false)
const promptTotalChars = ref(0)
const promptError = ref('')
const promptBusy = ref(false)

async function runPromptPreview() {
  if (!promptBookId.value) return
  promptError.value = ''
  promptBusy.value = true
  try {
    const res = await api.promptPreview(
      promptBookId.value,
      Number(promptMaxChars.value) || 0,
      Number(promptChapter.value) || 1,
    )
    if (disposed) return
    promptSystem.value = res.system_prompt
    promptUser.value = res.user_prompt
    promptSysLen.value = res.system_len
    promptUsrLen.value = res.user_len
    promptTruncated.value = res.chapter_truncated
    promptTotalChars.value = res.chapter_total_chars
    promptChapterLabel.value = res.chapter_label
  } catch (e) {
    if (!disposed) promptError.value = (e as Error).message
  } finally {
    if (!disposed) promptBusy.value = false
  }
}

watch(() => route?.query, (q) => {
  if (!q) return
  if (q.tab !== undefined) activeTab.value = parseTabQuery(q.tab)
  const b = parseBookQuery(q.book)
  if (b) {
    if (activeTab.value === 'history') {
      historyBookId.value = b
      void loadHistory()
    } else if (activeTab.value === 'prompt') {
      promptBookId.value = b
    }
  }
})

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 5000)
  if (initialBook && activeTab.value === 'history') {
    void loadHistory()
  }
})
onUnmounted(() => {
  disposed = true
  if (timer) clearInterval(timer)
})

function fmtUsd(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return '—'
  return `~$${usd.toFixed(4)}`
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-wrap items-center gap-2">
      <h2 class="section-title">
        统计与诊断
        <span
          class="glass-badge ml-2 align-middle"
          :class="running ? 'badge-green' : 'badge-gray'"
          style="font-size: 11px"
        >分析服务: {{ running ? '运行中' : '空闲' }}</span>
      </h2>
      <button
        v-if="activeTab === 'ledger'"
        class="glass-button op-btn op-danger"
        style="font-size: 12px; margin-left: auto"
        :disabled="clearBusy || !hasLedgerData"
        @click="handleClear"
      >清零账本</button>
    </div>

    <!-- 5-Tab 导航栏 -->
    <div class="flex flex-wrap gap-2" role="tablist">
      <button type="button" role="tab" :aria-selected="activeTab === 'ledger'" class="glass-pill" :class="{ 'is-active': activeTab === 'ledger' }" @click="selectTab('ledger')">账本概览</button>
      <button type="button" role="tab" :aria-selected="activeTab === 'session'" class="glass-pill" :class="{ 'is-active': activeTab === 'session' }" @click="selectTab('session')">本次会话</button>
      <button type="button" role="tab" :aria-selected="activeTab === 'history'" class="glass-pill" :class="{ 'is-active': activeTab === 'history' }" @click="selectTab('history')">按书历史</button>
      <button type="button" role="tab" :aria-selected="activeTab === 'logs'" class="glass-pill" :class="{ 'is-active': activeTab === 'logs' }" @click="selectTab('logs')">实时日志 ({{ logs.length }})</button>
      <button type="button" role="tab" :aria-selected="activeTab === 'prompt'" class="glass-pill" :class="{ 'is-active': activeTab === 'prompt' }" @click="selectTab('prompt')">Prompt 预览</button>
    </div>

    <p v-if="error" class="glass-tinted-red px-3 py-2 rounded text-sm">{{ error }}</p>

    <!-- Tab 1：全局累计账本 -->
    <section v-if="activeTab === 'ledger'" class="space-y-6">
      <div v-if="hasLedgerData" class="glass-card overflow-hidden">
        <div class="px-4 py-2.5 text-sm font-semibold" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">
          全局累计账本
          <span class="ml-2 text-xs" style="color: var(--win-text-secondary)">
            牌价目录 {{ usage?.catalog_version || '—' }}
          </span>
        </div>
        <table class="glass-table">
          <thead>
            <tr>
              <th class="text-left">分类</th>
              <th class="text-left">模型</th>
              <th class="text-right">输入 Tokens</th>
              <th class="text-right">输出 Tokens</th>
              <th class="text-right">总计</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in ledgerRows" :key="row.key">
              <td>{{ row.label }}</td>
              <td style="color: var(--win-text-secondary)">{{ row.models }}</td>
              <td class="text-right">{{ row.input.toLocaleString() }}</td>
              <td class="text-right">{{ row.output.toLocaleString() }}</td>
              <td class="text-right font-medium">{{ (row.input + row.output).toLocaleString() }}</td>
            </tr>
            <tr class="stat-total-row">
              <td class="font-semibold">汇总</td>
              <td></td>
              <td class="text-right font-semibold">{{ ledgerTotalIn.toLocaleString() }}</td>
              <td class="text-right font-semibold">{{ ledgerTotalOut.toLocaleString() }}</td>
              <td class="text-right font-semibold">{{ (ledgerTotalIn + ledgerTotalOut).toLocaleString() }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="hasLedgerData" class="glass-card overflow-hidden">
        <div class="px-4 py-2.5 text-sm font-semibold" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">按模型费用</div>
        <table class="glass-table">
          <thead>
            <tr>
              <th class="text-left">模型</th>
              <th class="text-right">输入 Tokens</th>
              <th class="text-right">输出 Tokens</th>
              <th class="text-right">缓存命中</th>
              <th class="text-right">费用</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in feeRows" :key="row.model">
              <td>{{ row.model }}</td>
              <td class="text-right">{{ row.in.toLocaleString() }}</td>
              <td class="text-right">{{ row.out.toLocaleString() }}</td>
              <td class="text-right">{{ row.cached.toLocaleString() }}</td>
              <td class="text-right font-medium">{{ row.usdLabel }}</td>
            </tr>
            <tr class="stat-total-row">
              <td class="font-semibold">汇总</td>
              <td colspan="3"></td>
              <td class="text-right font-semibold">{{ totalUsdLabel }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="usage?.unpriced?.length" class="px-4 py-2 text-xs" style="color: var(--win-text-secondary)">
          未计价模型：{{ usage.unpriced.join('、') }}
        </div>
      </div>

      <div v-if="hasLedgerData" class="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div class="glass-stat text-center">
          <div class="stat-num" style="color: var(--win-accent)">{{ usage?.chapters_done || 0 }}</div>
          <div class="glass-stat-label">累计章数</div>
        </div>
        <div class="glass-stat text-center">
          <div class="stat-num" style="color: var(--win-success)">{{ (usage?.elapsed_seconds || 0).toFixed(0) }}s</div>
          <div class="glass-stat-label">累计耗时</div>
        </div>
        <div class="glass-stat text-center">
          <div class="stat-num" style="color: var(--win-accent)">{{ ledgerTotalOut.toLocaleString() }}</div>
          <div class="glass-stat-label">输出 Tokens</div>
        </div>
        <div class="glass-stat text-center">
          <div class="stat-num" style="color: var(--win-success)">{{ totalUsdLabel }}</div>
          <div class="glass-stat-label">累计费用</div>
        </div>
        <div class="glass-stat text-center">
          <div class="stat-num" style="color: var(--win-warning)">{{ usage?.retries || 0 }}</div>
          <div class="glass-stat-label">重试次数</div>
        </div>
        <div class="glass-stat text-center">
          <div class="stat-num" style="color: var(--win-danger)">{{ (usage?.failed_tokens || 0).toLocaleString() }}</div>
          <div class="glass-stat-label">失败Tokens·已扣费</div>
        </div>
        <div class="glass-stat text-center">
          <div class="stat-num" style="color: var(--win-accent)">{{ ledgerHitRate }}</div>
          <div class="glass-stat-label">KV缓存命中率</div>
        </div>
        <div class="glass-stat text-center">
          <div class="stat-num" style="color: var(--win-accent)">{{ ledgerCached.toLocaleString() }}</div>
          <div class="glass-stat-label">缓存命中 Tokens</div>
        </div>
      </div>

      <div v-if="!hasLedgerData" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-disabled)">
        暂无累计数据
      </div>
    </section>

    <!-- Tab 2：本次会话（逐章明细，内存态） -->
    <section v-else-if="activeTab === 'session'" class="space-y-4">
      <div class="grid grid-cols-2 sm:grid-cols-3 gap-3">
        <div class="glass-stat text-center">
          <div class="stat-num" style="color: var(--win-accent)">{{ recordedChaptersCount }}</div>
          <div class="glass-stat-label">当前会话已记录章数</div>
        </div>
        <div class="glass-stat text-center">
          <div class="stat-num" style="color: var(--win-success)">{{ totalElapsed }}s</div>
          <div class="glass-stat-label">当前会话耗时</div>
        </div>
        <div class="glass-stat text-center">
          <div class="stat-num" style="color: var(--win-text-primary)">{{ running ? '运行中' : '空闲' }}</div>
          <div class="glass-stat-label">分析服务状态</div>
        </div>
      </div>

      <div v-if="stats?.chapter_stats?.length" class="glass-card overflow-hidden">
        <div class="px-4 py-2.5 text-sm font-semibold" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">
          每批/章统计 ({{ stats.chapter_stats.length }}条 · 共 {{ recordedChaptersCount }}章)
          <span class="ml-2 text-xs" style="color: var(--win-text-secondary)">当前会话 · 进程重启清空</span>
        </div>
        <div class="stat-scroll">
          <table class="glass-table" style="font-size: 12px">
            <thead>
              <tr>
                <th class="text-left">章号/范围</th>
                <th class="text-left">耗时(s)</th>
                <th class="text-left">输入Tokens</th>
                <th class="text-left">输出Tokens</th>
                <th class="text-left">重试</th>
                <th class="text-left">失败Token</th>
                <th class="text-left">t/s</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(s, idx) in stats.chapter_stats" :key="idx">
                <td>{{ s.range || s.chapter }}</td>
                <td>{{ s.elapsed?.toFixed(1) }}</td>
                <td>{{ s.input_tokens }}</td>
                <td>{{ s.output_tokens }}</td>
                <td>{{ s.retries ?? 0 }}</td>
                <td>{{ (s.failed_tokens ?? 0).toLocaleString() }}</td>
                <td>{{ s.elapsed > 0 ? (s.output_tokens / s.elapsed).toFixed(1) : '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      <div v-else class="glass-card p-8 text-center text-sm" style="color: var(--win-text-disabled)">
        当前会话暂无逐章统计数据
      </div>
    </section>

    <!-- Tab 3：按书历史（严格 v-if 懒挂载，避免未激活时抢占全局 BookSelector） -->
    <section v-else-if="activeTab === 'history'" class="glass-card p-4 space-y-3">
      <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">按书历史 Token 统计</h3>
      <div class="flex gap-2 items-center flex-wrap">
        <div class="flex-1 min-w-[200px]"><BookSelector v-model="historyBookId" :sync-global="false" /></div>
        <button @click="loadHistory" :disabled="!historyBookId" class="glass-button">查看历史</button>
      </div>
      <div v-if="historyError" class="glass-tinted-red px-4 py-2 rounded text-sm">{{ historyError }}</div>
      <div v-if="historyData" class="glass-card p-4 text-sm space-y-3">
        <div v-if="historyData.analysis">
          <div class="font-medium mb-1">
            分析阶段（{{ historyData.analysis.finished_at || '时间未知' }}）
            <span v-if="historyData.analysis.total_usd !== undefined" class="ml-2" style="color: var(--win-text-secondary)">
              合计 {{ fmtUsd(historyData.analysis.total_usd) }}
            </span>
          </div>
          <div v-for="(v, k) in (historyData.analysis.categories || {})" :key="'a'+k">
            {{ CATEGORY_LABELS[String(k)] || k }}：入 {{ v.input_tokens }} / 出 {{ v.output_tokens }}
            <span v-if="v.model" style="color: var(--win-text-secondary)">（{{ v.model }}）</span>
            <span v-if="v.usd !== undefined" style="color: var(--win-text-secondary)">· {{ fmtUsd(v.usd) }}</span>
          </div>
          <div v-if="historyData.analysis.cached_tokens">KV 缓存命中：{{ historyData.analysis.cached_tokens }}</div>
          <div v-if="historyData.analysis.elapsed">耗时：{{ Math.round(historyData.analysis.elapsed) }} 秒</div>
          <div v-if="historyData.analysis.chapter_stats">逐章记录：{{ historyData.analysis.chapter_stats.length }} 条</div>
        </div>
        <div v-if="historyData.summary">
          <div class="font-medium mb-1">
            总结阶段（{{ historyData.summary.finished_at || '时间未知' }}）
            <span v-if="historyData.summary.total_usd !== undefined" class="ml-2" style="color: var(--win-text-secondary)">
              合计 {{ fmtUsd(historyData.summary.total_usd) }}
            </span>
          </div>
          <div v-for="(v, k) in (historyData.summary.categories || {})" :key="'s'+k">
            {{ CATEGORY_LABELS[String(k)] || k }}：入 {{ v.input_tokens }} / 出 {{ v.output_tokens }}
            <span v-if="v.model" style="color: var(--win-text-secondary)">（{{ v.model }}）</span>
            <span v-if="v.usd !== undefined" style="color: var(--win-text-secondary)">· {{ fmtUsd(v.usd) }}</span>
          </div>
        </div>
        <div v-if="!historyData.analysis && !historyData.summary" style="color: var(--win-text-disabled)">该书暂无历史统计</div>
      </div>
    </section>

    <!-- Tab 4：完整运行日志（从 SettingsPage 收口至此） -->
    <section v-else-if="activeTab === 'logs'" class="glass-card p-4 space-y-3">
      <div class="flex items-center justify-between gap-2 pb-2" style="border-bottom: 1px solid var(--win-stroke)">
        <h3 class="font-semibold text-sm">完整运行日志（{{ logs.length }} 条）</h3>
        <div class="flex items-center gap-2">
          <button type="button" @click="clearLogs" :disabled="!logs.length" class="glass-button btn-head-sm">清空日志</button>
          <button type="button" @click="handleDownloadLogs" class="glass-button btn-head-sm">下载完整日志</button>
        </div>
      </div>
      <LogConsole :logs="logs" />
    </section>

    <!-- Tab 5：Prompt 预览（严格 v-if 懒挂载，syncGlobal=false；从独立路由/设置页收口至此） -->
    <section v-else-if="activeTab === 'prompt'" class="glass-card p-4 space-y-4">
      <h3 class="font-semibold text-sm pb-2" style="border-bottom: 1px solid var(--win-stroke)">章节 Prompt 预览</h3>
      <div class="flex gap-2 items-center flex-wrap">
        <div class="flex-1 min-w-[200px]"><BookSelector v-model="promptBookId" :sync-global="false" /></div>
        <label class="text-sm flex items-center gap-1" style="color: var(--win-text-secondary)">
          章节原文上限
          <input v-model.number="promptMaxChars" type="number" min="0" step="500" class="glass-input" style="width: 110px" placeholder="0=不截断" />
        </label>
        <label class="text-sm flex items-center gap-1" style="color: var(--win-text-secondary)">
          章节号
          <input v-model.number="promptChapter" type="number" min="1" step="1" class="glass-input" style="width: 90px" placeholder="1" />
        </label>
        <button type="button" @click="runPromptPreview" :disabled="!promptBookId || promptBusy" class="glass-button glass-button-primary">
          {{ promptBusy ? '生成中…' : '生成预览' }}
        </button>
      </div>
      <div v-if="promptError" class="glass-tinted-red px-4 py-2 rounded text-sm">{{ promptError }}</div>
      <p v-if="promptTotalChars > 0" class="text-xs" style="color: var(--win-text-disabled)">
        章节原文共 {{ promptTotalChars }} 字符{{ promptTruncated ? '（已截断预览）' : '' }}
      </p>
      <div v-if="promptSystem" class="space-y-1.5">
        <div class="text-xs font-medium" style="color: var(--win-text-secondary)">System Prompt ({{ promptSysLen }} 字符)</div>
        <pre class="code-surface code-green p-3 overflow-auto max-h-60">{{ promptSystem }}</pre>
      </div>
      <div v-if="promptUser" class="space-y-1.5">
        <div class="text-xs font-medium" style="color: var(--win-text-secondary)">User Prompt（第{{ promptChapter }}章 {{ promptChapterLabel }} · {{ promptUsrLen }} 字符）</div>
        <pre class="code-surface code-cyan p-3 overflow-auto max-h-60">{{ promptUser }}</pre>
      </div>
    </section>

    <ConfirmDialog
      v-if="showClearConfirm"
      title="清零全局累计账本"
      message="确定清零全局累计账本？清零后累计消耗与费用归零（旧数据备份为 usage_state.json.bak），当前会话与按书历史不受影响。"
      confirm-text="确认清零"
      :danger="true"
      @confirm="confirmClear"
      @cancel="showClearConfirm = false"
    />
  </div>
</template>

<style scoped>
.stat-num {
  font-size: 1.25rem;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.stat-total-row td {
  background: var(--win-control-alt);
  border-top: 1px solid var(--win-stroke);
}
.stat-scroll {
  max-height: 50vh;
  overflow: auto;
}
</style>
