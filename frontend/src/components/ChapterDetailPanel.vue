<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import ChapterValue from './ChapterValue.vue'
import { api } from '../api/client'

// 用户指定章节后，若 1 分钟内没有任何"活动"（滚动/滚轮/触摸/按键/点击），
// 自动回到"不指定"状态（即永远显示最新章节）。
const IDLE_MS = 60_000

const props = withDefaults(defineProps<{
  bookId: string
  chapter: number
  analysisRunning?: boolean
  pauseIdle?: boolean
}>(), {
  analysisRunning: false,
  pauseIdle: false,
})
const emit = defineEmits<{
  'update:chapter': [value: number]
  'rerun-chapter': [chapter: number]
  'open-rerun': [chapter: number]
}>()

const manualChapter = computed<number>({
  get: () => props.chapter,
  set: (v) => emit('update:chapter', v),
})

const latestChapter = ref(0)
const result = ref<Record<string, unknown> | null>(null)
const loading = ref(false)
const error = ref('')

const isManual = computed(() => manualChapter.value > 0)
const effectiveChapter = computed(() =>
  manualChapter.value > 0 ? manualChapter.value : latestChapter.value
)
const blockSize = computed<number>(() => Number(result.value?.block_size) || 0)

const SECTION_LABELS: Record<string, string> = {
  core_events: '核心事件',
  character_arcs: '人物弧光',
  foreshadowing: '伏笔',
  anchor_stats: '锚点统计',
  plot_holes: '剧情漏洞',
  locations: '地点',
  spatial_relationships: '空间关系',
  cross_block: '跨块衔接',
  updated_knowledge: '知识更新',
  long_context_insights: '长上下文洞察',
}

const HIDDEN_SECTION_KEYS = new Set([
  'chapter_number',
  'block_size',
  'source_chapters',
  'raw_response',
  'anchor_stats',
])

const sections = computed(() => {
  if (!result.value) return []
  return Object.entries(result.value)
    .filter(([k]) => !HIDDEN_SECTION_KEYS.has(k))
    .map(([k, v]) => ({ key: k, label: SECTION_LABELS[k] || k, value: v }))
})

let idleTimer: ReturnType<typeof setTimeout> | null = null
let latestPoll: ReturnType<typeof setInterval> | null = null

async function loadLatest() {
  if (!props.bookId) return
  try {
    const res = await api.getLatestChapter(props.bookId)
    latestChapter.value = res.latest
  } catch (e) {
    console.error('获取最新章节失败:', e)
  }
}

// 请求序号：丢弃过期响应（快速改章号时先发的请求后返回会覆盖成错误章）
let requestSeq = 0

