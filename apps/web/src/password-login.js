function detailMessage(payload, fallback) {
  if (typeof payload?.detail === 'string') return payload.detail
  if (Array.isArray(payload?.detail)) return payload.detail.map((item) => item.msg || String(item)).join('；')
  return fallback
}

async function authRequest(apiUrl, path, body, fetchImpl, fallback) {
  let response
  try {
    response = await fetchImpl(`${apiUrl}${path}`, {
      ...(body ? { method: 'POST', body: JSON.stringify(body) } : {}),
      headers: { Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
      credentials: 'include',
    })
  } catch {
    throw new Error(fallback)
  }
  let payload
  try { payload = await response.json() } catch { throw new Error(fallback) }
  if (!response.ok) throw new Error(detailMessage(payload, fallback))
  return payload
}

export async function loadAuthProviders(apiUrl, fetchImpl = fetch) {
  const payload = await authRequest(apiUrl, '/auth/providers', null, fetchImpl, '无法读取登录方式，请稍后重试')
  const providers = Array.isArray(payload?.providers)
    ? payload.providers.filter((item) => item && typeof item.kind === 'string')
    : []
  return {
    providers,
    registration_open: payload.registration_open === true,
    requires_invite: payload.requires_invite === true,
    bootstrap_required: payload.bootstrap_required === true,
    account_label: typeof payload.account_label === 'string' && payload.account_label.trim() ? payload.account_label.trim() : '账号',
    sso_label: typeof payload.sso_label === 'string' && payload.sso_label.trim() ? payload.sso_label.trim() : '单点登录',
  }
}

export async function loadAuthConfig(apiUrl, fetchImpl = fetch) {
  const payload = await authRequest(apiUrl, '/auth/config', null, fetchImpl, '无法读取注册设置，请稍后重试')
  return { registration_open: payload.registration_open === true, requires_invite: payload.requires_invite === true }
}

export async function loginWithPassword(apiUrl, login, password, fetchImpl = fetch) {
  const normalizedLogin = login.trim()
  if (!normalizedLogin || !password) throw new Error('请输入用户名或邮箱和密码')
  if (Array.from(password).length > 128) throw new Error('密码不能超过 128 位')
  const payload = await authRequest(apiUrl, '/auth/login', { login: normalizedLogin, password }, fetchImpl, '登录服务暂时不可用')
  if (payload?.authenticated !== true) throw new Error('登录服务暂时不可用')
  return payload
}

export async function loginWithSsoPassword(apiUrl, account, password, fetchImpl = fetch) {
  const normalized = account.trim()
  if (!normalized || !password) throw new Error('请输入账号和密码')
  if (Array.from(password).length > 128) throw new Error('密码不能超过 128 位')
  const payload = await authRequest(apiUrl, '/auth/password/login', { account: normalized, password }, fetchImpl, '登录服务暂时不可用')
  if (payload?.authenticated !== true) throw new Error('登录服务暂时不可用')
  return payload
}

export async function startSsoLogin(apiUrl, nextPath = '/app', fetchImpl = fetch) {
  const safeNext = typeof nextPath === 'string' && nextPath.startsWith('/') && !nextPath.startsWith('//') ? nextPath : '/app'
  const payload = await authRequest(apiUrl, `/auth/sso/start?next=${encodeURIComponent(safeNext)}`, null, fetchImpl, '单点登录暂时不可用')
  if (typeof payload?.url !== 'string' || typeof payload?.state !== 'string' || !payload.state) throw new Error('单点登录暂时不可用')
  return payload
}

export async function exchangeSsoTicket(apiUrl, ticket, state, fetchImpl = fetch) {
  const body = state ? { ticket, state } : { ticket }
  const payload = await authRequest(apiUrl, '/auth/sso/exchange', body, fetchImpl, '单点登录暂时不可用')
  if (payload?.authenticated !== true) throw new Error('单点登录暂时不可用')
  return payload
}

export function validateNewPassword(password, confirmation) {
  if (Array.from(password).length < 8 || Array.from(password).length > 128) throw new Error('密码长度应为 8–128 位')
  if (password !== confirmation) throw new Error('两次输入的密码不一致')
}

export async function registerWithPassword(apiUrl, fields, requiresInvite = false, fetchImpl = fetch) {
  const username = fields.username.trim()
  if (!/^[a-zA-Z0-9_.-]{3,32}$/.test(username)) throw new Error('用户名应为 3–32 位字母、数字、下划线、点或短横线')
  validateNewPassword(fields.password, fields.confirmPassword)
  if (requiresInvite && !fields.invite_code?.trim()) throw new Error('请输入邀请码')
  const body = { username, password: fields.password }
  for (const field of ['email', 'display_name']) {
    if (fields[field]?.trim()) body[field] = fields[field].trim()
  }
  if (requiresInvite) body.invite_code = fields.invite_code.trim()
  const payload = await authRequest(apiUrl, '/auth/register', body, fetchImpl, '注册服务暂时不可用')
  if (payload?.authenticated !== true) throw new Error('注册服务暂时不可用')
  return payload
}
