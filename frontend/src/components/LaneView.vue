<script setup lang="ts">
/**
 * LaneView 车道组件
 *
 * 状态机：block_start → 找空 lane 占位（记 ts）→ block_done（chapter 匹配）→ 释放
 * 关键设计：
 * 1. 槽位物理固定（Slot Anchoring）：块进入时锁定槽位序号，完成时就地保留状态，
 *    避免并发块完成时后续车道上下跳变窜动。
 * 2. 动态吐字与进度感知：outputTokens > 0 时按生成进度估算并展现微脉冲 shimmer，
 *    未吐字时双模回退至基于 stallWarnSec 的时间归一化。
 * 3. 点击穿透联动：点击有效块触发 select-block 事件。
 */

import { ref, computed, onMounted, onUnmounted, watchEffect } from 'vue'

interface Lane {
  blockId: number
  range: string
  startedAt: number
  status: 'running' | 'done' | 'failed'
  lastTokenAt: number  // 最后一次 token_delta 时间戳（用于卡死检测）
  outputTokens: number  // 该车道累计 output tokens
  isThinking?: boolean  // 是否处于思考阶段（MiniMax/DeepSeek R1/Claude 思考模型）
}

interface SlotAssignment {
  blockId: number
  source: 'active' | 'finished'
}

