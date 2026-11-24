<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'
import BookSelector from '../components/BookSelector.vue'
import {
  api,
  type StoryAtlasDto,
  type AtlasCharacter,
  type CharacterListItem,
  type CharacterCardData,
} from '../api/client'
import { themeColors, onThemeChange } from '../utils/theme'

echarts.use([GraphChart, TooltipComponent, LegendComponent, SVGRenderer])

const ROLE_TIER_LABELS: Record<string, string> = {
  protagonist: '主角',
  major_ally: '核心盟友',
  major_antagonist: '主要反派',
  key_support: '关键配角',
  hidden_power: '幕后势力',
}

const ROLE_TIER_SIZE: Record<string, number> = {
  protagonist: 42,
  major_ally: 30,
  major_antagonist: 30,
  key_support: 22,
  hidden_power: 26,
}

const REL_CATEGORY_LABELS: Record<string, string> = {
  ally: '同盟/伙伴',
  hostile: '敌对/宿敌',
  mentor: '师徒/传承',
  romance: '情感/羁绊',
  faction: '势力/上下级',
  complex: '复杂牵连',
}

const route = useRoute()
const router = useRouter()

const bookId = ref('')
let pendingCharId = typeof route?.query?.id === 'string'
  ? route.query.id
  : (typeof route?.query?.name === 'string' ? route.query.name : '')

const activeTab = ref<'graph' | 'index'>('graph')
const showEdgeLabels = ref(true)
const searchQuery = ref('')

const loading = ref(false)
const loadError = ref('')
const atlas = ref<StoryAtlasDto | null>(null)
const characters = ref<CharacterListItem[]>([])

const selected = ref<string | null>(null)
const card = ref<CharacterCardData | null>(null)
const cardLoading = ref(false)
const cardError = ref('')
const cardTab = ref<'overview' | 'trajectory'>('overview')

const chartContainer = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
let chartObserver: ResizeObserver | null = null
let unwatchTheme: (() => void) | null = null

let loadSeq = 0
let cardSeq = 0

function escHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

const filteredCharacters = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return characters.value
  return characters.value.filter((c) => {
    if (c.name.toLowerCase().includes(q)) return true
    if (c.faction?.toLowerCase().includes(q)) return true
    if (c.identity_summary?.toLowerCase().includes(q)) return true
    if (c.aliases?.some(a => a.toLowerCase().includes(q))) return true
    return false
  })
})

function formatChapters(chapters: number[]): string {
  if (!chapters || !chapters.length) return '无'
  const sorted = [...chapters].sort((a, b) => a - b)
  const ranges: string[] = []
  let start = sorted[0]
  let end = sorted[0]
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i] === end + 1) {
      end = sorted[i]
    } else {
      ranges.push(start === end ? `${start}` : `${start}-${end}`)
      start = end = sorted[i]
    }
  }
  ranges.push(start === end ? `${start}` : `${start}-${end}`)
  return ranges.join(', ')
}

function sortedStates(states: Record<string, string>): { chapter: number; state: string }[] {
  if (!states) return []
  return Object.entries(states)
    .map(([k, v]) => ({ chapter: Number(k), state: v }))
    .filter(item => Number.isFinite(item.chapter))
    .sort((a, b) => a.chapter - b.chapter)
}

function topRelationships(
  rels: Record<string, { event_count: number; first_encounter: number }>,
  limit = 6,
): { name: string; rel: { event_count: number; first_encounter: number } }[] {
  if (!rels) return []
  return Object.entries(rels)
    .map(([name, rel]) => ({ name, rel }))
    .sort((a, b) => (b.rel?.event_count || 0) - (a.rel?.event_count || 0))
    .slice(0, limit)
}

function roleTierLabel(tier?: string): string {
  if (!tier) return ''
  return ROLE_TIER_LABELS[tier] || tier
}

function relCategoryColor(category?: string): string {
  const c = themeColors()
  switch (category) {
    case 'ally': return c.success
    case 'hostile': return c.danger
    case 'mentor': return c.warning
    case 'romance': return '#8764B8'
    case 'faction': return c.info
    default: return c.textSecondary
  }
}

