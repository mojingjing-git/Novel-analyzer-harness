<script setup lang="ts">
import { ref } from 'vue'
import BookSelector from '../components/BookSelector.vue'
import { api } from '../api/client'

const bookId = ref('')
const maxChars = ref(0)
const chapter = ref(1)
const chapterLabel = ref('')
const system = ref('')
const user = ref('')
const sysLen = ref(0)
const usrLen = ref(0)
const truncated = ref(false)
const totalChars = ref(0)
const error = ref('')

async function preview() {
  error.value = ''
  try {
    // 空输入（v-model.number 清空得 ''）会 422：发送时归一化为 0
    const res = await api.promptPreview(bookId.value, Number(maxChars.value) || 0, Number(chapter.value) || 1)
    system.value = res.system_prompt
    user.value = res.user_prompt
    sysLen.value = res.system_len
    usrLen.value = res.user_len
    truncated.value = res.chapter_truncated
    totalChars.value = res.chapter_total_chars
    chapterLabel.value = res.chapter_label
  } catch (e) { error.value = (e as Error).message }
}
</script>

<template>
  <div class="space-y-6">
    <h2 class="section-title">Prompt 预览</h2>
    <div class="flex gap-2 items-center flex-wrap">
      <div class="flex-1 min-w-[200px]"><BookSelector v-model="bookId" /></div>
      <label class="text-sm flex items-center gap-1" style="color: var(--win-text-secondary)">
        章节原文上限
        <input v-model.number="maxChars" type="number" min="0" step="500" class="glass-input" style="width: 110px" placeholder="0=不截断" />
      </label>
      <label class="text-sm flex items-center gap-1" style="color: var(--win-text-secondary)">
        章节号
        <input v-model.number="chapter" type="number" min="1" step="1" class="glass-input" style="width: 90px" placeholder="1" />
      </label>
      <button @click="preview" :disabled="!bookId" class="glass-button glass-button-primary">生成预览</button>
    </div>
    <div v-if="error" class="glass-tinted-red px-4 py-2 rounded text-sm">{{ error }}</div>
    <p v-if="totalChars > 0" class="text-xs" style="color: var(--win-text-disabled)">
      章节原文共 {{ totalChars }} 字符{{ truncated ? '（已截断预览）' : '' }}
    </p>
    <div v-if="system" class="space-y-2">
      <div class="text-sm" style="color: var(--win-text-secondary)">SYSTEM ({{ sysLen }} 字符)</div>
      <pre class="code-surface code-green p-3 overflow-auto max-h-60">{{ system }}</pre>
    </div>
    <div v-if="user" class="space-y-2">
      <div class="text-sm" style="color: var(--win-text-secondary)">USER（第{{ chapter }}章 {{ chapterLabel }} · {{ usrLen }} 字符）</div>
      <pre class="code-surface code-cyan p-3 overflow-auto max-h-60">{{ user }}</pre>
    </div>
  </div>
</template>
