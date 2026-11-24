<script setup lang="ts">
import type { AppConfigDto } from '../../api/client'
import ToggleSwitch from '../ToggleSwitch.vue'
import { tooltips } from '../../utils/settingsTooltips'

defineProps<{
  config: AppConfigDto
  errCls: (field: string) => string
}>()
</script>

<template>
  <div class="glass-card settings-card p-4 space-y-3">
    <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">运行与流程策略</h3>
    <div class="sf-num-grid">
      <div class="sf-num-row">
        <label v-tooltip="tooltips.block_size">块大小 (章/块):</label>
        <input v-model.number="config.analysis.block_size" :class="errCls('block_size')" type="number" min="1" max="200" class="glass-input sf-num-input" />
      </div>
      <div class="sf-num-row">
        <label v-tooltip="tooltips.summary_batch_size">总结批次大小:</label>
        <input v-model.number="config.analysis.summary_batch_size" :class="errCls('summary_batch_size')" type="number" min="5" class="glass-input sf-num-input" />
      </div>
    </div>
    <div class="sf-row">
      <label class="sf-label" title="V3 采用全书双向路由、多跳侦探深挖与四防对称终审；V2 采用单跳严格核对">复检引擎:</label>
      <div class="sf-control">
        <select
          :value="config.analysis.recheck_version || 'v3'"
          @change="config.analysis.recheck_version = ($event.target as HTMLSelectElement).value"
          :class="errCls('recheck_version')"
          class="glass-select flex-1 min-w-0"
        >
          <option value="v3">V3 多跳深挖与对称终审（推荐·高召回零人工）</option>
          <option value="v2">V2 单跳严格布尔核对（兼容模式）</option>
        </select>
      </div>
    </div>
    <div class="sf-num-row">
      <label title="一次复检最多发多少次调用；超出记「未核对」，可续跑">复检调用上限:</label>
      <input v-model.number="config.analysis.recheck_max_calls" :class="errCls('recheck_max_calls')" type="number" min="1" class="glass-input sf-num-input" />
    </div>
    <div class="space-y-3 pt-1">
      <template v-if="(config.analysis.recheck_version || 'v3') === 'v3'">
        <div v-tooltip="tooltips.recheck_v3_gray_u4">
          <ToggleSwitch
            :modelValue="Boolean(config.analysis.recheck_v3_gray_u4)"
            @update:modelValue="config.analysis.recheck_v3_gray_u4 = $event"
            label="V3 灰区终审 U4（约 +33% token）"
          />
        </div>
        <div v-tooltip="tooltips.recheck_v3_mid_single_hop">
          <ToggleSwitch
            :modelValue="Boolean(config.analysis.recheck_v3_mid_single_hop)"
            @update:modelValue="config.analysis.recheck_v3_mid_single_hop = $event"
            label="V3 支线单跳省钱（再省约 15%–25% token）"
          />
        </div>
      </template>
      <div v-tooltip="tooltips.auto_archive">
        <ToggleSwitch v-model="config.analysis.auto_archive" label="自动归档" />
      </div>
      <div v-tooltip="tooltips.auto_summary">
        <ToggleSwitch v-model="config.analysis.auto_summary" label="队列完成后自动总结" />
      </div>
      <div v-tooltip="tooltips.skip_moderation_blocked">
        <ToggleSwitch v-model="config.analysis.skip_moderation_blocked" label="跳过内容审核拦截章节" />
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
.sf-num-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px 16px;
}
.sf-num-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}
.sf-num-row label {
  font-size: 14px;
  color: var(--win-text-secondary);
  min-width: 0;
  flex: 1;
}
.sf-num-input {
  width: 110px;
  flex-shrink: 0;
}
@container (min-width: 460px) {
  .sf-num-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
.input-invalid {
  border-color: var(--win-danger) !important;
  box-shadow: 0 0 0 1px var(--win-danger);
}
</style>
