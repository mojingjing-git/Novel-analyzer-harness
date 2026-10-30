<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { RouterView } from 'vue-router'
import Icon from './Icon.vue'

const isDark = ref(false)
// 导航折叠：展开 280px / 收起 48px（只显示图标）
const collapsed = ref(false)
// 桌面端（pywebview）才渲染窗口控制按钮
const isDesktop = ref(false)
// 窗口最大化状态（仅桌面端，切换 maximize/restore 按钮图标）
const isMaximized = ref(false)

const navItems = [
  { path: '/', label: '分析队列', icon: 'queue' },
  { path: '/vnext', label: '新版空间', icon: 'workspace' },
  { path: '/characters', label: '角色卡', icon: 'characters' },
  { path: '/summary', label: '最终总结', icon: 'summary' },
  { path: '/splitter', label: '小说切分', icon: 'splitter' },
  { path: '/workspace', label: '工作区', icon: 'workspace' },
  { path: '/prompt', label: 'Prompt预览', icon: 'prompt' },
  { path: '/style', label: '风格分析', icon: 'style' },
  { path: '/stats', label: '统计面板', icon: 'stats' },
  { path: '/timeline', label: '时间线', icon: 'timeline' },
  { path: '/graph', label: '关系图', icon: 'graph' },
  { path: '/map', label: '地图', icon: 'map' },
  { path: '/settings', label: '设置', icon: 'settings' },
]

function toggleTheme() {
  isDark.value = !isDark.value
  if (isDark.value) document.documentElement.classList.add('dark')
  else document.documentElement.classList.remove('dark')
  localStorage.setItem('theme', isDark.value ? 'dark' : 'light')
}

// 窗口控制（桌面 pywebview）。浏览器内不存在 pywebview，按钮不渲染。
const wvApi = () => (window as unknown as { pywebview?: { api?: Record<string, unknown> } })?.pywebview?.api
function winMin() { void (wvApi()?.minimize as (() => void) | undefined)?.() }
function winMax() {
  void (wvApi()?.toggle_maximize as (() => void) | undefined)?.()
  // 乐观切换：resize 事件会同步后端真实状态做纠正
  void refreshMaximizedFromBackend()
}
function winClose() { void (wvApi()?.close as (() => void) | undefined)?.() }
function winTitlebarDblClick() {
  // frameless 模式下没有 Win32 系统标题栏，双击最大化/还原由前端接管
  winMax()
}

// 标题栏拖拽移动窗口（桌面端）：按下记录起点，移动按增量调用 pywebview move
// 注意：frameless + double-click 同时存在会冲突——浏览器对原生标题栏的双击放大是默认行为，
// 但本应用是 WebView2 + 自定义标题栏，dblclick 不会自动触发 maximize；用 Vue 的
// @dblclick 显式接管即可。
let dragState: { sx: number; sy: number; lastX: number; lastY: number } | null = null
function onTitlebarDown(e: MouseEvent) {
  const api = wvApi()
  if (!api?.move) return
  if ((e.target as HTMLElement).closest('.tb-btn')) return // 不拖拽按钮
  dragState = { sx: e.screenX, sy: e.screenY, lastX: e.screenX, lastY: e.screenY }
  window.addEventListener('mousemove', onTitlebarMove)
  window.addEventListener('mouseup', onTitlebarUp)
}
function onTitlebarMove(e: MouseEvent) {
  if (!dragState) return
  const dx = e.screenX - dragState.lastX
  const dy = e.screenY - dragState.lastY
  dragState.lastX = e.screenX
  dragState.lastY = e.screenY
  if (dx !== 0 || dy !== 0) void (wvApi()?.move as ((x: number, y: number) => void) | undefined)?.(dx, dy)
}
function onTitlebarUp() {
  dragState = null
  window.removeEventListener('mousemove', onTitlebarMove)
  window.removeEventListener('mouseup', onTitlebarUp)
}

// 同步 isMaximized：走后端 Win32 IsZoomed(hwnd) 真实查询。
// 前端启发式（innerWidth vs screen.width）会因 Aero Snap 接近全屏尺寸误判，
// 不可靠；后端用 ctypes 直查 Win32 窗口状态，是唯一权威来源。
async function refreshMaximizedFromBackend() {
  const fn = wvApi()?.is_maximized as (() => Promise<boolean> | boolean) | undefined
  if (!fn) return
  try {
    const v = await fn()
    isMaximized.value = !!v
  } catch {
    // 后端暂不可用（如窗口未就绪），保持现状
  }
}
// 窗口尺寸变化时（包括用户拖到屏幕边缘自动 max、双击 max、点按钮 max/restore
// 触发的 Win32 resize）都要同步状态。用 rAF 节流避免 resize 风暴时频繁 IPC。
let _resizeRaf = 0
function onWindowResize() {
  if (_resizeRaf) return
  _resizeRaf = requestAnimationFrame(() => {
    _resizeRaf = 0
    void refreshMaximizedFromBackend()
  })
}

