<script setup lang="ts">
import { ref, watch, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent, DataZoomComponent, LegendComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'
import BookSelector from '../components/BookSelector.vue'
import { api, type GraphNode, type GraphEdge } from '../api/client'
import { useRouter } from 'vue-router'
import { escapeHtml } from '../utils/markdown'

echarts.use([GraphChart, TooltipComponent, DataZoomComponent, LegendComponent, SVGRenderer])

const router = useRouter()
const bookId = ref('')
const nodes = ref<GraphNode[]>([])
const edges = ref<GraphEdge[]>([])
const selected = ref<string | null>(null)
const totalCharacters = ref(0)
const totalEdges = ref(0)
const chapterRange = ref<{ min: number; max: number }>({ min: 1, max: 1 })
const chapterRangeSelected = ref<[number, number]>([1, 1])
const maxNodes = ref(200)
const minEdgeWeight = ref(1)
const loading = ref(false)
const chartContainer = ref<HTMLDivElement>()
const initialized = ref(false)
let chart: echarts.ECharts | null = null

// 主题色（与 main.css --win-* 一致，硬编码 hex）
const COLORS = {
  accent: '#0067C0',
  success: '#0F7B0F',
  warning: '#9D5D00',
  strokeStrong: '#D0D0D0',
  layer: '#F3F3F3',
  textPrimary: 'rgba(0,0,0,1)',
  textSecondary: 'rgba(0,0,0,0.6)',
  textDisabled: 'rgba(0,0,0,0.36)',
}

async function loadData() {
  const bid = bookId.value
  if (!bid) {
    // 切回占位项时清掉上一本书遗留的加载态，否则一直转圈到选中新书
    loading.value = false
    return
  }
  loading.value = true
  try {
    const [cs, ce] = chapterRangeSelected.value
    const res = await api.getGraph(bid, {
      chapter_start: cs,
      chapter_end: ce,
      max_nodes: maxNodes.value,
      min_edge_weight: minEdgeWeight.value,
    })
    if (bid !== bookId.value) return   // 已切书：丢弃过期响应
    nodes.value = res.nodes
    edges.value = res.edges
    totalCharacters.value = res.total_characters
    totalEdges.value = res.total_edges
    chapterRange.value = res.chapter_range
    // 首次加载把 slider 拉到全书范围（用独立 flag 哨兵，避免与用户输入的 [1,1] 冲突）
    if (!initialized.value) {
      if (res.chapter_range.min != null && res.chapter_range.max != null) {
        chapterRangeSelected.value = [res.chapter_range.min, res.chapter_range.max]
      }
      initialized.value = true
    }
    // 等 v-else 分支渲染、chartContainer ref 挂载后再渲染图表
    await nextTick()
    renderChart()
  } catch (e) {
    if (bid !== bookId.value) return
    nodes.value = []
    edges.value = []
    console.error('加载关系图失败:', e)
  } finally {
    if (bid === bookId.value) loading.value = false
  }
}

function bucketByCount(count: number, maxCount: number): number {
  if (maxCount === 0) return 3
  const ratio = count / maxCount
  if (ratio > 0.7) return 0   // 核心
  if (ratio > 0.4) return 1   // 主要
  if (ratio > 0.15) return 2  // 次要
  return 3                     // 边缘
}

function renderChart() {
  if (nodes.value.length === 0) return
  initChart()
  if (!chart) return
  const maxCount = Math.max(...nodes.value.map(n => n.event_count || 1), 1)
  const maxWeight = Math.max(...edges.value.map(e => e.weight || 1), 1)

  const option = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: COLORS.layer,
      borderColor: COLORS.strokeStrong,
      textStyle: { color: COLORS.textPrimary, fontSize: 12 },
      formatter: (params: any) => {
        if (params.dataType === 'node') {
          const n = nodes.value.find(x => x.id === params.data.id)
          const related = edges.value.filter(e => e.source === params.data.id || e.target === params.data.id).length
          return `<div style="font-family:Microsoft YaHei">` +
            `<b>${escapeHtml(String(params.data.name))}</b><br/>` +
            `<span style="color:${COLORS.textSecondary}">出场章数: ${n?.event_count ?? 0}</span><br/>` +
            `<span style="color:${COLORS.textSecondary}">关联角色: ${related}</span>` +
            `</div>`
        } else if (params.dataType === 'edge') {
          const src = nodes.value.find(x => x.id === params.data.source)
          const tgt = nodes.value.find(x => x.id === params.data.target)
          return `<div style="font-family:Microsoft YaHei">` +
            `<b>${escapeHtml(String(src?.name ?? params.data.source))}</b> — <b>${escapeHtml(String(tgt?.name ?? params.data.target))}</b><br/>` +
            `<span style="color:${COLORS.textSecondary}">共现 ${params.data.value} 章</span>` +
            `</div>`
        }
        return ''
      },
    },
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      label: {
        show: true,
        position: 'right',
        fontSize: 11,
        color: COLORS.textPrimary,
        fontFamily: 'Microsoft YaHei',
      },
      labelLayout: { hideOverlap: true },
      emphasis: {
        focus: 'adjacency',
        lineStyle: { width: 3, opacity: 1 },
        label: { fontSize: 13, fontWeight: 'bold' as const },
      },
      force: {
        repulsion: 120,
        edgeLength: [30, 120],
        gravity: 0.1,
        layoutAnimation: true,
      },
      data: nodes.value.map(n => ({
        id: n.id,
        name: n.name,
        value: n.event_count,
        symbolSize: 12 + (n.event_count / maxCount) * 36,
        category: bucketByCount(n.event_count, maxCount),
        label: { show: true },
      })),
      links: edges.value.map(e => ({
        source: e.source,
        target: e.target,
        value: e.weight,
        lineStyle: {
          width: 0.5 + (e.weight / maxWeight) * 2.5,
          opacity: 0.15 + (e.weight / maxWeight) * 0.45,
          color: COLORS.strokeStrong,
          curveness: 0.1,
        },
      })),
      categories: [
        { name: '核心', itemStyle: { color: COLORS.accent } },
        { name: '主要', itemStyle: { color: COLORS.success } },
        { name: '次要', itemStyle: { color: COLORS.warning } },
        { name: '边缘', itemStyle: { color: COLORS.strokeStrong } },
      ],
      lineStyle: { color: COLORS.strokeStrong, curveness: 0.1 },
    }],
  }

  chart.setOption(option, true)  // true = notMerge，全量替换
}

