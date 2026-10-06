import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'
import { PROACTIVE_DEFAULTS, relativeTime } from './content-data.js'
import { activityPage, activityPath, feedPath, feedSources, mergeFeedPosts } from './feed-data.js'

const source = await readFile(new URL('./feed.jsx', import.meta.url), 'utf8')
const { code } = await transformWithEsbuild(source.replace(/^import .*\n/gm, '').replace(/export default function /g, 'function ').replace(/export function /g, 'function '), 'feed.jsx', {
  loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
})
const helpers = {
  React, PROACTIVE_DEFAULTS, relativeTime, feedPath, feedSources, mergeFeedPosts,
  Icon: () => null, ContentIcon: () => null,
  ContentDialog: ({ title, children }) => React.createElement('section', { 'aria-label': title }, children),
  ActionMenu: ({ children }) => React.createElement('div', null, children),
  Markdown: ({ text }) => React.createElement('p', null, text),
}
const context = vm.createContext(helpers)
vm.runInContext(code, context)

function elements(node, predicate) {
  if (!node || typeof node !== 'object') return []
  return (predicate(node) ? [node] : []).concat(React.Children.toArray(node.props?.children).flatMap((child) => elements(child, predicate)))
}

const flush = () => new Promise((resolve) => setImmediate(resolve))

function harness(request, { script = code, name = 'FeedPage', extra = {}, props = {} } = {}) {
  const states = [], effects = [], refs = [], callbacks = []
  let stateIndex = 0, effectIndex = 0, refIndex = 0, callbackIndex = 0
  const unchanged = (left, right) => left && right && left.length === right.length && right.every((value, index) => value === left[index])
  const sandbox = vm.createContext({ ...helpers, activityPage, activityPath, ...extra,
    useState: (initial) => {
      const index = stateIndex++
      if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial
      return [states[index], (update) => { states[index] = typeof update === 'function' ? update(states[index]) : update }]
    },
    useRef: (initial) => {
      const index = refIndex++
      if (!(index in refs)) refs[index] = { current: initial }
      return refs[index]
    },
    useCallback: (callback, deps) => {
      const index = callbackIndex++
      if (!unchanged(callbacks[index]?.deps, deps)) callbacks[index] = { deps, callback }
      return callbacks[index].callback
    },
    useEffect: (effect, deps) => {
      const index = effectIndex++
      if (unchanged(effects[index]?.deps, deps)) return
      effects[index]?.cleanup?.()
      effects[index] = { deps, run: effect }
    },
  })
  vm.runInContext(script, sandbox)
  return {
    render: () => {
      stateIndex = 0; effectIndex = 0; refIndex = 0; callbackIndex = 0
      return sandbox[name]({ request, notify: () => {}, onOpenSession: () => {}, openUser: () => {}, ...props })
    },
    runEffects: () => { for (const effect of effects) if (effect.run) { effect.cleanup = effect.run(); effect.run = null } },
    states,
    unmount: () => { for (const effect of effects) effect.cleanup?.() },
  }
}

test('feed sources accept only absolute http and https URLs', () => {
  const sources = feedSources([
    { title: 'HTTPS', url: 'https://example.com/story' },
    { title: 'HTTP', url: 'http://example.com/story' },
    ...['javascript:alert(1)', 'data:text/html,<script>', 'file:///etc/passwd', 'mailto:a@example.com', '//example.com/story', '/story', 'https://user:pass@example.com/story'].map((url) => ({ title: 'unsafe', url })),
    { url: null }, null,
  ])
  assert.deepEqual(sources.map((source) => source.title), ['HTTPS', 'HTTP'])
  assert.deepEqual(feedSources(null), [])
})

test('rendered source links discard unsafe URLs, escape titles and isolate new windows', () => {
  const html = renderToStaticMarkup(React.createElement(context.FeedSources, { sources: [
    { title: '<script>injection</script>', url: 'https://example.com/path?a=1&b=2' },
    { title: 'insecure scheme', url: 'javascript:alert(1)' },
    { title: 'local file', url: 'file:///secret' },
  ] }))
  assert.equal((html.match(/<a /g) || []).length, 1)
  assert.match(html, /target="_blank" rel="noopener noreferrer"/)
  assert.match(html, /&lt;script&gt;injection&lt;\/script&gt;/)
  assert.doesNotMatch(html, /javascript:|file:|<script>/)
  assert.equal(renderToStaticMarkup(React.createElement(context.FeedSources, { sources: [] })), '')
})

