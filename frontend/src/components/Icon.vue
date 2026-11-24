<script setup lang="ts">
/**
 * 线性 SVG 图标库（替代 emoji）
 * 风格：1.2px stroke / 24×24 / 圆角端点（近 Segoe Fluent 单线）
 */
import { computed } from 'vue'

interface Props {
  name: string
  size?: number | string
  strokeWidth?: number
  filled?: boolean
}
const props = withDefaults(defineProps<Props>(), {
  size: 18,
  strokeWidth: 1.5,
  filled: false,
})

// SVG 路径数据（来自 Phosphor Icons 的 line 变体，简化版）
const ICONS: Record<string, string> = {
  // 分析队列 - 任务列表
  queue: '<rect x="4" y="5" width="16" height="14" rx="2.5"/><line x1="8" y1="10" x2="16" y2="10"/><line x1="8" y1="14" x2="14" y2="14"/><circle cx="6" cy="10" r="0.6" fill="currentColor" stroke="none"/><circle cx="6" cy="14" r="0.6" fill="currentColor" stroke="none"/>',
  // 设置 - 齿轮
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>',
  // 章节详情 - 书
  chapter: '<path d="M4 5a2 2 0 0 1 2-2h12v17H6a2 2 0 0 0-2 2V5z"/><path d="M18 20H6a2 2 0 0 1 0-4h12"/>',
  // 数据聚合 - 链接
  aggregate: '<path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1.5-1.5"/>',
  // 角色卡 - 用户
  characters: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  // 最终总结 - 文档
  summary: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="14 3 14 9 20 9"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/>',
  // 小说切分 - 剪刀
  splitter: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/>',
  // 工作区 - 文件夹
  workspace: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/>',
  // Prompt预览 - 对话气泡
  prompt: '<path d="M21 12a8 8 0 0 1-11.5 7.2L4 21l1.8-5.5A8 8 0 1 1 21 12z"/>',
  // 风格分析 - 调色盘
  style: '<circle cx="12" cy="12" r="9"/><circle cx="8" cy="9" r="1" fill="currentColor" stroke="none"/><circle cx="16" cy="9" r="1" fill="currentColor" stroke="none"/><circle cx="9" cy="15" r="1" fill="currentColor" stroke="none"/><circle cx="15" cy="15" r="1" fill="currentColor" stroke="none"/>',
  // 统计面板 - 柱状图
  stats: '<line x1="4" y1="20" x2="20" y2="20"/><rect x="6" y="13" width="3" height="7"/><rect x="11" y="9" width="3" height="11"/><rect x="16" y="5" width="3" height="15"/>',
  // 时间线 - 时钟
  timeline: '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/>',
  // 角色关系图 - 网络
  graph: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><line x1="8" y1="7" x2="11" y2="16"/><line x1="16" y1="7" x2="13" y2="16"/><line x1="8" y1="6" x2="16" y2="6"/>',
  // 地图 - 定位针
  map: '<path d="M12 22s-8-7-8-13a8 8 0 0 1 16 0c0 6-8 13-8 13z"/><circle cx="12" cy="9" r="2.5"/>',
  // 世界舞台 - 地球经纬线
  world: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14.5 14.5 0 0 1 0 18"/><path d="M12 3a14.5 14.5 0 0 0 0 18"/>',
  // 通用图标
  search: '<circle cx="11" cy="11" r="7"/><line x1="16" y1="16" x2="21" y2="21"/>',
  play: '<polygon points="6 4 20 12 6 20 6 4"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="1"/>',
  refresh: '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
  arrow_up: '<line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>',
  arrow_down: '<line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
  sun: '<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.05" y2="7.05"/><line x1="16.95" y1="16.95" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.05" y2="16.95"/><line x1="16.95" y1="7.05" x2="19.07" y2="4.93"/>',
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="14 3 14 9 20 9"/>',
  trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  info: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><circle cx="12" cy="8" r="0.5" fill="currentColor" stroke="none"/>',
  warn: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><circle cx="12" cy="17" r="0.5" fill="currentColor" stroke="none"/>',
  error: '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  filter: '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
  minus: '<line x1="5" y1="12" x2="19" y2="12"/>',
  square: '<rect x="5" y="5" width="14" height="14" rx="1.5"/>',
  maximize: '<rect x="5" y="5" width="14" height="14" rx="1.5"/>',
  // 还原 - 后方方块 + 前方方块叠加（Win11 标准 restore 视觉）
  restore: '<rect x="9" y="9" width="11" height="11" rx="1.5"/><rect x="4" y="4" width="11" height="11" rx="1.5"/>',
}

