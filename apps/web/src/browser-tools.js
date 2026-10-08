const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif'])
export const DEFAULT_BROWSER_LIVE_HOST_SUFFIXES = ['.tencentags.com']

const suffixListeners = new Set()
let liveHostSuffixes = DEFAULT_BROWSER_LIVE_HOST_SUFFIXES.slice()

export function browserLiveHostSuffixes() {
  return liveHostSuffixes.slice()
}

export function subscribeBrowserLiveHostSuffixes(listener) {
  suffixListeners.add(listener)
  return () => suffixListeners.delete(listener)
}

function publishSuffixes(next) {
  liveHostSuffixes = next
  for (const listener of suffixListeners) listener(next.slice())
}

export function normalizeBrowserLiveHostSuffixes(values) {
  const source = Array.isArray(values) ? values : String(values || '').split(',')
  const seen = new Set()
  const next = []
  for (const item of source) {
    let text = String(item || '').trim().toLowerCase().replace(/\.+$/, '')
    if (!text || /[/\s@:\\]/.test(text) || text.includes('..')) continue
    if (!text.startsWith('.')) text = `.${text}`
    if (!/^\.[a-z0-9.-]+$/.test(text)) continue
    if (seen.has(text)) continue
    seen.add(text)
    next.push(text)
  }
  return next
}

export function setBrowserLiveHostSuffixes(values) {
  const next = normalizeBrowserLiveHostSuffixes(values)
  publishSuffixes(next.length ? next : DEFAULT_BROWSER_LIVE_HOST_SUFFIXES.slice())
  return liveHostSuffixes.slice()
}

async function fetchClientConfig(url) {
  return fetch(url, { headers: { Accept: 'application/json' }, credentials: 'include' })
}

export async function loadBrowserLiveHostSuffixes(apiBase, fetcher = fetchClientConfig) {
  const fallback = () => setBrowserLiveHostSuffixes(DEFAULT_BROWSER_LIVE_HOST_SUFFIXES)
  try {
    const root = String(apiBase || '').replace(/\/$/, '')
    if (!root) return fallback()
    const response = await fetcher(`${root}/client-config`)
    if (!response?.ok) return fallback()
    const payload = await response.json()
    const raw = payload && typeof payload === 'object' ? payload.browser_live_host_suffixes : null
    const next = normalizeBrowserLiveHostSuffixes(raw)
    if (!next.length) return fallback()
    return setBrowserLiveHostSuffixes(next)
  } catch {
    return fallback()
  }
}
const BROWSER_PROGRESS = {
  open: '正在打开网页…', read: '正在读取页面…', screenshot: '正在截取页面…',
  click: '正在点击页面…', type: '正在填写页面…', scroll: '正在滚动页面…',
  submit: '正在提交页面…', live: '正在打开实时画面…',
}

export function browserProgressLabel(tool) {
  const name = String(tool || '')
  return name.startsWith('browser.') ? BROWSER_PROGRESS[name.slice(8)] || '正在操作浏览器…' : ''
}

export function isBrowserLiveUrl(value, suffixes = liveHostSuffixes) {
  try {
    const url = new URL(value)
    const host = url.hostname.toLowerCase()
    const allowed = Array.isArray(suffixes) && suffixes.length ? suffixes : DEFAULT_BROWSER_LIVE_HOST_SUFFIXES
    const trusted = allowed.some((suffix) => host.endsWith(suffix) && host.length > suffix.length)
    return url.protocol === 'https:' && trusted && !url.username && !url.password && (!url.port || url.port === '443')
  } catch {
    return false
  }
}

export function isImageFile(mediaType) {
  return IMAGE_TYPES.has(String(mediaType || '').split(';')[0].trim().toLowerCase())
}

export function liveBrowserEvents(events) {
  return Array.isArray(events) ? events.filter((item) => item?.kind === 'browser_live' || item?.data?.kind === 'browser_live' || item?.payload?.kind === 'browser_live') : []
}

export function preserveBrowserLiveMessages(messages, previousMessages) {
  const previous = new Map(previousMessages.map((item) => [item.id, item]))
  return messages.map((message) => {
    const events = liveBrowserEvents(previous.get(message.id)?.toolEvents)
    return events.length ? { ...message, toolEvents: events } : message
  })
}

export async function fetchFileBlob(apiUrl, fileId, getAuthHeaders, signal) {
  const response = await fetch(`${apiUrl}/files/${encodeURIComponent(fileId)}/content`, {
    headers: { Accept: 'application/octet-stream', ...(getAuthHeaders ? getAuthHeaders() : {}) },
    credentials: 'include', signal,
  })
  if (!response.ok) throw new Error(`下载失败（${response.status}）`)
  return response.blob()
}
