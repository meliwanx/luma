import test from 'node:test'
import assert from 'node:assert/strict'
import { encodeWav, isRecordingSupported, MAX_RECORDING_SECONDS, resampleAudioBuffer, startRecording } from './voice.js'

function mockAudio(t, options = {}) {
  const originals = new Map(['navigator', 'window', 'setTimeout', 'clearTimeout'].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const tracks = []
  const contexts = []
  const timers = []
  const stream = { getTracks: () => tracks }
  tracks.push({ stops: 0, stop() { this.stops += 1 } })
  class AudioContext {
    constructor() {
      this.sampleRate = options.sampleRate || 48000
      this.state = options.suspended ? 'suspended' : 'running'
      this.destination = {}
      this.closeCount = 0
      this.source = { connect() {}, disconnect() { this.disconnected = true } }
      this.processor = { connect() {}, disconnect() { this.disconnected = true }, onaudioprocess: null }
      contexts.push(this)
    }
    resume() {
      if (options.resumeError) return Promise.reject(options.resumeError)
      this.state = 'running'
      return Promise.resolve()
    }
    createMediaStreamSource() {
      if (options.sourceError) throw options.sourceError
      return this.source
    }
    createScriptProcessor(size, inputChannels, outputChannels) {
      assert.equal(size, 4096)
      assert.equal(inputChannels, 1)
      assert.equal(outputChannels, 1)
      return this.processor
    }
    close() {
      this.closeCount += 1
      return options.close ? options.close() : Promise.resolve()
    }
  }
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: { getUserMedia: options.getUserMedia || (async (constraints) => {
    assert.deepEqual(constraints.audio, { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true })
    return stream
  }) } } })
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { AudioContext } })
  Object.defineProperty(globalThis, 'setTimeout', { configurable: true, value: (callback, delay) => {
    const timer = { callback, delay, cancelled: false }
    timers.push(timer)
    return timer
  } })
  Object.defineProperty(globalThis, 'clearTimeout', { configurable: true, value: (timer) => {
    if (timer) timer.cancelled = true
  } })
  t.after(() => {
    for (const [key, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  })
  return {
    stream, tracks, contexts, timers,
    emit(values) {
      const samples = values instanceof Float32Array ? values : new Float32Array(values)
      contexts[contexts.length - 1].processor.onaudioprocess?.({ inputBuffer: { getChannelData: () => samples } })
    },
  }
}

test('WAV contains mono PCM16 headers and clips both positive and negative samples', async () => {
  const blob = encodeWav(new Float32Array([-2, -1, -0.5, 0, 0.5, 1, 2]))
  assert.equal(blob.type, 'audio/wav')
  const buffer = await blob.arrayBuffer()
  const view = new DataView(buffer)
  assert.equal(new TextDecoder().decode(buffer.slice(0, 4)), 'RIFF')
  assert.equal(new TextDecoder().decode(buffer.slice(8, 12)), 'WAVE')
  assert.equal(new TextDecoder().decode(buffer.slice(36, 40)), 'data')
  assert.equal(view.getUint16(20, true), 1)
  assert.equal(view.getUint16(22, true), 1)
  assert.equal(view.getUint32(24, true), 16000)
  assert.equal(view.getUint32(28, true), 32000)
  assert.equal(view.getUint16(34, true), 16)
  assert.equal(view.getUint32(40, true), 14)
  assert.deepEqual(Array.from({ length: 7 }, (_, index) => view.getInt16(44 + index * 2, true)), [-32768, -32768, -16384, 0, 16383, 32767, 32767])
})

test('48 kHz samples are averaged into 16 kHz without changing equal-rate data', () => {
  assert.deepEqual([...resampleAudioBuffer(new Float32Array([0, 0.3, 0.6, -0.3, -0.6, -0.9]), 48000)].map((value) => Math.round(value * 10)), [3, -6])
  const samples = new Float32Array([0, 0.5])
  assert.equal(resampleAudioBuffer(samples, 16000), samples)
})

test('unsupported contexts are reported before requesting microphone access', async (t) => {
  mockAudio(t)
  delete globalThis.navigator.mediaDevices
  assert.equal(isRecordingSupported(), false)
  await assert.rejects(startRecording(), /当前环境不支持录音/)
})

test('stop emits a 16 kHz WAV, releases resources and returns the same promise twice', async (t) => {
  let releaseClose
  const env = mockAudio(t, { close: () => new Promise((resolve) => { releaseClose = resolve }) })
  const recorder = await startRecording()
  t.after(() => recorder.cancel())
  env.emit([0.3, 0.6, 0.9, -0.3, -0.6, -0.9])
  let automaticStops = 0
  recorder.onStop(() => { automaticStops += 1 })
  const first = recorder.stop()
  assert.equal(recorder.stop(), first)
  assert.equal(env.tracks[0].stops, 1)
  assert.equal(env.contexts[0].closeCount, 1)
  assert.equal(env.contexts[0].source.disconnected, true)
  assert.equal(env.contexts[0].processor.disconnected, true)
  assert.equal(env.contexts[0].processor.onaudioprocess, null)
  assert.equal(env.timers[0].cancelled, true)
  releaseClose()
  const buffer = await (await first).arrayBuffer()
  assert.equal(new DataView(buffer).getUint32(24, true), 16000)
  assert.equal(buffer.byteLength, 48)
  assert.equal(automaticStops, 0)
})

test('only one microphone permission request or recording can be active at a time', async (t) => {
  let allow
  const env = mockAudio(t, { getUserMedia: () => new Promise((resolve) => { allow = resolve }) })
  const first = startRecording()
  await assert.rejects(startRecording(), /已有录音正在进行/)
  allow(env.stream)
  const recorder = await first
  t.after(() => recorder.cancel())
  await assert.rejects(startRecording(), /已有录音正在进行/)
  recorder.cancel()
  const next = startRecording()
  allow(env.stream)
  const nextRecorder = await next
  nextRecorder.cancel()
})

test('permission denial has a Chinese browser and system settings instruction', async (t) => {
  const options = { getUserMedia: async () => { throw Object.assign(new Error('private native error'), { name: 'NotAllowedError' }) } }
  const env = mockAudio(t, options)
  await assert.rejects(startRecording(), (error) => {
    assert.match(error.message, /需要麦克风权限才能语音输入/)
    assert.match(error.message, /网站设置.*系统隐私设置/)
    assert.doesNotMatch(error.message, /private native error/)
    return true
  })
  globalThis.navigator.mediaDevices.getUserMedia = async () => env.stream
  const recorder = await startRecording()
  recorder.cancel()
})

test('AudioContext initialization failure stops acquired tracks and allows another recording', async (t) => {
  const options = { suspended: true, resumeError: new Error('resume failed') }
  const env = mockAudio(t, options)
  await assert.rejects(startRecording(), /无法启动麦克风/)
  assert.equal(env.tracks[0].stops, 1)
  assert.equal(env.contexts[0].closeCount, 1)
  options.resumeError = null
  const recorder = await startRecording()
  recorder.cancel()
})

test('level subscriptions follow audio energy and a failing view callback does not interrupt cleanup', async (t) => {
  const env = mockAudio(t)
  const recorder = await startRecording()
  t.after(() => recorder.cancel())
  const levels = []
  recorder.onLevel(() => { throw new Error('view failed') })
  const unsubscribe = recorder.onLevel((level) => levels.push(level))
  env.emit([0.125, -0.125])
  assert.deepEqual(levels, [0.5])
  unsubscribe()
  env.emit([1])
  await recorder.stop()
  assert.deepEqual(levels, [0.5])
  assert.equal(env.tracks[0].stops, 1)
})

test('cancel discards audio, stops tracks, closes context and prevents uploading', async (t) => {
  const env = mockAudio(t)
  const recorder = await startRecording()
  env.emit([1, 1, 1])
  let uploads = 0
  recorder.onStop(() => { uploads += 1 })
  recorder.cancel()
  recorder.cancel()
  assert.equal(env.tracks[0].stops, 1)
  assert.equal(env.contexts[0].closeCount, 1)
  assert.equal(env.timers[0].cancelled, true)
  await assert.rejects(recorder.stop(), /录音已取消/)
  assert.equal(uploads, 0)
})

test('120-second automatic stop notifies once and audio never exceeds the limit', async (t) => {
  const env = mockAudio(t, { sampleRate: 16000 })
  const recorder = await startRecording()
  t.after(() => recorder.cancel())
  assert.equal(env.timers[0].delay, MAX_RECORDING_SECONDS * 1000)
  env.emit(new Float32Array(16000 * (MAX_RECORDING_SECONDS + 1)))
  let notifications = 0
  const automaticStop = new Promise((resolve) => recorder.onStop((blob) => { notifications += 1; resolve(blob) }))
  env.timers[0].callback()
  const blob = await automaticStop
  assert.equal(blob.size, 44 + 16000 * MAX_RECORDING_SECONDS * 2)
  assert.equal(await recorder.stop(), blob)
  assert.equal(notifications, 1)
  assert.equal(env.tracks[0].stops, 1)
  assert.equal(env.contexts[0].closeCount, 1)
})

test('automatic encoding failures notify onError after microphone cleanup', async (t) => {
  const env = mockAudio(t)
  const recorder = await startRecording()
  t.after(() => recorder.cancel())
  const OriginalBlob = globalThis.Blob
  t.after(() => { globalThis.Blob = OriginalBlob })
  globalThis.Blob = class { constructor() { throw new Error('encode failed') } }
  const failure = new Promise((resolve) => recorder.onError(resolve))
  env.timers[0].callback()
  assert.match((await failure).message, /录音处理失败，请重新录制/)
  assert.equal(env.tracks[0].stops, 1)
  assert.equal(env.contexts[0].closeCount, 1)
})

test('context close rejection does not prevent a successful stop', async (t) => {
  const env = mockAudio(t, { close: () => Promise.reject(new Error('already closed')) })
  const recorder = await startRecording()
  env.emit([0, 0, 0])
  const blob = await recorder.stop()
  assert.equal(blob.size, 46)
  assert.equal(env.tracks[0].stops, 1)
})
