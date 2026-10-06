import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'
import { displaySecretReferences, toolStatusKey } from './mcp-chat.js'
import { liveBrowserEvents } from './browser-tools.js'

const source = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
const header = source.slice(source.indexOf('<header className="topbar chat-topbar">'), source.indexOf("        {view === 'chat' && <ChatView"))
const fragments = [
  source.slice(source.indexOf('const PSEUDO_STRONG'), source.indexOf('function ToolStatusRows(')),
  source.slice(source.indexOf('function MessageText('), source.indexOf('const contextTabs')),
  `function Header() { return (${header}) }`,
].join('\n')
const { code } = await transformWithEsbuild(fragments, 'main-ui.jsx', {
  loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
})

function renderHarness(overrides = {}) {
  const context = {
    React, displaySecretReferences, toolStatusKey, liveBrowserEvents,
    Icon: ({ name }) => React.createElement('svg', { 'data-icon': name }),
    LumaLogo: () => React.createElement('svg', { 'data-logo': 'luma' }),
    Markdown: ({ text }) => React.createElement('pre', null, text),
    ChatWidget: ({ widget }) => React.createElement('div', { 'data-widget-id': widget.id }, '结构化卡片'),
    ToolStatusRows: () => null,
    ProactiveLabels: () => null,
    view: 'chat', currentSession: { kind: 'side' }, viewLabel: '旁聊',
    chatsPanelOpen: false, completedSessions: new Set(), panelOpen: false,
    data: { sessions: [{ id: 'main', kind: 'main' }] },
    returnToMainChat: () => {}, openSession: () => {}, setChatsPanelOpen: () => {}, setPanelOpen: () => {},
    ...overrides,
  }
  vm.createContext(context)
  vm.runInContext(code, context)
  return context
}

test('side chat has a separate back control; main chat and other views do not', () => {
  const side = renderToStaticMarkup(React.createElement(renderHarness().Header))
  assert.match(side, /class="chat-back-btn"[^>]*aria-label="返回主聊天"/)
  assert.match(side, /data-icon="arrow-left"/)
  assert.match(side, /<\/button><div class="chat-menu-pill">/)
  assert.doesNotMatch(side, /notification-toggle|data-icon="bell"/)
  assert.match(side, /aria-label="打开动态面板"/)
  for (const overrides of [{ currentSession: { kind: 'main' } }, { view: 'activity' }]) {
    const html = renderToStaticMarkup(React.createElement(renderHarness(overrides).Header))
    assert.doesNotMatch(html, /chat-back-btn/)
  }
  const aside = source.slice(source.indexOf('<aside className="sidebar">'), source.indexOf('</aside>'))
  assert.doesNotMatch(aside, /className="new-chat"/)
})

function workspaceHandler(name) {
  const start = source.indexOf(`  function ${name}(`)
  assert.notEqual(start, -1, `${name} handler exists`)
  const end = source.indexOf('\n  }', start)
  assert.notEqual(end, -1, `${name} handler ends`)
  return source.slice(start, end + 4)
}

const navItemsStart = source.indexOf('const navItems = [')
const navigationItems = vm.runInNewContext(`${source.slice(navItemsStart, source.indexOf('\n]', navItemsStart) + 2)}\nnavItems`)

function navigationHarness({ view = 'chat', chatsPanelOpen = true, protocol = 'https:' } = {}) {
  const state = { view, chatsPanelOpen, searchOpen: false, settingsOpen: false }
  const calls = []
  function setState(name, update) {
    state[name] = typeof update === 'function' ? update(state[name]) : update
    context.view = state.view
    calls.push([name, state[name]])
  }
  const context = {
    view,
    setView: (update) => setState('view', update),
    setChatsPanelOpen: (update) => setState('chatsPanelOpen', update),
    setSearchOpen: (update) => setState('searchOpen', update),
    setSettingsOpen: (update) => setState('settingsOpen', update),
    loadProviderStatus: () => calls.push(['loadProviderStatus']),
    API_URL: 'https://luma.example/api/v1',
    window: { location: { protocol }, open: (url) => calls.push(['window.open', url]) },
    navigate: (path) => calls.push(['navigate', path]),
    useEffect: (effect, dependencies) => {
      assert.deepEqual(Array.from(dependencies), [state.view])
      effect()
    },
  }
  vm.createContext(context)
  vm.runInContext(['handleNavClick', 'openSettings', 'openAdmin'].map(workspaceHandler).join('\n'), context)
  return { context, state, calls }
}

