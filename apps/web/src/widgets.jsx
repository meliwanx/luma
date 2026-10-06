import React, { useEffect, useState } from 'react'
import Icon from './icons.jsx'

function WidgetTitle({ title }) {
  return title ? <h3 className="chat-widget-title">{title}</h3> : null
}

function CheckMark() {
  return <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m5 12 4.5 4.5L19 7" /></svg>
}

export function ChoiceWidget({ widget, onEvent, disabled }) {
  const { spec, state = {} } = widget
  const submitted = state.status === 'submitted'
  const [selected, setSelected] = useState(state.selected || [])
  const [busy, setBusy] = useState(false)
  useEffect(() => { setSelected(state.selected || []) }, [state.selected])

  async function submit(ids) {
    if (disabled || busy || submitted || !ids.length) return
    setBusy(true)
    try { await onEvent(widget, 'submit', ids) } catch { if (!spec.multiple) setSelected([]) } finally { setBusy(false) }
  }

  return <>
    <WidgetTitle title={spec.title} />
    <div className="chat-widget-choices" role="group" aria-label={spec.title || '选项'}>
      {(spec.options || []).map((option) => {
        const active = selected.includes(option.id)
        return <button type="button" key={option.id} className={`chat-widget-choice ${active ? 'selected' : ''} ${submitted && !active ? 'faded' : ''}`} aria-pressed={active} disabled={disabled || busy || submitted} onClick={() => {
          if (spec.multiple) setSelected((current) => current.includes(option.id) ? current.filter((id) => id !== option.id) : [...current, option.id])
          else { setSelected([option.id]); submit([option.id]) }
        }}><span className="chat-widget-choice-copy"><span>{option.label}</span>{option.description && <small>{option.description}</small>}</span>{active && <CheckMark />}</button>
      })}
    </div>
    {spec.multiple && !submitted && <div className="chat-widget-actions"><button type="button" className="chat-widget-primary" disabled={disabled || busy || !selected.length} onClick={() => submit(selected)}>{spec.submit_label || '确定'}</button></div>}
  </>
}

export function FormWidget({ widget, onEvent, disabled, notify }) {
  const { spec, state = {} } = widget
  const submitted = state.status === 'submitted'
  const [values, setValues] = useState(state.values || {})
  const [busy, setBusy] = useState(false)
  const [missing, setMissing] = useState([])
  useEffect(() => { setValues(state.values || {}) }, [state.values])

  async function submit(event) {
    event.preventDefault()
    if (disabled || busy || submitted) return
    const required = (spec.fields || []).filter((field) => field.required && !String(values[field.id] ?? '').trim()).map((field) => field.id)
    setMissing(required)
    if (required.length) { notify?.('请填写必填项'); return }
    setBusy(true)
    try { await onEvent(widget, 'submit', Object.fromEntries((spec.fields || []).map((field) => [field.id, String(values[field.id] ?? '')]))) } catch { /* onEvent shows the server error */ } finally { setBusy(false) }
  }

  return <>
    <WidgetTitle title={spec.title} />
    <form className="chat-widget-form" onSubmit={submit} noValidate>
      {(spec.fields || []).map((field) => {
        const value = String(values[field.id] ?? '')
        const controlId = `${widget.id}-${field.id}`
        const update = (event) => { setValues((current) => ({ ...current, [field.id]: event.target.value })); setMissing((current) => current.filter((id) => id !== field.id)) }
        return <div className="chat-widget-field" key={field.id}>
          <label htmlFor={controlId}>{field.label}{field.required && <span className="chat-widget-required" aria-label="必填"> *</span>}</label>
          {submitted ? <div className="chat-widget-readonly">{value || '—'}</div>
            : field.kind === 'textarea' ? <textarea id={controlId} value={value} placeholder={field.placeholder || ''} disabled={disabled || busy} aria-invalid={missing.includes(field.id)} onChange={update} rows={3} />
              : field.kind === 'select' ? <select id={controlId} value={value} disabled={disabled || busy} aria-invalid={missing.includes(field.id)} onChange={update}><option value="">{field.placeholder || '请选择'}</option>{(field.options || []).map((option) => <option key={option} value={option}>{option}</option>)}</select>
                : <input id={controlId} type={field.kind === 'number' || field.kind === 'date' ? field.kind : 'text'} value={value} placeholder={field.placeholder || ''} disabled={disabled || busy} aria-invalid={missing.includes(field.id)} onChange={update} />}
          {missing.includes(field.id) && !submitted && <small className="chat-widget-error">请填写此项</small>}
        </div>
      })}
      {!submitted && <div className="chat-widget-actions"><button type="submit" className="chat-widget-primary" disabled={disabled || busy}>{spec.submit_label || '提交'}</button></div>}
    </form>
  </>
}

