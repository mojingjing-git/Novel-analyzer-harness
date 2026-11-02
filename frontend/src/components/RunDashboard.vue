<script setup lang="ts">
/**
 * RunDashboard 运行中实时仪表盘
 *
 * 容器组件：聚合指标（含 sparkline）+ LaneView + DiscoveryFeed
 * 数据源：父组件传入
 * 聚合卡的"本会话输入/输出"取 token_delta 真实累计
 */

import { computed } from 'vue'
import CountUp from './CountUp.vue'
import LaneView from './LaneView.vue'
import DiscoveryFeed from './DiscoveryFeed.vue'

export interface ActiveBlock {
  range: string
  startedAt: number
}
export interface FinishedBlock {
  ok: boolean
  range: string
}
export interface DiscoveryItem {
  id: number
  ts: number
  events: number
  foreshadows: string[]
  characters: string[]
  unresolved: string[]
  highlighted: boolean
}

const props = withDefaults(defineProps<{
  /** 是否正在运行（控制显示） */
  running?: boolean
  /** 并发槽位数 */
  concurrency?: number
  /** 进度 */
  progress?: { current: number; total: number; eta: string }
  /** 本会话 token 统计（input / output） */
  sessionTokens?: { input: number; output: number } | null
  /** 活跃块 */
  activeBlocks?: Map<number, ActiveBlock>
  /** 已完成块（最近 N 个用于车道占位） */
  finishedBlocks?: Map<number, FinishedBlock>
  /** 块级 token 累计（车道进度条 + 卡死预警 + 思考态感知） */
  tokenByBlock?: Map<number, { outputTokens: number; lastTokenAt: number; isThinking?: boolean }>
  /** 30 点速率 sparkline */
  rateHistory?: number[]
  /** 所有活跃 block 的聚合速率（tok/s）：rateHistory 只保留单个 block 的瞬时速率，
   * 4 路并发时拿它显示会得到单 block 速率而非系统总吞吐。 */
  aggregateRate?: number
  /** 卡死预警阈值（秒） */
  stallWarnSec?: number
  /** 发现流 */
  discoveries?: DiscoveryItem[]
}>(), {
  running: false,
  concurrency: 4,
  progress: () => ({ current: 0, total: 0, eta: '' }),
  sessionTokens: null,
  activeBlocks: () => new Map(),
  finishedBlocks: () => new Map(),
  tokenByBlock: () => new Map(),
  rateHistory: () => [],
  aggregateRate: 0,
  stallWarnSec: 480,
  discoveries: () => [],
})

const emit = defineEmits<{
  (e: 'select-block', blockId: number): void
}>()

const inputTokens = computed(() => props.sessionTokens?.input ?? 0)
const outputTokens = computed(() => props.sessionTokens?.output ?? 0)
const completed = computed(() => props.progress?.current ?? 0)
const total = computed(() => props.progress?.total ?? 0)
const eta = computed(() => props.progress?.eta ?? '—')
// 当前显示用 aggregateRate（所有活跃 block 聚合），
// rateHistory 仍驱动 sparkline 历史曲线
const currentRate = computed(() => {
  if (props.aggregateRate !== undefined && props.aggregateRate > 0) {
    return props.aggregateRate
  }
  const h = props.rateHistory
  return h.length > 0 ? h[h.length - 1] : 0
})

function fmtNum(n: number): string {
  if (n >= 10000) return `${(n / 1000).toFixed(1)}k`
  // Math.round 去掉 CountUp 动画中间帧的浮点：
  // CountUp 在 t<1 时 display = from + delta * eased（浮点），
  // String() 会直接输出 "12.999759744828719" 这种 18 位小数
  return String(Math.round(n))
}

