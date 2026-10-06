import React, { useEffect, useMemo, useRef, useState } from 'react'
import Icon from './icons.jsx'

/** Return the timestamp used for ordering a session in the chat surfaces. */
export function sessionTimestamp(session) {
  return session?.last_message_at || session?.updated_at || session?.created_at || ''
}

/**
 * Format a timestamp in the short Chinese form used throughout the Muse chat
 * surfaces.  This intentionally uses the browser's local timezone, just like
 * the rest of the workspace timeline.
 */
export function relativeTime(value, now = Date.now()) {
  if (!value) return ''
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const delta = Math.max(0, now - date.getTime())
  const minute = 60 * 1000
  const hour = 60 * minute
  const day = 24 * hour
  if (delta < minute) return '刚刚'
  if (delta < hour) return `${Math.max(1, Math.floor(delta / minute))}分钟前`
  if (delta < day) return `${Math.max(1, Math.floor(delta / hour))}小时前`

  const today = new Date(now)
  const start = (item) => new Date(item.getFullYear(), item.getMonth(), item.getDate()).getTime()
  const days = Math.max(1, Math.floor((start(today) - start(date)) / day))
  if (days === 1) return '昨天'
  if (days <= 6) return `${days}天前`

  const currentYear = today.getFullYear()
  const month = date.getMonth() + 1
  const dateNumber = date.getDate()
  return date.getFullYear() === currentYear
    ? `${month}月${dateNumber}日`
    : `${date.getFullYear()}年${month}月${dateNumber}日`
}

function sessionTitle(session) {
  return session?.kind === 'main' ? '主聊天' : (session?.title || '新旁聊')
}

function sessionPreview(session) {
  const preview = typeof session?.last_message_preview === 'string' ? session.last_message_preview.trim() : ''
  return preview || '还没有消息'
}

function sessionSort(a, b) {
  const left = new Date(sessionTimestamp(a)).getTime()
  const right = new Date(sessionTimestamp(b)).getTime()
  return (Number.isNaN(right) ? 0 : right) - (Number.isNaN(left) ? 0 : left)
}

/**
 * Sliding chat list.  API work is deliberately kept in the parent so this
 * component remains useful in the web and Electron shells and can be tested
 * with a static session list.
 */
