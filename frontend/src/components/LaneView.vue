<script setup lang="ts">
/**
 * LaneView 并发车道组件（方案 A：2–3 列微卡网格 + 空闲自动收起）
 *
 * 状态机：block_start → 找空 lane 占位 → block_done（chapter 匹配）→ 保留结果直到被新块复用
 * 关键设计：
 * 1. 槽位物理固定（Slot Anchoring）：块进入时锁定槽位序号，完成时就地保留状态，避免卡片跳动窜位。
 * 2. 平滑宽度推进：配合父级 1 秒批量提交，进度条用 0.95s 线性过渡衔接。
 * 3. Win11 中性克制：状态以左侧 3px 细色条与极简小点表达，不再大面积染色或使用表情符号。
 */

import { ref, computed, onMounted, onUnmounted, watchEffect } from 'vue'
import CountUp from './CountUp.vue'

interface Lane {
  blockId: number
  range: string
  startedAt: number
  status: 'running' | 'done' | 'failed'
  lastTokenAt: number
  outputTokens: number
  isThinking?: boolean
}

interface SlotAssignment {
  blockId: number
  source: 'active' | 'finished'
}

const props = withDefaults(defineProps<{
  concurrency?: number
  activeBlocks?: Map<number, { range: string; startedAt: number }>
  finishedBlocks?: Map<number, { ok: boolean; range: string }>
  tokenByBlock?: Map<number, { outputTokens: number; lastTokenAt: number; isThinking?: boolean }>
  stallWarnSec?: number
  tickMs?: number
}>(), {
  concurrency: 4,
  activeBlocks: () => new Map(),
  finishedBlocks: () => new Map(),
  tokenByBlock: () => new Map(),
  stallWarnSec: 480,
  tickMs: 1000,
})

const emit = defineEmits<{
  (e: 'select-block', blockId: number): void
}>()

const now = ref(Date.now())
let tickTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  tickTimer = setInterval(() => { now.value = Date.now() }, props.tickMs)
})
onUnmounted(() => {
  if (tickTimer !== null) clearInterval(tickTimer)
})

const slotAssignments = ref<Array<SlotAssignment | null>>([])

function reconcileSlots() {
  const conc = props.concurrency
  const current = [...slotAssignments.value]
  if (current.length < conc) {
    while (current.length < conc) current.push(null)
  } else if (current.length > conc) {
    current.length = conc
  }

  if (props.activeBlocks.size === 0 && props.finishedBlocks.size === 0) {
    slotAssignments.value = new Array(conc).fill(null)
    return
  }

  const activeEntries = Array.from(props.activeBlocks.entries())
    .sort((a, b) => a[1].startedAt - b[1].startedAt)
  const activeIds = new Set(activeEntries.map(([id]) => id))

  for (let i = 0; i < current.length; i++) {
    const slot = current[i]
    if (slot && slot.source === 'active' && !activeIds.has(slot.blockId)) {
      if (props.finishedBlocks.has(slot.blockId)) {
        current[i] = { blockId: slot.blockId, source: 'finished' }
      } else {
        current[i] = null
      }
    }
  }

  for (const [blockId] of activeEntries) {
    const existingIndex = current.findIndex(s => s && s.source === 'active' && s.blockId === blockId)
    if (existingIndex !== -1) continue
    let targetIndex = current.findIndex(s => s === null)
    if (targetIndex === -1) {
      targetIndex = current.findIndex(s => s && s.source === 'finished')
    }
    if (targetIndex !== -1) {
      current[targetIndex] = { blockId, source: 'active' }
    }
  }

  const finishedEntries = Array.from(props.finishedBlocks.entries())
    .sort((a, b) => b[0] - a[0])

  for (const [blockId] of finishedEntries) {
    const alreadyFinishedSlot = current.some(s => s && s.source === 'finished' && s.blockId === blockId)
    if (alreadyFinishedSlot) continue
    const emptyIndex = current.findIndex(s => s === null)
    if (emptyIndex === -1) break
    current[emptyIndex] = { blockId, source: 'finished' }
  }

  slotAssignments.value = current
}

reconcileSlots()
watchEffect(() => {
  reconcileSlots()
})

const isIdle = computed(() => props.activeBlocks.size === 0 && props.finishedBlocks.size === 0)

