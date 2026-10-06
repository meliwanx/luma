import React, { useEffect, useRef, useState } from 'react'
import Icon from './icons.jsx'
import Markdown from './markdown.jsx'
import { ActionMenu, ContentDialog, ContentIcon } from './content-ui.jsx'
import { createIdeasPoller, normalizeIdeas, updateIdeaStatus } from './ideas-data.js'
import './ideas.css'

function IdeaRow({ idea, onOpen, onFeedback, busy }) {
  return <article className="ideas-row">
    <span className="ideas-row-icon"><ContentIcon name={idea.icon} size={22} /></span>
    <button type="button" className="ideas-row-copy" onClick={() => onOpen(idea)}>
      <strong>{idea.title}</strong><span>{idea.summary}</span>
    </button>
    <ActionMenu label={`点子操作：${idea.title}`}>
      <button type="button" disabled={busy} onClick={() => onFeedback(idea, 'more_like')}>更多类似</button>
      <button type="button" disabled={busy} onClick={() => onFeedback(idea, 'not_interested')}>不感兴趣</button>
    </ActionMenu>
  </article>
}

export default function IdeasPage({ request, notify, onOpenSession, onStart }) {
  const [data, setData] = useState(() => normalizeIdeas(null))
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [selected, setSelected] = useState(null)
  const [busy, setBusy] = useState(false)
  const poller = useRef(null)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    const controller = createIdeasPoller({
      load: () => request('/ideas'),
      onData: (next) => { setData(next); setLoading(false); setError('') },
      onError: (failure) => { setError(failure?.message || '点子加载失败，请稍后重试'); setLoading(false) },
    })
    poller.current = controller
    controller.refresh()
    return () => { mounted.current = false; controller.stop(); poller.current = null }
  }, [request])

  async function refresh() {
    if (refreshing) return
    setRefreshing(true)
    try {
      await request('/ideas/refresh', { method: 'POST' })
      if (!mounted.current) return
      setData((current) => ({ ...current, generating: true }))
      setError('')
      notify('正在为你想新的点子…')
      await poller.current?.refresh()
    } catch (failure) {
      if (mounted.current) notify(failure?.status === 429 ? '今天换的次数用完了' : failure?.message || '换一批失败，请稍后重试')
    } finally {
      if (mounted.current) setRefreshing(false)
    }
  }

  async function feedback(idea, action) {
    if (busy) return
    setBusy(true)
    try {
      await request(`/ideas/${encodeURIComponent(idea.id)}/feedback`, { method: 'POST', body: JSON.stringify({ action }) })
      if (!mounted.current) return
      if (action === 'not_interested') {
        setData((current) => updateIdeaStatus(current, idea.id, 'dismissed'))
        setSelected((current) => current?.id === idea.id ? null : current)
      }
      notify(action === 'more_like' ? '记下了，会为你想更多类似的点子' : '记下了，以后会少推荐这类点子')
    } catch (failure) {
      if (mounted.current) notify(failure?.message || '反馈失败，请稍后重试')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function start() {
    if (!selected || busy) return
    setBusy(true)
    try {
      const result = await request(`/ideas/${encodeURIComponent(selected.id)}/start`, { method: 'POST' })
      if (!mounted.current) return
      if (!result?.session_id || typeof result.prompt !== 'string' || !result.prompt.trim()) throw new Error('点子启动失败，请稍后重试')
      setData((current) => updateIdeaStatus(current, selected.id, 'started'))
      await onStart({ session_id: result.session_id, prompt: result.prompt })
      if (mounted.current) setSelected(null)
    } catch (failure) {
      if (mounted.current) notify(failure?.message || '点子启动失败，请稍后重试')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function openSource(sessionId) {
    if (!sessionId) return
    try { await onOpenSession(sessionId); if (mounted.current) setSelected(null) } catch (failure) { if (mounted.current) notify(failure?.message || '无法打开来源对话') }
  }

  return <section className="ideas-page">
    <header className="ideas-page-header">
      <div><h1>点子</h1><p>我会根据我们的对话，想一些能帮你的事放在这里。</p></div>
      <button type="button" className="ideas-refresh" disabled={refreshing} onClick={refresh}><Icon name="spark" size={16} />{refreshing ? '正在准备…' : '换一批'}</button>
    </header>
    {data.generating && <p className="ideas-generating" role="status"><Icon name="loader" size={16} />正在为你想新的点子…</p>}
    {error && <div className="ideas-error" role="alert"><span>{error}</span><button type="button" onClick={() => poller.current?.refresh()}>重试</button></div>}
    {loading ? <p className="ideas-empty" role="status">正在加载点子…</p> : <>
      <section className="ideas-section" aria-label="为你准备的点子"><h2>为你准备</h2>
        {data.featured.length ? <div className="ideas-list">{data.featured.map((idea) => <IdeaRow key={idea.id} idea={idea} onOpen={setSelected} onFeedback={feedback} busy={busy} />)}</div> : <p className="ideas-empty">多聊几次，我会更懂你需要什么</p>}
      </section>
      {data.groups.map((group) => <section key={group.name} className="ideas-section" aria-label={group.name}><h2>{group.name}</h2><div className="ideas-list">{group.items.map((idea) => <IdeaRow key={idea.id} idea={idea} onOpen={setSelected} onFeedback={feedback} busy={busy} />)}</div></section>)}
    </>}
    {selected && <ContentDialog title={selected.title} onClose={() => { if (!busy) setSelected(null) }} className="ideas-detail">
      <p className="ideas-detail-summary">{selected.summary}</p>
      <div className="ideas-detail-plan"><Markdown text={selected.plan_markdown || ''} /></div>
      {selected.sources?.length > 0 && <section className="ideas-sources"><h3>灵感来自</h3>{selected.sources.filter((source) => source?.session_id).map((source, index) => <button key={`${source.session_id}-${index}`} type="button" onClick={() => openSource(source.session_id)}><Icon name="chat" size={15} /><span>{source.session_title || '来源对话'}</span><Icon name="chevron-right" size={14} /></button>)}</section>}
      <footer className="ideas-detail-footer">
        <ActionMenu label="这个点子的更多操作"><button type="button" disabled={busy} onClick={() => feedback(selected, 'more_like')}>更多类似</button><button type="button" disabled={busy} onClick={() => feedback(selected, 'not_interested')}>不感兴趣</button></ActionMenu>
        <button type="button" className="ideas-start" disabled={busy} onClick={start}>{busy ? '处理中…' : '马上开始'}<Icon name="arrow-up-right" size={16} /></button>
      </footer>
    </ContentDialog>}
  </section>
}
