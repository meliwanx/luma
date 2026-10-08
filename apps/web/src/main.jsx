import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import Icon from './icons.jsx'
import LumaLogo from './logo.jsx'
import { ChatWidget } from './widgets.jsx'
import { ChatsPanel, SearchPalette } from './chat-panels.jsx'
import { SandboxToolCard } from './sandbox-cards.jsx'
import { browserProgressLabel, liveBrowserEvents, preserveBrowserLiveMessages } from './browser-tools.js'
import { PermissionsPanel, SandboxWorkspacePanel } from './sandbox-panels.jsx'
import { UsagePanel } from './usage-panels.jsx'
import { Composer } from './composer.jsx'
import Markdown from './markdown.jsx'
import LibraryPage from './library.jsx'
import IdeasPage from './ideas.jsx'
import FeedPage from './feed.jsx'
import ProactiveSettings from './proactive-settings.jsx'
import { ProactiveLabels } from './proactive-labels.jsx'
import { startIdeaSession } from './content-sessions.js'
import { containsSecretJson, displaySecretReferences, optimisticChatContent, toolStatusKey } from './mcp-chat.js'
import { SessionGenerations, disconnectGeneration, generationError, isGeneratingMessage, latestGeneratingMessage } from './session-generations.js'
import { createBottomFollower } from './chat-scroll.js'
import { exchangeSsoTicket, loadAuthProviders, loginWithPassword, loginWithSsoPassword, registerWithPassword, startSsoLogin } from './password-login.js'
import AccountSettings from './account-settings.jsx'
import { subscribeDesktopCommands } from './desktop-commands.js'
import './styles.css'

const browserDefaultApi = typeof window !== 'undefined' && /^https?:$/.test(window.location.protocol) && !['5173', '4173'].includes(window.location.port)
  ? `${window.location.origin}/api/v1`
  : 'http://localhost:8000/api/v1'
const API_URL = (import.meta.env.VITE_API_URL || browserDefaultApi).replace(/\/$/, '')
const ADMIN_API_URL = `${API_URL.replace(/\/v1$/, '')}/admin`
const AdminApp = React.lazy(() => import('./admin.jsx'))
const ACCESS_TOKEN_STORAGE_KEY = 'luma_access_token'
const SANDBOX_TOOL_KINDS = new Set(['sandbox', 'file', 'sandbox_import', 'preview', 'sandbox_job', 'browser_live'])

function createEmptyState() {
  return {
    sessions: [], sessionId: '', messages: [], tasks: [], runtimeJobs: [],
    approvals: [], goals: [], notifications: [], memories: [], activity: [], files: [],
    routines: [],
  }
}

const ACCENTS = [
  { value: '#1f7aec', label: '蓝' },
  { value: '#5b5bd6', label: '靛' },
  { value: '#8b5cf6', label: '紫' },
  { value: '#e5487f', label: '玫红' },
  { value: '#ea6a2a', label: '橙' },
  { value: '#22a559', label: '绿' },
  { value: '#0f9fa8', label: '青' },
  { value: '#8a6a4f', label: '棕' },
  { value: '#3f4650', label: '石墨' },
]

function accountName(account) {
  return account?.display_name || account?.username || account?.email || '我的账户'
}

function accountInitial(account) {
  return Array.from(accountName(account))[0]?.toUpperCase() || 'L'
}

const navItems = [
  { id: 'chat', label: '聊天', icon: 'chat' },
  { id: 'search', label: '搜索', icon: 'search', action: 'chat' },
  { id: 'activity', label: '动态', icon: 'activity' },
  { id: 'ideas', label: '点子', icon: 'idea' },
  { id: 'missions', label: '目标', icon: 'goal' },
  { id: 'library', label: '资源库', icon: 'library' },
]

