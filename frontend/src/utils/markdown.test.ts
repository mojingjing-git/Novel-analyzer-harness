/** Markdown 渲染器测试（vitest 版） */
import { describe, it, expect } from 'vitest'
import { renderMarkdown } from './markdown'

describe('markdown 渲染器', () => {
  it('标题', () => {
    const r = renderMarkdown('# H1\n## H2\n### H3')
    expect(r).toContain('<h1 class="md-h1">H1</h1>')
    expect(r).toContain('<h2 class="md-h2">H2</h2>')
    expect(r).toContain('<h3 class="md-h3">H3</h3>')
  })

  it('粗体/斜体/行内代码', () => {
    const r = renderMarkdown('**bold** *italic* `code`')
    expect(r).toContain('<strong>bold</strong>')
    expect(r).toContain('<em>italic</em>')
    expect(r).toContain('<code class="md-code">code</code>')
  })

  it('XSS 转义', () => {
    const r = renderMarkdown('<script>alert(1)</script>')
    expect(r).toContain('&lt;script&gt;')
    expect(r).not.toContain('<script>')
  })

  it('空文本返回空串', () => {
    expect(renderMarkdown('')).toBe('')
  })

  it('段落合并与换行', () => {
    const r = renderMarkdown('第一段\n第二段')
    expect(r).toContain('<p class="md-p">')
    expect(r).toContain('<br/>')
  })

  it('无序列表支持 - * + 三种前缀', () => {
    for (const prefix of ['-', '*', '+'] as const) {
      const r = renderMarkdown(`${prefix} a\n${prefix} b`)
      expect(r).toContain('<ul class="md-ul">')
      expect(r).toContain('<li>a</li>')
      expect(r).toContain('<li>b</li>')
    }
    const r = renderMarkdown('- a\n- b\n- c')
    expect(r).toContain('<li>c</li>')
  })

  it('有序列表', () => {
    const r = renderMarkdown('1. one\n2. two')
    expect(r).toContain('<ol class="md-ol">')
    expect(r).toContain('<li>one</li>')
    expect(r).toContain('<li>two</li>')
  })

  it('围栏代码块渲染与内容转义', () => {
    const r = renderMarkdown('```python\nprint("hi")\n```')
    expect(r).toContain('<pre class="md-pre">')
    expect(r).toContain('language-python')
    expect(r).toContain('print(&quot;hi&quot;)')
  })

  it('引用块', () => {
    const r = renderMarkdown('> quote here')
    expect(r).toContain('<blockquote class="md-quote">')
    expect(r).toContain('quote here')
  })

  it('链接危险协议被过滤为 #', () => {
    const r = renderMarkdown('[evil](javascript:alert(1))')
    expect(r).toContain('href="#"')
    expect(r).not.toContain('javascript:')
  })

  it('分隔线', () => {
    expect(renderMarkdown('---')).toContain('<hr class="md-hr"/>')
  })

  it('表格', () => {
    const r = renderMarkdown('| A | B |\n|---|---|\n| 1 | 2 |')
    expect(r).toContain('<table class="md-table">')
    expect(r).toContain('<th>A</th>')
    expect(r).toContain('<td>1</td>')
  })

  it('链接 href 不被强调正则污染', () => {
    const r = renderMarkdown('[doc](https://example.com/wiki/a_b_c)')
    expect(r).not.toContain('<em>')
    expect(r).not.toContain('<strong>')
    expect(r).not.toContain('<del>')
    expect(r).toContain('href="https://example.com/wiki/a_b_c"')
  })

  it('可见文本强调仍生效且 URL 内下划线不被污染', () => {
    const r = renderMarkdown('__init__ 与 [x](https://a.io/p__q)')
    expect(r).toContain('<strong>init</strong>')
    expect(r).toContain('href="https://a.io/p__q"')
  })

  it('链接完整形态：href/target/rel/锚文本', () => {
    const r = renderMarkdown('[百度](https://baidu.com)')
    expect(r).toContain('<a href="https://baidu.com" target="_blank" rel="noopener" class="md-link">百度</a>')
  })
})
