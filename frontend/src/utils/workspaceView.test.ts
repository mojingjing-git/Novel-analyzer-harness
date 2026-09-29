/** workspaceView 纯函数测试：大小阶梯与时间格式化 */
import { describe, it, expect } from 'vitest'
import { fmtSize, fmtTime } from './workspaceView'

describe('workspaceView.fmtSize', () => {
  it('B / KB / MB / GB 四档阶梯', () => {
    expect(fmtSize(0)).toBe('0 B')
    expect(fmtSize(-5)).toBe('0 B')
    expect(fmtSize(512)).toBe('512 B')
    expect(fmtSize(2048)).toBe('2 KB')
    expect(fmtSize(5 * 1024 * 1024)).toBe('5.0 MB')
    expect(fmtSize(3 * 1024 * 1024 * 1024)).toBe('3.00 GB')
  })
})

describe('workspaceView.fmtTime', () => {
  it('0 / 缺失 → —', () => {
    expect(fmtTime(0)).toBe('—')
  })

  it('epoch 秒 → 本地时区 YYYY-MM-DD HH:mm', () => {
    const d = new Date(2026, 8, 29, 14, 5)  // 本地时区构造，断言与时区无关
    expect(fmtTime(Math.floor(d.getTime() / 1000))).toBe('2026-09-29 14:05')
  })

  it('个位月/日/时/分补零', () => {
    const d = new Date(2026, 0, 5, 3, 7)
    expect(fmtTime(Math.floor(d.getTime() / 1000))).toBe('2026-01-05 03:07')
  })
})
