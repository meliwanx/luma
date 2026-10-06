import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { transformWithEsbuild } from 'vite'
import { subscribeDesktopCommands } from './desktop-commands.js'

const source = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
const composer = await readFile(new URL('./composer.jsx', import.meta.url), 'utf8')
const { code: composerCode } = await transformWithEsbuild(composer.replace(/^import .*\n/gm, '').replace('export function Composer', 'function Composer'), 'composer.jsx', {
  loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
})

function bridge() {
  let receive
  let removals = 0
  return {
    desktop: { onCommand: (callback) => { receive = callback; return () => { removals += 1 } } },
    emit: (command) => receive(command),
    removals: () => removals,
  }
}

test('browser sessions need no desktop bridge', () => {
  assert.equal(subscribeDesktopCommands(undefined, () => ({})), undefined)
  assert.equal(subscribeDesktopCommands({}, () => ({})), undefined)
})

test('desktop commands only dispatch the three allowed actions', () => {
  const b = bridge()
  const actions = []
  const stop = subscribeDesktopCommands(b.desktop, () => Object.fromEntries([
    'new-side-chat', 'open-search', 'start-voice', 'delete-all', 'constructor',
  ].map((command) => [command, () => actions.push(command)])))
  for (const command of ['new-side-chat', 'open-search', 'start-voice', 'delete-all', 'constructor', { command: 'start-voice' }, null]) b.emit(command)
  assert.deepEqual(actions, ['new-side-chat', 'open-search', 'start-voice'])
  stop()
})

test('one subscription uses the latest handler and ignores events after cleanup', () => {
  const b = bridge()
  const actions = []
  let handlers = { 'open-search': () => actions.push('old') }
  const stop = subscribeDesktopCommands(b.desktop, () => handlers)
  handlers = { 'open-search': () => actions.push('current') }
  b.emit('open-search')
  stop()
  stop()
  b.emit('open-search')
  assert.deepEqual(actions, ['current'])
  assert.equal(b.removals(), 1)
})

test('anonymous listeners consume tray events without executing actions', () => {
  const b = bridge()
  const stop = subscribeDesktopCommands(b.desktop, () => null)
  for (const command of ['new-side-chat', 'open-search', 'start-voice']) assert.doesNotThrow(() => b.emit(command))
  stop()
  assert.equal(b.removals(), 1)
})

function workspace(overrides = {}) {
  const actions = [], notices = []
  const context = {
    data: { sessionId: 'main' }, offline: false, desktopCommandsRef: { current: null },
    notify: (message) => notices.push(message),
    setSettingsOpen: (value) => actions.push(['settings', value]),
    setSearchOpen: (value) => actions.push(['search', value]),
    startNewChat: () => actions.push(['new-chat']),
    setView: (value) => actions.push(['view', value]),
    setVoiceStartKey: (update) => actions.push(['voice', update(0)]),
    ...overrides,
  }
  vm.createContext(context)
  vm.runInContext(source.slice(source.indexOf('  function desktopChatReady('), source.indexOf('  function handleNavClick(')), context)
  return { handlers: context.desktopCommandsRef.current, actions, notices }
}

test('tray actions reuse side chat and search and request only a voice draft', () => {
  const h = workspace()
  h.handlers['new-side-chat']()
  assert.deepEqual(h.actions.splice(0), [['settings', false], ['search', false], ['new-chat']])
  h.handlers['open-search']()
  assert.deepEqual(h.actions.splice(0), [['settings', false], ['search', true]])
  h.handlers['start-voice']()
  assert.deepEqual(h.actions, [['view', 'chat'], ['settings', false], ['search', false], ['voice', 1]])
  assert.deepEqual(h.notices, [])
})

test('tray actions cannot create chats or record before the workspace is ready', () => {
  for (const overrides of [{ data: { sessionId: '' } }, { offline: true }]) {
    const h = workspace(overrides)
    h.handlers['new-side-chat']()
    h.handlers['start-voice']()
    assert.deepEqual(h.actions, [])
    assert.equal(h.notices.length, 2)
    h.handlers['open-search']()
    assert.deepEqual(h.actions, [['settings', false], ['search', true]])
  }
})

function composerHarness({ status = 'idle', command = 1, supported = true } = {}) {
  const effects = [], starts = [], notices = []
  let finishes = 0, consumed = 0, sends = 0, states = 0
  const controller = { start: (context) => starts.push(context), finish: () => { finishes += 1 }, dispose: () => {} }
  const context = {
    React,
    useRef: (initial) => ({ current: initial }),
    useState: () => [states++ === 0 ? { status, elapsedSeconds: 0 } : 0, () => {}],
    useLayoutEffect: (effect, deps) => effects.push({ effect, deps }),
    isRecordingSupported: () => supported,
    createVoiceInput: () => controller,
    insertVoiceText: () => {},
    Icon: () => null,
  }
  vm.createContext(context)
  vm.runInContext(composerCode, context)
  context.Composer({
    input: '', setInput: () => {}, voiceStartKey: command,
    sessionId: 'side', voiceRaw: true,
    notify: (message) => notices.push(message), errorDetail: () => '',
    sendMessage: () => { sends += 1 }, onVoiceStartConsumed: () => { consumed += 1 },
  })
  // Initialize the existing controller before consuming the tray request.
  effects[1].effect()
  effects[3].effect()
  return { starts, notices, finishes, consumed, sends }
}

test('voice tray request starts the existing recorder and is consumed without sending', () => {
  const h = composerHarness()
  assert.deepEqual(JSON.parse(JSON.stringify(h.starts)), [{ sessionId: 'side', raw: true }])
  assert.equal(h.consumed, 1)
  assert.equal(h.finishes, 0)
  assert.equal(h.sends, 0)
  assert.equal(composerHarness({ command: 0 }).starts.length, 0)
})

test('repeated voice tray actions do not finish an active or busy recording', () => {
  for (const status of ['recording', 'requesting', 'processing']) {
    const h = composerHarness({ status })
    assert.equal(h.starts.length, 0)
    assert.equal(h.finishes, 0)
    assert.equal(h.consumed, 1)
    assert.equal(h.sends, 0)
  }
  const unsupported = composerHarness({ supported: false })
  assert.deepEqual(unsupported.notices, ['当前环境不支持录音'])
  assert.equal(unsupported.consumed, 1)
  assert.equal(unsupported.starts.length, 0)
})
