/** router 冒烟：AppLayout 下 12 个子路由完整性（新增页面必须注册于此） */
import { describe, it, expect } from 'vitest'
import { router } from './router'

describe('router 路由表', () => {
  it('AppLayout 下注册全部 12 个页面路由', () => {
    const paths = router.getRoutes().map((r) => r.path)
    const expected = [
      '/', '/settings', '/characters', '/summary', '/splitter', '/workspace',
      '/prompt', '/style', '/stats', '/timeline', '/graph', '/map',
    ]
    for (const p of expected) {
      expect(paths, `缺少路由 ${p}`).toContain(p)
    }
    // 13 条记录 = 12 个子路由 + 1 条父 AppLayout 记录；'/' 父子归一后出现两次，按集合去重
    expect(new Set(paths.filter((p) => expected.includes(p))).size).toBe(12)
  })
})