test('non-chat navigation closes the chat list before switching views or opening search', () => {
  for (const item of navigationItems.filter((item) => item.id !== 'chat')) {
    for (const view of ['chat', 'ideas']) {
      const h = navigationHarness({ view })
      h.context.handleNavClick(item)
      assert.equal(h.state.chatsPanelOpen, false, item.id)
      assert.equal(h.state.view, item.action || item.id)
      assert.deepEqual(h.calls, item.id === 'search'
        ? [['chatsPanelOpen', false], ['view', 'chat'], ['searchOpen', true]]
        : [['chatsPanelOpen', false], ['view', item.action || item.id]])
    }
  }
})

test('chat navigation still toggles the list or returns to chat with the list open', () => {
  const item = navigationItems.find((item) => item.id === 'chat')
  for (const chatsPanelOpen of [false, true]) {
    const chat = navigationHarness({ chatsPanelOpen })
    chat.context.handleNavClick(item)
    assert.equal(chat.state.view, 'chat')
    assert.deepEqual(chat.calls, [['chatsPanelOpen', !chatsPanelOpen]])
    const other = navigationHarness({ view: 'library', chatsPanelOpen })
    other.context.handleNavClick(item)
    assert.deepEqual(other.calls, [['view', 'chat'], ['chatsPanelOpen', true]])
  }
})

test('settings and avatar share a handler that closes the chat list before opening settings', () => {
  const sidebar = source.slice(source.indexOf('<aside className="sidebar">'), source.indexOf('</aside>'))
  for (const className of ['avatar', 'rail-settings']) {
    assert.match(sidebar, new RegExp(`className="${className}"[^>]*onClick=\\{openSettings\\}`))
  }
  const h = navigationHarness()
  h.context.openSettings()
  assert.equal(h.state.chatsPanelOpen, false)
  assert.equal(h.state.settingsOpen, true)
  assert.deepEqual(h.calls, [['chatsPanelOpen', false], ['settingsOpen', true], ['loadProviderStatus']])
})

test('admin entry closes the chat list before navigating in Web and Electron', () => {
  assert.match(source, /<SettingsModal[^>]*onOpenAdmin=\{openAdmin\}/)
  assert.match(source, /<SettingRow title="打开后台管理"[^\n]*onClick=\{onOpenAdmin\}/)
  for (const protocol of ['https:', 'file:']) {
    const h = navigationHarness({ protocol })
    h.context.openAdmin()
    assert.equal(h.state.chatsPanelOpen, false)
    assert.deepEqual(h.calls, [['chatsPanelOpen', false], protocol === 'file:'
      ? ['window.open', 'https://luma.example/admin']
      : ['navigate', '/admin']])
  }
})

test('view effect closes the chat list for any non-chat view without changing chat state', () => {
  const viewEffect = source.match(/  useEffect\(\(\) => \{(?:(?!\n  \},)[\s\S])*\n  \}, \[view\]\)/)?.[0]
  assert.ok(viewEffect, 'a view-dependent effect exists')
  for (const view of ['activity', 'ideas', 'missions', 'library', 'future-view', 'chat']) {
    for (const chatsPanelOpen of [false, true]) {
      const h = navigationHarness({ view, chatsPanelOpen })
      vm.runInContext(viewEffect, h.context)
      assert.equal(h.state.chatsPanelOpen, view === 'chat' ? chatsPanelOpen : false)
      assert.deepEqual(h.calls, view === 'chat' ? [] : [['chatsPanelOpen', false]])
    }
  }
})

function returnHarness(sessions, response, error) {
  let data = { sessions }
  const opened = [], requests = [], notices = []
  const context = {
    data,
    request: async (path) => { requests.push(path); if (error) throw error; return response },
    setData: (update) => { data = update(data) },
    openSession: async (id) => { opened.push(id) },
    notify: (notice) => notices.push(notice),
  }
  vm.createContext(context)
  vm.runInContext(source.slice(source.indexOf('  async function returnToMainChat('), source.indexOf('  function desktopChatReady(')), context)
  return { context, opened, requests, notices, current: () => data }
}

test('return uses the canonical loaded main session and existing session switching', async () => {
  const h = returnHarness([{ id: 'side', kind: 'side' }, { id: 'main', kind: 'main' }])
  await h.context.returnToMainChat()
  assert.deepEqual(h.opened, ['main'])
  assert.deepEqual(h.requests, [])
  assert.deepEqual(h.notices, [])
})