// Windows 11 Fluent 激活填充态变体（未登记的图标平滑回退至描边线框）
const FILLED_ICONS: Record<string, string> = {
  queue: '<path d="M6.5 4h11A2.5 2.5 0 0 1 20 6.5v11a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 17.5v-11A2.5 2.5 0 0 1 6.5 4z" fill="currentColor" stroke="none"/><path d="M8 10h8M8 14h6" stroke="var(--win-layer, #fff)" stroke-width="1.8" stroke-linecap="round"/><circle cx="6.2" cy="10" r="0.8" fill="var(--win-layer, #fff)" stroke="none"/><circle cx="6.2" cy="14" r="0.8" fill="var(--win-layer, #fff)" stroke="none"/>',
  settings: '<path d="M12 1a2 2 0 0 1 2 2v.2a7.9 7.9 0 0 1 1.9.8l.2-.2a2 2 0 0 1 2.8 0l1.4 1.4a2 2 0 0 1 0 2.8l-.2.2c.4.6.6 1.2.8 1.9h.2a2 2 0 0 1 2 2v2a2 2 0 0 1-2 2h-.2a7.9 7.9 0 0 1-.8 1.9l.2.2a2 2 0 0 1 0 2.8l-1.4 1.4a2 2 0 0 1-2.8 0l-.2-.2a7.9 7.9 0 0 1-1.9.8v.2a2 2 0 0 1-2 2h-2a2 2 0 0 1-2-2v-.2a7.9 7.9 0 0 1-1.9-.8l-.2.2a2 2 0 0 1-2.8 0l-1.4-1.4a2 2 0 0 1 0-2.8l.2-.2a7.9 7.9 0 0 1-.8-1.9H3a2 2 0 0 1-2-2v-2a2 2 0 0 1 2-2h.2c.2-.7.4-1.3.8-1.9l-.2-.2a2 2 0 0 1 0-2.8l1.4-1.4a2 2 0 0 1 2.8 0l.2.2c.6-.4 1.2-.6 1.9-.8V3a2 2 0 0 1 2-2h2zm0 8a3 3 0 1 0 0 6 3 3 0 0 0 0-6z" fill="currentColor" stroke="none"/>',
  workspace: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" fill="currentColor" stroke="none"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" fill="currentColor" stroke="none"/>',
  summary: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6z" fill="currentColor" stroke="none"/><line x1="8" y1="13" x2="16" y2="13" stroke="var(--win-layer, #fff)" stroke-width="1.8" stroke-linecap="round"/><line x1="8" y1="17" x2="13" y2="17" stroke="var(--win-layer, #fff)" stroke-width="1.8" stroke-linecap="round"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6z" fill="currentColor" stroke="none"/>',
  characters: '<circle cx="12" cy="8" r="4" fill="currentColor" stroke="none"/><path d="M4 21a8 8 0 0 1 16 0z" fill="currentColor" stroke="none"/>',
  chapter: '<path d="M4 5a2 2 0 0 1 2-2h12v17H6a2 2 0 0 0-2 2V5z" fill="currentColor" stroke="none"/><path d="M18 20H6a2 2 0 0 1 0-4h12" stroke="var(--win-layer, #fff)" stroke-width="1.8"/>',
  prompt: '<path d="M21 12a8 8 0 0 1-11.5 7.2L4 21l1.8-5.5A8 8 0 1 1 21 12z" fill="currentColor" stroke="none"/>',
  stats: '<line x1="4" y1="20" x2="20" y2="20" stroke-width="2"/><rect x="6" y="13" width="3" height="7" fill="currentColor" stroke="none"/><rect x="11" y="9" width="3" height="11" fill="currentColor" stroke="none"/><rect x="16" y="5" width="3" height="15" fill="currentColor" stroke="none"/>',
  timeline: '<circle cx="12" cy="12" r="9" fill="currentColor" stroke="none"/><polyline points="12 7 12 12 15 14" stroke="var(--win-layer, #fff)" stroke-width="2" stroke-linecap="round"/>',
  map: '<path d="M12 22s-8-7-8-13a8 8 0 0 1 16 0c0 6-8 13-8 13z" fill="currentColor" stroke="none"/><circle cx="12" cy="9" r="2.5" fill="var(--win-layer, #fff)" stroke="none"/>',
  world: '<circle cx="12" cy="12" r="9" fill="currentColor" stroke="none"/><path d="M3.5 12h17" stroke="var(--win-layer, #fff)" stroke-width="1.6" fill="none"/><path d="M12 3a14 14 0 0 1 0 18" stroke="var(--win-layer, #fff)" stroke-width="1.6" fill="none"/><path d="M12 3a14 14 0 0 0 0 18" stroke="var(--win-layer, #fff)" stroke-width="1.6" fill="none"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="2" fill="currentColor" stroke="none"/>',
  play: '<polygon points="6 4 20 12 6 20 6 4" fill="currentColor" stroke="none"/>',
}

const path = computed(() => {
  if (props.filled && FILLED_ICONS[props.name]) {
    return FILLED_ICONS[props.name]
  }
  return ICONS[props.name] || ICONS.info || ''
})
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
    class="icon"
    aria-hidden="true"
    v-html="path"
  />
</template>

<style scoped>
.icon {
  display: inline-block;
  vertical-align: middle;
  flex-shrink: 0;
}
</style>