export function ChatsPanel({
  sessions = [],
  currentSessionId = '',
  generatingBySession = new Map(),
  completedSessions = new Set(),
  onOpenSession,
  onOpenSearch,
  onCreateSideChat,
  onRenameSession,
  onDeleteSession,
  onClose,
  mobile = false,
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [sessionMenu, setSessionMenu] = useState('')
  const [deleting, setDeleting] = useState(null)
  const [renamingSessionId, setRenamingSessionId] = useState('')
  const [renameDraft, setRenameDraft] = useState('')
  const renameInputRef = useRef(null)
  const renameCommitRef = useRef(false)
  const panelRef = useRef(null)

  const mainSession = sessions.find((item) => item.kind === 'main')
  const sideSessions = sessions.filter((item) => item.kind !== 'main').sort(sessionSort)

  useEffect(() => {
    function closeMenus(event) {
      if (!panelRef.current?.contains(event.target)) return
      if (!event.target.closest('.chats-panel-menu') && !event.target.closest('.chats-panel-session-more')) {
        setMenuOpen(false)
        setSessionMenu('')
      }
    }
    document.addEventListener('mousedown', closeMenus)
    return () => document.removeEventListener('mousedown', closeMenus)
  }, [])

  async function createSideChat() {
    setMenuOpen(false)
    await onCreateSideChat?.()
  }

  async function deleteSession() {
    if (!deleting) return
    const session = deleting
    setDeleting(null)
    setSessionMenu('')
    await onDeleteSession?.(session)
  }

  function startRename(session) {
    setSessionMenu('')
    renameCommitRef.current = false
    setRenamingSessionId(session.id)
    setRenameDraft(session.title || '新旁聊')
  }

  useEffect(() => {
    if (!renamingSessionId) return undefined
    const timer = window.setTimeout(() => {
      renameInputRef.current?.focus()
      renameInputRef.current?.select()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [renamingSessionId])

  async function finishRename(session) {
    if (renameCommitRef.current) return
    renameCommitRef.current = true
    const title = renameDraft.trim()
    const original = (session.title || '新旁聊').trim()
    setRenamingSessionId('')
    setRenameDraft('')
    if (!title || title === original) return
    await onRenameSession?.(session, title)
  }

  function cancelRename() {
    renameCommitRef.current = true
    setRenamingSessionId('')
    setRenameDraft('')
  }

  return (
    <>
      <div className={`chats-panel-scrim ${mobile ? 'mobile' : ''}`} aria-hidden="true" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose?.() }} />
      <aside ref={panelRef} className="chats-panel" aria-label="聊天面板">
        <div className="chats-panel-header">
          <button type="button" className="chats-panel-search" onClick={() => onOpenSearch?.()}>
            <Icon name="search" size={17} />
            <span>搜索聊天</span>
            <kbd>⌘K</kbd>
          </button>
          <div className="chats-panel-menu">
            <button type="button" className="chats-panel-menu-button" aria-label="聊天菜单" aria-expanded={menuOpen} onClick={() => setMenuOpen((open) => !open)}><Icon name="more-horizontal" size={18} /></button>
            {menuOpen && <div className="chats-panel-menu-popover" role="menu"><button type="button" role="menuitem" onClick={createSideChat}><Icon name="plus" size={16} />新建旁聊</button></div>}
          </div>
        </div>

        <div className="chats-panel-scroll">
          <section className="chats-panel-section" aria-labelledby="chats-panel-main-heading">
            <h2 id="chats-panel-main-heading" className="chats-panel-section-title">主要聊天</h2>
            {mainSession ? (
              <SessionRow
                session={mainSession}
                active={mainSession.id === currentSessionId}
                generating={generatingBySession.has(mainSession.id)}
                completed={completedSessions.has(mainSession.id)}
                onOpenSession={onOpenSession}
                onClose={onClose}
              />
            ) : <div className="chats-panel-empty chats-panel-empty-main">主聊天尚未准备好</div>}
          </section>

          <section className="chats-panel-section" aria-labelledby="chats-panel-side-heading">
            <div className="chats-panel-section-title-row">
              <h2 id="chats-panel-side-heading" className="chats-panel-section-title">旁聊</h2>
              <button type="button" className="chats-panel-add" aria-label="新建旁聊" onClick={createSideChat}><Icon name="plus" size={17} /></button>
            </div>
            {sideSessions.length ? sideSessions.map((session) => (
              <SessionRow
                key={session.id}
                session={session}
                active={session.id === currentSessionId}
                generating={generatingBySession.has(session.id)}
                completed={completedSessions.has(session.id)}
                menuOpen={sessionMenu === session.id}
                onOpenSession={onOpenSession}
                onClose={onClose}
                onMenu={() => { setSessionMenu((id) => id === session.id ? '' : session.id); setMenuOpen(false) }}
                renaming={renamingSessionId === session.id}
                renameValue={renameDraft}
                renameInputRef={renameInputRef}
                onRenameChange={setRenameDraft}
                onRenameSubmit={() => void finishRename(session)}
                onRenameCancel={cancelRename}
                onRename={() => startRename(session)}
                onDelete={() => setDeleting(session)}
              />
            )) : (
              <div className="chats-panel-empty">
                <strong>发起旁聊</strong>
                <span>旁聊是按主题整理对话的可选方式。</span>
              </div>
            )}
          </section>
        </div>

        {deleting && <div className="chats-panel-confirm-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setDeleting(null) }}>
          <section className="chats-panel-confirm-dialog mcp-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-chat-title">
            <h3 id="delete-chat-title">删除旁聊？</h3>
            <p>「{sessionTitle(deleting)}」中的消息将一并删除。</p>
            <div className="chats-panel-confirm-actions mcp-confirm-actions">
              <button type="button" className="secondary-btn" onClick={() => setDeleting(null)}>取消</button>
              <button type="button" className="primary-btn danger chats-panel-danger" onClick={deleteSession}>删除</button>
            </div>
          </section>
        </div>}
      </aside>
    </>
  )
}

function SessionRow({ session, active, generating, completed, menuOpen, onOpenSession, onClose, onMenu, onRename, onDelete, renaming, renameValue, renameInputRef, onRenameChange, onRenameSubmit, onRenameCancel }) {
  const isSide = session.kind !== 'main'
  return (
    <div className={`chats-panel-session-row ${active ? 'active' : ''}`}>
      <button type="button" className="chats-panel-session-button" onClick={() => { onOpenSession?.(session.id); onClose?.() }} aria-current={active ? 'page' : undefined}>
        <span className="chats-panel-session-icon"><Icon name="chat" size={17} /></span>
        <span className="chats-panel-session-copy">
          {renaming ? <input
            ref={renameInputRef}
            className="chats-panel-session-title-input"
            value={renameValue}
            aria-label="旁聊标题"
            onChange={(event) => onRenameChange?.(event.target.value)}
            onClick={(event) => event.stopPropagation()}
            onBlur={onRenameSubmit}
            onKeyDown={(event) => {
              if (event.key === 'Enter') { event.preventDefault(); onRenameSubmit?.() }
              if (event.key === 'Escape') { event.preventDefault(); onRenameCancel?.() }
            }}
          /> : <strong className="chats-panel-session-title">{sessionTitle(session)}</strong>}
          <span className="chats-panel-session-preview">{sessionPreview(session)}</span>
        </span>
        {generating ? <span className="session-generation-spinner" role="status" aria-label="正在生成回复" /> : completed && <span className="session-completed-dot" role="status" aria-label="已完成回复" />}
        <time className="chats-panel-session-time" dateTime={sessionTimestamp(session) || undefined}>{relativeTime(sessionTimestamp(session))}</time>
      </button>
      {isSide && <div className="chats-panel-session-actions">
        <button type="button" className="chats-panel-session-more" aria-label={`管理${sessionTitle(session)}`} aria-expanded={menuOpen} onClick={(event) => { event.stopPropagation(); onMenu?.() }}><Icon name="more-horizontal" size={17} /></button>
        {menuOpen && <div className="chats-panel-menu-popover chats-panel-session-popover" role="menu">
          <button type="button" role="menuitem" onClick={() => onRename?.()}>重命名</button>
          <button type="button" role="menuitem" className="danger" onClick={() => onDelete?.()}>删除</button>
        </div>}
      </div>}
    </div>
  )
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Highlight plain text by rendering text segments and mark elements. */
export function highlightText(text, query) {
  const source = String(text || '')
  const needle = String(query || '').trim()
  if (!needle) return source
  const pattern = new RegExp(`(${escapeRegExp(needle)})`, 'ig')
  const lowerNeedle = needle.toLocaleLowerCase()
  return source.split(pattern).map((part, index) => part.toLocaleLowerCase() === lowerNeedle
    ? <mark key={`${part}-${index}`} className="search-palette-mark">{part}</mark>
    : <React.Fragment key={`${part}-${index}`}>{part}</React.Fragment>)
}

function normaliseResults(result) {
  return {
    sessions: Array.isArray(result?.sessions) ? result.sessions : [],
    messages: Array.isArray(result?.messages) ? result.messages : [],
  }
}

/**
 * Command-palette search for sessions and messages. `requestSearch` receives
 * `(query, { signal })`; the parent owns authentication and API base URL.
 */
export function SearchPalette({
  open = false,
  sessions = [],
  onClose,
  onOpenSession,
  onOpenMessage,
  requestSearch,
}) {
  const inputRef = useRef(null)
  const abortRef = useRef(null)
  const requestIdRef = useRef(0)
  const [query, setQuery] = useState('')
  const [result, setResult] = useState(null)
  const [status, setStatus] = useState('idle')
  const [selected, setSelected] = useState(0)

  useEffect(() => {
    if (!open) {
      abortRef.current?.abort()
      setQuery('')
      setResult(null)
      setStatus('idle')
      setSelected(0)
      return undefined
    }
    const timer = window.setTimeout(() => inputRef.current?.focus(), 0)
    return () => window.clearTimeout(timer)
  }, [open])

  useEffect(() => {
    if (!open) return undefined
    const trimmed = query.trim()
    abortRef.current?.abort()
    if (!trimmed) {
      setResult(null)
      setStatus('idle')
      setSelected(0)
      return undefined
    }
    // Hide the previous query's rows during the debounce window so an
    // in-flight response can never look like an answer to the new query.
    setStatus('loading')
    const requestId = ++requestIdRef.current
    const controller = new AbortController()
    abortRef.current = controller
    const timer = window.setTimeout(async () => {
      try {
        const payload = requestSearch
          ? await requestSearch(trimmed, { signal: controller.signal })
          : await fetch(`/api/v1/search?q=${encodeURIComponent(trimmed)}&limit=20`, { headers: { Accept: 'application/json' }, credentials: 'include', signal: controller.signal }).then((response) => {
            if (!response.ok) throw new Error(`搜索失败（${response.status}）`)
            return response.json()
          })
        if (controller.signal.aborted || requestId !== requestIdRef.current) return
        setResult(normaliseResults(payload))
        setSelected(0)
        setStatus('ready')
      } catch (error) {
        if (error?.name === 'AbortError' || controller.signal.aborted || requestId !== requestIdRef.current) return
        setStatus('error')
      }
    }, 250)
    return () => window.clearTimeout(timer)
  }, [open, query, requestSearch])

  const recent = useMemo(() => sessions.filter(Boolean).slice().sort(sessionSort).slice(0, 8), [sessions])
  const items = useMemo(() => {
    if (!query.trim()) return recent.map((session) => ({ type: 'session', session }))
    const found = result || { sessions: [], messages: [] }
    return [
      ...found.sessions.map((session) => ({ type: 'session', session })),
      ...found.messages.map((message) => ({ type: 'message', message })),
    ]
  }, [query, recent, result])

  useEffect(() => {
    setSelected((index) => Math.min(Math.max(0, items.length - 1), index))
  }, [items.length])

  if (!open) return null

  async function selectItem(item) {
    if (!item) return
    onClose?.()
    if (item.type === 'session') {
      await onOpenSession?.(item.session.id)
      return
    }
    const message = item.message
    await onOpenMessage?.(message)
  }

  function onKeyDown(event) {
    if (event.key === 'Escape') { event.preventDefault(); onClose?.(); return }
    if (event.key === 'ArrowDown') { event.preventDefault(); setSelected((index) => items.length ? (index + 1) % items.length : 0); return }
    if (event.key === 'ArrowUp') { event.preventDefault(); setSelected((index) => items.length ? (index - 1 + items.length) % items.length : 0); return }
    if (event.key === 'Enter' && items.length) { event.preventDefault(); void selectItem(items[selected]) }
  }

  const trimmed = query.trim()
  const emptyResults = trimmed && status === 'ready' && items.length === 0

  return (
    <div className="search-palette-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose?.() }}>
      <section className="search-palette" role="dialog" aria-modal="true" aria-labelledby="search-palette-title">
        <header className="search-palette-header">
          <Icon name="search" size={20} />
          <label id="search-palette-title" className="search-palette-input-label"><span className="sr-only">搜索</span><input ref={inputRef} className="search-palette-input" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={onKeyDown} placeholder="搜索" autoComplete="off" /></label>
          <kbd className="search-palette-kbd">Esc</kbd>
        </header>
        <div className="search-palette-body">
          {!trimmed && <SearchSection title="最近访问">{recent.length ? recent.map((session, index) => <SearchSessionResult key={session.id} session={session} active={selected === index} onMouseEnter={() => setSelected(index)} onClick={() => void selectItem({ type: 'session', session })} />) : <p className="search-palette-empty">还没有访问记录</p>}</SearchSection>}
          {trimmed && status === 'loading' && <p className="search-palette-loading">搜索中…</p>}
          {trimmed && status === 'error' && <p className="search-palette-error">搜索出错，请稍后重试</p>}
          {trimmed && status === 'ready' && <>
            {result?.sessions?.length > 0 && <SearchSection title="对话">{result.sessions.map((session, index) => <SearchSessionResult key={session.id} session={session} active={selected === index} query={trimmed} onMouseEnter={() => setSelected(index)} onClick={() => void selectItem({ type: 'session', session })} />)}</SearchSection>}
            {result?.messages?.length > 0 && <SearchSection title="消息">{result.messages.map((message, index) => {
              const itemIndex = (result.sessions || []).length + index
              return <SearchMessageResult key={message.id} message={message} query={trimmed} active={selected === itemIndex} onMouseEnter={() => setSelected(itemIndex)} onClick={() => void selectItem({ type: 'message', message })} />
            })}</SearchSection>}
            {emptyResults && <p className="search-palette-empty">没有找到相关内容</p>}
          </>}
        </div>
      </section>
    </div>
  )
}

function SearchSection({ title, children }) {
  return <section className="search-palette-section"><h2>{title}</h2><div>{children}</div></section>
}

function SearchSessionResult({ session, query, active, onMouseEnter, onClick }) {
  return <button type="button" className={`search-palette-result ${active ? 'active' : ''}`} onMouseEnter={onMouseEnter} onClick={onClick}>
    <span className="search-palette-result-icon"><Icon name="chat" size={17} /></span>
    <span className="search-palette-result-copy"><strong className="search-palette-result-title">{query ? highlightText(sessionTitle(session), query) : sessionTitle(session)}</strong><span className="search-palette-result-snippet">{sessionPreview(session)}</span></span>
    <span className="search-palette-result-meta">· {relativeTime(sessionTimestamp(session))}</span>
  </button>
}

function SearchMessageResult({ message, query, active, onMouseEnter, onClick }) {
  return <button type="button" className={`search-palette-result search-palette-message-result ${active ? 'active' : ''}`} onMouseEnter={onMouseEnter} onClick={onClick}>
    <span className="search-palette-result-icon"><Icon name="chat" size={17} /></span>
    <span className="search-palette-result-copy"><strong className="search-palette-result-title">{highlightText(message.snippet || '', query)}</strong><span className="search-palette-result-snippet">{message.session_kind === 'main' ? '主聊天' : (message.session_title || '旁聊')} · {relativeTime(message.created_at)}</span></span>
  </button>
}
