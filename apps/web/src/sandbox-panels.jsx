import React, { useEffect, useMemo, useState } from 'react'

const CATEGORY_LABELS = { mcp: '连接器', luma: 'Luma', sandbox: '沙箱', browser: '浏览器' }
const STATUS_LABELS = { running: '运行中', paused: '已暂停', none: '未创建' }

function formatBytes(value) {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes <= 0) return '无备份'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

function formatDate(value) {
  if (!value) return '从未使用'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function errorMessage(error, fallback) {
  let detail = error?.message || fallback
  try {
    const parsed = JSON.parse(detail)
    detail = parsed.detail || parsed.message || detail
  } catch (_) {
    // Keep plain-text API errors as-is.
  }
  return detail
}

function PermissionSwitch({ item, disabled, onChange }) {
  const checked = item.mode === 'always'
  return <button
    type="button"
    role="switch"
    aria-checked={checked}
    aria-label={`${item.label}：${checked ? '始终允许' : '每次询问'}`}
    disabled={disabled}
    className={`ms-switch ${checked ? 'on' : ''}`}
    onClick={() => onChange(!checked)}
  ><i /></button>
}

export function PermissionsPanel({ request, notify }) {
  const [items, setItems] = useState(undefined)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  useEffect(() => {
    let active = true
    request('/permissions').then((result) => {
      if (active) {
        setItems(Array.isArray(result?.items) ? result.items : [])
        setError('')
      }
    }).catch((reason) => {
      if (active) {
        setItems([])
        setError(errorMessage(reason, '权限加载失败'))
      }
    })
    return () => { active = false }
  }, [request])

  const groups = useMemo(() => {
    const grouped = { mcp: [], luma: [], sandbox: [], browser: [] }
    ;(items || []).forEach((item) => {
      const category = grouped[item.category] ? item.category : 'luma'
      grouped[category].push(item)
    })
    return grouped
  }, [items])

  async function updatePermission(item, always) {
    if (!item.allow_always || busy) return
    const mode = always ? 'always' : 'ask'
    setBusy(item.key)
    try {
      const result = await request(`/permissions/${encodeURIComponent(item.key)}`, {
        method: 'PUT',
        body: JSON.stringify({ mode }),
      })
      const updated = result?.item || result
      setItems((current) => (current || []).map((entry) => entry.key === item.key ? { ...entry, ...updated, mode } : entry))
    } catch (reason) {
      notify(errorMessage(reason, '权限更新失败'))
    } finally {
      setBusy('')
    }
  }

  if (items === undefined) return <div className="sandbox-settings-panel"><p className="ms-note">正在加载权限…</p></div>
  return <div className="sandbox-settings-panel">
    {error && <p className="sandbox-panel-error" role="alert">{error}</p>}
    {Object.entries(groups).map(([category, categoryItems]) => <section className="sandbox-settings-group" key={category}>
      <h4>{CATEGORY_LABELS[category]}</h4>
      <div className="ms-group">
        {categoryItems.length ? categoryItems.map((item) => <div className="permission-row" key={item.key}>
          <div className="permission-copy"><strong>{item.label || item.key}</strong><small>{item.description || '此操作的权限设置'}</small></div>
          <div className="permission-control">
            <span className="permission-mode">{item.mode === 'always' ? '始终允许' : '每次询问'}</span>
            <PermissionSwitch item={item} disabled={!item.allow_always || busy === item.key} onChange={(next) => updatePermission(item, next)} />
          </div>
          {item.allow_always === false && <small className="permission-confirm-note">此类操作始终需要确认</small>}
        </div>) : <p className="ms-note">暂无权限项</p>}
      </div>
    </section>)}
  </div>
}

export function SandboxWorkspacePanel({ request, notify }) {
  const [sandbox, setSandbox] = useState(undefined)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)

  async function loadStatus() {
    try {
      const result = await request('/sandbox')
      setSandbox(result || { state: 'none' })
      setError('')
    } catch (reason) {
      setError(errorMessage(reason, '沙箱状态加载失败'))
    }
  }

  useEffect(() => {
    let active = true
    request('/sandbox').then((result) => {
      if (active) {
        setSandbox(result || { state: 'none' })
        setError('')
      }
    }).catch((reason) => { if (active) setError(errorMessage(reason, '沙箱状态加载失败')) })
    return () => { active = false }
  }, [request])

  async function resetWorkspace() {
    setBusy(true)
    try {
      await request('/sandbox/reset', { method: 'POST' })
      setSandbox({ state: 'none', last_seen_at: null, backup: null })
      setConfirming(false)
      notify('沙箱工作区已重置')
    } catch (reason) {
      notify(errorMessage(reason, '重置工作区失败'))
    } finally {
      setBusy(false)
    }
  }

  const state = sandbox?.state || sandbox?.status || 'none'
  const statusLabel = STATUS_LABELS[state] || '未创建'
  const backup = sandbox?.backup

  return <div className="sandbox-settings-panel">
    {error && <p className="sandbox-panel-error" role="alert">{error}</p>}
    <div className="ms-group sandbox-status-group">
      <div className="sandbox-status-row"><div><strong>状态</strong><small>沙箱闲置后会暂停，恢复时继续使用工作区文件</small></div><span className="ms-value"><span className={`sandbox-state-dot ${state}`} />{sandbox === undefined ? '正在检查…' : statusLabel}</span></div>
      <div className="sandbox-status-row"><div><strong>最近使用</strong></div><span className="ms-value">{formatDate(sandbox?.last_seen_at)}</span></div>
      <div className="sandbox-status-row"><div><strong>备份大小</strong></div><span className="ms-value">{formatBytes(backup?.size_bytes)}</span></div>
      {backup?.created_at && <div className="sandbox-status-row"><div><strong>备份时间</strong></div><span className="ms-value">{formatDate(backup.created_at)}</span></div>}
      <div className="sandbox-reset-row"><div><strong>重置工作区</strong><small>清空工作区所有文件，不能撤销</small></div><button type="button" className="ms-btn mcp-danger" disabled={busy || sandbox === undefined} onClick={() => setConfirming(true)}>重置</button></div>
    </div>
    {confirming && <div className="sandbox-confirm-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setConfirming(false) }}>
      <div className="sandbox-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="sandbox-reset-title">
        <h3 id="sandbox-reset-title">重置沙箱工作区？</h3>
        <p>这会清空工作区中的所有文件，操作不能撤销。确定继续吗？</p>
        <div className="sandbox-confirm-actions"><button type="button" className="ms-btn" disabled={busy} onClick={() => setConfirming(false)}>取消</button><button type="button" className="ms-btn mcp-danger" disabled={busy} onClick={resetWorkspace}>{busy ? '重置中…' : '确认重置'}</button></div>
      </div>
    </div>}
  </div>
}
