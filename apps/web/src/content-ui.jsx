import React, { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import Icon from './icons.jsx'
import './content-ui.css'

const contentIcons = { chart: 'chart', doc: 'doc', search: 'search', calendar: 'calendar', code: 'code', mail: 'mail', spark: 'spark', book: 'book', money: 'money', heart: 'heart', globe: 'globe', checklist: 'goal', image: 'image', archive: 'archive', pin: 'pin', grid: 'grid' }

export function ContentIcon({ name, size = 22 }) {
  return <Icon name={contentIcons[name] || 'spark'} size={size} />
}

export function ContentDialog({ title, onClose, children, className = '' }) {
  const titleId = useId()
  // Keep dialogs outside masked/scrolling message containers while inheriting the theme.
  const [portalRoot] = useState(() => typeof document === 'undefined' ? null : document.querySelector('.app-shell.muse-shell') || document.body)
  const dialog = useRef(null)
  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    const previous = document.activeElement
    dialog.current?.focus()
    function onKeyDown(event) {
      const dialogs = document.querySelectorAll('[aria-modal="true"]')
      if (dialogs[dialogs.length - 1] !== dialog.current) return
      if (event.key === 'Escape') {
        // Let a menu or inline editor consume Escape before closing its dialog.
        if (event.target.closest('details[open], [data-escape-local]')) return
        event.preventDefault(); event.stopImmediatePropagation(); close.current()
      }
      if (event.key !== 'Tab') return
      const elements = Array.from(dialog.current.querySelectorAll('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], summary, [tabindex="0"]')).filter((element) => !element.closest('details:not([open])') || element.tagName === 'SUMMARY')
      const first = elements[0], last = elements[elements.length - 1]
      if (!first) { event.preventDefault(); return }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      if (previous?.isConnected) previous.focus()
    }
  }, [])
  const content = <div className="content-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={dialog} tabIndex={-1} className={`content-dialog ${className}`} role="dialog" aria-modal="true" aria-labelledby={titleId}>
      <header className="content-dialog-header"><h2 id={titleId}>{title}</h2><button type="button" className="icon-btn" onClick={onClose} aria-label="关闭弹窗"><Icon name="close" size={20} /></button></header>
      <div className="content-dialog-body">{children}</div>
    </section>
  </div>
  return portalRoot ? createPortal(content, portalRoot) : content
}

export function ActionMenu({ label = '更多操作', children }) {
  const menu = useRef(null)
  useEffect(() => {
    function outside(event) { if (menu.current && !menu.current.contains(event.target)) menu.current.open = false }
    function escape(event) { if (event.key === 'Escape' && menu.current?.open) { menu.current.open = false; menu.current.querySelector('summary')?.focus(); event.stopPropagation() } }
    document.addEventListener('pointerdown', outside)
    menu.current?.addEventListener('keydown', escape)
    const element = menu.current
    return () => { document.removeEventListener('pointerdown', outside); element?.removeEventListener('keydown', escape) }
  }, [])
  return <details ref={menu} className="content-action-menu"><summary aria-label={label} title={label}><Icon name="more-horizontal" size={20} /></summary><div className="content-menu-items" onClick={(event) => { if (event.target.closest('button') && menu.current) menu.current.open = false }}>{children}</div></details>
}