// Sparkline polyline points（手写 SVG，30 点；不引入 d3-shape）
const sparklinePoints = computed(() => {
  const h = props.rateHistory
  if (h.length === 0) return ''
  const w = 200, hh = 28, n = 30
  const max = Math.max(...h, 1)
  return h.slice(-n).map((v, i) => {
    const x = (i / (n - 1)) * w
    const y = hh - (v / max) * hh
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
})
</script>

<template>
  <div class="glass-card run-dashboard">
    <!-- ① 聚合指标行（4 卡） -->
    <div class="rd-aggregate">
      <div class="rd-card">
        <div class="rd-label">已完成</div>
        <div class="rd-value">
          <CountUp :value="completed" :format="fmtNum" />
          <span class="rd-suffix">/ {{ fmtNum(total) }}</span>
        </div>
      </div>
      <div class="rd-card">
        <div class="rd-label">ETA</div>
        <div class="rd-value rd-eta">{{ eta }}</div>
      </div>
      <div class="rd-card">
        <div class="rd-label">本会话输入</div>
        <div class="rd-value">
          <CountUp :value="inputTokens" :format="fmtNum" />
          <span class="rd-suffix">tok</span>
        </div>
      </div>
      <div class="rd-card">
        <div class="rd-label">本会话输出</div>
        <div class="rd-value">
          <CountUp :value="outputTokens" :format="fmtNum" />
          <span class="rd-suffix">tok</span>
        </div>
      </div>
    </div>

    <!-- ② 实时速率 sparkline（30 点手写 polyline） -->
    <div class="rd-sparkline">
      <div class="rd-sparkline-head">
        <span class="rd-label">实时输出速率</span>
        <span class="rd-rate-value">{{ currentRate.toFixed(0) }} tok/s</span>
      </div>
      <svg :width="200" :height="28" class="rd-sparkline-svg" v-if="rateHistory.length > 1">
        <polyline
          :points="sparklinePoints"
          fill="none"
          stroke="var(--win-accent, #4f46e5)"
          stroke-width="1.5"
          stroke-linejoin="round"
          stroke-linecap="round"
        />
      </svg>
      <div v-else class="rd-sparkline-empty">尚无数据</div>
    </div>

    <!-- ③ 并发车道（进度条 + 卡死预警） -->
    <LaneView
      :concurrency="concurrency"
      :active-blocks="activeBlocks"
      :finished-blocks="finishedBlocks"
      :token-by-block="tokenByBlock"
      :stall-warn-sec="stallWarnSec"
      @select-block="emit('select-block', $event)"
    />

    <!-- ④ 发现流 -->
    <DiscoveryFeed :items="discoveries" />
  </div>
</template>

<style scoped>
/* 根节点带 .glass-card，套用 main.css 的 Win11 卡片基类（背景/边框/圆角/阴影）
   内边距由本组件自己控制：glass-card 本身无 padding，避免与子组件的内部布局冲突 */
.run-dashboard {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  min-height: 0;
  overflow-y: auto;
}
.rd-aggregate {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}
.rd-card {
  padding: 10px 12px;
  border-radius: var(--win-radius-control);
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
}
.rd-label {
  font-size: 10px;
  color: var(--win-text-tertiary);
  margin-bottom: 2px;
}
.rd-value {
  display: flex;
  align-items: baseline;
  gap: 4px;
  font-size: 18px;
  font-weight: 600;
  color: var(--win-text-primary);
  font-variant-numeric: tabular-nums;
}
.rd-eta {
  font-size: 14px;
  font-weight: 500;
  font-family: ui-monospace, monospace;
}
.rd-suffix {
  font-size: 11px;
  color: var(--win-text-tertiary);
  font-weight: 400;
}
.rd-sparkline {
  padding: 8px 12px;
  border-radius: var(--win-radius-control);
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
}
.rd-sparkline-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 4px;
}
.rd-rate-value {
  font-size: 13px;
  font-weight: 600;
  color: var(--win-accent, #4f46e5);
  font-variant-numeric: tabular-nums;
}
.rd-sparkline-svg {
  display: block;
  width: 100%;
  height: 28px;
}
.rd-sparkline-empty {
  font-size: 11px;
  color: var(--win-text-tertiary);
  text-align: center;
  padding: 6px 0;
}
</style>
