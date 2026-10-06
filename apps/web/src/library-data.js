export const LIBRARY_TYPES = [
  { type: 'all', label: '全部', icon: 'grid', empty: '对话里的产出和上传的文件，都会留在这里。' },
  { type: 'document', label: '文档', icon: 'doc', empty: '还没有文档，写好的文字和上传的文档会放在这里。' },
  { type: 'sheet', label: '表格', icon: 'chart', empty: '还没有表格，整理好的数据会放在这里。' },
  { type: 'web', label: '网页', icon: 'globe', empty: '还没有网页产物，生成的网页会放在这里。' },
  { type: 'image', label: '图片', icon: 'image', empty: '还没有图片，截图和生成的图片会放在这里。' },
  { type: 'code', label: '代码', icon: 'code', empty: '还没有代码，写好的程序和脚本会放在这里。' },
  { type: 'archive', label: '压缩包', icon: 'archive', empty: '还没有压缩包，打包的文件会放在这里。' },
  { type: 'other', label: '其他', icon: 'doc', empty: '还没有其他文件，更多类型的文件会放在这里。' },
]

export function libraryType(item) {
  if (LIBRARY_TYPES.some((entry) => entry.type !== 'all' && entry.type === item?.type)) return item.type
  const name = String(item?.filename || '').toLowerCase()
  const media = String(item?.media_type || '').toLowerCase().split(';')[0].trim()
  if (/\.(?:png|jpe?g|gif|webp)$/.test(name) || /^image\/(?:png|jpeg|gif|webp)$/.test(media)) return 'image'
  if (/\.(?:csv|xlsx|xls)$/.test(name) || ['text/csv', 'application/vnd.ms-excel', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'].includes(media)) return 'sheet'
  if (/\.html?$/.test(name) || media === 'text/html') return 'web'
  if (/\.(?:py|js|ts|json|sql|sh)$/.test(name) || media === 'application/json') return 'code'
  if (/\.(?:zip|tar\.gz|tgz)$/.test(name) || ['application/zip', 'application/gzip', 'application/x-tar'].includes(media)) return 'archive'
  if (/\.(?:md|txt|pdf|doc|docx)$/.test(name) || ['text/markdown', 'text/plain', 'application/pdf', 'application/msword', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'].includes(media)) return 'document'
  return 'other'
}

export function filterLibraryItems(items, type = 'all') {
  return (Array.isArray(items) ? items : []).filter((item) => type === 'all' || libraryType(item) === type)
}

export function libraryCategoryRows(counts = {}) {
  return LIBRARY_TYPES.map((entry) => ({ ...entry, count: Number.isFinite(Number(counts?.[entry.type])) ? Math.max(0, Math.floor(Number(counts[entry.type]))) : 0 }))
}

export function libraryGroups(items) {
  const list = Array.isArray(items) ? items : []
  return { pinned: list.filter((item) => item.pinned), recent: list.filter((item) => !item.pinned) }
}

export function libraryPreviewKind(preview) {
  return ['csv', 'markdown', 'text', 'html_source', 'image', 'none'].includes(preview?.kind) ? preview.kind : 'none'
}

export function libraryQuery({ type = 'all', q = '', sort = 'recent', cursor = '' } = {}) {
  const params = new URLSearchParams({ type, q, sort, limit: '50' })
  if (cursor) params.set('cursor', cursor)
  return `/library?${params.toString()}`
}

export function patchLibraryItem(request, id, patch) {
  return request(`/library/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(patch) })
}

export const LIBRARY_VIEW_KEY = 'luma-library-view'

export function readLibraryView(storage) {
  try {
    return storage.getItem(LIBRARY_VIEW_KEY) === 'list' ? 'list' : 'grid'
  } catch {
    return 'grid'
  }
}

export function writeLibraryView(storage, view) {
  try {
    storage.setItem(LIBRARY_VIEW_KEY, view === 'list' ? 'list' : 'grid')
  } catch {}
}

export function isLibraryImageBlob(blob) {
  return /^image\/(?:png|jpeg|gif|webp)(?:;|$)/i.test(blob?.type || '')
}