function handleResize() {
  chart?.resize()
}

function initChart() {
  const el = chartContainer.value
  if (!el) return
  // 容器在 v-if/v-else 分支内切换时 DOM 会被销毁重建：旧实例绑定的节点已脱离
  // 文档，setOption 写进去也不会显示（表现为图表永久空白）。因此每次先校验
  // 实例持有的 DOM 是否仍是当前容器，不是则销毁重建。
  if (chart && chart.getDom() !== el) {
    chart.dispose()
    chart = null
  }
  if (!chart) {
    chart = echarts.init(el, null, { renderer: 'svg' })
    chart.on('click', (params: any) => {
      if (params.dataType === 'node') {
        selected.value = selected.value === params.data.id ? null : params.data.id
      }
    })
    window.addEventListener('resize', handleResize)
  }
}

onMounted(() => {
  // chartContainer 此时在 v-else 内未挂载（首屏 !bookId），init 推迟到 renderChart
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  chart = null
})

watch(bookId, () => {
  selected.value = null
  initialized.value = false  // 重置初始化哨兵，下一次 loadData 会把 slider 拉到新书全章
  chapterRangeSelected.value = [1, 1]
  loadData()
})

// 参数变化防抖
let debounceTimer: ReturnType<typeof setTimeout>
watch([chapterRangeSelected, maxNodes, minEdgeWeight], () => {
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(loadData, 400)
})

const selectedRelated = computed(() => {
  if (!selected.value) return []
  const related: { name: string; weight: number }[] = []
  for (const edge of edges.value) {
    if (edge.source === selected.value) {
      const n = nodes.value.find(x => x.id === edge.target)
      related.push({ name: n?.name || edge.target, weight: edge.weight })
    } else if (edge.target === selected.value) {
      const n = nodes.value.find(x => x.id === edge.source)
      related.push({ name: n?.name || edge.source, weight: edge.weight })
    }
  }
  return related.sort((a, b) => b.weight - a.weight)
})

const selectedNode = computed(() => {
  if (!selected.value) return null
  return nodes.value.find(n => n.id === selected.value) || null
})

function goToCharacterCard() {
  if (selected.value) {
    router.push({ name: 'characters', query: { id: selected.value } })
  }
}

function resetChapterRange() {
  chapterRangeSelected.value = [chapterRange.value.min, chapterRange.value.max]
}
</script>

