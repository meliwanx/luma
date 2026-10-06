import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'
import { browserProgressLabel, fetchFileBlob, isBrowserLiveUrl, isImageFile, liveBrowserEvents, preserveBrowserLiveMessages } from './browser-tools.js'

const source = await readFile(new URL('./sandbox-cards.jsx', import.meta.url), 'utf8')
const { code } = await transformWithEsbuild(source.replace(/^import .*\n/gm, '').replace('export function SandboxToolCard', 'function SandboxToolCard'), 'cards.jsx', {
  loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement',
})
const context = {
  React, useEffect: React.useEffect, useMemo: React.useMemo, useState: React.useState,
  isBrowserLiveUrl, isImageFile, fetchFileBlob, URL,
}
vm.createContext(context)
vm.runInContext(code, context)
const renderCard = (item) => renderToStaticMarkup(React.createElement(context.SandboxToolCard, { item, apiUrl: '/api/v1' }))

test('live card opens a Tencent view in a protected new window without printing the credential', () => {
  const url = 'https://9000-sandbox.ap-hongkong.tencentags.com/novnc/vnc_lite.html?access_token=temporary-fixture'
  const html = renderCard({ kind: 'sandbox', data: { kind: 'browser_live', url, expires_in: 60 } })
  assert.match(html, /观看实时画面/)
  assert.match(html, /target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer"/)
  assert.doesNotMatch(html.replace(/<[^>]*>/g, ''), /temporary-fixture|https:/)
  assert.doesNotMatch(html, /<iframe/)
})

test('live URLs reject unrelated, spoofed, insecure and credential-bearing hosts', () => {
  assert.equal(isBrowserLiveUrl('https://9000-test.ap-hongkong.tencentags.com/novnc/'), true)
  for (const url of ['https://tencentags.com/', 'http://test.tencentags.com/', 'https://test.tencentags.com.evil.example/', 'https://evil.example/', 'https://user:pass@test.tencentags.com/', 'https://test.tencentags.com:8443/', 'javascript:alert(1)']) {
    assert.equal(isBrowserLiveUrl(url), false, url)
    assert.doesNotMatch(renderCard({ kind: 'browser_live', url }), /href=/)
  }
})

test('image thumbnails share the authenticated download and never use tool-provided URLs', async () => {
  const original = globalThis.fetch
  const blob = new Blob(['png-fixture'], { type: 'image/png' })
  let request
  globalThis.fetch = async (...args) => { request = args; return { ok: true, blob: async () => blob } }
  try {
    assert.equal(await fetchFileBlob('/api/v1', 'file/slash', () => ({ Authorization: 'Bearer fixture' })), blob)
    assert.equal(request[0], '/api/v1/files/file%2Fslash/content')
    assert.equal(request[1].headers.Authorization, 'Bearer fixture')
    assert.equal(request[1].credentials, 'include')
    globalThis.fetch = async () => ({ ok: false, status: 403 })
    await assert.rejects(fetchFileBlob('/api/v1', 'file', () => ({})), /403/)
  } finally { globalThis.fetch = original }
  assert.equal(isImageFile('image/png'), true)
  assert.equal(isImageFile('IMAGE/JPEG; charset=binary'), true)
  assert.equal(isImageFile('image/svg+xml'), false)
  assert.equal(isImageFile('text/html'), false)
  assert.match(source, /<img className="sandbox-file-thumbnail" src=\{thumbnail\}/)
})

test('persisted screenshot payload renders a file card without tool-provided URLs', () => {
  const html = renderCard({
    kind: 'tool_result', call_id: 'screenshot-call',
    payload: { kind: 'file', file_id: 'screenshot-1', filename: 'homepage.png', media_type: 'image/png', url: 'https://untrusted.example/image.png' },
  })
  assert.match(html, /sandbox-file-card/)
  assert.match(html, /homepage.png/)
  assert.match(html, /下载/)
  assert.doesNotMatch(html, /untrusted.example|https:\/\//)
})

test('browser progress is readable and live credentials remain in transient events during refresh', () => {
  assert.equal(browserProgressLabel('browser.open'), '正在打开网页…')
  assert.equal(browserProgressLabel('browser.read'), '正在读取页面…')
  assert.equal(browserProgressLabel('mcp.search'), '')
  const live = { call_id: 'live-1', tool: 'browser.live', data: { kind: 'browser_live', url: 'https://view.tencentags.com/?access_token=fixture' } }
  const previous = [{ id: 'a1', content: '已打开实时画面', metadata: {}, toolEvents: [live, { tool: 'browser.read' }] }]
  const remote = [{ id: 'a1', content: '已打开实时画面', metadata: { tool_calls: [{ call_id: 'live-1', tool: 'browser.live' }] } }]
  const result = preserveBrowserLiveMessages(remote, previous)
  assert.deepEqual(liveBrowserEvents(result[0].toolEvents), [live])
  assert.doesNotMatch(JSON.stringify(result[0].metadata), /access_token/)
  assert.doesNotMatch(result[0].content, /access_token/)
  assert.equal(remote[0].toolEvents, undefined)
})

test('browser permissions render in their own group', async () => {
  const panelsSource = await readFile(new URL('./sandbox-panels.jsx', import.meta.url), 'utf8')
  const { code: panelsCode } = await transformWithEsbuild(panelsSource.replace(/^import .*\n/gm, '').replaceAll('export function ', 'function '), 'panels.jsx', {
    loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement',
  })
  let state = 0
  const panels = {
    React, useEffect: () => {}, useMemo: (read) => read(),
    useState: (initial) => [state++ === 0 ? [{ key: 'browser.submit', category: 'browser', label: '提交网页', mode: 'ask', allow_always: true }] : initial, () => {}],
  }
  vm.createContext(panels)
  vm.runInContext(panelsCode, panels)
  const html = renderToStaticMarkup(React.createElement(panels.PermissionsPanel, { request: () => {}, notify: () => {} }))
  const browser = html.slice(html.indexOf('<h4>浏览器</h4>'))
  assert.match(browser, /提交网页/)
  assert.match(browser, /每次询问/)
  assert.doesNotMatch(html.slice(html.indexOf('<h4>Luma</h4>'), html.indexOf('<h4>浏览器</h4>')), /提交网页/)
})