async function loadChapter() {
  // 早退也必须失效在飞请求——否则切书瞬间旧响应因 seq 未变而合法写回
  requestSeq++
  if (!props.bookId || effectiveChapter.value <= 0) {
    loading.value = false
    error.value = ''
    result.value = null
    return
  }
  const seq = requestSeq
  const ch = effectiveChapter.value
  loading.value = true
  error.value = ''
  try {
    const res = await api.getChapterResult(props.bookId, ch)
    if (seq !== requestSeq) return // 已有更新的请求，丢弃本次响应
    result.value = res.data
  } catch (e) {
    if (seq !== requestSeq) return
    error.value = (e as Error).message || '加载失败'
    result.value = null
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

// 活动事件 → 重置 1 分钟计时器（仅在手动指定模式下生效；重跑弹窗/运行期间暂停倒计时）
function resetIdle() {
  if (manualChapter.value <= 0 || props.pauseIdle) {
    if (idleTimer) {
      clearTimeout(idleTimer)
      idleTimer = null
    }
    return
  }
  if (idleTimer) clearTimeout(idleTimer)
  idleTimer = setTimeout(() => {
    manualChapter.value = 0 // 回到不指定状态
  }, IDLE_MS)
}
function onActivity() {
  resetIdle()
}

watch(
  () => props.pauseIdle,
  (paused) => {
    if (paused) {
      if (idleTimer) {
        clearTimeout(idleTimer)
        idleTimer = null
      }
    } else if (manualChapter.value > 0) {
      resetIdle()
    }
  }
)

defineExpose({
  reload: async () => {
    await loadLatest()
    await loadChapter()
  },
})

// 规范化 chapter：空 / 负数 / 小数 → 0 或向下取整；并管理空闲计时器
watch(
  () => props.chapter,
  (v) => {
    const n = Math.max(0, Math.floor(Number(v) || 0))
    if (n !== Number(v)) {
      emit('update:chapter', n)
      return
    }
    if (n > 0) resetIdle()
    else if (idleTimer) {
      clearTimeout(idleTimer)
      idleTimer = null
    }
  }
)

watch(
  () => props.bookId,
  async () => {
    latestChapter.value = 0
    result.value = null
    if (props.bookId) {
      await loadLatest()
      await loadChapter()
    }
  }
)

watch(effectiveChapter, loadChapter)

onMounted(() => {
  window.addEventListener('scroll', onActivity, { passive: true })
  window.addEventListener('wheel', onActivity, { passive: true })
  window.addEventListener('touchmove', onActivity, { passive: true })
  window.addEventListener('keydown', onActivity)
  window.addEventListener('click', onActivity)
  if (props.bookId) {
    void loadLatest()
    void loadChapter()
  }
  // 每 5s 刷新最新章节（运行中也能跟上），仅在未指定章节时
  latestPoll = setInterval(() => {
    if (manualChapter.value <= 0) loadLatest()
  }, 5000)
})

onUnmounted(() => {
  window.removeEventListener('scroll', onActivity)
  window.removeEventListener('wheel', onActivity)
  window.removeEventListener('touchmove', onActivity)
  window.removeEventListener('keydown', onActivity)
  window.removeEventListener('click', onActivity)
  if (idleTimer) clearTimeout(idleTimer)
  if (latestPoll) clearInterval(latestPoll)
})
</script>

<template>
  <div class="chapter-detail">
    <div class="cd-report glass-card">
      <div v-if="!bookId" class="cd-placeholder">请选择书目</div>
      <div v-else-if="loading" class="cd-placeholder">加载中...</div>
      <div v-else-if="error" class="cd-placeholder cd-error">{{ error }}</div>
      <div v-else-if="!result" class="cd-placeholder">暂无章节数据</div>
      <template v-else>
        <div class="cd-report-head">
          <span class="cd-chapter">第 {{ effectiveChapter }} 章</span>
          <span v-if="blockSize > 1" class="cd-blocksize">· 共 {{ blockSize }} 个文本块</span>
          <div class="cd-head-actions">
            <button
              v-if="bookId && effectiveChapter > 0"
              type="button"
              class="glass-button cd-rerun-btn"
              :disabled="analysisRunning"
              @click="emit('rerun-chapter', effectiveChapter)"
            >
              重跑本章
            </button>
            <button
              v-if="bookId"
              type="button"
              class="glass-button cd-rerun-btn"
              :disabled="analysisRunning"
              @click="emit('open-rerun', effectiveChapter)"
            >
              区间重跑…
            </button>
            <span class="cd-mode" :class="{ 'is-manual': isManual }">
              {{ isManual ? '指定章节' : '自动 · 最新章节' }}
            </span>
          </div>
        </div>
        <section v-for="sec in sections" :key="sec.key" class="cd-section">
          <h4 class="cd-section-title">{{ sec.label }}</h4>
          <ChapterValue :value="sec.value" />
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
.chapter-detail {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}
/* 液态玻璃框：直接复用全局 .glass-card（材质/边缘环/光泽带统一由其提供），这里只管布局 */
.cd-report {
  flex: 1;
  min-height: 0;
  overflow: auto;
  /* 16px 16px 对齐 4 倍数网格（原 18px 不合规） */
  padding: 16px;
  color: var(--win-text-primary);
}
.cd-placeholder {
  color: var(--win-text-secondary);
  font-size: 13px;
  padding: 24px 0;
  text-align: center;
}
.cd-error {
  color: var(--win-danger);
}
.cd-report-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding-bottom: 12px;
  margin-bottom: 14px;
  border-bottom: 1px solid var(--win-stroke);
}
/* Win11 Subtitle 层级：18px / 600，与下方段落标题 13px 形成层次 */
.cd-chapter {
  font-size: 18px;
  font-weight: 600;
  color: var(--win-text-primary);
}
.cd-blocksize {
  font-size: 12px;
  color: var(--win-text-secondary);
}
.cd-head-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.cd-rerun-btn {
  height: 26px;
  padding: 0 10px;
  font-size: 12px;
  white-space: nowrap;
}
.cd-mode {
  font-size: 12px;
  /* 4px / 12px 对齐 4 倍数网格 */
  padding: 4px 12px;
  border-radius: 4px;
  background: var(--win-info-bg);
  color: var(--win-info);
  white-space: nowrap;
}
.cd-mode.is-manual {
  background: var(--win-danger-bg);
  color: var(--win-danger);
}
.cd-section {
  margin-bottom: 18px;
}
.cd-section:last-child {
  margin-bottom: 0;
}
.cd-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--win-text-primary);
  margin: 0 0 8px;
  padding-left: 9px;
  border-left: 3px solid var(--win-accent);
}
</style>
