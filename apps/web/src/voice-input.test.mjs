import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import { createVoiceInput, insertVoiceText } from './voice-input.js'

const audio = new Blob(['recording'], { type: 'audio/wav' })
const tick = () => new Promise((resolve) => setImmediate(resolve))
const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function fakeRecorder() {
  const listeners = { level: new Set(), stop: new Set(), error: new Set() }
  const subscribe = (type, callback) => { listeners[type].add(callback); return () => listeners[type].delete(callback) }
  const recorder = {
    stops: 0, cancellations: 0,
    async stop() { recorder.stops += 1; return audio },
    cancel() { recorder.cancellations += 1 },
    onLevel: (callback) => subscribe('level', callback),
    onStop: (callback) => subscribe('stop', callback),
    onError: (callback) => subscribe('error', callback),
    emit: (type, value) => { for (const callback of listeners[type]) callback(value) },
  }
  return recorder
}

function harness(overrides = {}) {
  const recorder = fakeRecorder()
  const calls = [], states = [], texts = [], errors = [], levels = []
  const controller = createVoiceInput({
    record: async () => recorder,
    request: async (path, options) => { calls.push({ path, ...options }); return { text: '识别结果', transcript: '原文', cleaned: true } },
    onState: (state) => states.push(state),
    onLevel: (level) => levels.push(level),
    onText: (text) => texts.push(text),
    onError: (error) => errors.push(error),
    ...overrides,
  })
  return { controller, recorder, calls, states, texts, errors, levels }
}

test('manual finish uploads WAV and current session in smart mode and only returns a draft', async () => {
  const h = harness()
  try {
    await h.controller.start({ sessionId: 'side-chat' })
    await h.controller.finish()
    assert.equal(h.calls.length, 1)
    const call = h.calls[0]
    assert.equal(call.path, '/voice/transcribe')
    assert.equal(call.method, 'POST')
    assert.equal(call.body.get('session_id'), 'side-chat')
    assert.equal(call.body.get('mode'), 'smart')
    assert.equal(call.body.get('audio').name, 'voice.wav')
    assert.equal(call.body.get('audio').type, 'audio/wav')
    assert.equal(call.headers, undefined)
    assert.deepEqual(h.texts, ['识别结果'])
    assert.deepEqual(h.states.map((state) => state.status), ['requesting', 'recording', 'processing', 'processing', 'idle'])
  } finally { h.controller.dispose() }
})

test('raw mode is captured when recording starts and preserves fallback transcript', async () => {
  const h = harness({ request: async (path, options) => {
    assert.equal(options.body.get('mode'), 'raw')
    assert.equal(options.body.has('session_id'), false)
    return { text: ' 原话\n', transcript: ' 原话\n', cleaned: false }
  } })
  try {
    const context = { raw: true }
    await h.controller.start(context)
    context.raw = false
    await h.controller.finish()
    assert.deepEqual(h.texts, [' 原话\n'])
    assert.equal(h.errors.length, 0)
  } finally { h.controller.dispose() }
})

test('automatic stop uploads once even when finish is clicked at the limit', async () => {
  const h = harness()
  try {
    await h.controller.start({ raw: true, sessionId: 'main-chat' })
    h.recorder.emit('stop', audio)
    await h.controller.finish()
    await tick()
    assert.equal(h.calls.length, 1)
    assert.equal(h.calls[0].body.get('mode'), 'raw')
    assert.equal(h.calls[0].body.get('session_id'), 'main-chat')
    assert.equal(h.recorder.stops, 0)
  } finally { h.controller.dispose() }
})

test('cancel drops the audio, removes listeners and allows another recording', async () => {
  const h = harness()
  try {
    await h.controller.start()
    h.recorder.emit('level', 0.5)
    h.controller.cancel()
    h.recorder.emit('level', 1)
    h.recorder.emit('stop', audio)
    assert.equal(h.recorder.cancellations, 1)
    assert.equal(h.calls.length, 0)
    assert.deepEqual(h.levels, [0.5])
    await h.controller.start()
    assert.equal(h.states.at(-1).status, 'recording')
  } finally { h.controller.dispose() }
})

test('cancel during permission prompt releases a late microphone handle', async () => {
  const pending = deferred()
  const h = harness({ record: () => pending.promise })
  try {
    const starting = h.controller.start()
    h.controller.cancel()
    pending.resolve(h.recorder)
    await starting
    assert.equal(h.recorder.cancellations, 1)
    assert.equal(h.states.at(-1).status, 'idle')
    assert.equal(h.calls.length, 0)
  } finally { h.controller.dispose() }
})

test('session disposal aborts recognition and ignores a late response', async () => {
  const pending = deferred()
  let signal
  const h = harness({ request: (path, options) => { signal = options.signal; return pending.promise } })
  await h.controller.start({ sessionId: 'old-chat' })
  const finishing = h.controller.finish()
  await tick()
  h.controller.dispose()
  const stateCount = h.states.length
  assert.equal(signal.aborted, true)
  pending.resolve({ text: '不应进入新会话' })
  await finishing
  assert.deepEqual(h.texts, [])
  assert.deepEqual(h.errors, [])
  assert.equal(h.states.length, stateCount)
})

