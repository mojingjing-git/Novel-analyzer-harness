<script setup lang="ts">
// 递归渲染章节分析结果（白底卡片内的结构化内容）
withDefaults(
  defineProps<{ value: unknown; depth?: number }>(),
  { depth: 0 }
)

// 常见嵌套字段的中文标签，让报告更易读
const KEY_LABELS: Record<string, string> = {
  id: '编号',
  event: '事件',
  characters: '涉及人物',
  function: '作用',
  name: '名称',
  surface_action: '表层行动',
  inner_motivation: '内在动机',
  clue: '线索',
  type: '类型',
  implication: '暗示',
  summary: '小结',
  unresolved_questions: '未解问题',
  new_leads: '新线索',
  contextual_link: '上下文关联',
  timeline: '时间线',
  world_building: '世界观',
  thematic_elements: '主题元素',
  pattern: '模式',
  foreshadowing_network: '伏笔网络',
  pacing: '节奏',
  long_term_arcs: '长期弧线',
  description: '描述',
  parent: '上级',
  relation: '关系',
  from: '起点',
  to: '终点',
}

function isObject(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}
function isArray(v: unknown): v is unknown[] {
  return Array.isArray(v)
}
function isScalar(v: unknown): boolean {
  return v === null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean'
}
function objectEntries(v: Record<string, unknown>): [string, unknown][] {
  return Object.entries(v)
}
function keyLabel(k: string): string {
  return KEY_LABELS[k] || k
}
</script>

<template>
  <span v-if="isScalar(value)" class="cv-scalar">{{ value === null ? '—' : String(value) }}</span>

  <ul
    v-else-if="isArray(value)"
    class="cv-list"
    :class="{ 'cv-cards': depth === 0 && value.length > 0 && isObject(value[0]) }"
  >
    <li
      v-for="(item, i) in value"
      :key="i"
      class="cv-item"
      :class="{ 'cv-card': depth === 0 && isObject(value[0]) }"
    >
      <ChapterValue :value="item" :depth="depth + 1" />
    </li>
  </ul>

  <div v-else-if="isObject(value)" class="cv-object">
    <div v-for="[k, v] in objectEntries(value)" :key="k" class="cv-field">
      <span class="cv-key">{{ keyLabel(k) }}</span>
      <ChapterValue :value="v" :depth="depth + 1" />
    </div>
  </div>
</template>

<style scoped>
.cv-scalar {
  font-size: 13px;
  line-height: 1.65;
  color: var(--win-text-primary);
  white-space: pre-wrap;
  word-break: break-word;
}
.cv-list {
  margin: 4px 0;
  padding-left: 16px;
}
.cv-list.cv-cards {
  list-style: none;
  padding-left: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.cv-item {
  margin: 4px 0;
  line-height: 1.5;
}
.cv-card {
  background: var(--win-layer);
  border: 1px solid var(--win-stroke);
  border-radius: var(--win-radius-container);
  /* 12px 12px 对齐 4 倍数（原 10px 12px 不合规） */
  padding: 12px;
  box-shadow: var(--win-shadow-control);
  /* C 档动效：cv-card 微 hover */
  transition:
    background var(--win-duration-fast) var(--win-ease),
    border-color var(--win-duration-fast) var(--win-ease),
    transform var(--win-duration-fast) var(--win-ease);
}
.cv-card:hover {
  background: var(--win-control-hover);
  border-color: var(--win-stroke-strong);
  transform: translateX(2px);
}
.cv-object {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.cv-field {
  font-size: 13px;
  line-height: 1.65;
}
.cv-key {
  display: inline-block;
  font-weight: 500;
  color: var(--win-text-secondary);
  margin-right: 4px;
}
.cv-key::after {
  content: '：';
  font-weight: 400;
}
</style>
