<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import type { ConfigProfileSlotDto, ConfigProfilesResponse } from '../../api/client'

const props = defineProps<{
  profiles: ConfigProfilesResponse | null
  busy: boolean
  hasUnsavedChanges: boolean
  notice: string
  warnings: string[]
  error: string
  saveModalError: string
}>()

const emit = defineEmits<{
  apply: [slot: ConfigProfileSlotDto]
  save: [payload: { slotId: string; name: string; description: string; include_credentials: boolean }]
  clear: [slot: ConfigProfileSlotDto]
  'clear-save-error': []
}>()

const builtinSlots = computed(() => (props.profiles?.slots ?? []).filter((s) => s.kind === 'builtin'))
const userSlots = computed(() => (props.profiles?.slots ?? []).filter((s) => s.kind === 'user'))

const lastAppliedSlotName = computed(() => {
  const id = props.profiles?.last_applied_slot_id
  if (!id) return ''
  return props.profiles?.slots.find((s) => s.id === id)?.name || id
})

const activeSlotName = computed(() => {
  const id = props.profiles?.active_slot_id
  if (!id) return ''
  return props.profiles?.slots.find((s) => s.id === id)?.name || id
})

// ===== 存入自定义槽弹窗状态 =====
const saveModalSlot = ref<ConfigProfileSlotDto | null>(null)
const modalName = ref('')
const modalDescription = ref('')
const modalIncludeCredentials = ref(false)
const localModalError = ref('')
const nameInputRef = ref<HTMLInputElement | null>(null)
let modalTrigger: HTMLElement | null = null

function openSaveModal(slot: ConfigProfileSlotDto) {
  if (props.busy) return
  const activeEl = document.activeElement
  modalTrigger = activeEl instanceof HTMLElement && activeEl !== document.body ? activeEl : null
  saveModalSlot.value = slot
  modalName.value = slot.name || `自定义槽 ${slot.slot_index - 3}`
  modalDescription.value = slot.description || ''
  modalIncludeCredentials.value = slot.populated ? slot.include_credentials : false
  localModalError.value = ''
  emit('clear-save-error')
  void nextTick(() => {
    nameInputRef.value?.focus()
    nameInputRef.value?.select()
  })
}

function closeSaveModal(force = false) {
  if (!force && props.busy) return
  if (!saveModalSlot.value) return
  saveModalSlot.value = null
  localModalError.value = ''
  emit('clear-save-error')
  const trigger = modalTrigger
  modalTrigger = null
  void nextTick(() => {
    if (trigger?.isConnected) trigger.focus()
  })
}

function onModalFieldInput() {
  if (localModalError.value) localModalError.value = ''
  if (props.saveModalError) emit('clear-save-error')
}

function submitSave() {
  if (!saveModalSlot.value || props.busy) return
  const trimmedName = modalName.value.trim()
  if (!trimmedName) {
    localModalError.value = '请输入方案名称'
    return
  }
  if (trimmedName.length > 40) {
    localModalError.value = '方案名称长度不能超过 40 个字符'
    return
  }
  const trimmedDesc = modalDescription.value.trim()
  if (trimmedDesc.length > 160) {
    localModalError.value = '备注说明长度不能超过 160 个字符'
    return
  }
  localModalError.value = ''
  emit('save', {
    slotId: saveModalSlot.value.id,
    name: trimmedName,
    description: trimmedDesc,
    include_credentials: modalIncludeCredentials.value,
  })
}

function formatUpdatedAt(iso: string | null): string {
  if (!iso) return ''
  return iso.slice(0, 16).replace('T', ' ')
}

function formatPoolChars(chars: number): string {
  if (!chars) return '默认'
  if (chars >= 1000) return `${Math.round(chars / 1000)}K`
  return String(chars)
}

onBeforeUnmount(() => {
  modalTrigger = null
})

defineExpose({
  closeSaveModal: () => closeSaveModal(true),
})
</script>

