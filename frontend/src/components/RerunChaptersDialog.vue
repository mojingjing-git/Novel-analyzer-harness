<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api, type RerunChaptersRequest, type RerunOverrides } from '../api/client'

const props = withDefaults(defineProps<{
  bookId: string
  totalChapters?: number
  hasReport?: boolean
  initialChapters?: string
  currentChapter?: number
  busy?: boolean
}>(), {
  totalChapters: 0,
  hasReport: false,
  initialChapters: '',
  currentChapter: 0,
  busy: false,
})

const emit = defineEmits<{
  confirm: [payload: RerunChaptersRequest]
  cancel: []
}>()

const chaptersInput = ref(props.initialChapters || (props.currentChapter > 0 ? String(props.currentChapter) : ''))
const showAdvanced = ref(false)
const modelOverride = ref('')
const effortOverride = ref<'' | 'none' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'>('')
const temperatureOverride = ref<string>('')
const blockSizeOverride = ref<string>('1')
const concurrencyOverride = ref<string>('')
const updateRolling = ref(false)
const availableModels = ref<string[]>([])

watch(
  () => [props.bookId, props.initialChapters, props.currentChapter],
  () => {
    chaptersInput.value = props.initialChapters || (props.currentChapter > 0 ? String(props.currentChapter) : '')
  }
)

onMounted(async () => {
  try {
    const res = await api.getModels()
    availableModels.value = res.models || []
  } catch {
    availableModels.value = []
  }
})

const parsedPreview = computed<{ valid: boolean; chapters: number[]; message: string }>(() => {
  const raw = chaptersInput.value.trim()
  if (!raw) {
    return { valid: false, chapters: [], message: '请输入要重跑的章号或区间，例如 40 或 37-40, 55' }
  }
  const tokens = raw.split(/[,，;\s]+/).filter(Boolean)
  const set = new Set<number>()
  for (const tok of tokens) {
    const rangeMatch = tok.match(/^(\d+)\s*(?:-|\.\.)\s*(\d+)$/)
    if (rangeMatch) {
      const a = Number(rangeMatch[1])
      const b = Number(rangeMatch[2])
      if (!Number.isInteger(a) || !Number.isInteger(b) || a <= 0 || b <= 0) {
        return { valid: false, chapters: [], message: `章节号须为正整数: ${tok}` }
      }
      if (a > b) {
        return { valid: false, chapters: [], message: `范围起止倒置: ${tok}` }
      }
      if (b - a + 1 > 500) {
        return { valid: false, chapters: [], message: `单段范围过大（最多 500 章）: ${tok}` }
      }
      for (let c = a; c <= b; c++) set.add(c)
      continue
    }
    if (/^\d+$/.test(tok)) {
      const n = Number(tok)
      if (n <= 0) return { valid: false, chapters: [], message: `章节号须为正整数: ${tok}` }
      set.add(n)
      continue
    }
    return { valid: false, chapters: [], message: `无法识别的章号写法: ${tok}` }
  }

  const list = [...set].sort((a, b) => a - b)
  if (!list.length) {
    return { valid: false, chapters: [], message: '请至少指定 1 章' }
  }
  if (props.totalChapters > 0) {
    const outOfRange = list.filter((c) => c > props.totalChapters)
    if (outOfRange.length) {
      return {
        valid: false,
        chapters: [],
        message: `超出全书总章数 (${props.totalChapters}): ${outOfRange.slice(0, 5).join(', ')}`,
      }
    }
  }
  const head = list.slice(0, 10).join(', ')
  const suffix = list.length > 10 ? ` … 共 ${list.length} 章` : ''
  return { valid: true, chapters: list, message: `将重跑 ${list.length} 章：${head}${suffix}` }
})

function fillCurrentChapter() {
  if (props.currentChapter > 0) {
    chaptersInput.value = String(props.currentChapter)
  }
}

function submit() {
  if (!parsedPreview.value.valid || props.busy) return
  const overrides: RerunOverrides = {}
  if (modelOverride.value.trim()) overrides.model = modelOverride.value.trim()
  if (effortOverride.value) overrides.reasoning_effort = effortOverride.value
  if (temperatureOverride.value.trim() !== '') {
    const t = Number(temperatureOverride.value)
    if (!Number.isNaN(t) && t >= 0 && t <= 2) overrides.temperature = t
  }
  if (blockSizeOverride.value.trim() !== '') {
    const bs = Math.floor(Number(blockSizeOverride.value))
    if (!Number.isNaN(bs) && bs >= 1 && bs <= 200) overrides.block_size = bs
  }
  if (concurrencyOverride.value.trim() !== '') {
    const conc = Math.floor(Number(concurrencyOverride.value))
    if (!Number.isNaN(conc) && conc >= 1 && conc <= 64) overrides.concurrency = conc
  }

  emit('confirm', {
    book_id: props.bookId,
    chapters: chaptersInput.value.trim(),
    overrides: Object.keys(overrides).length ? overrides : undefined,
    update_rolling: updateRolling.value,
  })
}
</script>