onMounted(() => {
  isDesktop.value = !!wvApi()
  // P2：WebView2/pywebview 可能晚于挂载注入（官方要求监听 pywebviewready）；
  // AppLayout 为常驻布局，采样失败则窗口控制整个会话缺失。
  window.addEventListener('pywebviewready', () => {
    isDesktop.value = !!wvApi()
    void refreshMaximizedFromBackend()
  })
  // 监听窗口尺寸变化以同步 isMaximized（任何 max/restore 路径都会触发 Win32 resize）
  window.addEventListener('resize', onWindowResize)
  void refreshMaximizedFromBackend()

  if (new URLSearchParams(window.location.search).get('collapsed') === '1') {
    collapsed.value = true
  }
  const saved = localStorage.getItem('theme')
  if (saved === 'dark') {
    isDark.value = true
    document.documentElement.classList.add('dark')
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onWindowResize)
  if (_resizeRaf) cancelAnimationFrame(_resizeRaf)
})
</script>

<template>
  <div class="layout-root">
    <!-- 一体化标题栏（48px，Mica，可拖拽移动窗口，双击切换最大化） -->
    <header
      class="titlebar mica-surface"
      @mousedown="onTitlebarDown"
      @dblclick="winTitlebarDblClick"
    >
      <div class="titlebar-brand">
        <div class="logo-mark logo-mark-sm">
          <Icon name="queue" :size="16" />
        </div>
        <span class="titlebar-title">小说分析器</span>
      </div>
      <div v-if="isDesktop" class="titlebar-controls">
        <button class="tb-btn" title="最小化" @click="winMin"><Icon name="minus" :size="10" /></button>
        <button class="tb-btn" :title="isMaximized ? '还原' : '最大化'" @click="winMax">
          <Icon :name="isMaximized ? 'restore' : 'maximize'" :size="9" />
        </button>
        <button class="tb-btn tb-close" title="关闭" @click="winClose"><Icon name="x" :size="10" /></button>
      </div>
    </header>

    <div class="layout-body">
      <!-- Win11 NavigationView 侧边栏：Mica 基底（背景层），内容卡片白色浮于其上 -->
      <aside class="sidebar mica-surface" :class="{ collapsed }">
        <nav class="nav-list">
          <router-link
            v-for="item in navItems"
            :key="item.path"
            :to="item.path"
            class="nav-item"
            active-class="is-active"
            :exact-active-class="item.path === '/' ? 'is-active' : ''"
            :title="collapsed ? item.label : undefined"
          >
            <span class="nav-icon-wrap">
              <Icon :name="item.icon" :size="16" />
            </span>
            <span v-if="!collapsed" class="nav-label">{{ item.label }}</span>
          </router-link>
        </nav>

        <div class="sidebar-footer">
          <button class="nav-item foot-item" @click="collapsed = !collapsed" :title="collapsed ? '展开导航' : '收起导航'">
            <span class="nav-icon-wrap">
              <Icon :name="collapsed ? 'plus' : 'x'" :size="16" />
            </span>
            <span v-if="!collapsed" class="nav-label">折叠导航</span>
          </button>
          <button class="nav-item foot-item" @click="toggleTheme">
            <span class="nav-icon-wrap">
              <Icon :name="isDark ? 'sun' : 'moon'" :size="16" />
            </span>
            <span v-if="!collapsed" class="nav-label">{{ isDark ? '亮色' : '暗色' }}</span>
          </button>
        </div>
      </aside>

      <!-- 主内容区：Mica 基底，24px 内边距，独立滚动 -->
      <main class="main-area">
        <RouterView v-slot="{ Component, route }">
          <transition name="page" mode="out-in">
            <component :is="Component" :key="route.path" />
          </transition>
        </RouterView>
      </main>
    </div>
  </div>
</template>

<style scoped>
.layout-root {
  display: flex;
  flex-direction: column;
  height: 100%;
}

/* ===== 一体化标题栏（48px，Mica，可拖拽） =====
   frameless 模式下整个 WebView2 客户区都是我们的，标题栏底部加 1px 极淡分割线
   区分主内容区；Mica 模式下 backdrop 会透出，分割线依然可见。 */
