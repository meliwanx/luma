import React, { useCallback, useEffect, useRef, useState } from 'react'
import Icon from './icons.jsx'
import Markdown from './markdown.jsx'
import { ContentDialog, ActionMenu, ContentIcon } from './content-ui.jsx'
import { PROACTIVE_DEFAULTS, relativeTime } from './content-data.js'
import { feedPath, feedSources, mergeFeedPosts } from './feed-data.js'
import './feed.css'

export function FeedSources({ sources }) {
  const links = feedSources(sources)
  return links.length ? <ul className="feed-sources" aria-label="来源链接">{links.map((source, index) => <li key={`${source.url}-${index}`}><a href={source.url} target="_blank" rel="noopener noreferrer">{source.title}</a></li>)}</ul> : null
}

export function FeedPostCard({ post, busy, onFeedback, onDiscuss, onReason, onDelete }) {
  return <article className="feed-post">
    <header className="feed-post-header">
      <span className="feed-post-icon"><ContentIcon name={post.icon} size={21} /></span>
      <div className="feed-post-heading"><h3>{post.title}</h3><time dateTime={post.created_at}>{relativeTime(post.created_at)}</time></div>
      <ActionMenu label={`动态「${post.title}」的操作`}>
        <button type="button" onClick={() => onReason(post)}>为什么推给我</button>
        <button type="button" disabled={busy} onClick={() => onFeedback(post, 'not_interested')}>不感兴趣</button>
        <button type="button" disabled={busy} onClick={() => onDelete(post)}>删除</button>
      </ActionMenu>
    </header>
    <div className="feed-post-body"><Markdown text={post.body_markdown || ''} /></div>
    <FeedSources sources={post.sources} />
    <footer className="feed-post-footer">
      <button type="button" className={`feed-like ${post.liked ? 'liked' : ''}`} aria-label={post.liked ? '取消喜欢' : '喜欢'} aria-pressed={Boolean(post.liked)} disabled={busy} onClick={() => onFeedback(post, post.liked ? 'unlike' : 'like')}><ContentIcon name="heart" size={18} />{post.liked ? '已喜欢' : '喜欢'}</button>
      <button type="button" disabled={busy} onClick={() => onDiscuss(post)}><Icon name="chat" size={18} />讨论</button>
    </footer>
  </article>
}

