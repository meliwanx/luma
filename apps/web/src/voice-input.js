import { MAX_RECORDING_SECONDS, startRecording } from './voice.js'

export function insertVoiceText(value, text, position) {
  const index = Math.max(0, Math.min(value.length, position ?? value.length))
  return { value: value.slice(0, index) + text + value.slice(index), caret: index + text.length }
}

// Own the recording and its upload together so leaving a conversation cancels
// both, including a microphone permission request that resolves later.
export function createVoiceInput({ request, onState, onLevel, onText, onError, record = startRecording }) {
  let status = 'idle'
  let version = 0
  let disposed = false
  let recorder = null
  let timer = null
  let abortController = null
  let subscriptions = []
  let recordingContext = {}

  const active = (token) => !disposed && token === version
  const publish = (next, elapsedSeconds = 0) => {
    status = next
    if (!disposed) onState({ status: next, elapsedSeconds })
  }
  const clearRecording = () => {
    clearInterval(timer)
    timer = null
    subscriptions.forEach((unsubscribe) => unsubscribe())
    subscriptions = []
  }
  const fail = (error, token) => {
    if (!active(token)) return
    clearRecording()
    recorder?.cancel()
    recorder = null
    publish('idle')
    onError(error)
  }

  async function transcribe(audio, token, context) {
    if (!active(token)) return
    clearRecording()
    recorder = null
    publish('processing')
    const controller = new AbortController()
    abortController = controller
    try {
      const body = new FormData()
      body.append('audio', audio, 'voice.wav')
      body.append('mode', context.raw ? 'raw' : 'smart')
      if (context.sessionId) body.append('session_id', context.sessionId)
      const result = await request('/voice/transcribe', { method: 'POST', body, signal: controller.signal })
      if (!active(token)) return
      if (typeof result?.text !== 'string' || !result.text.trim()) throw new Error('没听清，请靠近麦克风再说一次')
      onText(result.text)
    } catch (error) {
      if (active(token)) onError(error)
    } finally {
      if (active(token)) {
        abortController = null
        publish('idle')
      }
    }
  }

  async function start(context = {}) {
    if (disposed || status !== 'idle') return
    const token = ++version
    recordingContext = { ...context }
    publish('requesting')
    try {
      const next = await record()
      if (!active(token)) { next.cancel(); return }
      recorder = next
      publish('recording')
      subscriptions.push(next.onLevel((level) => { if (active(token)) onLevel(level) }))
      subscriptions.push(next.onStop((audio) => { void transcribe(audio, token, recordingContext) }))
      subscriptions.push(next.onError((error) => fail(error, token)))
      const startedAt = Date.now()
      let previousSeconds = 0
      timer = setInterval(() => {
        const seconds = Math.max(0, Math.min(MAX_RECORDING_SECONDS, Math.floor((Date.now() - startedAt) / 1000)))
        if (seconds !== previousSeconds) {
          previousSeconds = seconds
          publish('recording', seconds)
        }
      }, 250)
    } catch (error) { fail(error, token) }
  }

  async function finish() {
    if (status !== 'recording' || !recorder) return
    const token = version
    const current = recorder
    clearRecording()
    publish('processing')
    try {
      const audio = await current.stop()
      await transcribe(audio, token, recordingContext)
    } catch (error) { fail(error, token) }
  }

  function cancel() {
    version += 1
    clearRecording()
    recorder?.cancel()
    recorder = null
    abortController?.abort()
    abortController = null
    publish('idle')
  }

  return {
    start,
    finish,
    cancel,
    dispose() { disposed = true; cancel() },
  }
}
