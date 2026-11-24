<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import type {
  AppConfigDto,
  ConfigFieldSchemaDto,
  ConfigGroupSchemaDto,
  ConfigSchemaDto,
} from '../../api/client'
import ToggleSwitch from '../ToggleSwitch.vue'
import {
  cloneSchemaDefault,
  getConfigValueByPath,
  isFieldModified,
  resetFieldToDefault,
  setConfigValueByPath,
} from '../../utils/settingsSchema'
import { jsonModes, tooltips, type CategoryDef } from '../../utils/settingsTooltips'

const props = defineProps<{
  show: boolean
  config: AppConfigDto
  schema: ConfigSchemaDto
  categoryDefs: CategoryDef[]
  keptSet: Set<string>
  errCls: (field: string) => string
}>()

const emit = defineEmits<{
  'toggle-category': [name: string]
  'select-all-categories': []
  'clear-all-categories': []
}>()

const searchQuery = ref('')
const onlyModified = ref(false)
const expandedGroups = reactive<Record<string, boolean>>({})

const advancedGroups = computed<ConfigGroupSchemaDto[]>(() =>
  props.schema.groups.filter((g) => g.ui_placement === 'advanced'),
)

const advancedFields = computed<ConfigFieldSchemaDto[]>(() =>
  props.schema.fields.filter((f) => f.ui_placement === 'advanced'),
)

const totalModifiedCount = computed(() =>
  advancedFields.value.filter((f) => isFieldModified(props.config, f)).length,
)

function isGroupExpanded(groupId: string): boolean {
  return expandedGroups[groupId] ?? true
}

function toggleGroupExpanded(groupId: string): void {
  expandedGroups[groupId] = !isGroupExpanded(groupId)
}

function expandAll(): void {
  for (const g of advancedGroups.value) {
    expandedGroups[g.id] = true
  }
}

function collapseAll(): void {
  for (const g of advancedGroups.value) {
    expandedGroups[g.id] = false
  }
}

function revealGroups(groupIds: string[]): void {
  searchQuery.value = ''
  onlyModified.value = false
  for (const gid of groupIds) {
    expandedGroups[gid] = true
  }
}

defineExpose({ revealGroups })

function matchesSearch(field: ConfigFieldSchemaDto, q: string): boolean {
  if (!q) return true
  return (
    field.path.toLowerCase().includes(q) ||
    field.key.toLowerCase().includes(q) ||
    field.label.toLowerCase().includes(q) ||
    field.description.toLowerCase().includes(q)
  )
}

const normalizedQuery = computed(() => searchQuery.value.trim().toLowerCase())

const fieldsByGroup = computed(() => {
  const map = new Map<string, { total: number; modified: number; visible: ConfigFieldSchemaDto[] }>()
  for (const g of advancedGroups.value) {
    map.set(g.id, { total: 0, modified: 0, visible: [] })
  }
  const q = normalizedQuery.value
  for (const field of advancedFields.value) {
    const bucket = map.get(field.group)
    if (!bucket) continue
    bucket.total += 1
    const mod = isFieldModified(props.config, field)
    if (mod) bucket.modified += 1
    if (onlyModified.value && !mod) continue
    if (!matchesSearch(field, q)) continue
    bucket.visible.push(field)
  }
  return map
})

const categoryFieldSpec = computed(() =>
  advancedFields.value.find((f) => f.key === 'foreshadow_kept_categories') ?? null,
)

const filteredCategoryDefs = computed(() => {
  const q = normalizedQuery.value
  if (!q) return props.categoryDefs
  const spec = categoryFieldSpec.value
  if (spec && matchesSearch(spec, q)) return props.categoryDefs
  return props.categoryDefs.filter(
    (c) =>
      c.name.toLowerCase().includes(q) ||
      c.description.toLowerCase().includes(q) ||
      c.examples.some((ex) => ex.toLowerCase().includes(q)),
  )
})

const visibleGroups = computed(() => {
  const q = normalizedQuery.value
  return advancedGroups.value.filter((g) => {
    if (g.id === 'foreshadow_categories') {
      if (props.categoryDefs.length === 0) return false
      const catSpec = categoryFieldSpec.value
      if (onlyModified.value && catSpec && !isFieldModified(props.config, catSpec)) return false
      if (q && filteredCategoryDefs.value.length === 0 && !(catSpec && matchesSearch(catSpec, q))) {
        return false
      }
      return true
    }
    const bucket = fieldsByGroup.value.get(g.id)
    if (!bucket) return false
    if (!q && !onlyModified.value) return bucket.total > 0
    return bucket.visible.length > 0
  })
})

function fieldTooltip(field: ConfigFieldSchemaDto): string {
  const base = tooltips[field.key] || field.description
  return `${base}\n\n配置键: ${field.path} | 默认值: ${JSON.stringify(field.default)}`
}

function fieldInputClass(field: ConfigFieldSchemaDto): string {
  const c1 = props.errCls(field.key)
  const c2 = props.errCls(field.path)
  return c1 || c2
}

