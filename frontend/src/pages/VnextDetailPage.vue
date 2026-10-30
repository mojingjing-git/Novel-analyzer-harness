<script setup lang="ts">
/**
 * vnext 项目详情（B3）：块清单 + 删除。
 *
 * 这一页刻意只读 + 一个危险操作：
 * - 块的三种状态要能区分（`done` / `stale:<原因>` / `missing:<原因>`），因为
 *   "需要重算"和"从没算过"在用户眼里是两件事，处理方式也不同；
 * - 删除要输入完整 `book_id`——删书不可撤销，按一下回车就删的风险太高。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, vnextErrorCode, vnextErrorText } from '../api/client'

interface BlockRow {
  block_signature: string
  status: string
}

const route = useRoute()
const router = useRouter()
const bookId = computed(() => String(route.params.bookId ?? ''))

const blocks = ref<BlockRow[]>([])
const loading = ref(false)
const error = ref('')
const errorCode = ref('')
const confirmText = ref('')
const deleting = ref(false)

const doneCount = computed(() => blocks.value.filter((b) => b.status === 'done').length)
const staleCount = computed(() => blocks.value.filter((b) => b.status.startsWith('stale')).length)
const missingCount = computed(() => blocks.value.filter((b) => b.status.startsWith('missing')).length)

function statusLabel(status: string): string {
  if (status === 'done') return '已完成'
  if (status.startsWith('stale')) return `需重算（${status.slice(6) || '依赖变化'}）`
  if (status.startsWith('missing')) return `未分析（${status.slice(8) || '无结果'}）`
  return status
}

function statusClass(status: string): string {
  if (status === 'done') return 'text-[var(--win-success,#0f7b0f)]'
  if (status.startsWith('stale')) return 'text-[var(--win-warning,#9d5d00)]'
  return 'text-[var(--win-text-secondary)]'
}

async function load() {
  if (!bookId.value) return
  loading.value = true
  error.value = ''
  errorCode.value = ''
  try {
    const payload = await api.vnextBlocks(bookId.value)
    blocks.value = payload.blocks ?? []
  } catch (e) {
    error.value = vnextErrorText(vnextErrorCode(e)) || (e as Error).message
    errorCode.value = vnextErrorCode(e)
  } finally {
    loading.value = false
  }
}

async function remove() {
  if (confirmText.value !== bookId.value) {
    error.value = '请原样输入 book_id 后再删除'
    return
  }
  deleting.value = true
  try {
    await api.vnextDeleteBook(bookId.value, confirmText.value)
    router.push('/vnext')
  } catch (e) {
    error.value = vnextErrorText(vnextErrorCode(e)) || (e as Error).message
    errorCode.value = vnextErrorCode(e)
  } finally {
    deleting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="mx-auto max-w-4xl p-6 space-y-6">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-xl font-semibold">项目详情</h1>
        <p class="mt-1 text-sm text-[var(--win-text-secondary)]">
          {{ bookId }} · 共 {{ blocks.length }} 块
          <span class="ml-2">已完成 {{ doneCount }}</span>
          <span class="ml-2">需重算 {{ staleCount }}</span>
          <span class="ml-2">未分析 {{ missingCount }}</span>
        </p>
      </div>
      <button class="glass-button" @click="router.push('/vnext')">返回</button>
    </header>

    <p v-if="error" class="text-sm text-[var(--win-danger,#c42b1c)]">
      {{ error }}<span v-if="errorCode" class="ml-1 opacity-60">({{ errorCode }})</span>
    </p>

    <section class="glass-card p-4">
      <div v-if="loading" class="text-sm text-[var(--win-text-secondary)]">加载中…</div>
      <table v-else-if="blocks.length" class="w-full text-sm">
        <thead>
          <tr class="text-left text-[var(--win-text-secondary)]">
            <th class="py-1">块</th>
            <th class="py-1">状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(block, index) in blocks" :key="block.block_signature">
            <td class="py-1 font-mono text-xs">{{ index + 1 }} · {{ block.block_signature.slice(0, 12) }}</td>
            <td class="py-1" :class="statusClass(block.status)">{{ statusLabel(block.status) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="text-sm text-[var(--win-text-secondary)]">还没有块（章节数不足以切块）。</p>
    </section>

    <section class="glass-card p-4 space-y-2">
      <h2 class="text-sm font-semibold">删除这个项目</h2>
      <p class="text-xs text-[var(--win-text-secondary)]">
        不可撤销。请原样输入 <code class="font-mono">{{ bookId }}</code> 以确认。
      </p>
      <div class="flex gap-2">
        <input v-model="confirmText" class="glass-input flex-1" :placeholder="bookId" />
        <button class="glass-button" :disabled="deleting" @click="remove">
          {{ deleting ? '删除中…' : '删除' }}
        </button>
      </div>
    </section>
  </div>
</template>