<template>
  <section v-if="profiles" class="glass-card settings-profiles-card p-4 space-y-4">
    <div class="flex items-start justify-between gap-3 flex-wrap pb-2.5" style="border-bottom: 1px solid var(--win-stroke)">
      <div>
        <div class="flex items-center gap-2 flex-wrap">
          <h3 class="font-semibold" style="letter-spacing: -0.01em">配置方案一键切换（6 槽位）</h3>
          <span v-if="profiles.active_slot_id" class="glass-badge badge-blue">
            当前激活: {{ activeSlotName }}
          </span>
          <span v-else-if="profiles.is_modified_from_last" class="glass-badge badge-orange">
            相对「{{ lastAppliedSlotName }}」已微调
          </span>
        </div>
        <p class="text-xs mt-1" style="color: var(--win-text-muted)">
          上排为模型无关的 3 档上下文通用预设（保留端点、密钥、模型与思考设置）；下排可将当前整套配置存入自定义槽位。
        </p>
      </div>
    </div>

    <p v-if="profiles.store_corrupted" class="text-xs glass-tinted-red px-2.5 py-1.5 rounded">
      检测到 config_profiles.json 解析失败，已锁定自定义槽写入以免覆盖原文件；内置预设仍可正常切换。
    </p>
    <p v-if="notice" class="text-xs glass-tinted-green px-2.5 py-1.5 rounded" aria-live="polite">
      {{ notice }}
    </p>
    <div v-if="warnings.length > 0" class="space-y-1">
      <p v-for="(w, idx) in warnings" :key="idx" class="text-xs glass-tinted-orange px-2.5 py-1.5 rounded">
        {{ w }}
      </p>
    </div>
    <p v-if="error" class="text-xs glass-tinted-red px-2.5 py-1.5 rounded">
      {{ error }}
    </p>

    <!-- 上排：3 个内置通用预设 -->
    <div class="space-y-2">
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <span class="text-xs font-semibold" style="color: var(--win-text-secondary)">内置通用默认预设（模型无关 · 只切策略与上下文预算）</span>
      </div>
      <div class="profiles-grid">
        <div
          v-for="slot in builtinSlots"
          :key="slot.id"
          :data-profile-slot="slot.id"
          class="profile-slot-card glass-inset"
          :class="{ 'is-slot-active': slot.is_active }"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <div class="flex items-center gap-1.5 flex-wrap">
                <span class="font-medium text-sm truncate">{{ slot.name }}</span>
                <span class="glass-badge" :class="slot.recommended ? 'badge-blue' : ''">{{ slot.badge }}</span>
                <span v-if="slot.is_active" class="glass-badge badge-green">当前生效</span>
              </div>
              <p class="text-xs mt-1 leading-relaxed" style="color: var(--win-text-secondary)">
                {{ slot.description }}
              </p>
            </div>
          </div>

          <div v-if="slot.summary" class="profile-chips mt-2.5">
            <span class="profile-chip">块 {{ slot.summary.block_size }}章</span>
            <span class="profile-chip">并发 {{ slot.summary.concurrency }}/{{ slot.summary.summary_concurrency }}/{{ slot.summary.recheck_concurrency }}</span>
            <span class="profile-chip">总结批 {{ slot.summary.summary_batch_size }}章</span>
            <span class="profile-chip">复检池 {{ formatPoolChars(slot.summary.recheck_v3_pool_max_chars) }} ({{ slot.summary.recheck_v3_batch_size }}条)</span>
          </div>

          <div class="mt-3 pt-2 flex items-center justify-between gap-2" style="border-top: 1px solid var(--win-stroke)">
            <span class="text-xs" style="color: var(--win-text-muted)">覆盖 {{ slot.managed_field_count }} 项策略参数</span>
            <button
              type="button"
              :data-profile-apply="slot.id"
              :disabled="busy || (slot.is_active && !hasUnsavedChanges)"
              @click="emit('apply', slot)"
              :class="slot.is_active ? 'glass-button btn-sm' : (slot.recommended ? 'glass-button glass-button-primary btn-sm' : 'glass-button btn-sm')"
            >
              {{ slot.is_active && !hasUnsavedChanges ? '已生效' : '一键切换' }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 下排：3 个用户自定义槽 -->
    <div class="space-y-2">
      <div class="flex items-center justify-between gap-2 flex-wrap">
        <span class="text-xs font-semibold" style="color: var(--win-text-secondary)">用户自定义配置槽（整套策略与模型配置 · 支持跨厂商切换）</span>
      </div>
      <div class="profiles-grid">
        <div
          v-for="slot in userSlots"
          :key="slot.id"
          :data-profile-slot="slot.id"
          class="profile-slot-card glass-inset"
          :class="{ 'is-slot-active': slot.is_active, 'is-slot-empty': !slot.populated }"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-1.5 flex-wrap">
                <span class="font-medium text-sm truncate">{{ slot.name }}</span>
                <span v-if="slot.is_active" class="glass-badge badge-green">当前生效</span>
                <span v-else-if="slot.populated" class="glass-badge">{{ slot.badge }}</span>
                <span v-else class="glass-badge badge-gray">空槽位</span>
                <span v-if="slot.has_api_key || slot.has_recheck_api_key" class="glass-badge badge-blue">含密钥</span>
              </div>

              <template v-if="slot.populated && slot.summary">
                <p v-if="slot.description" class="text-xs mt-1 leading-relaxed" style="color: var(--win-text-secondary)">
                  {{ slot.description }}
                </p>
                <p class="text-xs mt-1 truncate" style="color: var(--win-text-muted)" :title="slot.summary.model || '未填模型'">
                  模型: {{ slot.summary.model || '未填' }}<template v-if="slot.summary.base_url"> · {{ slot.summary.base_url }}</template>
                </p>
                <div class="profile-chips mt-2">
                  <span class="profile-chip">块 {{ slot.summary.block_size }}章</span>
                  <span class="profile-chip">并发 {{ slot.summary.concurrency }}</span>
                  <span class="profile-chip">总结批 {{ slot.summary.summary_batch_size }}</span>
                  <span class="profile-chip">复检 {{ (slot.summary.recheck_version || 'v3').toUpperCase() }}</span>
                </div>
              </template>
              <p v-else class="text-xs mt-1.5 leading-relaxed" style="color: var(--win-text-muted)">
                尚未存入方案，可将当前表单的整套连接与分析参数快照至此槽位。
              </p>
            </div>
          </div>

          <div class="mt-3 pt-2 flex items-center justify-between gap-1.5 flex-wrap" style="border-top: 1px solid var(--win-stroke)">
            <span v-if="slot.populated && slot.updated_at" class="text-xs" style="color: var(--win-text-muted)">
              更新于 {{ formatUpdatedAt(slot.updated_at) }}
            </span>
            <span v-else class="text-xs" style="color: var(--win-text-muted)">{{ slot.id }}</span>

            <div class="flex items-center gap-1.5 ml-auto">
              <template v-if="slot.populated">
                <button
                  type="button"
                  :data-profile-apply="slot.id"
                  :disabled="busy || (slot.is_active && !hasUnsavedChanges)"
                  @click="emit('apply', slot)"
                  :class="slot.is_active ? 'glass-button btn-sm' : 'glass-button glass-button-primary btn-sm'"
                >
                  {{ slot.is_active && !hasUnsavedChanges ? '已生效' : '一键切换' }}
                </button>
                <button
                  type="button"
                  :data-profile-save="slot.id"
                  :disabled="busy || profiles.store_corrupted"
                  @click="openSaveModal(slot)"
                  class="glass-button btn-sm"
                >
                  更新
                </button>
                <button
                  type="button"
                  :data-profile-clear="slot.id"
                  :disabled="busy || profiles.store_corrupted"
                  @click="emit('clear', slot)"
                  class="glass-button btn-sm"
                  style="color: var(--win-danger)"
                >
                  清空
                </button>
              </template>
              <button
                v-else
                type="button"
                :data-profile-save="slot.id"
                :disabled="busy || profiles.store_corrupted"
                @click="openSaveModal(slot)"
                class="glass-button btn-sm"
              >
                存入此槽
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 存入自定义槽位弹窗（Win11 ContentDialog 风格，纯色遮罩，保留原生 Tab 顺序） -->
    <Transition name="modal">
      <div
        v-if="saveModalSlot"
        class="profile-modal-mask"
        @mousedown.self.prevent
        @click.self="closeSaveModal()"
      >
        <form
          class="profile-modal-card glass-elevated"
          role="dialog"
          aria-modal="true"
          aria-label="存入自定义配置槽"
          @keydown.esc.stop.prevent="closeSaveModal()"
          @submit.prevent="submitSave"
        >
          <div class="profile-modal-body space-y-3">
            <div>
              <h3 class="profile-modal-title">存入 {{ saveModalSlot.id }}</h3>
              <p class="text-xs mt-1" style="color: var(--win-text-secondary)">
                将当前表单参数存入该槽位，不会立即改动当前已生效的磁盘配置。
              </p>
            </div>

            <div class="space-y-1">
              <label class="text-xs font-medium" style="color: var(--win-text-secondary)" for="profile-slot-name">
                方案名称（必填，最多 40 字）
              </label>
              <input
                id="profile-slot-name"
                ref="nameInputRef"
                v-model="modalName"
                maxlength="40"
                class="glass-input w-full"
                placeholder="例如：日常 256K + GPT6"
                @input="onModalFieldInput"
              />
            </div>

            <div class="space-y-1">
              <label class="text-xs font-medium" style="color: var(--win-text-secondary)" for="profile-slot-desc">
                备注说明（选填，最多 160 字）
              </label>
              <input
                id="profile-slot-desc"
                v-model="modalDescription"
                maxlength="160"
                class="glass-input w-full"
                placeholder="例如：主分析用 M3.1，复检走独立端点"
                @input="onModalFieldInput"
              />
            </div>

            <label class="flex items-start gap-2 text-xs cursor-pointer select-none pt-1" style="color: var(--win-text-secondary)">
              <input
                v-model="modalIncludeCredentials"
                type="checkbox"
                class="mt-0.5 shrink-0"
                @change="onModalFieldInput"
              />
              <span>
                一并存入本地 API Key（含复检 Key）
                <span class="block mt-0.5" style="color: var(--win-text-muted)">
                  不勾选时仅保存参数与模型，切换时沿用当前 Key；环境变量密钥永远不会落盘。
                </span>
              </span>
            </label>

            <div v-if="localModalError || saveModalError" class="text-xs glass-tinted-red px-2.5 py-1.5 rounded whitespace-pre-wrap" style="color: var(--win-danger)">
              {{ localModalError || saveModalError }}
            </div>
          </div>

          <div class="profile-modal-actions">
            <button type="button" :disabled="busy" @click="closeSaveModal()" class="glass-button">
              取消
            </button>
            <button type="submit" :disabled="busy" class="glass-button glass-button-primary">
              {{ busy ? '处理中...' : '确认存入' }}
            </button>
          </div>
        </form>
      </div>
    </Transition>
  </section>
</template>

<style scoped>
.settings-profiles-card {
  container-type: inline-size;
  min-width: 0;
}

.profiles-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 12px;
}

