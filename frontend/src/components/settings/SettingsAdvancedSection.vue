<script setup lang="ts">
import type { AppConfigDto } from '../../api/client'
import { tooltips, jsonModes, type CategoryDef } from '../../utils/settingsTooltips'

defineProps<{
  show: boolean
  config: AppConfigDto
  categoryDefs: CategoryDef[]
  keptSet: Set<string>
  errCls: (field: string) => string
}>()

const emit = defineEmits<{
  'toggle-category': [name: string]
  'select-all-categories': []
  'clear-all-categories': []
}>()
</script>

<template>
  <div v-show="show" class="settings-cards-grid">
    <!-- 高级分组 1：请求超时、JSON 模式与退火重试 -->
    <div class="glass-card settings-card p-4 space-y-3">
      <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">请求超时、JSON 模式与退火重试</h3>
      <div class="sf-num-grid">
        <div class="sf-num-row"><label v-tooltip="tooltips.max_tokens">Max Tokens:</label><input v-model.number="config.api.max_tokens" :class="errCls('max_tokens')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.timeout">分析 Timeout (s):</label><input v-model.number="config.api.timeout" :class="errCls('timeout')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.summary_timeout">总结 Timeout (s):</label><input v-model.number="config.api.summary_timeout" :class="errCls('summary_timeout')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.json_mode">JSON 模式:</label>
          <select v-model="config.api.json_mode" :class="errCls('json_mode')" class="glass-select sf-json-select">
            <option v-for="m in jsonModes" :key="m.value" :value="m.value">{{ m.label }}</option>
          </select>
        </div>
        <div class="sf-num-row"><label v-tooltip="tooltips.temperature">初始温度:</label><input v-model.number="config.api.temperature" :class="errCls('temperature')" type="number" step="0.01" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.temperature_step">温度步长:</label><input v-model.number="config.api.temperature_step" :class="errCls('temperature_step')" type="number" step="0.01" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.temperature_max_retries">退火重试:</label><input v-model.number="config.api.temperature_max_retries" :class="errCls('temperature_max_retries')" type="number" min="1" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.backoff_max_retries">退避重试:</label><input v-model.number="config.api.backoff_max_retries" :class="errCls('backoff_max_retries')" type="number" min="0" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label title="单次复检调用的输出上限（token）">复检 Max Tokens:</label><input v-model.number="config.analysis.recheck_max_tokens" :class="errCls('recheck_max_tokens')" type="number" min="1024" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label title="单次复检调用的超时（秒）；0 = 跟随分析 Timeout">复检 Timeout (s):</label><input v-model.number="config.analysis.recheck_timeout" :class="errCls('recheck_timeout')" type="number" min="0" class="glass-input sf-num-input" /></div>
        <div v-if="(config.analysis.recheck_version || 'v3') === 'v2'" class="sf-num-row">
          <label title="仅 V2 引擎生效：每次调用同时判定几条候选">V2 判定批大小:</label>
          <input v-model.number="config.analysis.recheck_v2_batch_size" :class="errCls('recheck_v2_batch_size')" type="number" min="1" max="20" class="glass-input sf-num-input" />
        </div>
      </div>
    </div>

    <!-- 高级分组 2：提示词上下文与滚动总结预算 -->
    <div class="glass-card settings-card p-4 space-y-3">
      <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke); letter-spacing: -0.01em">提示词上下文与滚动总结预算</h3>
      <div class="sf-num-grid">
        <div class="sf-num-row"><label v-tooltip="tooltips.rolling_early_chapters">触发章数:</label><input v-model.number="config.analysis.rolling_early_chapters" :class="errCls('rolling_early_chapters')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.rolling_max_milestones">里程碑上限:</label><input v-model.number="config.analysis.rolling_max_milestones" :class="errCls('rolling_max_milestones')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.rolling_max_momentum">势头上限:</label><input v-model.number="config.analysis.rolling_max_momentum" :class="errCls('rolling_max_momentum')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.checkpoint_interval">Checkpoint:</label><input v-model.number="config.analysis.checkpoint_interval" :class="errCls('checkpoint_interval')" type="number" min="0" max="100" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.max_arc_length">主线最大字数:</label><input v-model.number="config.analysis.max_arc_length" :class="errCls('max_arc_length')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.max_arcs_in_prompt">Prompt 主线条数:</label><input v-model.number="config.analysis.max_arcs_in_prompt" :class="errCls('max_arcs_in_prompt')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.max_summaries_in_prompt">Prompt 摘要数:</label><input v-model.number="config.analysis.max_summaries_in_prompt" :class="errCls('max_summaries_in_prompt')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.timeline_truncate">时间线截断:</label><input v-model.number="config.analysis.timeline_truncate" :class="errCls('timeline_truncate')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.max_character_states">角色状态数:</label><input v-model.number="config.analysis.max_character_states" :class="errCls('max_character_states')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.max_world_items">世界观条数:</label><input v-model.number="config.analysis.max_world_items" :class="errCls('max_world_items')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.max_foreshadow_entries">伏笔网络条数:</label><input v-model.number="config.analysis.max_foreshadow_entries" :class="errCls('max_foreshadow_entries')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.batch_summary_min_words">分卷摘要字数:</label><input v-model.number="config.analysis.batch_summary_min_words" :class="errCls('batch_summary_min_words')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label v-tooltip="tooltips.final_report_min_words">最终报告字数:</label><input v-model.number="config.analysis.final_report_min_words" :class="errCls('final_report_min_words')" type="number" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label title="卷摘要拼接总字符超过此阈值时触发分层压缩">压缩阈值 (字符):</label><input v-model.number="config.analysis.volume_compress_threshold" :class="errCls('volume_compress_threshold')" type="number" min="10000" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label title="每 N 卷为一组（首尾各 1 组保留全文，中间组截断到 1/2）">压缩组大小 (卷):</label><input v-model.number="config.analysis.volume_compress_group" :class="errCls('volume_compress_group')" type="number" min="1" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label title="伏笔最低保留重要度：低/中/高">伏笔最低重要度:</label>
          <select v-model="config.analysis.foreshadow_min_importance" class="glass-select sf-num-input"><option>低</option><option>中</option><option>高</option></select>
        </div>
        <div class="sf-num-row"><label title="伏笔最低保留置信度：低/中/高">伏笔最低置信度:</label>
          <select v-model="config.analysis.foreshadow_min_confidence" class="glass-select sf-num-input"><option>低</option><option>中</option><option>高</option></select>
        </div>
        <div class="sf-num-row"><label title="importance=高 的伏笔最多保留多少（-1=无上限）">高 importance 上限:</label><input v-model.number="config.analysis.max_foreshadow_catalog_high" type="number" min="-1" class="glass-input sf-num-input" /></div>
        <div class="sf-num-row"><label title="importance=中 的伏笔最多保留多少">中 importance 上限:</label><input v-model.number="config.analysis.max_foreshadow_catalog_mid" type="number" min="0" class="glass-input sf-num-input" /></div>
      </div>
    </div>

    <!-- 高级分组 3：伏笔分类（功能类别） -->
    <div v-if="categoryDefs.length" class="glass-card settings-card settings-span-2 p-4 space-y-3">
      <div class="flex items-center justify-between pb-2" style="border-bottom: 1px solid var(--win-stroke)">
        <h3 class="font-semibold" style="letter-spacing: -0.01em">伏笔分类（功能类别）</h3>
        <div class="flex gap-2">
          <button @click="emit('select-all-categories')" class="glass-pill">全选</button>
          <button @click="emit('clear-all-categories')" class="glass-pill" title="清空 = 只保留「其他」兜底">清空</button>
        </div>
      </div>
      <p class="text-xs" style="color: var(--win-text-secondary)">
        已选 {{ keptSet.size }} / {{ categoryDefs.length }}
      </p>
      <div class="sf-cat-grid">
        <label
          v-for="cat in categoryDefs"
          :key="cat.name"
          class="category-chip flex items-start gap-2 text-sm cursor-pointer p-1.5"
          :class="{ 'is-checked': keptSet.has(cat.name) }"
          v-tooltip="cat.description + (cat.examples.length ? '\n\n典型示例: ' + cat.examples.join(', ') : '')"
        >
          <input
            type="checkbox"
            :checked="keptSet.has(cat.name)"
            @change="emit('toggle-category', cat.name)"
            style="margin-top: 3px"
          />
          <span style="color: var(--win-text-primary)">{{ cat.name }}</span>
        </label>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-cards-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 20px;
}
.settings-card {
  container-type: inline-size;
  min-width: 0;
}
@container (min-width: 960px) {
  .settings-cards-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .settings-span-2 {
    grid-column: span 2;
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
.sf-json-select {
  width: 140px;
  max-width: 55%;
  flex-shrink: 0;
}
@container (min-width: 460px) {
  .sf-num-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
.sf-cat-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
@container (min-width: 640px) {
  .sf-cat-grid {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
}
.input-invalid {
  border-color: var(--win-danger) !important;
  box-shadow: 0 0 0 1px var(--win-danger);
}
.category-chip {
  border: 1px solid var(--win-stroke);
  border-radius: var(--win-radius-control);
  transition: background var(--win-duration-fast) var(--win-ease),
              border-color var(--win-duration-fast) var(--win-ease);
}
.category-chip.is-checked {
  background: var(--win-accent-soft);
  border-color: var(--win-accent);
}
</style>
