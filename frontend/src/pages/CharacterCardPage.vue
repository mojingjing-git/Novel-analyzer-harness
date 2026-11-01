<script setup lang="ts">
import { ref, watch } from 'vue'
import BookSelector from '../components/BookSelector.vue'
import { api } from '../api/client'

interface CharSummary { name: string; first_appearance?: number; total_events: number; chapters_count: number }
interface CharArc { chapter: number; surface_action: string; inner_motivation: string; change_delta: string; driver: string }
interface CharEvent { chapter: number; event: string; function: string }
interface CharRel { first_encounter: number; joint_events: { chapter: number; event: string; function: string }[]; event_count: number }

const bookId = ref('')
const characters = ref<CharSummary[]>([])
const loadingChars = ref(false)
const card = ref<{
  name: string
  first_appearance?: number | null
  chapters: number[]
  total_events: number
  arcs: CharArc[]
  events: CharEvent[]
  states: Record<string, string>
  relationships: Record<string, CharRel>
} | null>(null)
const loadingCard = ref(false)
const selectedName = ref('')
const cardError = ref('')

const listError = ref('')

// 请求序号：角色列表与 viewCard(cardSeq) 同构，防止 A 书慢返回覆盖 B 书列表
let listSeq = 0

async function loadCharacters() {
  const seq = ++listSeq
  characters.value = []
  card.value = null
  selectedName.value = ''
  listError.value = ''
  if (!bookId.value) return
  loadingChars.value = true
  try {
    const res = await api.getBookCharacters(bookId.value)
    if (seq !== listSeq) return
    characters.value = (res.characters as CharSummary[]) || []
  } catch (e) {
    if (seq !== listSeq) return
    console.error(e)
    listError.value = '加载角色列表失败: ' + (e as Error).message
  } finally {
    if (seq === listSeq) loadingChars.value = false
  }
}

// 请求序号：跨书/快速切卡时丢弃过期响应，防止 A 书的慢请求覆盖 B 书卡片
let cardSeq = 0

async function viewCard(name: string) {
  if (!bookId.value) return
  const seq = ++cardSeq
  selectedName.value = name
  card.value = null
  cardError.value = ''
  loadingCard.value = true
  try {
    const res = await api.getCharacterCard(bookId.value, name)
    if (seq !== cardSeq) return
    card.value = res.character as any
  } catch (e) {
    if (seq !== cardSeq) return
    cardError.value = '加载角色卡失败: ' + (e as Error).message
  } finally {
    if (seq === cardSeq) loadingCard.value = false
  }
}

// 把 [1,2,3,5,6,8] 压缩成 "1-3、5-6、8"
function formatChapters(chapters: number[]): string {
  if (!chapters || !chapters.length) return '无'
  const sorted = [...chapters].sort((a, b) => a - b)
  const ranges: string[] = []
  let start = sorted[0]
  let prev = sorted[0]
  for (let i = 1; i <= sorted.length; i++) {
    const cur = sorted[i]
    if (cur === prev + 1) {
      prev = cur
      continue
    }
    ranges.push(start === prev ? `第${start}章` : `第${start}-${prev}章`)
    start = cur
    prev = cur as number
  }
  return ranges.join('、')
}

function sortedStates(states: Record<string, string>): { chapter: number; state: string }[] {
  if (!states) return []
  return Object.entries(states)
    .map(([k, v]) => ({ chapter: Number(k), state: v }))
    .sort((a, b) => a.chapter - b.chapter)
}

function topRelationships(rels: Record<string, CharRel>, n = 6): { name: string; rel: CharRel }[] {
  if (!rels) return []
  return Object.entries(rels)
    .map(([name, rel]) => ({ name, rel }))
    .sort((a, b) => b.rel.event_count - a.rel.event_count)
    .slice(0, n)
}

watch(bookId, loadCharacters)
</script>

