import React, { useEffect, useMemo, useRef, useState } from 'react'
import { parseInline, parseMarkdown } from './markdown-parser.js'

export { parseInline, parseMarkdown } from './markdown-parser.js'

function Inline({ text }) {
  return renderInline(parseInline(text))
}

function renderInline(tokens) {
  return tokens.map((token, index) => {
    if (token.type === 'text') return token.text
    if (token.type === 'code') return <code key={index}>{token.text}</code>
    if (token.type === 'link') return <a key={index} href={token.href} target="_blank" rel="noopener noreferrer">{renderInline(token.children)}</a>
    const Tag = token.type === 'strong' ? 'strong' : token.type === 'em' ? 'em' : 'del'
    return <Tag key={index}>{renderInline(token.children)}</Tag>
  })
}

function CodeBlock({ block, className }) {
  const [copyState, setCopyState] = useState('复制')
  const timeout = useRef(null)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      clearTimeout(timeout.current)
    }
  }, [])
  async function copy() {
    let label = '已复制'
    try {
      await navigator.clipboard.writeText(block.text)
    } catch {
      label = '复制失败'
    }
    if (!mounted.current) return
    setCopyState(label)
    clearTimeout(timeout.current)
    timeout.current = setTimeout(() => setCopyState('复制'), 1800)
  }
  return <div className={className}>
    <div className="markdown-code-toolbar"><span className="markdown-code-language">{block.language}</span><button type="button" className="markdown-code-copy" onClick={copy} aria-label="复制代码">{copyState}</button></div>
    <pre className="markdown-code-content"><code>{block.text}</code></pre>
  </div>
}

function Block({ block }) {
  const gap = block.gap ? ' md-gap' : ''
  if (block.type === 'code') return <CodeBlock block={block} className={`markdown-code-block${gap}`} />
  if (block.type === 'table') return <div className={`markdown-table-scroll${gap}`} role="region" aria-label="Markdown 表格" tabIndex={0}>
    <table className="markdown-table"><thead><tr>{block.headers.map((cell, index) => <th key={index} scope="col" style={{ textAlign: block.align[index] || undefined }}><Inline text={cell} /></th>)}</tr></thead>
      <tbody>{block.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, index) => <td key={index} style={{ textAlign: block.align[index] || undefined }}><Inline text={cell} /></td>)}</tr>)}</tbody>
    </table>
  </div>
  if (block.type === 'heading') {
    const Heading = `h${block.level}`
    return <Heading className={`md-heading${gap}`}><Inline text={block.text} /></Heading>
  }
  if (block.type === 'list') {
    const List = block.ordered ? 'ol' : 'ul'
    return <List className={`markdown-list${gap}`} start={block.ordered ? block.start : undefined}>{block.items.map((item, index) => <li key={index}><Inline text={item.text} />{item.children.map((child, childIndex) => <Block key={childIndex} block={child} />)}</li>)}</List>
  }
  if (block.type === 'quote') return <blockquote className={`markdown-quote${gap}`}>{block.children.map((child, index) => <Block key={index} block={child} />)}</blockquote>
  if (block.type === 'rule') return <hr className={gap.trim() || undefined} />
  return <p className={gap.trim() || undefined}><Inline text={block.text} /></p>
}

export function Markdown({ text }) {
  const blocks = useMemo(() => parseMarkdown(text), [text])
  return <div className="message-markdown">{blocks.map((block, index) => <Block key={index} block={block} />)}</div>
}

export default Markdown
