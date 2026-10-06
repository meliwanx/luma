import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'

const source = await readFile(new URL('./logo.jsx', import.meta.url), 'utf8')
const { code } = await transformWithEsbuild(source.replace(/^import .*\n/gm, '').replace('export default ', ''), 'logo.jsx', {
  loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement',
})
const context = { React }
vm.createContext(context)
vm.runInContext(code, context)

test('wordmark uses the supplied vector path with theme ink and a tight viewBox', () => {
  const html = renderToStaticMarkup(React.createElement(context.LumaLogo, { label: 'Luma' }))
  assert.match(html, /viewBox="60 290 1020 480"/)
  assert.match(html, /stroke="currentColor"/)
  assert.match(html, /stroke-width="51"/)
  assert.match(html, /role="img" aria-label="Luma"/)
  assert.equal((html.match(/<path /g) || []).length, 1)
  assert.doesNotMatch(html, /<image|href=|aria-hidden/)
})

test('decorative wordmarks do not repeat nearby accessible labels', () => {
  const html = renderToStaticMarkup(React.createElement(context.LumaLogo, { className: 'empty-state-logo' }))
  assert.match(html, /class="luma-logo empty-state-logo"/)
  assert.match(html, /aria-hidden="true"/)
  assert.doesNotMatch(html, /role="img"|aria-label=/)
})