test('feed pagination safely encodes cursors and merges repeated post IDs', () => {
  const path = feedPath('next/a+b & page')
  const params = new URL(`https://example.test${path}`).searchParams
  assert.equal(params.get('limit'), '20')
  assert.equal(params.get('cursor'), 'next/a+b & page')
  assert.deepEqual(mergeFeedPosts([{ id: 'one', title: 'old' }], [{ id: 'one', title: 'updated' }, { id: 'two' }]), [{ id: 'one', title: 'updated' }, { id: 'two' }])
})

test('feed keeps the load-more fallback, prevents duplicate requests and stops repeated cursors', async () => {
  const pending = []
  const page = harness((path) => new Promise((resolve, reject) => pending.push({ path, resolve, reject })))
  page.render(); page.runEffects()
  assert.equal(pending[0].path, '/feed?limit=20')
  pending[0].resolve({ items: [{ id: 'one', title: 'one' }], next_cursor: 'next/a & b' })
  await flush()
  let tree = page.render(); page.runEffects()
  const more = elements(tree, (node) => node.type === 'button' && node.props.children === '加载更多')[0]
  assert.ok(more)
  const loading = more.props.onClick()
  more.props.onClick()
  assert.equal(pending.length, 2)
  assert.equal(new URL(`https://example.test${pending[1].path}`).searchParams.get('cursor'), 'next/a & b')
  pending[1].resolve({ items: [{ id: 'one', title: 'updated' }, { id: 'two', title: 'two' }], next_cursor: 'next/a & b' })
  await loading
  tree = page.render(); page.runEffects()
  assert.deepEqual(Array.from(page.states[0], (post) => post.id), ['one', 'two'])
  assert.equal(page.states[0][0].title, 'updated')
  assert.equal(page.states[1], null)
  assert.equal(elements(tree, (node) => node.type === 'button' && node.props.children === '加载更多').length, 0)
  page.unmount()
})

test('feed operations follow the contract and deletion waits for in-app confirmation', async () => {
  const calls = [], opened = [], notifications = []
  const post = { id: 'post/a', title: '帖子', body_markdown: '正文', liked: false }
  const page = harness(async (path, options) => {
    calls.push({ path, options })
    if (path.startsWith('/feed?')) return { items: [post], next_cursor: null }
    if (path.endsWith('/discuss')) return { session_id: 'session-a' }
    return null
  }, { props: { notify: (message) => notifications.push(message), onOpenSession: async (id, options) => { opened.push({ id, options }) } } })
  page.render(); page.runEffects(); await flush()
  let tree = page.render()
  let card = elements(tree, (node) => node.type?.name === 'FeedPostCard')[0]
  await card.props.onFeedback(post, 'like')
  assert.equal(calls[1].path, '/feed/post%2Fa/feedback')
  assert.equal(JSON.parse(calls[1].options.body).action, 'like')
  assert.equal(page.states[0][0].liked, true)
  await card.props.onDiscuss(post)
  assert.equal(opened.length, 1)
  assert.equal(opened[0].id, 'session-a')
  assert.equal(opened[0].options.focus, true)
  card.props.onDelete(post)
  assert.equal(calls.filter((call) => call.options?.method === 'DELETE').length, 0)
  tree = page.render()
  const dialog = elements(tree, (node) => node.type === helpers.ContentDialog && node.props.title === '删除动态')[0]
  assert.ok(dialog)
  await elements(dialog, (node) => node.type === 'button' && node.props.children === '删除')[0].props.onClick()
  assert.equal(calls.at(-1).path, '/feed/post%2Fa')
  assert.equal(calls.at(-1).options.method, 'DELETE')
  assert.equal(page.states[0].length, 0)
  assert.ok(notifications.includes('已删除'))
  page.unmount()
})

