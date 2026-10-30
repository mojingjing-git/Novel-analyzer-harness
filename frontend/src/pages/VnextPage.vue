<template>
  <div class="mx-auto max-w-5xl p-6">
    <header class="mb-4 flex items-baseline justify-between">
      <h1 class="text-lg font-semibold">新版数据空间（vnext）</h1>
      <button class="glass-button px-3 py-1 text-sm" :disabled="loading" @click="refresh">
        {{ loading ? '读取中…' : '刷新' }}
      </button>
    </header>

    <p class="mb-4 text-sm text-[var(--win-text-secondary)]">
      只读视图：列出 <code>workspace_vnext/</code> 里格式合法的项目，以及被拒绝的目录及原因。
      旧版 <code>workspace/</code> 不会被读取——两个数据空间不跨读。
    </p>

    <div v-if="error" class="glass-card mb-4 border-l-4 border-red-500 p-3 text-sm">{{ error }}</div>

    <section class="glass-card mb-4 p-4">
      <h2 class="mb-2 text-sm font-medium">可打开的项目（{{ books.length }}）</h2>
      <p v-if="!books.length" class="text-sm text-[var(--win-text-secondary)]">
        还没有 vnext 项目。请从原文创建新项目（旧目录不会自动迁移）。
      </p>
      <table v-else class="w-full text-sm">
        <thead class="text-left text-[var(--win-text-secondary)]">
          <tr><th class="py-1">书名</th><th>章节数</th><th>book_id</th></tr>
        </thead>
        <tbody>
          <tr v-for="book in books" :key="book.book_id">
            <td class="py-1">{{ book.name }}</td>
            <td>{{ book.total_chapters }}</td>
            <td class="font-mono text-xs">{{ book.book_id }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="rejected.length" class="glass-card p-4">
      <h2 class="mb-2 text-sm font-medium">被拒绝的目录（{{ rejected.length }}）</h2>
      <table class="w-full text-sm">
        <thead class="text-left text-[var(--win-text-secondary)]">
          <tr><th class="py-1">目录</th><th>原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in rejected" :key="item.path">
            <td class="py-1 font-mono text-xs">{{ item.path }}</td>
            <td>{{ item.reason }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api/client'

interface VnextBook { book_id: string; name: string; total_chapters: number }
interface VnextRejected { path: string; reason: string }

const books = ref<VnextBook[]>([])
const rejected = ref<VnextRejected[]>([])
const loading = ref(false)
const error = ref('')

async function refresh() {
  loading.value = true
  error.value = ''
  try {
    const payload = await api.vnextBooks()
    books.value = payload.books ?? []
    rejected.value = payload.rejected ?? []
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>
