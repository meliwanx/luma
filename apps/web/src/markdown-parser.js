const WORD_CHARACTER = /[A-Za-z0-9_]/
const MAX_NESTING = 16

function isEscaped(text, index) {
  let slashes = 0
  while (index > 0 && text[--index] === '\\') slashes += 1
  return slashes % 2 === 1
}

function safeLink(destination) {
  if (!/^https?:\/\//i.test(destination) || /[\s\u0000-\u001f\u007f]/.test(destination)) return null
  try {
    const url = new URL(destination)
    return ['http:', 'https:'].includes(url.protocol) && url.hostname ? url.href : null
  } catch {
    return null
  }
}

function codeSpan(text, start) {
  const marker = text.slice(start).match(/^`+/)[0]
  let close = text.indexOf(marker, start + marker.length)
  while (close !== -1 && (text[close - 1] === '`' || text[close + marker.length] === '`')) {
    close = text.indexOf(marker, close + marker.length)
  }
  if (close === -1) return null
  let content = text.slice(start + marker.length, close).replace(/\n/g, ' ')
  if (content.startsWith(' ') && content.endsWith(' ') && content.trim()) content = content.slice(1, -1)
  return { text: content, end: close + marker.length }
}

function closingDelimiter(text, marker, start) {
  const validClose = (close, end) => !/\s/.test(text[close - 1] || '')
    && (marker[0] !== '_' || !WORD_CHARACTER.test(text[end] || ''))
  for (let index = start; index < text.length;) {
    if (text[index] === '\\') {
      index += 2
      continue
    }
    if (text[index] === '`') {
      const span = codeSpan(text, index)
      if (span) {
        index = span.end
        continue
      }
      index += text.slice(index).match(/^`+/)[0].length
      continue
    }
    if (text[index] === '[') {
      const link = explicitLink(text, index)
      if (link) {
        index = link.end
        continue
      }
    }
    if (/^https?:\/\//i.test(text.slice(index, index + 8))) {
      const link = bareLink(text, index)
      if (link) {
        // A URL may contain literal delimiters, with the outer emphasis closing
        // at its tail: **https://example.com/a*b**.
        const close = link.end - marker.length
        if (text.slice(close, link.end) === marker && validClose(close, link.end)) return close
        index = link.end
        continue
      }
    }
    if (text.startsWith(marker, index)) {
      let end = index + marker.length
      while (text[end] === marker[0]) end += 1
      const close = end - marker.length
      if ((marker.length > 1 || end - index === 1) && validClose(close, end)) return close
      index = end
    } else {
      index += 1
    }
  }
  return -1
}

function explicitLink(text, start) {
  let labelEnd = start + 1
  let brackets = 1
  for (; labelEnd < text.length; labelEnd += 1) {
    if (isEscaped(text, labelEnd)) continue
    if (text[labelEnd] === '[') brackets += 1
    if (text[labelEnd] === ']' && --brackets === 0) break
  }
  if (brackets || text[labelEnd + 1] !== '(') return null
  let end = labelEnd + 2
  let parentheses = 1
  for (; end < text.length; end += 1) {
    if (isEscaped(text, end)) continue
    if (text[end] === '(') parentheses += 1
    if (text[end] === ')' && --parentheses === 0) break
  }
  if (parentheses) return null
  const destination = text.slice(labelEnd + 2, end).trim()
  return {
    end: end + 1,
    label: text.slice(start + 1, labelEnd),
    href: safeLink(destination.startsWith('<') && destination.endsWith('>') ? destination.slice(1, -1) : destination),
  }
}

function bareLink(text, start) {
  if (start > 0 && WORD_CHARACTER.test(text[start - 1])) return null
  const match = text.slice(start).match(/^https?:\/\/[^\s<>"'`，。；：！？、（）【】《》]+/i)
  if (!match) return null
  let destination = match[0].replace(/[.,;:!?\]}]+$/, '')
  while (destination.endsWith(')') && (destination.match(/\)/g) || []).length > (destination.match(/\(/g) || []).length) {
    destination = destination.slice(0, -1)
  }
  const href = safeLink(destination)
  return href ? { href, text: destination, end: start + destination.length } : null
}

