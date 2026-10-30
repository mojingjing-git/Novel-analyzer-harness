<template>
  <div class="mx-auto max-w-5xl p-6">
    <header class="mb-4 flex items-baseline justify-between">
      <h1 class="text-lg font-semibold">新版数据空间（vnext）</h1>
      <div class="flex gap-2">
        <button class="glass-button px-3 py-1 text-sm" :disabled="busy" @click="refresh">刷新</button>
        <button class="glass-button px-3 py-1 text-sm" :disabled="busy" @click="showCreate = !showCreate">
          {{ showCreate ? '收起' : '新建项目' }}
        </button>
      </div>
    </header>

    <p class="mb-4 text-sm text-[var(--win-text-secondary)]">
      只读 + 入队/移出。旧版 <code>workspace/</code> 不会被读取——两个数据空间不跨读。
      <strong>本版不执行分析</strong>：入队只是登记，不会开始跑。
    </p>

    <div v-if="error" class="glass-card mb-4 border-l-4 border-red-500 p-3 text-sm">
      <div>{{ error }}</div>
      <div v-if="errorCode" class="mt-1 font-mono text-xs opacity-70">{{ errorCode }}</div>
    </div>
    <div v-if="notice" class="glass-card mb-4 border-l-4 border-emerald-500 p-3 text-sm">{{ notice }}</div>

    <section v-if="showCreate" class="glass-card mb-4 p-4">
      <h2 class="mb-2 text-sm font-medium">新建项目</h2>
      <label class="mb-2 block text-sm">
        书名
        <input v-model="form.title" class="mt-1 w-full rounded border px-2 py-1" placeholder="例如：测试书" />
      </label>
      <label class="mb-2 block text-sm">
        原文（整本粘贴，用同一套切章器切分）
        <textarea v-model="form.text" rows="6" class="mt-1 w-full rounded border px-2 py-1 font-mono text-xs" />
      </label>
      <p class="mb-2 text-xs text-[var(--win-text-secondary)]">
        没有章节标题的文本会被整篇切成 1 章（这是既有行为，不是错误）。
      </p>
      <button class="glass-button px-3 py-1 text-sm" :disabled="busy || !form.title.trim()" @click="submitCreate">
        创建
      </button>
    </section>

    <section class="glass-card mb-4 p-4">
      <h2 class="mb-2 text-sm font-medium">可打开的项目（{{ books.length }}）</h2>
      <p v-if="!books.length" class="text-sm text-[var(--win-text-secondary)]">
        还没有 vnext 项目。用上面的「新建项目」从原文创建（旧目录不会自动迁移）。
      </p>
      <table v-else class="w-full text-sm">
        <thead class="text-left text-[var(--win-text-secondary)]">
          <tr><th class="py-1">书名</th><th>章节数</th><th>book_id</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="book in books" :key="book.book_id">
            <td class="py-1">{{ book.title }}</td>
            <td>{{ book.chapter_count }}</td>
            <td class="font-mono text-xs">{{ book.book_id }}</td>
            <td class="text-right">
              <button class="glass-button px-2 py-0.5 text-xs" :disabled="busy" @click="enqueue(book.book_id)">
                入队
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="glass-card mb-4 p-4">
      <h2 class="mb-2 text-sm font-medium">队列（{{ entries.length }}）</h2>
      <p v-if="!entries.length" class="text-sm text-[var(--win-text-secondary)]">队列为空。</p>
      <table v-else class="w-full text-sm">
        <thead class="text-left text-[var(--win-text-secondary)]">
          <tr><th class="py-1">书名</th><th>状态</th><th>块进度</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="entry in entries" :key="entry.book_id">
            <td class="py-1">{{ entry.name }}</td>
            <td>{{ entry.status }}</td>
            <td>{{ entry.completed_blocks }}/{{ entry.total_blocks }}</td>
            <td class="text-right">
              <button class="glass-button px-2 py-0.5 text-xs" :disabled="busy" @click="dequeue(entry.book_id)">
                移出
              </button>
            </td>
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
import { onMounted, reactive, ref } from 'vue'
import {
  api,
  vnextErrorCode,
  vnextErrorText,
  type VnextBook,
  type VnextQueueEntry,
  type VnextRejected,
} from '../api/client'

const books = ref<VnextBook[]>([])
const rejected = ref<VnextRejected[]>([])
const entries = ref<VnextQueueEntry[]>([])
const busy = ref(false)
const error = ref('')
const errorCode = ref('')
const notice = ref('')
const showCreate = ref(false)
const form = reactive({ title: '', text: '' })

function fail(err: unknown) {
  const code = vnextErrorCode(err)
  errorCode.value = code
  error.value = vnextErrorText(code) || (err instanceof Error ? err.message : String(err))
}

async function refresh() {
  busy.value = true
  error.value = ''
  errorCode.value = ''
  try {
    const [bookPayload, queuePayload] = await Promise.all([api.vnextBooks(), api.vnextQueue()])
    books.value = bookPayload.books ?? []
    rejected.value = bookPayload.rejected ?? []
    entries.value = queuePayload.entries ?? []
  } catch (err) {
    fail(err)
  } finally {
    busy.value = false
  }
}

async function submitCreate() {
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const created = await api.vnextCreateProject({ title: form.title.trim(), text: form.text })
    notice.value = `已创建「${created.title}」（${created.chapter_count} 章）`
    form.title = ''
    form.text = ''
    showCreate.value = false
    await refresh()
  } catch (err) {
    fail(err)
  } finally {
    busy.value = false
  }
}

async function enqueue(bookId: string) {
  busy.value = true
  error.value = ''
  try {
    const queue = await api.vnextEnqueue(bookId)
    entries.value = queue.entries ?? []
    notice.value = '已入队（本版不会开始分析）'
  } catch (err) {
    fail(err)
  } finally {
    busy.value = false
  }
}

async function dequeue(bookId: string) {
  busy.value = true
  error.value = ''
  try {
    const queue = await api.vnextDequeue(bookId)
    entries.value = queue.entries ?? []
    notice.value = '已移出队列'
  } catch (err) {
    fail(err)
  } finally {
    busy.value = false
  }
}

onMounted(refresh)
</script>
