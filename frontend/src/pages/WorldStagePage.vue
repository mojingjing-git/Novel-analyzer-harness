<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts/core'
import { TreeChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'
import BookSelector from '../components/BookSelector.vue'
import { api, type StoryAtlasDto, type AtlasTreeNode } from '../api/client'
import { themeColors, onThemeChange } from '../utils/theme'

echarts.use([TreeChart, TooltipComponent, SVGRenderer])

const NODE_TYPE_LABELS: Record<string, string> = {
  world: '世界/位面',
  region: '疆域/大区',
  faction: '势力/宗门',
  landmark: '地标/秘境',
  virtual: '全书总览',
}

const route = useRoute()
const router = useRouter()

const bookId = ref('')
const loading = ref(false)
const loadError = ref('')
const atlas = ref<StoryAtlasDto | null>(null)
const selectedWorldIdx = ref(-1) // -1 表示全部世界（多根时启用虚拟根节点）

const treeContainer = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
let treeObserver: ResizeObserver | null = null
let unwatchTheme: (() => void) | null = null
let loadSeq = 0

function escHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

/** 前端兜底防环与深度截断（<= 10 层），防止畸形树结构导致 ECharts 栈溢出 */
function sanitizeTreeNodes(nodes: AtlasTreeNode[], depth = 1, pathSeen = new Set<string>()): AtlasTreeNode[] {
  if (depth > 10 || !Array.isArray(nodes)) return []
  const out: AtlasTreeNode[] = []
  for (const n of nodes) {
    if (!n || typeof n !== 'object') continue
    const name = String(n.name || '').trim()
    if (!name || pathSeen.has(name)) continue
    const nextSeen = new Set(pathSeen)
    nextSeen.add(name)
    out.push({
      name,
      node_type: n.node_type || 'region',
      summary: String(n.summary || '').trim(),
      children: sanitizeTreeNodes(n.children || [], depth + 1, nextSeen),
    })
  }
  return out
}

const safeWorldTree = computed(() => sanitizeTreeNodes(atlas.value?.world_tree || []))

const stages = computed(() => {
  const list = atlas.value?.stages ? [...atlas.value.stages] : []
  return list.sort((a, b) =>
    (a.start_chapter || 0) - (b.start_chapter || 0) || (a.stage_no || 0) - (b.stage_no || 0)
  )
})

function nodeTypeColor(nodeType?: string): string {
  const c = themeColors()
  switch (nodeType) {
    case 'world': return c.accent
    case 'region': return c.success
    case 'faction': return c.warning
    case 'landmark': return c.info
    default: return c.textSecondary
  }
}

interface EChartsTreeNode {
  name: string
  node_type: string
  summary: string
  itemStyle: { color: string; borderColor: string }
  children: EChartsTreeNode[]
}

function toEChartsNode(node: AtlasTreeNode): EChartsTreeNode {
  const col = nodeTypeColor(node.node_type)
  return {
    name: node.name,
    node_type: node.node_type,
    summary: node.summary,
    itemStyle: { color: col, borderColor: col },
    children: (node.children || []).map(toEChartsNode),
  }
}

function handleResize() {
  chart?.resize()
}

function disposeChart() {
  treeObserver?.disconnect()
  treeObserver = null
  chart?.dispose()
  chart = null
}

function initTreeChart() {
  const el = treeContainer.value
  if (!el) return
  if (chart && chart.getDom() !== el) {
    disposeChart()
  }
  if (!chart) {
    chart = echarts.init(el, null, { renderer: 'svg' })
    window.addEventListener('resize', handleResize)
    if (typeof ResizeObserver !== 'undefined') {
      treeObserver = new ResizeObserver(() => handleResize())
      treeObserver.observe(el)
    }
  }
}

function renderTree() {
  const roots = safeWorldTree.value
  if (!atlas.value?.exists || !roots.length) {
    disposeChart()
    return
  }
  initTreeChart()
  if (!chart) return

  const c = themeColors()
  let rootData: EChartsTreeNode

  if (roots.length === 1) {
    rootData = toEChartsNode(roots[0])
  } else if (selectedWorldIdx.value >= 0 && selectedWorldIdx.value < roots.length) {
    rootData = toEChartsNode(roots[selectedWorldIdx.value])
  } else {
    rootData = {
      name: bookId.value || '全书世界体系',
      node_type: 'virtual',
      summary: `共 ${roots.length} 个独立界域/大世界分支（可点击顶部标签单独聚焦）`,
      itemStyle: { color: c.textSecondary, borderColor: c.textSecondary },
      children: roots.map(toEChartsNode),
    }
  }

  chart.setOption({
    tooltip: {
      trigger: 'item',
      triggerOn: 'mousemove',
      backgroundColor: c.layer,
      borderColor: c.strokeStrong,
      textStyle: { color: c.textPrimary, fontSize: 12 },
      formatter: (params: { data?: Partial<EChartsTreeNode> }) => {
        const d = params.data
        if (!d?.name) return ''
        const typeLabel = NODE_TYPE_LABELS[d.node_type || 'region'] || d.node_type || '节点'
        const summary = d.summary
          ? `<div style="max-width:260px;white-space:normal;margin-top:4px;opacity:0.85">${escHtml(d.summary)}</div>`
          : ''
        return `<div style="font-weight:600">${escHtml(d.name)}</div><div style="opacity:0.75">${escHtml(typeLabel)}</div>${summary}`
      },
    },
    series: [{
      type: 'tree',
      data: [rootData],
      top: '4%',
      left: '10%',
      bottom: '4%',
      right: '20%',
      orient: 'LR',
      roam: true,
      expandAndCollapse: true,
      initialTreeDepth: 3,
      edgeShape: 'curve',
      symbolSize: 10,
      lineStyle: {
        color: c.strokeStrong,
        width: 1.5,
      },
      label: {
        position: 'left',
        verticalAlign: 'middle',
        align: 'right',
        fontSize: 12,
        color: c.textPrimary,
      },
      leaves: {
        label: {
          position: 'right',
          verticalAlign: 'middle',
          align: 'left',
          fontSize: 11,
          color: c.textSecondary,
        },
      },
      emphasis: {
        focus: 'descendant',
      },
      animationDuration: 350,
      animationDurationUpdate: 450,
    }],
  }, true)
}

function fitTreeView() {
  chart?.resize()
  renderTree()
}

async function loadAtlas() {
  const bid = bookId.value
  const seq = ++loadSeq
  loadError.value = ''
  atlas.value = null
  selectedWorldIdx.value = -1

  if (!bid) {
    disposeChart()
    return
  }

  loading.value = true
  try {
    const res = await api.getAtlas(bid)
    if (seq !== loadSeq || bid !== bookId.value) return
    atlas.value = res
    loading.value = false
    await nextTick()
    renderTree()
  } catch (e) {
    if (seq !== loadSeq || bid !== bookId.value) return
    loadError.value = (e as Error).message || '加载世界舞台图谱失败'
    loading.value = false
  }
}

function selectWorld(idx: number) {
  selectedWorldIdx.value = idx
  renderTree()
}

function goSummary() {
  void router.push({ path: '/summary', query: bookId.value ? { book: bookId.value } : undefined })
}

watch(bookId, () => { void loadAtlas() })

onMounted(() => {
  const qBook = typeof route?.query?.book === 'string' ? route.query.book : ''
  if (qBook && !bookId.value) {
    bookId.value = qBook
  }
  unwatchTheme = onThemeChange(() => {
    if (atlas.value?.exists && safeWorldTree.value.length) renderTree()
  })
})

onBeforeUnmount(() => {
  unwatchTheme?.()
  window.removeEventListener('resize', handleResize)
  disposeChart()
})
</script>

<template>
  <div class="world-page space-y-6">
    <div>
      <h2 class="section-title">世界舞台</h2>
    </div>

    <BookSelector v-model="bookId" />

    <p v-if="loadError" class="glass-tinted-red px-3 py-2 rounded text-sm">{{ loadError }}</p>

    <div v-if="!bookId" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-muted)">
      请选择书目
    </div>

    <div v-else-if="loading" class="glass-card p-8 text-center text-sm" style="color: var(--win-text-secondary)">
      正在加载世界舞台图谱...
    </div>

    <div v-else-if="atlas && !atlas.exists" class="glass-card p-8 text-center space-y-3">
      <p class="text-sm" style="color: var(--win-text-secondary)">
        暂无全书图谱
      </p>
      <button @click="goSummary" class="glass-button glass-button-primary">前往最终总结</button>
    </div>

    <template v-else-if="atlas">
      <!-- 区块一：主线换地图阶段时间轴（按章节升序紧凑扫读） -->
      <section class="glass-card p-4 space-y-3">
        <div class="flex items-center justify-between pb-2" style="border-bottom: 1px solid var(--win-stroke)">
          <h3 class="font-semibold text-sm">主线阶段演进（换地图流）</h3>
          <span class="text-xs" style="color: var(--win-text-muted)">共 {{ stages.length }} 个阶段</span>
        </div>

        <div v-if="!stages.length" class="py-6 text-center text-sm" style="color: var(--win-text-muted)">
          当前图谱暂无阶段流数据
        </div>

        <div v-else class="stage-timeline">
          <div v-for="st in stages" :key="st.stage_no" class="stage-row">
            <!-- 章节轨 -->
            <div class="stage-rail">
              <div class="stage-chapter">第 {{ st.start_chapter }}–{{ st.end_chapter }} 章</div>
              <span class="glass-badge badge-blue stage-no-badge">阶段 {{ st.stage_no }}</span>
            </div>

            <!-- 时间轴竖线与节点圆点 -->
            <div class="stage-node-col" aria-hidden="true">
              <span class="stage-dot" />
              <span class="stage-line" />
            </div>

            <!-- 阶段主体（平铺底面，无嵌套阴影） -->
            <div class="glass-inset stage-body">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="font-semibold text-sm" style="color: var(--win-text-primary)">{{ st.title }}</div>
                <div class="flex flex-wrap gap-1.5 text-xs">
                  <span v-if="st.world_realm" class="stage-tag">{{ st.world_realm }}</span>
                  <span v-if="st.primary_location" class="stage-tag is-primary">{{ st.primary_location }}</span>
                </div>
              </div>

              <p v-if="st.core_conflict" class="text-xs" style="color: var(--win-text-primary); line-height: 1.55">
                {{ st.core_conflict }}
              </p>

              <div v-if="st.key_sub_locations?.length || st.active_factions?.length" class="flex flex-wrap gap-x-4 gap-y-1 text-xs" style="color: var(--win-text-secondary)">
                <span v-if="st.key_sub_locations?.length">
                  <span style="color: var(--win-text-muted)">关键地点：</span>{{ st.key_sub_locations.join(' · ') }}
                </span>
                <span v-if="st.active_factions?.length">
                  <span style="color: var(--win-text-muted)">活跃势力：</span>{{ st.active_factions.join(' · ') }}
                </span>
              </div>

              <div v-if="st.map_switch_reason" class="text-xs pt-1.5" style="border-top: 1px dashed var(--win-stroke); color: var(--win-text-secondary)">
                <span style="color: var(--win-accent); font-weight: 500">→ 转场动因：</span>{{ st.map_switch_reason }}
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- 区块二：世界/势力层级树 -->
      <section class="glass-card p-4 space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2 pb-2" style="border-bottom: 1px solid var(--win-stroke)">
          <h3 class="font-semibold text-sm">世界与势力层级树</h3>
          <div class="flex flex-wrap items-center gap-2">
            <!-- 多世界根切换 Pill（ECharts TreeChart 单根约束适配） -->
            <div v-if="safeWorldTree.length > 1" class="flex flex-wrap gap-1.5">
              <button
                @click="selectWorld(-1)"
                class="world-pill"
                :class="{ 'is-active': selectedWorldIdx === -1 }"
              >全部世界 ({{ safeWorldTree.length }})</button>
              <button
                v-for="(w, idx) in safeWorldTree"
                :key="w.name"
                @click="selectWorld(idx)"
                class="world-pill"
                :class="{ 'is-active': selectedWorldIdx === idx }"
              >{{ w.name }}</button>
            </div>
            <button v-if="safeWorldTree.length > 0" type="button" @click="fitTreeView" class="glass-button btn-sm">适配视图</button>
          </div>
        </div>

        <div v-if="!safeWorldTree.length" class="py-8 text-center text-sm" style="color: var(--win-text-muted)">
          当前图谱暂无世界层级树数据
        </div>

        <div v-else ref="treeContainer" class="world-tree-canvas"></div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.world-page {
  container-type: inline-size;
  min-width: 0;
}
.btn-sm {
  padding: 0 12px;
  white-space: nowrap;
}
.stage-timeline {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.stage-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 6px;
}
.stage-rail {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.stage-chapter {
  font-size: 12px;
  font-weight: 600;
  color: var(--win-text-secondary);
  font-variant-numeric: tabular-nums;
}
.stage-no-badge {
  font-size: 11px;
}
.stage-node-col {
  display: none;
}
.stage-body {
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  transition: background var(--win-duration-fast) var(--win-ease),
              border-color var(--win-duration-fast) var(--win-ease);
}
.stage-body:hover {
  background: var(--win-control-hover);
  border-color: var(--win-stroke-strong);
}

@container (min-width: 560px) {
  .stage-timeline {
    gap: 0;
  }
  .stage-row {
    grid-template-columns: 112px 16px minmax(0, 1fr);
    gap: 12px;
    align-items: stretch;
  }
  .stage-rail {
    flex-direction: column;
    align-items: flex-end;
    justify-content: flex-start;
    gap: 4px;
    padding-top: 8px;
    text-align: right;
  }
  .stage-node-col {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding-top: 12px;
  }
  .stage-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--win-accent);
    box-shadow: 0 0 0 3px var(--win-accent-soft);
    flex-shrink: 0;
  }
  .stage-line {
    width: 2px;
    flex: 1;
    margin-top: 4px;
    background: var(--win-stroke);
  }
  .stage-row:last-child .stage-line {
    display: none;
  }
  .stage-body {
    margin-bottom: 10px;
  }
  .stage-row:last-child .stage-body {
    margin-bottom: 0;
  }
}

.stage-tag {
  background: var(--win-layer);
  border: 1px solid var(--win-stroke);
  border-radius: var(--win-radius-control);
  padding: 1px 7px;
  color: var(--win-text-secondary);
}
.stage-tag.is-primary {
  background: var(--win-accent-soft);
  border-color: var(--win-accent);
  color: var(--win-accent);
}
.world-pill {
  padding: 3px 10px;
  font-size: 12px;
  border-radius: var(--win-radius-pill);
  border: 1px solid var(--win-stroke);
  background: var(--win-control-alt);
  color: var(--win-text-secondary);
  cursor: pointer;
  transition: background var(--win-duration-fast) var(--win-ease),
              border-color var(--win-duration-fast) var(--win-ease),
              color var(--win-duration-fast) var(--win-ease);
}
.world-pill:hover {
  background: var(--win-control-hover);
  color: var(--win-text-primary);
}
.world-pill.is-active {
  background: var(--win-accent-soft);
  border-color: var(--win-accent);
  color: var(--win-accent);
  font-weight: 600;
}
.world-tree-canvas {
  width: 100%;
  height: clamp(380px, 52vh, 520px);
  min-height: 360px;
}
</style>
