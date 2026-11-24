<script setup lang="ts">
import { ref, watch, computed, onMounted } from 'vue'
import BookSelector from '../components/BookSelector.vue'
import { api, type ForeshadowRankItem } from '../api/client'

interface TimelineEvent {
  chapter: number
  id?: string
  event: string
  characters: string | string[]
  function: string
  importance?: string
}

interface Foreshadow {
  chapter: number
  clue: string
  type: string
  category?: string
  confidence: number
  importance?: string
}

interface CategoryDef {
  name: string
  description: string
  examples: string[]
}

const bookId = ref('')
const mode = ref<'events' | 'foreshadows' | 'ranking'>('events')
const events = ref<TimelineEvent[]>([])
const foreshadows = ref<Foreshadow[]>([])
const ranking = ref<ForeshadowRankItem[]>([])
const minImportance = ref<'低' | '中' | '高'>('低')
const rankingStatusFilter = ref<'all' | 'active' | 'resolved' | 'dormant'>('all')

const filteredRanking = computed(() => {
  if (rankingStatusFilter.value === 'all') return ranking.value
  return ranking.value.filter(item => item.status === rankingStatusFilter.value)
})

const categories = ref<CategoryDef[]>([])
const selectedCategories = ref<Set<string> | null>(null)
const showCategoryFilter = ref(false)

const importanceRank: Record<string, number> = { 低: 0, 中: 1, 高: 2 }

onMounted(async () => {
  try {
    const res = await api.getForeshadowCategories()
    categories.value = res.defs
    if (res.defs.length) {
      selectedCategories.value = new Set(res.kept ?? res.defs.map(d => d.name))
    }
  } catch (e) { console.error('加载伏笔分类失败', e) }
})

const timelineLoading = ref(false)
const timelineError = ref('')
const rankingLoading = ref(false)
const rankingError = ref('')
const rankingMissing = ref('')
let timelineSeq = 0
let rankingSeq = 0

async function loadTimeline() {
  const id = bookId.value
  const seq = ++timelineSeq
  events.value = []
  foreshadows.value = []
  timelineError.value = ''
  timelineLoading.value = Boolean(id)
  if (!id) return
  try {
    const res = await api.getTimeline(id)
    if (seq !== timelineSeq) return
    events.value = res.events as TimelineEvent[]
    foreshadows.value = res.foreshadows as Foreshadow[]
  } catch (error) {
    if (seq === timelineSeq) timelineError.value = (error as Error).message || '无法读取时间线'
  } finally {
    if (seq === timelineSeq) timelineLoading.value = false
  }
}

async function loadRanking() {
  const id = bookId.value
  const seq = ++rankingSeq
  ranking.value = []
  rankingError.value = ''
  rankingMissing.value = ''
  rankingLoading.value = Boolean(id)
  if (!id) return
  try {
    const res = await api.getForeshadowRanking(id)
    if (seq === rankingSeq) ranking.value = res.ranking
  } catch (error) {
    if (seq !== rankingSeq) return
    const err = error as Error & { status?: number }
    if (err.status === 404) rankingMissing.value = err.message || '该书暂无伏笔排行'
    else rankingError.value = err.message || '无法读取伏笔排行'
  } finally {
    if (seq === rankingSeq) rankingLoading.value = false
  }
}

watch(bookId, () => {
  rankingStatusFilter.value = 'all'
  void loadTimeline()
  void loadRanking()
})

const viewLoading = computed(() => mode.value === 'ranking' ? rankingLoading.value : timelineLoading.value)
const viewError = computed(() => mode.value === 'ranking' ? rankingError.value : timelineError.value)

function retryCurrentView() {
  if (mode.value === 'ranking') void loadRanking()
  else void loadTimeline()
}

const filteredEvents = computed(() =>
  events.value.filter(ev => (importanceRank[ev.importance ?? '中'] ?? 0) >= importanceRank[minImportance.value]),
)

const filteredForeshadows = computed(() =>
  foreshadows.value.filter(fs => {
    if ((importanceRank[fs.importance ?? '中'] ?? 0) < importanceRank[minImportance.value]) return false
    if (selectedCategories.value === null) return true
    const cat = fs.category || '其他'
    return selectedCategories.value.has(cat)
  }),
)

