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

const categories = ref<CategoryDef[]>([])
const selectedCategories = ref<Set<string>>(new Set())
const showCategoryFilter = ref(false)

const importanceRank: Record<string, number> = { 低: 0, 中: 1, 高: 2 }

onMounted(async () => {
  try {
    const res = await api.getForeshadowCategories()
    categories.value = res.defs
    // 默认全选（与 config.analysis.foreshadow_kept_categories 一致）
    selectedCategories.value = new Set(res.kept.length ? res.kept : res.defs.map(d => d.name))
  } catch (e) { console.error('加载伏笔分类失败', e) }
})

// 请求序号：快速切书时丢弃过期响应，防止 A 书慢返回覆盖 B 书时间线
let timelineSeq = 0

watch(bookId, async () => {
  const seq = ++timelineSeq
  if (!bookId.value) { events.value = []; foreshadows.value = []; ranking.value = []; return }
  try {
    const res = await api.getTimeline(bookId.value)
    if (seq !== timelineSeq) return   // 已切到别的书：丢弃过期响应
    events.value = res.events as TimelineEvent[]
    foreshadows.value = res.foreshadows as Foreshadow[]
    // 排行数据（可能 404，静默处理）
    try {
      const rankRes = await api.getForeshadowRanking(bookId.value)
      if (seq !== timelineSeq) return
      ranking.value = rankRes.ranking
    } catch {
      if (seq !== timelineSeq) return
      ranking.value = []  // 账本不存在，排行为空
    }
  } catch (e) {
    if (seq !== timelineSeq) return
    // 切书失败必须清空：否则残留上一本书的数据被误当成当前书
    events.value = []
    foreshadows.value = []
    ranking.value = []
    console.error('加载时间线失败，已清空:', e)
  }
})

const filteredEvents = computed(() =>
  events.value.filter(ev => (importanceRank[ev.importance ?? '中'] ?? 0) >= importanceRank[minImportance.value]),
)

const filteredForeshadows = computed(() =>
  foreshadows.value.filter(fs => {
    if ((importanceRank[fs.importance ?? '中'] ?? 0) < importanceRank[minImportance.value]) return false
    if (selectedCategories.value.size === 0) return true
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
    <h2 class="section-title">时间线</h2>
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
          :title="'按伏笔分类筛选（已选 ' + selectedCategories.size + '/' + categories.length + '）'"
        >分类筛选 ({{ selectedCategories.size }})</button>
      </template>
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
          :class="{ 'is-active': selectedCategories.has(cat.name) }"
          :title="cat.description"
        >
          {{ cat.name }}<span v-if="categoryStats.has(cat.name)" class="opacity-60 ml-1">({{ categoryStats.get(cat.name) }})</span>
        </button>
      </div>
    </div>
    <div v-if="!bookId" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-disabled)">请选择书目</div>
    <div v-else-if="mode === 'ranking'" class="space-y-2">
      <div v-if="ranking.length === 0" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-disabled)">
        暂无排行数据，请先运行最终总结
      </div>
      <div v-for="(item, idx) in ranking" :key="item.id" class="tl-card">
        <div class="flex items-start gap-3">
          <span class="text-xs font-medium mt-0.5 shrink-0" style="color: var(--win-text-disabled)">#{{ idx + 1 }}</span>
          <div class="flex-1">
            <p style="color: var(--win-text-primary)">{{ item.description }}</p>
            <div class="mt-1.5 text-xs flex gap-3 flex-wrap items-center" style="color: var(--win-text-secondary)">
              <span>分数: <strong>{{ item.composite_score }}</strong></span>
              <span>跨度: ch{{ item.first_seen_chapter }}→ch{{ item.last_seen_chapter }} ({{ item.span }}章)</span>
              <span>检测: {{ item.evidence_count }}次</span>
              <span v-if="item.status === 'active'" class="status-tag badge-blue" style="font-size: 11px">活跃</span>
              <span v-else-if="item.status === 'resolved'" class="status-tag badge-green" style="font-size: 11px">已回收</span>
              <span v-else-if="item.status === 'dormant'" class="status-tag badge-gray" style="font-size: 11px">休眠</span>
              <span v-if="item.evidence_count <= 1 && item.span === 0" class="status-tag badge-orange" style="font-size: 11px">⚠️ 仅检测1次</span>
            </div>
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
    <div v-else-if="chapters.length === 0" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-disabled)">暂无数据</div>
    <div v-else class="space-y-3">
      <div v-for="[ch, group] in chapters" :key="ch" class="flex gap-3">
        <div class="shrink-0 w-16 text-right">
          <span class="glass-badge badge-blue" style="font-size: 11px; padding: 4px 10px">第{{ ch }}章</span>
        </div>
        <div class="flex-1 space-y-2">
          <template v-if="mode === 'events'">
            <div v-for="(ev, idx) in group.events" :key="idx" class="tl-card">
              <div class="flex items-start gap-2">
                <span class="text-xs mt-0.5" style="color: var(--win-text-disabled)">#{{ idx + 1 }}</span>
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
