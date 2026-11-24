<script setup lang="ts">
import { ref, onMounted, inject } from 'vue'
import { routerKey } from 'vue-router'
import { api, type WorkspaceNovel, type WorkspaceArchive } from '../api/client'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { fmtSize, fmtTime, suggestRestoreName } from '../utils/workspaceView'

const router = inject(routerKey, null)

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
  hasBlocks: boolean
  blocksCount: number
}

const novels = ref<NovelRow[]>([])
const archives = ref<ArchiveRow[]>([])
const loading = ref(false)
const error = ref('')
const notice = ref('')
const restoredSuccess = ref(false)

// 统一确认弹窗状态（替代浏览器原生 confirm()）
const confirmState = ref<{ title: string; message: string; confirmText: string; danger: boolean; action: () => Promise<void> } | null>(null)
function askConfirm(title: string, message: string, confirmText: string, danger: boolean, action: () => Promise<void>) {
  notice.value = ''
  restoredSuccess.value = false
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

// 还原切分弹窗状态
const restoreState = ref<{
  archiveName: string
  targetName: string
  busy: boolean
  dialogError: string
} | null>(null)

function openRestore(archiveName: string) {
  notice.value = ''
  restoredSuccess.value = false
  restoreState.value = {
    archiveName,
    targetName: suggestRestoreName(
      archiveName,
      novels.value.map((n) => n.name),
      archives.value.map((a) => a.name),
    ),
    busy: false,
    dialogError: '',
  }
}

async function submitRestore() {
  const st = restoreState.value
  if (!st || st.busy) return
  const target = st.targetName.trim()
  if (!target) {
    st.dialogError = '请输入还原到工作区的目录名'
    return
  }
  st.busy = true
  st.dialogError = ''
  try {
    const res = await api.restoreArchiveBlocks(st.archiveName, target)
    restoreState.value = null
    notice.value = `已将「${st.archiveName}」的 ${res.blocks_count} 章切分还原至 workspace/${res.novel_name}，并已加入待分析队列`
    restoredSuccess.value = true
    await load()
  } catch (e) {
    st.dialogError = (e as Error).message || String(e)
  } finally {
    st.busy = false
  }
}

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
      hasBlocks: x.has_blocks !== false,
      blocksCount: x.blocks_count ?? 0,
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
    </div>

    <div v-if="error" class="glass-tinted-red px-4 py-2 rounded text-sm">{{ error }}</div>
    <div v-if="notice" class="glass-tinted-green px-4 py-2 rounded text-sm flex flex-wrap items-center justify-between gap-2">
      <span>{{ notice }}</span>
      <button
        v-if="restoredSuccess"
        type="button"
        @click="router?.push('/')"
        class="glass-button text-xs shrink-0"
      >前往分析队列</button>
    </div>

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
        暂无小说
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
          class="ws-row flex items-center justify-between px-2 gap-3"
          style="border-bottom: 1px solid var(--win-stroke)"
        >
          <div class="min-w-0">
            <div class="truncate font-medium">{{ a.name }}</div>
            <div class="text-xs" style="color: var(--win-text-secondary)">
              <template v-if="a.blocksCount">{{ a.blocksCount }} 章切分 · </template>{{ fmtSize(a.size) }} · 修改于 {{ fmtTime(a.modified) }}
            </div>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            <button
              @click="openRestore(a.name)"
              :disabled="!a.hasBlocks"
              :title="a.hasBlocks ? '仅复制该归档的 blocks/ 切分章节到工作区（不含旧分析结果），便于换模型重跑对比' : '该归档不包含 blocks 切分目录'"
              class="glass-button"
            >
              还原切分
            </button>
            <button @click="delArchive(a.name)" class="glass-button glass-button-danger">删除</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 还原切分弹窗 -->
    <Transition name="modal">
      <div
        v-if="restoreState"
        class="restore-mask"
        @click.self="!restoreState.busy && (restoreState = null)"
      >
        <div class="restore-card glass-elevated">
          <div class="restore-body space-y-3">
            <h3 class="restore-title">还原切分到工作区</h3>
            <p class="restore-desc">
              将归档「{{ restoreState.archiveName }}」的 <code class="text-xs">blocks/</code> 原文复制到工作区。
            </p>
            <div class="space-y-1">
              <label class="text-xs" style="color: var(--win-text-secondary)">工作区目标目录名：</label>
              <input
                v-model="restoreState.targetName"
                type="text"
                class="glass-input w-full"
                placeholder="输入工作区目录名"
                :disabled="restoreState.busy"
                @keydown.enter="submitRestore"
              />
            </div>
            <p v-if="restoreState.dialogError" class="glass-tinted-red px-3 py-2 rounded text-xs">
              {{ restoreState.dialogError }}
            </p>
          </div>
          <div class="restore-actions">
            <button
              @click="restoreState = null"
              :disabled="restoreState.busy"
              class="glass-button"
            >取消</button>
            <button
              @click="submitRestore"
              :disabled="restoreState.busy"
              class="glass-button glass-button-primary"
            >{{ restoreState.busy ? '还原中...' : '确认还原' }}</button>
          </div>
        </div>
      </div>
    </Transition>

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
  /* 8px 12px 对齐 4 倍数 */
  padding: 8px 12px;
  transition: background var(--win-duration-fast) var(--win-ease);
}
.ws-row:hover {
  background: var(--win-control-hover);
}

.restore-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.35);
}
html.dark .restore-mask {
  background: rgba(0, 0, 0, 0.55);
}
.restore-card {
  max-width: 440px;
  width: calc(100% - 32px);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: var(--win-shadow-dialog);
}
.restore-body {
  padding: 24px 24px 16px;
}
.restore-title {
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.01em;
  line-height: 1.3;
  margin: 0;
  color: var(--win-text-primary);
}
.restore-desc {
  font-size: 13px;
  color: var(--win-text-secondary);
  line-height: 1.5;
  margin: 0;
}
.restore-actions {
  display: flex;
  gap: 8px;
  padding: 8px 16px 16px;
  justify-content: flex-end;
}
</style>
