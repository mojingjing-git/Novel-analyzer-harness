// 轻量级 Markdown → HTML 渲染器（零依赖）
// 支持：标题、粗体、斜体、行内代码、代码块、列表、引用、链接、分隔线、段落、表格
//
// 安全：先 HTML 转义再插入标签，避免 XSS

/** HTML 转义 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/** 行内格式：粗体/斜体/行内代码/链接 */
function inlineFormat(text: string): string {
  // 行内代码必须先处理（避免被后续规则修改）
  // 使用占位符保护内层内容
  const codeStash: string[] = []
  text = text.replace(/`([^`]+)`/g, (_, code) => {
    const idx = codeStash.push(`<code class="md-code">${escapeHtml(code)}</code>`) - 1
    return `\u0001CODE${idx}\u0001`
  })

  // 转义剩余内容
  text = escapeHtml(text)

  // 链接 [text](url)：生成 <a> 后整体入占位符，避免后续强调正则污染 href 值
  const linkStash: string[] = []
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
    // 防止 javascript: 等危险协议
    const safeUrl = /^(https?:|mailto:|#|\/)/i.test(url) ? url : '#'
    const idx = linkStash.push(
      `<a href="${safeUrl}" target="_blank" rel="noopener" class="md-link">${label}</a>`,
    ) - 1
    return `\u0001LINK${idx}\u0001`
  })

  // 粗体 **text** 或 __text__
  text = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
  text = text.replace(/__([^_\n]+)__/g, '<strong>$1</strong>')

  // 斜体 *text* 或 _text_
  text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
  text = text.replace(/(^|[^_])_([^_\n]+)_/g, '$1<em>$2</em>')

  // 删除线 ~~text~~
  text = text.replace(/~~([^~\n]+)~~/g, '<del>$1</del>')

  // 还原链接占位符
  text = text.replace(/\u0001LINK(\d+)\u0001/g, (_, idx) => linkStash[Number(idx)])

  // 还原行内代码占位符
  text = text.replace(/\u0001CODE(\d+)\u0001/g, (_, idx) => codeStash[Number(idx)])

  return text
}

export interface MarkdownHeading {
  id: string
  level: number
  text: string
}

/** 剥离标题中的行内 Markdown 语法，供目录显示 */
function stripInlineMarkdown(text: string): string {
  return text
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1')
    .replace(/\*\*([^*\n]+)\*\*/g, '$1')
    .replace(/__([^_\n]+)__/g, '$1')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1$2')
    .replace(/(^|[^_])_([^_\n]+)_/g, '$1$2')
    .replace(/~~([^~\n]+)~~/g, '$1')
    .trim()
}

/** 渲染 Markdown 并提取 h1-h3 目录锚点 */
export function renderMarkdownWithToc(text: string): { html: string; headings: MarkdownHeading[] } {
  if (!text) return { html: '', headings: [] }
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  const out: string[] = []
  const headings: MarkdownHeading[] = []
  let headingSeq = 0
  let i = 0

  while (i < lines.length) {
    const line = lines[i]

    // ===== 代码块（``` 围栏）=====
    const fenceMatch = /^```(\w*)\s*$/.exec(line)
    if (fenceMatch) {
      const lang = fenceMatch[1]
      const codeLines: string[] = []
      i++
      while (i < lines.length && !/^```\s*$/.test(lines[i])) {
        codeLines.push(lines[i])
        i++
      }
      i++ // 跳过结束 ```
      const langClass = lang ? ` class="language-${escapeHtml(lang)}"` : ''
      out.push(`<pre class="md-pre"><code${langClass}>${escapeHtml(codeLines.join('\n'))}</code></pre>`)
      continue
    }

    // ===== 表格（| col | col | 形式）=====
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[-:\s|]+\|\s*$/.test(lines[i + 1])) {
      const headerCells = line.split('|').slice(1, -1).map(c => c.trim())
      i += 2 // 跳过分隔行
      const bodyLines: string[][] = []
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        bodyLines.push(lines[i].split('|').slice(1, -1).map(c => c.trim()))
        i++
      }
      const thead = '<thead><tr>' + headerCells.map(c => `<th>${inlineFormat(c)}</th>`).join('') + '</tr></thead>'
      const tbody = '<tbody>' + bodyLines.map(
        row => '<tr>' + row.map(c => `<td>${inlineFormat(c)}</td>`).join('') + '</tr>'
      ).join('') + '</tbody>'
      out.push(`<div class="md-table-wrap"><table class="md-table">${thead}${tbody}</table></div>`)
      continue
    }

    // ===== 标题 ######
    const hMatch = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line)
    if (hMatch) {
      const level = hMatch[1].length
      if (level <= 3) {
        const id = `md-h-${++headingSeq}`
        headings.push({ id, level, text: stripInlineMarkdown(hMatch[2]) })
        out.push(`<h${level} id="${id}" class="md-h${level}">${inlineFormat(hMatch[2])}</h${level}>`)
      } else {
        out.push(`<h${level} class="md-h${level}">${inlineFormat(hMatch[2])}</h${level}>`)
      }
      i++
      continue
    }

    // ===== 分隔线 ---
    if (/^[-*_]{3,}\s*$/.test(line)) {
      out.push('<hr class="md-hr"/>')
      i++
      continue
    }

    // ===== 引用 >
    if (/^>\s?/.test(line)) {
      const quoteLines: string[] = []
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        quoteLines.push(lines[i].replace(/^>\s?/, ''))
        i++
      }
      out.push(`<blockquote class="md-quote">${quoteLines.map(l => `<p>${inlineFormat(l)}</p>`).join('')}</blockquote>`)
      continue
    }

    // ===== 无序列表 - * +
    const ulMatch = /^[-*+]\s+(.+)$/.exec(line)
    if (ulMatch) {
      const items: string[] = []
      while (i < lines.length) {
        const m = /^[-*+]\s+(.+)$/.exec(lines[i])
        if (!m) break
        items.push(`<li>${inlineFormat(m[1])}</li>`)
        i++
      }
      out.push(`<ul class="md-ul">${items.join('')}</ul>`)
      continue
    }

    // ===== 有序列表 1. 2.
    const olMatch = /^\d+\.\s+(.+)$/.exec(line)
    if (olMatch) {
      const items: string[] = []
      while (i < lines.length) {
        const m = /^\d+\.\s+(.+)$/.exec(lines[i])
        if (!m) break
        items.push(`<li>${inlineFormat(m[1])}</li>`)
        i++
      }
      out.push(`<ol class="md-ol">${items.join('')}</ol>`)
      continue
    }

    // ===== 空行 =====
    if (line.trim() === '') {
      i++
      continue
    }

    // ===== 普通段落（连续非空行合并）=====
    const paraLines: string[] = [line]
    i++
    while (i < lines.length && lines[i].trim() !== '' &&
           !/^(#{1,6}\s|```|>\s?|[-*+]\s|\d+\.\s|\|)/.test(lines[i])) {
      paraLines.push(lines[i])
      i++
    }
    // 单换行 → <br>
    out.push(`<p class="md-p">${inlineFormat(paraLines.join('\n')).replace(/\n/g, '<br/>')}</p>`)
  }

  return { html: out.join('\n'), headings }
}

/** 渲染 Markdown 文本为 HTML 字符串 */
export function renderMarkdown(text: string): string {
  return renderMarkdownWithToc(text).html
}