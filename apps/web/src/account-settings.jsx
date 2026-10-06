import React, { useEffect, useRef, useState } from 'react'
import { validateNewPassword } from './password-login.js'

const CLIENT_NAMES = { web: '网页', desktop: '桌面端', flutter: '移动端', ios: 'iOS', android: 'Android', macos: 'macOS', windows: 'Windows' }

export function loginDeviceTime(value) {
  const numeric = typeof value === 'number' || (typeof value === 'string' && /^\d{10,13}$/.test(value))
  const epoch = numeric ? Number(value) : 0
  const date = numeric ? new Date(epoch < 1e12 ? epoch * 1000 : epoch) : new Date(value)
  return Number.isNaN(date.getTime()) ? '未知' : date.toLocaleString()
}

function accountError(error) {
  try {
    const payload = JSON.parse(error.message)
    if (typeof payload.detail === 'string') return payload.detail
    if (Array.isArray(payload.detail)) return payload.detail.map((item) => item.msg || String(item)).join('；')
  } catch { /* plain-text errors */ }
  return error.message || '操作失败'
}

export default function AccountSettings({ account, request, onProfileUpdated, onSignedOut, notify }) {
  const [displayName, setDisplayName] = useState(account?.display_name || '')
  const [email, setEmail] = useState(account?.email || '')
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [deletePassword, setDeletePassword] = useState('')
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [devices, setDevices] = useState(null)
  const [deviceError, setDeviceError] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const pending = useRef(false)

  useEffect(() => {
    setDisplayName(account?.display_name || '')
    setEmail(account?.email || '')
  }, [account?.display_name, account?.email])

  useEffect(() => {
    let active = true
    request('/auth/sessions').then((items) => { if (active) setDevices(Array.isArray(items) ? items : items?.sessions || []) })
      .catch((failure) => { if (active) { setDevices([]); setDeviceError(accountError(failure)) } })
    return () => { active = false }
  }, [request])

  async function refreshDevices() {
    try {
      const items = await request('/auth/sessions')
      setDevices(Array.isArray(items) ? items : items?.sessions || [])
      setDeviceError('')
    } catch (failure) { setDeviceError(accountError(failure)) }
  }

  async function perform(action, operation) {
    if (pending.current) return
    pending.current = true
    setBusy(action)
    setError('')
    try { await operation() } catch (failure) { setError(accountError(failure)) }
    finally { pending.current = false; setBusy('') }
  }

  async function saveProfile(event) {
    event.preventDefault()
    await perform('profile', async () => {
      const result = await request('/account/profile', { method: 'PATCH', body: JSON.stringify({ display_name: displayName.trim(), email: email.trim() || null }) })
      const user = result?.user || result
      if (user?.user_id) onProfileUpdated(user)
      else onProfileUpdated(await request('/auth/me'))
      notify('资料已更新')
    })
  }

  async function changePassword(event) {
    event.preventDefault()
    await perform('password', async () => {
      if (!currentPassword) throw new Error('请输入当前密码')
      validateNewPassword(newPassword, confirmation)
      await request('/account/password', { method: 'POST', skipAuthRequired: true, body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) })
      setCurrentPassword(''); setNewPassword(''); setConfirmation('')
      await refreshDevices()
      notify('密码已修改，其他设备已退出登录')
    })
  }

  async function revokeDevice(device) {
    if (!device.id || !window.confirm('移除这个登录设备？')) return
    await perform(`device-${device.id}`, async () => {
      await request(`/auth/sessions/${encodeURIComponent(device.id)}`, { method: 'DELETE' })
      if (device.current === true || device.is_current === true) { onSignedOut(); return }
      // The session API does not always identify the current device. Reloading
      // authenticated sessions also detects when this request revoked our own.
      await refreshDevices()
      notify('已移除登录设备')
    })
  }

  async function logoutAll() {
    if (!window.confirm('退出所有设备？当前设备也会退出登录。')) return
    await perform('logout-all', async () => {
      await request('/auth/logout-all', { method: 'POST' })
      onSignedOut()
    })
  }

  async function deleteAccount(event) {
    event.preventDefault()
    if (!deletePassword) { setError('请输入密码'); return }
    if (!window.confirm('确认永久删除账户及全部对话、文件、记忆和连接器？此操作无法撤销。')) return
    await perform('delete', async () => {
      await request('/account', { method: 'DELETE', skipAuthRequired: true, body: JSON.stringify({ password: deletePassword }) })
      setDeletePassword('')
      onSignedOut()
    })
  }

  return <div className="ms-account-settings">
    {error && <p className="muse-auth-error" role="alert">{error}</p>}
    <h4>个人资料</h4>
    <form className="ms-account-form" onSubmit={saveProfile}>
      <label>用户名<input value={account?.username || ''} readOnly autoComplete="username" /></label>
      <label>邮箱<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" disabled={Boolean(busy)} /></label>
      <label>显示名<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} autoComplete="nickname" disabled={Boolean(busy)} /></label>
      <button type="submit" className="ms-btn mcp-primary" disabled={Boolean(busy)}>{busy === 'profile' ? '保存中…' : '保存资料'}</button>
    </form>
    <h4>修改密码</h4>
    <form className="ms-account-form" onSubmit={changePassword}>
      <label>当前密码<input type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} autoComplete="current-password" required disabled={Boolean(busy)} /></label>
      <label>新密码<input type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" required disabled={Boolean(busy)} aria-describedby="account-password-rule" /></label>
      <label>确认新密码<input type="password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="new-password" required disabled={Boolean(busy)} /></label>
      <p id="account-password-rule" className="ms-note">密码须为 8–128 位。修改后其他设备会退出登录。</p>
      <button type="submit" className="ms-btn" disabled={Boolean(busy)}>{busy === 'password' ? '修改中…' : '修改密码'}</button>
    </form>
    <h4>登录设备</h4>
    <div className="ms-group">
      {deviceError && <div className="ms-row"><p role="alert">{deviceError}</p><button type="button" className="ms-btn" onClick={refreshDevices} disabled={Boolean(busy)}>重试</button></div>}
      {devices === null ? <p className="ms-note">正在加载…</p> : devices.length ? devices.map((device) => <div className="ms-row" key={device.id}>
        <div className="ms-row-text"><strong>{CLIENT_NAMES[device.client || device.client_type] || device.client || device.client_type || '登录设备'}{device.platform ? ` · ${device.platform}` : ''}{(device.current || device.is_current) && <em className="ms-tag">本设备</em>}</strong><small>{device.last_used_at || device.created_at ? `最近活跃 ${loginDeviceTime(device.last_used_at || device.created_at)}` : '有效登录会话'}</small></div>
        <button type="button" className="ms-btn mcp-danger" disabled={Boolean(busy)} onClick={() => revokeDevice(device)}>{busy === `device-${device.id}` ? '移除中…' : '移除'}</button>
      </div>) : !deviceError && <p className="ms-note">暂无设备记录</p>}
    </div>
    <button type="button" className="ms-btn" disabled={Boolean(busy)} onClick={logoutAll}>{busy === 'logout-all' ? '退出中…' : '退出所有设备'}</button>
    <h4>删除账户</h4>
    <p className="ms-note">删除账户会永久删除全部数据。你的数据存放在你部署的服务器上。</p>
    {deleteOpen ? <form className="ms-account-form" onSubmit={deleteAccount}>
      <label>输入密码以删除账户<input type="password" autoComplete="current-password" value={deletePassword} onChange={(event) => setDeletePassword(event.target.value)} required disabled={Boolean(busy)} /></label>
      <div className="mcp-form-actions"><button type="button" className="ms-btn" disabled={Boolean(busy)} onClick={() => { setDeleteOpen(false); setDeletePassword('') }}>取消</button><button type="submit" className="ms-btn mcp-danger" disabled={Boolean(busy)}>{busy === 'delete' ? '删除中…' : '永久删除账户'}</button></div>
    </form> : <button type="button" className="ms-btn mcp-danger" disabled={Boolean(busy)} onClick={() => setDeleteOpen(true)}>删除账户…</button>}
  </div>
}
