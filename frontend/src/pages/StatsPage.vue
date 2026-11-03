<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { api, type TokenStatsResponse, type UsageLedgerResponse } from '../api/client'
import BookSelector from '../components/BookSelector.vue'

const stats = ref<TokenStatsResponse | null>(null)
const usage = ref<UsageLedgerResponse | null>(null)
const running = ref(false)
const error = ref('')
const clearBusy = ref(false)
let timer: ReturnType<typeof setInterval> | null = null

async function refresh() {
  try {
    // 运行状态与统计分离：结束后也能查看本次完整统计
    const status = await api.analysisStatus()
    running.value = status?.running ?? false
    stats.value = await api.getTokenStats()
    usage.value = await api.getUsage()
    error.value = ''
  } catch (e) {
    // 失败显式提示，不再静默保留旧数据
    error.value = '刷新统计失败: ' + (e as Error).message
  }
}

async function handleClear() {
  if (!window.confirm('确定清零全局累计账本？清零后累计消耗与费用归零（旧数据备份为 usage_state.json.bak），当前会话与按书历史不受影响。')) return
  clearBusy.value = true
  try {
    await api.clearUsage()
    await refresh()
  } catch (e) {
    error.value = '清零失败: ' + (e as Error).message
  } finally {
    clearBusy.value = false
  }
}
onMounted(() => { refresh(); timer = setInterval(refresh, 5000) })
onUnmounted(() => { if (timer) clearInterval(timer) })

// ============ 全局累计账本（持久化，跨进程累积） ============

const CATEGORY_LABELS: Record<string, string> = {
  chapter: '章节分析', rolling: '滚动总结', batch: '卷摘要', final: '最终报告',
  foreshadow: '伏笔分析', summary: '卷摘要（总结侧）', reconciliation: '对账',
  recheck: '复检', style: '文风',
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

// ============ 按书历史（落盘 JSON） ============

const historyBookId = ref('')
const historyData = ref<{ analysis?: any; summary?: any } | null>(null)
const historyError = ref('')
async function loadHistory() {
  historyError.value = ''
  historyData.value = null
  try {
    historyData.value = await api.getBookTokenStats(historyBookId.value)
  } catch (e) { historyError.value = (e as Error).message }
}

function fmtUsd(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return '—'
  return `~$${usd.toFixed(4)}`
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center gap-2">
      <h2 class="section-title">
        统计面板
        <span
          class="glass-badge ml-2 align-middle"
          :class="running ? 'badge-green' : 'badge-gray'"
          style="font-size: 11px"
        >分析服务: {{ running ? '运行中' : '空闲' }}</span>
      </h2>
      <button
        class="glass-button op-btn op-danger"
        style="font-size: 12px; margin-left: auto"
        :disabled="clearBusy || !hasLedgerData"
        @click="handleClear"
      >清零账本</button>
    </div>

    <p v-if="error" class="glass-tinted-red px-3 py-2 rounded text-sm">{{ error }}</p>

    <!-- 全局累计账本：持久化（usage_state.json），跨进程累积，只增不减 -->
    <div v-if="hasLedgerData" class="glass-card overflow-hidden">
      <div class="px-4 py-2.5 text-sm font-semibold" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">
        全局累计账本
        <span class="ml-2 text-xs" style="color: var(--win-text-secondary)">
          持久化，跨进程累积；牌价目录 {{ usage?.catalog_version || '—' }}，费用为 models.dev 挂牌价估算
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

    <!-- 按模型费用：费用只在模型维度计算（跨书换模型不会串价） -->
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
        未计价模型（牌价库查不到，不计入费用）：{{ usage.unpriced.join('、') }}
      </div>
    </div>

    <div v-if="hasLedgerData" class="grid grid-cols-4 gap-3">
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
      暂无累计数据——跑一轮分析/总结后这里会持续累积（持久化，重启不清零）
    </div>

    <!-- 当前会话逐章明细：分析服务内存态，进程重启清空 -->
    <div v-if="stats?.chapter_stats?.length" class="glass-card overflow-hidden">
      <div class="px-4 py-2.5 text-sm font-semibold" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">
        每章统计 ({{ stats.chapter_stats.length }}章)
        <span class="ml-2 text-xs" style="color: var(--win-text-secondary)">当前会话逐章明细（进程重启清空）</span>
      </div>
      <div class="stat-scroll">
        <table class="glass-table" style="font-size: 12px">
          <thead>
            <tr>
              <th class="text-left">章号</th>
              <th class="text-left">耗时(s)</th>
              <th class="text-left">输入Tokens</th>
              <th class="text-left">输出Tokens</th>
              <th class="text-left">重试</th>
              <th class="text-left">失败Token</th>
              <th class="text-left">t/s</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in stats.chapter_stats" :key="s.chapter">
              <td>{{ s.chapter }}</td>
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

    <div class="glass-card p-4 space-y-3">
      <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">按书历史 Token 统计</h3>
      <div class="flex gap-2 items-center flex-wrap">
        <div class="flex-1 min-w-[200px]"><BookSelector v-model="historyBookId" /></div>
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
    </div>
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