function relCategoryLabel(category?: string): string {
  if (!category) return '关联'
  return REL_CATEGORY_LABELS[category] || category
}

function computeGraphForce(nodeCount: number): {
  repulsion: number
  edgeLength: [number, number]
  gravity: number
  zoom: number
} {
  if (nodeCount <= 6) {
    return { repulsion: 210, edgeLength: [75, 130], gravity: 0.25, zoom: 1.25 }
  }
  if (nodeCount <= 12) {
    return { repulsion: 300, edgeLength: [90, 160], gravity: 0.16, zoom: 1.1 }
  }
  return { repulsion: 420, edgeLength: [105, 195], gravity: 0.10, zoom: 1.0 }
}

function handleResize() {
  chart?.resize()
}

function disposeChart() {
  chartObserver?.disconnect()
  chartObserver = null
  chart?.dispose()
  chart = null
}

function initChart() {
  const el = chartContainer.value
  if (!el) return
  if (chart && chart.getDom() !== el) {
    disposeChart()
  }
  if (!chart) {
    chart = echarts.init(el, null, { renderer: 'svg' })
    chart.on('click', (params) => {
      const dataObj = params.data && typeof params.data === 'object' ? (params.data as { name?: string }) : null
      if (params.dataType === 'node' && dataObj?.name) {
        void viewCard(dataObj.name)
      }
    })
    window.addEventListener('resize', handleResize)
    if (typeof ResizeObserver !== 'undefined') {
      chartObserver = new ResizeObserver(() => handleResize())
      chartObserver.observe(el)
    }
  }
}

function renderChart() {
  if (activeTab.value !== 'graph') return
  const data = atlas.value
  if (!data || !data.exists || !data.characters.length) {
    disposeChart()
    return
  }
  initChart()
  if (!chart) return

  const c = themeColors()
  const factionNames: string[] = []
  for (const ch of data.characters) {
    const f = ch.faction?.trim() || '散修/独立'
    if (!factionNames.includes(f)) factionNames.push(f)
  }
  const palette = [c.accent, c.success, c.warning, c.danger, '#8764B8', '#038387', '#CA5010', c.textSecondary]
  const categories = factionNames.map((name, idx) => ({
    name,
    itemStyle: { color: palette[idx % palette.length] },
  }))
  const factionIndex = new Map(factionNames.map((name, idx) => [name, idx]))
  const charByName = new Map<string, AtlasCharacter>(data.characters.map(ch => [ch.canonical_name, ch]))

  const nodes = data.characters.map((ch) => {
    const f = ch.faction?.trim() || '散修/独立'
    const isSel = selected.value === ch.canonical_name
    return {
      name: ch.canonical_name,
      category: factionIndex.get(f) ?? 0,
      symbolSize: ROLE_TIER_SIZE[ch.role_tier] ?? 22,
      value: ch.aliases?.length || 0,
      itemStyle: isSel
        ? { borderColor: c.textPrimary, borderWidth: 3 }
        : undefined,
      label: {
        show: true,
        position: 'bottom' as const,
        color: c.textPrimary,
        fontSize: ch.role_tier === 'protagonist' ? 13 : 11,
        fontWeight: ch.role_tier === 'protagonist' ? 600 : 400,
      },
    }
  })

  const links = data.relationships.map((rel) => {
    const edgeColor = relCategoryColor(rel.category)
    return {
      source: rel.source,
      target: rel.target,
      relation_tag: rel.relation_tag,
      rel_category: rel.category,
      evolution: rel.evolution,
      evidence_chapters: rel.evidence_chapters,
      lineStyle: {
        color: edgeColor,
        width: 1.8,
        curveness: 0.16,
        opacity: 0.85,
      },
      label: {
        show: showEdgeLabels.value,
        formatter: rel.relation_tag || '',
        fontSize: 10,
        color: c.textSecondary,
      },
    }
  })

  const forceOpt = computeGraphForce(nodes.length)

  chart.setOption({
    tooltip: {
      trigger: 'item',
      backgroundColor: c.layer,
      borderColor: c.strokeStrong,
      textStyle: { color: c.textPrimary, fontSize: 12 },
      formatter: (params: { dataType?: string; data?: Record<string, unknown> }) => {
        if (params.dataType === 'node') {
          const ch = charByName.get(String(params.data?.name || ''))
          if (!ch) return ''
          const lines = [
            `<div style="font-weight:600;margin-bottom:2px">${escHtml(ch.canonical_name)}</div>`,
            `<div>定位：${escHtml(roleTierLabel(ch.role_tier))}${ch.faction ? ` · 阵营：${escHtml(ch.faction)}` : ''}</div>`,
          ]
          if (ch.aliases?.length) {
            lines.push(`<div>别名/马甲：${escHtml(ch.aliases.join('、'))}</div>`)
          }
          if (ch.identity_summary) {
            lines.push(`<div style="max-width:260px;white-space:normal;margin-top:4px;opacity:0.85">${escHtml(ch.identity_summary)}</div>`)
          }
          return lines.join('')
        }
        if (params.dataType === 'edge' && params.data) {
          const d = params.data
          const evs = Array.isArray(d.evidence_chapters) && d.evidence_chapters.length
            ? `（依据章：${d.evidence_chapters.slice(0, 6).join(', ')}）`
            : ''
          const evo = d.evolution
            ? `<div style="max-width:260px;white-space:normal;margin-top:4px;opacity:0.85">${escHtml(String(d.evolution))}</div>`
            : ''
          return `<div style="font-weight:600">${escHtml(String(d.source))} ↔ ${escHtml(String(d.target))}</div>` +
            `<div>${escHtml(String(d.relation_tag || ''))} · ${escHtml(relCategoryLabel(String(d.rel_category || '')))} ${escHtml(evs)}</div>${evo}`
        }
        return ''
      },
    },
    legend: factionNames.length <= 10
      ? [{
          data: factionNames,
          top: 6,
          textStyle: { color: c.textSecondary, fontSize: 11 },
        }]
      : undefined,
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      center: ['50%', '52%'],
      zoom: forceOpt.zoom,
      scaleLimit: { min: 0.4, max: 3 },
      categories,
      data: nodes,
      links,
      force: {
        repulsion: forceOpt.repulsion,
        edgeLength: forceOpt.edgeLength,
        gravity: forceOpt.gravity,
      },
      emphasis: {
        focus: 'adjacency',
        lineStyle: { width: 3.5 },
      },
    }],
  }, true)
}

