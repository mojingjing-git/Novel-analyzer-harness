<script setup lang="ts">
/**
 * CountUp 数字滚动动画
 *
 * 接收 :value (number)，内部用 requestAnimationFrame 缓动 600ms
 * :format 函数把数字渲染成字符串（适配 K/M 缩写、百分比等）
 *
 * 缓动：ease-out cubic（曲线 1 - (1-t)^3），起始快、收尾慢，符合 Win11 Fluent 节奏
 * 性能：单 timer / 组件挂载期间动态启动/取消，避免泄漏
 */
import { ref, watch, onMounted, onUnmounted } from 'vue'

const props = withDefaults(defineProps<{
  value: number
  /** 数字 → 字符串的格式化函数，默认直接转字符串 */
  format?: (n: number) => string
  /** 动画时长（毫秒），默认 600 */
  duration?: number
  /** 缓动曲线：easeOut（KPI 卡片）或 linear（1 秒采样连续数字） */
  easing?: 'easeOut' | 'linear'
}>(), {
  duration: 600,
  easing: 'easeOut',
  format: (n: number) => String(Math.round(n)),
})

const display = ref(0)
let rafId: number | null = null

function animate(from: number, to: number) {
  // 取消未结束的动画
  if (rafId !== null) {
    cancelAnimationFrame(rafId)
    rafId = null
  }
  // 极小差值不动画（避免抖动）
  if (Math.abs(to - from) < 0.01) {
    display.value = to
    return
  }
  const start = performance.now()
  const delta = to - from

  const tick = (now: number) => {
    const rawT = props.duration > 0 ? (now - start) / props.duration : 1
    const t = Math.max(0, Math.min(1, rawT))
    const eased = props.easing === 'linear' ? t : 1 - Math.pow(1 - t, 3)
    display.value = from + delta * eased
    if (t < 1) {
      rafId = requestAnimationFrame(tick)
    } else {
      display.value = to
      rafId = null
    }
  }
  rafId = requestAnimationFrame(tick)
}

// 监听值变化驱动动画：从当前中间帧 display.value 连续过渡，避免 1 秒批量提交时回跳闪断
watch(() => props.value, (newVal) => {
  animate(display.value, newVal)
})

onMounted(() => {
  // 首屏直接显示当前值（避免空白），不动画
  display.value = props.value
})

// 卸载后终止动画循环（与文件头“无泄漏”承诺对齐；影响有界但应兑现）
onUnmounted(() => { if (rafId !== null) cancelAnimationFrame(rafId) })

</script>

<template>
  <!-- .count-up 在 main.css 中提供 tabular-nums，避免数字宽度抖动 -->
  <span class="count-up">{{ format(display) }}</span>
</template>