function choiceOptions(field: ConfigFieldSchemaDto): { value: string; label: string }[] {
  if (field.key === 'json_mode') return jsonModes
  return field.choices.map((c) => ({ value: c, label: c || '(默认/空)' }))
}

function onNumberInput(field: ConfigFieldSchemaDto, evt: Event): void {
  const raw = (evt.target as HTMLInputElement).value
  if (raw.trim() === '') {
    setConfigValueByPath(props.config, field, '')
    return
  }
  const num = Number(raw)
  if (!Number.isNaN(num)) {
    setConfigValueByPath(props.config, field, field.field_type === 'int' ? Math.trunc(num) : num)
  }
}

function formatListStr(val: unknown): string {
  return Array.isArray(val) ? val.join(', ') : ''
}

function onListStrChange(field: ConfigFieldSchemaDto, evt: Event): void {
  const raw = (evt.target as HTMLInputElement).value
  const parsed = raw
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean)
  setConfigValueByPath(props.config, field, parsed.length > 0 ? parsed : cloneSchemaDefault(field))
}

function onOptionalStrInput(field: ConfigFieldSchemaDto, evt: Event): void {
  const raw = (evt.target as HTMLInputElement).value.trim()
  setConfigValueByPath(props.config, field, raw ? raw : null)
}

function onStrInput(field: ConfigFieldSchemaDto, evt: Event): void {
  setConfigValueByPath(props.config, field, (evt.target as HTMLInputElement).value)
}

function onSelectChange(field: ConfigFieldSchemaDto, evt: Event): void {
  setConfigValueByPath(props.config, field, (evt.target as HTMLSelectElement).value)
}

function statusBadgeLabel(status: ConfigFieldSchemaDto['status']): string {
  if (status === 'reserved') return '预留'
  if (status === 'legacy') return '兼容'
  return ''
}
</script>

