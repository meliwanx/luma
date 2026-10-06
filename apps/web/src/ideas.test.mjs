import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { createIdeasPoller, IDEAS_POLL_DELAY_MS, IDEAS_POLL_MAX_RETRIES, normalizeIdeas, updateIdeaStatus } from './ideas-data.js'

function clock() {
  let id = 0
  const pending = new Map()
  const delays = []
  return {
    pending, delays,
    schedule(callback, delay) { delays.push(delay); pending.set(++id, callback); return id },
    cancel(handle) { pending.delete(handle) },
    async tick() {
      const next = pending.entries().next().value
      if (!next) return false
      pending.delete(next[0])
      await next[1]()
      return true
    },
  }
}

function harness(load) {
  const timers = clock()
  const responses = [], errors = []
  let requests = 0
  const poller = createIdeasPoller({ load: () => load(++requests), onData: (value) => responses.push(value), onError: (error) => errors.push(error), schedule: timers.schedule, cancel: timers.cancel })
  return { poller, timers, responses, errors, get requests() { return requests } }
}

test('ideas generating=false never schedules a retry', async () => {
  const h = harness(async () => ({ generating: false, featured: [], groups: [] }))
  await h.poller.refresh()
  assert.equal(h.requests, 1)
  assert.equal(h.timers.pending.size, 0)
  assert.equal(h.responses[0].generating, false)
})

test('ideas generating=true fetches after five seconds and stops after six retries', async () => {
  const h = harness(async () => ({ generating: true }))
  await h.poller.refresh()
  for (let retry = 0; retry < IDEAS_POLL_MAX_RETRIES; retry += 1) {
    assert.equal(h.timers.pending.size, 1)
    assert.equal(await h.timers.tick(), true)
  }
  assert.equal(h.requests, 7)
  assert.equal(h.responses.length, 7)
  assert.equal(h.timers.pending.size, 0)
  assert.equal(await h.timers.tick(), false)
  assert.deepEqual(h.timers.delays, Array(6).fill(5000))
  assert.equal(IDEAS_POLL_DELAY_MS, 5000)
})

test('ideas polling stops as soon as generation finishes', async () => {
  const h = harness(async (request) => ({ generating: request < 3 }))
  await h.poller.refresh()
  await h.timers.tick()
  await h.timers.tick()
  assert.equal(h.requests, 3)
  assert.equal(h.responses.at(-1).generating, false)
  assert.equal(h.timers.pending.size, 0)
})

test('unmount cancels the scheduled ideas poll', async () => {
  const h = harness(async () => ({ generating: true }))
  await h.poller.refresh()
  h.poller.stop()
  assert.equal(h.timers.pending.size, 0)
  assert.equal(await h.timers.tick(), false)
  await h.poller.refresh()
  assert.equal(h.requests, 1)
})

test('unmount ignores an in-flight response and does not restart polling', async () => {
  let resolve
  const h = harness(() => new Promise((done) => { resolve = done }))
  const pending = h.poller.refresh()
  h.poller.stop()
  resolve({ generating: true })
  await pending
  assert.equal(h.responses.length, 0)
  assert.equal(h.timers.pending.size, 0)
})

test('manual refresh invalidates stale responses and resets the retry budget', async () => {
  const h = harness(async () => ({ generating: true }))
  await h.poller.refresh()
  for (let retry = 0; retry < 6; retry += 1) await h.timers.tick()
  await h.poller.refresh()
  assert.equal(h.timers.pending.size, 1)
  for (let retry = 0; retry < 6; retry += 1) await h.timers.tick()
  assert.equal(h.requests, 14)
  assert.equal(h.timers.pending.size, 0)

  const resolvers = []
  const stale = harness(() => new Promise((resolve) => resolvers.push(resolve)))
  const first = stale.poller.refresh()
  const second = stale.poller.refresh()
  resolvers[1]({ generating: false, featured: [{ id: 'new' }] })
  await second
  resolvers[0]({ generating: true, featured: [{ id: 'old' }] })
  await first
  assert.deepEqual(stale.responses.map((data) => data.featured[0].id), ['new'])
  assert.equal(stale.timers.pending.size, 0)
})

test('failed ideas polling reports the error and schedules no further request', async () => {
  const failure = new Error('服务暂时不可用')
  const h = harness(async (request) => { if (request > 1) throw failure; return { generating: true } })
  await h.poller.refresh()
  await h.timers.tick()
  assert.equal(h.requests, 2)
  assert.deepEqual(h.errors, [failure])
  assert.equal(h.timers.pending.size, 0)
})

test('template ideas remain visible when personalized ideas are absent or dismissed', () => {
  const data = normalizeIdeas({ featured: [{ id: 'personal', status: 'dismissed' }], groups: [{ name: '效率提升', items: [{ id: 'template', is_template: true, status: 'active' }] }] })
  assert.deepEqual(data.featured, [])
  assert.equal(data.groups[0].items[0].id, 'template')
  const started = updateIdeaStatus(data, 'template', 'started')
  assert.equal(started.groups[0].items[0].status, 'started')
  assert.equal(data.groups[0].items[0].status, 'active')
  assert.deepEqual(updateIdeaStatus(data, 'template', 'dismissed').groups[0].items, [])
})

test('idea details use safe Markdown and starting delegates to the normal chat send flow', async () => {
  const source = await readFile(new URL('./ideas.jsx', import.meta.url), 'utf8')
  assert.match(source, /<Markdown text=\{selected\.plan_markdown \|\| ''\}/)
  assert.match(source, /await onStart\(\{ session_id: result\.session_id, prompt: result\.prompt \}\)/)
  assert.match(source, /failure\?\.status === 429 \? '今天换的次数用完了'/)
  assert.doesNotMatch(source, /dangerouslySetInnerHTML|<iframe|\/messages\/stream/)
})