// Session previews are rendered in several places (the chat list and search
// palette). Keep the value plain text even when a message contains markdown.
function plainTextPreview(value, limit = 80) {
  const text = displaySecretReferences(value)
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[`*_~>#-]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  return Array.from(text).slice(0, limit).join('')
}

function formatTime(value) {
  if (!value) return ''
  if (/^\d{2}:\d{2}$/.test(value)) return value
  if (value === '现在' || value === '刚刚') return value
  const numeric = typeof value === 'number' || (typeof value === 'string' && /^\d{10,13}$/.test(value))
  const epoch = numeric ? Number(value) : 0
  const date = numeric ? new Date(epoch < 1e12 ? epoch * 1000 : epoch) : new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date)
}

// Optimistic messages carry '现在'/'刚刚' until the server echoes a timestamp;
// bare 'HH:MM' strings have no day, so they never start a separator.
function parseDate(value) {
  if (value === '现在' || value === '刚刚') return new Date()
  if (!value || /^\d{1,2}:\d{2}$/.test(value)) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function dayLabel(date) {
  const startOfDay = (value) => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime()
  const days = Math.round((startOfDay(new Date()) - startOfDay(date)) / 86400000)
  if (days === 0) return '今天'
  if (days === 1) return '昨天'
  return `${date.getMonth() + 1}月${date.getDate()}日`
}

function separatorLabel(date) {
  return `${dayLabel(date)} ${date.getHours()}:${String(date.getMinutes()).padStart(2, '0')}`
}

function runtimeActivityToUi(item) {
  return {
    at: item.created_at,
    title: item.title,
    detail: item.detail,
    time: formatTime(item.created_at),
    text: item.detail ? `${item.title} · ${item.detail}` : item.title,
    tag: String(item.kind || 'RUNTIME').split(/[._]/)[0].toUpperCase(),
  }
}

function statusLabel(status) {
  return ({ todo: '待开始', queued: '待开始', running: '执行中', in_progress: '进行中', waiting_approval: '等你确认', succeeded: '已完成', failed: '失败', done: '已完成', cancelled: '已取消' })[status] || status
}

// Streaming tool events are transient UI state. Keep one row per call/event
// identity so a reconnect or a later status update cannot duplicate rows.
function mergeStreamEvent(events, event, payload = {}, eventId = '') {
  const identity = payload.call_id || payload.approval_id || payload.id || eventId || payload.tool || payload.reason || 'event'
  const key = `${event}:${identity}`
  const next = { ...payload, event_type: event, stream_key: key }
  return [...events.filter((item) => (item.stream_key || `${item.event_type || 'tool'}:${item.call_id || item.approval_id || item.id || item.tool || item.reason || 'event'}`) !== key), next]
}

// Lets the backend tell web, desktop (Electron loads this same bundle) and
// native clients apart in its usage statistics.
const CLIENT_HEADERS = {
  'X-Luma-Client': typeof navigator !== 'undefined' && /Electron\//.test(navigator.userAgent) ? 'desktop' : 'web',
  'X-Luma-Client-Version': __APP_VERSION__,
  ...(typeof navigator !== 'undefined' && (navigator.userAgentData?.platform || navigator.platform) ? { 'X-Luma-Platform': navigator.userAgentData?.platform || navigator.platform } : {}),
}

function authHeaders() {
  const accessToken = sessionStorage.getItem(ACCESS_TOKEN_STORAGE_KEY) || localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)
  return { ...CLIENT_HEADERS, ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}) }
}

async function request(path, options = {}) {
  return requestUrl(`${API_URL}${path}`, options)
}

async function requestBlob(path, options = {}) {
  const response = await fetch(`${API_URL}${path}`, { ...options, headers: authHeaders(), credentials: 'include' })
  if (!response.ok) {
    const error = new Error(`文件加载失败（${response.status}）`)
    error.status = response.status
    if (response.status === 401) window.dispatchEvent(new CustomEvent('luma-auth-required'))
    if (response.status === 503) window.dispatchEvent(new CustomEvent('luma-service-unavailable'))
    throw error
  }
  return response.blob()
}

async function requestMessagesPage(sessionId, { before, limit = 50 } = {}) {
  const params = new URLSearchParams({ limit: String(limit) })
  if (before) params.set('before', before)
  const response = await fetch(`${API_URL}/sessions/${encodeURIComponent(sessionId)}/messages?${params.toString()}`, {
    headers: { Accept: 'application/json', ...authHeaders() },
    credentials: 'include',
  })
  if (!response.ok) {
    const detail = await response.text()
    const error = new Error(detail || `消息加载失败（${response.status}）`)
    error.status = response.status
    if (response.status === 401 && typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('luma-auth-required'))
    if (response.status === 503 && typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('luma-service-unavailable'))
    throw error
  }
  return {
    messages: await response.json(),
    hasMore: response.headers.get('X-Has-More') === 'true',
    oldestId: response.headers.get('X-Oldest-Id') || '',
  }
}

function adminRequest(endpoint, options = {}) {
  if (endpoint === '/me') return request('/auth/me', options)
  return requestUrl(`${ADMIN_API_URL}${endpoint}`, options)
}

async function requestUrl(url, { headers, skipAuthRequired = false, ...options } = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...authHeaders(), ...(headers || {}) },
    credentials: 'include',
  })
  if (!response.ok) {
    const rawDetail = await response.text()
    let detail = rawDetail
    try {
      const payload = JSON.parse(rawDetail)
      if (typeof payload.detail === 'string') detail = payload.detail
      else if (Array.isArray(payload.detail)) detail = payload.detail.map((item) => item.msg || String(item)).join('；')
    } catch { /* plain-text response */ }
    const error = new Error(detail || `请求失败（${response.status}）`)
    error.status = response.status
    if (response.status === 401 && !skipAuthRequired && typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('luma-auth-required'))
    // A cache/Redis outage is not an authentication failure. Let the app
    // surface a recoverable banner while keeping the current session intact.
    if (response.status === 503 && typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('luma-service-unavailable', { detail: { url } }))
    }
    throw error
  }
  if (response.status === 204) return null
  return response.json()
}

/**
 * Consume an SSE response without assuming that network chunks line up with
 * SSE frames. The server can split a UTF-8 character, a line, or the blank
 * line separating events across reads, so TextDecoder's streaming mode and a
 * retained tail are both required here.
 */
async function consumeSSE(response, onEvent, onActivity) {
  if (!response.body) throw new Error('流式响应没有内容')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finished = false

  function consumeFrame(frame) {
    onActivity?.()
    const lines = frame.split(/\r\n|\n|\r/)
    let event = 'message'
    let eventId = ''
    const data = []
    for (const line of lines) {
      if (!line || line.startsWith(':')) continue
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('id:')) eventId = line.slice(3).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
    }
    if (!data.length) return
    const raw = data.join('\n')
    try {
      if (onEvent(event, JSON.parse(raw), eventId) === true) finished = true
    } catch {
      throw new Error('流式响应格式无效')
    }
  }

  function consumeAvailableFrames() {
    // Keep every possible line ending until its complete blank-line
    // separator arrives. This matters when a CRLF is split across reads.
    const separator = /\r\n\r\n|\n\n|\r\r/
    let match = separator.exec(buffer)
    while (match && !finished) {
      consumeFrame(buffer.slice(0, match.index))
      buffer = buffer.slice(match.index + match[0].length)
      match = separator.exec(buffer)
    }
  }

  try {
    while (!finished) {
      const { value, done } = await reader.read()
      if (done) {
        finished = true
        buffer += decoder.decode()
        consumeAvailableFrames()
        if (buffer.trim()) consumeFrame(buffer)
        break
      }
      buffer += decoder.decode(value, { stream: true })
      consumeAvailableFrames()
    }
  } finally {
    reader.releaseLock()
  }
}

const RESUME_IDLE_TIMEOUT_MS = 60 * 1000
const RESUME_MAX_DURATION_MS = 660 * 1000

function abortError() {
  const error = new Error('操作已取消')
  error.name = 'AbortError'
  return error
}

function waitForRetry(delay, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError())
      return
    }
    let timer = window.setTimeout(done, delay)
    function cleanup() {
      window.clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
    }
    function done() {
      cleanup()
      resolve()
    }
    function onAbort() {
      cleanup()
      reject(abortError())
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/**
 * Resume a durable message stream until the callback reports completion.
 * The idle deadline moves forward on every SSE frame, including comment pings;
 * the absolute limit prevents a live stream from running forever.
 */
async function streamResume(messageId, { after = '', onEvent, signal, startedAt = Date.now() } = {}) {
  let lastEventId = after || ''
  let retryDelay = 1000
  let lastActivityAt = Date.now()
  let lastError = null
  let completed = false
  let idleReconnectAttempted = false

  while (!completed) {
    if (signal?.aborted) throw abortError()
    const now = Date.now()
    if (now - startedAt >= RESUME_MAX_DURATION_MS) break

    const controller = new AbortController()
    let idleTimer = null
    let totalTimer = null
    let idleExpired = false
    let totalExpired = false
    const onAbort = () => controller.abort()
    const resetIdleTimer = () => {
      if (idleTimer) window.clearTimeout(idleTimer)
      const remaining = Math.max(1, RESUME_IDLE_TIMEOUT_MS - (Date.now() - lastActivityAt))
      idleTimer = window.setTimeout(() => {
        idleExpired = true
        controller.abort()
      }, remaining)
    }
    const markActivity = () => {
      lastActivityAt = Date.now()
      idleReconnectAttempted = false
      resetIdleTimer()
    }
    signal?.addEventListener('abort', onAbort, { once: true })
    resetIdleTimer()
    totalTimer = window.setTimeout(() => {
      totalExpired = true
      controller.abort()
    }, Math.max(1, RESUME_MAX_DURATION_MS - (Date.now() - startedAt)))

    try {
      const query = lastEventId ? `?after=${encodeURIComponent(lastEventId)}` : ''
      const headers = { Accept: 'text/event-stream', ...authHeaders() }
      if (lastEventId) headers['Last-Event-ID'] = lastEventId
      const response = await fetch(`${API_URL}/messages/${encodeURIComponent(messageId)}/stream${query}`, {
        headers,
        credentials: 'include',
        signal: controller.signal,
      })
      if (!response.ok) {
        const error = new Error(`续读失败（${response.status}）`)
        error.status = response.status
        if (response.status === 401 && typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('luma-auth-required'))
        if (response.status === 503 && typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('luma-service-unavailable'))
        throw error
      }
      lastActivityAt = Date.now()
      resetIdleTimer()
      retryDelay = 1000
      await consumeSSE(response, (event, payload = {}, eventId = '') => {
        markActivity()
        if (eventId) lastEventId = eventId
        if (onEvent?.(event, payload, eventId) === true) {
          completed = true
          return true
        }
        return false
      }, markActivity)
      if (completed) return { eventId: lastEventId }
      throw new Error('续读连接已断开')
    } catch (error) {
      lastError = error
      if (signal?.aborted || error?.name === 'AbortError' && !idleExpired && !totalExpired) throw error
      if (error?.status === 401) throw error
      const elapsed = Date.now() - startedAt
      const idleFor = Date.now() - lastActivityAt
      if (totalExpired || elapsed >= RESUME_MAX_DURATION_MS) break
      if (idleExpired || idleFor >= RESUME_IDLE_TIMEOUT_MS) {
        if (idleReconnectAttempted) break
        idleReconnectAttempted = true
        continue
      }
      const remaining = Math.min(RESUME_IDLE_TIMEOUT_MS - idleFor, RESUME_MAX_DURATION_MS - elapsed)
      await waitForRetry(Math.min(retryDelay, Math.max(1, remaining)), signal)
      retryDelay = Math.min(10000, retryDelay * 2)
    } finally {
      if (idleTimer) window.clearTimeout(idleTimer)
      if (totalTimer) window.clearTimeout(totalTimer)
      signal?.removeEventListener('abort', onAbort)
    }
  }

  const timeout = new Error(lastError?.message || '流式响应未完整结束')
  timeout.code = 'resume_timeout'
  throw timeout
}

function streamAssistantId() {
  return `stream-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

function WorkspaceApp() {
  const [view, setView] = useState('chat')
  const [data, setData] = useState(() => createEmptyState())
  const [offline, setOffline] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [input, setInput] = useState('')
  const [composerResetKey, setComposerResetKey] = useState(0)
  const [voiceStartKey, setVoiceStartKey] = useState(0)
  const [generatingBySession, setGeneratingBySession] = useState(() => new Map())
  const [completedSessions, setCompletedSessions] = useState(() => new Set())
  const [widgetPendingSessions, setWidgetPendingSessions] = useState(() => new Set())
  const [sentMessageVersion, setSentMessageVersion] = useState(0)
  const [streamSubscriptionVersion, setStreamSubscriptionVersion] = useState(0)
  const [files, setFiles] = useState([])
  const [uploading, setUploading] = useState(false)
  const [toast, setToast] = useState('')
  const [updateAvailable, setUpdateAvailable] = useState(false)
  const [serviceUnavailable, setServiceUnavailable] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settings, setSettings] = useState(() => ({
    // The workspace follows Muse's light, content-first visual language by
    // default. A user's explicit theme preference is still respected.
    theme: localStorage.getItem('luma-theme') || 'light',
    notifications: localStorage.getItem('luma-notifications') !== 'off',
    enterToSend: localStorage.getItem('luma-enter-to-send') !== 'off',
    voiceRaw: localStorage.getItem('luma-voice-raw') === 'on',
    accent: localStorage.getItem('luma-accent') || ACCENTS[0].value,
    fontSize: localStorage.getItem('luma-font-size') || 'standard',
  }))
  const [systemDark, setSystemDark] = useState(() => window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false)
  const resolvedTheme = settings.theme === 'system' ? (systemDark ? 'dark' : 'light') : settings.theme
  const [provider, setProvider] = useState(null)
  const [account, setAccount] = useState(null)
  const [hasMoreMessages, setHasMoreMessages] = useState(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [panelOpen, setPanelOpen] = useState(() => {
    const saved = localStorage.getItem('luma-context-panel')
    return saved ? saved === 'open' : window.innerWidth >= 1180
  })
  const [chatsPanelOpen, setChatsPanelOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const [highlightedMessageId, setHighlightedMessageId] = useState('')
  const generationsRef = useRef(new SessionGenerations())
  // The active generation owns its transport so the composer can stop a
  // request before the server has emitted an assistant message id.
  const activeGenerationRef = useRef(null)
  const widgetEventPendingRef = useRef(new Set())
  const notificationAbortRef = useRef(null)
  const notificationsRef = useRef([])
  const oldestMessageIdRef = useRef('')
  const loadedOlderMessagesRef = useRef(false)
  // Keep the selected conversation independent of the five-second workspace
  // poll. A poll must never switch a user back to the main chat after they
  // opened a side chat.
  const activeSessionIdRef = useRef('')
  const sessionSwitchRef = useRef(0)
  const workspaceRevisionRef = useRef(0)
  const messagesStateRef = useRef(data.messages)
  const hasMoreMessagesRef = useRef(hasMoreMessages)
  const desktopCommandsRef = useRef(null)
  const sendTextRef = useRef(null)
  sendTextRef.current = sendText
  messagesStateRef.current = data.messages
  hasMoreMessagesRef.current = hasMoreMessages
  const sending = generatingBySession.has(data.sessionId)

  useEffect(() => subscribeDesktopCommands(window.desktop, () => desktopCommandsRef.current), [])

  useEffect(() => {
    if (view !== 'chat') setChatsPanelOpen(false)
  }, [view])

  function publishGenerations() {
    setGeneratingBySession(new Map(generationsRef.current.messages))
    setCompletedSessions(new Set(generationsRef.current.completed))
  }

  function trackGeneration(sessionId, messageId = '') {
    const progress = generationsRef.current.track(sessionId, messageId)
    publishGenerations()
    return progress
  }

  function finishGeneration(sessionId, messageId, complete = false) {
    if (generationsRef.current.finish(sessionId, messageId, { selectedSessionId: activeSessionIdRef.current, complete })) {
      if (sessionId === activeSessionIdRef.current) workspaceRevisionRef.current += 1
      publishGenerations()
    }
  }

  function isSessionBusy(sessionId = activeSessionIdRef.current) {
    return generationsRef.current.messages.has(sessionId) || widgetEventPendingRef.current.has(sessionId)
  }

  function reconcileGeneration(sessionId, messages, expectedId) {
    const tracker = generationsRef.current
    if (!tracker.messages.has(sessionId) || tracker.messages.get(sessionId) !== expectedId) return
    const progress = tracker.progress.get(sessionId)
    const latestAssistant = [...messages].reverse().find((message) => message.role === 'assistant')
    const message = expectedId
      ? messages.find((item) => item.id === expectedId)
      : latestAssistant?.id !== progress?.previousAssistantId ? latestAssistant : null
    if (!message) return
    if (!expectedId) trackGeneration(sessionId, message.id)
    if (progress?.cancelRequested && isGeneratingMessage(message)) {
      if (progress.cancelInFlight) return
      progress.cancelInFlight = true
      request(`/messages/${encodeURIComponent(message.id)}/cancel`, { method: 'POST' }).then(() => {
        finishGeneration(sessionId, message.id)
        setData((current) => current.sessionId !== sessionId ? current : {
          ...current, messages: current.messages.map((item) => item.id === message.id ? { ...item, status: 'incomplete', streaming: false } : item),
        })
        notify('已停止生成')
      }).catch((error) => { progress.cancelInFlight = false; notify(generationError(error, '停止生成失败')) })
      return
    }
    if (isGeneratingMessage(message)) return
    finishGeneration(sessionId, message.id, message.status === 'complete')
    setData((current) => ({
      ...current,
      sessions: current.sessions.map((session) => session.id === sessionId ? {
        ...session, last_message_preview: plainTextPreview(message.content), last_message_at: message.created_at,
      } : session),
    }))
  }

  useEffect(() => {
    let active = true
    let polling = false
    async function pollBackgroundGenerations() {
      if (polling) return
      polling = true
      try {
        for (const [sessionId, messageId] of [...generationsRef.current.messages]) {
          if (!active || !generationsRef.current.shouldPoll(sessionId, activeSessionIdRef.current)) continue
          try {
            const page = await requestMessagesPage(sessionId)
            if (active) reconcileGeneration(sessionId, page.messages, messageId)
          } catch { /* the next bounded poll retries transient failures */ }
        }
      } finally { polling = false }
    }
    const timer = window.setInterval(pollBackgroundGenerations, 5000)
    return () => {
      active = false
      window.clearInterval(timer)
      disconnectGeneration(activeGenerationRef.current)
    }
  }, [])

  const pendingTasks = useMemo(() => data.tasks.filter((task) => !['done', 'cancelled'].includes(task.status)), [data.tasks])
  const hasConversation = useMemo(() => data.messages.some((message) => message.role === 'user'), [data.messages])

  useEffect(() => {
    const query = window.matchMedia?.('(prefers-color-scheme: dark)')
    if (!query) return undefined
    const onChange = (event) => setSystemDark(event.matches)
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])

  useEffect(() => {
    const onKeyDown = (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setSearchOpen(true)
      }
      if (event.key === 'Escape') {
        if (searchOpen) setSearchOpen(false)
        if (chatsPanelOpen) setChatsPanelOpen(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [chatsPanelOpen, searchOpen])

  useEffect(() => {
    document.documentElement.dataset.theme = resolvedTheme
    localStorage.setItem('luma-theme', settings.theme)
  }, [settings.theme, resolvedTheme])

  useEffect(() => {
    localStorage.setItem('luma-notifications', settings.notifications ? 'on' : 'off')
    localStorage.setItem('luma-enter-to-send', settings.enterToSend ? 'on' : 'off')
    localStorage.setItem('luma-voice-raw', settings.voiceRaw ? 'on' : 'off')
    localStorage.setItem('luma-accent', settings.accent)
    localStorage.setItem('luma-font-size', settings.fontSize)
  }, [settings.notifications, settings.enterToSend, settings.voiceRaw, settings.accent, settings.fontSize])

  useEffect(() => {
    localStorage.setItem('luma-context-panel', panelOpen ? 'open' : 'closed')
  }, [panelOpen])

  useEffect(() => {
    const onUnavailable = () => setServiceUnavailable(true)
    window.addEventListener('luma-service-unavailable', onUnavailable)
    return () => window.removeEventListener('luma-service-unavailable', onUnavailable)
  }, [])

  // Notifications are delivered over one resumable SSE connection. The
  // cursor is the server's event id, so reconnecting cannot lose an event
  // that arrived while a tab was offline.
  useEffect(() => {
    let active = true
    let lastEventId = ''
    let retryDelay = 1000
    let retryTimer = null

    function scheduleReconnect() {
      if (!active || retryTimer) return
      retryTimer = window.setTimeout(() => {
        retryTimer = null
        connect()
      }, retryDelay)
      retryDelay = Math.min(60000, retryDelay * 2)
    }

    async function connect() {
      if (!active) return
      const controller = new AbortController()
      notificationAbortRef.current = controller
      try {
        const headers = { Accept: 'text/event-stream', ...authHeaders() }
        if (lastEventId) headers['Last-Event-ID'] = lastEventId
        const response = await fetch(`${API_URL}/notifications/stream`, { headers, credentials: 'include', signal: controller.signal })
        if (!response.ok) {
          const detail = await response.text()
          const error = new Error(detail || `通知流失败（${response.status}）`)
          error.status = response.status
          if (response.status === 401) {
            active = false
            if (retryTimer) window.clearTimeout(retryTimer)
            notificationAbortRef.current?.abort()
            window.dispatchEvent(new CustomEvent('luma-auth-required'))
          }
          if (response.status === 503) window.dispatchEvent(new CustomEvent('luma-service-unavailable'))
          throw error
        }
        retryDelay = 1000
        setServiceUnavailable(false)
        await consumeSSE(response, (event, payload, eventId) => {
          if (eventId) lastEventId = eventId
          if (event !== 'notification' || !payload?.id) return
          setData((current) => {
            const notifications = [payload, ...(current.notifications || []).filter((item) => item.id !== payload.id)]
            notificationsRef.current = notifications.slice(0, 100)
            return { ...current, notifications: notifications.slice(0, 100) }
          })
          notifyDesktop(payload.title || 'Luma 通知', payload.body || '')
        })
        if (active) scheduleReconnect()
      } catch (error) {
        if (error?.name !== 'AbortError' && error?.status !== 401 && active) {
          if (error?.status === 503) setServiceUnavailable(true)
          scheduleReconnect()
        }
      }
    }

    connect()
    return () => {
      active = false
      if (retryTimer) window.clearTimeout(retryTimer)
      notificationAbortRef.current?.abort()
    }
  }, [settings.notifications])

  useEffect(() => {
    let active = true
    async function checkForUpdate() {
      if (!active || generationsRef.current.messages.size || document.visibilityState === 'hidden') return
      const currentScript = document.querySelector('script[src*="assets/index-"]')?.getAttribute('src')
      if (!currentScript) return
      try {
        const html = await fetch('/app', { cache: 'no-store', credentials: 'include' }).then((response) => response.ok ? response.text() : '')
        const remoteScript = html.match(/assets\/index-[^"' ?]+\.js/)?.[0]
        if (remoteScript && !currentScript.includes(remoteScript)) setUpdateAvailable(true)
      } catch { /* best effort */ }
    }
    checkForUpdate()
    const interval = window.setInterval(checkForUpdate, 10 * 60 * 1000)
    const onFocus = () => checkForUpdate()
    const onVisibility = () => { if (document.visibilityState === 'visible') checkForUpdate() }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)
    return () => { active = false; window.clearInterval(interval); window.removeEventListener('focus', onFocus); document.removeEventListener('visibilitychange', onVisibility) }
  }, [])

  useEffect(() => {
    let active = true
    request('/auth/me').then((me) => { if (active) setAccount(me?.user || me || null) }).catch(() => {})
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    let lastRemoteSignature = ''
    let initialNotificationsLoaded = false
    async function load() {
      const revision = workspaceRevisionRef.current
      try {
        // The server owns the single main-chat invariant. Do not recreate a
        // session from the first list item: that used to race across workers
        // and could turn a side chat into the active chat during polling.
        const [mainSession, listedSessions] = await Promise.all([
          request('/sessions/main'),
          request('/sessions?limit=50'),
        ])
        const sessions = Array.isArray(listedSessions) ? [...listedSessions] : []
        if (mainSession?.id && !sessions.some((item) => item.id === mainSession.id)) sessions.unshift(mainSession)
        const selectedId = activeSessionIdRef.current || mainSession?.id
        const session = sessions.find((item) => item.id === selectedId) || mainSession
        if (!session?.id) throw new Error('没有可用的聊天会话')
        activeSessionIdRef.current = session.id
        const switchToken = sessionSwitchRef.current
        const observedGenerationId = generationsRef.current.messages.get(session.id)
        const [messagePage, tasks, memories, runtimeJobs, approvals, runtimeActivity, goals, notificationResult, sessionFiles, routines] = await Promise.all([
          requestMessagesPage(session.id),
          request('/tasks'),
          request('/memories'),
          request('/runtime/jobs'),
          request('/runtime/approvals'),
          request('/runtime/activity'),
          request('/goals'),
          initialNotificationsLoaded ? Promise.resolve(null) : request('/notifications?unread_only=true'),
          request(`/files?session_id=${encodeURIComponent(session.id)}`),
          request('/routines'),
        ])
        // A user may have selected another session while the poll was in
        // flight. Its response must not replace the newer conversation.
        if (activeSessionIdRef.current !== session.id || switchToken !== sessionSwitchRef.current || workspaceRevisionRef.current !== revision) return
        const messages = messagePage.messages
        if (Array.isArray(notificationResult)) {
          const byId = new Map(notificationResult.map((item) => [item.id, item]))
          notificationsRef.current.forEach((item) => byId.set(item.id, item))
          notificationsRef.current = Array.from(byId.values()).slice(0, 100)
          initialNotificationsLoaded = true
        }
        const notifications = notificationsRef.current
        oldestMessageIdRef.current = messagePage.oldestId
        setHasMoreMessages(messagePage.hasMore)
        const snapshot = { ...createEmptyState(), sessions, sessionId: session.id, messages, tasks, memories, runtimeJobs, approvals, goals, notifications, files: sessionFiles, routines, activity: runtimeActivity.map(runtimeActivityToUi) }
        const signature = JSON.stringify({
          sessionId: snapshot.sessionId,
          sessions: sessions.map((item) => [item.id, item.title, item.kind, item.last_message_preview, item.last_message_at, item.message_count]),
          messages: messages.map((message) => [message.id, message.role, message.status, message.content, message.created_at, message.metadata]),
          tasks,
          memories,
          runtimeJobs,
          approvals,
          runtimeActivity,
          goals,
          notifications,
          sessionFiles,
          routines,
        })
        // Polling keeps already-open Web/Electron windows in sync with a
        // message sent by another device. Do not replace the optimistic
        // streaming view while this client is actively sending.
        if (active) {
          setOffline(false)
          setLoadError('')
          if (!(activeGenerationRef.current?.sessionId === session.id && !activeGenerationRef.current.detached)) {
            reconcileGeneration(session.id, messages, observedGenerationId)
            const pending = latestGeneratingMessage(messages)
            if (pending) {
              trackGeneration(session.id, pending.id)
              setStreamSubscriptionVersion((current) => current + 1)
            }
          }
          if (!(activeGenerationRef.current?.sessionId === session.id && !activeGenerationRef.current.detached) && !widgetEventPendingRef.current.has(session.id) && signature !== lastRemoteSignature) {
            lastRemoteSignature = signature
            setData((current) => {
              if (current.sessionId !== snapshot.sessionId) return snapshot
              const refreshed = { ...snapshot, messages: preserveBrowserLiveMessages(snapshot.messages, current.messages) }
              if (!loadedOlderMessagesRef.current) return refreshed
              const byId = new Map(current.messages.map((item) => [item.id, item]))
              refreshed.messages.forEach((item) => byId.set(item.id, item))
              const mergedMessages = Array.from(byId.values()).sort((a, b) => {
                const left = new Date(a.created_at).getTime()
                const right = new Date(b.created_at).getTime()
                return (Number.isNaN(left) ? 0 : left) - (Number.isNaN(right) ? 0 : right) || String(a.id).localeCompare(String(b.id))
              })
              return { ...refreshed, messages: mergedMessages }
            })
          }
        }
      } catch (error) {
        if (active) {
          setOffline(true)
          setLoadError(error?.status === 503 ? '' : '无法加载工作区数据，请稍后重试。')
          setData((current) => current.sessions.length ? current : createEmptyState())
        }
      }
    }
    load()
    const interval = window.setInterval(load, 5000)
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') load()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      active = false
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [])

  async function loadProviderStatus() {
    try {
      const status = await request('/provider/status')
      setProvider(status)
    } catch {
      setProvider({ provider: 'unavailable', configured: false })
    }
  }

  function openSettings() {
    setChatsPanelOpen(false)
    setSettingsOpen(true)
    loadProviderStatus()
  }

  function openAdmin() {
    setChatsPanelOpen(false)
    if (window.location.protocol === 'file:') window.open(`${API_URL.replace(/\/api\/v1$/, '')}/admin`)
    else navigate('/admin')
  }

  function notifyDesktop(title, body) {
    if (!settings.notifications || !document.hidden || typeof Notification === 'undefined' || Notification.permission !== 'granted') return
    try { new Notification(title, { body: String(body || '').slice(0, 120), tag: 'luma-reply' }) } catch { /* unsupported context */ }
  }

  function notify(message) {
    setToast(message)
    window.setTimeout(() => setToast(''), 2200)
  }

  async function refreshSessionList() {
    const sessions = await request('/sessions?limit=50')
    if (!Array.isArray(sessions)) return []
    setData((current) => ({ ...current, sessions }))
    return sessions
  }

  const requestSearch = useCallback(async (query, { signal } = {}) => {
    const response = await fetch(`${API_URL}/search?q=${encodeURIComponent(query)}&limit=20`, {
      headers: { Accept: 'application/json', ...authHeaders() },
      credentials: 'include',
      signal,
    })
    if (!response.ok) throw new Error(`搜索失败（${response.status}）`)
    return response.json()
  }, [])

  async function openSession(id) {
    if (!id) return false
    const generation = activeGenerationRef.current
    if (generation) {
      disconnectGeneration(generation)
      if (activeGenerationRef.current === generation) activeGenerationRef.current = null
    }
    generationsRef.current.completed.delete(id)
    publishGenerations()
    const switchToken = sessionSwitchRef.current + 1
    const revision = workspaceRevisionRef.current
    sessionSwitchRef.current = switchToken
    activeSessionIdRef.current = id
    loadedOlderMessagesRef.current = false
    oldestMessageIdRef.current = ''
    setHasMoreMessages(false)
    setView('chat')
    setFiles([])
    // Clear the visible page immediately so a late frame from the previous
    // transport cannot be mistaken for a message in the newly selected chat.
    setData((current) => ({ ...current, sessionId: id, messages: [], files: [] }))
    try {
      const [page, sessionFiles] = await Promise.all([
        requestMessagesPage(id),
        request(`/files?session_id=${encodeURIComponent(id)}`),
      ])
      if (switchToken !== sessionSwitchRef.current || activeSessionIdRef.current !== id || workspaceRevisionRef.current !== revision) return false
      if (activeGenerationRef.current?.sessionId === id) return true
      oldestMessageIdRef.current = page.oldestId || ''
      setHasMoreMessages(page.hasMore)
      const pending = latestGeneratingMessage(page.messages)
      if (pending) trackGeneration(id, pending.id)
      else reconcileGeneration(id, page.messages, generationsRef.current.messages.get(id))
      setData((current) => ({ ...current, sessionId: id, messages: page.messages, files: sessionFiles || [] }))
      return true
    } catch (error) {
      if (switchToken === sessionSwitchRef.current) notify(error?.status === 503 ? '服务暂时不可用' : '无法加载这个聊天')
      return false
    }
  }

  function findLoadedMessage(messageId) {
    return messagesStateRef.current.find((item) => item.id === messageId)
  }

  function scrollToMessage(messageId) {
    setHighlightedMessageId(messageId)
    window.requestAnimationFrame(() => {
      const element = Array.from(document.querySelectorAll('[data-message-id]')).find((item) => item.getAttribute('data-message-id') === String(messageId))
      element?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
    window.setTimeout(() => setHighlightedMessageId((current) => current === messageId ? '' : current), 1500)
  }

  async function openSearchMessage(message) {
    if (!message?.session_id) return
    setSearchOpen(false)
    await openSession(message.session_id)
    await new Promise((resolve) => window.setTimeout(resolve, 0))
    let found = findLoadedMessage(message.id)
    let pages = 0
    while (!found && pages < 10 && hasMoreMessagesRef.current) {
      const hasMore = await loadOlderMessages(message.session_id)
      pages += 1
      await new Promise((resolve) => window.setTimeout(resolve, 0))
      found = findLoadedMessage(message.id)
      if (!hasMore) break
    }
    if (found) scrollToMessage(message.id)
    else notify('消息较早，已打开所在对话')
  }

  async function openContentSession(id, { focus = false } = {}) {
    const opening = openSession(id)
    void refreshSessionList().catch(() => {})
    const opened = await opening
    if (!opened || activeSessionIdRef.current !== id) return false
    setChatsPanelOpen(false)
    if (focus) setComposerResetKey((value) => value + 1)
    return true
  }

  async function startIdeaChat(result) {
    return startIdeaSession(result, openContentSession, (prompt, metadata, sessionId) => {
      if (activeSessionIdRef.current !== sessionId) return false
      return sendTextRef.current(prompt, metadata, sessionId)
    })
  }

  async function renameSession(session, title) {
    if (!session?.id || session.kind === 'main' || typeof title !== 'string') return
    const nextTitle = title.trim()
    if (!nextTitle || nextTitle === (session.title || '新旁聊').trim()) return
    try {
      const updated = await request(`/sessions/${encodeURIComponent(session.id)}`, { method: 'PATCH', body: JSON.stringify({ title: nextTitle }) })
      workspaceRevisionRef.current += 1
      setData((current) => ({ ...current, sessions: current.sessions.map((item) => item.id === session.id ? { ...item, ...(updated || { title: nextTitle }) } : item) }))
      notify('旁聊已重命名')
    } catch (error) {
      notify(error.message || '重命名失败')
    }
  }

  async function deleteSession(session) {
    if (!session?.id || session.kind === 'main') return
    try {
      await request(`/sessions/${encodeURIComponent(session.id)}`, { method: 'DELETE' })
      workspaceRevisionRef.current += 1
      if (activeGenerationRef.current?.sessionId === session.id) {
        disconnectGeneration(activeGenerationRef.current)
        activeGenerationRef.current = null
      }
      generationsRef.current.forget(session.id)
      publishGenerations()
      const fallback = data.sessions.find((item) => item.kind === 'main' && item.id !== session.id)
      setData((current) => ({ ...current, sessions: current.sessions.filter((item) => item.id !== session.id) }))
      if (data.sessionId === session.id && fallback?.id) await openSession(fallback.id)
      notify('旁聊已删除')
    } catch (error) {
      notify(error.message || '删除旁聊失败')
    }
  }

  async function sendText(content, messageMetadata = {}, sessionId = data.sessionId) {
    content = content.trim()
    if (!content || isSessionBusy(sessionId)) return false
    if (!sessionId) return false
    const isConfirmationContinuation = ['confirm', 'cancel'].includes(messageMetadata.widget_event?.action)
    const selectedSession = data.sessions.find((item) => item.id === sessionId)
    const shouldRefreshSideTitle = !isConfirmationContinuation && selectedSession?.kind === 'side' && ['新旁聊', '新对话', '新的对话'].includes(selectedSession.title)
    const hasSecrets = containsSecretJson(content)
    if (activeSessionIdRef.current === sessionId) workspaceRevisionRef.current += 1
    const progress = trackGeneration(sessionId)
    progress.previousAssistantId = [...data.messages].reverse().find((message) => message.role === 'assistant')?.id || ''
    const optimistic = isConfirmationContinuation ? null : { id: `local-${Date.now()}`, role: 'user', content: optimisticChatContent(content), created_at: '现在', metadata: messageMetadata }
    const preview = optimistic ? plainTextPreview(optimistic.content) : ''
    const previewAt = new Date().toISOString()
    if (optimistic) setData((current) => {
      if (current.sessionId !== sessionId) return current
      const selected = current.sessions.find((item) => item.id === sessionId)
      const updatedSession = selected ? { ...selected, last_message_preview: preview, last_message_at: previewAt, message_count: Number(selected.message_count || 0) + 1 } : null
      return {
        ...current,
        messages: [...current.messages, optimistic],
        sessions: updatedSession ? [updatedSession, ...current.sessions.filter((item) => item.id !== sessionId)] : current.sessions,
      }
    })
    if (activeSessionIdRef.current === sessionId) setSentMessageVersion((current) => current + 1)

    // Do not fabricate an assistant response while the API is unavailable.
    if (offline) {
      if (optimistic) setData((current) => {
        if (current.sessionId !== sessionId) return current
        const sessions = selectedSession
          ? [selectedSession, ...current.sessions.filter((item) => item.id !== sessionId)]
          : current.sessions
        return { ...current, messages: current.messages.filter((message) => message.id !== optimistic.id), sessions }
      })
      notify('服务暂时不可用，请稍后重试')
      finishGeneration(sessionId, '')
      return true
    }

    let started = false
    let assistantId = ''
    let assistantContent = ''
    let toolEvents = []
    let done = false
    let streamError = ''
    let lastEventId = ''
    const streamStartedAt = Date.now()
    const generation = {
      controller: new AbortController(),
      sessionId,
      assistantId: '',
      remoteId: '',
      cancelled: false,
      detached: false,
    }
    if (activeSessionIdRef.current === sessionId) activeGenerationRef.current = generation

    function updateGenerationData(updater) {
      setData((current) => {
        if (generation.cancelled || generation.detached || current.sessionId !== generation.sessionId || activeSessionIdRef.current !== generation.sessionId) return current
        return updater(current)
      })
    }

    function ensureAssistant(start = {}) {
      if (generation.cancelled || generation.detached) return false
      if (started && assistantId) return true
      started = true
      generation.remoteId = start.message_id || generation.remoteId || ''
      assistantId = generation.remoteId || streamAssistantId()
      generation.assistantId = assistantId
      trackGeneration(sessionId, generation.remoteId)
      if (activeSessionIdRef.current !== sessionId) {
        disconnectGeneration(generation)
        return false
      }
      toolEvents = data.messages.find((message) => message.id === assistantId)?.toolEvents || []
      const assistant = {
        id: assistantId,
        role: 'assistant',
        content: '',
        created_at: start.created_at || '现在',
        status: 'streaming',
        streaming: true,
        metadata: start.metadata || {},
      }
      updateGenerationData((current) => {
        const found = current.messages.some((message) => message.id === assistantId)
        return {
          ...current,
          messages: found
            ? current.messages.map((message) => message.id === assistantId ? {
              ...message,
              ...assistant,
              created_at: start.created_at || message.created_at,
              metadata: { ...(message.metadata || {}), ...(start.metadata || {}) },
            } : message)
            : [...current.messages, assistant],
        }
      })
      return true
    }

    function applyStreamEvent(event, payload = {}, eventId = '') {
      if (generation.cancelled || generation.detached) return true
      if (eventId) { lastEventId = eventId; progress.after = eventId }
      if (event === 'start') {
        if (!ensureAssistant(payload)) return true
        // The backend auto-names a default side chat when the first user
        // message is persisted. Refresh as soon as the stream starts so the
        // panel gets that title without waiting for the next poll.
        if (shouldRefreshSideTitle) void refreshSessionList().catch(() => {})
        // Some server versions echo the persisted user message. Replace the
        // optimistic row in place so it can never appear twice.
        if (optimistic && typeof payload.user_message?.content === 'string') {
          updateGenerationData((current) => ({
            ...current,
            messages: current.messages.map((message) => message.id === optimistic.id ? { ...message, ...payload.user_message } : message),
            sessions: current.sessions.map((session) => session.id === sessionId && session.last_message_at === previewAt
              ? { ...session, last_message_preview: plainTextPreview(payload.user_message.content) }
              : session),
          }))
        }
        return
      }
      if (event === 'status') {
        if (!ensureAssistant(payload)) return true
        const status = payload.status || 'streaming'
        updateGenerationData((current) => ({ ...current, messages: current.messages.map((message) => message.id === assistantId ? { ...message, status, streaming: status === 'streaming' } : message) }))
        return
      }
      if (event === 'delta') {
        if (!ensureAssistant()) return true
        const delta = typeof payload.content === 'string' ? payload.content : ''
        if (!delta) return
        assistantContent += delta
        progress.content = assistantContent
        updateGenerationData((current) => ({
          ...current,
          messages: current.messages.map((message) => message.id === assistantId ? { ...message, content: assistantContent, status: 'streaming', streaming: true } : message),
        }))
        return
      }
      if (event === 'tool' || event === 'widget' || event === 'approval' || event === 'tool_limit') {
        if (!ensureAssistant()) return true
        toolEvents = mergeStreamEvent(toolEvents, event, payload, eventId)
        progress.toolEvents = toolEvents
        updateGenerationData((current) => ({
          ...current,
          messages: current.messages.map((message) => {
            if (message.id !== assistantId) return message
            const next = { ...message, toolEvents, status: 'streaming', streaming: true }
            // A persisted widget is rendered from metadata once the done
            // payload arrives. During streaming, retain a validated object
            // from the event so a reconnect does not discard it.
            if (event === 'widget' && payload.id && (payload.spec || payload.type)) {
              const widgets = Array.isArray(message.metadata?.widgets) ? message.metadata.widgets : []
              next.metadata = { ...(message.metadata || {}), widgets: [...widgets.filter((item) => item.id !== payload.id), payload] }
            }
            return next
          }),
        }))
        return
      }
      if (event === 'error') {
        if (!assistantId) return true
        streamError = payload.message || payload.error || '流式响应中断'
        updateGenerationData((current) => ({ ...current, messages: current.messages.map((message) => message.id === assistantId ? { ...message, metadata: { ...(message.metadata || {}), error: streamError } } : message) }))
        return
      }
      if (event === 'done') {
        if (generation.cancelled || generation.detached) return true
        done = true
        const persisted = payload && typeof payload === 'object' ? payload : {}
        generation.remoteId = persisted.id || generation.remoteId || ''
        assistantId = persisted.id || assistantId || streamAssistantId()
        generation.assistantId = assistantId
        assistantContent = typeof persisted.content === 'string' ? persisted.content : assistantContent
        const finalStatus = persisted.status || (streamError ? 'error' : 'complete')
        finishGeneration(sessionId, generation.remoteId, finalStatus === 'complete')
        const finalAssistant = {
          id: assistantId,
          role: 'assistant',
          content: assistantContent,
          created_at: persisted.created_at || '刚刚',
          status: finalStatus,
          metadata: { ...(persisted.metadata || {}), ...(streamError ? { error: streamError } : {}) },
          toolEvents,
          streaming: false,
        }
        updateGenerationData((current) => {
          const found = current.messages.some((message) => message.id === assistantId)
          const assistantPreview = plainTextPreview(assistantContent)
          const assistantAt = persisted.created_at || new Date().toISOString()
          const selected = current.sessions.find((item) => item.id === sessionId)
          const updatedSession = selected && assistantPreview
            ? { ...selected, last_message_preview: assistantPreview, last_message_at: assistantAt, message_count: Number(selected.message_count || 0) + (isConfirmationContinuation ? 0 : 1) }
            : selected
          return {
            ...current,
            messages: found
              ? current.messages.map((message) => message.id === assistantId ? { ...message, ...finalAssistant } : message)
              : [...current.messages, finalAssistant],
            sessions: updatedSession ? [updatedSession, ...current.sessions.filter((item) => item.id !== sessionId)] : current.sessions,
            activity: finalStatus === 'complete' ? [{ time: '刚刚', text: '完成了一次对话', tag: 'CHAT' }, ...current.activity] : current.activity,
          }
        })
        if (finalStatus === 'complete' && shouldRefreshSideTitle) void refreshSessionList().catch(() => {})
        if (finalStatus === 'complete') notifyDesktop('Luma 已回复', assistantContent)
        return true
      }
      return false
    }

    async function resumeUntilDone() {
      if (done || generation.cancelled || generation.detached) return
      await streamResume(assistantId, {
        after: lastEventId,
        onEvent: applyStreamEvent,
        signal: generation.controller.signal,
        startedAt: streamStartedAt,
      })
    }

    try {
      const responseRequest = fetch(`${API_URL}/sessions/${encodeURIComponent(sessionId)}/messages/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', ...authHeaders() },
        body: JSON.stringify({ role: 'user', content, metadata: messageMetadata }),
        signal: generation.controller.signal,
      })
      content = ''
      const response = await responseRequest
      if (!response.ok) {
        const detail = await response.text()
        if (response.status === 401) window.dispatchEvent(new CustomEvent('luma-auth-required'))
        if (response.status === 503) window.dispatchEvent(new CustomEvent('luma-service-unavailable'))
        const error = new Error(detail || `流式接口失败（${response.status}）`)
        error.status = response.status
        throw error
      }

      try {
        await consumeSSE(response, applyStreamEvent)
        if (!done && started) await resumeUntilDone()
      } catch (connectionError) {
        if (!started || done || generation.detached || generation.cancelled) throw connectionError
        await resumeUntilDone()
      }

      if (generation.cancelled || generation.detached) return true
      if (!done) {
        if (!started) throw new Error(streamError || '流式响应未开始')
        streamError = streamError || '流式响应未完整结束'
      }
      if (streamError && started) {
        updateGenerationData((current) => ({
          ...current,
          messages: current.messages.map((message) => message.id === assistantId ? { ...message, status: 'incomplete', streaming: false, metadata: { ...(message.metadata || {}), incomplete: true, error: streamError } } : message),
        }))
        notify(streamError)
      }
    } catch (error) {
      if (generation.detached || generation.cancelled) {
        if (assistantId) {
          updateGenerationData((current) => ({
            ...current,
            messages: current.messages.map((item) => item.id === assistantId ? { ...item, status: 'incomplete', streaming: false, metadata: { ...(item.metadata || {}), incomplete: true, error: 'client_cancelled' } } : item),
          }))
        }
        return true
      }
      if (started) {
        const message = streamError || '连接中断，正在同步回复'
        updateGenerationData((current) => ({
          ...current,
          messages: current.messages.map((item) => item.id === assistantId ? { ...item, metadata: { ...(item.metadata || {}), sync_error: message } } : item),
        }))
        notify(message)
      } else {
        // Before `start`, no remote assistant exists. Keep the user's message
        // visible and surface the actual transport error instead of inventing
        // a local model response.
        notify(hasSecrets ? '敏感信息未确认保存，请重新粘贴后重试' : generationError(error))
      }
    } finally {
      // Disconnection preserves cloud state. A late transport completion must
      // not clear another conversation's generation or subscription.
      if (!generation.detached && !generation.cancelled && !started) finishGeneration(sessionId, '')
      if (activeGenerationRef.current === generation) {
        activeGenerationRef.current = null
        if (started && !done && !generation.detached && !generation.cancelled) setStreamSubscriptionVersion((current) => current + 1)
      }
    }
    return true
  }

  const resumableMessageId = latestGeneratingMessage(data.messages)?.id || ''

  // Only the selected session owns an SSE transport. Reopening it resumes the
  // durable assistant and cursor rather than issuing another POST.
  useEffect(() => {
    const message = data.messages.find((item) => item.id === resumableMessageId)
    const sessionId = data.sessionId
    if (!message || !sessionId || activeGenerationRef.current) return undefined
    const progress = trackGeneration(sessionId, message.id)
    if (progress.cancelRequested) return undefined
    let active = true
    const generation = {
      controller: new AbortController(), sessionId,
      assistantId: message.id, remoteId: message.id,
      cancelled: false, detached: false,
    }
    activeGenerationRef.current = generation
    let content = progress.after ? progress.content : ''
    let hasDelta = Boolean(progress.after)
    let toolEvents = progress.toolEvents || message.toolEvents || []

    function updateMessages(updater) {
      setData((current) => {
        if (!active || generation.detached || generation.cancelled || current.sessionId !== sessionId || activeSessionIdRef.current !== sessionId) return current
        return { ...current, messages: current.messages.map((item) => item.id === message.id ? updater(item) : item) }
      })
    }

    function apply(event, payload = {}, eventId = '') {
      if (!active || generation.detached || generation.cancelled) return true
      if (eventId) progress.after = eventId
      if (event === 'delta') {
        const delta = typeof payload.content === 'string' ? payload.content : ''
        if (!delta) return
        if (!hasDelta) { hasDelta = true; content = '' }
        content += delta
        progress.content = content
        updateMessages((item) => ({ ...item, content, status: 'streaming', streaming: true }))
      } else if (event === 'tool' || event === 'widget' || event === 'approval' || event === 'tool_limit') {
        toolEvents = mergeStreamEvent(toolEvents, event, payload, eventId)
        progress.toolEvents = toolEvents
        updateMessages((item) => {
          const next = { ...item, toolEvents, status: 'streaming', streaming: true }
          if (event === 'widget' && payload.id && (payload.spec || payload.type)) {
            const widgets = Array.isArray(item.metadata?.widgets) ? item.metadata.widgets : []
            next.metadata = { ...(item.metadata || {}), widgets: [...widgets.filter((widget) => widget.id !== payload.id), payload] }
          }
          return next
        })
      } else if (event === 'status') {
        const status = payload.status || 'streaming'
        updateMessages((item) => ({ ...item, status, streaming: ['pending', 'streaming'].includes(status) }))
      } else if (event === 'error') {
        updateMessages((item) => ({ ...item, metadata: { ...(item.metadata || {}), error: payload.message || payload.error } }))
      } else if (event === 'done') {
        const persisted = payload && typeof payload === 'object' ? payload : {}
        const finalContent = typeof persisted.content === 'string' ? persisted.content : (hasDelta ? content : message.content || '')
        updateMessages((item) => ({ ...item, ...persisted, content: finalContent, metadata: { ...(item.metadata || {}), ...(persisted.metadata || {}) }, status: persisted.status || 'complete', toolEvents, streaming: false }))
        finishGeneration(sessionId, message.id, (persisted.status || 'complete') === 'complete')
        return true
      }
      return false
    }

    streamResume(message.id, {
      after: progress.after, onEvent: apply, signal: generation.controller.signal,
    }).catch((error) => {
      if (!active || generation.detached || generation.cancelled || error?.name === 'AbortError') return
      // Transport failure does not imply that the cloud generation failed.
      // Workspace polling will reconcile its durable status and content.
      notify('连接中断，正在同步回复')
    }).finally(() => {
      if (activeGenerationRef.current === generation) activeGenerationRef.current = null
    })
    return () => {
      active = false
      disconnectGeneration(generation)
      if (activeGenerationRef.current === generation) activeGenerationRef.current = null
    }
  }, [data.sessionId, resumableMessageId, streamSubscriptionVersion])

  async function cancelMessage(message) {
    const sessionId = activeSessionIdRef.current
    const generation = activeGenerationRef.current
    const matchesGeneration = generation?.sessionId === sessionId && (!message || message.id === generation.assistantId || message.id === generation.remoteId)
    if (!isGeneratingMessage(message) && !matchesGeneration && !generationsRef.current.messages.has(sessionId)) return false
    workspaceRevisionRef.current += 1

    // Abort the browser transport first. This covers the short window before
    // the server has assigned an assistant id and prevents late SSE frames
    // from creating or updating a message after the user pressed stop.
    if (generation && matchesGeneration) {
      generation.cancelled = true
      generation.controller?.abort()
      if (activeGenerationRef.current === generation) activeGenerationRef.current = null
    }

    const messageId = message?.id && !String(message.id).startsWith('stream-')
      ? message.id
      : generation?.remoteId && !String(generation.remoteId).startsWith('stream-')
        ? generation.remoteId
        : generationsRef.current.messages.get(sessionId) || ''
    if (message?.id) {
      setData((current) => ({ ...current, messages: current.messages.map((item) => item.id === message.id ? { ...item, status: 'incomplete', streaming: false, metadata: { ...(item.metadata || {}), incomplete: true, error: 'client_cancelled' } } : item) }))
    }
    if (!messageId) {
      const progress = generationsRef.current.progress.get(sessionId)
      if (progress) progress.cancelRequested = true
      notify('正在停止生成')
      return true
    }
    try {
      await request(`/messages/${encodeURIComponent(messageId)}/cancel`, { method: 'POST' })
      finishGeneration(sessionId, messageId)
      notify('已停止生成')
    } catch (error) {
      notify(error.message || '停止生成失败')
    }
    return true
  }

  function retryMessage(message) {
    if (!message || message.role !== 'assistant' || isSessionBusy()) return
    const index = data.messages.findIndex((item) => item.id === message.id)
    const previous = index > 0 ? [...data.messages.slice(0, index)].reverse().find((item) => item.role === 'user') : null
    if (!previous?.content) return notify('找不到要重试的消息')
    void sendText(previous.content, previous.metadata || {})
  }

  function sendMessage(event) {
    event?.preventDefault()
    const content = input.trim()
    if (!content || isSessionBusy()) return
    if (containsSecretJson(content)) setComposerResetKey((current) => current + 1)
    const attachedFiles = files.map(({ id, filename, media_type: mediaType, size_bytes: sizeBytes, sha256 }) => ({ id, filename, media_type: mediaType, size_bytes: sizeBytes, sha256 }))
    setInput('')
    setFiles([])
    void sendText(content, attachedFiles.length ? { files: attachedFiles } : {})
  }

  async function onWidgetEvent(widget, action, value) {
    const sessionId = data.sessionId
    if (offline || isSessionBusy(sessionId)) return
    workspaceRevisionRef.current += 1
    widgetEventPendingRef.current.add(sessionId)
    setWidgetPendingSessions(new Set(widgetEventPendingRef.current))
    let result
    try {
      result = await request(`/widgets/${encodeURIComponent(widget.id)}/events`, {
        method: 'POST',
        body: JSON.stringify({ action, value }),
      })
      if (result?.widget) {
        setData((current) => current.sessionId !== sessionId ? current : ({
          ...current,
          messages: current.messages.map((message) => {
            const widgets = message.metadata?.widgets
            if (!Array.isArray(widgets) || !widgets.some((item) => item.id === widget.id)) return message
            return { ...message, metadata: { ...message.metadata, widgets: widgets.map((item) => item.id === widget.id ? result.widget : item) } }
          }),
        }))
      }
    } catch (error) {
      let detail = error.message || '组件操作失败'
      try {
        const parsed = JSON.parse(detail)
        detail = typeof parsed.detail === 'string' ? parsed.detail : detail
      } catch { /* response was plain text */ }
      notify(detail)
      throw error
    } finally {
      widgetEventPendingRef.current.delete(sessionId)
      setWidgetPendingSessions(new Set(widgetEventPendingRef.current))
    }
    if (result?.message) await sendText(result.message, { widget_event: { widget_id: widget.id, action } }, sessionId)
  }

  async function uploadFile(file) {
    if (!file || uploading) return
    setUploading(true)
    try {
      const body = new FormData()
      body.append('session_id', data.sessionId)
      body.append('upload', file)
      const response = await fetch(`${API_URL}/files`, { method: 'POST', body, credentials: 'include', headers: authHeaders() })
      if (!response.ok) throw new Error(await response.text())
      const uploaded = await response.json()
      setFiles((current) => [uploaded, ...current.filter((item) => item.id !== uploaded.id)])
      notify(`已添加 ${uploaded.filename}`)
    } catch (error) {
      notify(error.message || '文件上传失败')
    } finally {
      setUploading(false)
    }
  }

  async function removeFile(fileId) {
    try {
      await request(`/files/${encodeURIComponent(fileId)}`, { method: 'DELETE' })
      setFiles((current) => current.filter((item) => item.id !== fileId))
    } catch (error) {
      notify(error.message || '文件删除失败')
    }
  }

  async function addTask() {
    if (offline) { notify('服务暂时不可用，请稍后重试'); return }
    const title = window.prompt('你想让 Luma 跟进什么？')?.trim()
    if (!title) return
    const task = { id: `local-task-${Date.now()}`, title, description: '由对话创建，等待继续拆解', status: 'todo', progress: 0, due_at: '待安排' }
    setData((current) => ({ ...current, tasks: [task, ...current.tasks] }))
    try {
      if (!offline) await request('/tasks', { method: 'POST', body: JSON.stringify({ title, description: task.description, status: 'todo' }) })
    } catch {
      setData((current) => ({ ...current, tasks: current.tasks.filter((item) => item.id !== task.id) }))
      setOffline(true)
      notify('任务保存失败，请稍后重试')
      return
    }
    notify('任务已加入列表')
  }

  async function loadOlderMessages(targetSessionId = activeSessionIdRef.current || data.sessionId) {
    if (loadingOlder || !hasMoreMessagesRef.current || !targetSessionId || !oldestMessageIdRef.current) return false
    setLoadingOlder(true)
    try {
      const page = await requestMessagesPage(targetSessionId, { before: oldestMessageIdRef.current })
      oldestMessageIdRef.current = page.oldestId || oldestMessageIdRef.current
      setHasMoreMessages(page.hasMore)
      setData((current) => {
        if (current.sessionId !== targetSessionId) return current
        const byId = new Map(page.messages.map((item) => [item.id, item]))
        current.messages.forEach((item) => byId.set(item.id, item))
        const messages = Array.from(byId.values()).sort((a, b) => {
          const left = new Date(a.created_at).getTime()
          const right = new Date(b.created_at).getTime()
          return (Number.isNaN(left) ? 0 : left) - (Number.isNaN(right) ? 0 : right) || String(a.id).localeCompare(String(b.id))
        })
        return { ...current, messages }
      })
      loadedOlderMessagesRef.current = true
      return page.hasMore
    } catch (error) {
      notify(error?.status === 503 ? '服务暂时不可用' : '更早的消息加载失败')
      return false
    } finally {
      setLoadingOlder(false)
    }
  }

  async function startRuntimeJob(type, payload = {}) {
    try {
      const job = await request('/runtime/jobs', { method: 'POST', body: JSON.stringify({ type, payload }) })
      setData((current) => ({ ...current, runtimeJobs: [job, ...current.runtimeJobs.filter((item) => item.id !== job.id)], activity: [{ time: '刚刚', text: type === 'briefing' ? '已排队生成晨间简报' : '已排队后台任务', tag: 'RUNTIME' }, ...current.activity] }))
      setOffline(false)
      notify(job.status === 'waiting_approval' ? '已提交，等待你的确认' : '已交给后台 runtime 执行')
    } catch {
      setOffline(true)
      notify('后台 runtime 暂时不可用')
    }
  }

  async function decideRuntimeApproval(approvalId, decision, remember = false) {
    try {
      const approval = await request(`/runtime/approvals/${approvalId}/decision`, { method: 'POST', body: JSON.stringify({ decision, ...(remember ? { remember: true } : {}) }) })
      setData((current) => ({
        ...current,
        approvals: current.approvals.filter((item) => item.id !== approval.id),
        runtimeJobs: current.runtimeJobs.map((job) => job.id === approval.job_id ? { ...job, status: decision === 'approve' ? 'queued' : 'cancelled' } : job),
        activity: [{ time: '刚刚', text: decision === 'approve' ? '已批准一项后台操作' : '已拒绝一项后台操作', tag: 'APPROVAL' }, ...current.activity],
      }))
      notify(decision === 'approve' ? '已批准，runtime 将继续执行' : '已拒绝这项操作')
    } catch {
      notify('审批状态更新失败，请稍后重试')
    }
  }

  async function addMemory() {
    if (offline) { notify('服务暂时不可用，请稍后重试'); return }
    const content = window.prompt('要让 Luma 记住什么？')?.trim()
    if (!content) return
    const memory = { id: `local-memory-${Date.now()}`, category: 'fact', content }
    setData((current) => ({ ...current, memories: [memory, ...current.memories] }))
    try {
      if (!offline) await request('/memories', { method: 'POST', body: JSON.stringify({ content, category: 'fact' }) })
    } catch {
      setData((current) => ({ ...current, memories: current.memories.filter((item) => item.id !== memory.id) }))
      setOffline(true)
      notify('记忆保存失败，请稍后重试')
      return
    }
    notify('已加入记忆')
  }

  async function removeMemory(id) {
    if (offline) { notify('服务暂时不可用，请稍后重试'); return }
    setData((current) => ({ ...current, memories: current.memories.filter((memory) => memory.id !== id) }))
    try { if (!id.startsWith('offline-') && !id.startsWith('local-')) await request(`/memories/${id}`, { method: 'DELETE' }) } catch { setOffline(true) }
    notify('已删除这条记忆')
  }

  async function confirmMemory(id) {
    if (offline) { notify('服务暂时不可用，请稍后重试'); return }
    try {
      const memory = await request(`/memories/${encodeURIComponent(id)}/confirm`, { method: 'POST', body: JSON.stringify({}) })
      setData((current) => ({ ...current, memories: current.memories.map((item) => item.id === id ? memory : item) }))
      notify('已确认这条记忆')
    } catch (error) { notify(error.message || '确认记忆失败') }
  }

  async function toggleMemoryPinned(id, pinned) {
    if (offline) { notify('服务暂时不可用，请稍后重试'); return }
    setData((current) => ({ ...current, memories: current.memories.map((item) => item.id === id ? { ...item, pinned } : item) }))
    try {
      const memory = await request(`/memories/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ pinned }) })
      setData((current) => ({ ...current, memories: current.memories.map((item) => item.id === id ? memory : item) }))
    } catch (error) {
      setData((current) => ({ ...current, memories: current.memories.map((item) => item.id === id ? { ...item, pinned: !pinned } : item) }))
      notify(error.message || '更新置顶状态失败')
    }
  }

  async function createRoutine(payload) {
    if (offline) { notify('服务暂时不可用，请稍后重试'); return }
    try {
      const routine = await request('/routines', { method: 'POST', body: JSON.stringify(payload) })
      setData((current) => ({ ...current, routines: [routine, ...(current.routines || [])] }))
      notify('例程已创建')
    } catch (error) { notify(error.message || '创建例程失败'); throw error }
  }

  async function updateRoutine(id, payload) {
    if (offline) { notify('服务暂时不可用，请稍后重试'); return }
    try {
      const routine = await request(`/routines/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(payload) })
      setData((current) => ({ ...current, routines: (current.routines || []).map((item) => item.id === id ? routine : item) }))
      notify('例程已更新')
    } catch (error) { notify(error.message || '更新例程失败'); throw error }
  }

  async function deleteRoutine(id) {
    if (offline) { notify('服务暂时不可用，请稍后重试'); return }
    try {
      await request(`/routines/${encodeURIComponent(id)}`, { method: 'DELETE' })
      setData((current) => ({ ...current, routines: (current.routines || []).filter((item) => item.id !== id) }))
      notify('例程已删除')
    } catch (error) { notify(error.message || '删除例程失败') }
  }

  async function runRoutine(routine) {
    if (offline) { notify('服务暂时不可用，请稍后重试'); return }
    try {
      const job = await request(`/routines/${encodeURIComponent(routine.id)}/run`, { method: 'POST' })
      setData((current) => ({ ...current, runtimeJobs: [job, ...(current.runtimeJobs || []).filter((item) => item.id !== job.id)] }))
      notify('例程已开始运行')
    } catch (error) { notify(error.message || '运行例程失败') }
  }

  async function startNewChat() {
    try {
      const session = await request('/sessions', {
        method: 'POST',
        body: JSON.stringify({ kind: 'side' }),
      })
      setData((current) => ({
        ...current,
        sessions: [session, ...current.sessions.filter((item) => item.id !== session.id)],
      }))
      await openSession(session.id)
      setOffline(false)
      notify('已开始新的旁聊')
    } catch {
      setOffline(true)
      notify('无法创建远端会话，请检查服务连接')
    }
  }

  async function returnToMainChat() {
    try {
      let session = data.sessions.find((item) => item.kind === 'main')
      if (!session) {
        session = await request('/sessions/main')
        if (!session?.id) throw new Error('没有可用的主聊天')
        setData((current) => ({
          ...current,
          sessions: [session, ...current.sessions.filter((item) => item.id !== session.id)],
        }))
      }
      await openSession(session.id)
    } catch {
      notify('无法返回主聊天，请稍后重试')
    }
  }

  function desktopChatReady() {
    if (data.sessionId && !offline) return true
    notify(offline ? '服务暂时不可用，请稍后重试' : '正在打开工作区，请稍后重试')
    return false
  }

  desktopCommandsRef.current = {
    'new-side-chat': () => {
      if (!desktopChatReady()) return
      setSettingsOpen(false)
      setSearchOpen(false)
      void startNewChat()
    },
    'open-search': () => {
      setSettingsOpen(false)
      setSearchOpen(true)
    },
    'start-voice': () => {
      if (!desktopChatReady()) return
      setView('chat')
      setSettingsOpen(false)
      setSearchOpen(false)
      setVoiceStartKey((current) => current + 1)
    },
  }

  function handleNavClick(item) {
    if (item.id !== 'chat') setChatsPanelOpen(false)
    if (item.id === 'search') {
      setView('chat')
      setSearchOpen(true)
      return
    }
    if (item.id === 'chat') {
      if (view !== 'chat') {
        setView('chat')
        setChatsPanelOpen(true)
      } else setChatsPanelOpen((open) => !open)
      return
    }
    setView(item.action || item.id)
  }

  const currentSession = data.sessions.find((item) => item.id === data.sessionId)
  const viewLabel = view === 'chat'
    ? (currentSession?.kind === 'main' ? '主聊天' : currentSession?.title || '聊天')
    : ({ missions: '目标', library: '资源库', activity: '动态', ideas: '点子' })[view]

  return (
    <div className={`app-shell ${resolvedTheme === 'light' ? 'theme-light' : ''} muse-shell font-${settings.fontSize} view-${view} ${view === 'chat' && panelOpen ? 'panel-open' : ''} ${chatsPanelOpen ? 'chats-open' : ''}`} style={{ '--mc-user': settings.accent }}>
      {updateAvailable && <div className="app-update-banner" role="status"><span>Luma 已更新</span><button type="button" onClick={() => window.location.reload()}>刷新</button><button type="button" className="app-update-close" aria-label="关闭更新提示" onClick={() => setUpdateAvailable(false)}>×</button></div>}
      {serviceUnavailable && <div className="app-service-banner" role="alert"><span>服务暂时不可用，将自动重试</span><button type="button" onClick={() => setServiceUnavailable(false)}>知道了</button></div>}
      {loadError && <div className="app-error-banner" role="alert"><span>{loadError}</span><button type="button" onClick={() => window.location.reload()}>重试</button></div>}
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark"><LumaLogo label="Luma" /></div><div><span>personal operating system</span></div></div>
        <nav className="nav" aria-label="主导航">
          {navItems.map((item) => <button className={`nav-item ${view === item.id ? 'active' : ''}`} key={item.id} aria-label={item.label} aria-current={view === item.id ? 'page' : undefined} data-tip={item.label} data-icon={item.icon} onClick={() => handleNavClick(item)}><Icon name={item.icon} /><span className="nav-label">{item.label}</span><em>{item.id === 'missions' ? pendingTasks.length : ''}</em></button>)}
        </nav>
        <div className="sidebar-bottom"><div className="privacy-chip"><span className="pulse" /><div><strong>{offline ? '连接失败' : '已同步'}</strong><small>{offline ? '服务暂时不可用' : '所有设备共用此空间'}</small></div></div><div className="profile"><button className="avatar" aria-label="账户与设置" data-tip={accountName(account)} onClick={openSettings}>{account?.avatar_url ? <img src={account.avatar_url} alt="" /> : accountInitial(account)}</button><div><strong>{accountName(account)}</strong><small>个人空间</small></div><span className="dots"><Icon name="more-horizontal" size={18} /></span></div><button className="rail-settings" aria-label="设置" data-tip="设置" data-icon="menu" onClick={openSettings}><Icon name="menu" /></button></div>
      </aside>

      <main className={`main-panel ${view === 'chat' && hasConversation ? 'chat-active' : ''}`}>
        <header className="topbar chat-topbar">
          <div className="chat-heading-controls">
            {view === 'chat' && currentSession?.kind === 'side' && <button type="button" className="chat-back-btn" title="返回主聊天" aria-label="返回主聊天" onClick={returnToMainChat}><Icon name="arrow-left" /></button>}
            <div className="chat-menu-pill"><button aria-label={chatsPanelOpen ? '关闭聊天面板' : '打开聊天面板'} onClick={() => setChatsPanelOpen((open) => !open)}><Icon name="menu" /></button><span>{viewLabel}</span></div>
            {view === 'chat' && data.sessions.some((session) => session.kind === 'main' && completedSessions.has(session.id)) && <button type="button" className="main-chat-completed" aria-label="主聊天已完成回复" title="主聊天已完成回复" onClick={() => openSession(data.sessions.find((session) => session.kind === 'main').id)}><span className="session-completed-dot" /></button>}
          </div>
          <div className="topbar-logo"><LumaLogo label="Luma" /></div>
          <div className="top-actions">{view === 'chat' && !panelOpen && <button className="icon-btn panel-toggle" title="打开动态面板" aria-label="打开动态面板" onClick={() => setPanelOpen(true)}><Icon name="spark" /></button>}</div>
        </header>
        {view === 'chat' && <ChatView data={data} pendingTasks={pendingTasks} input={input} setInput={setInput} composerResetKey={composerResetKey} voiceStartKey={voiceStartKey} onVoiceStartConsumed={() => setVoiceStartKey(0)} sendMessage={sendMessage} sentMessageVersion={sentMessageVersion} widgetPending={widgetPendingSessions.has(data.sessionId)} sending={sending} offline={offline} onWidgetEvent={onWidgetEvent} onRuntimeApproval={decideRuntimeApproval} notify={notify} setView={setView} enterToSend={settings.enterToSend} voiceRaw={settings.voiceRaw} hasConversation={hasConversation} files={files} uploading={uploading} onUploadFile={uploadFile} onRemoveFile={removeFile} onCancelMessage={cancelMessage} onRetryMessage={retryMessage} onLoadOlder={loadOlderMessages} hasMoreMessages={hasMoreMessages} loadingOlder={loadingOlder} highlightedMessageId={highlightedMessageId} />}
        {view === 'missions' && <MissionsView goals={data.goals} tasks={data.tasks} runtimeJobs={data.runtimeJobs} approvals={data.approvals} routines={data.routines} addTask={addTask} startRuntimeJob={startRuntimeJob} decideRuntimeApproval={decideRuntimeApproval} onCreateRoutine={createRoutine} onUpdateRoutine={updateRoutine} onDeleteRoutine={deleteRoutine} onRunRoutine={runRoutine} />}
        {view === 'library' && <LibraryPage request={request} requestBlob={requestBlob} notify={notify} onOpenSession={openContentSession} />}
        {view === 'ideas' && <IdeasPage request={request} notify={notify} onOpenSession={openContentSession} onStart={startIdeaChat} />}
        {view === 'activity' && <FeedPage request={request} notify={notify} onOpenSession={openContentSession} />}
      </main>
      {chatsPanelOpen && <ChatsPanel sessions={data.sessions} generatingBySession={generatingBySession} completedSessions={completedSessions} currentSessionId={data.sessionId} onOpenSession={openSession} onOpenSearch={() => setSearchOpen(true)} onCreateSideChat={startNewChat} onRenameSession={renameSession} onDeleteSession={deleteSession} onClose={() => setChatsPanelOpen(false)} mobile />}
      {view === 'chat' && panelOpen && <ContextPanel data={data} pendingTasks={pendingTasks} offline={offline} onClose={() => setPanelOpen(false)} decideRuntimeApproval={decideRuntimeApproval} />}
      <div className={`toast ${toast ? 'show' : ''}`}>{toast}</div>
      <SearchPalette open={searchOpen} sessions={data.sessions} onClose={() => setSearchOpen(false)} onOpenSession={openSession} onOpenMessage={openSearchMessage} requestSearch={requestSearch} />
      {settingsOpen && <SettingsModal settings={settings} setSettings={setSettings} account={account} onProfileUpdated={setAccount} provider={provider} offline={offline} data={data} pendingTasks={pendingTasks} panelOpen={panelOpen} setPanelOpen={setPanelOpen} notify={notify} onOpenSession={openSession} onOpenAdmin={openAdmin} addMemory={addMemory} removeMemory={removeMemory} confirmMemory={confirmMemory} toggleMemoryPinned={toggleMemoryPinned} onClose={() => setSettingsOpen(false)} onRefresh={loadProviderStatus} />}
    </div>
  )
}

function ChatView({ data, pendingTasks, input, setInput, composerResetKey, voiceStartKey, onVoiceStartConsumed, sendMessage, sentMessageVersion, widgetPending, sending, offline, onWidgetEvent, onRuntimeApproval, notify, setView, enterToSend, voiceRaw, hasConversation, files, uploading, onUploadFile, onRemoveFile, onLoadOlder, hasMoreMessages = false, loadingOlder = false, onCancelMessage, onRetryMessage, highlightedMessageId = '' }) {
  const messagesRef = useRef(null)
  const fileInputRef = useRef(null)
  const scrollAnchorRef = useRef(null)
  const composerRef = useRef(null)
  const followerRef = useRef(null)
  const sentVersionRef = useRef(sentMessageVersion)
  const loadingOlderRef = useRef(loadingOlder)
  loadingOlderRef.current = loadingOlder

  useEffect(() => {
    const follower = createBottomFollower(() => loadingOlderRef.current || scrollAnchorRef.current ? null : messagesRef.current, {
      requestFrame: (callback) => window.requestAnimationFrame(callback),
      cancelFrame: (id) => window.cancelAnimationFrame(id),
      setDelay: (callback, delay) => window.setTimeout(callback, delay),
      clearDelay: (id) => window.clearTimeout(id),
    })
    followerRef.current = follower
    function onResize() {
      const height = window.visualViewport?.height || window.innerHeight
      document.documentElement.style.setProperty('--workspace-viewport-height', `${height}px`)
      follower.schedule()
    }
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => follower.schedule())
    if (messagesRef.current) observer?.observe(messagesRef.current)
    if (composerRef.current) observer?.observe(composerRef.current)
    const column = messagesRef.current?.parentElement
    column?.addEventListener('transitionend', onResize)
    window.addEventListener('resize', onResize)
    window.visualViewport?.addEventListener('resize', onResize)
    onResize()
    return () => {
      observer?.disconnect()
      follower.dispose()
      column?.removeEventListener('transitionend', onResize)
      window.removeEventListener('resize', onResize)
      window.visualViewport?.removeEventListener('resize', onResize)
      document.documentElement.style.removeProperty('--workspace-viewport-height')
    }
  }, [])

  useEffect(() => {
    followerRef.current?.reset()
    followerRef.current?.schedule(true)
  }, [data.sessionId])

  useEffect(() => {
    const justSent = sentVersionRef.current !== sentMessageVersion
    sentVersionRef.current = sentMessageVersion
    followerRef.current?.schedule(justSent)
  }, [data.messages, sentMessageVersion, loadingOlder])

  function handleScroll(event) {
    const element = event.currentTarget
    followerRef.current?.updatePosition()
    if (element.scrollTop > 48 || !hasMoreMessages || loadingOlder || !onLoadOlder) return
    const previousHeight = element.scrollHeight
    const previousTop = element.scrollTop
    scrollAnchorRef.current = { previousHeight, previousTop }
    Promise.resolve(onLoadOlder()).finally(() => {
      requestAnimationFrame(() => {
        const anchor = scrollAnchorRef.current
        if (!anchor || !messagesRef.current) return
        const current = messagesRef.current
        current.scrollTop = anchor.previousTop + (current.scrollHeight - anchor.previousHeight)
        scrollAnchorRef.current = null
      })
    })
  }

  // Like Muse, time lives in centered separators between bursts of
  // conversation rather than inside every bubble.
  const timeline = useMemo(() => {
    let previous = null
    return data.messages.map((message, index) => {
      const date = parseDate(message.created_at)
      let label = ''
      if (date) {
        if (!previous || date - previous > 10 * 60 * 1000 || dayLabel(date) !== dayLabel(previous)) label = separatorLabel(date)
        previous = date
      } else if (index === 0 && message.created_at) {
        label = formatTime(message.created_at)
      }
      return { message, label }
    })
  }, [data.messages])
  const activeStreamingMessage = [...data.messages].reverse().find((message) => message.role === 'assistant' && isGeneratingMessage(message))
  const generating = Boolean(sending || activeStreamingMessage)
  const canSend = !widgetPending && Boolean(generating || input.trim() || files.length > 0)
  const handleSendClick = generating ? () => onCancelMessage?.(activeStreamingMessage) : undefined
  return <section className={`dashboard muse-chat-view ${hasConversation ? 'conversation-started' : ''}`}>
    <div className={`chat-layout ${hasConversation ? 'active-chat' : ''}`}><div className="chat-column"><div className="messages" ref={messagesRef} onScroll={handleScroll}>{loadingOlder && <div className="messages-loading-older" role="status">正在加载更早的消息…</div>}{!timeline.length && <div className="empty-state chat-empty-state"><LumaLogo className="empty-state-logo" /><h4>暂无消息</h4><p>发送一条消息，开始和 Luma 对话。</p></div>}{timeline.map(({ message, label }, index) => <React.Fragment key={message.id}>{label && <div className="chat-time">{label}</div>}<Message message={message} highlighted={message.id === highlightedMessageId} onWidgetEvent={onWidgetEvent} onRuntimeApproval={onRuntimeApproval} disabled={sending || widgetPending} offline={offline} notify={notify} onRetry={onRetryMessage ? () => onRetryMessage(message, timeline[index - 1]?.message) : undefined} /></React.Fragment>)}</div><div className="composer-wrap" ref={composerRef}><div className="composer-files" aria-label="待发送文件">{files.map((file) => <span className="composer-file" key={file.id}>{file.filename}<button type="button" onClick={() => onRemoveFile(file.id)} aria-label={`移除 ${file.filename}`}><Icon name="close" size={15} /></button></span>)}</div><Composer input={input} setInput={setInput} resetKey={composerResetKey} voiceStartKey={voiceStartKey} onVoiceStartConsumed={onVoiceStartConsumed} sendMessage={sendMessage} enterToSend={enterToSend} canSend={canSend} generating={generating} onCancelMessage={handleSendClick} onAttach={() => fileInputRef.current?.click()} sessionId={data.sessionId} voiceRaw={voiceRaw} request={request} notify={notify} errorDetail={errorDetail} /><input ref={fileInputRef} type="file" hidden multiple onChange={(event) => { Array.from(event.target.files || []).forEach(onUploadFile); event.target.value = '' }} /></div></div></div>
  </section>
}

const SETTINGS_SECTIONS = [
  { id: 'general', label: '通用', icon: 'settings' },
  { id: 'account', label: '账号', icon: 'screen-user' },
  { id: 'chat', label: '对话', icon: 'chat' },
  { id: 'proactive', label: '主动消息', icon: 'spark' },
  { id: 'memory', label: '记忆', icon: 'library' },
  { id: 'usage', label: '用量', icon: 'activity' },
  { id: 'notifications', label: '通知', icon: 'bell' },
  { id: 'connectors', label: '连接器', icon: 'grid' },
  { id: 'permissions', label: '权限', icon: 'shield' },
  { id: 'sandbox', label: '沙箱工作区', icon: 'devices' },
  { id: 'data', label: '数据控制', icon: 'lock' },
  { id: 'admin', label: '后台管理', icon: 'shield', adminOnly: true },
  { id: 'help', label: '帮助与关于', icon: 'help' },
]

const THEME_MODES = [
  { value: 'light', label: '浅色', icon: 'sun' },
  { value: 'dark', label: '深色', icon: 'moon' },
  { value: 'system', label: '跟随系统', icon: 'monitor' },
]

function notificationPermission() {
  return typeof Notification === 'undefined' ? 'unsupported' : Notification.permission
}

function SettingRow({ title, hint, children }) {
  return <div className="ms-row"><div className="ms-row-text"><strong>{title}</strong>{hint && <small>{hint}</small>}</div><div className="ms-row-control">{children}</div></div>
}

function Switch({ checked, onChange, label }) {
  return <button type="button" role="switch" aria-checked={checked} aria-label={label} className={`ms-switch ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}><i /></button>
}

function StatusDot({ ok, pending }) {
  return <span className={`ms-dot ${pending ? 'pending' : ok ? 'ok' : 'off'}`} />
}

function errorDetail(error, fallback = '操作失败') {
  let detail = error?.message || fallback
  try { const parsed = JSON.parse(detail); detail = parsed.detail || parsed.message || detail } catch { /* plain text */ }
  return detail
}

function endpointHost(endpoint) {
  try { return new URL(endpoint).hostname } catch { return endpoint || '未知主机' }
}

function HeaderEditor({ rows, setRows, password = false }) {
  return <div className="mcp-header-editor">{rows.map((row, index) => <div className="mcp-header-row" key={`${index}-${row.key}`}><input value={row.key} placeholder="请求头名称" onChange={(event) => setRows((items) => items.map((item, i) => i === index ? { ...item, key: event.target.value } : item))} /><input type={password && index === 0 ? 'password' : 'text'} value={row.value} placeholder="值" onChange={(event) => setRows((items) => items.map((item, i) => i === index ? { ...item, value: event.target.value } : item))} /><button type="button" className="mcp-remove-header" aria-label="移除请求头" onClick={() => setRows((items) => items.length > 1 ? items.filter((_, i) => i !== index) : items)}><Icon name="close" size={14} /></button></div>)}<button type="button" className="ms-btn mcp-add-header" onClick={() => setRows((items) => [...items, { key: '', value: '' }])}>添加请求头</button></div>
}

function rowsToHeaders(rows) {
  return Object.fromEntries(rows.filter((item) => item.key.trim()).map((item) => [item.key.trim(), item.value]))
}

function McpAddForm({ onAdded, notify, busy, setBusy }) {
  const [mode, setMode] = useState('fill')
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [headers, setHeaders] = useState([{ key: 'Authorization', value: '' }])
  const [config, setConfig] = useState('')
  const [errors, setErrors] = useState([])
  async function submit(event) {
    event.preventDefault()
    setBusy('add')
    try {
      const body = mode === 'json' ? { config } : { name: name.trim() || undefined, url: url.trim(), headers: rowsToHeaders(headers) }
      const result = await request('/connectors/mcp', { method: 'POST', body: JSON.stringify(body) })
      const added = result?.connectors || []
      setErrors(result?.errors || [])
      if (added.length) notify(`已添加 ${added[0].name}（${(added[0].metadata?.tools || []).length || (added[0].capabilities || []).length} 个工具）`)
      if (result?.errors?.length) notify(result.errors.map((item) => `${item.name || 'MCP'}：${item.detail}`).join('；'))
      onAdded(!result?.errors?.length)
      setName(''); setUrl(''); setConfig(''); setHeaders([{ key: 'Authorization', value: '' }])
    } catch (error) { const detail = errorDetail(error, '添加 MCP 失败'); setErrors([{ name: 'MCP', detail }]); notify(detail) } finally { setBusy('') }
  }
  return <form className="mcp-add-form" onSubmit={submit}><div className="ms-segment" role="tablist"><button type="button" className={mode === 'fill' ? 'active' : ''} onClick={() => setMode('fill')}>填写</button><button type="button" className={mode === 'json' ? 'active' : ''} onClick={() => setMode('json')}>粘贴 JSON</button></div>{errors.length > 0 && <ul className="mcp-form-errors">{errors.map((item, index) => <li key={`${item.name}-${index}`}>{item.name ? `${item.name}：` : ''}{item.detail}</li>)}</ul>}{mode === 'fill' ? <><label>名称（可选）<input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：我的数据服务" /></label><label>URL<input required type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://example.com/mcp" /></label><label>请求头<HeaderEditor rows={headers} setRows={setHeaders} password /></label></> : <label>配置 JSON<textarea required value={config} onChange={(event) => setConfig(event.target.value)} placeholder={'{\n  "mcpServers": {\n    "my-data": {\n      "url": "https://example.com/mcp",\n      "headers": { "Authorization": "Bearer <你的令牌>" }\n    }\n  }\n}'} rows={8} /></label>}<div className="mcp-form-actions"><button type="button" className="ms-btn" onClick={() => onAdded(true)}>取消</button><button type="submit" className="ms-btn mcp-primary" disabled={busy === 'add'}>{busy === 'add' ? '正在连接…' : '添加 MCP'}</button></div></form>
}

function McpConnectorRow({ connector, toggleConnector, notify, refresh }) {
  const [expanded, setExpanded] = useState(false)
  const [editingHeaders, setEditingHeaders] = useState(false)
  const [rows, setRows] = useState([{ key: 'Authorization', value: '' }])
  const [busy, setBusy] = useState('')
  const metadata = connector.metadata || {}
  const tools = Array.isArray(metadata.tools) ? metadata.tools : []
  async function patchMcp(body) {
    setBusy('patch')
    try { const updated = await request(`/connectors/${connector.id}/mcp`, { method: 'PATCH', body: JSON.stringify(body) }); refresh(updated); notify('连接器已更新') } catch (error) { notify(errorDetail(error)) } finally { setBusy('') }
  }
  async function sync() {
    setBusy('sync')
    try { const updated = await request(`/connectors/${connector.id}/sync`, { method: 'POST' }); refresh(updated); notify(updated?.metadata?.status === 'error' ? updated.metadata.last_error : '已重新同步工具') } catch (error) { notify(errorDetail(error, '同步失败')) } finally { setBusy('') }
  }
  async function saveHeaders(event) {
    event.preventDefault()
    await patchMcp({ headers: rowsToHeaders(rows) })
    setEditingHeaders(false)
  }
  async function remove() {
    if (!window.confirm(`删除连接器「${connector.name}」？`)) return
    setBusy('delete')
    try { await request(`/connectors/${connector.id}`, { method: 'DELETE' }); refresh({ id: connector.id, _deleted: true }); notify('连接器已删除') } catch (error) { notify(errorDetail(error, '删除失败')) } finally { setBusy('') }
  }
  return <div className={`mcp-connector ${expanded ? 'expanded' : ''}`}><div className="mcp-connector-head"><button type="button" className="mcp-connector-toggle" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}><span className="mcp-chevron">{expanded ? '⌄' : '›'}</span><span className="mcp-connector-copy"><strong>{connector.name}</strong><small>{endpointHost(connector.endpoint)} · {tools.length || (connector.capabilities || []).length} 个工具</small></span><StatusDot ok={metadata.status === 'ok'} pending={metadata.status === 'pending'} /></button><Switch label={`${connector.name} 启用`} checked={connector.enabled} onChange={(enabled) => toggleConnector(connector, enabled)} /></div>{metadata.status === 'error' && metadata.last_error && <p className="mcp-error-text">{metadata.last_error}</p>}{expanded && <div className="mcp-connector-detail"><div className="mcp-detail-label">请求头提示</div><div className="mcp-hints">{Object.entries(metadata.header_hints || {}).length ? Object.entries(metadata.header_hints).map(([key, value]) => <div key={key}><span>{key}</span><code>{value}</code></div>) : <span>未设置请求头</span>}</div><div className="mcp-detail-label">工具</div><div className="mcp-tools">{tools.length ? tools.map((tool) => <div className="mcp-tool-row" key={tool.name}><span className="mcp-tool-copy"><strong>{tool.title || tool.name}</strong><small>{tool.description || '无描述'}</small></span>{!tool.read_only && <em>需确认</em>}<Switch label={`${tool.title || tool.name} 启用`} checked={tool.enabled !== false} onChange={(enabled) => patchMcp({ tools: { [tool.name]: enabled } })} /></div>) : <small className="mcp-empty">暂无工具</small>}</div><div className="mcp-detail-actions"><button type="button" className="ms-btn" disabled={busy === 'sync'} onClick={sync}>{busy === 'sync' ? '同步中…' : '重新同步'}</button><button type="button" className="ms-btn" onClick={() => { setEditingHeaders((value) => !value); setRows([{ key: 'Authorization', value: '' }]) }}>更新令牌</button><button type="button" className="ms-btn mcp-danger" disabled={busy === 'delete'} onClick={remove}>删除</button></div>{editingHeaders && <form className="mcp-token-form" onSubmit={saveHeaders}><p>只填写需要替换的请求头，提交后整体更新。</p><HeaderEditor rows={rows} setRows={setRows} password /><div className="mcp-form-actions"><button type="submit" className="ms-btn mcp-primary" disabled={busy === 'patch'}>{busy === 'patch' ? '保存中…' : '保存令牌'}</button></div></form>}</div>}</div>
}

function SettingsModal({ settings, setSettings, account, onProfileUpdated, provider, offline, data, pendingTasks, panelOpen, setPanelOpen, notify, onOpenSession, onOpenAdmin, addMemory, removeMemory, confirmMemory, toggleMemoryPinned, onClose, onRefresh }) {
  const [section, setSection] = useState('general')
  const isAdmin = account?.role === 'admin'
  const [permission, setPermission] = useState(notificationPermission)
  const [system, setSystem] = useState(null)
  const [sandbox, setSandbox] = useState(undefined)
  const [connectors, setConnectors] = useState(null)
  const [autoMemory, setAutoMemory] = useState(true)
  const [busy, setBusy] = useState('')
  const [mcpAddOpen, setMcpAddOpen] = useState(false)
  const update = (patch) => setSettings((current) => ({ ...current, ...patch }))

  useEffect(() => {
    function onKeyDown(event) { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  useEffect(() => {
    let active = true
    const settle = (setter, fallback) => [(value) => { if (active) setter(value) }, () => { if (active) setter(fallback) }]
    if (section === 'connectors') {
      onRefresh()
      request('/system/status').then(...settle(setSystem, {}))
      request('/runtime/sandbox').then(...settle(setSandbox, null))
      request('/connectors').then(...settle(setConnectors, []))
    }
    return () => { active = false }
  }, [section])

  useEffect(() => {
    let active = true
    request('/memories/settings').then((result) => { if (active) setAutoMemory(result?.auto_extract !== false) }).catch(() => {})
    return () => { active = false }
  }, [])

  async function toggleNotifications(next) {
    if (next && permission === 'unsupported') { notify('当前浏览器不支持桌面通知'); return }
    if (next && permission === 'default') {
      const result = await Notification.requestPermission()
      setPermission(result)
      if (result !== 'granted') { notify('浏览器未允许通知'); return }
    }
    update({ notifications: next })
  }

  async function toggleAutoMemory(next) {
    const previous = autoMemory
    setAutoMemory(next)
    try {
      const result = await request('/memories/settings', { method: 'PATCH', body: JSON.stringify({ auto_extract: next }) })
      setAutoMemory(result?.auto_extract !== false)
      notify(next ? '自动记忆已开启' : '自动记忆已关闭')
    } catch (error) {
      setAutoMemory(previous)
      notify(errorDetail(error, '自动记忆设置失败'))
    }
  }

  function testNotification() {
    if (permission !== 'granted') { notify('请先在浏览器中允许 Luma 发送通知'); return }
    new Notification('Luma', { body: '桌面通知已开启，回复完成时会在这里提醒你。' })
  }

  async function releaseSandbox() {
    setBusy('sandbox')
    try {
      await request('/runtime/sandbox', { method: 'DELETE' })
      setSandbox(null)
      notify('沙箱已释放')
    } catch (error) {
      notify(error.message || '释放失败')
    } finally {
      setBusy('')
    }
  }

  async function toggleConnector(connector, enabled) {
    setConnectors((items) => items.map((item) => item.id === connector.id ? { ...item, enabled } : item))
    try {
      await request(`/connectors/${connector.id}`, { method: 'PATCH', body: JSON.stringify({ enabled }) })
    } catch (error) {
      setConnectors((items) => items.map((item) => item.id === connector.id ? { ...item, enabled: !enabled } : item))
      notify(error.message || '更新失败')
    }
  }

  function updateConnector(updated) {
    if (updated?._deleted) { setConnectors((items) => items.filter((item) => item.id !== updated.id)); return }
    setConnectors((items) => items.map((item) => item.id === updated.id ? updated : item))
  }

  async function exportData() {
    setBusy('export')
    try {
      const snapshot = await request('/export')
      const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `luma-export-${new Date().toISOString().slice(0, 10)}.json`
      link.click()
      URL.revokeObjectURL(url)
    } catch (error) {
      notify(error.message || '导出失败')
    } finally {
      setBusy('')
    }
  }

  function resetPreferences() {
    setSettings((current) => ({ ...current, theme: 'light', notifications: true, enterToSend: true, voiceRaw: false, accent: ACCENTS[0].value, fontSize: 'standard' }))
    notify('偏好已恢复默认')
  }

  function clearAuthentication() {
    sessionStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY)
    localStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY)
    window.dispatchEvent(new CustomEvent('luma-auth-required'))
    navigate('/login', { replace: true })
  }

  async function logout() {
    try { await request('/auth/logout', { method: 'POST' }) } catch { /* the local token is cleared either way */ }
    clearAuthentication()
  }

  const sections = SETTINGS_SECTIONS.filter((item) => !item.adminOnly || isAdmin)
  const current = sections.find((item) => item.id === section) || sections[0]
  const unread = (data.notifications || []).length
  const usage = [
    { label: '旁聊', value: (data.sessions || []).filter((item) => item.kind === 'side').length, unit: '个' },
    { label: '记忆', value: data.memories.length, unit: '条' },
    { label: '待办任务', value: pendingTasks.length, unit: '项' },
    { label: '目标', value: (data.goals || []).length, unit: '个' },
  ]
  const providerOnline = provider?.configured && !offline
  const idleMinutes = Math.round((system?.agent_runtime?.idle_ttl_seconds || 300) / 60)

  const panes = {
    general: <>
      <div className="ms-account">
        <span className="ms-avatar">{account?.avatar_url ? <img src={account.avatar_url} alt="" /> : accountInitial(account)}</span>
        <div><strong>{accountName(account)}</strong><small>{account?.username || account?.email || '个人空间'}</small></div>
      </div>
      <h4>使用情况</h4>
      <div className="ms-usage">{usage.map((item) => <div className="ms-usage-card" key={item.label}><small>{item.label}</small><strong>{item.value}<em>{item.unit}</em></strong><span className="ms-bar"><i style={{ width: `${Math.min(100, item.value * 4)}%` }} /></span></div>)}</div>
      <div className="ms-group">
        <SettingRow title="语言"><span className="ms-value">简体中文</span></SettingRow>
      </div>
      <h4>外观</h4>
      <div className="ms-group">
        <SettingRow title="模式"><div className="ms-segment" role="radiogroup" aria-label="外观模式">{THEME_MODES.map((mode) => <button type="button" key={mode.value} role="radio" aria-checked={settings.theme === mode.value} aria-label={mode.label} title={mode.label} className={settings.theme === mode.value ? 'active' : ''} onClick={() => update({ theme: mode.value })}><Icon name={mode.icon} size={17} /></button>)}</div></SettingRow>
        <SettingRow title="主题颜色"><div className="ms-swatches" role="radiogroup" aria-label="主题颜色">{ACCENTS.map((accent) => <button type="button" key={accent.value} role="radio" aria-checked={settings.accent === accent.value} aria-label={accent.label} title={accent.label} className={settings.accent === accent.value ? 'active' : ''} style={{ '--swatch': accent.value }} onClick={() => update({ accent: accent.value })} />)}</div></SettingRow>
        <SettingRow title="字号"><div className="ms-segment text" role="radiogroup" aria-label="字号">{[['standard', '标准'], ['large', '较大']].map(([value, label]) => <button type="button" key={value} role="radio" aria-checked={settings.fontSize === value} className={settings.fontSize === value ? 'active' : ''} onClick={() => update({ fontSize: value })}>{label}</button>)}</div></SettingRow>
      </div>
    </>,
    account: <AccountSettings account={account} request={request} notify={notify} onProfileUpdated={onProfileUpdated} onSignedOut={clearAuthentication} />,
    chat: <div className="ms-group">
      <SettingRow title="Enter 发送" hint="关闭后 Enter 换行，点击发送按钮提交"><Switch label="Enter 发送" checked={settings.enterToSend} onChange={(enterToSend) => update({ enterToSend })} /></SettingRow>
      <SettingRow title="语音输入：原话模式" hint="不做智能整理，保留识别原文"><Switch label="语音输入：原话模式（不做智能整理）" checked={settings.voiceRaw} onChange={(voiceRaw) => update({ voiceRaw })} /></SettingRow>
      <SettingRow title="显示右侧动态面板" hint="在对话旁展示任务、记忆和最近动态"><Switch label="显示右侧动态面板" checked={panelOpen} onChange={setPanelOpen} /></SettingRow>
      <SettingRow title="多端同步" hint="网页、桌面端和手机共用同一个对话空间"><span className="ms-value"><StatusDot ok={!offline} />{offline ? '离线' : '已同步'}</span></SettingRow>
    </div>,
    notifications: <>
      <div className="ms-group">
        <SettingRow title="桌面通知" hint="窗口不在前台时，Luma 回复完成会提醒你"><Switch label="桌面通知" checked={settings.notifications && permission === 'granted'} onChange={toggleNotifications} /></SettingRow>
        <SettingRow title="浏览器权限" hint={permission === 'denied' ? '已被拒绝，请在浏览器地址栏的网站设置中重新允许' : undefined}><span className="ms-value"><StatusDot ok={permission === 'granted'} pending={permission === 'default'} />{{ granted: '已允许', denied: '已拒绝', default: '未询问', unsupported: '不支持' }[permission]}</span></SettingRow>
        <SettingRow title="发送测试通知"><button type="button" className="ms-btn" onClick={testNotification}>测试</button></SettingRow>
      </div>
      <div className="ms-group">
        <SettingRow title="未读提醒" hint="任务和后台作业产生的站内提醒"><span className="ms-value">{unread} 条</span></SettingRow>
      </div>
    </>,
    proactive: <ProactiveSettings request={request} notify={notify} />,
    memory: <>
      <div className="ms-group"><SettingRow title="自动记忆" hint="从对话中提取偏好和事实，推断内容可在下方确认"><Switch label="自动记忆" checked={autoMemory} onChange={toggleAutoMemory} /></SettingRow></div>
      <MemoryView memories={data.memories} addMemory={addMemory} removeMemory={removeMemory} confirmMemory={confirmMemory} toggleMemoryPinned={toggleMemoryPinned} />
    </>,
    usage: <UsagePanel request={request} onOpenSession={(id) => { onClose(); onOpenSession(id) }} />,
    permissions: <PermissionsPanel request={request} notify={notify} />,
    sandbox: <SandboxWorkspacePanel request={request} notify={notify} />,
    connectors: <>
      <div className="mcp-section-heading"><h4>MCP 连接器</h4><button type="button" className="ms-btn" onClick={() => setMcpAddOpen((value) => !value)}>{mcpAddOpen ? '收起' : '添加 MCP'}</button></div>
      {mcpAddOpen && <McpAddForm notify={notify} busy={busy} setBusy={setBusy} onAdded={(close = true) => { if (close) setMcpAddOpen(false); request('/connectors').then(setConnectors).catch(() => {}) }} />}
      <div className="ms-group mcp-list">
        {connectors === null ? <SettingRow title="正在加载…" /> : connectors.filter((connector) => connector.kind === 'mcp').length ? connectors.filter((connector) => connector.kind === 'mcp').map((connector) => <McpConnectorRow key={connector.id} connector={connector} toggleConnector={toggleConnector} notify={notify} refresh={updateConnector} />) : <SettingRow title="还没有 MCP 连接器" hint="添加远程 MCP 服务后，Luma 可以按需查询工具" />}
      </div>
      <h4>模型服务</h4>
      <div className="ms-group">
        <SettingRow title={providerOnline ? '模型已连接' : offline ? '服务暂时不可用' : provider ? '模型未配置' : '正在检查…'} hint={provider?.model || '兼容 OpenAI 协议的模型服务'}><span className="ms-value"><StatusDot ok={providerOnline} pending={!provider} /><button type="button" className="ms-btn" onClick={onRefresh}>刷新</button></span></SettingRow>
      </div>
      <h4>Agent 沙箱</h4>
      <div className="ms-group">
        <SettingRow title={sandbox === undefined ? '正在检查…' : sandbox && sandbox.status === 'running' ? '沙箱运行中' : '当前没有运行中的沙箱'} hint={`执行代码或浏览网页时按需启动，空闲 ${idleMinutes} 分钟后自动释放`}>{sandbox && sandbox.status === 'running' ? <button type="button" className="ms-btn" disabled={busy === 'sandbox'} onClick={releaseSandbox}>{busy === 'sandbox' ? '释放中…' : '立即释放'}</button> : <span className="ms-value"><StatusDot ok={false} pending={sandbox === undefined} />空闲</span>}</SettingRow>
      </div>
      <h4>服务状态</h4>
      <div className="ms-group">
        {[['数据库', system?.storage?.reachable], ['缓存（Redis）', system?.redis?.reachable], ['任务执行引擎（Agent Runtime）', system?.agent_runtime?.enabled]].map(([label, ok]) => <SettingRow key={label} title={label}><span className="ms-value"><StatusDot ok={ok} pending={!system} />{!system ? '检查中' : ok ? '正常' : '不可用'}</span></SettingRow>)}
      </div>
      <h4>已添加的连接器</h4>
      <div className="ms-group">
        {connectors === null ? <SettingRow title="正在加载…" /> : connectors.filter((connector) => connector.kind !== 'mcp').length ? connectors.filter((connector) => connector.kind !== 'mcp').map((connector) => <SettingRow key={connector.id} title={connector.name} hint={[connector.kind, ...(connector.capabilities || [])].join(' · ')}><Switch label={connector.name} checked={connector.enabled} onChange={(enabled) => toggleConnector(connector, enabled)} /></SettingRow>) : <SettingRow title="还没有其他连接器" hint="连接邮箱、日历或其他服务后，Luma 可以替你读取和处理" />}
      </div>
      <p className="ms-note">令牌只加密保存在服务端，浏览器和模型都看不到。</p>
    </>,
    data: <>
      <div className="ms-group">
        <SettingRow title="导出全部数据" hint="对话、任务、记忆和目标，导出为 JSON 文件"><button type="button" className="ms-btn" disabled={busy === 'export'} onClick={exportData}><Icon name="download" size={15} />{busy === 'export' ? '导出中…' : '导出'}</button></SettingRow>
        <SettingRow title="记忆" hint={`Luma 记住了 ${data.memories.length} 条关于你的信息`}><button type="button" className="ms-btn" onClick={() => setSection('memory')}>管理</button></SettingRow>
        <SettingRow title="重置偏好" hint="外观、字号和通知恢复为默认，不影响任何数据"><button type="button" className="ms-btn" onClick={resetPreferences}>重置</button></SettingRow>
      </div>
      <p className="ms-note">你的数据存放在你部署的服务器上。模型密钥只保存在服务端。</p>
    </>,
    admin: <div className="ms-group">
      <SettingRow title="打开后台管理" hint="运行实例、用户、对话与请求统计"><button type="button" className="ms-btn" onClick={onOpenAdmin}>打开<Icon name="arrow-up-right" size={15} /></button></SettingRow>
    </div>,
    help: <>
      <div className="ms-group">
        <SettingRow title="版本"><span className="ms-value">Luma {__APP_VERSION__}</span></SettingRow>
        <SettingRow title="服务地址"><span className="ms-value muted">{API_URL.replace(/\/api\/v1$/, '')}</span></SettingRow>
      </div>
      <h4>快捷键</h4>
      <div className="ms-group">
        {[['发送消息', ['Enter']], ['换行', ['Shift', 'Enter']], ['关闭弹窗', ['Esc']]].map(([label, keys]) => <SettingRow key={label} title={label}><span className="ms-keys">{keys.map((key) => <kbd key={key}>{key}</kbd>)}</span></SettingRow>)}
      </div>
    </>,
  }

  return <div className="ms-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section className="ms-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <nav className="ms-nav" aria-label="设置分类">
        <div className="ms-nav-list">{sections.map((item) => <button type="button" key={item.id} className={item.id === current.id ? 'active' : ''} onClick={() => setSection(item.id)}><Icon name={item.icon} size={18} />{item.label}</button>)}</div>
        <button type="button" className="ms-logout" onClick={logout}><Icon name="logout" size={18} />退出登录</button>
      </nav>
      <div className="ms-main">
        <header className="ms-header"><h2 id="settings-title">{current.label}</h2><button type="button" className="ms-close" onClick={onClose} aria-label="关闭设置"><Icon name="close" size={18} /></button></header>
        <div className="ms-body" key={current.id}>{panes[current.id]}</div>
      </div>
    </section>
  </div>
}

const PSEUDO_STRONG = /<\s*(?:tool_call|function_calls|function\s*=\s*luma[^>]*|invoke\s+name\s*=\s*["']?\s*luma[^>]*?)\s*>/ig
const PSEUDO_WEAK_OPEN = /^<\s*(?:parameter\b|invoke\b|function\s*=|tool_call\b|function_calls\b)[^>]*>/i
const PSEUDO_CLOSE_AT_START = /^\s*<\/\s*(?:parameter|invoke|function_calls|tool_call|function)\s*>/i

function fencedRanges(content) {
  const ranges = []
  const fence = /^\s*```([^\r\n]*)(?:\r?\n|$)/gm
  let open = null
  let match
  while ((match = fence.exec(content))) {
    const info = match[1].trim()
    if (!open) {
      open = { start: match.index, luma: /^luma-ui(?:\s|$)/i.test(info) }
    } else if (!info) {
      ranges.push({ start: open.start, end: match.index + match[0].length, luma: open.luma })
      open = null
    }
  }
  if (open) ranges.push({ start: open.start, end: content.length, luma: open.luma })
  return ranges
}

function balancedObjectEnd(content, brace) {
  let depth = 0
  let quote = false
  let escaped = false
  for (let index = brace; index < content.length; index += 1) {
    const char = content[index]
    if (quote) {
      if (escaped) escaped = false
      else if (char === '\\') escaped = true
      else if (char === '"') quote = false
      continue
    }
    if (char === '"') quote = true
    else if (char === '{') depth += 1
    else if (char === '}' && --depth === 0) return index + 1
  }
  return -1
}

function pseudoCloseEnd(content, from) {
  let cursor = from
  let found = false
  while (true) {
    const match = content.slice(cursor).match(PSEUDO_CLOSE_AT_START)
    if (!match) return found ? cursor : -1
    found = true
    cursor += match[0].length
  }
}

function inlineCodeRanges(content) {
  const ranges = []
  let lineStart = 0
  while (lineStart <= content.length) {
    const lineEnd = content.indexOf('\n', lineStart)
    const end = lineEnd < 0 ? content.length : lineEnd
    let open = -1
    for (let index = lineStart; index < end; index += 1) {
      if (content[index] !== '`' || content[index - 1] === '`' || content[index + 1] === '`') continue
      if (open < 0) open = index
      else {
        ranges.push({ start: open, end: index + 1 })
        open = -1
      }
    }
    lineStart = lineEnd < 0 ? content.length + 1 : lineEnd + 1
  }
  return ranges
}

function insideInlineCode(content, index, ranges = inlineCodeRanges(content)) {
  return ranges.some((range) => index >= range.start && index < range.end)
}

function nextStrongMarker(content, from, ranges) {
  PSEUDO_STRONG.lastIndex = from
  let match
  while ((match = PSEUDO_STRONG.exec(content))) {
    if (!insideInlineCode(content, match.index, ranges)) return match
  }
  return null
}

function pseudoAnalysis(content, openEnd) {
  let cursor = openEnd
  while (true) {
    const whitespace = /^\s*/.exec(content.slice(cursor))
    cursor += whitespace[0].length
    const weak = PSEUDO_WEAK_OPEN.exec(content.slice(cursor))
    if (!weak) break
    cursor += weak[0].length
  }
  if (cursor >= content.length) return { kind: 'tail', end: content.length }
  if (content[cursor] !== '{') return { kind: 'text', end: openEnd }
  const jsonEnd = balancedObjectEnd(content, cursor)
  if (jsonEnd < 0) return { kind: 'truncated', end: content.length }
  const closeEnd = pseudoCloseEnd(content, jsonEnd)
  return { kind: 'pseudo', end: closeEnd >= 0 ? closeEnd : jsonEnd }
}

function trailingStrongPrefix(text) {
  const start = text.lastIndexOf('<')
  if (start < 0) return ''
  if (insideInlineCode(text, start)) return ''
  const suffix = text.slice(start)
  if (/^<\s*tool(?:[_a-z]*)?\s*$/i.test(suffix)) return suffix
  if (/^<\s*function(?:[_=\s\w-]*)?$/i.test(suffix)) return suffix
  if (/^<\s*invoke\s+name(?:\s*=\s*["']?\s*luma[\w-]*)?$/i.test(suffix)) return suffix
  return ''
}

function assistantParts(message) {
  // Keep an empty streaming response empty so Message can render the typing
  // indicator without duplicating a textual "正在生成…" status.
  const content = message.content || ''
  const widgets = Array.isArray(message.metadata?.widgets) ? message.metadata.widgets : []
  const parts = []

  function addText(text, parseMarkers = true) {
    if (!text.trim()) return
    if (!parseMarkers) {
      parts.push({ type: 'text', text })
      return
    }
    const markers = /\[\[widget:([^\]]+)\]\]/g
    let cursor = 0
    let match
    while ((match = markers.exec(text))) {
      if (text.slice(cursor, match.index).trim()) parts.push({ type: 'text', text: text.slice(cursor, match.index) })
      const widget = widgets.find((item) => item.id === match[1])
      if (widget) parts.push({ type: 'widget', widget })
      cursor = markers.lastIndex
    }
    if (text.slice(cursor).trim()) parts.push({ type: 'text', text: text.slice(cursor) })
  }

  function stripPseudo(text) {
    let cursor = 0
    const ranges = inlineCodeRanges(text)
    let match = nextStrongMarker(text, 0, ranges)
    while (match) {
      addText(text.slice(cursor, match.index))
      const analysis = pseudoAnalysis(text, match.index + match[0].length)
      if (analysis.kind === 'text') {
        addText(text.slice(match.index, match.index + match[0].length))
        cursor = match.index + match[0].length
      } else {
        cursor = analysis.end
      }
      if (cursor >= text.length) return
      match = nextStrongMarker(text, cursor, ranges)
    }
    addText(text.slice(cursor))
  }

  function streamText(text) {
    let cursor = 0
    let preservedStrongEnd = -1
    const ranges = inlineCodeRanges(text)
    let match = nextStrongMarker(text, 0, ranges)
    while (match) {
      addText(text.slice(cursor, match.index))
      const analysis = pseudoAnalysis(text, match.index + match[0].length)
      if (analysis.kind === 'text') {
        addText(text.slice(match.index, match.index + match[0].length))
        cursor = match.index + match[0].length
        preservedStrongEnd = cursor
      } else {
        parts.push({ type: 'loading' })
        cursor = analysis.end
      }
      if (cursor >= text.length) return
      match = nextStrongMarker(text, cursor, ranges)
    }
    const rest = text.slice(cursor)
    const hidden = trailingStrongPrefix(rest)
    const hiddenStart = hidden ? rest.length - hidden.length : -1
    const followsPreservedStrong = hidden && preservedStrongEnd >= 0 && text.slice(preservedStrongEnd, cursor + hiddenStart).trim() === ''
    addText(hidden && !followsPreservedStrong ? rest.slice(0, -hidden.length) : rest)
  }

  const streaming = message.status ? message.status === 'streaming' : Boolean(message.streaming || message.metadata?.incomplete)
  const ranges = fencedRanges(content)
  let cursor = 0
  for (const range of ranges) {
    const before = content.slice(cursor, range.start)
    if (streaming) streamText(before)
    else stripPseudo(before)
    if (range.luma && streaming) parts.push({ type: 'loading' })
    else addText(content.slice(range.start, range.end), false)
    cursor = range.end
  }
  const tail = content.slice(cursor)
  if (streaming) streamText(tail)
  else stripPseudo(tail)
  const mergedParts = []
  for (const part of parts) {
    if (part.type === 'text' && !part.text.trim()) continue
    const previous = mergedParts[mergedParts.length - 1]
    if (part.type === 'text' && previous?.type === 'text') previous.text += part.text
    else mergedParts.push(part)
  }
  return mergedParts
}

function Message({ message, onWidgetEvent, onRuntimeApproval, disabled, offline, notify, onRetry, highlighted = false }) {
  const isAssistant = message.role === 'assistant'
  const status = message.status || (message.streaming ? 'streaming' : message.metadata?.incomplete ? 'incomplete' : 'complete')
  const streaming = status === 'streaming'
  const errored = status === 'error'
  const incomplete = status === 'incomplete'
  const parts = isAssistant ? assistantParts({ ...message, streaming }) : []
  const hasWidget = parts.some((part) => part.type !== 'text')
  const persistedToolEvents = [
    ...(Array.isArray(message.metadata?.tool_calls) ? message.metadata.tool_calls : []),
    ...(Array.isArray(message.metadata?.tool_events) ? message.metadata.tool_events : []),
  ]
  const liveToolEvents = Array.isArray(message.toolEvents) ? message.toolEvents : []
  const browserEvents = liveBrowserEvents(liveToolEvents)
  const displayedToolEvents = [...persistedToolEvents.filter((item, index) => !browserEvents.some((live) => toolStatusKey(live) === toolStatusKey(item, index))), ...liveToolEvents].filter((item, index, all) => {
    const key = toolStatusKey(item, index)
    return index === all.findIndex((candidate, candidateIndex) => toolStatusKey(candidate, candidateIndex) === key)
  })
  const toolEvents = isAssistant ? (streaming ? liveToolEvents : displayedToolEvents) : []
  const showTypingIndicator = isAssistant && streaming && !String(message.content || '').trim() && parts.length === 0
  return <div data-message-id={message.id} className={`message ${message.role} ${isAssistant && (message.metadata?.proactive || message.metadata?.feed_post_id) ? 'has-source-labels' : ''} ${highlighted ? 'message-highlighted' : ''} ${streaming ? 'streaming' : ''} ${errored ? 'error' : ''} ${incomplete ? 'incomplete' : ''} ${hasWidget || toolEvents.length ? 'has-widget' : ''} ${message.role === 'user' && message.metadata?.widget_event ? 'widget-reply' : ''}`} aria-busy={streaming || undefined}>
    <div className="message-avatar">{isAssistant ? <LumaLogo /> : <Icon name="screen-user" size={14} />}</div>
    {isAssistant && <ProactiveLabels message={message} />}
    {isAssistant && toolEvents.length > 0 && <ToolStatusRows events={toolEvents} onRuntimeApproval={onRuntimeApproval} />}
    {isAssistant ? (showTypingIndicator
      ? <div className="bubble message-typing" key="typing" role="status" aria-label="正在生成"><span className="typing-dots" aria-hidden="true"><i /><i /><i /></span></div>
      : parts.map((part, index) => part.type === 'widget'
        ? <ChatWidget key={`${part.widget.id}-${index}`} widget={part.widget} onEvent={onWidgetEvent} disabled={disabled} offline={offline} notify={notify} />
        : part.type === 'loading' ? <div className="chat-widget-loading" key={`loading-${index}`} role="status" aria-label="正在生成卡片"><span className="typing-dots" aria-hidden="true"><i /><i /><i /></span></div>
          : <div className="bubble" key={`text-${index}`}><MessageText text={part.text} /></div>))
      : <div className="bubble"><p>{displaySecretReferences(message.content)}</p></div>}
    {isAssistant && errored && <div className="message-status-actions"><span className="message-status-label">{message.metadata?.error || '生成失败'}</span>{onRetry && <button type="button" className="message-action secondary-btn" onClick={() => onRetry(message)}>重试</button>}</div>}
    {isAssistant && incomplete && <div className="message-status-actions"><span className="message-status-label">已中断</span>{onRetry && <button type="button" className="message-action secondary-btn" onClick={() => onRetry(message)}>重试</button>}</div>}
  </div>
}

function ToolStatusRows({ events, onRuntimeApproval }) {
  return <div className="chat-tool-events" aria-label="工具调用状态">{events.map((item, index) => {
    const nestedKind = item.payload && typeof item.payload === 'object'
      ? item.payload.kind
      : item.data && typeof item.data === 'object'
        ? item.data.kind
        : undefined
    if (SANDBOX_TOOL_KINDS.has(item.kind) || SANDBOX_TOOL_KINDS.has(nestedKind)) {
      return <SandboxToolCard key={`${item.call_id || item.job_id || item.file_id || item.kind || 'sandbox'}-${index}`} item={item} apiUrl={API_URL} getAuthHeaders={authHeaders} request={request} />
    }
    const eventType = item.event_type || item.kind || 'tool'
    const connector = String(item.connector || item.connector_name || '连接器')
    const title = String(item.title || item.tool || '工具')
    const status = item.status || (eventType === 'approval' ? 'waiting_approval' : 'running')
    let text = `${connector} · ${title} 调用失败`
    let icon = 'close'
    if (eventType === 'tool_limit') {
      text = String(item.reason || '已达到工具调用上限')
      icon = 'shield'
    } else if (eventType === 'approval' || status === 'waiting_approval') {
      text = `${title} 需要你确认`
      icon = 'shield'
    } else if (eventType === 'widget') {
      text = item.id ? '已生成交互组件' : '正在生成交互组件…'
      icon = item.id ? 'check-circle' : 'loader'
    } else if (status === 'running') {
      text = browserProgressLabel(item.tool || title) || `正在查询 ${connector} · ${title}…`
      icon = 'loader'
    } else if (status === 'ok') {
      text = `已查询 ${connector} · ${title}`
      icon = 'check-circle'
    } else if (status === 'needs_confirmation') {
      text = `${title} 需要你确认`
      icon = 'shield'
    }
    if (eventType === 'approval' || status === 'waiting_approval') {
      const approvalId = item.approval_id || item.id
      const allowAlways = item.allow_always === true
      const actionable = Boolean(approvalId) && status === 'waiting_approval'
      return <article className={`chat-tool-approval ${status}`} key={item.stream_key || `${eventType}:${item.call_id || item.tool || item.approval_id || index}`}><div className="chat-tool-approval-copy"><span className="chat-tool-event-icon"><Icon name="shield" size={13} /></span><span>{text}</span>{item.permission_key && <small>{String(item.permission_key)}</small>}</div>{actionable && <div className="chat-tool-approval-actions"><button type="button" className="chat-tool-approval-reject" onClick={() => onRuntimeApproval?.(approvalId, 'reject')}>拒绝</button><button type="button" className="chat-tool-approval-approve" onClick={() => onRuntimeApproval?.(approvalId, 'approve')}>批准</button>{allowAlways && <button type="button" className="chat-tool-approval-always" onClick={() => onRuntimeApproval?.(approvalId, 'approve', true)}>始终允许</button>}</div>}</article>
    }
    return <div className={`chat-tool-event ${status}`} key={item.stream_key || `${eventType}:${item.call_id || item.tool || item.approval_id || index}`}><span className="chat-tool-event-icon"><Icon name={icon} size={13} /></span><span>{text}</span></div>
  })}</div>
}

function MessageText({ text }) {
  return <Markdown text={displaySecretReferences(text)} />
}

const contextTabs = [
  { id: 'activity', label: '动态', icon: 'activity' },
  { id: 'approvals', label: '待确认', icon: 'shield' },
  { id: 'tasks', label: '任务', icon: 'goal' },
  { id: 'memory', label: '记忆', icon: 'library' },
]

function activityDay(item) {
  const date = item.at ? parseDate(item.at) : null
  if (date) return dayLabel(date)
  if (/^(刚刚|今天)/.test(item.time || '')) return '今天'
  if (/^昨天/.test(item.time || '')) return '昨天'
  return '最近'
}

function ContextPanel({ data, pendingTasks, offline, onClose, decideRuntimeApproval }) {
  const [tab, setTab] = useState('activity')
  const activityGroups = useMemo(() => data.activity.reduce((groups, item) => {
    const label = activityDay(item)
    const group = groups[groups.length - 1]
    if (group?.label === label) group.items.push(item)
    else groups.push({ label, items: [item] })
    return groups
  }, []), [data.activity])

  return <aside className="context-panel" aria-label="Luma 动态">
    <button className="context-close" aria-label="关闭面板" title="关闭" onClick={onClose}><Icon name="close" size={18} /></button>
    <div className="context-agent"><div className="context-avatar"><LumaLogo /></div><strong>Luma</strong><span className={`context-status ${offline ? '' : 'online'}`}><i />{offline ? '离线' : '已连接'}</span></div>
    <div className="context-tabs" role="tablist">{contextTabs.map((item) => <button key={item.id} role="tab" aria-selected={tab === item.id} aria-label={item.label} title={item.label} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}><Icon name={item.icon} size={17} /></button>)}</div>
    {tab === 'activity' && (activityGroups.length ? activityGroups.map((group) => <section className="context-group" key={group.label}><h4>{group.label}</h4>{group.items.map((item, index) => <ContextItem key={index} icon="check-circle" title={item.title || item.text} detail={item.detail} time={item.time} />)}</section>) : <p className="context-empty">还没有动态</p>)}
    {tab === 'approvals' && (data.approvals.length ? <section className="context-group"><h4>等你确认</h4>{data.approvals.map((approval) => <ContextItem key={approval.id} icon="shield" title={`确认${approval.action === 'create_memory' ? '保存一条记忆' : '后台操作'}`} detail={approval.payload?.content || approval.payload?.title}><div className="context-actions"><button onClick={() => decideRuntimeApproval(approval.id, 'reject')}>拒绝</button><button className="primary" onClick={() => decideRuntimeApproval(approval.id, 'approve')}>批准</button>{approval.allow_always === true && <button onClick={() => decideRuntimeApproval(approval.id, 'approve', true)}>始终允许</button>}</div></ContextItem>)}</section> : <p className="context-empty">没有需要你确认的操作</p>)}
    {tab === 'tasks' && (pendingTasks.length ? <section className="context-group"><h4>进行中</h4>{pendingTasks.map((task) => <ContextItem key={task.id} icon="goal" title={task.title} detail={task.description} time={`${statusLabel(task.status)} · ${task.progress || 0}%`}><div className="context-progress"><i style={{ width: `${task.progress || 0}%` }} /></div></ContextItem>)}</section> : <p className="context-empty">暂时没有进行中的任务</p>)}
    {tab === 'memory' && (data.memories.length ? <section className="context-group"><h4>Luma 记得</h4>{data.memories.map((memory) => <ContextItem key={memory.id} icon="library" title={memory.content} time={memory.category === 'preference' ? '偏好' : memory.category === 'fact' ? '事实' : memory.category} />)}</section> : <p className="context-empty">还没有记忆</p>)}
  </aside>
}

function ContextItem({ icon, title, detail, time, children }) {
  return <article className="context-item"><span className="context-item-icon"><Icon name={icon} size={18} /></span><div><strong>{title}</strong>{detail && <p>{detail}</p>}{time && <time>{time}</time>}{children}</div></article>
}

function routineScheduleText(schedule) {
  const value = String(schedule || '')
  const daily = value.match(/^daily\s+(\d{2}:\d{2})$/i)
  if (daily) return `每天 ${daily[1]}`
  const weekly = value.match(/^weekly\s+([1-7])\s+(\d{2}:\d{2})$/i)
  if (weekly) return `每周${['一', '二', '三', '四', '五', '六', '日'][Number(weekly[1]) - 1]} ${weekly[2]}`
  const every = value.match(/^every\s+(\d+)\s*m$/i)
  return every ? `每 ${every[1]} 分钟` : value
}

function routineTimeText(value) {
  if (!value) return '—'
  try { return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value)) } catch { return formatTime(value) }
}

function RoutineForm({ initial, onSubmit, onCancel }) {
  const parse = (schedule) => {
    const value = String(schedule || '')
    const daily = value.match(/^daily\s+(\d{2}:\d{2})$/i)
    if (daily) return { frequency: 'daily', time: daily[1], weekday: '1', interval: '15' }
    const weekly = value.match(/^weekly\s+([1-7])\s+(\d{2}:\d{2})$/i)
    if (weekly) return { frequency: 'weekly', time: weekly[2], weekday: weekly[1], interval: '15' }
    const every = value.match(/^every\s+(\d+)\s*m$/i)
    return { frequency: 'every', time: '09:00', weekday: '1', interval: every?.[1] || '15' }
  }
  const parsed = parse(initial?.schedule)
  const [title, setTitle] = useState(initial?.title || '')
  const [prompt, setPrompt] = useState(initial?.prompt || '')
  const [frequency, setFrequency] = useState(parsed.frequency)
  const [time, setTime] = useState(parsed.time)
  const [weekday, setWeekday] = useState(parsed.weekday)
  const [interval, setInterval] = useState(parsed.interval)
  const [enabled, setEnabled] = useState(initial?.enabled !== false)
  const [busy, setBusy] = useState(false)
  async function submit(event) {
    event.preventDefault()
    if (!title.trim() || !prompt.trim() || busy) return
    const schedule = frequency === 'daily' ? `daily ${time}` : frequency === 'weekly' ? `weekly ${weekday} ${time}` : `every ${Math.max(15, Number(interval) || 15)}m`
    setBusy(true)
    try { await onSubmit({ title: title.trim(), prompt: prompt.trim(), schedule, timezone: 'Asia/Shanghai', enabled }) } finally { setBusy(false) }
  }
  return <form className="routine-form" onSubmit={submit}>
    <div className="routine-form-grid"><label>名称<input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={300} required /></label><label>频率<select value={frequency} onChange={(event) => setFrequency(event.target.value)}><option value="daily">每天</option><option value="weekly">每周</option><option value="every">每 N 分钟</option></select></label></div>
    <label>提示词<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={2} maxLength={20000} required /></label>
    {frequency === 'weekly' && <label>星期<select value={weekday} onChange={(event) => setWeekday(event.target.value)}>{['一', '二', '三', '四', '五', '六', '日'].map((day, index) => <option key={day} value={String(index + 1)}>星期{day}</option>)}</select></label>}
    {frequency !== 'every' ? <label>运行时间<input type="time" value={time} onChange={(event) => setTime(event.target.value)} required /></label> : <label>间隔分钟数<input type="number" min="15" step="1" value={interval} onChange={(event) => setInterval(event.target.value)} required /></label>}
    <label className="routine-enabled"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />启用例程</label>
    <div className="routine-form-actions"><button type="button" className="secondary-btn" onClick={onCancel}>取消</button><button type="submit" className="primary-btn" disabled={busy}>{busy ? '保存中…' : initial ? '保存修改' : '创建例程'}</button></div>
  </form>
}

function RoutineSection({ routines = [], onCreate, onUpdate, onDelete, onRun }) {
  const [editing, setEditing] = useState(null)
  const [creating, setCreating] = useState(false)
  return <section className="routine-section"><div className="rail-heading"><span>例程</span><button type="button" className="small-link" onClick={() => { setCreating(true); setEditing(null) }}>＋ 新建例程</button></div>
    {(creating || editing) && <RoutineForm key={editing?.id || 'new'} initial={editing} onCancel={() => { setCreating(false); setEditing(null) }} onSubmit={async (payload) => { if (editing) await onUpdate(editing.id, payload); else await onCreate(payload); setCreating(false); setEditing(null) }} />}
    {routines.length ? <div className="routine-list">{routines.map((routine) => <article className="routine-card" key={routine.id}><div className="routine-card-head"><div><h4>{routine.title}</h4><p>{routineScheduleText(routine.schedule)} · {routine.timezone || 'Asia/Shanghai'}</p></div><span className={`routine-status ${routine.enabled ? 'enabled' : ''}`}>{routine.enabled ? '已启用' : '已停用'}</span></div><p className="routine-prompt">{routine.prompt}</p><div className="routine-meta"><span>下次：{routine.next_run_at ? routineTimeText(routine.next_run_at) : '未安排'}</span><span>上次：{routine.last_run_at ? `${routineTimeText(routine.last_run_at)} · ${statusLabel(routine.last_status || '未知')}` : '尚未运行'}</span></div><div className="routine-actions"><button type="button" onClick={() => onRun(routine)}>立即运行</button><button type="button" onClick={() => onUpdate(routine.id, { enabled: !routine.enabled })}>{routine.enabled ? '停用' : '启用'}</button><button type="button" onClick={() => setEditing(routine)}>编辑</button><button type="button" className="danger" onClick={() => { if (window.confirm(`删除例程「${routine.title}」？`)) onDelete(routine.id) }}>删除</button></div></article>)}</div> : !creating && <EmptyState title="还没有例程" description="创建一个每天、每周或按间隔自动运行的例程。" />}
  </section>
}

function MissionsView({ goals, tasks, runtimeJobs, approvals, routines = [], addTask, startRuntimeJob, decideRuntimeApproval, onCreateRoutine, onUpdateRoutine, onDeleteRoutine, onRunRoutine }) {
  return <section className="subview"><div className="section-heading"><div><span className="label">MISSIONS</span><h3>让事情持续向前</h3><p className="subcopy">目标和任务写入远端数据库，runtime 会在后台继续处理；需要写入长期记忆时会先等你确认。</p></div><div className="section-actions"><button className="secondary-btn" onClick={() => startRuntimeJob('briefing')}>运行简报</button><button className="primary-btn" onClick={addTask}>＋ 新任务</button></div></div>{goals?.length > 0 && <><div className="rail-heading goal-list-heading"><span>长期目标</span><span>{goals.length} 个目标</span></div><div className="mission-grid">{goals.map((goal) => <article className={`mission-card ${goal.status === 'completed' ? 'done' : ''}`} key={goal.id}><span className="status">{goal.status === 'completed' ? '已完成' : '长期目标'}</span><h4>{goal.title}</h4><p>{goal.description || '持续推进中的长期目标。'}</p><div className="progress"><i style={{ width: `${goal.progress || 0}%` }} /></div><div className="item-meta">{goal.progress || 0}% · {goal.due_at ? formatTime(goal.due_at) : '持续跟进'}</div></article>)}</div></>}{approvals.length > 0 && <div className="approval-stack">{approvals.map((approval) => <article className="approval-card" key={approval.id}><div><span className="status waiting">等待你确认</span><h4>确认 {approval.action === 'create_memory' ? '保存一条记忆' : '后台操作'}</h4><p>{approval.payload?.content || approval.payload?.title || 'runtime 请求执行一项写入操作。'}</p></div><div className="approval-actions"><button className="secondary-btn" onClick={() => decideRuntimeApproval(approval.id, 'reject')}>拒绝</button><button className="primary-btn" onClick={() => decideRuntimeApproval(approval.id, 'approve')}>批准</button>{approval.allow_always === true && <button className="secondary-btn" onClick={() => decideRuntimeApproval(approval.id, 'approve', true)}>始终允许</button>}</div></article>)}</div>}<div className="mission-grid">{tasks.length ? tasks.map((task) => <article className={`mission-card ${task.status === 'done' ? 'done' : ''}`} key={task.id}><span className="status">{statusLabel(task.status)}</span><h4>{task.title}</h4><p>{task.description || ''}</p><div className="progress"><i style={{ width: `${task.progress || 0}%` }} /></div><div className="item-meta">{task.due_at || '未安排'} · {task.progress || 0}%</div></article>) : <EmptyState title="还没有任务" description="把想做的事交给 Luma，它会持续跟进。" />}</div><div className="runtime-jobs">{runtimeJobs.slice(0, 8).map((job) => <div className="runtime-job" key={job.id}><span className={`runtime-dot ${job.status}`} /><div><strong>{job.type}</strong><small>{statusLabel(job.status)} · {formatTime(job.updated_at)}</small></div>{job.result?.task_id && <em>已创建任务</em>}</div>)}</div><RoutineSection routines={routines} onCreate={onCreateRoutine} onUpdate={onUpdateRoutine} onDelete={onDeleteRoutine} onRun={onRunRoutine} /></section>
}

function MemoryView({ memories, addMemory, removeMemory, confirmMemory, toggleMemoryPinned }) {
  return <section className="subview"><div className="section-heading"><div><span className="label">MEMORY</span><h3>Luma 记得什么</h3><p className="subcopy">你可以随时查看、编辑或删除。推断内容会先等待你的确认。</p></div><button className="primary-btn" onClick={addMemory}>＋ 添加记忆</button></div><div className="memory-grid">{memories.length ? memories.map((memory) => {
    const inferred = memory.category === 'inferred' || memory.category === '推断'
    const label = inferred ? '推断' : ({ preference: '偏好', fact: '事实' })[memory.category] || memory.category || '记忆'
    return <article className={`memory-card ${memory.pinned ? 'pinned' : ''}`} key={memory.id}><div className="memory-card-head"><span className="memory-type">{label}</span><button type="button" className="memory-pin" aria-pressed={Boolean(memory.pinned)} onClick={() => toggleMemoryPinned?.(memory.id, !memory.pinned)}>{memory.pinned ? '已置顶' : '置顶'}</button></div><p>{memory.content}</p><div className="memory-card-actions">{inferred && <button type="button" className="memory-confirm" onClick={() => confirmMemory?.(memory.id)}>确认</button>}<button type="button" onClick={() => removeMemory(memory.id)}>删除</button></div></article>
  }) : <EmptyState title="还没有记忆" description="确认后的偏好和事实会出现在这里。" />}</div></section>
}

function EmptyState({ title, description }) {
  return <div className="empty-state"><LumaLogo className="empty-state-logo" /><h4>{title}</h4><p>{description}</p></div>
}

function navigate(path, { replace = false } = {}) {
  const method = replace ? 'replaceState' : 'pushState'
  window.history[method]({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

function Brand({ compact = false }) {
  return <div className={`site-brand ${compact ? 'compact' : ''}`}><span className="site-brand-mark"><LumaLogo label="Luma" /></span><span>{!compact && <small>personal operating system</small>}</span></div>
}

function LandingPage() {
  const [openFeature, setOpenFeature] = useState(0)
  const features = [
    { title: '像聊天一样自然', body: '告诉 Luma 你想完成什么。它会理解上下文、记住进度，并把复杂的事情拆成下一步。' },
    { title: '一台属于你的安全电脑', body: 'Luma 的运行时会持续工作，浏览网页、整理文件、执行任务，并在需要时回来找你确认。' },
    { title: '始终推进你的目标', body: '把一次想法变成长期计划。Luma 会追踪变化，在合适的时间提醒你并提出下一步建议。' },
    { title: '连接你已经在用的工具', body: '从邮件、日历到团队工具，Luma 可以在你的授权范围内读懂信息并协助行动。' },
  ]
  return <div className="muse-site">
    <header className="muse-nav">
      <div className="muse-nav-left"><button className="muse-menu" aria-label="打开菜单">☰</button><button className="muse-brand" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}><LumaLogo label="Luma" /></button></div>
      <nav className="muse-nav-links"><a href="#features">功能</a><a href="#security">安全</a><a href="#goals">目标</a><a href="#faq">常见问题</a></nav>
      <div className="muse-nav-actions"><button className="muse-login" onClick={() => navigate('/login')}>登录</button><button className="muse-cta small" onClick={() => navigate('/login')}>开始使用 <span>↗</span></button></div>
    </header>
    <main>
      <section className="muse-hero">
        <div className="muse-hero-backdrop" />
        <div className="muse-hero-copy"><span className="muse-kicker light">你的个人 AI 助理</span><h1>把琐事交给 Luma，<br /><em>专注真正重要的事。</em></h1><p>从对话到行动，Luma 在你身边持续工作，帮你完成那些一直没时间完成的事。</p><button className="muse-cta" onClick={() => navigate('/login')}>开始使用 <span>→</span></button></div>
        <div className="muse-hero-scene" aria-label="Luma personal workspace preview"><div className="muse-scene-glow" /><div className="muse-scene-window"><div className="muse-scene-bar"><span /><span /><span /><b>LUMA / PERSONAL SPACE</b></div><div className="muse-scene-content"><small>GOOD MORNING</small><h2>今天有空间<br />做重要的事。</h2><div className="muse-scene-message"><i><LumaLogo /></i><div><b>我已经整理好今天的重点。</b><small>3 个任务 · 2 个待确认事项</small></div></div><div className="muse-scene-input">告诉我你正在想什么… <strong>↑</strong></div></div></div><div className="muse-scene-chip chip-one">◫ <span>任务持续跟进</span><b>64%</b></div><div className="muse-scene-chip chip-two">✧ <span>记住你的偏好</span></div></div>
        <div className="muse-hero-scroll">向下探索 <span>↓</span></div>
      </section>

      <section className="muse-intro"><span className="muse-kicker">LUMA FEATURES</span><div className="muse-intro-row"><h2>把忙碌交给它，<br /><span>把时间留给自己。</span></h2><div><p>Luma 了解你的目标和节奏，在你授权的范围内持续行动，让每一天真正重要的事情向前推进。</p><button className="muse-cta blue" onClick={() => navigate('/login')}>试用 Luma <span>→</span></button></div></div></section>

      <section className="muse-features" id="features"><div className="muse-feature-list">{features.map((feature, index) => <button className={`muse-feature-row ${openFeature === index ? 'active' : ''}`} key={feature.title} onClick={() => setOpenFeature(index)}><span><strong>{feature.title}</strong>{openFeature === index && <small>{feature.body}</small>}</span><b>{openFeature === index ? '−' : '+'}</b></button>)}</div><div className="muse-phone"><div className="muse-phone-shadow" /><div className="muse-phone-screen"><div className="muse-phone-head"><span>‹</span><LumaLogo label="Luma" /><span>•••</span></div><div className="muse-phone-paper"><small>PERSONAL SPACE</small><strong>Field trip<br />permission slip</strong><div className="muse-paper-lines" /><span>PDF · ready to send</span></div><div className="muse-bubble white">Filled out the field trip form from your email.</div><div className="muse-bubble blue">Omg I forgot! <i><LumaLogo /></i></div><div className="muse-phone-compose">Send it <b>↑</b></div></div></div></section>

      <section className="muse-security" id="security"><span className="muse-kicker">SECURE AND IN CONTROL</span><div className="muse-card-grid"><article><span className="muse-card-icon">♢</span><h3>你的个人资料安全可控</h3><p>密钥留在服务端，记忆可以查看、编辑和删除。Luma 只在你授权的边界内工作。</p></article><article><span className="muse-card-icon">✓</span><h3>重要操作先让你确认</h3><p>发送邮件、写入第三方或其他敏感动作会先展示清晰的操作卡片，并保留完整活动记录。</p></article><article><span className="muse-card-icon">◌</span><h3>跨设备保持同一个空间</h3><p>Web、macOS、Windows、iOS 和 Android 共用同一套会话、任务和记忆。</p></article></div></section>

      <section className="muse-goals" id="goals"><div><span className="muse-kicker">GOALS AND IDEAS</span><h2>让一个想法，<br /><span>变成正在发生的事。</span></h2><p>告诉 Luma 你想去哪里。它会帮你做计划、持续跟进，在变化发生时带着新的建议回来。</p><button className="muse-cta blue" onClick={() => navigate('/login')}>开始一个目标 <span>→</span></button></div><div className="muse-goal-board"><div className="goal-board-top"><span>THIS WEEK</span><b>目标进度</b></div><div className="goal-main"><span>建立个人作品集</span><strong>72%</strong><div><i /></div></div><div className="goal-task"><span className="done">✓</span><p><b>整理项目案例</b><small>已完成 · 今天 09:20</small></p></div><div className="goal-task"><span className="active">→</span><p><b>写首页第一版</b><small>进行中 · Luma 正在跟进</small></p></div><div className="goal-task"><span>○</span><p><b>发布并邀请反馈</b><small>下一步 · 周五</small></p></div></div></section>

      <section className="muse-faq" id="faq"><span className="muse-kicker">FREQUENTLY ASKED QUESTIONS</span>{['Luma 可以帮我做什么？', 'Luma 会在我关闭应用后继续工作吗？', '我的数据和登录信息安全吗？', '我可以随时删除 Luma 记住的内容吗？'].map((question) => <button key={question}><span>{question}</span><b>＋</b></button>)}</section>
      <section className="muse-final"><span className="muse-kicker light">准备好了吗？</span><h2>从下一件重要的事开始。</h2><button className="muse-cta" onClick={() => navigate('/login')}>进入 Luma <span>→</span></button></section>
    </main>
    <footer className="muse-footer"><div className="muse-brand footer-brand"><LumaLogo label="Luma" /></div><span>© 2026 Luma · 一个更清晰的个人工作空间</span><div><a href="#security">隐私</a><a href="mailto:hello@luma.local">联系我们</a></div></footer>
  </div>
}

function FeatureCard({ icon, title, description, tone }) {
  return <article className={`feature-card ${tone}`}><span className="feature-icon">{icon}</span><h3>{title}</h3><p>{description}</p><span className="feature-arrow">↗</span></article>
}

function LoginPage({ onAuthenticated }) {
  const [config, setConfig] = useState({ registration_open: false, requires_invite: false })
  const [tab, setTab] = useState('login')
  const [fields, setFields] = useState({ login: '', username: '', email: '', display_name: '', password: '', confirmPassword: '', invite_code: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [configError, setConfigError] = useState('')
  const [providers, setProviders] = useState([{ name: 'password', label: '账号密码', kind: 'password' }])
  const [accountLabel, setAccountLabel] = useState('账号')
  const [ssoLabel, setSsoLabel] = useState('单点登录')
  const [ssoAccount, setSsoAccount] = useState('')
  const [ssoPassword, setSsoPassword] = useState('')
  const submittingRef = useRef(false)
  useEffect(() => {
    let active = true
    loadAuthProviders(API_URL).then((value) => {
      if (!active) return
      setConfig({ registration_open: value.registration_open === true, requires_invite: value.requires_invite === true })
      setProviders(Array.isArray(value.providers) ? value.providers : [])
      setAccountLabel(value.account_label || '账号')
      setSsoLabel(value.sso_label || '单点登录')
    }).catch((failure) => { if (active) setConfigError(failure.message) })
    return () => { active = false }
  }, [])
  function updateField(name, value) { setFields((current) => ({ ...current, [name]: value })) }
  function selectTab(next) {
    setTab(next)
    setError('')
    setFields((current) => ({ ...current, password: '', confirmPassword: '', invite_code: '' }))
  }
  async function submit(event) {
    event.preventDefault()
    if (submittingRef.current) return
    if (tab === 'register' && !config.registration_open) { setError('注册尚未开放'); return }
    submittingRef.current = true
    setBusy(true)
    setError('')
    try {
      const payload = tab === 'register'
        ? await registerWithPassword(API_URL, fields, config.requires_invite)
        : await loginWithPassword(API_URL, fields.login, fields.password)
      if (payload.access_token) sessionStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, payload.access_token)
      setFields((current) => ({ ...current, password: '', confirmPassword: '', invite_code: '' }))
      onAuthenticated()
      navigate('/app', { replace: true })
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '账号服务暂时不可用')
    } finally {
      submittingRef.current = false
      setBusy(false)
    }
  }
  async function submitSsoPassword(event) {
    event.preventDefault()
    if (submittingRef.current) return
    submittingRef.current = true
    setBusy(true)
    setError('')
    try {
      const payload = await loginWithSsoPassword(API_URL, ssoAccount, ssoPassword)
      if (payload.access_token) sessionStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, payload.access_token)
      setSsoPassword('')
      onAuthenticated()
      navigate('/app', { replace: true })
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '登录服务暂时不可用')
    } finally {
      submittingRef.current = false
      setBusy(false)
    }
  }
  const registering = tab === 'register' && config.registration_open
  const passwordProvider = providers.find((item) => item?.kind === 'password')
  const redirectProvider = providers.find((item) => item?.kind === 'redirect')
  const credentialsProvider = providers.find((item) => item?.kind === 'credentials')
  return <div className="muse-auth-page">
    <header className="muse-auth-nav"><button className="muse-brand" onClick={() => navigate('/')}><LumaLogo label="Luma" /></button><span>你的个人空间 <button onClick={() => navigate('/')}>了解 Luma</button></span></header>
    <main className="muse-auth-main">
      <div className="muse-auth-icon"><LumaLogo /></div><span className="muse-kicker">YOUR PERSONAL SPACE</span><h1>{registering ? '注册 Luma' : '登录 Luma'}</h1><p>你的数据存放在你部署的服务器上。</p>
      {passwordProvider && <div className="muse-auth-tabs" role="tablist" aria-label="账号认证"><button id="auth-login-tab" type="button" role="tab" aria-selected={!registering} aria-controls="auth-form" disabled={busy} onClick={() => selectTab('login')}>登录</button>{config.registration_open && <button id="auth-register-tab" type="button" role="tab" aria-selected={registering} aria-controls="auth-form" disabled={busy} onClick={() => selectTab('register')}>注册</button>}</div>}
      {passwordProvider && <form id="auth-form" className="muse-auth-form" role="tabpanel" aria-labelledby={registering ? 'auth-register-tab' : 'auth-login-tab'} onSubmit={submit}>
        {registering ? <>
          <label htmlFor="register-username">用户名<input id="register-username" name="username" type="text" autoComplete="username" placeholder="3–32 位字母、数字、_.-" minLength={3} maxLength={32} pattern="[a-zA-Z0-9_.\-]{3,32}" required disabled={busy} value={fields.username} onChange={(event) => updateField('username', event.target.value)} /></label>
          <label htmlFor="register-email">邮箱（可选）<input id="register-email" name="email" type="email" autoComplete="email" disabled={busy} value={fields.email} onChange={(event) => updateField('email', event.target.value)} /></label>
          <label htmlFor="register-display-name">显示名（可选）<input id="register-display-name" name="display_name" type="text" autoComplete="nickname" disabled={busy} value={fields.display_name} onChange={(event) => updateField('display_name', event.target.value)} /></label>
        </> : <label htmlFor="login-account">用户名或邮箱<input id="login-account" name="login" type="text" autoComplete="username" placeholder="用户名或邮箱" required disabled={busy} value={fields.login} onChange={(event) => updateField('login', event.target.value)} /></label>}
        <label htmlFor="auth-password">密码<input id="auth-password" name="password" type="password" autoComplete={registering ? 'new-password' : 'current-password'} required disabled={busy} value={fields.password} onChange={(event) => updateField('password', event.target.value)} aria-describedby={registering ? 'registration-password-rule' : undefined} /></label>
        {registering && <>
          <p id="registration-password-rule" className="muse-auth-rule">密码长度为 8–128 位。</p>
          <label htmlFor="register-confirm-password">确认密码<input id="register-confirm-password" name="confirmPassword" type="password" autoComplete="new-password" required disabled={busy} value={fields.confirmPassword} onChange={(event) => updateField('confirmPassword', event.target.value)} /></label>
          {config.requires_invite && <label htmlFor="register-invite-code">邀请码<input id="register-invite-code" name="invite_code" type="text" autoComplete="off" required disabled={busy} value={fields.invite_code} onChange={(event) => updateField('invite_code', event.target.value)} /></label>}
        </>}
        <button type="submit" className="muse-auth-button muse-auth-submit" disabled={busy}>{busy ? (registering ? '注册中…' : '登录中…') : (registering ? '注册' : '登录')}<b aria-hidden="true">→</b></button>
        {error && !credentialsProvider && <p className="muse-auth-error" role="alert">{error}</p>}
      </form>}
      {redirectProvider && <button type="button" className="muse-auth-button muse-auth-submit" disabled={busy} onClick={() => beginSsoLogin(redirectProvider, setError)}>{redirectProvider.label || ssoLabel}<b aria-hidden="true">→</b></button>}
      {credentialsProvider && <form className="muse-auth-form" onSubmit={submitSsoPassword}>
        <label htmlFor="sso-account">{accountLabel}<input id="sso-account" name="account" type="text" autoComplete="username" placeholder={accountLabel} maxLength={64} required disabled={busy} value={ssoAccount} onChange={(event) => setSsoAccount(event.target.value)} /></label>
        <label htmlFor="sso-password">密码<input id="sso-password" name="password" type="password" autoComplete="current-password" maxLength={128} required disabled={busy} value={ssoPassword} onChange={(event) => setSsoPassword(event.target.value)} /></label>
        <button type="submit" className="muse-auth-button muse-auth-submit" disabled={busy}>{busy ? '登录中…' : '登录'}<b aria-hidden="true">→</b></button>
      </form>}
      {error && (!passwordProvider || credentialsProvider) && <p className="muse-auth-error" role="alert">{error}</p>}
      {configError && <p className="muse-auth-help" role="alert">{configError}</p>}
      <div className="muse-auth-divider"><span>在你的服务器上安全登录</span></div><div className="muse-auth-points"><span>◉ <b>跨设备同步</b><small>所有设备都能继续</small></span><span>⌁ <b>隐私优先</b><small>账号和数据由你管理</small></span></div>
    </main>
    <footer className="muse-auth-footer"><span>© 2026 Luma contributors</span><span>PERSONAL AI, BY LUMA</span></footer>
  </div>
}

const SSO_CALLBACK_STORAGE_KEY = 'luma_sso_state'

function ssoRedirectOrigin(provider) {
  const value = provider && typeof provider.origin === 'string' ? provider.origin : ''
  try {
    const target = new URL(value)
    if (target.protocol !== 'https:' && target.protocol !== 'http:') return ''
    return target.origin
  } catch {
    return ''
  }
}

async function beginSsoLogin(provider, reportError) {
  try {
    const started = await startSsoLogin(API_URL, '/app')
    const target = new URL(started.url)
    if (target.protocol !== 'https:' && target.protocol !== 'http:') throw new Error('单点登录暂时不可用')
    const expected = ssoRedirectOrigin(provider)
    if (expected && target.origin !== expected) throw new Error('单点登录暂时不可用')
    if (!started.state) throw new Error('单点登录暂时不可用')
    sessionStorage.setItem(SSO_CALLBACK_STORAGE_KEY, JSON.stringify({ state: started.state, next: '/app' }))
    window.location.assign(started.url)
  } catch (failure) {
    const message = failure instanceof Error ? failure.message : '单点登录暂时不可用'
    if (typeof reportError === 'function') reportError(message)
  }
}

function RouteLoading() {
  return <div className="callback-shell route-loading"><div className="callback-card"><Brand /><div className="callback-status loading"><span className="callback-icon">◌</span><h1>正在打开 Luma</h1><p>正在确认你的登录状态…</p></div></div></div>
}

function RouteServiceUnavailable() {
  return <div className="callback-shell route-loading"><div className="callback-card"><Brand /><div className="callback-status error"><span className="callback-icon">!</span><h1>服务暂时不可用</h1><p>正在稍后重试，登录状态不会被清除。</p><button className="site-primary" onClick={() => window.location.reload()}>重试 <span>↻</span></button></div></div></div>
}

function SsoCallbackPage() {
  const [status, setStatus] = useState('loading')
  const [message, setMessage] = useState('正在确认登录…')
  useEffect(() => {
    let active = true
    async function exchange() {
      const params = new URLSearchParams(window.location.search)
      const ticket = params.get('ticket') || ''
      const returnedState = params.get('state') || ''
      let pending = null
      try { pending = JSON.parse(sessionStorage.getItem(SSO_CALLBACK_STORAGE_KEY) || 'null') } catch { pending = null }
      const expectedState = pending?.state || ''
      if (!ticket || (returnedState && (!expectedState || returnedState !== expectedState))) {
        if (active) { setStatus('error'); setMessage('登录交易已失效，请重新发起登录。') }
        return
      }
      try {
        const payload = await exchangeSsoTicket(API_URL, ticket, returnedState)
        if (payload.access_token) sessionStorage.setItem(ACCESS_TOKEN_STORAGE_KEY, payload.access_token)
        sessionStorage.removeItem(SSO_CALLBACK_STORAGE_KEY)
        const next = pending?.next && pending.next.startsWith('/') && !pending.next.startsWith('//') ? pending.next : '/app'
        if (active) { setStatus('success'); setMessage('登录成功，正在进入工作空间…'); window.setTimeout(() => navigate(next), 350) }
      } catch (failure) {
        if (active) { setStatus('error'); setMessage(failure instanceof Error ? failure.message : '单点登录暂时不可用') }
      }
    }
    exchange()
    return () => { active = false }
  }, [])
  return <div className="callback-shell"><div className="callback-card"><Brand /><div className={`callback-status ${status}`}><span className="callback-icon">{status === 'loading' ? '◌' : status === 'success' ? '✓' : '!'}</span><h1>{status === 'loading' ? '正在登录' : status === 'success' ? '欢迎回来' : '登录没有完成'}</h1><p>{message}</p>{status === 'error' && <button className="site-primary" onClick={() => navigate('/login')}>重新登录 <span>→</span></button>}</div></div></div>
}

function RouteApp() {
  const isFileClient = window.location.protocol === 'file:'
  const isClientEntry = isFileClient || new URLSearchParams(window.location.search).get('client') === '1'
  const initialPath = window.location.protocol === 'file:'
    ? '/app'
    : (window.location.pathname.replace(/\/$/, '') || '/')
  const [path, setPath] = useState(() => initialPath)
  const needsAuth = !isFileClient && (isClientEntry || ['/', '/login', '/app', '/chat', '/admin'].includes(initialPath))
  const [authState, setAuthState] = useState(() => needsAuth ? 'checking' : 'unknown')
  useEffect(() => {
    // Discard a tray action that arrived during loading before login.
    if (authState !== 'anonymous') return undefined
    return subscribeDesktopCommands(window.desktop, () => null)
  }, [authState])
  useEffect(() => { const onPopState = () => setPath(window.location.pathname.replace(/\/$/, '') || '/'); window.addEventListener('popstate', onPopState); return () => window.removeEventListener('popstate', onPopState) }, [])
  useEffect(() => {
    const onAuthRequired = () => {
      sessionStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY)
      localStorage.removeItem(ACCESS_TOKEN_STORAGE_KEY)
      setAuthState('anonymous')
    }
    window.addEventListener('luma-auth-required', onAuthRequired)
    return () => window.removeEventListener('luma-auth-required', onAuthRequired)
  }, [])
  useEffect(() => {
    const protectedPath = isClientEntry || ['/', '/login', '/app', '/chat', '/admin'].includes(path)
    if (!protectedPath || window.location.protocol === 'file:') return undefined
    let active = true
    setAuthState('checking')
    const accessToken = sessionStorage.getItem(ACCESS_TOKEN_STORAGE_KEY) || localStorage.getItem(ACCESS_TOKEN_STORAGE_KEY)
    fetch(`${API_URL}/auth/me`, {
      headers: { Accept: 'application/json', ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}) },
      credentials: 'include',
    }).then((response) => {
      if (active) setAuthState(response.status === 503 ? 'unavailable' : response.ok ? 'authenticated' : 'anonymous')
    }).catch(() => {
      if (active) setAuthState('anonymous')
    })
    return () => { active = false }
  }, [path, isClientEntry])
  useEffect(() => {
    if (authState === 'authenticated' && (path === '/' || path === '/login')) navigate('/app', { replace: true })
  }, [authState, path])
  if (path === '/sso-callback') return <SsoCallbackPage />
  if ((path === '/' || path === '/login') && authState === 'checking') return <RouteLoading />
  if ((path === '/' || path === '/login') && authState === 'authenticated') return <RouteLoading />
  if (['/', '/login', '/app', '/chat', '/admin'].includes(path) && authState === 'unavailable') return <RouteServiceUnavailable />
  if (path === '/login') return <LoginPage onAuthenticated={() => setAuthState('authenticated')} />
  if (['/app', '/chat'].includes(path) && authState === 'checking') return <RouteLoading />
  if (['/app', '/chat'].includes(path) && authState === 'anonymous') return <LoginPage onAuthenticated={() => setAuthState('authenticated')} />
  if (path === '/app' || path === '/chat') return <WorkspaceApp />
  if (path === '/admin' && authState === 'checking') return <RouteLoading />
  if (path === '/admin' && authState === 'anonymous') return <LoginPage onAuthenticated={() => setAuthState('authenticated')} />
  if (path === '/admin') return <React.Suspense fallback={<RouteLoading />}><AdminApp request={adminRequest} onExit={() => navigate('/app')} /></React.Suspense>
  return <LandingPage />
}

createRoot(document.getElementById('root')).render(<RouteApp />)