// Keep URLs and code spans atomic before looking for emphasis delimiters.
export function parseInline(value, depth = 0, allowLinks = true) {
  const text = String(value ?? '')
  if (depth >= MAX_NESTING) return [{ type: 'text', text }]
  const tokens = []
  let pending = ''
  const append = (token) => {
    if (pending) tokens.push({ type: 'text', text: pending })
    pending = ''
    tokens.push(token)
  }
  for (let index = 0; index < text.length;) {
    if (text[index] === '\\' && /[\\`*_{}[\]()#+\-.!|>~]/.test(text[index + 1] || '')) {
      pending += text[index + 1]
      index += 2
      continue
    }
    if (text[index] === '`') {
      const marker = text.slice(index).match(/^`+/)[0]
      const span = codeSpan(text, index)
      if (span) {
        append({ type: 'code', text: span.text })
        index = span.end
        continue
      }
      pending += marker
      index += marker.length
      continue
    }
    if (allowLinks && text[index] === '[') {
      const link = explicitLink(text, index)
      if (link) {
        if (link.href) append({ type: 'link', href: link.href, children: parseInline(link.label, depth + 1, false) })
        else pending += text.slice(index, link.end)
        index = link.end
        continue
      }
    }
    if (allowLinks && /^https?:\/\//i.test(text.slice(index, index + 8))) {
      const link = bareLink(text, index)
      if (link) {
        append({ type: 'link', href: link.href, children: [{ type: 'text', text: link.text }] })
        index = link.end
        continue
      }
    }
    const marker = ['**', '__', '~~', '*', '_'].find((candidate) => text.startsWith(candidate, index))
    if (marker && !/\s/.test(text[index + marker.length] || '')
      && (marker[0] !== '_' || !WORD_CHARACTER.test(text[index - 1] || ''))) {
      const close = closingDelimiter(text, marker, index + marker.length)
      if (close > index + marker.length) {
        append({
          type: marker === '~~' ? 'del' : marker.length === 2 ? 'strong' : 'em',
          children: parseInline(text.slice(index + marker.length, close), depth + 1, allowLinks),
        })
        index = close + marker.length
        continue
      }
    }
    pending += text[index]
    index += 1
  }
  if (pending) tokens.push({ type: 'text', text: pending })
  return tokens
}

function tableCells(line) {
  const source = line.trim()
  const cells = []
  let cell = ''
  let codeMarker = ''
  let hasPipe = false
  for (let index = 0; index < source.length; index += 1) {
    const character = source[index]
    if (character === '`' && !isEscaped(source, index)) {
      const marker = source.slice(index).match(/^`+/)[0]
      if (!codeMarker) codeMarker = marker
      else if (marker === codeMarker) codeMarker = ''
      cell += marker
      index += marker.length - 1
    } else if (character === '|' && !codeMarker && !isEscaped(source, index)) {
      hasPipe = true
      cells.push(cell.trim())
      cell = ''
    } else {
      cell += character
    }
  }
  cells.push(cell.trim())
  if (hasPipe && source.startsWith('|')) cells.shift()
  if (hasPipe && source.endsWith('|') && !isEscaped(source, source.length - 1) && !codeMarker) cells.pop()
  return { cells, hasPipe }
}

function tableHeader(lines, index) {
  if (index + 1 >= lines.length) return null
  const header = tableCells(lines[index])
  const separator = tableCells(lines[index + 1])
  if (!(header.hasPipe || separator.hasPipe) || !header.cells.length || header.cells.length !== separator.cells.length
    || !separator.cells.every((cell) => /^:?-{3,}:?$/.test(cell))) return null
  const align = separator.cells.map((cell) => cell.startsWith(':') ? cell.endsWith(':') ? 'center' : 'left' : cell.endsWith(':') ? 'right' : null)
  return { headers: header.cells, align }
}

function fence(line) {
  const match = line.match(/^ {0,3}(`{3,})(.*)$/)
  if (!match || match[2].includes('`')) return null
  return { marker: match[1], language: match[2].trim() }
}

function listItem(line) {
  const match = line.match(/^([ \t]*)([-+*•]|\d+[.)])\s+(.*)$/)
  if (!match) return null
  return {
    indent: match[1].replace(/\t/g, '    ').length,
    ordered: /^\d/.test(match[2]),
    start: /^\d/.test(match[2]) ? Number.parseInt(match[2], 10) : undefined,
    text: match[3],
  }
}

function horizontalRule(line) {
  return /^ {0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$/.test(line)
}

function parseList(lines, start, depth) {
  const first = listItem(lines[start])
  const block = { type: 'list', ordered: first.ordered, start: first.start, items: [] }
  let index = start
  while (index < lines.length) {
    const item = listItem(lines[index])
    if (item && item.indent === first.indent && item.ordered === first.ordered) {
      block.items.push({ text: item.text, children: [] })
      index += 1
    } else if (item && item.indent > first.indent && depth < MAX_NESTING) {
      const nested = parseList(lines, index, depth + 1)
      block.items[block.items.length - 1].children.push(nested.block)
      index = nested.end
    } else if (!lines[index].trim() && index + 1 < lines.length && listItem(lines[index + 1])?.indent >= first.indent) {
      index += 1
    } else {
      const indentation = lines[index].match(/^[ \t]*/)[0].replace(/\t/g, '    ').length
      if (item || !lines[index].trim() || indentation <= first.indent) break
      block.items[block.items.length - 1].text += `\n${lines[index].trim()}`
      index += 1
    }
  }
  return { block, end: index }
}

function startsBlock(lines, index) {
  const line = lines[index]
  return Boolean(fence(line) || /^ {0,3}#{1,6}\s+/.test(line) || /^\s*>/.test(line)
    || horizontalRule(line) || listItem(line) || tableHeader(lines, index))
}

// All output is data. Neither raw HTML nor model-provided URLs can become markup.
export function parseMarkdown(value, depth = 0) {
  const lines = String(value ?? '').replace(/\r\n?/g, '\n').split('\n')
  if (depth >= MAX_NESTING) return [{ type: 'paragraph', text: lines.join('\n'), gap: false }]
  const blocks = []
  let gap = false
  let index = 0
  const add = (block) => {
    blocks.push({ ...block, gap })
    gap = false
  }
  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) {
      gap = blocks.length > 0
      index += 1
      continue
    }
    const opening = fence(line)
    if (opening) {
      const content = []
      let closed = false
      index += 1
      for (; index < lines.length; index += 1) {
        const ending = lines[index].match(/^ {0,3}(`+)\s*$/)
        if (ending && ending[1][0] === opening.marker[0] && ending[1].length >= opening.marker.length) {
          closed = true
          index += 1
          break
        }
        content.push(lines[index])
      }
      add({ type: 'code', language: opening.language, text: content.join('\n'), closed })
      continue
    }
    const table = tableHeader(lines, index)
    if (table) {
      const rows = []
      index += 2
      while (index < lines.length && lines[index].trim()) {
        if (fence(lines[index]) || /^\s*>/.test(lines[index]) || /^ {0,3}#{1,6}\s+/.test(lines[index]) || horizontalRule(lines[index]) || listItem(lines[index])) break
        const row = tableCells(lines[index])
        if (!row.hasPipe && table.headers.length > 1) break
        rows.push(table.headers.map((_, cellIndex) => row.cells[cellIndex] || ''))
        index += 1
      }
      add({ type: 'table', ...table, rows })
      continue
    }
    const heading = line.match(/^ {0,3}(#{1,6})\s+(.*)$/)
    if (heading) {
      add({ type: 'heading', level: heading[1].length, text: heading[2].replace(/\s+#+\s*$/, '') })
      index += 1
      continue
    }
    if (horizontalRule(line)) {
      add({ type: 'rule' })
      index += 1
      continue
    }
    if (/^\s*>/.test(line)) {
      const quote = []
      while (index < lines.length && /^\s*>/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*> ?/, ''))
        index += 1
      }
      add({ type: 'quote', children: parseMarkdown(quote.join('\n'), depth + 1) })
      continue
    }
    if (listItem(line)) {
      const list = parseList(lines, index, depth)
      add(list.block)
      index = list.end
      continue
    }
    const paragraph = [line]
    index += 1
    while (index < lines.length && lines[index].trim() && !startsBlock(lines, index)) {
      paragraph.push(lines[index])
      index += 1
    }
    add({ type: 'paragraph', text: paragraph.join('\n') })
  }
  return blocks
}