function fitGraphView() {
  chart?.resize()
  renderChart()
}

async function loadData() {
  const bid = bookId.value
  const seq = ++loadSeq
  selected.value = null
  card.value = null
  cardError.value = ''
  loadError.value = ''
  atlas.value = null
  characters.value = []

  if (!bid) {
    disposeChart()
    return
  }

  loading.value = true
  try {
    const [atlasRes, charsRes] = await Promise.allSettled([
      api.getAtlas(bid),
      api.getBookCharacters(bid),
    ])
    if (seq !== loadSeq || bid !== bookId.value) return

    if (atlasRes.status === 'fulfilled') {
      atlas.value = atlasRes.value
    } else {
      loadError.value = (atlasRes.reason as Error)?.message || '图谱加载失败'
    }

    if (charsRes.status === 'fulfilled') {
      characters.value = charsRes.value.characters || []
    }

    // 若尚未生成全书图谱但已有单章人物索引，自动切换到全部角色索引
    if (atlas.value && !atlas.value.exists && characters.value.length > 0 && !pendingCharId) {
      activeTab.value = 'index'
    }

    loading.value = false
    await nextTick()
    renderChart()

    if (pendingCharId) {
      const target = pendingCharId
      pendingCharId = ''
      await viewCard(target)
    }
  } catch (e) {
    if (seq !== loadSeq || bid !== bookId.value) return
    loadError.value = (e as Error).message || '加载失败'
    loading.value = false
  }
}