export function ChecklistWidget({ widget, onEvent, disabled }) {
  const { spec, state = {} } = widget
  const [done, setDone] = useState(state.done || {})
  const [busy, setBusy] = useState(false)
  useEffect(() => { setDone(state.done || {}) }, [state.done])
  const items = spec.items || []
  const completed = items.filter((item) => Boolean(done[item.id] ?? item.done)).length
  const remaining = items.length - completed

  async function toggle(item) {
    if (disabled || busy) return
    const next = !Boolean(done[item.id] ?? item.done)
    setDone((current) => ({ ...current, [item.id]: next }))
    setBusy(true)
    try { await onEvent(widget, 'toggle', { item_id: item.id, done: next }) }
    catch { setDone((current) => ({ ...current, [item.id]: !next })) }
    finally { setBusy(false) }
  }

  async function createTasks() {
    if (disabled || busy || state.tasks_created || !remaining) return
    if (!window.confirm(`把 ${remaining} 项未完成的事项加入任务？`)) return
    setBusy(true)
    try { await onEvent(widget, 'create_tasks', null) } catch { /* onEvent shows the server error */ } finally { setBusy(false) }
  }

  return <>
    <div className="chat-widget-heading"><WidgetTitle title={spec.title} /><span className="chat-widget-progress">已完成 {completed}/{items.length}</span></div>
    <div className="chat-widget-checklist">{items.map((item) => {
      const checked = Boolean(done[item.id] ?? item.done)
      return <button type="button" key={item.id} className={`chat-widget-check-row ${checked ? 'checked' : ''}`} role="checkbox" aria-checked={checked} disabled={disabled || busy} onClick={() => toggle(item)}><span className="chat-widget-check-circle">{checked && <CheckMark />}</span><span className="chat-widget-check-copy"><span>{item.label}</span>{item.note && <small>{item.note}</small>}</span></button>
    })}</div>
    {spec.allow_create_tasks !== false && <div className="chat-widget-actions"><button type="button" className="chat-widget-secondary" disabled={disabled || busy || state.tasks_created || !remaining} onClick={createTasks}>{state.tasks_created ? '已加入任务' : '转为任务'}</button></div>}
  </>
}

export function CardsWidget({ widget, onEvent, disabled }) {
  const { spec, state = {} } = widget
  const submitted = state.status === 'submitted'
  const [selected, setSelected] = useState(state.selected || [])
  const [busy, setBusy] = useState(false)
  useEffect(() => { setSelected(state.selected || []) }, [state.selected])

  async function select(item) {
    if (disabled || busy || submitted || !spec.selectable) return
    setSelected([item.id])
    setBusy(true)
    try { await onEvent(widget, 'select', item.id) } catch { setSelected([]) } finally { setBusy(false) }
  }

  return <>
    <WidgetTitle title={spec.title} />
    <div className="chat-widget-cards">{(spec.items || []).map((item) => {
      const active = selected.includes(item.id)
      const content = <><span className="chat-widget-card-top"><strong>{item.title}</strong>{active && <CheckMark />}</span>{item.subtitle && <small className="chat-widget-card-subtitle">{item.subtitle}</small>}{item.body && <span className="chat-widget-card-body">{item.body}</span>}{item.tag && <span className="chat-widget-card-tag">{item.tag}</span>}</>
      return spec.selectable
        ? <button type="button" key={item.id} className={`chat-widget-card selectable ${active ? 'selected' : ''} ${submitted && !active ? 'faded' : ''}`} aria-pressed={active} disabled={disabled || busy || submitted} onClick={() => select(item)}>{content}</button>
        : <div key={item.id} className="chat-widget-card">{content}</div>
    })}</div>
  </>
}

export function ConfirmWidget({ widget, onEvent, disabled }) {
  const { spec, state = {} } = widget
  const status = state.status || 'pending'
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (status !== 'pending') setBusy(false) }, [status])

  async function act(action) {
    if (disabled || busy || status !== 'pending') return
    setBusy(true)
    try { await onEvent(widget, action, null) } catch { setBusy(false) }
  }

  const statusText = { running: '正在执行…', done: '已执行', failed: '执行失败', cancelled: '已取消' }[status]
  return <>
    <div className="chat-widget-heading"><WidgetTitle title={spec.title || '需要你确认'} />{statusText && <span className={`chat-widget-confirm-status ${status}`}>{statusText}</span>}</div>
    {spec.body && <p className="chat-widget-confirm-body">{spec.body}</p>}
    {Array.isArray(spec.details) && spec.details.length > 0 && <dl className="chat-widget-confirm-details">{spec.details.map((item, index) => <React.Fragment key={`${item.label}-${index}`}><dt>{item.label}：</dt><dd>{item.value}</dd></React.Fragment>)}</dl>}
    {status === 'pending' && <div className="chat-widget-actions"><button type="button" className="chat-widget-primary" disabled={disabled || busy} onClick={() => act('confirm')}>{spec.confirm_label || '确认执行'}</button><button type="button" className="chat-widget-secondary" disabled={disabled || busy} onClick={() => act('cancel')}>{spec.cancel_label || '取消'}</button></div>}
  </>
}

export function ChatWidget({ widget, onEvent, disabled = false, offline = false, notify }) {
  if (!widget?.spec) return null
  const Renderer = { choice: ChoiceWidget, form: FormWidget, checklist: ChecklistWidget, cards: CardsWidget, confirm: ConfirmWidget }[widget.type]
  if (!Renderer) return widget.fallback ? <div className="bubble"><p>{widget.fallback}</p></div> : null
  return <section className="chat-widget" aria-label={widget.spec.title || '对话组件'}><Renderer widget={widget} onEvent={onEvent} disabled={disabled || offline} notify={notify} />{offline && <p className="chat-widget-unavailable">离线时不可操作</p>}</section>
}
