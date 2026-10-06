import { safeExternalUrl } from './content-data.js'

export function feedPath(cursor = '') {
  const params = new URLSearchParams({ limit: '20' })
  if (cursor) params.set('cursor', cursor)
  return `/feed?${params}`
}

export function feedSources(sources) {
  if (!Array.isArray(sources)) return []
  return sources.flatMap((source) => {
    const url = safeExternalUrl(source?.url)
    return url ? [{ title: String(source.title || url), url }] : []
  })
}

export function mergeFeedPosts(current, incoming) {
  const posts = new Map(current.map((post) => [post.id, post]))
  for (const post of incoming || []) posts.set(post.id, post)
  return [...posts.values()]
}

export function activityPath(userId = '', kind = '', cursor = '') {
  const params = new URLSearchParams({ limit: '100' })
  if (userId.trim()) params.set('user_id', userId.trim())
  if (kind.trim()) params.set('kind', kind.trim())
  if (cursor) params.set('cursor', cursor)
  return `/activity?${params}`
}

export function activityPage(data) {
  return {
    items: Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []),
    next_cursor: !Array.isArray(data) && typeof data?.next_cursor === 'string' ? data.next_cursor : null,
  }
}