const chapters = computed(() => {
  const map = new Map<number, { events: TimelineEvent[]; foreshadows: Foreshadow[] }>()
  if (mode.value === 'events') {
    for (const ev of filteredEvents.value) {
      if (!map.has(ev.chapter)) map.set(ev.chapter, { events: [], foreshadows: [] })
      map.get(ev.chapter)!.events.push(ev)
    }
  } else {
    for (const fs of filteredForeshadows.value) {
      if (!map.has(fs.chapter)) map.set(fs.chapter, { events: [], foreshadows: [] })
      map.get(fs.chapter)!.foreshadows.push(fs)
    }
  }
  return [...map.entries()].sort((a, b) => a[0] - b[0])
})

// 伏笔类型 → 玻璃着色（沿用系统色，亮暗主题自适应）
const typeTints: Record<string, string> = {
  plant: 'tl-tint-green',
  recall: 'tl-tint-blue',
  develop: 'tl-tint-orange',
  resolve: 'tl-tint-purple',
}

const typeLabels: Record<string, string> = {
  plant: '埋设',
  recall: '回收',
  develop: '发展',
  resolve: '揭晓',
}

const importanceBadge: Record<string, string> = {
  高: 'badge-red',
  中: 'badge-orange',
  低: 'badge-gray',
}

function charStr(c: string | string[]): string {
  if (Array.isArray(c)) return c.join(', ')
  return c
}

function toggleCategory(name: string) {
  const next = new Set(selectedCategories.value)
  if (next.has(name)) next.delete(name); else next.add(name)
  selectedCategories.value = next
}

function selectAllCategories() {
  selectedCategories.value = new Set(categories.value.map(c => c.name))
}

function clearAllCategories() {
  selectedCategories.value = new Set()
}

const categoryStats = computed(() => {
  // 统计当前 foreshadows 中各 category 出现次数（基于原始数据，不受筛选影响）
  const map = new Map<string, number>()
  for (const fs of foreshadows.value) {
    const cat = fs.category || '其他'
    map.set(cat, (map.get(cat) ?? 0) + 1)
  }
  return map
})
</script>

