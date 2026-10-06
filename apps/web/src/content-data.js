export const PROACTIVE_DEFAULTS = {
  enabled: true, max_per_day: 2, window_start: '09:00', window_end: '21:30',
  timezone: 'Asia/Shanghai', topics_like: '', topics_avoid: '', style: '',
  feed_enabled: true, feed_per_day: 1, feed_instructions: '',
}

export function relativeTime(value, now = Date.now()) {
  const timestamp = new Date(value).getTime()
  if (!value || !Number.isFinite(timestamp)) return ''
  const seconds = Math.max(0, (now - timestamp) / 1000)
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)} 天前`
  return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'numeric', day: 'numeric' }).format(new Date(timestamp))
}

export function safeExternalUrl(value) {
  if (typeof value !== 'string' || !/^https?:\/\//i.test(value.trim())) return ''
  try {
    const url = new URL(value.trim())
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) return ''
    return url.href
  } catch { return '' }
}

export function proactivePrefs(values) {
  const result = {}
  for (const key of Object.keys(PROACTIVE_DEFAULTS)) result[key] = values?.[key] ?? PROACTIVE_DEFAULTS[key]
  return result
}

export function validateProactivePrefs(values) {
  if (!Number.isInteger(values.max_per_day) || values.max_per_day < 0 || values.max_per_day > 5) return '每天最多需为 0–5'
  if (!Number.isInteger(values.feed_per_day) || values.feed_per_day < 0 || values.feed_per_day > 3) return '每天篇数需为 0–3'
  if (![values.window_start, values.window_end].every((value) => /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value))) return '请选择有效的时间段'
  if (String(values.topics_like || '').length > 1000 || String(values.topics_avoid || '').length > 1000) return '话题最多 1000 字'
  if (String(values.style || '').length > 500) return '风格最多 500 字'
  return ''
}

export async function saveProactivePrefs(request, values) {
  const latest = await request('/proactive/prefs')
  const payload = proactivePrefs({ ...values, feed_instructions: latest?.feed_instructions ?? '' })
  await request('/proactive/prefs', { method: 'PUT', body: JSON.stringify(payload) })
  return payload
}
