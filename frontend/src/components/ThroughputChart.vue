<script setup lang="ts">
/**
 * ThroughputChart 60 秒平滑输出吞吐面积图（对数刻度）
 *
 * 共用于运行概览与最终总结页：支持自定义采样间隔与三次贝塞尔平滑插值，
 * 当前总速率经 CountUp 平滑滚动展示，右侧提供对数刻度尺，支持鼠标悬浮查看历史刻度。
 */

import { computed, ref } from 'vue'
import CountUp from './CountUp.vue'
import { calcLogChartCeiling, buildLogScaleTicks, buildSmoothAreaPaths } from '../utils/queueFeed'

let gradientSeq = 0

const props = withDefaults(defineProps<{
  /** 60 秒等距平滑总吞吐历史 */
  rateHistory?: number[]
  /** 当前聚合总速率（tok/s） */
  aggregateRate?: number
  /** 是否正在运行（影响空状态文案） */
  running?: boolean
  /** 标题文字 */
  title?: string
  /** 采样周期（ms），决定 CountUp 时长与悬停时间刻度换算 */
  sampleIntervalMs?: number
}>(), {
  rateHistory: () => [],
  aggregateRate: 0,
  running: false,
  title: '系统总输出吞吐',
  sampleIntervalMs: 1000,
})

const gradientId = `tc-area-${++gradientSeq}`
const hoverIndex = ref<number | null>(null)

const currentRate = computed(() => (
  Number.isFinite(props.aggregateRate) ? Math.max(0, props.aggregateRate) : 0
))

const countUpDuration = computed(() => Math.max(120, Math.round(props.sampleIntervalMs * 0.88)))

function fmtInt(n: number): string {
  return String(Math.round(n))
}

const chartCeiling = computed(() => calcLogChartCeiling(props.rateHistory))
const logTicks = computed(() => buildLogScaleTicks(chartCeiling.value, 56))
const chartPaths = computed(() => buildSmoothAreaPaths(props.rateHistory, 400, 56, chartCeiling.value, 'log'))

const hoverPoint = computed(() => {
  const idx = hoverIndex.value
  if (idx === null || idx < 0 || idx >= chartPaths.value.points.length) return null
  const pt = chartPaths.value.points[idx]
  const stepsAgo = chartPaths.value.points.length - 1 - idx
  const secAgo = (stepsAgo * props.sampleIntervalMs) / 1000
  return {
    xPct: (pt.x / 400) * 100,
    yPct: (pt.y / 56) * 100,
    x: pt.x,
    secLabel: stepsAgo === 0 ? '当前' : `-${Math.round(secAgo * 100) / 100}s`,
    valueText: `${Math.round(pt.value)} tok/s`,
  }
})

function onChartMove(e: MouseEvent) {
  const len = props.rateHistory.length
  if (len < 2) return
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  if (rect.width <= 0) return
  const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width))
  hoverIndex.value = Math.round(ratio * (len - 1))
}

function onChartLeave() {
  hoverIndex.value = null
}
</script>