<template>
  <div class="space-y-6">
    <h2 class="section-title">角色卡</h2>
    <p class="section-subtitle">从聚合数据中查看每个角色的出场、弧光、事件与关系网。</p>

    <BookSelector v-model="bookId" />

    <div v-if="listError" class="glass-tinted-red px-4 py-2 rounded text-sm">{{ listError }}</div>

    <!-- 角色列表 -->
    <div v-if="loadingChars" class="text-sm" style="color: var(--win-text-disabled)">加载角色列表中…</div>
    <div v-else-if="characters.length" class="flex gap-2 flex-wrap">
      <button
        v-for="c in characters"
        :key="c.name"
        @click="viewCard(c.name)"
        class="glass-button"
        :class="{ 'glass-button-primary': selectedName === c.name }"
      >
        {{ c.name }}
        <span class="ml-1 opacity-70 text-xs">{{ c.total_events }} 事件</span>
      </button>
    </div>
    <div v-else-if="bookId" class="text-sm" style="color: var(--win-text-disabled)">
      该书目暂无角色数据（请先运行「数据聚合」生成 character_tracking 聚合文件）。
    </div>

    <!-- 角色卡详情 -->
    <div v-if="loadingCard" class="glass-card"><div class="text-sm" style="color: var(--win-text-disabled)">生成角色卡中…</div></div>
    <div v-else-if="cardError" class="glass-card" style="color: var(--win-danger)">{{ cardError }}</div>

    <div v-else-if="card" class="glass-card space-y-5">
      <div class="flex items-baseline justify-between">
        <h3 class="text-lg font-semibold" style="color: var(--win-text-primary)">{{ card.name }}</h3>
        <div class="text-xs" style="color: var(--win-text-disabled)">
          首次出现：第{{ card.first_appearance ?? '未知' }}章 · 共 {{ card.total_events }} 事件
        </div>
      </div>

      <!-- 基本信息 -->
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div class="glass-stat">
          <div class="glass-stat-label">首次出现</div>
          <div class="glass-stat-value">第{{ card.first_appearance ?? '—' }}章</div>
        </div>
        <div class="glass-stat">
          <div class="glass-stat-label">出场章节</div>
          <div class="glass-stat-value">{{ card.chapters.length }} 章</div>
        </div>
        <div class="glass-stat">
          <div class="glass-stat-label">参与事件</div>
          <div class="glass-stat-value">{{ card.total_events }}</div>
        </div>
        <div class="glass-stat">
          <div class="glass-stat-label">关系数</div>
          <div class="glass-stat-value">{{ Object.keys(card.relationships || {}).length }}</div>
        </div>
      </div>

      <!-- 出场章节 -->
      <div v-if="card.chapters.length">
        <div class="block-title">出场章节</div>
        <div class="text-sm" style="color: var(--win-text-primary)">{{ formatChapters(card.chapters) }}</div>
      </div>

      <!-- 人物弧光 -->
      <div v-if="card.arcs.length">
        <div class="block-title">人物弧光（{{ card.arcs.length }}）</div>
        <div class="space-y-2">
          <div v-for="arc in card.arcs" :key="arc.chapter" class="arc-item">
            <div class="font-medium" style="color: var(--win-text-primary)">第{{ arc.chapter }}章</div>
            <div class="text-xs mt-1" style="color: var(--win-text-secondary)">
              <span class="arc-key">表面行为</span>{{ arc.surface_action }}
            </div>
            <div class="text-xs mt-0.5" style="color: var(--win-text-secondary)">
              <span class="arc-key">内在动机</span>{{ arc.inner_motivation }}
            </div>
            <div class="text-xs mt-0.5" style="color: var(--win-text-secondary)">
              <span class="arc-key">变化幅度</span>{{ arc.change_delta }}
              <span class="arc-key ml-2">驱动</span>{{ arc.driver }}
            </div>
          </div>
        </div>
      </div>

      <!-- 主要事件 -->
      <div v-if="card.events.length">
        <div class="block-title">主要事件（{{ card.events.length }}）</div>
        <div class="space-y-1">
          <div v-for="(ev, i) in card.events" :key="i" class="event-item">
            <span class="text-xs" style="color: var(--win-accent)">第{{ ev.chapter }}章</span>
            <span class="text-sm ml-2" style="color: var(--win-text-primary)">{{ ev.event }}</span>
            <span class="text-xs ml-2 opacity-70" style="color: var(--win-text-disabled)">功能：{{ ev.function }}</span>
          </div>
        </div>
      </div>

      <!-- 状态演变 -->
      <div v-if="sortedStates(card.states).length">
        <div class="block-title">状态演变</div>
        <div class="space-y-0.5">
          <div v-for="s in sortedStates(card.states)" :key="s.chapter" class="text-xs" style="color: var(--win-text-secondary)">
            <span class="opacity-70">第{{ s.chapter }}章：</span>{{ s.state }}
          </div>
        </div>
      </div>

      <!-- 人际关系 -->
      <div v-if="topRelationships(card.relationships).length">
        <div class="block-title">人际关系</div>
        <div class="space-y-2">
          <div v-for="r in topRelationships(card.relationships)" :key="r.name" class="rel-item">
            <div class="flex items-baseline justify-between">
              <span class="font-medium" style="color: var(--win-text-primary)">{{ r.name }}</span>
              <span class="text-xs" style="color: var(--win-text-disabled)">共同事件 {{ r.rel.event_count }} 个</span>
            </div>
            <div class="text-xs mt-1 opacity-80" style="color: var(--win-text-secondary)">
              首次相遇：第{{ r.rel.first_encounter }}章
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.block-title {
  font-size: var(--win-font-caption);
  font-weight: 600;
  color: var(--win-text-secondary);
  padding-bottom: 6px;
  margin-bottom: 8px;
  border-bottom: 1px solid var(--win-stroke);
}
.arc-item, .rel-item {
  background: var(--win-control-alt);
  border: 1px solid var(--win-stroke);
  border-left: 3px solid var(--win-accent);
  border-radius: var(--win-radius-container);
  /* 12px 对齐 4 倍数（原 10px 12px 不合规） */
  padding: 12px;
  /* C 档动效：卡片内嵌 item 微 hover */
  transition:
    background var(--win-duration-fast) var(--win-ease),
    border-color var(--win-duration-fast) var(--win-ease),
    transform var(--win-duration-fast) var(--win-ease);
}
.arc-item:hover, .rel-item:hover {
  background: var(--win-control-hover);
  border-color: var(--win-stroke-strong);
  border-left-color: var(--win-accent-hover);
  transform: translateX(2px);
}
.event-item {
  border-left: 3px solid var(--win-warning);
  padding-left: 8px;
}
.arc-key {
  display: inline-block;
  min-width: 56px;
  color: var(--win-text-disabled);
}
</style>
