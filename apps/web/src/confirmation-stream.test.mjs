import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { disconnectGeneration, generationError } from './session-generations.js'
import { containsSecretJson, optimisticChatContent } from './mcp-chat.js'

const source = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')

for (const action of ['confirm', 'cancel']) {
  test(`${action} continuation updates the original assistant through start, delta and done without adding rows`, async () => {
    const originalUser = { id: 'u1', role: 'user', content: '删除这条记录', created_at: '2026-10-05T08:00:00Z' }
    const originalAssistant = {
      id: 'a1', role: 'assistant', content: '请确认这次操作', created_at: '2026-10-05T08:00:01Z', status: 'complete',
      metadata: { widgets: [{ id: 'w1', type: 'confirm', state: { status: action === 'confirm' ? 'done' : 'cancelled' } }] },
      toolEvents: [{ tool: 'records.delete', call_id: 'prior-call', status: 'needs_confirmation' }],
    }
    const otherAssistant = { id: 'a2', role: 'assistant', content: '另一条回复', status: 'complete' }
    let current = { sessionId: 's1', sessions: [{ id: 's1', kind: 'main', message_count: 3 }], messages: [originalUser, originalAssistant, otherAssistant], activity: [] }
    const snapshots = [], requests = [], notices = []
    const content = action === 'confirm' ? '已确认执行删除' : '已取消删除'
    const reply = action === 'confirm' ? '已删除记录。' : '已取消操作。'
    const context = {
      data: current, isSessionBusy: () => false, trackGeneration: () => ({}), finishGeneration: () => {}, disconnectGeneration, generationError,
      workspaceRevisionRef: { current: 0 }, activeGenerationRef: { current: null }, activeSessionIdRef: { current: 's1' },
      offline: false, AbortController, API_URL: 'https://luma.example/api/v1',
      containsSecretJson, optimisticChatContent, plainTextPreview: (value) => String(value || ''),
      setData: (update) => { current = update(current); snapshots.push(JSON.parse(JSON.stringify(current))) },
      setSentMessageVersion: () => {}, setStreamSubscriptionVersion: () => {}, notify: (value) => notices.push(value), notifyDesktop: () => {}, authHeaders: () => ({}),
      streamAssistantId: () => 'assistant-local',
      mergeStreamEvent: (events, event, payload) => [...events, { ...payload, event_type: event }],
      fetch: async (url, options) => { requests.push({ url, ...options }); return { ok: true } },
      consumeSSE: async (_response, apply) => {
        apply('start', { message_id: 'a1', session_id: 's1', user_message: originalUser })
        const streaming = current.messages.find((message) => message.id === 'a1')
        assert.equal(current.messages.length, 3)
        assert.equal(streaming.created_at, originalAssistant.created_at)
        assert.equal(streaming.metadata.widgets[0].id, 'w1')
        assert.equal(streaming.content, '')
        assert.equal(streaming.streaming, true)
        apply('status', { message_id: 'a1', status: 'streaming' })
        apply('delta', { content: reply.slice(0, 2) })
        assert.equal(current.messages.find((message) => message.id === 'a1').content, reply.slice(0, 2))
        apply('tool', { tool: 'records.delete', call_id: 'current-call', status: action === 'confirm' ? 'ok' : 'cancelled' }, '2')
        assert.equal(current.messages.find((message) => message.id === 'a1').toolEvents.length, 2)
        apply('delta', { content: reply.slice(2) })
        apply('done', { id: 'a1', role: 'assistant', content: reply, created_at: originalAssistant.created_at, status: 'complete', metadata: originalAssistant.metadata })
      },
      window: { dispatchEvent: () => {} },
    }
    vm.createContext(context)
    vm.runInContext(source.slice(source.indexOf('  async function sendText('), source.indexOf('  const resumableMessageId')), context)
    await context.sendText(content, { widget_event: { widget_id: 'w1', action } })
    assert.equal(requests.length, 1)
    assert.equal(JSON.parse(requests[0].body).metadata.widget_event.action, action)
    assert.equal(notices.length, 0)
    assert.deepEqual(current.messages.map((message) => message.id), ['u1', 'a1', 'a2'])
    assert.deepEqual(current.messages[0], originalUser)
    assert.deepEqual(current.messages[2], otherAssistant)
    assert.equal(current.messages[1].content, reply)
    assert.equal(current.messages[1].streaming, false)
    assert.equal(current.sessions[0].message_count, 3)
    for (const snapshot of snapshots) {
      assert.equal(snapshot.messages.length, 3)
      assert.equal(snapshot.messages.filter((message) => message.id === 'a1').length, 1)
      assert.equal(snapshot.messages.some((message) => message.content === content), false)
    }
  })
}