<template>
  <div v-show="show" class="space-y-4">
    <!-- 高级参数检索与过滤工具条 -->
    <div class="glass-card p-3.5 flex flex-wrap items-center gap-3">
      <input
        v-model="searchQuery"
        data-test="advanced-search"
        type="search"
        class="glass-input flex-1 min-w-52"
        placeholder="搜索参数名、键名或说明..."
      />
      <label class="flex items-center gap-2 text-sm cursor-pointer select-none" style="color: var(--win-text-primary)">
        <input
          v-model="onlyModified"
          data-test="advanced-only-modified"
          type="checkbox"
        />
        <span>仅看已修改 ({{ totalModifiedCount }})</span>
      </label>
      <div class="flex gap-2 ml-auto">
        <button type="button" class="glass-pill" @click="expandAll">全部展开</button>
        <button type="button" class="glass-pill" @click="collapseAll">全部折叠</button>
      </div>
    </div>

    <div
      v-if="visibleGroups.length === 0"
      class="glass-card p-6 text-center text-sm"
      style="color: var(--win-text-secondary)"
    >
      未找到匹配的高级配置参数，请调整搜索关键词或取消「仅看已修改」筛选。
    </div>

    <!-- 7 大高级参数分组 -->
    <div class="settings-cards-grid">
      <template v-for="group in visibleGroups" :key="group.id">
        <!-- 分组：50 类伏笔功能分类 -->
        <div
          v-if="group.id === 'foreshadow_categories'"
          :data-group-id="group.id"
          class="glass-card settings-card settings-span-2 p-4 space-y-3"
        >
          <div class="flex items-center justify-between gap-2 pb-2" style="border-bottom: 1px solid var(--win-stroke)">
            <button
              type="button"
              class="flex items-center gap-2 text-left font-semibold"
              style="letter-spacing: -0.01em; color: var(--win-text-primary)"
              @click="toggleGroupExpanded(group.id)"
            >
              <span>{{ isGroupExpanded(group.id) ? '▾' : '▸' }}</span>
              <span>{{ group.title }}</span>
              <span class="glass-badge text-xs">已选 {{ keptSet.size }} / {{ categoryDefs.length }}</span>
            </button>
            <div class="flex gap-2">
              <button
                v-if="categoryFieldSpec && isFieldModified(config, categoryFieldSpec)"
                type="button"
                class="glass-pill"
                @click="resetFieldToDefault(config, categoryFieldSpec)"
              >重置</button>
              <button type="button" @click="emit('select-all-categories')" class="glass-pill">全选</button>
              <button type="button" @click="emit('clear-all-categories')" class="glass-pill" title="清空 = 只保留「其他」兜底">清空</button>
            </div>
          </div>
          <div v-show="isGroupExpanded(group.id)" class="space-y-2.5">
            <p class="text-xs" style="color: var(--win-text-secondary)">{{ group.description }}</p>
            <div class="sf-cat-grid">
              <label
                v-for="cat in filteredCategoryDefs"
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

        <!-- 其余 6 大高级分组：Schema 驱动渲染 -->
        <div
          v-else
          :data-group-id="group.id"
          class="glass-card settings-card settings-span-2 p-4 space-y-3"
        >
          <div class="flex items-center justify-between gap-2 pb-2" style="border-bottom: 1px solid var(--win-stroke)">
            <button
              type="button"
              class="flex items-center gap-2 text-left font-semibold min-w-0"
              style="letter-spacing: -0.01em; color: var(--win-text-primary)"
              @click="toggleGroupExpanded(group.id)"
            >
              <span>{{ isGroupExpanded(group.id) ? '▾' : '▸' }}</span>
              <span class="truncate">{{ group.title }}</span>
              <span class="glass-badge text-xs shrink-0">
                {{ fieldsByGroup.get(group.id)?.visible.length ?? 0 }} 项
              </span>
              <span
                v-if="(fieldsByGroup.get(group.id)?.modified ?? 0) > 0"
                class="glass-badge badge-orange text-xs shrink-0"
              >
                已改 {{ fieldsByGroup.get(group.id)?.modified }}
              </span>
            </button>
          </div>

          <div v-show="isGroupExpanded(group.id)" class="space-y-3">
            <p class="text-xs" style="color: var(--win-text-secondary)">{{ group.description }}</p>
            <div class="sf-num-grid">
              <div
                v-for="field in fieldsByGroup.get(group.id)?.visible ?? []"
                :key="field.path"
                :data-field-path="field.path"
                class="sf-num-row"
              >
                <div class="sf-field-meta min-w-0 flex-1">
                  <div class="flex items-center gap-1.5 flex-wrap">
                    <label v-tooltip="fieldTooltip(field)" class="sf-field-label">
                      {{ field.label }}
                    </label>
                    <span
                      v-if="statusBadgeLabel(field.status)"
                      class="glass-badge text-[10px]"
                    >{{ statusBadgeLabel(field.status) }}</span>
                    <button
                      v-if="isFieldModified(config, field)"
                      type="button"
                      class="sf-reset-btn"
                      :data-reset-field="field.path"
                      title="恢复为默认值"
                      @click="resetFieldToDefault(config, field)"
                    >重置</button>
                  </div>
                  <div class="text-[11px] font-mono truncate" style="color: var(--win-text-muted)">
                    {{ field.key }}
                  </div>
                </div>

                <!-- 按字段类型分发控件 -->
                <ToggleSwitch
                  v-if="field.field_type === 'bool'"
                  :modelValue="Boolean(getConfigValueByPath(config, field))"
                  @update:modelValue="setConfigValueByPath(config, field, $event)"
                />
                <select
                  v-else-if="field.choices.length > 0"
                  :value="String(getConfigValueByPath(config, field) ?? '')"
                  :class="fieldInputClass(field)"
                  :data-field-input="field.path"
                  class="glass-select sf-json-select"
                  @change="onSelectChange(field, $event)"
                >
                  <option v-for="opt in choiceOptions(field)" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                  </option>
                </select>
                <input
                  v-else-if="field.field_type === 'int' || field.field_type === 'float'"
                  type="number"
                  :value="getConfigValueByPath(config, field)"
                  :min="field.min_val ?? undefined"
                  :max="field.max_val ?? undefined"
                  :step="field.step ?? (field.field_type === 'float' ? 0.01 : 1)"
                  :class="fieldInputClass(field)"
                  :data-field-input="field.path"
                  class="glass-input sf-num-input"
                  @input="onNumberInput(field, $event)"
                />
                <input
                  v-else-if="field.field_type === 'list_str'"
                  type="text"
                  :value="formatListStr(getConfigValueByPath(config, field))"
                  :class="fieldInputClass(field)"
                  :data-field-input="field.path"
                  placeholder="逗号分隔，例如 utf-8, gbk"
                  class="glass-input sf-text-input"
                  @change="onListStrChange(field, $event)"
                />
                <input
                  v-else-if="field.field_type === 'optional_str'"
                  type="text"
                  :value="String(getConfigValueByPath(config, field) ?? '')"
                  :class="fieldInputClass(field)"
                  :data-field-input="field.path"
                  placeholder="留空 = 自动/null"
                  class="glass-input sf-text-input"
                  @input="onOptionalStrInput(field, $event)"
                />
                <input
                  v-else
                  type="text"
                  :value="String(getConfigValueByPath(config, field) ?? '')"
                  :class="fieldInputClass(field)"
                  :data-field-input="field.path"
                  class="glass-input sf-text-input"
                  @input="onStrInput(field, $event)"
                />
              </div>
            </div>
          </div>
        </div>
      </template>
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
  gap: 10px 18px;
}
.sf-num-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
}
.sf-field-label {
  font-size: 13px;
  color: var(--win-text-secondary);
}
.sf-reset-btn {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  border: 1px solid var(--win-stroke);
  color: var(--win-accent);
  background: transparent;
  cursor: pointer;
}
.sf-num-input {
  width: 118px;
  flex-shrink: 0;
}
.sf-text-input {
  width: 170px;
  max-width: 55%;
  flex-shrink: 0;
}
.sf-json-select {
  width: 150px;
  max-width: 55%;
  flex-shrink: 0;
}
@container (min-width: 540px) {
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
