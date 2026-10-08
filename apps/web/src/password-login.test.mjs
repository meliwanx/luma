import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'
import { loadAuthConfig, loginWithPassword, registerWithPassword, validateNewPassword } from './password-login.js'

const success = { authenticated: true, user: { user_id: 'u1', username: 'reader' }, access_token: 'test-session' }
const response = (payload = success, ok = true) => ({ ok, json: async () => payload })

test('login sends the shared JSON contract and trims only the username or email', async () => {
  let request
  const result = await loginWithPassword('/api/v1', '  reader@example.test  ', ' password ', async (url, options) => {
    request = { url, ...options }; return response()
  })
  assert.deepEqual(result, success)
  assert.equal(request.url, '/api/v1/auth/login')
  assert.equal(request.method, 'POST')
  assert.equal(request.credentials, 'include')
  assert.equal(request.headers['Content-Type'], 'application/json')
  assert.deepEqual(JSON.parse(request.body), { login: 'reader@example.test', password: ' password ' })
})

test('empty login/password and an oversized password do not submit', async () => {
  let calls = 0
  for (const [login, password] of [['', 'test-password'], ['   ', 'test-password'], ['reader', ''], ['reader', 'x'.repeat(129)]]) {
    await assert.rejects(loginWithPassword('/api/v1', login, password, async () => { calls += 1 }))
  }
  assert.equal(calls, 0)
})

test('authentication errors display server detail without substituting status-based copy', async () => {
  for (const detail of ['用户名或密码错误', '请求过于频繁，请稍后再试', '邀请码不正确', '注册尚未开放']) {
    await assert.rejects(loginWithPassword('/api/v1', 'reader', 'test-password', async () => response({ detail }, false)), { message: detail })
  }
  await assert.rejects(loginWithPassword('/api/v1', 'reader', 'test-password', async () => { throw new Error('transport failure') }), { message: '登录服务暂时不可用' })
  await assert.rejects(loginWithPassword('/api/v1', 'reader', 'test-password', async () => response({ authenticated: false })), { message: '登录服务暂时不可用' })
})

test('registration configuration uses the public backend contract with cookies', async () => {
  let request
  const config = await loadAuthConfig('/api/v1', async (url, options) => {
    request = { url, ...options }; return response({ registration_open: true, requires_invite: true })
  })
  assert.deepEqual(config, { registration_open: true, requires_invite: true })
  assert.equal(request.url, '/api/v1/auth/config')
  assert.equal(request.credentials, 'include')
})

test('registration sends optional fields and invitation only when required', async () => {
  const fields = { username: ' reader ', email: ' reader@example.test ', display_name: ' Reader ', password: 'test-password', confirmPassword: 'test-password', invite_code: ' invite-test ' }
  for (const requiresInvite of [false, true]) {
    let request
    assert.deepEqual(await registerWithPassword('/api/v1', fields, requiresInvite, async (url, options) => { request = { url, ...options }; return response() }), success)
    assert.equal(request.url, '/api/v1/auth/register')
    assert.equal(request.method, 'POST')
    assert.equal(request.credentials, 'include')
    assert.deepEqual(JSON.parse(request.body), { username: 'reader', email: 'reader@example.test', display_name: 'Reader', password: 'test-password', ...(requiresInvite ? { invite_code: 'invite-test' } : {}) })
    assert.equal(request.body.includes('confirmPassword'), false)
  }
})

test('registration rejects invalid username, mismatched/out-of-range passwords, and missing invite before network', async () => {
  let calls = 0
  const fields = { username: 'reader', password: 'test-password', confirmPassword: 'test-password' }
  for (const patch of [{ username: 'xy' }, { username: 'invalid name' }, { password: 'short', confirmPassword: 'short' }, { password: 'x'.repeat(129), confirmPassword: 'x'.repeat(129) }, { confirmPassword: 'different' }]) {
    await assert.rejects(registerWithPassword('/api/v1', { ...fields, ...patch }, false, async () => { calls += 1 }))
  }
  await assert.rejects(registerWithPassword('/api/v1', fields, true, async () => { calls += 1 }), { message: '请输入邀请码' })
  assert.equal(calls, 0)
  validateNewPassword('🙂'.repeat(128), '🙂'.repeat(128))
  assert.throws(() => validateNewPassword('🙂'.repeat(129), '🙂'.repeat(129)))
})

