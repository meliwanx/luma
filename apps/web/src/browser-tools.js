const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif'])
const BROWSER_PROGRESS = {
  open: '正在打开网页…', read: '正在读取页面…', screenshot: '正在截取页面…',
  click: '正在点击页面…', type: '正在填写页面…', scroll: '正在滚动页面…',
  submit: '正在提交页面…', live: '正在打开实时画面…',
}

export function browserProgressLabel(tool) {
  const name = String(tool || '')
  return name.startsWith('browser.') ? BROWSER_PROGRESS[name.slice(8)] || '正在操作浏览器…' : ''
}

export function isBrowserLiveUrl(value) {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && url.hostname.endsWith('.tencentags.com') && !url.username && !url.password && (!url.port || url.port === '443')
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