<template>
  <div class="space-y-6">
    <h2 class="section-title">角色关系图</h2>
    <BookSelector v-model="bookId" />

    <div v-if="!bookId" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-disabled)">
      请选择书目
    </div>
    <div v-else-if="loading && nodes.length === 0" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-secondary)">
      加载中...
    </div>
    <div v-else-if="nodes.length === 0" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-disabled)">
      暂无数据
    </div>
    <div v-else class="space-y-4">
      <!-- 工具栏 -->
      <div class="glass-card p-4 flex flex-wrap items-center gap-4 text-sm">
        <div>
          <span style="color: var(--win-text-secondary)">角色:</span>
          <span class="font-medium ml-1">{{ nodes.length }}</span>
          <span v-if="totalCharacters > nodes.length" style="color: var(--win-text-disabled)">
            / {{ totalCharacters }}（已截断 Top-{{ maxNodes }}）
          </span>
        </div>
        <div>
          <span style="color: var(--win-text-secondary)">关系:</span>
          <span class="font-medium ml-1">{{ edges.length }}</span>
        </div>
        <div class="flex items-center gap-2">
          <span style="color: var(--win-text-secondary)">章节范围:</span>
          <span class="font-medium">{{ chapterRangeSelected[0] }} - {{ chapterRangeSelected[1] }}</span>
        </div>
        <div class="flex items-center gap-2">
          <label style="color: var(--win-text-secondary)">Top-N:</label>
          <select v-model.number="maxNodes" class="glass-input" style="width: 80px">
            <option :value="50">50</option>
            <option :value="100">100</option>
            <option :value="200">200</option>
            <option :value="300">300</option>
            <option :value="500">500</option>
          </select>
        </div>
        <div class="flex items-center gap-2">
          <label style="color: var(--win-text-secondary)">边权阈值:</label>
          <select v-model.number="minEdgeWeight" class="glass-input" style="width: 80px">
            <option :value="1">≥1</option>
            <option :value="2">≥2</option>
            <option :value="3">≥3</option>
            <option :value="5">≥5</option>
          </select>
        </div>
      </div>

      <!-- 章节范围输入 -->
      <div class="glass-card p-4">
        <div class="text-xs mb-2" style="color: var(--win-text-secondary)">
          章节范围: {{ chapterRange.min }} - {{ chapterRange.max }}
        </div>
        <div class="flex items-center gap-3">
          <input
            type="number"
            v-model.number="chapterRangeSelected[0]"
            :min="chapterRange.min"
            :max="chapterRangeSelected[1]"
            class="glass-input"
            style="width: 80px"
          />
          <span style="color: var(--win-text-disabled)">—</span>
          <input
            type="number"
            v-model.number="chapterRangeSelected[1]"
            :min="chapterRangeSelected[0]"
            :max="chapterRange.max"
            class="glass-input"
            style="width: 80px"
          />
          <button class="glass-button btn-sm" @click="resetChapterRange">
            全书
          </button>
        </div>
      </div>

      <!-- 图 + 右侧面板 -->
      <div class="flex gap-4">
        <div class="flex-1 glass-card p-2">
          <div ref="chartContainer" style="width: 100%; height: 560px;"></div>
        </div>
        <div v-if="selected && selectedNode" class="w-64 shrink-0">
          <div class="glass-card p-4 space-y-2">
            <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">
              {{ selectedNode.name }}
            </h3>
            <div class="text-sm" style="color: var(--win-text-secondary)">
              出场章数: {{ selectedNode.event_count }}
            </div>
            <div class="text-sm font-medium pt-2" style="color: var(--win-text-primary)">
              关联角色 ({{ selectedRelated.length }}):
            </div>
            <div class="space-y-1 max-h-80 overflow-y-auto">
              <div v-for="(rel, idx) in selectedRelated" :key="idx" class="rel-row">
                <span style="color: var(--win-text-primary)">{{ rel.name }}</span>
                <span style="color: var(--win-text-disabled)">权重 {{ rel.weight }}</span>
              </div>
              <div v-if="!selectedRelated.length" class="text-xs" style="color: var(--win-text-disabled)">
                无关联
              </div>
            </div>
            <button class="glass-button btn-sm w-full mt-2" @click="goToCharacterCard">
              查看角色卡片
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.rel-row {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
  padding: 4px 10px;
  border-radius: var(--win-radius-control);
}
</style>
