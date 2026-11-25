<script setup lang="ts">
/**
 * RunDashboard 运行中实时仪表盘
 *
 * 容器组件：聚合 KPI（4 卡）+ 60 秒 1s 平滑总吞吐面积图 + 并发车道（微卡网格）+ 发现流
 * 设计对齐 Win11 Fluent：中性层叠表面、单一系统蓝强调、平滑曲线与连续计数缓动
 */

import { computed } from 'vue'
import CountUp from './CountUp.vue'
import LaneView from './LaneView.vue'
import DiscoveryFeed from './DiscoveryFeed.vue'
import ThroughputChart from './ThroughputChart.vue'
import { type SessionTokenStatsResponse } from '../api/client'
import { fmt, fmtPercent, formatSessionCost } from '../utils/queueFeed'

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
  /** 每块章数（>1 时展示章节换算） */
  blockSize?: number
  /** 本会话 token 统计（input / output） */
  sessionTokens?: { input: number; output: number } | null
  /** 会话合并附加指标（缓存命中 / 命中率 / 总消耗 / 费用） */
  sessionMeta?: {
    cached: number
    hitRate: number
    total: number
    cost?: SessionTokenStatsResponse['cost'] | null
    costTooltip?: string
  } | null
  /** 活跃块 */
  activeBlocks?: Map<number, ActiveBlock>
  /** 已完成块（最近 N 个用于车道占位） */
  finishedBlocks?: Map<number, FinishedBlock>
  /** 块级 token 累计（车道进度条 + 卡死预警 + 思考态感知） */
  tokenByBlock?: Map<number, { outputTokens: number; lastTokenAt: number; isThinking?: boolean }>
  /** 60 秒等距平滑总吞吐历史 */
  rateHistory?: number[]
  /** 所有活跃 block 的聚合速率（tok/s） */
  aggregateRate?: number
  /** 卡死预警阈值（秒） */
  stallWarnSec?: number
  /** 发现流 */
  discoveries?: DiscoveryItem[]
}>(), {
  running: false,
  concurrency: 4,
  progress: () => ({ current: 0, total: 0, eta: '' }),
  blockSize: 1,
  sessionTokens: null,
  sessionMeta: null,
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
const progressPct = computed(() => {
  if (!total.value || total.value <= 0) return 0
  return Math.min(100, Math.round((completed.value / total.value) * 100))
})
const eta = computed(() => props.progress?.eta?.trim() || '—')

function fmtNum(n: number): string {
  if (n >= 10000) return `${(n / 1000).toFixed(1)}k`
  return String(Math.round(n))
}
</script>

<template>
  <div class="glass-card run-dashboard">
    <!-- ① 聚合指标行（4 卡，窄容器 2×2，宽容器 4×1） -->
    <div class="rd-aggregate">
      <div class="rd-card">
        <div class="rd-label">已完成</div>
        <div class="rd-value">
          <CountUp :value="completed" :format="fmtNum" />
          <span class="rd-suffix">/ {{ fmtNum(total) }}</span>
          <span v-if="total > 0" class="rd-suffix">章</span>
        </div>
        <div v-if="total > 0" class="rd-progress-track">
          <div class="rd-progress-fill" :style="{ width: `${progressPct}%` }" />
        </div>
      </div>
      <div class="rd-card">
        <div class="rd-label">ETA</div>
        <div class="rd-value rd-eta" :title="eta">{{ eta }}</div>
      </div>
      <div class="rd-card">
        <div class="rd-label">本会话输入</div>
        <div class="rd-value">
          <CountUp :value="inputTokens" :format="fmtNum" :duration="850" />
          <span class="rd-suffix">tok</span>
        </div>
      </div>
      <div class="rd-card">
        <div class="rd-label">本会话输出</div>
        <div class="rd-value">
          <CountUp :value="outputTokens" :format="fmtNum" :duration="850" />
          <span class="rd-suffix">tok</span>
        </div>
      </div>
    </div>

    <!-- ①b 合并的会话附加统计（缓存命中 / 命中率 / 总消耗 / 费用） -->
    <div v-if="sessionMeta && (sessionMeta.total > 0 || sessionMeta.cached > 0 || sessionMeta.cost)" class="rd-session-meta">
      <span>命中缓存 <b>{{ fmt(sessionMeta.cached) }}</b></span>
      <span>命中率 <b>{{ fmtPercent(sessionMeta.hitRate) }}</b></span>
      <span>总消耗 <b>{{ fmt(sessionMeta.total) }}</b></span>
      <span v-if="sessionMeta.cost" :title="sessionMeta.costTooltip || undefined">费用 <b>{{ formatSessionCost(sessionMeta.cost) }}</b></span>
    </div>

    <!-- ② 系统总输出吞吐面积图（过去 60 秒，1 秒等距平滑采样） -->
    <ThroughputChart
      :rate-history="rateHistory"
      :aggregate-rate="aggregateRate"
      :running="running"
    />

    <!-- ③ 并发车道（微卡网格 + 卡死预警） -->
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
.run-dashboard {
  container-type: inline-size;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
}
.rd-aggregate {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
@container (min-width: 500px) {
  .rd-aggregate {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
}
.rd-card {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border-radius: var(--win-radius-control);
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
}
.rd-label {
  font-size: 11px;
  color: var(--win-text-muted);
  margin-bottom: 2px;
}
.rd-value {
  display: flex;
  align-items: baseline;
  gap: 4px;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 18px;
  font-weight: 600;
  color: var(--win-text-primary);
  font-variant-numeric: tabular-nums;
}
.rd-eta {
  display: block;
  font-size: 14px;
  font-weight: 500;
  font-family: ui-monospace, monospace;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.rd-suffix {
  font-size: 11px;
  color: var(--win-text-muted);
  font-weight: 400;
}
.rd-progress-track {
  width: 100%;
  height: 3px;
  margin-top: 4px;
  border-radius: var(--win-radius-pill);
  background: var(--win-stroke);
  overflow: hidden;
}
.rd-progress-fill {
  height: 100%;
  border-radius: var(--win-radius-pill);
  background: var(--win-accent);
  transition: width 600ms var(--win-ease);
}
.rd-session-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 14px;
  padding: 6px 10px;
  border-radius: var(--win-radius-control);
  background: var(--win-control-alt);
  font-size: 11.5px;
  color: var(--win-text-secondary);
  font-variant-numeric: tabular-nums;
}
.rd-session-meta b {
  color: var(--win-text-primary);
  font-weight: 500;
}
</style>