const lanes = computed<Lane[]>(() => {
  const conc = props.concurrency
  const assignments = slotAssignments.value
  const result: Lane[] = []

  for (let i = 0; i < conc; i++) {
    const slot = assignments[i]
    if (!slot || slot.blockId === -1) {
      result.push({
        blockId: -1,
        range: '',
        startedAt: 0,
        status: 'running',
        lastTokenAt: 0,
        outputTokens: 0,
      })
      continue
    }

    if (slot.source === 'active' && props.activeBlocks.has(slot.blockId)) {
      const info = props.activeBlocks.get(slot.blockId)!
      const tk = props.tokenByBlock.get(slot.blockId)
      result.push({
        blockId: slot.blockId,
        range: info.range,
        startedAt: info.startedAt,
        status: 'running',
        lastTokenAt: tk?.lastTokenAt ?? info.startedAt,
        outputTokens: tk?.outputTokens ?? 0,
        isThinking: Boolean(tk?.isThinking),
      })
    } else if (props.finishedBlocks.has(slot.blockId)) {
      const info = props.finishedBlocks.get(slot.blockId)!
      result.push({
        blockId: slot.blockId,
        range: info.range,
        startedAt: 0,
        status: info.ok ? 'done' : 'failed',
        lastTokenAt: 0,
        outputTokens: 0,
        isThinking: false,
      })
    } else {
      result.push({
        blockId: -1,
        range: '',
        startedAt: 0,
        status: 'running',
        lastTokenAt: 0,
        outputTokens: 0,
        isThinking: false,
      })
    }
  }

  return result
})

function elapsedStr(startedAt: number): string {
  if (startedAt === 0) return ''
  const sec = Math.max(0, Math.floor((now.value - startedAt) / 1000))
  if (sec < 60) return `${sec}s`
  return `${Math.floor(sec / 60)}m${sec % 60}s`
}

function progressFrac(lane: Lane): number {
  if (lane.startedAt === 0) return 0
  if (lane.outputTokens > 0) {
    if (lane.isThinking) {
      return Math.min(0.05 + (lane.outputTokens / 2000) * 0.40, 0.45)
    }
    const est = 0.05 + Math.min(lane.outputTokens / 1600, 1) * 0.93
    return Math.min(est, 0.98)
  }
  const sec = Math.max(0, (now.value - lane.startedAt) / 1000)
  return Math.min(sec / props.stallWarnSec, 1)
}

function isStalled(lane: Lane): boolean {
  if (lane.status !== 'running' || lane.startedAt === 0) return false
  const elapsedSec = (now.value - lane.startedAt) / 1000
  if (elapsedSec < props.stallWarnSec) return false
  return (now.value - lane.lastTokenAt) > 5000
}

function isStreaming(lane: Lane): boolean {
  if (lane.status !== 'running' || lane.startedAt === 0) return false
  if (lane.outputTokens <= 0) return false
  return (now.value - lane.lastTokenAt) < 2000
}

function isClickable(lane: Lane): boolean {
  return lane.blockId > 0 && lane.blockId < 900000
}

function handleLaneClick(lane: Lane) {
  if (isClickable(lane)) {
    emit('select-block', lane.blockId)
  }
}

function statusClass(lane: Lane): string {
  if (lane.blockId === -1) return 'lane-empty'
  if (lane.status === 'done') return 'lane-done'
  if (lane.status === 'failed') return 'lane-failed'
  if (isStalled(lane)) return 'lane-stalled'
  if (lane.isThinking) return 'lane-running lane-thinking'
  return 'lane-running'
}

const stalledCount = computed(() => lanes.value.filter(isStalled).length)
</script>

