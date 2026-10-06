import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'
import { PROACTIVE_DEFAULTS, proactivePrefs, saveProactivePrefs, validateProactivePrefs } from './content-data.js'
import { startIdeaSession } from './content-sessions.js'

const source = await readFile(new URL('./proactive-labels.jsx', import.meta.url), 'utf8')
const { code } = await transformWithEsbuild(source.replace(/^import .*\n/gm, '').replace('export function', 'function'), 'proactive-labels.jsx', { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' })
const context = { React, useState: React.useState, ContentDialog: ({ title, children }) => React.createElement('section', null, title, children) }
vm.createContext(context)
vm.runInContext(code, context)
const renderLabels = (message) => renderToStaticMarkup(React.createElement(context.ProactiveLabels, { message }))

test('assistant proactive label renders and reason stays escaped plain text', () => {
  const html = renderLabels({ role: 'assistant', metadata: { proactive: { kind: 'tip', reason: '<img src=x onerror=alert(1)>' } } })
  assert.match(html, /class="message-proactive-label"/)
  assert.match(html, />主动<\/button>/)
  assert.match(html, /title="&lt;img src=x onerror=alert\(1\)&gt;"/)
  assert.doesNotMatch(html, /<img|onerror="/)
  assert.match(source, /ContentDialog title="为什么发给你"/)
})

test('feed label renders separately and ordinary/user messages have no proactive tags', () => {
  const metadata = { feed_post_id: 'post-1', proactive: { reason: '跟进' } }
  const html = renderLabels({ role: 'assistant', metadata })
  assert.match(html, /来自动态/)
  assert.match(html, /主动/)
  assert.equal(renderLabels({ role: 'assistant', metadata: {} }), '')
  assert.equal(renderLabels({ role: 'user', metadata }), '')
})

test('prefs defaults follow the contract and exclude unexpected fields', () => {
  assert.equal(PROACTIVE_DEFAULTS.timezone, 'Asia/Shanghai')
  assert.equal(PROACTIVE_DEFAULTS.window_end, '21:30')
  const prefs = proactivePrefs({ enabled: false, max_per_day: 0, feed_per_day: 0, unexpected: 'ignore' })
  assert.equal(prefs.enabled, false)
  assert.equal(prefs.max_per_day, 0)
  assert.equal(prefs.feed_per_day, 0)
  assert.equal(prefs.unexpected, undefined)
})

test('prefs validates ranges, time inputs, and text limits', () => {
  assert.equal(validateProactivePrefs(PROACTIVE_DEFAULTS), '')
  assert.equal(validateProactivePrefs({ ...PROACTIVE_DEFAULTS, max_per_day: 5, feed_per_day: 3 }), '')
  for (const patch of [{ max_per_day: 6 }, { max_per_day: 1.5 }, { max_per_day: -1 }, { feed_per_day: 4 }, { window_start: '24:00' }, { window_end: '09:60' }, { topics_like: 'x'.repeat(1001) }, { topics_avoid: 'x'.repeat(1001) }, { style: 'x'.repeat(501) }]) {
    assert.notEqual(validateProactivePrefs({ ...PROACTIVE_DEFAULTS, ...patch }), '')
  }
})

test('saving proactive settings preserves feed instructions edited since loading', async () => {
  const calls = []
  const request = async (path, options) => { calls.push({ path, options }); return options ? {} : { feed_instructions: '最新动态说明' } }
  const result = await saveProactivePrefs(request, { ...PROACTIVE_DEFAULTS, style: '简洁', feed_instructions: '旧说明' })
  assert.equal(calls[0].path, '/proactive/prefs')
  assert.equal(calls[1].options.method, 'PUT')
  const payload = JSON.parse(calls[1].options.body)
  assert.equal(payload.feed_instructions, '最新动态说明')
  assert.equal(payload.style, '简洁')
  assert.deepEqual(result, payload)
})

test('idea start opens and focuses the returned session before normal send with explicit session id', async () => {
  const calls = []
  const result = await startIdeaSession({ session_id: 'side-1', prompt: '我们开始做这个' }, async (...args) => { calls.push(['open', ...args]); return true }, async (...args) => { calls.push(['send', ...args]); return true })
  assert.equal(result, true)
  assert.deepEqual(calls, [['open', 'side-1', { focus: true }], ['send', '我们开始做这个', {}, 'side-1']])
})

test('idea start never sends after a failed session switch or invalid response', async () => {
  let sent = false
  assert.equal(await startIdeaSession({ session_id: 's', prompt: 'p' }, async () => false, async () => { sent = true }), false)
  assert.equal(sent, false)
  await assert.rejects(startIdeaSession({ session_id: 's', prompt: '' }, async () => true, async () => { sent = true }), /无效/)
  assert.equal(sent, false)
})

test('navigation installs new pages and memory lives inside settings', async () => {
  const main = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
  assert.match(main, /view === 'library' && <LibraryPage/)
  assert.match(main, /view === 'ideas' && <IdeasPage/)
  assert.match(main, /view === 'activity' && <FeedPage/)
  assert.doesNotMatch(main, /view === 'memory' &&|<ActivityView/)
  assert.match(main, /memory: <>[\s\S]*?<MemoryView/)
  assert.match(main, /isAssistant && <ProactiveLabels message=\{message\}/)
})

test('content navigation opens immediately while refreshing the session list in the background', async () => {
  const main = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
  const script = main.slice(main.indexOf('  async function openContentSession('), main.indexOf('  async function startIdeaChat('))
  const calls = []
  const state = { current: '' }
  const sandbox = { activeSessionIdRef: state,
    openSession: async (id) => { calls.push('open'); state.current = id; return true },
    refreshSessionList: () => { calls.push('refresh'); return new Promise(() => {}) },
    setChatsPanelOpen: (value) => calls.push(value),
    setComposerResetKey: (update) => calls.push(update(0)),
  }
  vm.createContext(sandbox)
  vm.runInContext(script, sandbox)
  assert.equal(await sandbox.openContentSession('new', { focus: true }), true)
  assert.deepEqual(calls, ['open', 'refresh', false, 1])
})

test('a late content navigation does not steal focus after the user selects another chat', async () => {
  const main = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
  const script = main.slice(main.indexOf('  async function openContentSession('), main.indexOf('  async function startIdeaChat('))
  let complete
  let focused = false
  const state = { current: 'old' }
  const sandbox = { activeSessionIdRef: state,
    openSession: (id) => { state.current = id; return new Promise((resolve) => { complete = resolve }) },
    refreshSessionList: async () => [],
    setChatsPanelOpen: () => { focused = true },
    setComposerResetKey: () => { focused = true },
  }
  vm.createContext(sandbox)
  vm.runInContext(script, sandbox)
  const opening = sandbox.openContentSession('new', { focus: true })
  state.current = 'later-selection'
  complete(true)
  assert.equal(await opening, false)
  assert.equal(focused, false)
  assert.equal(state.current, 'later-selection')
})
