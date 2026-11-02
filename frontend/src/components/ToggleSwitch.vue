<script setup lang="ts">
/**
 * Windows 11 Fluent Design ToggleSwitch (40px × 20px)
 * 交互：胶囊轨道 + 12px 实心拇指 + 按下微拉伸弹性动效
 */
interface Props {
  modelValue: boolean
  disabled?: boolean
  label?: string
}

const props = withDefaults(defineProps<Props>(), {
  modelValue: false,
  disabled: false,
  label: '',
})

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  change: [value: boolean]
}>()

function toggle() {
  if (props.disabled) return
  const next = !props.modelValue
  emit('update:modelValue', next)
  emit('change', next)
}
</script>

<template>
  <label class="win-toggle-wrap" :class="{ 'is-disabled': disabled }">
    <button
      type="button"
      role="switch"
      class="win-toggle"
      :class="{ 'is-checked': modelValue }"
      :aria-checked="modelValue"
      :disabled="disabled"
      @click="toggle"
    >
      <span class="win-toggle-thumb" />
    </button>
    <span v-if="label || $slots.default" class="win-toggle-label">
      <slot>{{ label }}</slot>
    </span>
  </label>
</template>

<style scoped>
.win-toggle-wrap {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  user-select: none;
  font-size: var(--win-font-body, 14px);
  color: var(--win-text-primary);
}
.win-toggle-wrap.is-disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

/* 40px × 20px Win11 胶囊轨道 */
.win-toggle {
  position: relative;
  width: 40px;
  height: 20px;
  border-radius: 10px;
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke-strong);
  padding: 0;
  margin: 0;
  cursor: pointer;
  outline: none;
  flex-shrink: 0;
  transition:
    background-color var(--win-duration-fast) var(--win-ease),
    border-color var(--win-duration-fast) var(--win-ease);
}
.win-toggle:hover:not(:disabled) {
  background: var(--win-control-hover);
}
.win-toggle:focus-visible {
  outline: 2px solid var(--win-accent);
  outline-offset: 2px;
}

/* 选中态（Accent Blue 背景，无外边框） */
.win-toggle.is-checked {
  background: var(--win-accent);
  border-color: transparent;
}
.win-toggle.is-checked:hover:not(:disabled) {
  background: var(--win-accent-hover);
}
.win-toggle.is-checked:active:not(:disabled) {
  background: var(--win-accent-pressed);
}

/* 12px 实心拇指 */
.win-toggle-thumb {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 12px;
  height: 12px;
  border-radius: 6px;
  background: var(--win-text-secondary);
  transition:
    transform var(--win-duration-normal) var(--win-ease),
    width 100ms var(--win-ease),
    background-color var(--win-duration-fast) var(--win-ease);
}
.win-toggle:hover:not(:disabled) .win-toggle-thumb {
  background: var(--win-text-primary);
}

/* 按下时的横向微拉伸（Win11 标志性弹性动效） */
.win-toggle:active:not(:disabled) .win-toggle-thumb {
  width: 14px;
}

/* 选中态位置平移 20px，拇指变白 */
.win-toggle.is-checked .win-toggle-thumb {
  transform: translateX(20px);
  background: #FFFFFF;
}
.win-toggle.is-checked:active:not(:disabled) .win-toggle-thumb {
  transform: translateX(18px);
  width: 14px;
}

.win-toggle:disabled {
  cursor: not-allowed;
}

.win-toggle-label {
  line-height: 1.4;
}
</style>