const source = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
const fragment = source.slice(source.indexOf('function LoginPage('), source.indexOf('function RouteLoading('))
const { code } = await transformWithEsbuild(fragment, 'login-page.jsx', { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' })

function pageHarness({ config = { registration_open: true, requires_invite: false }, fields = {}, login, register } = {}) {
  const states = [{ registration_open: false, requires_invite: false }, 'login', { login: 'reader', username: 'reader', email: '', display_name: '', password: 'test-password', confirmPassword: 'test-password', invite_code: '', ...fields }, false, '', '']
  const refs = [], effects = [], requests = [], stored = [], navigations = []
  let stateIndex = 0, refIndex = 0, authenticated = 0
  const context = {
    React, Error, API_URL: '/api/v1', ACCESS_TOKEN_STORAGE_KEY: 'luma_access_token',
    useBrand: () => ({ product_name: 'Luma', name: 'Luma', tagline: '个人助理', logo_url: '', primary_color: '#2563EB' }),
    BrandLogo: () => React.createElement('svg', { 'data-logo': 'brand' }),
    useState: (initial) => { const index = stateIndex++; if (!(index in states)) states[index] = initial; return [states[index], (value) => { states[index] = typeof value === 'function' ? value(states[index]) : value }] },
    useRef: (initial) => { const index = refIndex++; return refs[index] ||= { current: initial } },
    useEffect: (callback) => effects.push(callback),
    loadAuthConfig: async () => config,
    loginWithPassword: async (...args) => { requests.push({ type: 'login', args }); return login ? login(...args) : loginWithPassword(...args, async () => response()) },
    registerWithPassword: async (...args) => { requests.push({ type: 'register', args }); return register ? register(...args) : registerWithPassword(...args, async () => response()) },
    sessionStorage: { setItem: (key, value) => stored.push({ key, value }) }, navigate: (...args) => navigations.push(args),
  }
  vm.createContext(context); vm.runInContext(code, context)
  const render = () => { stateIndex = 0; refIndex = 0; return context.LoginPage({ onAuthenticated: () => { authenticated += 1 } }) }
  render(); const effect = effects.shift()
  return { render, states, requests, stored, navigations, authenticated: () => authenticated, initialize: async () => { effect(); await Promise.resolve(); await Promise.resolve() } }
}

function findElement(element, predicate) {
  if (!React.isValidElement(element)) return null
  if (predicate(element)) return element
  for (const child of React.Children.toArray(element.props.children)) { const found = findElement(child, predicate); if (found) return found }
  return null
}

function registerTab(page) {
  findElement(page.render(), (element) => element.props.id === 'auth-register-tab').props.onClick()
  // Choosing a tab clears credentials, then the user enters the new password.
  page.states[2] = { ...page.states[2], password: 'test-password', confirmPassword: 'test-password' }
}

test('login always appears; registration tab only appears when registration_open is true', async () => {
  for (const registration_open of [false, true]) {
    const page = pageHarness({ config: { registration_open, requires_invite: false } }); await page.initialize()
    const html = renderToStaticMarkup(page.render())
    assert.match(html, /name="login"[^>]*autoComplete="username"/)
    assert.match(html, /name="password"[^>]*autoComplete="current-password"/)
    assert.equal(html.includes('id="auth-register-tab"'), registration_open)
  }
})

test('invitation field appears only on an open registration tab that requires invites', async () => {
  for (const requires_invite of [false, true]) {
    const page = pageHarness({ config: { registration_open: true, requires_invite } }); await page.initialize()
    assert.doesNotMatch(renderToStaticMarkup(page.render()), /name="invite_code"/)
    registerTab(page)
    const html = renderToStaticMarkup(page.render())
    assert.match(html, /name="username"/); assert.match(html, /name="email"/); assert.match(html, /name="display_name"/); assert.match(html, /name="confirmPassword"/)
    assert.equal(html.includes('name="invite_code"'), requires_invite)
  }
})

test('successful form login stores only the session token and clears passwords before entering the app', async () => {
  const page = pageHarness(); await page.initialize()
  await findElement(page.render(), (element) => element.type === 'form').props.onSubmit({ preventDefault() {} })
  assert.equal(page.requests[0].type, 'login'); assert.deepEqual(page.stored, [{ key: 'luma_access_token', value: 'test-session' }])
  assert.equal(page.states[2].password, ''); assert.equal(page.authenticated(), 1)
  assert.equal(page.navigations[0][0], '/app'); assert.equal(page.navigations[0][1].replace, true)
})

test('registration form uses configuration and displays validation failures without navigating', async () => {
  const page = pageHarness({ config: { registration_open: true, requires_invite: true } }); await page.initialize(); registerTab(page)
  await findElement(page.render(), (element) => element.type === 'form').props.onSubmit({ preventDefault() {} })
  assert.equal(page.requests[0].type, 'register'); assert.equal(page.requests[0].args[2], true)
  assert.match(renderToStaticMarkup(page.render()), /role="alert">请输入邀请码/)
  assert.deepEqual(page.stored, []); assert.deepEqual(page.navigations, [])
  page.states[2] = { ...page.states[2], invite_code: 'invite-test' }
  await findElement(page.render(), (element) => element.type === 'form').props.onSubmit({ preventDefault() {} })
  assert.equal(page.authenticated(), 1)
})

test('pending form prevents duplicate submissions; server detail remains visible on failure', async () => {
  let resolveLogin
  const pending = new Promise((resolve) => { resolveLogin = resolve })
  const page = pageHarness({ login: () => pending }); await page.initialize()
  const form = findElement(page.render(), (element) => element.type === 'form')
  const first = form.props.onSubmit({ preventDefault() {} }); await form.props.onSubmit({ preventDefault() {} })
  assert.equal(page.requests.length, 1); assert.match(renderToStaticMarkup(page.render()), /登录中…/)
  resolveLogin(success); await first
  const failure = pageHarness({ login: async () => { throw new Error('用户名或密码错误') } }); await failure.initialize()
  await findElement(failure.render(), (element) => element.type === 'form').props.onSubmit({ preventDefault() {} })
  assert.match(renderToStaticMarkup(failure.render()), /role="alert">用户名或密码错误/)
  assert.deepEqual(failure.stored, []); assert.deepEqual(failure.navigations, [])
})
