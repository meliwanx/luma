import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'
import { validateNewPassword } from './password-login.js'

const source = (await readFile(new URL('./account-settings.jsx', import.meta.url), 'utf8'))
  .replace(/^import .*\n/gm, '').replace('export default function', 'function').replace('export function', 'function')
const { code } = await transformWithEsbuild(source, 'account-settings.jsx', { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' })
const user = { user_id: 'u1', username: 'reader', email: 'reader@example.test', display_name: 'Reader', role: 'user' }
const device = { id: 'device/one', client_type: 'desktop', platform: 'macOS', created_at: 1791331200, last_used_at: 1791331200 }

function findAll(element, predicate, found = []) {
  if (!React.isValidElement(element)) return found
  if (predicate(element)) found.push(element)
  for (const child of React.Children.toArray(element.props.children)) findAll(child, predicate, found)
  return found
}

function harness({ states: initial = {}, confirm = true, requestImpl } = {}) {
  const states = [user.display_name, user.email, '', '', '', '', false, [device], '', '', '']
  for (const [index, value] of Object.entries(initial)) states[index] = value
  const refs = [], effects = [], requests = [], notices = [], profiles = [], confirmations = []
  let stateIndex = 0, refIndex = 0, signedOut = 0
  const context = {
    React, Error, Date, validateNewPassword,
    useState: (value) => { const index = stateIndex++; if (!(index in states)) states[index] = value; return [states[index], (update) => { states[index] = typeof update === 'function' ? update(states[index]) : update }] },
    useRef: (value) => { const index = refIndex++; return refs[index] ||= { current: value } },
    useEffect: (effect) => effects.push(effect),
    window: { confirm: (message) => { confirmations.push(message); return confirm } },
  }
  vm.createContext(context); vm.runInContext(code, context)
  const request = async (path, options = {}) => {
    requests.push({ path, ...options })
    if (requestImpl) return requestImpl(path, options)
    if (path === '/auth/sessions') return [device]
    if (path === '/account/profile') return { ...user, display_name: 'Updated', email: 'updated@example.test' }
    return null
  }
  const render = () => { stateIndex = 0; refIndex = 0; return context.AccountSettings({ account: user, request, onProfileUpdated: (profile) => profiles.push(profile), onSignedOut: () => { signedOut += 1 }, notify: (message) => notices.push(message) }) }
  const forms = () => findAll(render(), (element) => element.type === 'form')
  const button = (text) => findAll(render(), (element) => element.type === 'button' && element.props.children === text)[0]
  render()
  return { states, render, forms, button, requests, notices, profiles, confirmations, signedOut: () => signedOut, time: context.loginDeviceTime }
}

test('profile edit sends a JSON patch and updates the displayed account', async () => {
  const h = harness({ states: { 0: ' Updated ', 1: ' updated@example.test ' } })
  await h.forms()[0].props.onSubmit({ preventDefault() {} })
  assert.equal(h.requests[0].path, '/account/profile'); assert.equal(h.requests[0].method, 'PATCH')
  assert.deepEqual(JSON.parse(h.requests[0].body), { display_name: 'Updated', email: 'updated@example.test' })
  assert.equal(h.profiles[0].display_name, 'Updated'); assert.deepEqual(h.notices, ['资料已更新'])
})

test('password mismatch prevents mutation; successful change clears credentials and refreshes revoked sessions', async () => {
  const h = harness({ states: { 2: 'test-current', 3: 'test-new-password', 4: 'different' } })
  await h.forms()[1].props.onSubmit({ preventDefault() {} })
  assert.deepEqual(h.requests, []); assert.match(renderToStaticMarkup(h.render()), /两次输入的密码不一致/)
  h.states[4] = 'test-new-password'
  await h.forms()[1].props.onSubmit({ preventDefault() {} })
  assert.equal(h.requests[0].path, '/account/password'); assert.equal(h.requests[0].method, 'POST')
  assert.deepEqual(JSON.parse(h.requests[0].body), { current_password: 'test-current', new_password: 'test-new-password' })
  assert.equal(h.requests[0].skipAuthRequired, true); assert.equal(h.requests[1].path, '/auth/sessions')
  assert.deepEqual(h.states.slice(2, 5), ['', '', '']); assert.equal(h.signedOut(), 0)
})

test('wrong current password stays visible and keeps the current session', async () => {
  const h = harness({ states: { 2: 'test-current', 3: 'test-new-password', 4: 'test-new-password' }, requestImpl: async () => { throw new Error('{"detail":"当前密码错误"}') } })
  await h.forms()[1].props.onSubmit({ preventDefault() {} })
  assert.match(renderToStaticMarkup(h.render()), /role="alert">当前密码错误/)
  assert.equal(h.signedOut(), 0); assert.equal(h.requests[0].skipAuthRequired, true)
})

test('devices support retained client_type and epoch-second timestamps', () => {
  const h = harness()
  const html = renderToStaticMarkup(h.render())
  assert.match(html, /桌面端 · macOS/); assert.match(html, /2026/); assert.doesNotMatch(html, /1970/)
  assert.equal(h.time(1791331200), h.time(1791331200000))
})

test('revocation confirms, encodes the session ID, and refreshes authenticated sessions to detect self-revocation', async () => {
  const h = harness()
  await h.button('移除').props.onClick()
  assert.equal(h.confirmations.length, 1)
  assert.equal(h.requests[0].path, '/auth/sessions/device%2Fone'); assert.equal(h.requests[0].method, 'DELETE')
  assert.equal(h.requests[1].path, '/auth/sessions'); assert.equal(h.requests[1].skipAuthRequired, undefined)
})

test('logout-all clears local authentication only after the server succeeds', async () => {
  const h = harness()
  await h.button('退出所有设备').props.onClick()
  assert.equal(h.requests[0].path, '/auth/logout-all'); assert.equal(h.requests[0].method, 'POST'); assert.equal(h.signedOut(), 1)
  const cancel = harness({ confirm: false }); await cancel.button('退出所有设备').props.onClick(); assert.deepEqual(cancel.requests, [])
  const failed = harness({ requestImpl: async () => { throw new Error('暂时不可用') } }); await failed.button('退出所有设备').props.onClick(); assert.equal(failed.signedOut(), 0)
})

test('delete requires a password and a second confirmation before deleting all account data', async () => {
  const empty = harness({ states: { 6: true } })
  await empty.forms()[2].props.onSubmit({ preventDefault() {} }); assert.deepEqual(empty.requests, []); assert.deepEqual(empty.confirmations, [])
  const cancel = harness({ states: { 5: 'test-current', 6: true }, confirm: false })
  await cancel.forms()[2].props.onSubmit({ preventDefault() {} }); assert.equal(cancel.confirmations.length, 1); assert.deepEqual(cancel.requests, [])
  const h = harness({ states: { 5: 'test-current', 6: true } })
  await h.forms()[2].props.onSubmit({ preventDefault() {} })
  assert.equal(h.confirmations.length, 1); assert.match(h.confirmations[0], /无法撤销/)
  assert.equal(h.requests[0].path, '/account'); assert.equal(h.requests[0].method, 'DELETE')
  assert.deepEqual(JSON.parse(h.requests[0].body), { password: 'test-current' })
  assert.equal(h.states[5], ''); assert.equal(h.signedOut(), 1)
})

const main = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
const requestSource = main.slice(main.indexOf('async function requestUrl('), main.indexOf('\n/**', main.indexOf('async function requestUrl(')))

test('password reauthentication failures do not clear sessions; normal session 401 still does', async () => {
  const events = [], requests = []
  const requestUrl = vm.runInNewContext(`${requestSource}\nrequestUrl`, {
    FormData, authHeaders: () => ({}), CustomEvent: class { constructor(type) { this.type = type } }, window: { dispatchEvent: (event) => events.push(event.type) },
    fetch: async (url, options) => { requests.push(options); return { ok: false, status: 401, text: async () => '{"detail":"当前密码错误"}' } },
  })
  await assert.rejects(requestUrl('/api/v1/account/password', { skipAuthRequired: true, method: 'POST', body: '{}' }), { message: '当前密码错误' })
  assert.deepEqual(events, []); assert.equal('skipAuthRequired' in requests[0], false)
  await assert.rejects(requestUrl('/api/v1/auth/sessions'))
  assert.deepEqual(events, ['luma-auth-required'])
})