<template>
  <div class="space-y-6">
    <h2 class="section-title">事件与伏笔</h2>
    <BookSelector v-model="bookId" />
    <div v-if="bookId" class="flex gap-2 flex-wrap items-center">
      <button @click="mode = 'events'" class="glass-pill" :class="{ 'is-active': mode === 'events' }">事件时间线 ({{ filteredEvents.length }})</button>
      <button @click="mode = 'foreshadows'" class="glass-pill" :class="{ 'is-active': mode === 'foreshadows' }">伏笔时间线 ({{ filteredForeshadows.length }})</button>
      <button @click="mode = 'ranking'" class="glass-pill" :class="{ 'is-active': mode === 'ranking' }">伏笔排行 ({{ ranking.length }})</button>
      <template v-if="mode !== 'ranking'">
        <span class="text-sm ml-2" style="color: var(--win-text-secondary)">最低重要度:</span>
        <select v-model="minImportance" class="glass-select" style="width: 92px">
          <option value="低">全部</option>
          <option value="中">中及以上</option>
          <option value="高">只看高</option>
        </select>
        <button
          v-if="mode === 'foreshadows'"
          @click="showCategoryFilter = !showCategoryFilter"
          class="glass-pill"
          :class="{ 'is-active': showCategoryFilter }"
          :disabled="!categories.length"
          :title="categories.length ? '按伏笔分类筛选（已选 ' + (selectedCategories?.size ?? 0) + '/' + categories.length + '）' : '分类列表不可用时显示全部分类'"
        >分类筛选<span v-if="selectedCategories"> ({{ selectedCategories.size }})</span></button>
      </template>
      <div v-else class="flex items-center gap-1.5 ml-2 flex-wrap">
        <button
          v-for="opt in [
            { key: 'all', label: '全部' },
            { key: 'active', label: '活跃' },
            { key: 'resolved', label: '已回收' },
            { key: 'dormant', label: '休眠' },
          ]"
          :key="opt.key"
          type="button"
          class="glass-pill text-xs"
          :class="{ 'is-active': rankingStatusFilter === opt.key }"
          @click="rankingStatusFilter = opt.key as 'all' | 'active' | 'resolved' | 'dormant'"
        >{{ opt.label }}</button>
      </div>
    </div>
    <div
      v-if="mode === 'foreshadows' && showCategoryFilter && categories.length"
      class="glass-card p-3"
    >
      <div class="flex items-center justify-between mb-2">
        <div class="text-sm" style="color: var(--win-text-secondary)">仅显示勾选分类的伏笔</div>
        <div class="flex gap-2">
          <button @click="selectAllCategories" class="glass-pill">全选</button>
          <button @click="clearAllCategories" class="glass-pill">清空</button>
        </div>
      </div>
      <div class="flex gap-1.5 flex-wrap">
        <button
          v-for="cat in categories"
          :key="cat.name"
          @click="toggleCategory(cat.name)"
          class="glass-pill text-xs"
          :class="{ 'is-active': selectedCategories?.has(cat.name) }"
          :title="cat.description"
        >
          {{ cat.name }}<span v-if="categoryStats.has(cat.name)" class="opacity-60 ml-1">({{ categoryStats.get(cat.name) }})</span>
        </button>
      </div>
    </div>
    <div v-if="!bookId" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-disabled)">请选择书目</div>
    <div v-else-if="viewLoading" class="glass-card p-8 text-center text-sm" role="status">
      {{ mode === 'ranking' ? '正在加载伏笔排行…' : '正在加载时间线…' }}
    </div>
    <div v-else-if="viewError" class="glass-card p-8 text-center text-sm space-y-3">
      <p role="alert">{{ mode === 'ranking' ? '伏笔排行加载失败' : '时间线加载失败' }}：{{ viewError }}</p>
      <button type="button" class="glass-button" @click="retryCurrentView">重试</button>
    </div>
    <div v-else-if="mode === 'ranking'" class="space-y-2">
      <div v-if="ranking.length === 0" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-disabled)">
        {{ rankingMissing || '暂无排行数据' }}
      </div>
      <div v-else-if="filteredRanking.length === 0" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-disabled)">
        当前状态筛选下暂无伏笔
      </div>
      <div v-for="(item, idx) in filteredRanking" :key="item.id" class="tl-card">
        <div class="flex items-start gap-3">
          <span class="text-xs font-medium mt-0.5 shrink-0" style="color: var(--win-text-muted)">#{{ idx + 1 }}</span>
          <div class="flex-1 space-y-1">
            <p style="color: var(--win-text-primary)">{{ item.description }}</p>
            <div class="text-xs flex gap-3 flex-wrap items-center" style="color: var(--win-text-secondary)">
              <span>分数: <strong>{{ item.composite_score }}</strong></span>
              <span>跨度: ch{{ item.first_seen_chapter }}→ch{{ item.last_seen_chapter }} ({{ item.span }}章)</span>
              <span>检测: {{ item.evidence_count }}次</span>
              <span v-if="item.status === 'active'" class="status-tag badge-blue" style="font-size: 11px">活跃</span>
              <span v-else-if="item.status === 'resolved'" class="status-tag badge-green" style="font-size: 11px">已回收</span>
              <span v-else-if="item.status === 'dormant'" class="status-tag badge-gray" style="font-size: 11px">休眠</span>
              <span v-if="item.source_type === 'human_override'" class="status-tag badge-purple" style="font-size: 11px">人工复核</span>
              <span v-if="item.resolved_chapter != null && item.resolved_chapter > 0" style="color: var(--win-success)">
                回收于第{{ item.resolved_chapter }}章
              </span>
              <span v-if="item.resolution_quality === 'verified'" class="status-tag badge-green" style="font-size: 11px">证据已核实</span>
              <span v-else-if="item.resolution_quality === 'questioned'" class="status-tag badge-orange" style="font-size: 11px">证据存疑</span>
              <span v-if="item.needs_review" class="status-tag badge-orange" style="font-size: 11px">建议复审</span>
              <span v-if="item.evidence_count <= 1 && item.span === 0" class="status-tag badge-orange" style="font-size: 11px">⚠️ 仅检测1次</span>
            </div>
            <p v-if="item.resolution" class="text-xs" style="color: var(--win-text-secondary)">
              回收说明：{{ item.resolution }}
            </p>
          </div>
          <div class="shrink-0 flex flex-col items-end gap-1">
            <span class="glass-badge" :class="importanceBadge[item.importance] || 'badge-gray'">{{ item.importance }}</span>
            <!-- 分数条 -->
            <div class="w-20 h-1.5 rounded-full overflow-hidden" style="background: var(--win-stroke)">
              <div class="h-full rounded-full" :style="{ width: Math.min(item.composite_score / 4, 100) + '%', background: 'var(--win-accent)' }"></div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div v-else-if="chapters.length === 0" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-disabled)">
      {{ mode === 'foreshadows' && selectedCategories?.size === 0 ? '未选择伏笔分类' : '暂无符合当前条件的数据' }}
    </div>
    <div v-else class="space-y-3">
      <div v-for="[ch, group] in chapters" :key="ch" class="flex gap-3">
        <div class="shrink-0 w-16 text-right">
          <span class="glass-badge badge-blue" style="font-size: 11px; padding: 4px 10px">第{{ ch }}章</span>
        </div>
        <div class="flex-1 space-y-2">
          <template v-if="mode === 'events'">
            <div v-for="(ev, idx) in group.events" :key="idx" class="tl-card">
              <div class="flex items-start gap-2">
                <span class="text-xs mt-0.5" style="color: var(--win-text-muted)">#{{ idx + 1 }}</span>
                <div class="flex-1">
                  <p style="color: var(--win-text-primary)">{{ ev.event }}</p>
                  <div class="flex gap-3 mt-1 text-xs" style="color: var(--win-text-secondary)">
                    <span v-if="charStr(ev.characters)">角色: {{ charStr(ev.characters) }}</span>
                    <span v-if="ev.function">功能: {{ ev.function }}</span>
                  </div>
                </div>
                <span v-if="ev.importance" class="glass-badge shrink-0" :class="importanceBadge[ev.importance] || 'badge-gray'">{{ ev.importance }}</span>
              </div>
            </div>
          </template>
          <template v-else>
            <div v-for="(fs, idx) in group.foreshadows" :key="idx" class="tl-card" :class="typeTints[fs.type] || ''">
              <div class="flex items-start gap-2">
                <span class="text-xs font-medium mt-0.5">{{ typeLabels[fs.type] || fs.type }}</span>
                <div class="flex-1">
                  <p>{{ fs.clue }}</p>
                  <div class="mt-1 text-xs opacity-70 flex gap-2 items-center">
                    <span v-if="fs.category" class="status-tag badge-blue" style="font-size: 11px">{{ fs.category }}</span>
                    <span>置信度: {{ (fs.confidence * 100).toFixed(0) }}%</span>
                  </div>
                </div>
                <span v-if="fs.importance" class="glass-badge shrink-0" :class="importanceBadge[fs.importance] || 'badge-gray'">{{ fs.importance }}</span>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tl-card {
  background: var(--win-layer);
  border: 1px solid var(--win-stroke);
  border-radius: var(--win-radius-container);
  padding: 12px 14px;
  font-size: 13px;
  color: var(--win-text-primary);
  box-shadow: var(--win-shadow-control);
}
/* 伏笔类型着色：Win11 低饱和功能色浅底 + 同色描边 */
.tl-tint-green  { background: var(--win-success-bg); border-color: var(--win-success); }
.tl-tint-blue   { background: var(--win-info-bg);    border-color: var(--win-info); }
.tl-tint-orange { background: var(--win-warning-bg); border-color: var(--win-warning); }
.tl-tint-purple { background: var(--win-accent-soft); border-color: var(--win-accent); }
</style>
