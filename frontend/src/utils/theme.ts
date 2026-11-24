/**
 * 主题工具：给 CSS 变量帮不上忙的场景用（ECharts 等画布渲染，颜色在
 * setOption 时固化为具体值，.dark 类切换对已绘制内容无效）。
 *
 * `themeColors()` 每次调用都从 html 上实时解析 --win-* 变量，返回值即
 * 当前主题配色；切换主题后由调用方重取并重绘——用 `onThemeChange`
 * 监听 html.dark 的增删（AppLayout 的 toggleTheme 正是切这个类）。
 */
export interface ThemeColors {
  accent: string
  accentSoft: string
  success: string
  warning: string
  danger: string
  info: string
  /** 分割线/边框（--win-stroke） */
  strokeStrong: string
  /** 内容层/卡片底色（--win-layer），tooltip 等浮层用 */
  layer: string
  textPrimary: string
  textSecondary: string
  textMuted: string
  textDisabled: string
}

export function themeColors(): ThemeColors {
  const cs = getComputedStyle(document.documentElement)
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback
  return {
    accent: v('--win-accent', '#0067C0'),
    accentSoft: v('--win-accent-soft', 'rgba(0, 103, 192, 0.08)'),
    success: v('--win-success', '#0F7B0F'),
    warning: v('--win-warning', '#9D5D00'),
    danger: v('--win-danger', '#C42B1C'),
    info: v('--win-info', '#0067C0'),
    strokeStrong: v('--win-stroke', '#E5E5E5'),
    layer: v('--win-layer', '#FFFFFF'),
    textPrimary: v('--win-text-primary', 'rgba(0, 0, 0, 1)'),
    textSecondary: v('--win-text-secondary', 'rgba(0, 0, 0, 0.66)'),
    textMuted: v('--win-text-muted', 'rgba(0, 0, 0, 0.56)'),
    textDisabled: v('--win-text-disabled', 'rgba(0, 0, 0, 0.42)'),
  }
}

/** 监听主题切换；返回取消监听函数（组件卸载时调用）。 */
export function onThemeChange(cb: () => void): () => void {
  const observer = new MutationObserver(() => cb())
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  return () => observer.disconnect()
}
