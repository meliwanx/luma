import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { containsSecretJson, optimisticChatContent } from './mcp-chat.js'
import { BACKGROUND_GENERATION_LIMIT_MS, SessionGenerations, disconnectGeneration, generationError, isGeneratingMessage, latestGeneratingMessage } from './session-generations.js'

const source = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
const flush = () => new Promise((resolve) => setImmediate(resolve))
const terminal = (id, content = '已回复') => ({ id, role: 'assistant', status: 'complete', content, created_at: '2026-10-05T12:00:00Z' })

function harness({ consume, fetchResponse, pages = {}, failRequest } = {}) {
  const tracker = new SessionGenerations()
  const requests = [], notices = []
  let current = {
    sessionId: 's1', sessions: [{ id: 'main', kind: 'main' }, { id: 's1', kind: 'side', title: '工作' }, { id: 's2', kind: 'side', title: '学习' }],
    messages: [terminal('old-a')], activity: [], files: [],
  }
  const context = {
    data: current, workspaceRevisionRef: { current: 0 }, generationsRef: { current: tracker }, activeGenerationRef: { current: null },
    activeSessionIdRef: { current: 's1' }, sessionSwitchRef: { current: 0 },
    widgetEventPendingRef: { current: new Set() }, loadedOlderMessagesRef: { current: false }, oldestMessageIdRef: { current: '' },
    offline: false, AbortController, API_URL: 'https://luma.example/api/v1',
    containsSecretJson, optimisticChatContent, disconnectGeneration, generationError, isGeneratingMessage, latestGeneratingMessage,
    plainTextPreview: (value) => String(value || ''), streamAssistantId: () => 'stream-local',
    authHeaders: () => ({}), mergeStreamEvent: (events, event, payload) => [...events, { ...payload, event_type: event }],
    setData: (update) => { current = update(current); context.data = current },
    setGeneratingBySession: () => {}, setCompletedSessions: () => {}, setSentMessageVersion: () => {}, setStreamSubscriptionVersion: () => {},
    setHasMoreMessages: () => {}, setView: () => {}, setFiles: () => {},
    notify: (value) => notices.push(value), notifyDesktop: () => {},
    request: async (path, options) => {
      requests.push({ path, ...options })
      if (failRequest) throw new Error('操作失败')
      if (options?.method === 'PATCH') return { title: JSON.parse(options.body).title }
      return []
    },
    requestMessagesPage: async (id) => ({ messages: pages[id] || [], hasMore: false, oldestId: '' }),
    fetch: async (url, options) => {
      requests.push({ url, ...options })
      return fetchResponse || { ok: true, sessionId: decodeURIComponent(url.match(/sessions\/([^/]+)/)?.[1] || '') }
    },
    consumeSSE: consume || (async (response, apply) => {
      apply('start', { message_id: `a-${response.sessionId}` })
      apply('done', terminal(`a-${response.sessionId}`))
    }),
    window: { dispatchEvent: () => {} },
  }
  vm.createContext(context)
  for (const [start, end] of [
    ['  function publishGenerations(', '  useEffect(() => {\n    let active = true\n    let polling'],
    ['  async function openSession(', '  function findLoadedMessage('],
    ['  async function renameSession(', '  async function sendText('],
    ['  async function sendText(', '  const resumableMessageId'],
    ['  async function cancelMessage(', '  function retryMessage('],
  ]) vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end)), context)
  return {
    context, tracker, requests, notices,
    get current() { return current },
    setMessages: (messages) => context.setData((data) => ({ ...data, messages })),
  }
}

test('generation state keeps independent session ids, cursors and unread completions', () => {
  const tracker = new SessionGenerations()
  tracker.track('main', 'a-main', 100)
  const progress = tracker.track('side', '', 100)
  progress.after = '5-0'
  progress.content = '前半段'
  assert.equal(tracker.track('side', 'a-side', 200), progress)
  assert.equal(tracker.messages.size, 2)
  assert.equal(tracker.finish('side', 'stale-id', { complete: true }), false)
  assert.equal(tracker.finish('main', 'a-main', { selectedSessionId: 'side', complete: true }), true)
  assert.equal(tracker.completed.has('main'), true)
  assert.equal(tracker.messages.get('side'), 'a-side')
  tracker.finish('side', 'a-side', { selectedSessionId: 'side', complete: true })
  assert.equal(tracker.completed.has('side'), false)
  tracker.forget('main')
  assert.equal(tracker.completed.has('main'), false)
})

