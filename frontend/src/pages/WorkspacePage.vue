<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api, type WorkspaceNovel, type WorkspaceArchive } from '../api/client'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { fmtSize, fmtTime } from '../utils/workspaceView'

interface NovelRow {
  name: string
  chapters: number
  size: number
  modified: number
}
interface ArchiveRow {
  name: string
  size: number
  modified: number
}

const novels = ref<NovelRow[]>([])
const archives = ref<ArchiveRow[]>([])
const loading = ref(false)
const error = ref('')

// 统一确认弹窗状态（替代浏览器原生 confirm()）
const confirmState = ref<{ title: string; message: string; confirmText: string; danger: boolean; action: () => Promise<void> } | null>(null)
function askConfirm(title: string, message: string, confirmText: string, danger: boolean, action: () => Promise<void>) {
  confirmState.value = { title, message, confirmText, danger, action }
}
async function runConfirmed() {
  const s = confirmState.value
  confirmState.value = null
  if (!s) return
  try {
    await s.action()
    await load()
  } catch (e) {
    error.value = (e as Error).message || String(e)
  }
}

// fmtSize / fmtTime：纯函数本体在 utils/workspaceView.ts（可独立 vitest）

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [n, a] = await Promise.all([api.workspaceNovels(), api.workspaceArchives()])
    novels.value = n.novels.map((x: WorkspaceNovel) => ({
      name: x.name,
      chapters: x.blocks_count,
      size: x.dir_size,
      modified: x.dir_mtime,
    }))
    archives.value = a.archives.map((x: WorkspaceArchive) => ({
      name: x.name,
      size: x.total_size,
      modified: x.dir_mtime,
    }))
  } catch (e) {
    error.value = (e as Error).message || String(e)
  } finally {
    loading.value = false
  }
}
function archive(name: string) {
  askConfirm('归档小说', `归档《${name}》?`, '归档', false, async () => { await api.archiveNovel(name) })
}
function archiveAll() {
  askConfirm('批量归档', '批量归档所有小说?', '全部归档', false, async () => { await api.archiveAll() })
}
function delArchive(name: string) {
  askConfirm('删除归档', `删除归档《${name}》? 此操作不可恢复`, '删除', true, async () => { await api.deleteArchive(name) })
}

onMounted(load)
</script>

<template>
  <div class="space-y-6 max-w-4xl">
    <div>
      <h2 class="section-title">工作区管理</h2>
      <p class="section-subtitle">管理 workspace/ 中的小说与 分析结果/ 归档</p>
    </div>

    <div v-if="error" class="glass-tinted-red px-4 py-2 rounded text-sm">{{ error }}</div>

    <!-- 小说列表 -->
    <div class="glass-card p-4 space-y-3">
      <div class="flex items-center justify-between pb-2" style="border-bottom: 1px solid var(--win-stroke)">
        <h3 class="font-semibold">workspace/ 小说</h3>
        <button
          @click="archiveAll"
          :disabled="loading || novels.length === 0"
          class="glass-button"
        >
          批量归档
        </button>
      </div>

      <div v-if="loading" class="text-sm py-4" style="color: var(--win-text-disabled)">加载中…</div>
      <div
        v-else-if="novels.length === 0"
        class="text-sm py-4"
        style="color: var(--win-text-disabled)"
      >
        暂无小说。请先在「小说切分」切分，或把小说放入 workspace/ 目录。
      </div>
      <div v-else class="space-y-1">
        <div
          v-for="n in novels"
          :key="n.name"
          class="ws-row flex items-center justify-between px-2"
          style="border-bottom: 1px solid var(--win-stroke)"
        >
          <div class="min-w-0">
            <div class="truncate font-medium">{{ n.name }}</div>
            <div class="text-xs" style="color: var(--win-text-secondary)">
              {{ n.chapters }} 章 · {{ fmtSize(n.size) }} · 修改于 {{ fmtTime(n.modified) }}
            </div>
          </div>
          <button @click="archive(n.name)" class="glass-button glass-button-primary shrink-0">归档</button>
        </div>
      </div>
    </div>

    <!-- 归档列表 -->
    <div class="glass-card p-4 space-y-3">
      <h3 class="font-semibold pb-2" style="border-bottom: 1px solid var(--win-stroke)">分析结果/ 归档</h3>
      <div v-if="loading" class="text-sm py-4" style="color: var(--win-text-disabled)">加载中…</div>
      <div v-else-if="archives.length === 0" class="text-sm py-4" style="color: var(--win-text-disabled)">暂无归档。</div>
      <div v-else class="space-y-1 list-enter">
        <div
          v-for="a in archives"
          :key="a.name"
          class="flex items-center justify-between py-2 px-2 rounded"
          style="border-bottom: 1px solid var(--win-stroke)"
        >
          <div class="min-w-0">
            <div class="truncate font-medium">{{ a.name }}</div>
            <div class="text-xs" style="color: var(--win-text-secondary)">
              {{ fmtSize(a.size) }} · 修改于 {{ fmtTime(a.modified) }}
            </div>
          </div>
          <button @click="delArchive(a.name)" class="glass-button glass-button-danger shrink-0">删除</button>
        </div>
      </div>
    </div>

    <!-- 统一确认弹窗（玻璃材质） -->
    <ConfirmDialog
      v-if="confirmState"
      :title="confirmState.title"
      :message="confirmState.message"
      :confirm-text="confirmState.confirmText"
      :danger="confirmState.danger"
      @confirm="runConfirmed"
      @cancel="confirmState = null"
    />
  </div>
</template>

<style scoped>
.ws-row {
  min-height: 40px;
  border-radius: var(--win-radius-control);
  /* 8px 12px 对齐 4 倍数（原 6px 8px 不合规） */
  padding: 8px 12px;
  transition: background var(--win-duration-fast) var(--win-ease);
}
.ws-row:hover {
  background: var(--win-control-hover);
}
</style>
