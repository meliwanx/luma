import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'
import { parseInline, parseMarkdown } from './markdown-parser.js'

// Exercise the actual React renderer using the project's existing JSX compiler.
const require = createRequire(import.meta.url)
const source = await readFile(new URL('./markdown.jsx', import.meta.url), 'utf8')
const compiled = await transformWithEsbuild(source, 'markdown.jsx', { loader: 'jsx', jsx: 'transform' })
const rendererSource = compiled.code
  .replace(/(['"])react\1/g, JSON.stringify(pathToFileURL(require.resolve('react')).href))
  .replace(/(['"])\.\/markdown-parser\.js\1/g, JSON.stringify(new URL('./markdown-parser.js', import.meta.url).href))
const { Markdown } = await import(`data:text/javascript;base64,${Buffer.from(rendererSource).toString('base64')}`)
const render = (text) => renderToStaticMarkup(React.createElement(Markdown, { text }))

function allTokens(tokens) {
  return tokens.flatMap((token) => [token, ...allTokens(token.children || [])])
}

test('GFM tables support left, center and right alignment', () => {
  const [table] = parseMarkdown('| 周期 | 状态 | 金额 |\n|:---|:---:|---:|\n| 本周 | **完成** | 20 |')
  assert.equal(table.type, 'table')
  assert.deepEqual(table.headers, ['周期', '状态', '金额'])
  assert.deepEqual(table.align, ['left', 'center', 'right'])
  assert.deepEqual(table.rows, [['本周', '**完成**', '20']])
  const html = render('| 周期 | 状态 | 金额 |\n|:---|:---:|---:|\n| 本周 | **完成** | 20 |')
  assert.match(html, /class="markdown-table-scroll"/)
  assert.match(html, /<thead><tr><th scope="col" style="text-align:left">周期<\/th>/)
  assert.match(html, /<td style="text-align:center"><strong>完成<\/strong><\/td>/)
  assert.match(html, /<td style="text-align:right">20<\/td>/)
})

test('GFM tables do not require leading or trailing pipes', () => {
  const [table] = parseMarkdown('周期 | 状态\n--- | :---:\n本周 | `完成`')
  assert.equal(table.type, 'table')
  assert.deepEqual(table.headers, ['周期', '状态'])
  assert.deepEqual(table.align, [null, 'center'])
  assert.deepEqual(table.rows, [['本周', '`完成`']])
})

test('single-column tables also accept data rows without outer pipes', () => {
  const [table, paragraph] = parseMarkdown('| 状态 |\n| --- |\n完成\n进行中\n\n表格之后')
  assert.equal(table.type, 'table')
  assert.deepEqual(table.rows, [['完成'], ['进行中']])
  assert.equal(paragraph.type, 'paragraph')
  assert.equal(paragraph.text, '表格之后')
})

test('escaped pipes and pipes inside code stay within their cells', () => {
  const markdown = '| 项目 | 状态 |\n| --- | --- |\n| a\\|b | `x|y` |\n| ``a`|b`` | 成功 |'
  const [table] = parseMarkdown(markdown)
  assert.deepEqual(table.rows, [['a\\|b', '`x|y`'], ['``a`|b``', '成功']])
  const html = render(markdown)
  assert.match(html, /<td>a\|b<\/td>/)
  assert.match(html, /<td><code>x\|y<\/code><\/td>/)
  assert.match(html, /<td><code>a`\|b<\/code><\/td>/)
})

test('streaming tables render the available header and incomplete rows', () => {
  const [headerOnly] = parseMarkdown('| 周期 | 状态 |\n| --- | --- |')
  assert.equal(headerOnly.type, 'table')
  assert.deepEqual(headerOnly.rows, [])
  const [partial] = parseMarkdown('| 周期 | 状态 |\n| --- | --- |\n| 本周')
  assert.deepEqual(partial.rows, [['本周', '']])
  const html = render('| 周期 | 状态 |\n| --- | --- |\n| 本周 | **进行')
  assert.match(html, /<td>\*\*进行<\/td>/)
  assert.doesNotThrow(() => render('| 周期 | 状态 |\n| --- | :--'))
})

test('tables stop before ordinary following prose', () => {
  const blocks = parseMarkdown('周期 | 状态\n--- | ---\n本周 | 完成\n口径说明')
  assert.deepEqual(blocks.map((block) => block.type), ['table', 'paragraph'])
  assert.equal(blocks[1].text, '口径说明')
})

test('unclosed fenced code blocks preserve streamed content', () => {
  const [block] = parseMarkdown('```python\nvalue = "<script>"\n  print(value)')
  assert.equal(block.type, 'code')
  assert.equal(block.language, 'python')
  assert.equal(block.closed, false)
  assert.equal(block.text, 'value = "<script>"\n  print(value)')
  const html = render('```python\nvalue = "<script>"\n  print(value)')
  assert.match(html, /aria-label="复制代码">复制<\/button>/)
  assert.match(html, /<pre class="markdown-code-content"><code>value = &quot;&lt;script&gt;&quot;\n  print\(value\)<\/code><\/pre>/)
  assert.doesNotMatch(html, /<script>/)
})

test('fences keep Markdown literal and require a matching closing fence', () => {
  const blocks = parseMarkdown('````js\n**literal**\n```\nhttps://example.com\n````\n之后')
  assert.deepEqual(blocks.map((block) => block.type), ['code', 'paragraph'])
  assert.equal(blocks[0].closed, true)
  assert.equal(blocks[0].text, '**literal**\n```\nhttps://example.com')
  const html = render('```text\n**literal**\n```')
  assert.doesNotMatch(html, /<strong>/)
  assert.match(html, /<code>\*\*literal\*\*<\/code>/)
})

test('all six heading levels, rules, paragraphs and blank lines render semantically', () => {
  const markdown = '# 一\n## 二\n### 三\n#### 四\n##### 五\n###### 六\n---\n第一行\n第二行\n\n下一段'
  const blocks = parseMarkdown(markdown)
  assert.deepEqual(blocks.slice(0, 6).map((block) => block.level), [1, 2, 3, 4, 5, 6])
  assert.equal(blocks[6].type, 'rule')
  assert.equal(blocks[7].text, '第一行\n第二行')
  assert.equal(blocks[8].gap, true)
  const html = render(markdown)
  for (let level = 1; level <= 6; level += 1) assert.match(html, new RegExp(`<h${level} class="md-heading">`))
  assert.match(html, /<hr\/>/)
  assert.match(html, /<p class="md-gap">下一段<\/p>/)
})

test('indentation nests ordered and unordered lists and leaves following prose intact', () => {
  const markdown = '3. 外层\n   - 子项 **加粗**\n   - 第二项\n4. 后续\n正文\n- 另一列表'
  const blocks = parseMarkdown(markdown)
  assert.deepEqual(blocks.map((block) => block.type), ['list', 'paragraph', 'list'])
  assert.equal(blocks[0].ordered, true)
  assert.equal(blocks[0].start, 3)
  assert.equal(blocks[0].items.length, 2)
  assert.equal(blocks[0].items[0].children[0].ordered, false)
  assert.equal(blocks[0].items[0].children[0].items.length, 2)
  const html = render(markdown)
  assert.match(html, /<ol class="markdown-list" start="3"><li>外层<ul class="markdown-list"><li>子项 <strong>加粗<\/strong>/)
  assert.match(html, /<\/ol><p>正文<\/p><ul/)
})

test('quotes contain safely parsed blocks including nested quotes', () => {
  const markdown = '> # 引用\n>\n> - 项目\n> > 内层\n\n正文'
  const blocks = parseMarkdown(markdown)
  assert.equal(blocks[0].type, 'quote')
  assert.deepEqual(blocks[0].children.map((block) => block.type), ['heading', 'list', 'quote'])
  const html = render(markdown)
  assert.match(html, /<blockquote class="markdown-quote"><h1/)
  assert.match(html, /<blockquote class="markdown-quote"><p>内层<\/p><\/blockquote>/)
})

test('Chinese and full-width punctuation do not prevent bold emphasis', () => {
  const text = '**口径说明**（取数时沿用）\n前文**加粗**后文'
  const strong = allTokens(parseInline(text)).filter((token) => token.type === 'strong')
  assert.deepEqual(strong.map((token) => token.children[0].text), ['口径说明', '加粗'])
  const html = render(text)
  assert.match(html, /<strong>口径说明<\/strong>（取数时沿用）/)
  assert.match(html, /前文<strong>加粗<\/strong>后文/)
})

test('inline emphasis supports both delimiters, deletion and nesting', () => {
  const html = render('__粗__ *斜* _斜_ ~~删除~~ **粗 *斜***')
  assert.match(html, /<strong>粗<\/strong> <em>斜<\/em> <em>斜<\/em> <del>删除<\/del>/)
  assert.match(html, /<strong>粗 <em>斜<\/em><\/strong>/)
})

test('emphasis closing delimiters ignore literal markers inside code and URLs', () => {
  const html = render('**`含**代码`** *看 https://example.com/a*b* **https://example.com**')
  assert.match(html, /<strong><code>含\*\*代码<\/code><\/strong>/)
  assert.match(html, /<em>看 <a href="https:\/\/example.com\/a\*b"[^>]*>https:\/\/example.com\/a\*b<\/a><\/em>/)
  assert.match(html, /<strong><a href="https:\/\/example.com\/"[^>]*>https:\/\/example.com<\/a><\/strong>/)
  const linked = render('**[链接](https://example.com/a**b)**')
  assert.match(linked, /<strong><a href="https:\/\/example.com\/a\*\*b"[^>]*>链接<\/a><\/strong>/)
})

test('snake_case and URL underscores remain literal', () => {
  const markdown = 'snake_case_name snake__case__name https://example.com/snake_case_name?q=a_b_c'
  const tokens = allTokens(parseInline(markdown))
  assert.equal(tokens.filter((token) => token.type === 'em').length, 0)
  assert.equal(tokens.filter((token) => token.type === 'strong').length, 0)
  const html = render(markdown)
  assert.match(html, /snake_case_name snake__case__name /)
  assert.match(html, /href="https:\/\/example.com\/snake_case_name\?q=a_b_c"/)
  assert.doesNotMatch(html, /<em>/)
})

test('explicit and bare links allow only http and https and trim prose punctuation', () => {
  const markdown = '[**官网**](https://example.com/path_(part)) http://example.com/a_b_c，https://example.org/end.'
  const links = allTokens(parseInline(markdown)).filter((token) => token.type === 'link')
  assert.deepEqual(links.map((link) => link.href), ['https://example.com/path_(part)', 'http://example.com/a_b_c', 'https://example.org/end'])
  const html = render(markdown)
  assert.match(html, /rel="noopener noreferrer"><strong>官网<\/strong><\/a>/)
  assert.match(html, /https:\/\/example.org\/end<\/a>\./)
})

test('javascript, data, file, ftp and relative links never generate anchors', () => {
  for (const destination of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'data:text/html,<script>alert(1)</script>', 'file:///tmp/file', 'ftp://example.com', '//example.com', '/app', 'https://']) {
    const markdown = `[文字](${destination})`
    assert.equal(allTokens(parseInline(markdown)).filter((token) => token.type === 'link').length, 0, destination)
    assert.doesNotMatch(render(markdown), /<a\b/, destination)
  }
  assert.doesNotMatch(render('[https://safe.example](javascript:alert(1))'), /<a\b/)
})

test('HTML and link labels are React text, never injected markup', () => {
  const html = render('<img src=x onerror=alert(1)> [<script>坏</script>](https://example.com)')
  assert.doesNotMatch(html, /<(?:img|script)\b/)
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/)
  assert.match(html, /&lt;script&gt;坏&lt;\/script&gt;<\/a>/)
})

test('inline code and escapes prevent emphasis and link interpretation', () => {
  const html = render('`**literal** https://example.com` ``a`b`` \\*普通\\* \\_普通\\_')
  assert.match(html, /<code>\*\*literal\*\* https:\/\/example.com<\/code>/)
  assert.match(html, /<code>a`b<\/code>/)
  assert.match(html, /\*普通\* _普通_/)
  assert.doesNotMatch(html, /<(?:strong|em|a)\b/)
})

test('empty input and incomplete syntax are safe during streaming', () => {
  assert.deepEqual(parseMarkdown(''), [])
  for (const markdown of ['```', '**', '__', '~~', '[文字](', '`', '#', '| 标题 |', '> '.repeat(100) + '末尾']) {
    assert.doesNotThrow(() => render(markdown), markdown)
  }
})