test('background status polling excludes the selected chat and stops after ten minutes', () => {
  const tracker = new SessionGenerations()
  tracker.track('s1', 'a1', 1000)
  assert.equal(tracker.shouldPoll('s1', 's2', 1001), true)
  assert.equal(tracker.shouldPoll('s1', 's1', 1001), false)
  assert.equal(tracker.shouldPoll('s1', 's2', 1000 + BACKGROUND_GENERATION_LIMIT_MS), false)
  assert.equal(tracker.shouldPoll('missing', 's2', 1001), false)
})

test('pending and streaming assistants resume, but a terminal latest assistant never revives an older stream', () => {
  for (const status of ['pending', 'streaming']) assert.equal(isGeneratingMessage({ role: 'assistant', status }), true)
  assert.equal(isGeneratingMessage({ role: 'user', status: 'pending' }), false)
  assert.equal(latestGeneratingMessage([{ id: 'a1', role: 'assistant', status: 'streaming' }, terminal('a2')]), null)
  assert.equal(latestGeneratingMessage([{ id: 'a1', role: 'assistant', status: 'pending' }]).id, 'a1')
})

test('switching disconnects a stream and a second chat sends while the first cloud reply keeps running', async () => {
  let firstApply, release
  const gate = new Promise((resolve) => { release = resolve })
  const h = harness({ consume: async (response, apply) => {
    apply('start', { message_id: `a-${response.sessionId}` })
    if (response.sessionId === 's1') {
      firstApply = apply
      apply('delta', { content: '前半段' }, '1-0')
      await gate
    } else apply('done', terminal(`a-${response.sessionId}`))
  } })
  const firstSend = h.context.sendText('第一条')
  await flush()
  const subscription = h.context.activeGenerationRef.current
  await h.context.openSession('s2')
  assert.equal(subscription.controller.signal.aborted, true)
  assert.equal(subscription.cancelled, false)
  assert.equal(h.tracker.messages.get('s1'), 'a-s1')
  assert.equal(h.tracker.progress.get('s1').after, '1-0')
  assert.equal(await h.context.sendText('第二条'), true)
  assert.equal(h.tracker.messages.has('s2'), false)
  const secondMessages = JSON.stringify(h.current.messages)
  firstApply('done', terminal('a-s1', '迟到的回调'))
  release()
  await firstSend
  assert.equal(JSON.stringify(h.current.messages), secondMessages)
  assert.equal(h.tracker.messages.has('s1'), true)
  assert.equal(h.requests.some((request) => request.path?.endsWith('/cancel')), false)
  h.context.reconcileGeneration('s1', [terminal('a-s1')], 'a-s1')
  assert.equal(h.tracker.completed.has('s1'), true)
  assert.equal(JSON.stringify(h.current.messages), secondMessages)
})

test('switching before the start event still discovers the durable message without cancelling it', async () => {
  let release
  const h = harness({ consume: async () => new Promise((resolve) => { release = resolve }) })
  const send = h.context.sendText('后台执行')
  await flush()
  await h.context.openSession('s2')
  assert.equal(h.tracker.messages.get('s1'), '')
  h.context.reconcileGeneration('s1', [{ id: 'new-a', role: 'assistant', status: 'pending' }], '')
  assert.equal(h.tracker.messages.get('s1'), 'new-a')
  assert.equal(h.requests.some((request) => request.path?.endsWith('/cancel')), false)
  release()
  await send
})

test('returning to a completed chat acknowledges its badge and loads its final content', async () => {
  const h = harness({ pages: { s1: [terminal('a1', '后台完成的回复')] } })
  h.tracker.track('s1', 'a1')
  await h.context.openSession('s2')
  h.context.reconcileGeneration('s1', [terminal('a1')], 'a1')
  assert.equal(h.tracker.completed.has('s1'), true)
  await h.context.openSession('s1')
  assert.equal(h.tracker.completed.has('s1'), false)
  assert.equal(h.current.messages[0].content, '后台完成的回复')
})