<template>
  <div class="lane-view">
    <div class="lane-head">
      <span class="lane-title">并发车道</span>
      <span class="lane-sub">
        {{ activeBlocks.size }} / {{ concurrency }} 运行中
        <template v-if="stalledCount > 0">
          · <span class="lane-warn">⚠ {{ stalledCount }} 卡死</span>
        </template>
      </span>
    </div>

    <div v-if="isIdle" class="lane-idle">
      当前无运行中任务
    </div>

    <div v-else class="lane-list">
      <div
        v-for="(lane, i) in lanes"
        :key="i"
        :class="[
          'lane-row',
          statusClass(lane),
          {
            'lane-clickable': isClickable(lane),
            'lane-streaming': isStreaming(lane),
          }
        ]"
        :title="isClickable(lane) ? '点击查看对应章节详情' : undefined"
        @click="handleLaneClick(lane)"
      >
        <div class="lane-top">
          <div class="lane-id-wrap">
            <span class="lane-slot">#{{ i + 1 }}</span>
            <span class="lane-range">
              <template v-if="lane.blockId === -1">— 空槽 —</template>
              <template v-else>{{ lane.range || `block ${lane.blockId}` }}</template>
            </span>
          </div>
          <div class="lane-state">
            <template v-if="lane.blockId === -1">—</template>
            <template v-else-if="lane.status === 'running' && isStalled(lane)">卡死</template>
            <template v-else-if="lane.status === 'running' && lane.isThinking">
              <span class="lane-streaming-dot is-thinking" />思考
            </template>
            <template v-else-if="lane.status === 'running'">
              <span v-if="isStreaming(lane)" class="lane-streaming-dot" />
              <span v-else class="lane-idle-dot" />{{ lane.outputTokens > 0 ? '输出' : '等待首字' }}
            </template>
            <template v-else-if="lane.status === 'done'">完成</template>
            <template v-else>失败</template>
          </div>
        </div>

        <div v-if="lane.blockId !== -1" class="lane-bottom">
          <span class="lane-elapsed">{{ elapsedStr(lane.startedAt) || '—' }}</span>
          <span
            v-if="lane.status === 'running'"
            :class="['lane-tok', { 'is-thinking': lane.isThinking }]"
          >
            <template v-if="lane.outputTokens > 0">{{ lane.isThinking ? '思考 ' : '' }}<CountUp :key="lane.blockId" :value="lane.outputTokens" :duration="950" easing="linear" /> tok</template>
          </span>
          <span v-else class="lane-tok">
            {{ lane.status === 'done' ? '已落盘' : '需关注' }}
          </span>
        </div>

        <div
          v-if="lane.status === 'running' && lane.startedAt > 0"
          class="lane-progress"
          title="按等待时间或已生成 Token 估算活动程度"
        >
          <div
            class="lane-progress-bar"
            :style="{ width: (progressFrac(lane) * 100) + '%' }"
            :class="{ 'lane-progress-warn': isStalled(lane) }"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.lane-view {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.lane-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.lane-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--win-text-primary);
}
.lane-sub {
  font-size: 11px;
  color: var(--win-text-muted);
  font-variant-numeric: tabular-nums;
}
.lane-warn {
  color: var(--win-danger);
  font-weight: 500;
}
.lane-idle {
  padding: 12px;
  border-radius: var(--win-radius-control);
  border: 1px dashed var(--win-stroke);
  background: var(--win-control-alt);
  color: var(--win-text-muted);
  font-size: 12px;
  text-align: center;
}
.lane-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 8px;
}
.lane-row {
  position: relative;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 4px;
  min-height: 48px;
  padding: 8px 10px 7px;
  border-radius: var(--win-radius-control);
  font-size: 12px;
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
  border-left: 3px solid var(--win-stroke-strong);
  overflow: hidden;
  transition: background var(--win-duration-fast) var(--win-ease),
              border-color var(--win-duration-fast) var(--win-ease);
}
.lane-row.lane-clickable {
  cursor: pointer;
  user-select: none;
}
.lane-row.lane-clickable:hover {
  background: var(--win-control-hover);
  border-color: var(--win-stroke-strong);
}
.lane-row.lane-running {
  background: var(--win-layer);
  border-left-color: var(--win-accent);
}
.lane-row.lane-thinking {
  background: var(--win-layer);
  border-left-color: var(--win-warning);
}
.lane-row.lane-stalled {
  background: var(--win-danger-bg);
  border-color: var(--win-danger);
  border-left-color: var(--win-danger);
  animation: lane-pulse 1.8s ease-in-out infinite;
}
@keyframes lane-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.78; }
}
.lane-row.lane-done {
  background: var(--win-control-alt);
  border-left-color: var(--win-success);
}
.lane-row.lane-failed {
  background: var(--win-danger-bg);
  border-left-color: var(--win-danger);
}
.lane-row.lane-empty {
  opacity: 0.45;
  border-style: dashed;
  border-left-width: 1px;
  border-left-color: var(--win-stroke);
  justify-content: center;
}
.lane-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  min-width: 0;
}
.lane-id-wrap {
  display: flex;
  align-items: baseline;
  gap: 5px;
  min-width: 0;
  flex: 1;
}
.lane-slot {
  font-size: 10px;
  color: var(--win-text-muted);
  font-family: ui-monospace, monospace;
  flex-shrink: 0;
}
.lane-range {
  color: var(--win-text-primary);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.lane-state {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-weight: 500;
  color: var(--win-text-secondary);
  flex-shrink: 0;
}
.lane-row.lane-running .lane-state {
  color: var(--win-accent);
}
.lane-row.lane-thinking .lane-state {
  color: var(--win-warning);
}
.lane-row.lane-done .lane-state {
  color: var(--win-success);
}
.lane-row.lane-failed .lane-state,
.lane-row.lane-stalled .lane-state {
  color: var(--win-danger);
}
.lane-bottom {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
  font-family: ui-monospace, monospace;
  font-variant-numeric: tabular-nums;
}
.lane-elapsed {
  font-size: 11px;
  color: var(--win-text-muted);
}
.lane-tok {
  font-size: 11px;
  color: var(--win-text-secondary);
  font-weight: 500;
}
.lane-tok.is-thinking {
  color: var(--win-warning);
}
.lane-idle-dot,
.lane-streaming-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--win-accent);
}
.lane-idle-dot {
  opacity: 0.45;
}
.lane-streaming-dot {
  animation: dot-pulse 1.1s infinite alternate ease-in-out;
}
.lane-streaming-dot.is-thinking {
  background: var(--win-warning);
}
@keyframes dot-pulse {
  0% { transform: scale(0.85); opacity: 0.55; }
  100% { transform: scale(1.15); opacity: 1; }
}
.lane-progress {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 2px;
  background: var(--win-stroke);
  overflow: hidden;
}
.lane-progress-bar {
  height: 100%;
  background: var(--win-accent);
  transition: width 0.95s linear, background 0.3s;
}
.lane-row.lane-thinking .lane-progress-bar {
  background: var(--win-warning);
}
.lane-progress-bar.lane-progress-warn {
  background: var(--win-danger);
}
</style>