test('return restores the main session from the server if absent from the list', async () => {
  const h = returnHarness([{ id: 'side', kind: 'side' }], { id: 'main', kind: 'main' })
  await h.context.returnToMainChat()
  assert.deepEqual(h.requests, ['/sessions/main'])
  assert.deepEqual(h.opened, ['main'])
  assert.deepEqual(Array.from(h.current().sessions, (session) => session.id), ['main', 'side'])
})

test('a failed main-session lookup leaves the chat intact and reports the failure', async () => {
  for (const response of [null, {}]) {
    const h = returnHarness([{ id: 'side', kind: 'side' }], response)
    await h.context.returnToMainChat()
    assert.deepEqual(h.opened, [])
    assert.equal(h.current().sessions[0].id, 'side')
    assert.deepEqual(h.notices, ['无法返回主聊天，请稍后重试'])
  }
  const h = returnHarness([{ id: 'side', kind: 'side' }], null, new Error('服务不可用'))
  await h.context.returnToMainChat()
  assert.deepEqual(h.opened, [])
  assert.equal(h.notices.length, 1)
})

test('message text masks stored secret references before Markdown receives them', () => {
  const reference = '{{secret:sec_0123456789abcdef}}'
  let received = ''
  const context = renderHarness({ Markdown: ({ text }) => { received = text; return React.createElement('p', null, text) } })
  renderToStaticMarkup(React.createElement(context.MessageText, { text: `| 令牌 |\n|---|\n| ${reference} |` }))
  assert.doesNotMatch(received, /sec_0123456789abcdef|\{\{secret:/)
  assert.match(received, /••••••（已加密保存）/)
})

test('empty replies and streaming cards only show three loading dots', () => {
  const context = renderHarness()
  for (const content of ['', '```luma-ui\n{"type":"form"', '<tool_call>{"name":"luma.test"']) {
    const html = renderToStaticMarkup(React.createElement(context.Message, {
      message: { id: 'stream', role: 'assistant', content, status: 'streaming' },
    }))
    assert.equal((html.match(/<i><\/i>/g) || []).length, 3)
    assert.equal(html.replace(/<[^>]*>/g, ''), '')
    assert.match(html, /role="status"/)
  }
})

test('widget markers still render cards while fenced marker examples stay literal', () => {
  const context = renderHarness()
  const html = renderToStaticMarkup(React.createElement(context.Message, {
    message: {
      id: 'done', role: 'assistant', status: 'complete',
      content: '表格如下\n[[widget:w1]]\n```text\n[[widget:w1]]\n```',
      metadata: { widgets: [{ id: 'w1', type: 'form' }] },
    },
  }))
  assert.equal((html.match(/data-widget-id="w1"/g) || []).length, 1)
  assert.match(html, /\[\[widget:w1\]\]/)
})

test('durable live markers do not hide the client-only browser viewing event after done', () => {
  let events = []
  const live = { stream_key: 'tool:live-1', call_id: 'live-1', tool: 'browser.live', status: 'ok', data: { kind: 'browser_live', url: 'https://view.tencentags.com/?access_token=fixture' } }
  const context = renderHarness({ ToolStatusRows: ({ events: value }) => { events = value; return null } })
  renderToStaticMarkup(React.createElement(context.Message, {
    message: { id: 'a1', role: 'assistant', status: 'complete', content: '已打开实时画面', metadata: { tool_calls: [{ call_id: 'live-1', tool: 'browser.live', status: 'ok' }] }, toolEvents: [live] },
  }))
  assert.equal(events.length, 1)
  assert.equal(events[0], live)
})

test('final messages read persisted file events and avoid duplicate screenshot cards', () => {
  let events = []
  const file = { kind: 'file', file_id: 'screenshot-1', filename: 'homepage.png', media_type: 'image/png' }
  const call = { call_id: 'screenshot-call', tool: 'browser.screenshot', status: 'ok', data: file }
  const event = { kind: 'tool_result', call_id: 'screenshot-call', payload: file }
  const context = renderHarness({ ToolStatusRows: ({ events: value }) => { events = value; return null } })
  for (const metadata of [{ tool_calls: [call], tool_events: [event] }, { tool_events: [event] }]) {
    const html = renderToStaticMarkup(React.createElement(context.Message, {
      message: { id: 'a1', role: 'assistant', status: 'complete', content: '已截取首页。', metadata },
    }))
    assert.match(html, /已截取首页。/)
    assert.equal(events.length, 1)
    assert.equal((events[0].payload || events[0].data).file_id, 'screenshot-1')
  }
})
