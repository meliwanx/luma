import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Icon from './icons.jsx'
import Markdown from './markdown.jsx'
import { ActionMenu, ContentDialog, ContentIcon } from './content-ui.jsx'
import { relativeTime } from './content-data.js'
import { LIBRARY_TYPES, filterLibraryItems, isLibraryImageBlob, libraryCategoryRows, libraryGroups, libraryPreviewKind, libraryQuery, libraryType, patchLibraryItem, readLibraryView, writeLibraryView } from './library-data.js'
import './library.css'

function filePath(id) {
  return `/files/${encodeURIComponent(id)}/content`
}

function libraryPath(id) {
  return `/library/${encodeURIComponent(id)}`
}

function itemTitle(item) {
  return item.title || item.filename || '未命名文件'
}

function useFileImage(item, requestBlob) {
  const [image, setImage] = useState({ id: null, url: '', loading: false })
  useEffect(() => {
    if (!item?.id || libraryType(item) !== 'image') {
      setImage({ id: null, url: '', loading: false })
      return undefined
    }
    setImage({ id: item.id, url: '', loading: true })
    let alive = true
    let objectUrl = ''
    Promise.resolve().then(() => requestBlob(filePath(item.id))).then((blob) => {
      if (!alive) return
      if (!isLibraryImageBlob(blob)) { setImage({ id: item.id, url: '', loading: false }); return }
      objectUrl = URL.createObjectURL(blob)
      setImage({ id: item.id, url: objectUrl, loading: false })
    }).catch(() => { if (alive) setImage({ id: item.id, url: '', loading: false }) })
    return () => {
      alive = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [item?.id, item?.type, item?.media_type, requestBlob])
  if (!item?.id || libraryType(item) !== 'image') return { url: '', loading: false }
  return image.id === item.id ? image : { url: '', loading: true }
}

export function LibraryCategories({ counts, type, onChange }) {
  return <nav className="library-categories" aria-label="资源类型">
    {libraryCategoryRows(counts).map((entry) => <button type="button" key={entry.type} className={type === entry.type ? 'active' : ''} aria-pressed={type === entry.type} onClick={() => onChange(entry.type)}>
      <ContentIcon name={entry.icon} size={18} /><span>{entry.label}</span><small>{entry.count}</small>
    </button>)}
  </nav>
}

function LibraryCard({ item, requestBlob, onPreview, onOpenSession }) {
  const image = useFileImage(item, requestBlob)
  const category = LIBRARY_TYPES.find((entry) => entry.type === libraryType(item)) || LIBRARY_TYPES[7]
  return <article className="library-card">
    <button type="button" className="library-card-open" onClick={() => onPreview(item)} aria-label={`预览 ${itemTitle(item)}`}>
      <span className={`library-thumbnail library-thumbnail-${category.type}`}>
        {image.url ? <img src={image.url} alt="" loading="lazy" /> : <><ContentIcon name={category.icon} size={28} /><b>{Array.from(itemTitle(item))[0]}</b></>}
      </span>
      <span className="library-card-copy"><strong title={itemTitle(item)}>{itemTitle(item)}</strong><small>{category.label} · {relativeTime(item.last_opened_at || item.created_at)}</small></span>
      {item.pinned && <span className="library-pinned" aria-label="已置顶"><ContentIcon name="pin" size={14} /></span>}
    </button>
    {item.session_id && <button type="button" className="library-source" onClick={() => onOpenSession(item.session_id)}><Icon name="chat" size={13} /><span>来自 {item.session_title || '对话'}</span></button>}
  </article>
}

export function LibraryPreview({ preview, imageUrl, imageLoading = false, onDownload, onCopy }) {
  const kind = libraryPreviewKind(preview)
  if (kind === 'csv') return <>
    <p className="library-preview-note">共 {preview.total_rows ?? preview.rows?.length ?? 0} 行{preview.truncated ? ' · 预览已截断，仅展示部分行或列，请下载查看完整内容。' : ''}</p>
    <div className="library-table-scroll" role="region" aria-label="表格预览" tabIndex={0}><table><thead><tr>{(preview.columns || []).map((column, index) => <th key={index} scope="col">{String(column ?? '')}</th>)}</tr></thead><tbody>{(preview.rows || []).map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{String(cell ?? '')}</td>)}</tr>)}</tbody></table></div>
  </>
  if (kind === 'markdown') return <>{preview.truncated && <p className="library-preview-note">预览已截断，请下载查看完整内容。</p>}<Markdown text={preview.text || ''} /></>
  if (kind === 'text' || kind === 'html_source') return <>
    {kind === 'html_source' && <p className="library-preview-note">网页产物仅显示源码，下载后可在浏览器中打开</p>}
    <div className="library-code-toolbar"><span>{kind === 'html_source' ? 'HTML 源码' : (preview.language || '纯文本')}</span><button type="button" className="library-button" onClick={() => onCopy(preview.text || '')}>复制</button></div>
    {preview.truncated && <p className="library-preview-note">预览已截断，请下载查看完整内容。</p>}
    <pre className="library-code"><code>{preview.text || ''}</code></pre>
  </>
  if (kind === 'image') return imageLoading ? <p className="library-loading" role="status">正在加载图片…</p> : imageUrl ? <img className="library-preview-image" src={imageUrl} alt="资源预览" /> : <div className="library-empty">图片暂时无法预览，请下载查看。<button type="button" className="library-button" onClick={onDownload}>下载</button></div>
  return <div className="library-empty">此类型暂不支持预览<button type="button" className="library-button" onClick={onDownload}><Icon name="download" size={16} />下载</button></div>
}