@container (min-width: 760px) {
  .profiles-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

.profile-slot-card {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 12px;
  min-width: 0;
  transition:
    border-color var(--win-duration-fast) var(--win-ease),
    background var(--win-duration-fast) var(--win-ease);
}

.profile-slot-card.is-slot-active {
  border-color: var(--win-accent);
  background: var(--win-accent-soft);
}

.profile-slot-card.is-slot-empty {
  border-style: dashed;
}

.profile-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.profile-chip {
  display: inline-flex;
  align-items: center;
  padding: 2px 6px;
  font-size: 11px;
  border-radius: var(--win-radius-control);
  background: var(--win-layer);
  border: 1px solid var(--win-stroke);
  color: var(--win-text-secondary);
}

.btn-sm {
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
}

.profile-modal-mask {
  position: fixed;
  inset: 0;
  z-index: 55;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.35);
}

html.dark .profile-modal-mask {
  background: rgba(0, 0, 0, 0.55);
}

.profile-modal-card {
  max-width: 440px;
  width: calc(100% - 32px);
  border-radius: var(--win-radius-container);
  overflow: hidden;
  background-clip: padding-box;
  box-shadow: var(--win-shadow-dialog);
}

.profile-modal-body {
  padding: 20px 24px 16px;
}

.profile-modal-title {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.01em;
  line-height: 1.3;
  margin: 0;
  color: var(--win-text-primary);
}

.profile-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 24px 16px;
  background: var(--win-control-alt);
  border-top: 1px solid var(--win-stroke);
}
</style>
