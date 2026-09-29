<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { api, type SplitterPreview } from '../api/client'
import {
  baseName, splitterStatusText, appendFileUnique, rollbackProcessing,
  groupChaptersByVolume, type SplitterFileItem,
} from '../utils/splitterView'

type FileItem = SplitterFileItem
const statusText = splitterStatusText
type BatchResult = {
  total: number
  success: number
  fail: number
  results: { file: string; book: string; total_chapters: number; ok: boolean; error: string }[]
}

const files = ref<FileItem[]>([])
const opts = reactive({
  mode: 'auto' as 'auto' | 'custom',
  // 自定义模式下的章节正则（空 = 让后端走自动检测，避免用户用错的 pattern 导致整本书被切成一章）
  pattern: '',
  use_volume: true,
  min_words: 0,
  merge_tiny: false,
  max_words: 0,
  remove_ads: false,
})

const busy = ref(false)
const preview = ref<SplitterPreview | null>(null)
const previewTitle = ref('')
const batchResult = ref<BatchResult | null>(null)
const errorMsg = ref('')

// baseName / statusText / 去重追加 / F-2 回滚 / 卷分组：纯函数本体在 utils/splitterView.ts（可独立 vitest）

function addFileByPath(p: string) {
  const r = appendFileUnique(files.value, p)
  if (r.added) files.value = r.items
}

// 文件选择：桌面窗口（pywebview）走后端 create_file_dialog 拿真实本地路径
// （Windows WebView2 的 <input type=file> 不给 File 对象注入 path 属性，原实现选完文件无反应）；
// 浏览器环境 fallback 到原生 input（FileReader 等场景由后续功能处理）。
const fileInput = ref<HTMLInputElement | null>(null)
async function triggerAddFiles() {
  const wv = (window as unknown as { pywebview?: { api?: { pick_files?: () => Promise<string[] | null> } } }).pywebview
  if (wv?.api?.pick_files) {
    try {
      const paths = await wv.api.pick_files()
      if (paths && paths.length) paths.forEach(addFileByPath)
    } catch (e) {
      errorMsg.value = (e as Error).message
    }
    return
  }
  fileInput.value?.click()
}
function onFilesPicked(e: Event) {
  const input = e.target as HTMLInputElement
  const list = input.files
  if (list && list.length) {
    for (const f of Array.from(list)) {
      const p = (f as unknown as { path?: string }).path
      if (p) addFileByPath(p)
    }
  }
  input.value = '' // 允许重复选择同一文件
}
function removeFile(idx: number) {
  files.value.splice(idx, 1)
}
function clearFiles() {
  files.value = []
  batchResult.value = null
}

async function inferAll() {
  if (!files.value.length) return
  for (const f of files.value) {
    try {
      const r = await api.inferBookName(f.file_path)
      f.book_name = r.book_name
    } catch (e) {
      /* 忽略单本推断失败 */
    }
  }
}

function previewBody(filePath: string) {
  return {
    file_path: filePath,
    mode: opts.mode,
    pattern: opts.pattern,
    use_volume: opts.use_volume,
    min_words: Number(opts.min_words) || 0,
    merge_tiny: opts.merge_tiny,
    max_words: Number(opts.max_words) || 0,
    remove_ads: opts.remove_ads,
  }
}

async function previewOne(item: FileItem) {
  errorMsg.value = ''
  try {
    preview.value = await api.previewSplit(previewBody(item.file_path))
    previewTitle.value = item.book_name
  } catch (e) {
    errorMsg.value = (e as Error).message
  }
}

async function doBatch() {
  if (!files.value.length) return
  busy.value = true
  batchResult.value = null
  errorMsg.value = ''
  files.value.forEach((f) => {
    f.status = 'processing'
    f.error = ''
  })
  try {
    const res = await api.batchSplit({
      file_paths: files.value.map((f) => f.file_path),
      book_names: files.value.map((f) => f.book_name),
      mode: opts.mode,
      pattern: opts.pattern,
      use_volume: opts.use_volume,
      min_words: Number(opts.min_words) || 0,
      merge_tiny: opts.merge_tiny,
      max_words: Number(opts.max_words) || 0,
      remove_ads: opts.remove_ads,
    })
    batchResult.value = res
    for (const r of res.results) {
      const it = files.value.find((f) => f.file_path === r.file)
      if (!it) continue
      it.status = r.ok ? 'done' : 'error'
      it.chapters = r.total_chapters
      it.error = r.error
    }
  } catch (e) {
    errorMsg.value = (e as Error).message
    // 整体失败（网络断/后端崩溃）：所有行恢复"待处理"，否则永久显示"处理中"卡死 UI
    // （F-2，回滚逻辑在 utils/splitterView.rollbackProcessing）
    files.value = rollbackProcessing(files.value)
  } finally {
    busy.value = false
  }
}