<template>
  <div class="rd-sparkline">
    <div class="rd-sparkline-head">
      <div class="rd-sparkline-title">
        <span class="rd-label" style="margin-bottom: 0">{{ title }}</span>
        <span class="rd-scale-hint">60s</span>
      </div>
      <div class="rd-sparkline-meta">
        <span v-if="hoverPoint" class="rd-hover-readout">
          {{ hoverPoint.secLabel }} · {{ hoverPoint.valueText }}
        </span>
        <span class="rd-rate-value">
          <CountUp v-if="currentRate > 0" :value="currentRate" :format="fmtInt" :duration="countUpDuration" easing="linear" /><template v-else>0</template> tok/s
        </span>
      </div>
    </div>

    <div v-if="rateHistory.length > 1" class="rd-chart-body">
      <div
        class="rd-chart-canvas"
        @mousemove="onChartMove"
        @mouseleave="onChartLeave"
      >
        <svg viewBox="0 0 400 56" preserveAspectRatio="none" class="rd-sparkline-svg">
          <defs>
            <linearGradient :id="gradientId" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="var(--win-accent)" stop-opacity="0.20" />
              <stop offset="100%" stop-color="var(--win-accent)" stop-opacity="0.01" />
            </linearGradient>
          </defs>
          <line
            v-for="tick in logTicks.filter(t => t.value > 0)"
            :key="tick.value"
            x1="0" :y1="tick.y" x2="400" :y2="tick.y"
            stroke="var(--win-stroke)"
            stroke-dasharray="2 3"
            stroke-width="1"
            vector-effect="non-scaling-stroke"
          />
          <path :d="chartPaths.area" :fill="`url(#${gradientId})`" />
          <path
            :d="chartPaths.line"
            fill="none"
            stroke="var(--win-accent)"
            stroke-width="1.75"
            stroke-linejoin="round"
            stroke-linecap="round"
            vector-effect="non-scaling-stroke"
          />
          <line
            v-if="hoverPoint"
            :x1="hoverPoint.x" y1="0" :x2="hoverPoint.x" y2="56"
            stroke="var(--win-stroke-strong)"
            stroke-dasharray="2 2"
            stroke-width="1"
            vector-effect="non-scaling-stroke"
          />
        </svg>
        <span
          v-if="hoverPoint"
          class="rd-hover-dot"
          :style="{ left: hoverPoint.xPct + '%', top: hoverPoint.yPct + '%' }"
        />
      </div>
      <div class="rd-log-axis" title="对数刻度 (tok/s)">
        <div
          v-for="tick in logTicks"
          :key="tick.value"
          class="rd-log-tick"
          :style="{ top: tick.yPct + '%' }"
        >
          <span class="rd-log-notch" />
          <span class="rd-log-label">{{ tick.label }}</span>
        </div>
      </div>
    </div>
    <div v-else class="rd-sparkline-empty">
      {{ running ? '采集中…' : '暂无数据' }}
    </div>
  </div>
</template>

<style scoped>
.rd-label {
  font-size: 11px;
  color: var(--win-text-muted);
}
.rd-sparkline {
  padding: 10px 12px;
  border-radius: var(--win-radius-control);
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
}
.rd-sparkline-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 6px;
}
.rd-sparkline-title {
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.rd-sparkline-meta {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-variant-numeric: tabular-nums;
}
.rd-scale-hint {
  font-size: 10px;
  color: var(--win-text-muted);
}
.rd-hover-readout {
  font-size: 11px;
  color: var(--win-text-secondary);
  font-family: ui-monospace, monospace;
}
.rd-rate-value {
  font-size: 13px;
  font-weight: 600;
  color: var(--win-accent);
  font-variant-numeric: tabular-nums;
}
.rd-chart-body {
  display: flex;
  align-items: stretch;
  gap: 6px;
  padding: 4px 0;
}
.rd-chart-canvas {
  position: relative;
  flex: 1;
  min-width: 0;
  height: 64px;
}
.rd-sparkline-svg {
  display: block;
  width: 100%;
  height: 64px;
}
.rd-log-axis {
  position: relative;
  width: 34px;
  flex-shrink: 0;
  height: 64px;
  border-left: 1px solid var(--win-stroke);
}
.rd-log-tick {
  position: absolute;
  left: 0;
  right: 0;
  display: flex;
  align-items: center;
  gap: 3px;
  transform: translateY(-50%);
  pointer-events: none;
}
.rd-log-notch {
  width: 3px;
  height: 1px;
  background: var(--win-stroke-strong);
  flex-shrink: 0;
}
.rd-log-label {
  font-size: 9px;
  line-height: 1;
  color: var(--win-text-muted);
  font-variant-numeric: tabular-nums;
  font-family: ui-monospace, monospace;
}
.rd-hover-dot {
  position: absolute;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--win-accent);
  border: 1.5px solid var(--win-layer);
  transform: translate(-50%, -50%);
  pointer-events: none;
}
.rd-sparkline-empty {
  font-size: 11px;
  color: var(--win-text-muted);
  text-align: center;
  padding: 16px 0;
  border-top: 1px dashed var(--win-stroke);
}
</style>