test('reopened pending assistant resumes from its saved cursor and ignores frames after leaving', async () => {
  const h = harness()
  const progress = h.tracker.track('s1', 'a1')
  progress.after = '4-0'
  progress.content = '前半段'
  h.setMessages([{ id: 'a1', role: 'assistant', status: 'pending', content: '前半段' }])
  let options, cleanup
  h.context.useEffect = (effect) => { cleanup = effect() }
  h.context.streamSubscriptionVersion = 0
  h.context.streamResume = (_id, params) => { options = params; return new Promise(() => {}) }
  vm.runInContext(source.slice(source.indexOf('  const resumableMessageId'), source.indexOf('  async function cancelMessage(')), h.context)
  assert.equal(options.after, '4-0')
  options.onEvent('delta', { content: '后半段' }, '5-0')
  assert.equal(h.current.messages[0].content, '前半段后半段')
  await h.context.openSession('s2')
  cleanup()
  const before = JSON.stringify(h.current)
  options.onEvent('done', terminal('a1'))
  assert.equal(JSON.stringify(h.current), before)
  assert.equal(options.signal.aborted, true)
  assert.equal(h.tracker.messages.get('s1'), 'a1')
})

test('only stopping invokes the server cancellation endpoint for the current session', async () => {
  const h = harness()
  h.tracker.track('s1', 'a1')
  h.tracker.track('s2', 'a2')
  h.setMessages([{ id: 'a1', role: 'assistant', status: 'pending' }])
  await h.context.cancelMessage(h.current.messages[0])
  assert.equal(h.requests.filter((request) => request.path?.endsWith('/cancel')).length, 1)
  assert.equal(h.requests[0].path, '/messages/a1/cancel')
  assert.equal(h.tracker.messages.has('s1'), false)
  assert.equal(h.tracker.messages.get('s2'), 'a2')
})

test('stopping before the remote id arrives cancels the discovered message rather than pretending success', async () => {
  let release
  const h = harness({ consume: async () => new Promise((resolve) => { release = resolve }) })
  const send = h.context.sendText('马上停止')
  await flush()
  await h.context.cancelMessage()
  assert.equal(h.notices.at(-1), '正在停止生成')
  assert.equal(h.tracker.messages.has('s1'), true)
  h.context.reconcileGeneration('s1', [{ id: 'new-a', role: 'assistant', status: 'streaming' }], '')
  await flush()
  assert.equal(h.requests.some((request) => request.path === '/messages/new-a/cancel'), true)
  assert.equal(h.tracker.messages.has('s1'), false)
  release()
  await send
})

test('concurrent generation rejection has an actionable Chinese message and only clears that session', async () => {
  const h = harness({ fetchResponse: { ok: false, status: 429, text: async () => '{"detail":{"code":"too_many_generations"}}' } })
  h.tracker.track('s2', 'a2')
  await h.context.sendText('新的消息')
  assert.equal(h.notices.at(-1), '同时进行的回复太多，请稍后')
  assert.equal(h.tracker.messages.has('s1'), false)
  assert.equal(h.tracker.messages.get('s2'), 'a2')
  assert.equal(generationError(new Error('{"detail":"服务繁忙"}')), '服务繁忙')
})

test('deleting the selected generating side chat removes its row and returns to main without a cancel request', async () => {
  const h = harness()
  h.tracker.track('s1', 'a1')
  const subscription = { sessionId: 's1', controller: new AbortController(), cancelled: false }
  h.context.activeGenerationRef.current = subscription
  await h.context.deleteSession(h.current.sessions.find((session) => session.id === 's1'))
  assert.equal(h.requests[0].path, '/sessions/s1')
  assert.equal(h.requests[0].method, 'DELETE')
  assert.equal(h.current.sessions.some((session) => session.id === 's1'), false)
  assert.equal(h.current.sessionId, 'main')
  assert.equal(h.tracker.messages.has('s1'), false)
  assert.equal(subscription.controller.signal.aborted, true)
  assert.equal(h.requests.some((request) => request.path?.endsWith('/cancel')), false)
})

test('renaming a side chat patches its title and updates the list immediately', async () => {
  const h = harness()
  await h.context.renameSession(h.current.sessions.find((session) => session.id === 's1'), '  新名称  ')
  assert.equal(h.requests[0].method, 'PATCH')
  assert.equal(JSON.parse(h.requests[0].body).title, '新名称')
  assert.equal(h.current.sessions.find((session) => session.id === 's1').title, '新名称')
})

test('failed side chat deletion and rename preserve rows and show the error', async () => {
  const h = harness({ failRequest: true })
  const selected = h.current.sessions.find((session) => session.id === 's1')
  await h.context.deleteSession(selected)
  await h.context.renameSession(selected, '新名称')
  assert.equal(h.current.sessions.length, 3)
  assert.equal(h.current.sessions.find((session) => session.id === 's1').title, '工作')
  assert.deepEqual(h.notices, ['操作失败', '操作失败'])
})

