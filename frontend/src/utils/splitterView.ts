/**
 * SplitterPage 的纯函数层（从组件原样抽取）。
 * 语义锁定：F-2 整体失败回滚 processing→pending（防 UI 永久卡"处理中"）。
 */
import type { SplitterPreview } from '../api/client'

export type SplitterFileStatus = 'pending' | 'processing' | 'done' | 'error'

export interface SplitterFileItem {
  file_path: string
  book_name: string
  status: SplitterFileStatus
  chapters: number
  error: string
}

/** 路径反斜杠归一 + 剥 .txt/.docx 扩展名（docx 支持 2026-09-28 起） */
export function baseName(p: string): string {
  const s = p.replace(/\\/g, '/').split('/').pop() || p
  return s.replace(/\.(txt|docx)$/i, '')
}

export function splitterStatusText(s: SplitterFileStatus): string {
  return { pending: '待处理', processing: '处理中', done: '完成', error: '失败' }[s]
}

/** 按完整路径去重追加 */
export function appendFileUnique(
  list: SplitterFileItem[],
  p: string,
): { items: SplitterFileItem[]; added: boolean } {
  if (list.some((f) => f.file_path === p)) return { items: list, added: false }
  return {
    items: [...list, { file_path: p, book_name: baseName(p), status: 'pending', chapters: 0, error: '' }],
    added: true,
  }
}

/** F-2：批量切分整体失败时，所有"处理中"行回滚"待处理"，防 UI 永久卡死 */
export function rollbackProcessing(list: SplitterFileItem[]): SplitterFileItem[] {
  return list.map((f) => (f.status === 'processing' ? { ...f, status: 'pending' } : f))
}

export interface ChapterGroup {
  volume: string
  items: SplitterPreview['chapters']
}

/** 预览章节按卷分组：相邻同卷合并为一组 */
export function groupChaptersByVolume(chapters: SplitterPreview['chapters']): ChapterGroup[] {
  const groups: ChapterGroup[] = []
  let cur: ChapterGroup | null = null
  for (const ch of chapters) {
    const vol = ch.volume || ''
    if (!cur || cur.volume !== vol) {
      cur = { volume: vol, items: [] }
      groups.push(cur)
    }
    cur.items.push(ch)
  }
  return groups
}
