export const BACKGROUND_GENERATION_LIMIT_MS = 10 * 60 * 1000

export function isGeneratingMessage(message) {
  return message?.role === 'assistant' && (['pending', 'streaming'].includes(message.status) || message.streaming === true)
}

export function latestGeneratingMessage(messages = []) {
  const lastAssistant = [...messages].reverse().find((message) => message.role === 'assistant')
  return isGeneratingMessage(lastAssistant) ? lastAssistant : null
}

// These maps outlive the selected conversation and its SSE subscription.
export class SessionGenerations {
  messages = new Map()
  progress = new Map()
  completed = new Set()

  track(sessionId, messageId = '', now = Date.now()) {
    const previousId = this.messages.get(sessionId)
    const previous = this.progress.get(sessionId)
    const sameGeneration = this.messages.has(sessionId) && (!previousId || previousId === messageId)
    this.messages.set(sessionId, messageId || previousId || '')
    if (!sameGeneration || !previous) this.progress.set(sessionId, { startedAt: now, after: '', content: '' })
    this.completed.delete(sessionId)
    return this.progress.get(sessionId)
  }

  finish(sessionId, messageId, { selectedSessionId = '', complete = false } = {}) {
    if (!this.messages.has(sessionId) || this.messages.get(sessionId) !== messageId) return false
    this.messages.delete(sessionId)
    this.progress.delete(sessionId)
    if (complete && selectedSessionId !== sessionId) this.completed.add(sessionId)
    return true
  }

  forget(sessionId) {
    this.messages.delete(sessionId)
    this.progress.delete(sessionId)
    this.completed.delete(sessionId)
  }

  shouldPoll(sessionId, selectedSessionId, now = Date.now()) {
    const progress = this.progress.get(sessionId)
    return this.messages.has(sessionId) && sessionId !== selectedSessionId && progress && now - progress.startedAt < BACKGROUND_GENERATION_LIMIT_MS
  }
}

export function disconnectGeneration(generation) {
  if (!generation) return
  generation.detached = true
  generation.controller?.abort()
}

export function generationError(error, fallback = '无法连接助手') {
  let detail = error?.message || fallback
  try {
    const parsed = JSON.parse(detail)
    if (parsed.detail?.code === 'too_many_generations') return '同时进行的回复太多，请稍后'
    if (typeof parsed.detail === 'string') detail = parsed.detail
  } catch { /* plain-text response */ }
  return detail.includes('too_many_generations') ? '同时进行的回复太多，请稍后' : detail
}