<template>
  <Transition name="modal">
    <div class="rerun-mask" @mousedown.self.prevent @click.self="$emit('cancel')">
      <div
        class="rerun-card glass-elevated"
        role="dialog"
        aria-modal="true"
        aria-label="定点重跑章节"
        @keydown.esc.stop.prevent="$emit('cancel')"
      >
        <div class="rerun-head">
          <div>
            <h3 class="rerun-title">定点重跑章节</h3>
            <p class="rerun-sub">
              《{{ bookId }}》<template v-if="totalChapters"> · 共 {{ totalChapters }} 章</template>
            </p>
          </div>
          <button
            v-if="currentChapter > 0"
            type="button"
            class="glass-button btn-chip"
            @click="fillCurrentChapter"
          >
            填当前第 {{ currentChapter }} 章
          </button>
        </div>

        <div class="rerun-body">
          <label class="field-label">
            目标章节或区间
            <input
              v-model="chaptersInput"
              type="text"
              placeholder="例如：40 或 37-40, 55"
              class="glass-input field-input"
              @keydown.enter.prevent="submit"
            />
          </label>
          <p class="preview-line" :class="parsedPreview.valid ? 'ok' : 'err'">
            {{ parsedPreview.message }}
          </p>

          <p class="tip-box">
            重跑使用当前已完成前置章节构建知识库并自动过滤未来章剧透；覆盖落盘后会自动重建全书
            <code>knowledge.json</code> 与聚合索引；若某章重跑失败，保留其原有结果文件不变。
          </p>
          <p v-if="hasReport" class="warn-box">
            该书已生成全书最终总结或伏笔账本；章节重跑完成后，既有报告与账本不会自动重算，请在「最终总结」页重新生成。
          </p>

          <button
            type="button"
            class="adv-toggle"
            @click="showAdvanced = !showAdvanced"
          >
            {{ showAdvanced ? '▾ 收起' : '▸ 展开' }}本次临时覆盖参数（不修改全局设置）
          </button>

          <div v-if="showAdvanced" class="adv-grid">
            <label class="field-label">
              临时模型（留空跟随全局）
              <input
                v-model="modelOverride"
                list="rerun-model-options"
                placeholder="跟随当前配置"
                class="glass-input field-input"
              />
              <datalist id="rerun-model-options">
                <option v-for="m in availableModels" :key="m" :value="m" />
              </datalist>
            </label>

            <label class="field-label">
              思考档位
              <select v-model="effortOverride" class="glass-select field-input">
                <option value="">跟随当前配置</option>
                <option value="none">关闭思考 (none)</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
                <option value="xhigh">xhigh</option>
                <option value="max">max</option>
              </select>
            </label>

            <label class="field-label">
              采样温度 (0~2)
              <input
                v-model="temperatureOverride"
                type="number"
                step="0.05"
                min="0"
                max="2"
                placeholder="跟随当前配置"
                class="glass-input field-input"
              />
            </label>

            <label class="field-label">
              批大小 block_size
              <input
                v-model="blockSizeOverride"
                type="number"
                min="1"
                max="200"
                placeholder="建议 1"
                class="glass-input field-input"
              />
            </label>

            <label class="field-label">
              并发数 concurrency
              <input
                v-model="concurrencyOverride"
                type="number"
                min="1"
                max="64"
                placeholder="跟随当前配置"
                class="glass-input field-input"
              />
            </label>

            <label class="check-row">
              <input v-model="updateRolling" type="checkbox" />
              <span>同步刷新 rolling_summary.json（历史章重跑默认关闭）</span>
            </label>
          </div>
        </div>

        <div class="rerun-actions">
          <button type="button" class="glass-button" @click="$emit('cancel')">取消</button>
          <button
            type="button"
            class="glass-button glass-button-primary"
            :disabled="!parsedPreview.valid || busy"
            @click="submit"
          >
            {{ busy ? '提交中...' : '开始重跑' }}
          </button>
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.rerun-mask {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.35);
  transition: opacity var(--win-duration-normal) var(--win-ease);
}
html.dark .rerun-mask {
  background: rgba(0, 0, 0, 0.55);
}
.rerun-card {
  width: min(520px, calc(100% - 32px));
  max-height: calc(100vh - 48px);
  display: flex;
  flex-direction: column;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: var(--win-shadow-dialog);
}
.rerun-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 20px 24px 12px;
  border-bottom: 1px solid var(--win-stroke);
}
.rerun-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: var(--win-text-primary);
}
.rerun-sub {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--win-text-secondary);
}
.btn-chip {
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
  white-space: nowrap;
}
.rerun-body {
  padding: 16px 24px;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.field-label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--win-text-secondary);
}
.field-input {
  width: 100%;
}
.preview-line {
  margin: 0;
  font-size: 12px;
}
.preview-line.ok {
  color: var(--win-accent);
}
.preview-line.err {
  color: var(--win-danger);
}
.tip-box,
.warn-box {
  margin: 0;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;
}
.tip-box {
  background: var(--win-surface-subtle, rgba(128, 128, 128, 0.08));
  color: var(--win-text-secondary);
}
.warn-box {
  background: var(--win-info-bg);
  color: var(--win-info);
}
.adv-toggle {
  align-self: flex-start;
  background: none;
  border: none;
  padding: 4px 0;
  font-size: 12px;
  color: var(--win-accent);
  cursor: pointer;
}
.adv-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 12px;
  padding-top: 4px;
}
.check-row {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--win-text-secondary);
  cursor: pointer;
}
.rerun-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px 16px;
  border-top: 1px solid var(--win-stroke);
}
</style>