async function viewCard(name: string) {
  const bid = bookId.value
  if (!bid || !name) return
  const seq = ++cardSeq
  selected.value = name
  cardLoading.value = true
  cardError.value = ''
  card.value = null
  try {
    const res = await api.getCharacterCard(bid, name)
    if (seq !== cardSeq || bid !== bookId.value) return
    card.value = res.character
    selected.value = res.character.name || name
    const hasOverview = Boolean(
      res.character.arc_milestones?.length ||
      res.character.semantic_relationships?.length ||
      Object.keys(res.character.relationships || {}).length,
    )
    const hasTrajectory = Boolean(
      res.character.chapters?.length ||
      res.character.arcs?.length ||
      res.character.events?.length ||
      Object.keys(res.character.states || {}).length,
    )
    cardTab.value = !hasOverview && hasTrajectory ? 'trajectory' : 'overview'
    if (activeTab.value === 'graph') renderChart()
  } catch (e) {
    if (seq !== cardSeq || bid !== bookId.value) return
    cardError.value = (e as Error).message || '加载角色卡失败'
  } finally {
    if (seq === cardSeq) cardLoading.value = false
  }
}

function closeCard() {
  selected.value = null
  card.value = null
  cardError.value = ''
  if (activeTab.value === 'graph') renderChart()
}

function goSummary() {
  void router.push({ path: '/summary', query: bookId.value ? { book: bookId.value } : undefined })
}

watch(bookId, () => { void loadData() })
watch(showEdgeLabels, () => { renderChart() })
watch(activeTab, async (tab) => {
  if (tab === 'graph') {
    await nextTick()
    renderChart()
  } else {
    disposeChart()
  }
})

function onKeyDown(e: KeyboardEvent) {
  if (e.key === 'Escape' && selected.value) {
    closeCard()
  }
}

onMounted(() => {
  const qBook = typeof route?.query?.book === 'string' ? route.query.book : ''
  if (qBook && !bookId.value) {
    bookId.value = qBook
  }
  window.addEventListener('keydown', onKeyDown)
  unwatchTheme = onThemeChange(() => {
    if (atlas.value?.exists && activeTab.value === 'graph') renderChart()
  })
})

onBeforeUnmount(() => {
  unwatchTheme?.()
  window.removeEventListener('resize', handleResize)
  window.removeEventListener('keydown', onKeyDown)
  disposeChart()
})
</script>