test('saving feed instructions reads the latest prefs and preserves other fields', async () => {
  const calls = [], notices = []
  let prefsReads = 0
  const page = harness(async (path, options) => {
    calls.push({ path, options })
    if (path.startsWith('/feed?')) return { items: [], next_cursor: null }
    if (path === '/proactive/prefs' && !options) return ++prefsReads === 1 ? { ...PROACTIVE_DEFAULTS, feed_instructions: '旧说明' } : { ...PROACTIVE_DEFAULTS, enabled: false, max_per_day: 5, feed_per_day: 3, feed_instructions: '并行更改' }
    return null
  }, { props: { notify: (message) => notices.push(message) } })
  page.render(); page.runEffects(); await flush()
  let tree = page.render()
  await elements(tree, (node) => node.type === 'button' && node.props['aria-label'] === '动态说明')[0].props.onClick()
  tree = page.render()
  elements(tree, (node) => node.type === 'textarea')[0].props.onChange({ target: { value: '新说明' } })
  tree = page.render()
  await elements(tree, (node) => node.type === 'form')[0].props.onSubmit({ preventDefault: () => {} })
  const saved = calls.find((call) => call.options?.method === 'PUT')
  const savedPrefs = JSON.parse(saved.options.body)
  assert.equal(prefsReads, 2)
  assert.equal(saved.path, '/proactive/prefs')
  assert.equal(savedPrefs.feed_instructions, '新说明')
  assert.equal(savedPrefs.enabled, false)
  assert.equal(savedPrefs.max_per_day, 5)
  assert.equal(savedPrefs.feed_per_day, 3)
  assert.ok(notices.includes('已保存'))
  page.unmount()
})

test('unmounted feed pages ignore late list responses', async () => {
  let resolve
  const page = harness(() => new Promise((done) => { resolve = done }))
  page.render(); page.runEffects(); page.unmount()
  resolve({ items: [{ id: 'late' }], next_cursor: 'next' })
  await flush()
  assert.equal(page.states[0].length, 0)
  assert.equal(page.states[1], null)
})

test('admin activity filters encode input and normalize legacy array or paged results', () => {
  const params = new URL(`https://example.test${activityPath(' user/a & b ', ' tool+call ', 'cursor/=')}`).searchParams
  assert.equal(params.get('limit'), '100')
  assert.equal(params.get('user_id'), 'user/a & b')
  assert.equal(params.get('kind'), 'tool+call')
  assert.equal(params.get('cursor'), 'cursor/=')
  assert.deepEqual(activityPage([{ id: 'one' }]), { items: [{ id: 'one' }], next_cursor: null })
  assert.deepEqual(activityPage({ items: [{ id: 'one' }], next_cursor: 'next' }), { items: [{ id: 'one' }], next_cursor: 'next' })
  assert.deepEqual(activityPage(null), { items: [], next_cursor: null })
})

test('admin activity paginates cursors and resets paging when filters change', async () => {
  const adminSource = await readFile(new URL('./admin.jsx', import.meta.url), 'utf8')
  const fragment = adminSource.slice(adminSource.indexOf('function ActivityLog('), adminSource.indexOf('function Audit('))
  const transformed = await transformWithEsbuild(fragment, 'activity.jsx', { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' })
  const pending = []
  const page = harness((path) => new Promise((resolve) => pending.push({ path, resolve })), {
    script: transformed.code, name: 'ActivityLog',
    extra: { Section: () => null, Table: () => null, Loading: () => null, ErrorBox: () => null, Pill: () => null, fmtTime: (value) => value },
  })
  page.render(); page.runEffects()
  assert.equal(pending[0].path, '/activity?limit=100')
  pending[0].resolve({ items: [{ id: 'one' }], next_cursor: 'page/two' }); await flush()
  let tree = page.render()
  elements(tree, (node) => node.type === 'button' && node.props.children === '下一页')[0].props.onClick()
  tree = page.render(); page.runEffects()
  assert.equal(new URL(`https://example.test${pending[1].path}`).searchParams.get('cursor'), 'page/two')
  const filter = tree.props.actions
  elements(filter, (node) => node.type === 'input' && node.props['aria-label'] === '运行日志用户 ID')[0].props.onChange({ target: { value: 'user+one' } })
  elements(filter, (node) => node.type === 'input' && node.props['aria-label'] === '运行日志类型')[0].props.onChange({ target: { value: 'tool' } })
  tree = page.render()
  tree.props.actions.props.onSubmit({ preventDefault: () => {} })
  tree = page.render(); page.runEffects()
  const params = new URL(`https://example.test${pending[2].path}`).searchParams
  assert.equal(params.get('user_id'), 'user+one')
  assert.equal(params.get('kind'), 'tool')
  assert.equal(params.has('cursor'), false)
  pending[1].resolve({ items: [{ id: 'stale' }], next_cursor: null }); await flush()
  assert.equal(page.states[4].data, null)
  pending[2].resolve({ items: [{ id: 'filtered' }], next_cursor: null }); await flush()
  assert.equal(page.states[4].data.items[0].id, 'filtered')
  page.unmount()
})
