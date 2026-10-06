import React, { useEffect, useState } from 'react'
import { proactivePrefs, saveProactivePrefs, validateProactivePrefs } from './content-data.js'
import './proactive.css'

const TIMEZONES = ['Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Tokyo', 'Asia/Singapore', 'Europe/London', 'Europe/Paris', 'America/New_York', 'America/Los_Angeles', 'UTC']

function PrefSwitch({ checked, onChange, label }) {
  return <button type="button" role="switch" aria-label={label} aria-checked={Boolean(checked)} className={`ms-switch ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}><i /></button>
}

export default function ProactiveSettings({ request, notify }) {
  const [prefs, setPrefs] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let active = true
    setLoading(true)
    setError('')
    request('/proactive/prefs').then((result) => { if (active) setPrefs(proactivePrefs(result)) })
      .catch(() => { if (active) setError('主动消息设置加载失败') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [request, revision])
  const update = (key, value) => setPrefs((current) => ({ ...current, [key]: value }))
  async function save(event) {
    event.preventDefault()
    if (saving) return
    const invalid = validateProactivePrefs(prefs)
    if (invalid) { notify(invalid); return }
    setSaving(true)
    try {
      // Dynamic instructions are edited on the feed page; keep its latest value.
      const payload = await saveProactivePrefs(request, prefs)
      setPrefs(payload)
      notify('已保存')
    } catch { notify('保存失败，请稍后重试') }
    finally { setSaving(false) }
  }
  if (loading) return <p className="ms-note" role="status">正在加载…</p>
  if (error) return <div role="alert"><p>{error}</p><button type="button" className="ms-btn" onClick={() => setRevision((value) => value + 1)}>重试</button></div>
  return <form className="proactive-settings" onSubmit={save}>
    <fieldset disabled={saving}>
      <div className="ms-group proactive-options">
        <div className="proactive-toggle"><span>主动消息</span><PrefSwitch label="主动消息" checked={prefs.enabled} onChange={(value) => update('enabled', value)} /></div>
        <label className="proactive-field"><span>每天最多</span><input type="number" min="0" max="5" step="1" required value={prefs.max_per_day} onChange={(event) => update('max_per_day', event.target.value === '' ? '' : Number(event.target.value))} /></label>
        <div className="proactive-field"><span>时间段</span><div className="proactive-time-window"><label><span className="sr-only">开始时间</span><input type="time" required value={prefs.window_start} onChange={(event) => update('window_start', event.target.value)} /></label><span>至</span><label><span className="sr-only">结束时间</span><input type="time" required value={prefs.window_end} onChange={(event) => update('window_end', event.target.value)} /></label></div></div>
        <label className="proactive-field"><span>时区</span><select value={prefs.timezone} onChange={(event) => update('timezone', event.target.value)}>{[...new Set([...TIMEZONES, prefs.timezone])].map((zone) => <option key={zone} value={zone}>{zone}</option>)}</select></label>
        <label className="proactive-field stacked"><span>想听的话题</span><textarea rows="3" maxLength="1000" value={prefs.topics_like} onChange={(event) => update('topics_like', event.target.value)} /></label>
        <label className="proactive-field stacked"><span>不想听的话题</span><textarea rows="3" maxLength="1000" value={prefs.topics_avoid} onChange={(event) => update('topics_avoid', event.target.value)} /></label>
        <label className="proactive-field stacked"><span>风格</span><textarea rows="2" maxLength="500" value={prefs.style} onChange={(event) => update('style', event.target.value)} /></label>
      </div>
      <h4>动态</h4>
      <div className="ms-group proactive-options">
        <div className="proactive-toggle"><span>动态开关</span><PrefSwitch label="动态开关" checked={prefs.feed_enabled} onChange={(value) => update('feed_enabled', value)} /></div>
        <label className="proactive-field"><span>每天篇数</span><input type="number" min="0" max="3" step="1" required value={prefs.feed_per_day} onChange={(event) => update('feed_per_day', event.target.value === '' ? '' : Number(event.target.value))} /></label>
      </div>
      <button type="submit" className="primary-btn proactive-save">{saving ? '保存中…' : '保存'}</button>
    </fieldset>
  </form>
}
