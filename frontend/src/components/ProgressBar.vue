<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  current: number
  total: number
  eta?: string
  label?: string
  blockSize?: number
}>()

const percent = computed(() => {
  if (!props.total || props.total <= 0) return 0
  return Math.min(100, Math.round((props.current / props.total) * 100))
})
</script>

<template>
  <div class="space-y-1.5">
    <div class="flex justify-between text-xs" style="color: var(--win-text-secondary)">
      <span>
        {{ label || '进度' }}: {{ current }}/{{ total }}
      </span>
      <span v-if="eta">预计剩余: {{ eta }}</span>
    </div>
    <div class="glass-progress-track">
      <div class="glass-progress-fill" :style="{ width: `${percent}%` }" />
    </div>
  </div>
</template>