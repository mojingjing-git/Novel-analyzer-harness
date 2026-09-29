import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

/**
 * i18n locale 控制 composable（2026-09-02 L2 接口预留）
 *
 * 当前阶段：仅暴露接口，不接入实际切换逻辑。
 * 未来 i18n 启用时，只需在此处加 `await api.putSettings({...gui.language: locale.value})`。
 */
export function useLocale() {
  const { locale } = useI18n()

  const supportedLocales = ['zh-CN', 'en'] as const
  type SupportedLocale = typeof supportedLocales[number]

  const currentLocale = computed<SupportedLocale>(() => {
    const l = locale.value
    return (supportedLocales as readonly string[]).includes(l) ? l as SupportedLocale : 'zh-CN'
  })

  function setLocale(lang: SupportedLocale) {
    locale.value = lang
    // TODO(i18n 启用时): 同步到后端 config.gui.language
    // Open Question (2026-09-02): 当前 type cast `(supportedLocales as readonly string[]).includes(l) ? l as SupportedLocale : 'zh-CN'`
    //   对 'zh_CN' (下划线) 等 typo 不会拒，未来加 setLocale 的来源校验
  }

  return {
    locale: currentLocale,
    setLocale,
    availableLocales: supportedLocales,
  }
}
