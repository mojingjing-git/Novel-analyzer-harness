/**
 * useLocale 契约测试（i18n L2 接口预留）：白名单回退 zh-CN、setLocale 直写 i18n locale。
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { createI18n } from 'vue-i18n'
import { useLocale } from './useLocale'

function mountWithI18n(initialLocale: string) {
  const i18n = createI18n({
    legacy: false,
    locale: initialLocale,
    fallbackLocale: 'en',
    messages: {},
    missingWarn: false,
    fallbackWarn: false,
  })
  let api!: ReturnType<typeof useLocale>
  const Comp = defineComponent({
    setup() {
      api = useLocale()
      return () => null
    },
  })
  mount(Comp, { global: { plugins: [i18n] } })
  return { api, i18n }
}

describe('useLocale 契约', () => {
  it('支持语言表 = zh-CN / en', () => {
    const { api } = mountWithI18n('zh-CN')
    expect(api.availableLocales).toEqual(['zh-CN', 'en'])
  })

  it('i18n locale 是白名单内 → 透传', () => {
    const { api, i18n } = mountWithI18n('zh-CN')
    i18n.global.locale.value = 'en'
    expect(api.locale.value).toBe('en')
  })

  it('非白名单 locale（zh_CN 下划线 typo）→ 回退 zh-CN（既定 Open Question 行为）', () => {
    const { api } = mountWithI18n('zh_CN')
    expect(api.locale.value).toBe('zh-CN')
  })

  it('setLocale 直写 i18n locale', () => {
    const { api, i18n } = mountWithI18n('zh-CN')
    api.setLocale('en')
    expect(i18n.global.locale.value).toBe('en')
    expect(api.locale.value).toBe('en')
  })
})