.titlebar {
  height: 48px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  user-select: none;
  border-bottom: 1px solid var(--win-divider, rgba(0, 0, 0, 0.06));
}
html.dark .titlebar {
  border-bottom-color: rgba(255, 255, 255, 0.08);
}
.titlebar-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-left: 16px;
  min-width: 0;
  cursor: default;
}
.logo-mark-sm {
  width: 20px;
  height: 20px;
  border-radius: 5px;
}
.logo-mark {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--win-accent);
  color: #fff;
  box-shadow: var(--win-shadow-control);
  flex-shrink: 0;
}
.titlebar-title {
  font-size: 14px;
  font-weight: 400;
  color: var(--win-text-primary);
  white-space: nowrap;
}
.titlebar-controls {
  display: flex;
  height: 100%;
  margin-left: auto;
  align-items: center;
}
.tb-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 32px;
  border: none;
  background: transparent;
  color: var(--win-text-primary);
  cursor: pointer;
  transition: background var(--win-duration-fast) var(--win-ease);
}
.tb-btn:hover { background: var(--win-control-hover); }
.tb-btn:active { background: var(--win-control-pressed); }
.tb-close:hover { background: #C42B1C; color: #fff; }
.tb-close:active { background: #A3231A; color: #fff; }

/* ===== 主体 ===== */
.layout-body {
  flex: 1;
  display: flex;
  min-height: 0;
}

/* ===== 侧边栏（Win11 NavigationView，Mica 背景层） =====
   宽度 200px：Win11 默认 240px 与紧凑 72px 之间的折中下限，
   满足 12 个导航项不换行 + 折叠/主题按钮完整宽度体验 */
.sidebar {
  width: 200px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  transition: width var(--win-duration-normal) var(--win-ease);
  overflow: hidden;
  /* 与内容区仅靠 Mica 层次区分，无硬分割线；加一层极淡的色调微差 */
  background-image:
    linear-gradient(180deg, rgba(255, 255, 255, 0.03), rgba(255, 255, 255, 0.01)),
    var(--win-mica);
}
html.dark .sidebar {
  background-image:
    linear-gradient(180deg, rgba(255, 255, 255, 0.015), rgba(255, 255, 255, 0.005)),
    var(--win-mica);
}
.sidebar.collapsed {
  width: 48px;
}
.sidebar.collapsed .nav-item {
  padding: 0 6px;
  justify-content: center;
}
.sidebar.collapsed .nav-item.is-active::before {
  left: 0;
  height: 16px;
}

/* ===== 导航列表 =====
   overflow-x: clip 替代 hidden：clip 根本不创建滚动容器，
   即便 overflow-y: auto 的垂直滚动条出现（占 17px 布局宽度）也不会触发横向滚动条。
   滚动条整体隐藏（Win11 NavigationView 同款：内容超高时滚轮可滚、不显示滚动条），
   故不用 scrollbar-gutter（避免侧边栏右缘常驻 17px 滚动条槽）。 */
.nav-list {
  flex: 1;
  overflow-x: clip;
  overflow-y: auto;
  padding: 8px 8px 4px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  scrollbar-width: none;
  position: relative;
}
.nav-list::-webkit-scrollbar {
  display: none;
}
.nav-item.foot-item {
  border: none;
  background: transparent;
  font-family: inherit;
  width: 100%;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}

.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  height: 40px;
  padding: 0 12px 0 16px;
  overflow: visible;
  border-radius: var(--win-radius-control);
  font-size: 14px;
  font-weight: 400;
  color: var(--win-text-secondary);
  text-decoration: none;
  -webkit-tap-highlight-color: transparent;
  cursor: pointer;
  outline: none;
  white-space: nowrap;
  transition:
    background var(--win-duration-fast) var(--win-ease),
    color var(--win-duration-fast) var(--win-ease);
}
.nav-item:hover {
  background: var(--win-control-hover);
  color: var(--win-text-primary);
}
.nav-item:active {
  background: var(--win-control-pressed);
}

.nav-icon-wrap {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex-shrink: 0;
  color: currentColor;
}

/* 选中态：左侧 4px 强调色竖条（Selection Indicator）+ 悬停底色 */
.nav-item.is-active {
  background: var(--win-control-hover);
  color: var(--win-text-primary);
  font-weight: 600;
}
.nav-item.is-active::before {
  content: "";
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  height: 22px;
  width: 3px;
  border-radius: 2px;
  background: var(--win-accent);
}
.nav-item.is-active .nav-icon-wrap {
  color: var(--win-accent);
}

/* ===== 底部按钮区 ===== */
.sidebar-footer {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px;
  flex-shrink: 0;
}
.foot-item {
  color: var(--win-text-secondary);
  font-size: 14px;
}
.foot-item:hover {
  color: var(--win-text-primary);
}

/* ===== 主内容区（Mica 基底，卡片白色叠加） ===== */
.main-area {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  min-width: 0;
}
</style>
