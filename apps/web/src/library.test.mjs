import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'
import { LIBRARY_TYPES, LIBRARY_VIEW_KEY, filterLibraryItems, isLibraryImageBlob, libraryCategoryRows, libraryGroups, libraryPreviewKind, libraryQuery, libraryType, patchLibraryItem, readLibraryView, writeLibraryView } from './library-data.js'

const source = await readFile(new URL('./library.jsx', import.meta.url), 'utf8')
const { code } = await transformWithEsbuild(source.replace(/^import .*\n/gm, '').replaceAll('export function ', 'function ').replace('export default function ', 'function '), 'library.jsx', {
  loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement',
})
const Icon = ({ name }) => React.createElement('svg', { 'data-icon': name })
const context = {
  React, useState: React.useState, useEffect: React.useEffect, useMemo: React.useMemo, useCallback: React.useCallback, useRef: React.useRef,
  Icon, ContentIcon: Icon, Markdown: ({ text }) => React.createElement('div', { className: 'safe-markdown' }, text),
  LIBRARY_TYPES, filterLibraryItems, isLibraryImageBlob, libraryCategoryRows, libraryGroups, libraryPreviewKind, libraryQuery, libraryType, patchLibraryItem, readLibraryView, writeLibraryView,
  relativeTime: () => '刚刚',
}
vm.createContext(context)
vm.runInContext(code, context)
const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props))
const preview = (value, extra = {}) => render(context.LibraryPreview, { preview: value, onDownload: () => {}, onCopy: () => {}, ...extra })

test('library filters each resource type and separates pinned items without affecting counts', () => {
  const items = LIBRARY_TYPES.filter((entry) => entry.type !== 'all').map((entry, index) => ({ id: String(index), type: entry.type, pinned: index === 0 }))
  assert.equal(filterLibraryItems(items).length, 7)
  for (const entry of LIBRARY_TYPES.slice(1)) assert.deepEqual(filterLibraryItems(items, entry.type).map((item) => item.type), [entry.type])
  assert.deepEqual(libraryGroups(items).pinned.map((item) => item.id), ['0'])
  assert.equal(libraryGroups(items).recent.length, 6)
  assert.deepEqual(filterLibraryItems(null), [])
})

test('library category counts render the server total instead of the number of loaded cards', () => {
  const counts = { all: 284, document: 96, sheet: 23, web: 8, image: 105, code: 31, archive: 12, other: 9 }
  const html = render(context.LibraryCategories, { counts, type: 'sheet', onChange: () => {} })
  for (const entry of LIBRARY_TYPES) {
    assert.match(html, new RegExp(`<span>${entry.label}</span><small>${counts[entry.type]}</small>`))
  }
  assert.match(html, /class="active" aria-pressed="true"[^>]*>.*?<span>表格<\/span>/)
  assert.equal(libraryCategoryRows({ all: -1, document: '8', image: 'invalid' })[0].count, 0)
  assert.equal(libraryCategoryRows({ document: '8' })[1].count, 8)
  assert.equal(libraryCategoryRows({ image: 'invalid' })[4].count, 0)
})

test('library fallback types cover the contract extensions and respect the server type', () => {
  for (const [filename, type] of [['REPORT.DOCX', 'document'], ['notes.md', 'document'], ['data.xlsx', 'sheet'], ['page.htm', 'web'], ['shot.JPEG', 'image'], ['run.sh', 'code'], ['bundle.tar.gz', 'archive'], ['bundle.tgz', 'archive'], ['attachment.bin', 'other']]) {
    assert.equal(libraryType({ filename }), type, filename)
  }
  assert.equal(libraryType({ media_type: 'image/png' }), 'image')
  assert.equal(libraryType({ type: 'other', filename: 'example.txt' }), 'other')
  assert.equal(isLibraryImageBlob(new Blob(['x'], { type: 'image/png' })), true)
  assert.equal(isLibraryImageBlob(new Blob(['x'], { type: 'text/html' })), false)
  assert.equal(isLibraryImageBlob(new Blob(['x'], { type: 'image/svg+xml' })), false)
})