test('stop failure cancels recorder and returns to idle without uploading', async () => {
  const h = harness()
  h.recorder.stop = async () => { throw new Error('录音处理失败') }
  try {
    await h.controller.start()
    await h.controller.finish()
    assert.equal(h.errors[0].message, '录音处理失败')
    assert.equal(h.calls.length, 0)
    assert.equal(h.recorder.cancellations, 1)
    assert.equal(h.states.at(-1).status, 'idle')
  } finally { h.controller.dispose() }
})

test('automatic stop errors reset UI and release recorder', async () => {
  const h = harness()
  try {
    await h.controller.start()
    h.recorder.emit('error', new Error('录音处理失败，请重新录制'))
    assert.equal(h.errors.length, 1)
    assert.equal(h.recorder.cancellations, 1)
    assert.equal(h.states.at(-1).status, 'idle')
  } finally { h.controller.dispose() }
})

test('permission errors reach the toast callback and a second start can retry', async () => {
  let attempt = 0
  const h = harness({ record: async () => {
    if (attempt++ === 0) throw new Error('需要麦克风权限才能语音输入，请在网站设置中允许麦克风')
    return h.recorder
  } })
  try {
    await h.controller.start()
    assert.match(h.errors[0].message, /需要麦克风权限/)
    assert.equal(h.states.at(-1).status, 'idle')
    await h.controller.start()
    assert.equal(h.states.at(-1).status, 'recording')
  } finally { h.controller.dispose() }
})

test('empty recognition text leaves the draft alone and gives a Chinese error', async () => {
  const h = harness({ request: async () => ({ text: ' \n ' }) })
  try {
    await h.controller.start()
    await h.controller.finish()
    assert.deepEqual(h.texts, [])
    assert.equal(h.errors[0].message, '没听清，请靠近麦克风再说一次')
  } finally { h.controller.dispose() }
})

test('rapid starts share one permission request', async () => {
  const pending = deferred()
  let starts = 0
  const h = harness({ record: () => { starts += 1; return pending.promise } })
  try {
    const first = h.controller.start()
    await h.controller.start()
    assert.equal(starts, 1)
    pending.resolve(h.recorder)
    await first
  } finally { h.controller.dispose() }
})

test('text insertion preserves both sides of the current cursor including emoji', () => {
  assert.deepEqual(insertVoiceText('前🙂后', '语音', 3), { value: '前🙂语音后', caret: 5 })
  assert.deepEqual(insertVoiceText('草稿', '开头', 0), { value: '开头草稿', caret: 2 })
  assert.deepEqual(insertVoiceText('草稿', '结尾', 99), { value: '草稿结尾', caret: 4 })
})

const mainSource = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
const requestStart = mainSource.indexOf('async function requestUrl(')
const requestSource = mainSource.slice(requestStart, mainSource.indexOf('\n/**', requestStart))
const detailStart = mainSource.indexOf('function errorDetail(')
const detailSource = mainSource.slice(detailStart, mainSource.indexOf('\nfunction endpointHost(', detailStart))

test('existing request wrapper lets browser generate multipart boundary and retains auth context', async () => {
  let sent
  const requestUrl = vm.runInNewContext(`${requestSource}\nrequestUrl`, {
    FormData,
    authHeaders: () => ({ 'X-Luma-Client': 'web' }),
    fetch: async (url, options) => { sent = { url, ...options }; return { ok: true, status: 200, json: async () => ({ text: '结果' }) } },
  })
  const body = new FormData()
  body.append('audio', audio, 'voice.wav')
  await requestUrl('/api/v1/voice/transcribe', { method: 'POST', body })
  assert.equal(sent.body, body)
  assert.equal(Object.hasOwn(sent.headers, 'Content-Type'), false)
  assert.equal(sent.headers['X-Luma-Client'], 'web')
  assert.equal(sent.credentials, 'include')
  await requestUrl('/api/v1/settings', { method: 'POST', body: '{}' })
  assert.equal(sent.headers['Content-Type'], 'application/json')
})

test('backend errors display the existing Chinese detail toast for 413, 422, 429 and upstream failures', async () => {
  const errorDetail = vm.runInNewContext(`${detailSource}\nerrorDetail`)
  for (const [status, detail] of [[413, '录音太长，请控制在 2 分钟内'], [422, '没听清，请靠近麦克风再说一次'], [429, '请求太频繁，请稍后再试'], [502, '语音服务暂时不可用'], [503, '语音服务未配置'], [504, '语音识别超时']]) {
    const requestUrl = vm.runInNewContext(`${requestSource}\nrequestUrl`, {
      FormData, authHeaders: () => ({}),
      fetch: async () => ({ ok: false, status, text: async () => JSON.stringify({ detail }) }),
    })
    await assert.rejects(requestUrl('/api/v1/voice/transcribe'), (error) => {
      assert.equal(error.status, status)
      assert.equal(errorDetail(error), detail)
      return true
    })
  }
})
