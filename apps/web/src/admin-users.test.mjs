import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'

const source = await readFile(new URL('./admin.jsx', import.meta.url), 'utf8')
const fragment = source.slice(source.indexOf('function Users('), source.indexOf('function Sessions('))
const { code } = await transformWithEsbuild(fragment, 'admin-users.jsx', { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' })

function harness({ role = 'user', status = 'active', self = false, confirm = true, failure } = {}) {
  const states = ['', '', '', ''], requests = []
  const user = { user_id: 'user/one', username: 'reader', display_name: 'Reader', email: 'reader@example.test', role, status }
  let index = 0, reloads = 0
  const Table = ({ columns, rows }) => React.createElement('table', null, React.createElement('tbody', null, rows.map((row) => React.createElement('tr', { key: row.user_id }, columns.map((column) => React.createElement('td', { key: column.key }, column.render ? column.render(row) : row[column.key]))))))
  const context = {
    React, useState: (value) => { const current = index++; return [states[current] ?? value, (update) => { states[current] = update }] },
    useAdmin: () => ({ data: { total: 1, items: [user] }, error: '', reload: async () => { reloads += 1 } }),
    Section: ({ actions, children }) => React.createElement('section', null, actions, children), Icon: () => null, Table,
    Pill: ({ children }) => React.createElement('span', null, children), Loading: () => null,
    ErrorBox: ({ message }) => React.createElement('p', { role: 'alert' }, message), fmtNum: (value) => value ?? '—', fmtAgo: () => '—', clientLabels: {}, userName: (value) => value.display_name || value.username || value.user_id,
    window: { confirm: () => confirm },
  }
  vm.createContext(context); vm.runInContext(code, context)
  const render = () => { index = 0; return context.Users({ request: async (path, options) => { requests.push({ path, ...options }); if (failure) throw new Error(failure) }, openUser: () => {}, currentUserId: self ? user.user_id : 'admin-self' }) }
  const table = () => React.Children.toArray(render().props.children).find((element) => React.isValidElement(element) && element.type === Table)
  const actions = () => table().props.columns.find((column) => column.key === 'actions').render(user)
  const buttons = () => React.Children.toArray(actions().props.children)
  return { render, requests, buttons, actions, reloads: () => reloads }
}

test('user management displays roles/status and sends role/status patches with encoded IDs', async () => {
  for (const [role, status, expectedRole, expectedStatus] of [['user', 'active', 'admin', 'disabled'], ['admin', 'disabled', 'user', 'active']]) {
    const h = harness({ role, status })
    const html = renderToStaticMarkup(h.render())
    assert.match(html, role === 'admin' ? /取消管理员/ : /设为管理员/)
    assert.match(html, status === 'disabled' ? /已禁用/ : /正常/)
    await h.buttons()[0].props.onClick(); await h.buttons()[1].props.onClick()
    assert.equal(h.requests[0].path, '/users/user%2Fone'); assert.equal(h.requests[0].method, 'PATCH')
    assert.deepEqual(JSON.parse(h.requests[0].body), { role: expectedRole }); assert.deepEqual(JSON.parse(h.requests[1].body), { status: expectedStatus })
    assert.equal(h.reloads(), 2)
  }
})

test('self-administration is disabled, row navigation is stopped, and canceling disable sends no patch', async () => {
  const h = harness({ self: true })
  for (const button of h.buttons()) { assert.equal(button.props.disabled, true); await button.props.onClick() }
  assert.deepEqual(h.requests, [])
  let stopped = false; h.actions().props.onClick({ stopPropagation: () => { stopped = true } }); assert.equal(stopped, true)
  const cancel = harness({ confirm: false }); await cancel.buttons()[1].props.onClick(); assert.deepEqual(cancel.requests, [])
})

test('administrator mutation errors remain visible without claiming success', async () => {
  const h = harness({ failure: '不能禁用自己的账户' })
  await h.buttons()[1].props.onClick()
  assert.match(renderToStaticMarkup(h.render()), /role="alert">不能禁用自己的账户/); assert.equal(h.reloads(), 0)
})

test('admin request forwards mutation options and gets role from the shared account endpoint', async () => {
  const main = await readFile(new URL('./main.jsx', import.meta.url), 'utf8')
  const fragment = main.slice(main.indexOf('function adminRequest('), main.indexOf('async function requestUrl('))
  const calls = []
  const adminRequest = vm.runInNewContext(`${fragment}\nadminRequest`, { ADMIN_API_URL: '/api/admin', requestUrl: async (...args) => calls.push(args), request: async (...args) => calls.push(args) })
  const options = { method: 'PATCH', body: '{"role":"admin"}' }
  await adminRequest('/users/user-one', options); await adminRequest('/me')
  assert.equal(calls[0][0], '/api/admin/users/user-one'); assert.equal(calls[0][1], options); assert.equal(calls[1][0], '/auth/me')
})