const props = withDefaults(defineProps<{
  /** 起始 lane 数（来自 status.concurrency） */
  concurrency?: number
  /** 当前活跃块（start 时加，done/failed 时移除） */
  activeBlocks?: Map<number, { range: string; startedAt: number }>
  /** 已完成块（done=true:绿，done=false:红） */
  finishedBlocks?: Map<number, { ok: boolean; range: string }>
  /** 块级 token 累计（key: blockId, value: {outputTokens, lastTokenAt, isThinking?}） */
  tokenByBlock?: Map<number, { outputTokens: number; lastTokenAt: number; isThinking?: boolean }>
  /** 卡死预警阈值（秒），默认 480 */
  stallWarnSec?: number
  /** tick 频率（毫秒），默认 1000 */
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

// 物理槽位数组：稳定映射每个槽位的 blockId 与来源
const slotAssignments = ref<Array<SlotAssignment | null>>([])

function reconcileSlots() {
  const conc = props.concurrency
  const current = [...slotAssignments.value]
  if (current.length < conc) {
    while (current.length < conc) current.push(null)
  } else if (current.length > conc) {
    current.length = conc
  }

  // 若活跃和完成皆为空，清空全部槽位（例如重置/切换）
  if (props.activeBlocks.size === 0 && props.finishedBlocks.size === 0) {
    slotAssignments.value = new Array(conc).fill(null)
    return
  }

  const activeEntries = Array.from(props.activeBlocks.entries())
    .sort((a, b) => a[1].startedAt - b[1].startedAt)
  const activeIds = new Set(activeEntries.map(([id]) => id))

  // 1. 原活跃槽位若是移出活跃列表：检查是否转为 finished，就地保留槽位
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

  // 2. 为新增的活跃块分配槽位（保持旧槽位不动）
  for (const [blockId] of activeEntries) {
    const existingIndex = current.findIndex(s => s && s.source === 'active' && s.blockId === blockId)
    if (existingIndex !== -1) {
      continue
    }
    // 优先填补空槽
    let targetIndex = current.findIndex(s => s === null)
    // 若无空槽，复用最前的一个 finished 槽位
    if (targetIndex === -1) {
      targetIndex = current.findIndex(s => s && s.source === 'finished')
    }
    if (targetIndex !== -1) {
      current[targetIndex] = { blockId, source: 'active' }
    }
  }

  // 3. 空槽用 finishedBlocks 补齐（按 blockId 降序，冷启动兼容 Test 2 / Test 4）
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

// 确保在 setup 同步执行一次，并在依赖更新时自动重新对齐
reconcileSlots()
watchEffect(() => {
  reconcileSlots()
})

// 渲染 lanes：根据物理槽位映射生成具体的 Lane 数据
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
  const sec = Math.floor((now.value - startedAt) / 1000)
  if (sec < 60) return `${sec}s`
  return `${Math.floor(sec / 60)}m${sec % 60}s`
}

// 进度 0-1：双模估算。outputTokens > 0 时估算产出；为 0 时严格按 stallWarnSec 归一化（Test 6 保底）
function progressFrac(lane: Lane): number {
  if (lane.startedAt === 0) return 0
  if (lane.outputTokens > 0) {
    if (lane.isThinking) {
      // 思考阶段进度估算（按 2000 思考 token 封顶 45%）
      return Math.min(0.05 + (lane.outputTokens / 2000) * 0.40, 0.45)
    }
    const est = 0.05 + Math.min(lane.outputTokens / 1600, 1) * 0.93
    return Math.min(est, 0.98)
  }
  const sec = (now.value - lane.startedAt) / 1000
  return Math.min(sec / props.stallWarnSec, 1)
}

// 卡死判定：超过 stallWarnSec 且无 token_delta 更新
function isStalled(lane: Lane): boolean {
  if (lane.status !== 'running' || lane.startedAt === 0) return false
  const elapsedSec = (now.value - lane.startedAt) / 1000
  if (elapsedSec < props.stallWarnSec) return false
  // 卡死 = 已用超阈值 + 至少 5 秒无 token_delta（防止最后一刻还在动）
  return (now.value - lane.lastTokenAt) > 5000
}

// 流式吐字微脉冲感知：近 1500ms 内有 token 更新
function isStreaming(lane: Lane): boolean {
  if (lane.status !== 'running' || lane.startedAt === 0) return false
  if (lane.outputTokens <= 0) return false
  return (now.value - lane.lastTokenAt) < 1500
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
    <div class="lane-list">
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
        <div class="lane-slot">#{{ i + 1 }}</div>
        <div class="lane-range">
          <template v-if="lane.blockId === -1">— 空槽 —</template>
          <template v-else>{{ lane.range || `block ${lane.blockId}` }}</template>
        </div>
        <div class="lane-elapsed">
          {{ elapsedStr(lane.startedAt) }}
          <span
            v-if="lane.status === 'running' && lane.outputTokens > 0"
            :class="['lane-tok', { 'is-thinking': lane.isThinking }]"
          >
            <span
              v-if="isStreaming(lane)"
              :class="['lane-streaming-dot', { 'is-thinking': lane.isThinking }]"
            />
            {{ lane.isThinking ? '思考 ' : '' }}{{ lane.outputTokens }}tok
          </span>
        </div>
        <div class="lane-state">
          <template v-if="lane.blockId === -1">—</template>
          <template v-else-if="lane.status === 'running' && isStalled(lane)">⚠</template>
          <template v-else-if="lane.status === 'running' && lane.isThinking">🧠</template>
          <template v-else-if="lane.status === 'running'">⏳</template>
          <template v-else-if="lane.status === 'done'">✓</template>
          <template v-else>✗</template>
        </div>
        <div
          v-if="lane.status === 'running' && lane.startedAt > 0"
          class="lane-progress"
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
  gap: 8px;
}
.lane-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--win-text-primary);
}
.lane-sub {
  font-size: 11px;
  color: var(--win-text-tertiary);
}
.lane-warn {
  color: #ef4444;
  font-weight: 500;
}
.lane-list {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.lane-row {
  display: grid;
  grid-template-columns: 36px 1fr 90px 24px;
  grid-template-rows: auto auto;
  align-items: center;
  gap: 4px 8px;
  padding: 6px 10px;
  border-radius: 4px;
  font-size: 12px;
  background: var(--win-bg-secondary, rgba(255,255,255,0.04));
  border: 1px solid var(--win-border-secondary, rgba(255,255,255,0.08));
  transition: background 0.2s, border-color 0.2s;
}
.lane-row.lane-clickable {
  cursor: pointer;
  user-select: none;
}
.lane-row.lane-clickable:hover {
  background: var(--win-control-hover, rgba(255, 255, 255, 0.08));
}
.lane-row.lane-running {
  border-color: var(--win-accent, #4f46e5);
  background: rgba(79, 70, 229, 0.08);
}
.lane-row.lane-clickable.lane-running:hover {
  background: rgba(79, 70, 229, 0.16);
}
.lane-row.lane-thinking {
  border-color: rgba(168, 85, 247, 0.5);
  background: rgba(168, 85, 247, 0.08);
}
.lane-row.lane-clickable.lane-thinking:hover {
  background: rgba(168, 85, 247, 0.16);
}
.lane-row.lane-stalled {
  border-color: #ef4444;
  background: rgba(239, 68, 68, 0.12);
  animation: lane-pulse 1.5s ease-in-out infinite;
}
@keyframes lane-pulse {
  0%, 100% { background: rgba(239, 68, 68, 0.12); }
  50% { background: rgba(239, 68, 68, 0.22); }
}
.lane-row.lane-done {
  border-color: rgba(34, 197, 94, 0.4);
  background: rgba(34, 197, 94, 0.06);
}
.lane-row.lane-clickable.lane-done:hover {
  background: rgba(34, 197, 94, 0.12);
}
.lane-row.lane-failed {
  border-color: rgba(239, 68, 68, 0.4);
  background: rgba(239, 68, 68, 0.06);
}
.lane-row.lane-clickable.lane-failed:hover {
  background: rgba(239, 68, 68, 0.12);
}
.lane-row.lane-empty {
  opacity: 0.4;
  border-style: dashed;
}
.lane-slot {
  font-size: 10px;
  color: var(--win-text-tertiary);
  font-family: ui-monospace, monospace;
  grid-row: 1;
}
.lane-range {
  color: var(--win-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  grid-row: 1;
}
.lane-elapsed {
  font-size: 11px;
  color: var(--win-text-tertiary);
  font-family: ui-monospace, monospace;
  text-align: right;
  font-variant-numeric: tabular-nums;
  grid-row: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 1px;
}
.lane-tok {
  font-size: 9px;
  color: var(--win-text-tertiary);
  font-weight: 500;
  display: inline-flex;
  align-items: center;
}
.lane-tok.is-thinking {
  color: #c084fc;
}
.lane-streaming-dot {
  display: inline-block;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--win-accent, #4f46e5);
  margin-right: 3px;
  animation: dot-pulse 1s infinite alternate ease-in-out;
}
.lane-streaming-dot.is-thinking {
  background: #a855f7;
  box-shadow: 0 0 6px rgba(168, 85, 247, 0.6);
}
@keyframes dot-pulse {
  0% { transform: scale(0.8); opacity: 0.6; }
  100% { transform: scale(1.3); opacity: 1; }
}
.lane-state {
  font-size: 14px;
  text-align: center;
  grid-row: 1;
}
.lane-progress {
  grid-column: 1 / -1;
  grid-row: 2;
  height: 3px;
  background: rgba(255, 255, 255, 0.06);
  border-radius: 2px;
  overflow: hidden;
  margin-top: 2px;
}
.lane-progress-bar {
  height: 100%;
  background: var(--win-accent, #4f46e5);
  transition: width 0.3s linear, background 0.3s;
  position: relative;
}
.lane-row.lane-thinking .lane-progress-bar {
  background: #a855f7;
}
.lane-progress-bar.lane-progress-warn {
  background: #ef4444;
}
/* 流式吐字微脉冲（Shimmer 流光） */
.lane-row.lane-streaming .lane-progress-bar::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: linear-gradient(
    90deg,
    transparent 0%,
    rgba(255, 255, 255, 0.5) 50%,
    transparent 100%
  );
  animation: lane-shimmer 1.2s infinite ease-in-out;
}
@keyframes lane-shimmer {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(100%); }
}
</style>
