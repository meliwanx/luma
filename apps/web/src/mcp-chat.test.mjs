import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { disconnectGeneration, generationError } from './session-generations.js'
import { containsSecretJson, displaySecretReferences, optimisticChatContent, toolStatusKey } from './mcp-chat.js'

const token = 'intake-test-secret'
const config = JSON.stringify({ mcpServers: { 'my-data': { type: 'http', url: 'https://example.com/mcp', headers: { Authorization: `Bearer ${token}`, 'X-Key': token } } } })
const reference = '{{secret:sec_0123456789abcdef}}'
const redacted = config.replace(`Bearer ${token}`, reference).replace(token, reference)
const toolEvent = { call_id: 'call-1', tool: 'luma.connectors.add_mcp', title: '连接 MCP', status: 'ok' }
const source = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')

test('whole JSON, fenced JSON and mixed text are hidden in optimistic messages and previews', () => {
  for (const content of [config, `\`\`\`json\n${config}\n\`\`\``, `帮我连接：${config}\n谢谢`]) {
    assert.equal(containsSecretJson(content), true)
    assert.equal(optimisticChatContent(content).includes(token), false)
  }
})

test('single server and malformed secret-bearing JSON are hidden', () => {
  for (const content of [
    JSON.stringify({ type: 'streamable-http', url: 'https://example.com/mcp', headers: { Authorization: token } }),
    `{"mcpServers":{"bad":{"url":"http://127.0.0.1/mcp","headers":{"Authorization":"${token}`,
    `{"url":"https://example.com/mcp","type":"sse","headers":{"Authorization":"${token}`,
  ]) assert.equal(optimisticChatContent(content).includes(token), false)
})

test('escaped JSON property names cannot leave credentials in the optimistic row', () => {
  const escaped = config.replace('mcpServers', 'mcp\\u0053ervers').replace('headers', 'head\\u0065rs')
  assert.equal(containsSecretJson(escaped), true)
  assert.equal(optimisticChatContent(escaped).includes(token), false)
})

test('ordinary JSON and prose remain unchanged', () => {
  for (const content of ['{"name":"Alice","count":2}', '{"mcpServers":{"public":{"url":"https://example.com/mcp"}}}', '什么是 mcpServers？', '解释 {"type":"chart","values":[1,2]}']) {
    assert.equal(containsSecretJson(content), false)
    assert.equal(optimisticChatContent(content), content)
  }
})

test('secret references are masked for display without changing JSON structure or prose', () => {
  const masked = displaySecretReferences(`帮我配置 ${redacted} 谢谢`)
  assert.equal(masked.includes(reference), false)
  assert.equal(masked.includes('https://example.com/mcp'), true)
  assert.equal(masked.includes('my-data'), true)
  assert.equal(masked.match(/••••••（已加密保存）/g).length, 2)
  assert.equal(displaySecretReferences('普通 JSON {"answer":42}'), '普通 JSON {"answer":42}')
  assert.equal(redacted.includes(reference), true)
})

test('session previews mask references before formatting and clipping', () => {
  const context = { displaySecretReferences }
  vm.createContext(context)
  const previewFunction = source.slice(source.indexOf('function plainTextPreview('), source.indexOf('function formatTime('))
  vm.runInContext(previewFunction, context)
  assert.equal(context.plainTextPreview(`令牌 \`${reference}\``), '令牌 ••••••（已加密保存）')
  assert.equal(context.plainTextPreview(reference, 6), '••••••')
})

test('all secret JSON keys are protected even outside an MCP configuration', () => {
  for (const key of ['headers', 'env', 'Authorization', 'token', 'apiKey', 'api_key', 'PASSWORD', 'secret', 'access_token', 'accessKey', 'client-secret', 'API-KEY']) {
    const message = `保存 {"${key}":"${token}"} 然后继续`
    assert.equal(containsSecretJson(message), true)
    assert.equal(optimisticChatContent(message).includes(token), false)
  }
})