test('the five-second background poll completes only hidden sessions and never replaces the selected timeline', async () => {
  const h = harness()
  h.tracker.track('s1', 'a1')
  h.tracker.track('s2', 'a2')
  h.tracker.track('expired', 'a-expired', Date.now() - BACKGROUND_GENERATION_LIMIT_MS)
  h.context.activeSessionIdRef.current = 's2'
  h.context.setData((data) => ({ ...data, sessionId: 's2', messages: [terminal('selected-message')] }))
  let poll, cleanup
  const requestedSessions = []
  h.context.useEffect = (effect) => { cleanup = effect() }
  h.context.window.setInterval = (callback, delay) => { assert.equal(delay, 5000); poll = callback; return 1 }
  h.context.window.clearInterval = () => {}
  h.context.requestMessagesPage = async (id) => { requestedSessions.push(id); return { messages: [terminal('a1')] } }
  const start = source.indexOf('  useEffect(() => {\n    let active = true\n    let polling')
  const end = source.indexOf('\n  const pendingTasks =', start)
  vm.runInContext(source.slice(start, end), h.context)
  await poll()
  assert.deepEqual(requestedSessions, ['s1'])
  assert.equal(h.current.messages[0].id, 'selected-message')
  assert.equal(h.tracker.completed.has('s1'), true)
  assert.equal(h.tracker.messages.get('s2'), 'a2')
  cleanup()
})

test('a workspace poll started before sending cannot replace the newly finished reply with its stale page', async () => {
  const h = harness()
  let releasePage, cleanup
  const pageGate = new Promise((resolve) => { releasePage = resolve })
  h.context.useEffect = (effect) => { cleanup = effect() }
  h.context.request = async (path) => {
    if (path === '/sessions/main') return { id: 'main', kind: 'main' }
    if (path === '/sessions?limit=50') return h.current.sessions
    return []
  }
  h.context.requestMessagesPage = async () => { await pageGate; return { messages: [terminal('stale-a')], hasMore: false } }
  h.context.notificationsRef = { current: [] }
  h.context.createEmptyState = () => ({ sessions: [], messages: [], activity: [] })
  h.context.runtimeActivityToUi = (value) => value
  h.context.setOffline = () => {}
  h.context.setLoadError = () => {}
  h.context.window.setInterval = () => 1
  h.context.window.clearInterval = () => {}
  h.context.document = { addEventListener: () => {}, removeEventListener: () => {} }
  const start = source.indexOf('  useEffect(() => {\n    let active = true\n    let lastRemoteSignature')
  const end = source.indexOf('\n  async function loadProviderStatus(', start)
  vm.runInContext(source.slice(start, end), h.context)
  await flush()
  await h.context.sendText('新消息')
  const finishedTimeline = JSON.stringify(h.current.messages)
  releasePage()
  await flush()
  assert.equal(JSON.stringify(h.current.messages), finishedTimeline)
  assert.equal(h.current.messages.some((message) => message.id === 'a-s1'), true)
  assert.equal(h.current.messages.some((message) => message.id === 'stale-a'), false)
  cleanup()
})

test('an asynchronous widget continuation starts in its original hidden session without replacing the selected subscription', async () => {
  const h = harness()
  h.context.activeSessionIdRef.current = 's2'
  h.context.setData((data) => ({ ...data, sessionId: 's2', messages: [terminal('selected-a')] }))
  h.tracker.track('s2', 'selected-a')
  const selectedSubscription = { sessionId: 's2', controller: new AbortController() }
  h.context.activeGenerationRef.current = selectedSubscription
  await h.context.sendText('已确认执行', { widget_event: { widget_id: 'w1', action: 'confirm' } }, 's1')
  assert.equal(h.requests[0].url.endsWith('/sessions/s1/messages/stream'), true)
  assert.equal(h.requests[0].signal.aborted, true)
  assert.equal(h.tracker.messages.get('s1'), 'a-s1')
  assert.equal(h.tracker.messages.get('s2'), 'selected-a')
  assert.equal(h.context.activeGenerationRef.current, selectedSubscription)
  assert.equal(selectedSubscription.controller.signal.aborted, false)
  assert.equal(h.current.messages[0].id, 'selected-a')
})