test('html_source preview escapes markup as source and never renders an executable document', () => {
  const html = preview({ kind: 'html_source', text: '<script>alert(1)</script><img src=x onerror="alert(2)">', truncated: true })
  assert.match(html, /网页产物仅显示源码，下载后可在浏览器中打开/)
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/)
  assert.match(html, /&lt;img src=x onerror=/)
  assert.match(html, /预览已截断/)
  assert.doesNotMatch(html, /<iframe|<script|<img/)
  assert.doesNotMatch(source, /dangerouslySetInnerHTML|innerHTML|<iframe/)
})

test('library preview renders csv, safe markdown, text, images and unsupported branches', () => {
  const csv = preview({ kind: 'csv', columns: ['name', '<svg>'], rows: [['Alice', '<script>']], total_rows: 301, truncated: true })
  assert.match(csv, /共 301 行/)
  assert.match(csv, /仅展示部分行或列/)
  assert.match(csv, /<table>/)
  assert.match(csv, /&lt;script&gt;/)
  assert.match(preview({ kind: 'markdown', text: '# 说明' }), /class="safe-markdown"/)
  const text = preview({ kind: 'text', text: '<tag>code</tag>', language: 'python' })
  assert.match(text, /python/)
  assert.match(text, /复制/)
  assert.match(text, /<pre[^>]*><code>&lt;tag&gt;code&lt;\/tag&gt;<\/code>/)
  assert.match(preview({ kind: 'image' }, { imageUrl: 'blob:test-image' }), /<img[^>]*src="blob:test-image"/)
  const loadingImage = preview({ kind: 'image' }, { imageLoading: true })
  assert.match(loadingImage, /正在加载图片/)
  assert.doesNotMatch(loadingImage, /无法预览/)
  assert.match(preview({ kind: 'none' }), /此类型暂不支持预览/)
  assert.match(preview({ kind: 'unexpected' }), /此类型暂不支持预览/)
  assert.match(preview({ kind: 'none' }), /下载/)
  assert.equal(libraryPreviewKind({ kind: 'html_source' }), 'html_source')
})

test('library query safely encodes search and paging while preserving contract parameters', () => {
  const path = libraryQuery({ type: 'web', q: '报告 & 计划', sort: 'title', cursor: 'a+b=/x' })
  const url = new URL(path, 'https://example.test')
  assert.equal(url.pathname, '/library')
  assert.equal(url.searchParams.get('type'), 'web')
  assert.equal(url.searchParams.get('q'), '报告 & 计划')
  assert.equal(url.searchParams.get('sort'), 'title')
  assert.equal(url.searchParams.get('limit'), '50')
  assert.equal(url.searchParams.get('cursor'), 'a+b=/x')
})

test('view storage falls back safely when browser storage is unavailable', () => {
  const failing = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') } }
  assert.equal(readLibraryView(failing), 'grid')
  assert.doesNotThrow(() => writeLibraryView(failing, 'list'))
  let stored
  const storage = { getItem: () => 'list', setItem: (...args) => { stored = args } }
  assert.equal(readLibraryView(storage), 'list')
  writeLibraryView(storage, 'list')
  assert.deepEqual(stored, [LIBRARY_VIEW_KEY, 'list'])
})

test('library PATCH sends JSON text with exact contract fields and an encoded resource id', async () => {
  let payload
  const updated = { id: 'file/slash', title: '新标题', pinned: true }
  const request = async (path, options) => { payload = { path, ...options }; return updated }
  assert.equal(await patchLibraryItem(request, updated.id, { title: updated.title, pinned: true }), updated)
  assert.equal(payload.path, '/library/file%2Fslash')
  assert.equal(payload.method, 'PATCH')
  assert.equal(typeof payload.body, 'string')
  assert.deepEqual(JSON.parse(payload.body), { title: '新标题', pinned: true })
  assert.match(source, /await patchLibraryItem\(request, id, patch\)/)
})

test('resource cards show safe titles and source conversation links without external asset URLs', () => {
  const html = render(context.LibraryCard, { item: { id: 'file', type: 'document', title: '<script>报告</script>', filename: 'report.md', session_id: 'session', session_title: '计划' }, requestBlob: () => {}, onPreview: () => {}, onOpenSession: () => {} })
  assert.match(html, /来自 计划/)
  assert.match(html, /&lt;script&gt;报告&lt;\/script&gt;/)
  assert.doesNotMatch(html, /<script|href=/)
  assert.match(source, /requestBlob\(filePath\(item\.id\)\)/)
  assert.match(source, /URL\.revokeObjectURL\(objectUrl\)/)
})
