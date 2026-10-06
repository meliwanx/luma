import assert from 'node:assert/strict'
import test from 'node:test'
import { createBottomFollower, nearBottom } from './chat-scroll.js'

function harness() {
  let nextId = 0
  const frames = new Map(), timers = new Map()
  const element = { scrollHeight: 1000, scrollTop: 500, clientHeight: 500 }
  const follower = createBottomFollower(() => element, {
    requestFrame: (callback) => { const id = ++nextId; frames.set(id, callback); return id },
    cancelFrame: (id) => frames.delete(id),
    setDelay: (callback) => { const id = ++nextId; timers.set(id, callback); return id },
    clearDelay: (id) => timers.delete(id),
  })
  function flushFrames() { const pending = [...frames.values()]; frames.clear(); pending.forEach((callback) => callback()) }
  function settle() { const pending = [...timers.values()]; timers.clear(); pending.forEach((callback) => callback()); flushFrames() }
  return { element, follower, frames, timers, flushFrames, settle }
}

test('saved bottom position stays pinned when a large streamed delta exceeds the near-bottom threshold', () => {
  const h = harness()
  h.follower.updatePosition()
  h.element.scrollHeight += 500
  assert.equal(nearBottom(h.element), false)
  h.follower.schedule()
  h.flushFrames()
  assert.equal(h.element.scrollTop, h.element.scrollHeight)
})

test('keyboard and composer resizing align on the next frame and after the layout animation', () => {
  const h = harness()
  h.follower.updatePosition()
  h.element.clientHeight = 250
  h.follower.schedule()
  h.flushFrames()
  assert.equal(h.element.scrollTop, 1000)
  h.element.scrollTop = 740
  h.element.clientHeight = 400
  h.element.scrollHeight = 1200
  h.settle()
  assert.equal(h.element.scrollTop, 1200)
})

test('reading history stays in place through streamed updates and viewport resizing', () => {
  const h = harness()
  h.element.scrollTop = 80
  h.follower.updatePosition()
  h.element.scrollHeight = 2000
  h.element.clientHeight = 250
  h.follower.schedule()
  h.flushFrames()
  h.settle()
  assert.equal(h.element.scrollTop, 80)
})

test('sending forces the newest message into view even when the reader was far from the bottom', () => {
  const h = harness()
  h.element.scrollTop = 80
  h.follower.updatePosition()
  h.follower.schedule(true)
  assert.equal(h.element.scrollTop, 80)
  h.flushFrames()
  assert.equal(h.element.scrollTop, 1000)
  h.element.scrollHeight = 1300
  h.follower.schedule()
  h.flushFrames()
  assert.equal(h.element.scrollTop, 1300)
})

test('pending layout work stops when the chat view is unmounted', () => {
  const h = harness()
  h.follower.schedule(true)
  assert.equal(h.frames.size, 1)
  assert.equal(h.timers.size, 1)
  h.follower.dispose()
  h.follower.schedule(true)
  assert.equal(h.frames.size, 0)
  assert.equal(h.timers.size, 0)
})
