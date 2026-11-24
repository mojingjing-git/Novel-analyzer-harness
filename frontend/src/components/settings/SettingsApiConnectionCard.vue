<script setup lang="ts">
import type { AppConfigDto } from '../../api/client'
import { tooltips } from '../../utils/settingsTooltips'

defineProps<{
  config: AppConfigDto
  presets: Record<string, { base_url: string; model: string; api_key?: string }>
  catalogVersion: string
  catalogSyncing: boolean
  catalogSyncMsg: string
  errCls: (field: string) => string
}>()

const emit = defineEmits<{
  'sync-catalog': []
  preset: [name: string]
}>()
</script>

<template>
  <div class="glass-card settings-card p-4 space-y-3">
    <div class="flex items-center justify-between gap-2 flex-wrap pb-2" style="border-bottom: 1px solid var(--win-stroke)">
      <h3 class="font-semibold" style="letter-spacing: -0.01em">主 API 连接</h3>
      <div class="flex items-center gap-2 flex-wrap">
        <span
          v-if="catalogSyncMsg"
          class="text-xs"
          :style="{ color: catalogSyncMsg.includes('失败') ? 'var(--win-danger)' : 'var(--win-success)' }"
        >
          {{ catalogSyncMsg }}
        </span>
        <span v-else-if="catalogVersion" class="text-xs" style="color: var(--win-text-muted)">
          模型库: {{ catalogVersion }}
        </span>
        <button
          @click="emit('sync-catalog')"
          :disabled="catalogSyncing"
          class="glass-button btn-sm"
          style="font-size: 11px"
          title="从 models.dev 远程更新最新的模型规格与定价数据"
        >
          {{ catalogSyncing ? '同步中...' : '更新模型库' }}
        </button>
      </div>
    </div>
    <div class="sf-row">
      <label class="sf-label" v-tooltip="tooltips.api_preset">API 预设:</label>
      <div class="sf-control">
        <select @change="emit('preset', ($event.target as HTMLSelectElement).value)" class="glass-select flex-1 min-w-0">
          <option value="">选择预设...</option>
          <option v-for="(_, name) in presets" :key="name" :value="name">{{ name }}</option>
        </select>
      </div>
    </div>
    <div class="sf-row">
      <label class="sf-label" v-tooltip="tooltips.base_url">Base URL:</label>
      <div class="sf-control">
        <input v-model="config.api.base_url" :class="errCls('base_url')" class="glass-input flex-1 min-w-0" />
      </div>
    </div>
    <div class="sf-row">
      <label class="sf-label" v-tooltip="tooltips.api_key">API Key:</label>
      <div class="sf-control">
        <input v-model="config.api.api_key" :class="errCls('api_key')" type="password" class="glass-input flex-1 min-w-0" />
      </div>
    </div>
    <div class="sf-row">
      <label class="sf-label" v-tooltip="tooltips.provider">协议格式:</label>
      <div class="sf-control">
        <select v-model="config.api.provider" class="glass-select flex-1 min-w-0">
          <option value="auto">auto（自动检测）</option>
          <option value="openai">openai（OpenAI 兼容）</option>
          <option value="anthropic">anthropic（/v1/messages）</option>
        </select>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-card {
  container-type: inline-size;
  min-width: 0;
}
.sf-row {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
  min-width: 0;
}
.sf-label {
  font-size: 14px;
  color: var(--win-text-secondary);
  min-width: 0;
}
.sf-control {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
  width: 100%;
}
.sf-control > input,
.sf-control > select {
  flex: 1 1 160px;
  min-width: 0;
}
@container (min-width: 500px) {
  .sf-row {
    display: grid;
    grid-template-columns: 112px minmax(0, 1fr);
    align-items: center;
    gap: 8px 12px;
  }
}
.input-invalid {
  border-color: var(--win-danger) !important;
  box-shadow: 0 0 0 1px var(--win-danger);
}
.btn-sm {
  padding: 0 12px;
  white-space: nowrap;
}
</style>
