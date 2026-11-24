<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api, type BookInfo } from '../api/client'
import { useSelectedBook } from '../composables/useSelectedBook'
import Icon from './Icon.vue'

const props = withDefaults(defineProps<{
  modelValue: string
  syncGlobal?: boolean
}>(), {
  syncGlobal: true,
})
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const books = ref<BookInfo[]>([])
const loading = ref(false)
const { selectedBook, setSelectedBook } = useSelectedBook()
let isInitialLoad = true

async function load(manual = false) {
  loading.value = true
  try {
    const data = await api.listBooks()
    books.value = data
    const exists = (id: string) => Boolean(id) && data.some((b) => b.id === id)
    if (isInitialLoad && !manual) {
      isInitialLoad = false
      // 支持 ?book=<book_id> 查询参数作为初始选中（预览/验收用途，不影响默认行为）
      const q = new URLSearchParams(window.location.search).get('book') || ''
      if (!props.modelValue) {
        if (q && exists(q)) {
          if (props.syncGlobal) setSelectedBook(q)
          emit('update:modelValue', q)
        } else if (props.syncGlobal && exists(selectedBook.value)) {
          emit('update:modelValue', selectedBook.value)
        } else if (props.syncGlobal && selectedBook.value && !exists(selectedBook.value)) {
          setSelectedBook('')
        }
      } else if (!exists(props.modelValue)) {
        if (props.syncGlobal) setSelectedBook('')
        emit('update:modelValue', '')
      }
    } else {
      isInitialLoad = false
      if (props.modelValue && !exists(props.modelValue)) {
        if (props.syncGlobal) setSelectedBook('')
        emit('update:modelValue', '')
      }
    }
  }
  catch (e) { console.error('加载书目失败:', e) }
  finally { loading.value = false }
}

function onChange(e: Event) {
  const val = (e.target as HTMLSelectElement).value
  if (props.syncGlobal) setSelectedBook(val)
  emit('update:modelValue', val)
}

onMounted(() => { void load(false) })
</script>

<template>
  <div class="book-selector">
    <select :value="modelValue" @change="onChange" class="glass-select flex-1">
      <option value="">选择书目...</option>
      <option v-for="b in books" :key="b.id" :value="b.id">
        {{ b.name }} ({{ b.total_chapters }} 章){{ b.has_report ? ' ✓已总结' : '' }}{{ b.has_atlas ? ' ✓图谱' : '' }}
      </option>
    </select>
    <button @click="load(true)" :disabled="loading" class="glass-button" title="刷新书目列表">
      <span v-if="loading" class="win-spinner"></span>
      <Icon v-else name="refresh" :size="13" />
    </button>
  </div>
</template>

<style scoped>
.book-selector {
  display: flex;
  align-items: center;
  gap: 8px;
}
.win-spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 1.6px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>