export default function LibraryPage({ request, requestBlob, notify, onOpenSession }) {
  const [type, setType] = useState('all')
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState('recent')
  const [view, setView] = useState(() => {
    try { return readLibraryView(window.localStorage) } catch { return 'grid' }
  })
  const [items, setItems] = useState([])
  const [counts, setCounts] = useState({})
  const [cursor, setCursor] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState(null)
  const [preview, setPreview] = useState(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState('')
  const [renaming, setRenaming] = useState(false)
  const [titleDraft, setTitleDraft] = useState('')
  const [deleting, setDeleting] = useState(null)
  const [busy, setBusy] = useState(false)
  const listSequence = useRef(0)
  const previewSequence = useRef(0)
  const loadingRef = useRef(false)
  const active = useRef(true)
  const downloaded = useRef(new Map())
  const loadLatest = useRef(null)
  const previewImage = useFileImage(preview?.kind === 'image' ? selected : null, requestBlob)

  useEffect(() => {
    active.current = true
    return () => {
      active.current = false
      listSequence.current += 1
      previewSequence.current += 1
      downloaded.current.forEach((timer, url) => { clearTimeout(timer); URL.revokeObjectURL(url) })
      downloaded.current.clear()
    }
  }, [])

  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 250)
    return () => clearTimeout(timer)
  }, [search])

  const load = useCallback(async (nextCursor = '') => {
    if (nextCursor && loadingRef.current) return
    const sequence = ++listSequence.current
    loadingRef.current = true
    setLoading(true)
    setError('')
    try {
      const data = await request(libraryQuery({ type, q: query, sort, cursor: nextCursor }))
      if (!active.current || sequence !== listSequence.current) return
      setItems((previous) => nextCursor ? [...previous, ...(data.items || []).filter((item) => !previous.some((old) => old.id === item.id))] : (data.items || []))
      setCounts(data.counts || {})
      setCursor(data.next_cursor || null)
    } catch (err) {
      if (active.current && sequence === listSequence.current) setError(err.message || '资源加载失败')
    } finally {
      if (active.current && sequence === listSequence.current) { loadingRef.current = false; setLoading(false) }
    }
  }, [request, type, query, sort])
  loadLatest.current = load

  useEffect(() => {
    setItems([])
    setCursor(null)
    load()
  }, [load])

  const groups = useMemo(() => libraryGroups(filterLibraryItems(items, type)), [items, type])
  const category = LIBRARY_TYPES.find((entry) => entry.type === type) || LIBRARY_TYPES[0]

  function changeView(nextView) {
    setView(nextView)
    try { writeLibraryView(window.localStorage, nextView) } catch {}
  }

  function closePreview() {
    previewSequence.current += 1
    setSelected(null)
    setPreview(null)
    setRenaming(false)
    setPreviewLoading(false)
  }

  async function openPreview(item) {
    const sequence = ++previewSequence.current
    setSelected(item)
    setPreview(null)
    setPreviewError('')
    setPreviewLoading(true)
    setRenaming(false)
    try {
      const data = await request(`${libraryPath(item.id)}/preview`)
      if (!active.current || sequence !== previewSequence.current) return
      setPreview(data)
      const opened = new Date().toISOString()
      setItems((previous) => previous.map((entry) => entry.id === item.id ? { ...entry, last_opened_at: opened } : entry))
      loadLatest.current()
    } catch (err) {
      if (active.current && sequence === previewSequence.current) setPreviewError(err.message || '预览加载失败')
    } finally {
      if (active.current && sequence === previewSequence.current) setPreviewLoading(false)
    }
  }

  async function download(item) {
    try {
      const blob = await requestBlob(filePath(item.id))
      if (!active.current) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = item.filename || itemTitle(item)
      document.body.appendChild(link)
      link.click()
      link.remove()
      const timer = setTimeout(() => { URL.revokeObjectURL(url); downloaded.current.delete(url) }, 1000)
      downloaded.current.set(url, timer)
    } catch (err) { notify(err.message || '下载失败') }
  }

  async function updateSelected(patch) {
    if (!selected || busy) return
    setBusy(true)
    const id = selected.id
    try {
      const item = await patchLibraryItem(request, id, patch)
      if (!active.current) return
      setItems((previous) => previous.map((entry) => entry.id === id ? { ...entry, ...item } : entry))
      setSelected((previous) => previous?.id === id ? { ...previous, ...item } : previous)
      setRenaming(false)
      notify('已保存')
      loadLatest.current()
    } catch (err) { notify(err.message || '保存失败') }
    finally { if (active.current) setBusy(false) }
  }

  function rename(event) {
    event.preventDefault()
    const title = titleDraft.trim()
    if (!title || title.length > 200) { notify('标题需为 1–200 字'); return }
    updateSelected({ title })
  }

  async function remove() {
    if (!deleting || busy) return
    setBusy(true)
    const item = deleting
    try {
      await request(`/files/${encodeURIComponent(item.id)}`, { method: 'DELETE' })
      if (!active.current) return
      setItems((previous) => previous.filter((entry) => entry.id !== item.id))
      setCounts((previous) => ({ ...previous, all: Math.max(0, (previous.all || 0) - 1), [libraryType(item)]: Math.max(0, (previous[libraryType(item)] || 0) - 1) }))
      setDeleting(null)
      closePreview()
      notify('已删除')
      loadLatest.current()
    } catch (err) { notify(err.message || '删除失败') }
    finally { if (active.current) setBusy(false) }
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); notify('已复制') }
    catch { notify('复制失败') }
  }

  async function openSession(sessionId) {
    try { await onOpenSession(sessionId); closePreview() }
    catch (err) { notify(err.message || '打开对话失败') }
  }

  return <section className="library-page" aria-label="资源库">
    <aside className="library-sidebar"><label className="library-search"><Icon name="search" size={18} /><input type="search" placeholder="搜索资源" aria-label="搜索资源" value={search} onChange={(event) => setSearch(event.target.value)} /></label><LibraryCategories counts={counts} type={type} onChange={setType} /></aside>
    <div className="library-main">
      <header className="library-header"><h1>资源库</h1><div className="library-tools"><label className="library-sort"><span className="library-sr-only">资源排序</span><select value={sort} onChange={(event) => setSort(event.target.value)}><option value="recent">最近打开</option><option value="created">创建时间</option><option value="title">标题</option></select></label><div className="library-view" aria-label="显示方式"><button type="button" aria-label="网格视图" aria-pressed={view === 'grid'} onClick={() => changeView('grid')}><Icon name="grid" size={18} /></button><button type="button" aria-label="列表视图" aria-pressed={view === 'list'} onClick={() => changeView('list')}><Icon name="menu" size={18} /></button></div></div></header>
      {error && <div className="library-error" role="alert">{error}<button type="button" className="library-button" onClick={() => load()} disabled={loading}>重试</button></div>}
      {['pinned', 'recent'].map((key) => groups[key].length > 0 && <section className="library-section" key={key}><h2>{key === 'pinned' ? '置顶' : '最近'}</h2><div className={`library-items ${view}`}>{groups[key].map((item) => <LibraryCard key={item.id} item={item} requestBlob={requestBlob} onPreview={openPreview} onOpenSession={openSession} />)}</div></section>)}
      {!loading && !error && items.length === 0 && <div className="library-empty"><ContentIcon name={category.icon} size={32} /><p>{query ? '没有找到匹配的资源，试试其他关键词。' : category.empty}</p></div>}
      {loading && <p className="library-loading" role="status">正在加载资源…</p>}
      {cursor && !loading && <button type="button" className="library-load-more library-button" onClick={() => load(cursor)}>加载更多</button>}
    </div>
    {selected && <ContentDialog title={itemTitle(selected)} className="library-preview-dialog" onClose={closePreview}>
      <div className="library-preview-actions"><button type="button" className="library-button" onClick={() => download(selected)}><Icon name="download" size={16} />下载</button><ActionMenu label="资源操作"><button type="button" disabled={busy} onClick={() => updateSelected({ pinned: !selected.pinned })}>{selected.pinned ? '取消置顶' : '置顶'}</button><button type="button" disabled={busy} onClick={() => { setTitleDraft(itemTitle(selected)); setRenaming(true) }}>重命名</button>{selected.session_id && <button type="button" onClick={() => openSession(selected.session_id)}>跳到对话</button>}<button type="button" className="library-danger" disabled={busy} onClick={() => setDeleting(selected)}>删除</button></ActionMenu></div>
      {renaming && <form className="library-rename" data-escape-local onSubmit={rename} onKeyDown={(event) => { if (event.key === 'Escape') { event.stopPropagation(); setRenaming(false) } }}><input autoFocus aria-label="资源标题" maxLength={200} value={titleDraft} onChange={(event) => setTitleDraft(event.target.value)} /><button type="submit" className="library-button" disabled={busy || !titleDraft.trim()}>保存</button><button type="button" className="library-button" onClick={() => setRenaming(false)}>取消</button></form>}
      {previewLoading ? <p className="library-loading" role="status">正在加载预览…</p> : previewError ? <div className="library-error" role="alert">{previewError}<button type="button" className="library-button" onClick={() => openPreview(selected)}>重试</button></div> : preview && <LibraryPreview preview={preview} imageUrl={previewImage.url} imageLoading={previewImage.loading} onDownload={() => download(selected)} onCopy={copy} />}
    </ContentDialog>}
    {deleting && <ContentDialog title="删除资源" onClose={() => { if (!busy) setDeleting(null) }}><p>确定删除「{itemTitle(deleting)}」吗？删除后无法恢复。</p><div className="library-confirm-actions"><button type="button" className="library-button" disabled={busy} onClick={() => setDeleting(null)}>取消</button><button type="button" className="library-button library-danger" disabled={busy} onClick={remove}>{busy ? '正在删除…' : '确认删除'}</button></div></ContentDialog>}
  </section>
}