function sendHarness({ fail = false, offline = false } = {}) {
  let current = { sessionId: 's1', sessions: [{ id: 's1', kind: 'main', title: '主聊天', message_count: 0 }], messages: [], activity: [] }
  const notices = [], requests = [], snapshots = []
  const context = {
    data: current,
    isSessionBusy: () => false, trackGeneration: () => ({}), finishGeneration: () => {}, disconnectGeneration, generationError,
    workspaceRevisionRef: { current: 0 }, activeGenerationRef: { current: null }, activeSessionIdRef: { current: 's1' },
    offline, AbortController, API_URL: 'https://luma.example/api/v1',
    containsSecretJson, displaySecretReferences, optimisticChatContent, toolStatusKey,
    plainTextPreview: (value) => String(value || '').slice(0, 80),
    setData: (update) => { current = update(current); snapshots.push(JSON.stringify(current)) },
    setSentMessageVersion: () => {}, setStreamSubscriptionVersion: () => {}, notify: (value) => notices.push(value), notifyDesktop: () => {},
    authHeaders: () => ({}), streamAssistantId: () => 'assistant-local',
    mergeStreamEvent: (events, event, payload) => [...events, { ...payload, event_type: event }],
    fetch: async (url, options) => {
      requests.push({ url, ...options })
      assert.equal(JSON.stringify(current).includes(token), false)
      return { ok: !fail, status: fail ? 400 : 200, text: async () => `transport error ${token}` }
    },
    consumeSSE: async (_response, apply) => {
      apply('start', { message_id: 'a1', user_message: { id: 'u1', role: 'user', content: redacted } })
      assert.equal(current.messages.find((message) => message.role === 'user').content, redacted)
      assert.equal(current.sessions[0].last_message_preview.includes('等待服务端'), false)
      apply('tool', toolEvent, '7')
      apply('done', { id: 'a1', content: '已连接。', status: 'complete', metadata: { tool_events: [toolEvent] } })
    },
    window: { dispatchEvent: () => {} },
  }
  vm.createContext(context)
  const functionSource = source.slice(source.indexOf('  async function sendText('), source.indexOf('  const resumableMessageId'))
  vm.runInContext(functionSource, context)
  return { send: (content) => context.sendText(content), get current() { return current }, notices, requests, snapshots }
}

test('stream start replaces the safe optimistic content and preview with the server message', async () => {
  const h = sendHarness()
  await h.send(config)
  assert.equal(JSON.parse(h.requests[0].body).content, config)
  assert.equal(h.current.messages.find((message) => message.role === 'user').content, redacted)
  assert.equal(h.current.messages.find((message) => message.role === 'assistant').metadata.tool_events[0].tool, 'luma.connectors.add_mcp')
  assert.equal(h.snapshots.some((snapshot) => snapshot.includes(token)), false)
})

test('a request failing before start retains only safe content and a safe error summary', async () => {
  const h = sendHarness({ fail: true })
  await h.send(config)
  assert.equal(h.current.messages.length, 1)
  assert.equal(JSON.stringify(h.current).includes(token), false)
  assert.equal(JSON.stringify(h.notices).includes(token), false)
  assert.equal(h.notices[0], '敏感信息未确认保存，请重新粘贴后重试')
})

test('offline sends discard the safe optimistic row and do not send a request', async () => {
  const h = sendHarness({ offline: true })
  await h.send(config)
  assert.equal(h.requests.length, 0)
  assert.equal(h.current.messages.length, 0)
  assert.equal(h.snapshots.some((snapshot) => snapshot.includes(token)), false)
})

test('sending secret content clears the draft and replaces the textarea to discard native undo history', async () => {
  const changes = [], sent = []
  const context = {
    input: config, isSessionBusy: () => false, trackGeneration: () => ({}), finishGeneration: () => {}, disconnectGeneration, generationError, files: [],
    containsSecretJson, setComposerResetKey: (update) => changes.push(['reset', update(0)]),
    setInput: (value) => changes.push(['input', value]), setFiles: () => {}, sendText: (content) => sent.push(content),
  }
  vm.createContext(context)
  const functionSource = source.slice(source.indexOf('  function sendMessage('), source.indexOf('  async function onWidgetEvent('))
  vm.runInContext(functionSource, context)
  context.sendMessage()
  assert.deepEqual(changes, [['reset', 1], ['input', '']])
  assert.deepEqual(sent, [config])
  const composer = await readFile(new URL('./composer.jsx', import.meta.url), 'utf8')
  assert.match(composer, /<textarea key=\{resetKey\}/)
  assert.doesNotMatch(source, /setMcpPrompt|addMcpConfigFromChat/)
})