<template>
  <div class="char-page space-y-5">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h2 class="section-title">人物谱系</h2>
      </div>
      <div class="flex items-center gap-2">
        <button
          @click="activeTab = 'graph'"
          class="glass-button btn-sm"
          :class="{ 'glass-button-primary': activeTab === 'graph' }"
        >核心关系网</button>
        <button
          @click="activeTab = 'index'"
          class="glass-button btn-sm"
          :class="{ 'glass-button-primary': activeTab === 'index' }"
        >全部角色索引 ({{ characters.length }})</button>
      </div>
    </div>

    <BookSelector v-model="bookId" />

    <p v-if="loadError" class="glass-tinted-red px-3 py-2 rounded text-sm">{{ loadError }}</p>

    <div v-if="!bookId" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-muted)">
      请选择书目
    </div>

    <div v-else-if="loading" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-secondary)">
      正在加载人物谱系...
    </div>

    <div v-else class="char-workspace" :class="{ 'has-detail': Boolean(selected) }">
      <!-- 左侧主视图：核心关系网 或 全部角色索引 -->
      <div class="char-main-pane">
        <!-- Tab 1: 核心关系网 -->
        <div v-if="activeTab === 'graph'" class="glass-card p-4 space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-2 pb-2" style="border-bottom: 1px solid var(--win-stroke)">
            <div class="text-sm font-semibold">
              语义关系网
              <span v-if="atlas?.exists" class="text-xs font-normal ml-2" style="color: var(--win-text-secondary)">
                {{ atlas.characters.length }} 核心人物 · {{ atlas.relationships.length }} 条关系边
              </span>
            </div>
            <div v-if="atlas?.exists" class="flex items-center gap-3 flex-wrap">
              <label class="flex items-center gap-1.5 text-xs cursor-pointer select-none" style="color: var(--win-text-secondary)">
                <input v-model="showEdgeLabels" type="checkbox" />
                显示关系标签
              </label>
              <button type="button" @click="fitGraphView" class="glass-button btn-sm">适配视图</button>
            </div>
          </div>

          <div v-if="!atlas?.exists" class="py-10 text-center space-y-3">
            <p class="text-sm" style="color: var(--win-text-secondary)">
              暂无全书图谱
            </p>
            <div class="flex justify-center gap-2 flex-wrap">
              <button @click="goSummary" class="glass-button glass-button-primary btn-sm">前往最终总结</button>
              <button v-if="characters.length" @click="activeTab = 'index'" class="glass-button btn-sm">
                浏览全章角色索引 ({{ characters.length }})
              </button>
            </div>
          </div>

          <div v-else-if="!atlas.characters.length" class="py-10 text-center text-sm" style="color: var(--win-text-muted)">
            当前图谱中暂无核心人物条目
          </div>

          <div v-else ref="chartContainer" class="atlas-graph-canvas"></div>
        </div>

        <!-- Tab 2: 全部角色索引（支持别名搜索） -->
        <div v-else class="glass-card p-4 space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-2 pb-2" style="border-bottom: 1px solid var(--win-stroke)">
            <div class="text-sm font-semibold">全部角色索引（共 {{ filteredCharacters.length }} 名）</div>
            <input
              v-model="searchQuery"
              type="text"
              class="glass-input text-sm"
              style="width: min(240px, 100%)"
              placeholder="搜索角色名 / 别名 / 阵营..."
            />
          </div>

          <div v-if="!filteredCharacters.length" class="py-8 text-center text-sm" style="color: var(--win-text-muted)">
            {{ characters.length ? '无匹配角色' : '暂无角色数据' }}
          </div>

          <div v-else class="grid grid-cols-1 md:grid-cols-2 gap-2.5 max-h-[620px] overflow-y-auto pr-1">
            <button
              v-for="ch in filteredCharacters"
              :key="ch.name"
              @click="viewCard(ch.name)"
              class="char-index-card text-left p-3 space-y-1"
              :class="{ 'is-selected': selected === ch.name }"
            >
              <div class="flex items-center justify-between gap-2">
                <span class="font-medium text-sm truncate" style="color: var(--win-text-primary)">{{ ch.name }}</span>
                <div class="flex items-center gap-1 shrink-0">
                  <span v-if="ch.in_atlas" class="glass-badge badge-blue" style="font-size: 10px">核心</span>
                  <span v-if="ch.faction" class="glass-badge" style="font-size: 10px">{{ ch.faction }}</span>
                </div>
              </div>
              <div v-if="ch.aliases?.length" class="text-xs truncate" style="color: var(--win-accent)">
                别名：{{ ch.aliases.join('、') }}
              </div>
              <div v-if="ch.identity_summary" class="text-xs line-clamp-2" style="color: var(--win-text-secondary)">
                {{ ch.identity_summary }}
              </div>
              <div class="text-xs" style="color: var(--win-text-muted)">
                首现第{{ ch.first_appearance ?? '?' }}章 · {{ ch.chapters_count }} 章 · {{ ch.total_events }} 事件
              </div>
            </button>
          </div>
        </div>
      </div>

      <!-- 窄窗右侧滑出抽屉遮罩 -->
      <div v-if="selected" class="char-drawer-backdrop" @click="closeCard" />

      <!-- 角色卡详情：宽窗右侧并排，窄窗右侧滑出抽屉 -->
      <aside v-if="selected" class="char-detail-pane space-y-3" role="complementary" aria-label="人物详情">
        <div v-if="cardLoading" class="glass-card p-6 text-center text-sm" style="color: var(--win-text-secondary)">
          正在加载「{{ selected }}」的角色档案...
        </div>

        <div v-else-if="cardError" class="glass-card p-4 space-y-2" style="color: var(--win-danger)">
          <div class="flex items-center justify-between">
            <span>{{ cardError }}</span>
            <button @click="closeCard" class="glass-button btn-sm">关闭</button>
          </div>
        </div>

        <div v-else-if="card" class="glass-card p-4 space-y-4 max-h-[720px] overflow-y-auto">
          <div class="flex items-start justify-between gap-2">
            <div class="space-y-1">
              <div class="flex items-center gap-2 flex-wrap">
                <h3 class="text-lg font-semibold" style="color: var(--win-text-primary)">{{ card.name }}</h3>
                <span v-if="card.role_tier" class="glass-badge badge-blue" style="font-size: 11px">
                  {{ roleTierLabel(card.role_tier) }}
                </span>
                <span v-if="card.faction" class="glass-badge" style="font-size: 11px">{{ card.faction }}</span>
              </div>
              <div v-if="card.aliases?.length" class="flex items-center gap-1.5 flex-wrap text-xs" style="color: var(--win-text-secondary)">
                <span>已归并别名/马甲：</span>
                <span v-for="al in card.aliases" :key="al" class="alias-pill">{{ al }}</span>
              </div>
            </div>
            <button @click="closeCard" class="glass-button btn-sm shrink-0">关闭</button>
          </div>

          <p v-if="card.identity_summary" class="text-xs p-2.5 rounded" style="background: var(--win-control-alt); color: var(--win-text-secondary); line-height: 1.55">
            {{ card.identity_summary }}
          </p>

          <!-- 基本统计（平铺小格，无嵌套阴影） -->
          <div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <div class="glass-inset char-stat">
              <div class="char-stat-label">首次出现</div>
              <div class="char-stat-value">第{{ card.first_appearance ?? '—' }}章</div>
            </div>
            <div class="glass-inset char-stat">
              <div class="char-stat-label">出场章节</div>
              <div class="char-stat-value">{{ card.chapters.length }} 章</div>
            </div>
            <div class="glass-inset char-stat">
              <div class="char-stat-label">参与事件</div>
              <div class="char-stat-value">{{ card.total_events }}</div>
            </div>
            <div class="glass-inset char-stat">
              <div class="char-stat-label">关联人物</div>
              <div class="char-stat-value">
                {{ Math.max(card.semantic_relationships?.length || 0, Object.keys(card.relationships || {}).length) }}
              </div>
            </div>
          </div>

          <!-- 角色详情 2-Tab（v-show 保活，分章轨迹消除内嵌 max-h 滚动容器） -->
          <div class="flex flex-wrap gap-2" role="tablist">
            <button
              type="button"
              role="tab"
              :aria-selected="cardTab === 'overview'"
              class="glass-pill text-xs"
              :class="{ 'is-active': cardTab === 'overview' }"
              @click="cardTab = 'overview'"
            >概览与关系</button>
            <button
              type="button"
              role="tab"
              :aria-selected="cardTab === 'trajectory'"
              class="glass-pill text-xs"
              :class="{ 'is-active': cardTab === 'trajectory' }"
              @click="cardTab = 'trajectory'"
            >分章轨迹</button>
          </div>

          <div v-show="cardTab === 'overview'" class="space-y-4">
            <!-- 关键弧光里程碑（语义图谱） -->
            <div v-if="card.arc_milestones?.length">
              <div class="block-title">关键成长里程碑</div>
              <div class="space-y-1.5">
                <div v-for="(ms, idx) in card.arc_milestones" :key="idx" class="arc-item">
                  <div v-if="ms.chapter_range" class="text-xs font-medium" style="color: var(--win-accent)">
                    第 {{ ms.chapter_range }} 章
                  </div>
                  <div class="text-xs mt-0.5" style="color: var(--win-text-primary)">{{ ms.milestone }}</div>
                </div>
              </div>
            </div>

            <!-- 语义关系 -->
            <div v-if="card.semantic_relationships?.length">
              <div class="block-title">核心语义关系（{{ card.semantic_relationships.length }}）</div>
              <div class="space-y-2">
                <button
                  v-for="(sr, idx) in card.semantic_relationships"
                  :key="idx"
                  @click="viewCard(sr.peer)"
                  class="rel-item w-full text-left"
                >
                  <div class="flex items-center justify-between gap-2">
                    <span class="font-medium text-sm" style="color: var(--win-text-primary)">
                      {{ sr.direction === 'out' ? '→ ' : '← ' }}{{ sr.peer }}
                    </span>
                    <span
                      class="text-xs px-2 py-0.5 rounded"
                      :style="{ color: relCategoryColor(sr.category), border: '1px solid ' + relCategoryColor(sr.category) }"
                    >
                      {{ sr.relation_tag }} · {{ relCategoryLabel(sr.category) }}
                    </span>
                  </div>
                  <div v-if="sr.evolution" class="text-xs mt-1" style="color: var(--win-text-secondary)">
                    {{ sr.evolution }}
                  </div>
                  <div v-if="sr.evidence_chapters?.length" class="text-xs mt-1" style="color: var(--win-text-muted)">
                    关键章节：{{ sr.evidence_chapters.slice(0, 8).join(', ') }}
                  </div>
                </button>
              </div>
            </div>

            <!-- 同章事件共现 -->
            <div v-if="topRelationships(card.relationships).length">
              <div class="block-title">同章事件共现 Top {{ topRelationships(card.relationships).length }}</div>
              <div class="space-y-1.5">
                <button
                  v-for="r in topRelationships(card.relationships)"
                  :key="r.name"
                  @click="viewCard(r.name)"
                  class="rel-item w-full text-left"
                >
                  <div class="flex items-baseline justify-between">
                    <span class="font-medium text-xs" style="color: var(--win-text-primary)">{{ r.name }}</span>
                    <span class="text-xs" style="color: var(--win-text-muted)">
                      共现 {{ r.rel.event_count }} 次 · 首遇第{{ r.rel.first_encounter }}章
                    </span>
                  </div>
                </button>
              </div>
            </div>

            <div
              v-if="!card.arc_milestones?.length && !card.semantic_relationships?.length && !topRelationships(card.relationships).length"
              class="text-xs py-4 text-center"
              style="color: var(--win-text-muted)"
            >
              暂无核心里程碑或关系数据，可切换至「分章轨迹」查看详细记录
            </div>
          </div>

          <div v-show="cardTab === 'trajectory'" class="space-y-4">
            <!-- 出场章节 -->
            <div v-if="card.chapters.length">
              <div class="block-title">出场章节</div>
              <div class="text-xs" style="color: var(--win-text-primary)">{{ formatChapters(card.chapters) }}</div>
            </div>

            <!-- 分章人物弧光 -->
            <div v-if="card.arcs.length">
              <div class="block-title">分章人物弧光（{{ card.arcs.length }}）</div>
              <div class="space-y-2">
                <div v-for="arc in card.arcs" :key="arc.chapter" class="arc-item">
                  <div class="font-medium text-xs" style="color: var(--win-text-primary)">第{{ arc.chapter }}章</div>
                  <div class="text-xs mt-1" style="color: var(--win-text-secondary)">
                    <span class="arc-key">表面行为</span>{{ arc.surface_action }}
                  </div>
                  <div class="text-xs mt-0.5" style="color: var(--win-text-secondary)">
                    <span class="arc-key">内在动机</span>{{ arc.inner_motivation }}
                  </div>
                  <div class="text-xs mt-0.5" style="color: var(--win-text-secondary)">
                    <span class="arc-key">变化幅度</span>{{ arc.change_delta }}
                    <span class="arc-key ml-2">驱动</span>{{ arc.driver }}
                  </div>
                </div>
              </div>
            </div>

            <!-- 主要事件 -->
            <div v-if="card.events.length">
              <div class="block-title">主要事件（{{ card.events.length }}）</div>
              <div class="space-y-1">
                <div v-for="(ev, i) in card.events" :key="i" class="event-item">
                  <span class="text-xs" style="color: var(--win-accent)">第{{ ev.chapter }}章</span>
                  <span class="text-xs ml-2" style="color: var(--win-text-primary)">{{ ev.event }}</span>
                  <span v-if="ev.function" class="text-xs ml-2" style="color: var(--win-text-muted)">功能：{{ ev.function }}</span>
                </div>
              </div>
            </div>

            <!-- 状态演变 -->
            <div v-if="sortedStates(card.states).length">
              <div class="block-title">状态演变</div>
              <div class="space-y-0.5">
                <div v-for="s in sortedStates(card.states)" :key="s.chapter" class="text-xs" style="color: var(--win-text-secondary)">
                  <span style="color: var(--win-text-muted)">第{{ s.chapter }}章：</span>{{ s.state }}
                </div>
              </div>
            </div>

            <div
              v-if="!card.chapters.length && !card.arcs.length && !card.events.length && !sortedStates(card.states).length"
              class="text-xs py-4 text-center"
              style="color: var(--win-text-muted)"
            >
              暂无分章轨迹数据
            </div>
          </div>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.char-page {
  container-type: inline-size;
  min-width: 0;
}
.char-workspace {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.char-main-pane {
  min-width: 0;
}
.char-drawer-backdrop {
  position: fixed;
  inset: 0;
  z-index: 45;
  background: rgba(0, 0, 0, 0.28);
}
:global(html.dark) .char-drawer-backdrop {
  background: rgba(0, 0, 0, 0.5);
}
.char-detail-pane {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: min(440px, 92vw);
  z-index: 50;
  background: var(--win-layer);
  border-left: 1px solid var(--win-stroke-strong);
  box-shadow: var(--win-shadow-dialog);
  overflow-y: auto;
  padding: 16px;
}
.char-detail-pane > .glass-card {
  border: none;
  box-shadow: none;
  padding: 0;
  max-height: none;
  overflow: visible;
  background: transparent;
}
@container (min-width: 1020px) {
  .char-workspace.has-detail {
    grid-template-columns: minmax(0, 1.35fr) minmax(350px, 0.95fr);
  }
  .char-drawer-backdrop {
    display: none;
  }
  .char-detail-pane {
    position: static;
    width: auto;
    z-index: auto;
    background: transparent;
    border-left: none;
    box-shadow: none;
    overflow: visible;
    padding: 0;
  }
  .char-detail-pane > .glass-card {
    background: var(--win-layer);
    border: 1px solid var(--win-stroke);
    box-shadow: var(--win-shadow-card);
    padding: 16px;
    max-height: 720px;
    overflow-y: auto;
  }
}

.atlas-graph-canvas {
  width: 100%;
  height: clamp(380px, 56vh, 540px);
  min-height: 360px;
}
.btn-sm {
  padding: 0 12px;
  white-space: nowrap;
}
.char-stat {
  padding: 8px 10px;
}
.char-stat-label {
  font-size: 11px;
  color: var(--win-text-muted);
}
.char-stat-value {
  font-size: 15px;
  font-weight: 600;
  color: var(--win-text-primary);
  margin-top: 2px;
  font-variant-numeric: tabular-nums;
}
.char-index-card {
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
  border-radius: var(--win-radius-container);
  transition: background var(--win-duration-fast) var(--win-ease),
              border-color var(--win-duration-fast) var(--win-ease);
}
.char-index-card:hover {
  background: var(--win-control-hover);
  border-color: var(--win-stroke-strong);
}
.char-index-card.is-selected {
  background: var(--win-accent-soft);
  border-color: var(--win-accent);
}
.alias-pill {
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
  border-radius: var(--win-radius-pill);
  padding: 1px 8px;
  font-size: 11px;
  color: var(--win-text-primary);
}
.block-title {
  font-size: var(--win-font-caption);
  font-weight: 600;
  color: var(--win-text-secondary);
  padding-bottom: 6px;
  margin-bottom: 8px;
  border-bottom: 1px solid var(--win-stroke);
}
.arc-item, .rel-item {
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
  border-left: 3px solid var(--win-accent);
  border-radius: var(--win-radius-container);
  padding: 10px 12px;
  transition:
    background var(--win-duration-fast) var(--win-ease),
    border-color var(--win-duration-fast) var(--win-ease),
    transform var(--win-duration-fast) var(--win-ease);
}
.arc-item:hover, .rel-item:hover {
  background: var(--win-control-hover);
  border-color: var(--win-stroke-strong);
  border-left-color: var(--win-accent-hover);
  transform: translateX(2px);
}
.event-item {
  border-left: 3px solid var(--win-warning);
  padding-left: 8px;
}
.arc-key {
  display: inline-block;
  min-width: 56px;
  color: var(--win-text-muted);
}
</style>
