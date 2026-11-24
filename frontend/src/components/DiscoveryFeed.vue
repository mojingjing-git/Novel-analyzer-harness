<script setup lang="ts">
/**
 * DiscoveryFeed 发现流组件
 *
 * 消费 discovery 消息，append-only，ring buffer 200 条
 * 自动滚底；hover 暂停滚动
 * 新伏笔/新人物使用 Win11 左侧 3px 系统蓝强调条标注
 */

import { ref, computed, onMounted, nextTick, watch } from 'vue'

interface DiscoveryItem {
  id: number
  ts: number
  events: number
  foreshadows: string[]
  characters: string[]
  unresolved: string[]
  highlighted: boolean
}

const props = withDefaults(defineProps<{
  /** 当前发现流条目（外部 append，本组件只读不维护） */
  items?: DiscoveryItem[]
  /** ring buffer 上限 */
  maxItems?: number
}>(), {
  items: () => [],
  maxItems: 200,
})

const listEl = ref<HTMLElement | null>(null)
const paused = ref(false)

const displayItems = computed<DiscoveryItem[]>(() => {
  const arr = props.items
  return arr.length > props.maxItems ? arr.slice(arr.length - props.maxItems) : arr
})

function timeStr(ts: number): string {
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}

function onMouseEnter() { paused.value = true }
function onMouseLeave() { paused.value = false }

watch(() => props.items.length, async () => {
  if (paused.value) return
  await nextTick()
  if (listEl.value) {
    listEl.value.scrollTop = listEl.value.scrollHeight
  }
})

onMounted(() => {
  nextTick(() => {
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  })
})
</script>

<template>
  <div class="discovery-feed">
    <div class="df-head">
      <span class="df-title">新发现</span>
      <span class="df-sub">{{ displayItems.length }} 条{{ paused ? '（暂停滚动）' : '' }}</span>
    </div>
    <div
      ref="listEl"
      class="df-list"
      @mouseenter="onMouseEnter"
      @mouseleave="onMouseLeave"
    >
      <div v-if="displayItems.length === 0" class="df-empty">尚无新发现</div>
      <div
        v-for="item in displayItems"
        :key="item.id"
        :class="['df-item', item.highlighted ? 'df-highlight' : '']"
      >
        <span class="df-time">{{ timeStr(item.ts) }}</span>
        <div class="df-body">
          <div class="df-summary">
            完成块 · {{ item.events }} 个核心事件
          </div>
          <div v-if="item.foreshadows.length" class="df-foreshadows">
            <span class="df-label">伏笔</span>
            <span
              v-for="(f, i) in item.foreshadows"
              :key="i"
              class="df-tag df-tag-foreshadow"
            >{{ f }}</span>
          </div>
          <div v-if="item.characters.length" class="df-characters">
            <span class="df-label">新人物</span>
            <span
              v-for="(c, i) in item.characters"
              :key="i"
              class="df-tag df-tag-character"
            >{{ c }}</span>
          </div>
          <div v-if="item.unresolved.length" class="df-unresolved">
            <span class="df-label">悬念</span>
            <span
              v-for="(u, i) in item.unresolved"
              :key="i"
              class="df-tag df-tag-unresolved"
            >{{ u }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.discovery-feed {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 0;
}
.df-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.df-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--win-text-primary);
}
.df-sub {
  font-size: 11px;
  color: var(--win-text-muted);
}
.df-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 280px;
  overflow-y: auto;
  padding: 6px;
  border-radius: var(--win-radius-control);
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
}
.df-empty {
  text-align: center;
  color: var(--win-text-muted);
  font-size: 12px;
  padding: 20px 0;
}
.df-item {
  display: grid;
  grid-template-columns: 54px 1fr;
  gap: 6px;
  padding: 6px 8px;
  border-radius: var(--win-radius-control);
  font-size: 12px;
  background: var(--win-layer);
  border: 1px solid transparent;
}
.df-item.df-highlight {
  border-color: var(--win-stroke);
  border-left: 3px solid var(--win-accent);
}
.df-time {
  font-family: ui-monospace, monospace;
  font-size: 10px;
  color: var(--win-text-muted);
  align-self: start;
  padding-top: 2px;
  font-variant-numeric: tabular-nums;
}
.df-body {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.df-summary {
  color: var(--win-text-primary);
  font-weight: 500;
}
.df-label {
  color: var(--win-text-muted);
  font-size: 10px;
  margin-right: 4px;
}
.df-foreshadows, .df-characters, .df-unresolved {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: baseline;
}
.df-tag {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
  color: var(--win-text-secondary);
}
.df-tag-foreshadow {
  color: var(--win-accent);
  background: var(--win-accent-soft);
  border-color: transparent;
}
.df-tag-character {
  color: var(--win-text-primary);
  background: var(--win-control-alt);
  border-color: var(--win-stroke);
  font-weight: 500;
}
.df-tag-unresolved {
  color: var(--win-text-secondary);
}
</style>
