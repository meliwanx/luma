export const MAX_RECORDING_SECONDS = 120
const OUTPUT_SAMPLE_RATE = 16000
let activeRecording = null

function audioContextConstructor() {
  return typeof window === 'undefined' ? null : (window.AudioContext || window.webkitAudioContext)
}

export function isRecordingSupported() {
  return typeof navigator !== 'undefined' && typeof navigator.mediaDevices?.getUserMedia === 'function'
    && typeof audioContextConstructor() === 'function'
}

function mergeAudioChunks(chunks) {
  const length = chunks.reduce((total, chunk) => total + chunk.length, 0)
  const samples = new Float32Array(length)
  let offset = 0
  for (const chunk of chunks) {
    samples.set(chunk, offset)
    offset += chunk.length
  }
  return samples
}

export function resampleAudioBuffer(samples, inputRate, outputRate = OUTPUT_SAMPLE_RATE) {
  if (inputRate === outputRate) return samples
  const ratio = inputRate / outputRate
  const output = new Float32Array(Math.round(samples.length / ratio))
  for (let index = 0; index < output.length; index += 1) {
    const start = Math.floor(index * ratio)
    const end = Math.min(Math.floor((index + 1) * ratio), samples.length)
    let total = 0
    for (let sourceIndex = start; sourceIndex < end; sourceIndex += 1) total += samples[sourceIndex]
    output[index] = end > start ? total / (end - start) : (samples[start] || 0)
  }
  return output
}

export function encodeWav(samples, sampleRate = OUTPUT_SAMPLE_RATE) {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  const write = (offset, text) => {
    for (let index = 0; index < text.length; index += 1) view.setUint8(offset + index, text.charCodeAt(index))
  }
  write(0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  write(8, 'WAVE')
  write(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  write(36, 'data')
  view.setUint32(40, samples.length * 2, true)
  for (let index = 0; index < samples.length; index += 1) {
    const value = Math.max(-1, Math.min(1, samples[index]))
    view.setInt16(44 + index * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true)
  }
  return new Blob([buffer], { type: 'audio/wav' })
}

function recordingError(error) {
  if (['NotAllowedError', 'PermissionDeniedError', 'SecurityError'].includes(error?.name)) {
    return new Error('需要麦克风权限才能语音输入，请在浏览器地址栏的网站设置中允许麦克风；仍无法使用时，请检查系统隐私设置中的麦克风权限')
  }
  if (['NotFoundError', 'DevicesNotFoundError'].includes(error?.name)) {
    return new Error('未找到可用麦克风，请连接麦克风后重试')
  }
  if (['NotReadableError', 'TrackStartError'].includes(error?.name)) {
    return new Error('麦克风被占用，请关闭其他录音应用后重试')
  }
  return new Error('无法启动麦克风，请检查设备后重试')
}

function callListeners(listeners, value) {
  for (const callback of listeners) {
    try {
      const result = callback(value)
      if (result?.catch) void result.catch(() => undefined)
    } catch {
      // A view callback must not prevent microphone cleanup.
    }
  }
}

/** onStop/onError notify only for the automatic 120-second stop. */
export async function startRecording() {
  if (!isRecordingSupported()) throw new Error('当前环境不支持录音')
  if (activeRecording) throw new Error('已有录音正在进行，请先结束当前录音')

  const owner = Symbol('voice-recording')
  activeRecording = owner
  let stream = null
  let context = null
  let source = null
  let processor = null
  let timer = null
  let cleaned = false
  let cancelled = false
  let stopPromise = null
  let cleanupPromise = null
  let sampleCount = 0
  const chunks = []
  const levelListeners = new Set()
  const stopListeners = new Set()
  const errorListeners = new Set()

  function cleanup() {
    if (cleaned) return cleanupPromise
    cleaned = true
    clearTimeout(timer)
    if (processor) processor.onaudioprocess = null
    for (const node of [processor, source]) {
      try {
        node?.disconnect()
      } catch {
        // Some WebViews throw for nodes that are already disconnected.
      }
    }
    for (const track of stream?.getTracks() || []) {
      try {
        track.stop()
      } catch {
        // Continue releasing the other tracks and the audio context.
      }
    }
    if (activeRecording === owner) activeRecording = null
    callListeners(levelListeners, 0)
    levelListeners.clear()
    try {
      cleanupPromise = context ? Promise.resolve(context.close()).catch(() => undefined) : Promise.resolve()
    } catch {
      cleanupPromise = Promise.resolve()
    }
    return cleanupPromise
  }

  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    })
    const Context = audioContextConstructor()
    context = new Context()
    if (context.state === 'suspended') await context.resume()
    source = context.createMediaStreamSource(stream)
    processor = context.createScriptProcessor(4096, 1, 1)
    const maxSamples = Math.floor(context.sampleRate * MAX_RECORDING_SECONDS)
    processor.onaudioprocess = (event) => {
      if (cleaned) return
      const input = event.inputBuffer.getChannelData(0)
      const length = Math.min(input.length, maxSamples - sampleCount)
      if (length <= 0) return
      const chunk = new Float32Array(input.subarray(0, length))
      chunks.push(chunk)
      sampleCount += length
      let energy = 0
      for (const value of chunk) energy += value * value
      callListeners(levelListeners, Math.min(1, Math.sqrt(energy / length) * 4))
    }
    source.connect(processor)
    processor.connect(context.destination)
  } catch (error) {
    await cleanup()
    throw recordingError(error)
  }

  function stop() {
    if (stopPromise) return stopPromise
    if (cancelled) return Promise.reject(new Error('录音已取消'))
    const sampleRate = context.sampleRate
    const released = cleanup()
    stopPromise = (async () => {
      await released
      const samples = resampleAudioBuffer(mergeAudioChunks(chunks), sampleRate)
      chunks.length = 0
      return encodeWav(samples)
    })()
    return stopPromise
  }

  timer = setTimeout(() => {
    void stop().then((blob) => {
      if (!cancelled) callListeners(stopListeners, blob)
      stopListeners.clear()
      errorListeners.clear()
    }).catch(() => {
      if (!cancelled) callListeners(errorListeners, new Error('录音处理失败，请重新录制'))
      stopListeners.clear()
      errorListeners.clear()
    })
  }, MAX_RECORDING_SECONDS * 1000)

  return {
    stop,
    cancel() {
      cancelled = true
      void cleanup()
      chunks.length = 0
      stopListeners.clear()
      errorListeners.clear()
    },
    onLevel(callback) {
      levelListeners.add(callback)
      return () => levelListeners.delete(callback)
    },
    onStop(callback) {
      stopListeners.add(callback)
      return () => stopListeners.delete(callback)
    },
    onError(callback) {
      errorListeners.add(callback)
      return () => errorListeners.delete(callback)
    },
  }
}