// 按卷分组展示（分组逻辑在 utils/splitterView.groupChaptersByVolume）
const groupedChapters = computed(() => {
  if (!preview.value) return []
  return groupChaptersByVolume(preview.value.chapters)
})
</script>

<template>
  <div class="space-y-6 max-w-3xl">
    <h2 class="section-title">小说切分</h2>
    <p class="section-subtitle">自动识别卷/章/回/节、番外、序章等结构，支持广告清理、去重与超大章拆分 · 可一次选择多本批量切分</p>

    <div v-if="errorMsg" class="glass-tinted-red px-4 py-2 rounded text-sm">{{ errorMsg }}</div>

    <!-- 文件列表 -->
    <div class="glass-card p-4 space-y-3">
      <div class="flex items-center justify-between" style="border-bottom: 1px solid var(--win-stroke); padding-bottom: 8px">
        <h3 class="font-semibold">
          小说文件列表
          <span class="text-sm" style="color: var(--win-text-secondary)">({{ files.length }})</span>
        </h3>
        <div class="flex gap-2">
          <button @click="triggerAddFiles" class="glass-button">+ 添加文件（可多选）</button>
          <button @click="clearFiles" :disabled="!files.length" class="glass-button">清空</button>
          <input ref="fileInput" type="file" accept=".txt,.docx" multiple style="display:none" @change="onFilesPicked" />
        </div>
      </div>

      <div v-if="!files.length" class="text-sm py-6 text-center" style="color: var(--win-text-disabled)">
        尚未添加小说。点击「添加文件」可一次选择多本 txt，每本将切分到 workspace/书名/blocks/。
      </div>

      <div v-else class="space-y-2">
        <div
          v-for="(f, idx) in files"
          :key="f.file_path"
          class="flex items-center gap-2 px-2 py-2 rounded"
          style="border: 1px solid var(--win-stroke)"
        >
          <div class="min-w-0 flex-1">
            <div class="truncate text-sm" :title="f.file_path">{{ baseName(f.file_path) || f.file_path }}</div>
            <input v-model="f.book_name" placeholder="书名" class="glass-input mt-1 w-full text-xs" :disabled="busy" />
          </div>
          <span
            class="text-xs px-2 py-0.5 rounded shrink-0"
            :class="{
              'st-pending': f.status === 'pending',
              'st-processing': f.status === 'processing',
              'st-done': f.status === 'done',
              'st-error': f.status === 'error',
            }"
            >{{ statusText(f.status) }}<span v-if="f.status === 'done'"> · {{ f.chapters }}章</span></span
          >
          <button @click="previewOne(f)" :disabled="busy" class="glass-button text-xs shrink-0">预览</button>
          <button @click="removeFile(idx)" :disabled="busy" class="glass-button text-xs shrink-0" style="color: var(--win-danger)">移除</button>
        </div>
      </div>
    </div>

    <!-- 切分模式 -->
    <div class="glass-card p-4 space-y-3">
      <h3 class="card-head">切分模式</h3>
      <div class="seg">
        <button :class="{ active: opts.mode === 'auto' }" @click="opts.mode = 'auto'">自动检测</button>
        <button :class="{ active: opts.mode === 'custom' }" @click="opts.mode = 'custom'">自定义正则</button>
      </div>
      <div v-if="opts.mode === 'custom'" class="flex items-center gap-2">
        <label class="w-20 text-sm" style="color: var(--win-text-secondary)">章节正则:</label>
        <input v-model="opts.pattern" class="glass-input flex-1 font-mono text-xs" />
      </div>
      <p v-else class="text-xs" style="color: var(--win-text-disabled)">
        自动扫描正文前 800 行，对多套候选正则打分，选出本书最优匹配（支持 第X章/回/节、序章/楔子/番外/尾声、英文 Chapter、数字编号等）
      </p>
      <p v-if="preview" class="text-xs" style="color: var(--win-text-secondary)">
        {{ previewTitle }} 检测到的正则：<code class="font-mono">{{ preview.pattern_name }}</code>
        <span class="opacity-60"> — {{ preview.detected_pattern }}</span>
      </p>
    </div>

    <!-- 选项 -->
    <div class="glass-card p-4 space-y-3">
      <h3 class="card-head">切分选项</h3>
      <div class="opt-row">
        <span class="text-sm" style="color: var(--win-text-secondary)">识别卷 / 部 / 篇层级</span>
        <label class="switch"><input type="checkbox" v-model="opts.use_volume" /><span class="slider"></span></label>
      </div>
      <div class="opt-row">
        <span class="text-sm" style="color: var(--win-text-secondary)">清理广告 / 水印行</span>
        <label class="switch"><input type="checkbox" v-model="opts.remove_ads" /><span class="slider"></span></label>
      </div>
      <div class="opt-row">
        <span class="text-sm" style="color: var(--win-text-secondary)">最小字数（短于此值跳过）</span>
        <input v-model.number="opts.min_words" type="number" min="0" class="glass-input w-28" />
      </div>
      <div class="opt-row" :style="{ opacity: opts.min_words > 0 ? 1 : 0.45 }">
        <span class="text-sm" style="color: var(--win-text-secondary)">
          合并短章到下一章
          <span class="opacity-60" v-if="opts.min_words === 0">（需设置最小字数）</span>
        </span>
        <label class="switch">
          <input type="checkbox" v-model="opts.merge_tiny" :disabled="opts.min_words === 0" />
          <span class="slider"></span>
        </label>
      </div>
      <div class="opt-row">
        <span class="text-sm" style="color: var(--win-text-secondary)">最大字数（超出自动拆分，0=不拆）</span>
        <input v-model.number="opts.max_words" type="number" min="0" class="glass-input w-28" />
      </div>
    </div>

    <!-- 操作 -->
    <div class="flex gap-2 flex-wrap">
      <button @click="doBatch" :disabled="busy || !files.length" class="glass-button glass-button-primary">
        批量切分（{{ files.length }} 本）
      </button>
      <button @click="inferAll" :disabled="busy || !files.length" class="glass-button">为全部推断书名</button>
      <span v-if="busy" class="text-sm self-center" style="color: var(--win-text-secondary)">处理中…</span>
    </div>

    <!-- 批量结果 -->
    <div v-if="batchResult" class="glass-card p-4 space-y-2">
      <h3 class="card-head">批量结果</h3>
      <div class="text-sm">
        共 {{ batchResult.total }} 本 —
        成功 <b style="color: var(--win-success)">{{ batchResult.success }}</b> ·
        失败 <b style="color: var(--win-danger)">{{ batchResult.fail }}</b>
      </div>
      <div class="space-y-1">
        <div
          v-for="r in batchResult.results"
          :key="r.file"
          class="text-xs flex justify-between gap-3"
          :style="{ color: r.ok ? 'var(--win-text-secondary)' : 'var(--win-danger)' }"
        >
          <span class="truncate" :title="r.file">{{ r.book || baseName(r.file) }}</span>
          <span class="shrink-0">{{ r.ok ? r.total_chapters + ' 章' : '失败: ' + r.error }}</span>
        </div>
      </div>
    </div>

    <!-- 单本预览 -->
    <div v-if="preview" class="glass-card overflow-hidden">
      <div class="px-4 py-3" style="border-bottom: 1px solid var(--win-stroke)">
        <div class="flex flex-wrap gap-x-4 gap-y-1 text-sm">
          <span>{{ previewTitle }}</span>
          <span>共 <b>{{ preview.total_chapters }}</b> 章</span>
          <span><b>{{ preview.total_words.toLocaleString() }}</b> 字</span>
          <span v-if="preview.total_volumes > 0">{{ preview.total_volumes }} 卷</span>
          <span v-if="preview.dedup_count > 0" style="color: var(--win-warning)">去重 {{ preview.dedup_count }} 章</span>
        </div>
        <div v-if="preview.metadata?.title || preview.metadata?.author" class="text-xs mt-1" style="color: var(--win-text-secondary)">
          <span v-if="preview.metadata.title">书名：{{ preview.metadata.title }}</span>
          <span v-if="preview.metadata.author" class="ml-3">作者：{{ preview.metadata.author }}</span>
        </div>
        <div class="text-xs mt-1" style="color: var(--win-text-disabled)">
          字数 平均 {{ preview.stats.avg }} / 最小 {{ preview.stats.min }} / 最大 {{ preview.stats.max }} / 中位 {{ preview.stats.median }}
        </div>
      </div>
      <div class="max-h-[50vh] overflow-auto">
        <table class="glass-table" style="font-size: 12px; width: 100%">
          <thead>
            <tr>
              <th class="px-2 py-1 text-left w-12">序号</th>
              <th class="px-2 py-1 text-left">标题</th>
              <th class="px-2 py-1 text-left w-16">字数</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="g in groupedChapters" :key="g.volume">
              <tr v-if="g.volume" class="volume-row">
                <td :colspan="3" class="px-2 py-1 font-medium" style="background: var(--win-control-alt); color: var(--win-text-secondary)">{{ g.volume }}</td>
              </tr>
              <tr v-for="ch in g.items" :key="ch.index" style="border-top: 1px solid var(--win-stroke)">
                <td class="px-2 py-1 text-tertiary">{{ ch.index }}</td>
                <td class="px-2 py-1">{{ ch.title }}</td>
                <td class="px-2 py-1 text-right" style="color: var(--win-text-secondary)">{{ ch.word_count.toLocaleString() }}</td>
              </tr>
            </template>
          </tbody>
        </table>
        <div v-if="preview.total_chapters > preview.preview_count" class="px-3 py-2 text-center text-xs" style="color: var(--win-text-disabled)">
          仅显示前 {{ preview.preview_count }} 章，共 {{ preview.total_chapters }} 章
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.card-head {
  font-weight: 600;
  letter-spacing: -0.01em;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--win-stroke);
}
.opt-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.seg {
  display: inline-flex;
  border-radius: var(--win-radius-control);
  /* 4px 对齐 4 倍数（原 2px 不合规） */
  padding: 4px;
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
  /* 4px 对齐 4 倍数（原 2px 不合规） */
  gap: 4px;
}
.seg button {
  border: none;
  background: transparent;
  color: var(--win-text-secondary);
  /* 8px 12px 对齐 4 倍数（原 6px 14px 不合规） */
  padding: 4px 12px;
  border-radius: var(--win-radius-control);
  cursor: pointer;
  font-size: 13px;
  transition: background var(--win-duration-fast) var(--win-ease), color var(--win-duration-fast) var(--win-ease);
}
.seg button.active {
  background: var(--win-layer);
  color: var(--win-text-primary);
  box-shadow: var(--win-shadow-control);
}
/* Win11 ToggleSwitch：40×20，滑块 12px，开启态强调色 */
.switch { position: relative; display: inline-block; width: 40px; height: 20px; flex: none; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider {
  position: absolute; inset: 0; cursor: pointer;
  background: var(--win-text-disabled);
  border-radius: 999px; transition: background var(--win-duration-fast) var(--win-ease);
}
.slider:before {
  content: ''; position: absolute; height: 12px; width: 12px; left: 4px; top: 4px;
  background: #fff; border-radius: 50%; transition: transform var(--win-duration-fast) var(--win-ease);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
}
.switch input:checked + .slider { background: var(--win-accent); }
.switch input:checked + .slider:before { transform: translateX(20px); }
.switch input:disabled + .slider { opacity: 0.45; cursor: not-allowed; }
.volume-row td { font-size: 11px; letter-spacing: 0.02em; }
.text-tertiary { color: var(--win-text-disabled); }
/* 文件状态色（走系统色 token，亮暗主题自适应） */
.st-pending { color: var(--win-text-secondary); }
.st-processing { color: var(--win-accent); }
.st-done { color: var(--win-success); }
.st-error { color: var(--win-danger); }
</style>
