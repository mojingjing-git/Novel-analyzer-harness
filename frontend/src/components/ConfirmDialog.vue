<script setup lang="ts">
defineProps<{
  title?: string
  message: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
}>()

const emit = defineEmits<{
  confirm: []
  cancel: []
}>()

function onConfirm() { emit('confirm') }
function onCancel() { emit('cancel') }
</script>

<template>
  <Transition name="modal">
    <div class="confirm-mask" @click.self="onCancel">
      <div class="confirm-card glass-elevated">
        <div class="confirm-body">
          <h3 v-if="title" class="confirm-title">{{ title }}</h3>
          <p class="confirm-message">{{ message }}</p>
        </div>
        <div class="confirm-actions">
          <button @click="onCancel" class="glass-button">{{ cancelText || '取消' }}</button>
          <button
            @click="onConfirm"
            :class="danger ? 'glass-button glass-button-danger' : 'glass-button glass-button-primary'"
          >{{ confirmText || '确认' }}</button>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
/* Win11 ContentDialog：浅色/深色半透背板 + 8px 圆角 + In-app Acrylic 浮层卡片 */
.confirm-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  /* Win11 标准 Dialog 遮罩：柔和半透，不再嵌套 blur，由 card 独立呈现 Acrylic 亚克力材质 */
  background: rgba(0, 0, 0, 0.35);
  transition: opacity var(--win-duration-normal) var(--win-ease);
}
html.dark .confirm-mask {
  background: rgba(0, 0, 0, 0.55);
}

.confirm-card {
  max-width: 380px;
  width: calc(100% - 32px);
  /* Win11 ContentDialog 标准圆角 */
  border-radius: 8px;
  overflow: hidden;
  background-clip: padding-box;
  /* Win11 Dialog 阴影：flyout 之上，更远更软 */
  box-shadow: var(--win-shadow-dialog);
}

.confirm-body {
  padding: 24px 24px 20px;
}

/* Win11 ContentDialog 标题：20px / 600 / 负字距 */
.confirm-title {
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.01em;
  line-height: 1.3;
  margin: 0;
  color: var(--win-text-primary);
}

/* Win11 Body：14px / secondary 文字，行高 1.5 */
.confirm-message {
  font-size: 14px;
  color: var(--win-text-secondary);
  line-height: 1.5;
  /* 12px 0 0 对齐 4 倍数（原 10px 0 0 不合规） */
  margin: 12px 0 0;
}

.confirm-actions {
  display: flex;
  gap: 8px;
  padding: 8px 16px 16px;
  justify-content: flex-end;
}
</style>