export default function FeedPage({ request, notify, onOpenSession }) {
  const [posts, setPosts] = useState([])
  const [cursor, setCursor] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(new Set())
  const [refreshing, setRefreshing] = useState(false)
  const [reason, setReason] = useState(null)
  const [deletePost, setDeletePost] = useState(null)
  const [instructionsOpen, setInstructionsOpen] = useState(false)
  const [instructions, setInstructions] = useState('')
  const [prefsLoading, setPrefsLoading] = useState(false)
  const [prefsError, setPrefsError] = useState('')
  const [saving, setSaving] = useState(false)
  const mounted = useRef(false)
  const fetching = useRef(false)
  const generation = useRef(0)
  const prefsGeneration = useRef(0)
  const sentinel = useRef(null)
  const pendingPosts = useRef(new Set())

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; generation.current += 1; prefsGeneration.current += 1 }
  }, [])

  const load = useCallback(async (next = '') => {
    if (fetching.current) return
    fetching.current = true
    const token = generation.current
    setLoading(true)
    setError('')
    try {
      const data = await request(feedPath(next))
      if (!mounted.current || token !== generation.current) return
      setPosts((current) => next ? mergeFeedPosts(current, data.items || []) : (data.items || []))
      setCursor(data.next_cursor && data.next_cursor !== next ? data.next_cursor : null)
    } catch (failure) {
      if (mounted.current && token === generation.current) setError(failure.message || '动态加载失败，请重试')
    } finally {
      if (token === generation.current) {
        fetching.current = false
        if (mounted.current) setLoading(false)
      }
    }
  }, [request])

  useEffect(() => {
    generation.current += 1
    fetching.current = false
    setPosts([])
    setCursor(null)
    load()
    return () => { generation.current += 1; fetching.current = false }
  }, [load])

  useEffect(() => {
    if (!cursor || loading || error || !sentinel.current || typeof IntersectionObserver === 'undefined') return undefined
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) load(cursor)
    }, { rootMargin: '240px' })
    observer.observe(sentinel.current)
    return () => observer.disconnect()
  }, [cursor, loading, error, load])

  async function withPost(post, action) {
    if (pendingPosts.current.has(post.id)) return
    pendingPosts.current.add(post.id)
    setBusy(new Set(pendingPosts.current))
    try {
      await action()
    } catch (failure) {
      if (mounted.current) notify(failure.message || '操作失败，请重试')
    } finally {
      pendingPosts.current.delete(post.id)
      if (mounted.current) setBusy(new Set(pendingPosts.current))
    }
  }

  function feedback(post, action) {
    return withPost(post, async () => {
      await request(`/feed/${encodeURIComponent(post.id)}/feedback`, { method: 'POST', body: JSON.stringify({ action }) })
      if (!mounted.current) return
      setPosts((current) => action === 'not_interested' ? current.filter((item) => item.id !== post.id) : current.map((item) => item.id === post.id ? { ...item, liked: action === 'like' } : item))
      if (action === 'not_interested') notify('以后会少推荐类似内容')
    })
  }

  function discuss(post) {
    return withPost(post, async () => {
      const data = await request(`/feed/${encodeURIComponent(post.id)}/discuss`, { method: 'POST' })
      if (mounted.current) await onOpenSession(data.session_id, { focus: true })
    })
  }

  function remove() {
    if (!deletePost) return
    const post = deletePost
    return withPost(post, async () => {
      await request(`/feed/${encodeURIComponent(post.id)}`, { method: 'DELETE' })
      if (!mounted.current) return
      setPosts((current) => current.filter((item) => item.id !== post.id))
      setDeletePost(null)
      notify('已删除')
    })
  }

  async function refresh() {
    if (refreshing) return
    setRefreshing(true)
    try {
      await request('/feed/refresh', { method: 'POST' })
      if (mounted.current) notify('正在为你准备一篇新动态')
    } catch (failure) {
      if (mounted.current) notify(failure.status === 429 ? '今天刷新的次数用完了' : (failure.message || '刷新失败，请重试'))
    } finally {
      if (mounted.current) setRefreshing(false)
    }
  }

  async function openInstructions() {
    const token = ++prefsGeneration.current
    setInstructionsOpen(true)
    setPrefsLoading(true)
    setPrefsError('')
    setInstructions('')
    try {
      const prefs = await request('/proactive/prefs')
      if (mounted.current && token === prefsGeneration.current) setInstructions(prefs.feed_instructions || '')
    } catch (failure) {
      if (mounted.current && token === prefsGeneration.current) setPrefsError(failure.message || '动态说明加载失败')
    } finally {
      if (mounted.current && token === prefsGeneration.current) setPrefsLoading(false)
    }
  }

  function closeInstructions() {
    if (saving) return
    prefsGeneration.current += 1
    setInstructionsOpen(false)
  }

  async function saveInstructions(event) {
    event.preventDefault()
    if (prefsLoading || saving || prefsError) return
    setSaving(true)
    try {
      const latest = await request('/proactive/prefs')
      await request('/proactive/prefs', { method: 'PUT', body: JSON.stringify({ ...PROACTIVE_DEFAULTS, ...latest, feed_instructions: instructions }) })
      if (mounted.current) { setInstructionsOpen(false); notify('已保存') }
    } catch (failure) {
      if (mounted.current) notify(failure.message || '保存失败，请重试')
    } finally {
      if (mounted.current) setSaving(false)
    }
  }

  return <section className="feed-page">
    <header className="feed-page-header"><h2>动态</h2><div className="feed-page-actions">
      <button type="button" className="feed-icon-button" onClick={openInstructions} aria-label="动态说明" title="动态说明"><Icon name="sliders" size={19} /></button>
      <button type="button" className="feed-icon-button" disabled={refreshing} onClick={refresh} aria-label="刷新动态" title="刷新动态"><Icon name="refresh" size={19} /></button>
    </div></header>
    {!posts.length && !loading && !error && <p className="feed-empty">我会每天根据你关心的话题写点东西。也可以点右上角告诉我你想看什么。</p>}
    <div className="feed-posts">{posts.map((post) => <FeedPostCard key={post.id} post={post} busy={busy.has(post.id)} onFeedback={feedback} onDiscuss={discuss} onReason={setReason} onDelete={setDeletePost} />)}</div>
    {error && <div className="feed-error" role="alert"><p>{error}</p><button type="button" className="secondary-btn" onClick={() => load(cursor || '')}>重试</button></div>}
    {loading && <p className="feed-loading" role="status">正在加载动态…</p>}
    <div ref={sentinel} className="feed-load-more">{cursor && !error && <button type="button" className="secondary-btn" disabled={loading} onClick={() => load(cursor)}>{loading ? '加载中…' : '加载更多'}</button>}</div>
    {reason && <ContentDialog title="为什么推给我" onClose={() => setReason(null)}><p className="feed-dialog-text">{reason.reason || '这篇动态与你关心的话题有关。'}</p></ContentDialog>}
    {deletePost && <ContentDialog title="删除动态" onClose={() => { if (!busy.has(deletePost.id)) setDeletePost(null) }}><p className="feed-dialog-text">确定删除「{deletePost.title}」吗？</p><div className="feed-dialog-actions"><button type="button" className="secondary-btn" disabled={busy.has(deletePost.id)} onClick={() => setDeletePost(null)}>取消</button><button type="button" className="primary-btn" disabled={busy.has(deletePost.id)} onClick={remove}>{busy.has(deletePost.id) ? '删除中…' : '删除'}</button></div></ContentDialog>}
    {instructionsOpen && <ContentDialog title="动态说明" onClose={closeInstructions}><form onSubmit={saveInstructions} className="feed-instructions"><label htmlFor="feed-instructions">告诉我你想看什么</label>{prefsLoading && <p role="status">正在加载…</p>}{prefsError && <div className="feed-error" role="alert"><p>{prefsError}</p><button type="button" className="secondary-btn" onClick={openInstructions}>重试</button></div>}<textarea id="feed-instructions" value={instructions} onChange={(event) => setInstructions(event.target.value)} maxLength={2000} rows={7} disabled={prefsLoading || saving || Boolean(prefsError)} placeholder="例如：关注技术进展、行业趋势，每篇简明一些。" /><small>{instructions.length} / 2000</small><div className="feed-dialog-actions"><button type="button" className="secondary-btn" onClick={closeInstructions} disabled={saving}>取消</button><button type="submit" className="primary-btn" disabled={prefsLoading || saving || Boolean(prefsError)}>{saving ? '保存中…' : '保存'}</button></div></form></ContentDialog>}
  </section>
}
