/** WorkspacePage 的纯函数层：文件大小阶梯、时间戳格式化与归档还原命名建议 */
export function fmtSize(b: number): string {
  if (!b || b < 0) return '0 B'
  if (b < 1024) return b + ' B'
  const kb = b / 1024
  if (kb < 1024) return kb.toFixed(0) + ' KB'
  const mb = kb / 1024
  if (mb < 1024) return mb.toFixed(1) + ' MB'
  return (mb / 1024).toFixed(2) + ' GB'
}

/** epoch 秒 → `YYYY-MM-DD HH:mm`（本地时区）；0/缺失 → '—' */
export function fmtTime(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 剥离归档目录名末尾的 `-YYYYMMDD-HHMM` 时间戳 */
export function stripArchiveTimestamp(archiveName: string): string {
  return (archiveName || '').trim().replace(/-\d{8}-\d{4}$/, '')
}

/** 为归档切分还原生成不冲突的默认工作区目录名 */
export function suggestRestoreName(
  archiveName: string,
  existingNovels: string[],
  existingArchives: string[] = [],
): string {
  const base = stripArchiveTimestamp(archiveName) || (archiveName || '').trim()
  const used = new Set([...existingNovels, ...existingArchives])
  if (!used.has(base)) return base
  let idx = 2
  while (used.has(`${base}-${idx}`)) idx++
  return `${base}-${idx}`
}
