import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { transformWithEsbuild } from 'vite'
import { PURPOSE_LABELS, axisTokens, chartScale, formatLatency, formatTokens, modelAnalysisPath, usageNumber } from './usage-data.js'

const source = await readFile(new URL('./usage-panels.jsx', import.meta.url), 'utf8')
const { code } = await transformWithEsbuild(source.replace(/^import .*\n/gm, '').replace(/export function /g, 'function '), 'usage-panels.jsx', {
  loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment',
})
const helpers = { React, PURPOSE_LABELS, axisTokens, chartScale, formatLatency, formatTokens, usageNumber }
const context = vm.createContext({ ...helpers })
vm.runInContext(code, context)
const render = (name, props) => renderToStaticMarkup(React.createElement(context[name], props))

test('chart axes follow the data and remain finite for empty or invalid values', () => {
  assert.deepEqual(chartScale([10500], { integer: true }), { maximum: 15000, ticks: [0, 5000, 10000, 15000] })
  assert.deepEqual(chartScale([], { integer: true }), { maximum: 1, ticks: [0, 1] })
  assert.deepEqual(chartScale([NaN, -1, undefined], { integer: true }), { maximum: 1, ticks: [0, 1] })
  assert.ok(chartScale([.012]).maximum >= .012)
  assert.equal(axisTokens(15000), '15k')
  assert.equal(formatLatency(null), '—')
  assert.equal(formatLatency(1550), '1.6 s')
})

test('model filters encode model and user IDs into the API query', () => {
  const path = modelAnalysisPath('24h', ' model/a & b ', ' user+id ')
  const params = new URL(`https://example.test${path}`).searchParams
  assert.equal(params.get('range'), '24h')
  assert.equal(params.get('model'), 'model/a & b')
  assert.equal(params.get('user_id'), 'user+id')
  assert.equal(modelAnalysisPath('7d'), '/models?range=7d')
})

test('daily usage charts draw every day for each supported range with computed ticks', () => {
  for (const days of [7, 30, 90]) {
    const daily = Array.from({ length: days }, (_, index) => ({ date: new Date(Date.UTC(2026, 9, index + 1)).toISOString().slice(0, 10), calls: 2, total_tokens: index === 1 ? 10500 : 0 }))
    const html = render('UsageDailyChart', { daily })
    assert.equal((html.match(/class="usage-bar"/g) || []).length, days)
    assert.match(html, />15k<\/text>/)
    assert.match(html, /2026-10-02 · 10,500 tokens · 2 次请求/)
    assert.doesNotMatch(html, /NaN|Infinity/)
  }
  assert.match(render('UsageDailyChart', { daily: [] }), /这段时间还没有模型用量/)
})

test('usage report includes estimation, performance, distributions and safe session titles', () => {
  const data = {
    totals: { calls: 4, prompt_tokens: 700, completion_tokens: 300, total_tokens: 1000, estimated_ratio: .25 },
    performance: { first_token_ms_avg: 125, duration_ms_p50: 500, duration_ms_p95: 1500 },
    daily: [{ date: '2026-10-06', calls: 4, total_tokens: 1000 }],
    purposes: [{ purpose: 'chat_round', total_tokens: 750 }, { purpose: 'summary', total_tokens: 250 }],
    models: [{ model: 'test-model', total_tokens: 1000 }],
    top_sessions: [{ session_id: 'session-1', title: '<script>secret</script>', calls: 4, total_tokens: 1000 }],
  }
  const html = render('UserUsageReport', { data, onOpenSession: () => {} })
  assert.match(html, /部分为估算 · 25.0%/)
  assert.match(html, /125 ms/)
  assert.match(html, /P50 500 ms · P95 1.5 s/)
  assert.match(html, /对话轮次/)
  assert.match(html, /会话摘要/)
  assert.match(html, /width:75%/)
  assert.match(html, /test-model/)
  assert.match(html, /&lt;script&gt;secret&lt;\/script&gt;/)
  assert.doesNotMatch(html, /<script>/)
  const exact = render('UserUsageReport', { data: { ...data, totals: { ...data.totals, estimated_ratio: 0 } }, onOpenSession: () => {} })
  assert.doesNotMatch(exact, /部分为估算/)
})

function elements(node, predicate) {
  if (!node || typeof node !== 'object') return []
  const found = predicate(node) ? [node] : []
  return found.concat(React.Children.toArray(node.props?.children).flatMap((child) => elements(child, predicate)))
}

test('Top session buttons preserve the actual session ID for navigation', () => {
  const opened = []
  const tree = context.UserUsageReport({ data: { top_sessions: [{ session_id: 's/a', title: '会话', calls: 1, total_tokens: 100 }] }, onOpenSession: (id) => opened.push(id) })
  const button = elements(tree, (node) => node.type === 'button')[0]
  button.props.onClick()
  assert.deepEqual(opened, ['s/a'])
})

test('model charts show percentages and preserve gaps for missing latency measurements', () => {
  const series = [
    { bucket: '2026-10-06T01:00:00Z', calls: 2, total_tokens: 100, first_token_ms_p95: 100, error_rate: .25 },
    { bucket: '2026-10-06T02+00:00', calls: 0, total_tokens: 0, first_token_ms_p95: null, error_rate: 0 },
    { bucket: '2026-10-06T03:00:00Z', calls: 1, total_tokens: 50, first_token_ms_p95: 500, error_rate: 0 },
  ]
  const latency = render('ModelPerformanceChart', { series, metric: 'first_token_ms_p95', hourly: true })
  assert.equal((latency.match(/class="usage-point "/g) || []).length, 2)
  assert.match(latency, /d="M [^"]+ M /)
  assert.match(latency, /02:00/)
  assert.doesNotMatch(latency, /NaN|Infinity/)
  assert.match(render('ModelPerformanceChart', { series, metric: 'error_rate' }), /错误率 25.0%/)
})

test('charts handle empty and single-point series; 90 days stay inside the plotted bounds', () => {
  assert.match(render('ModelPerformanceChart', { series: [], metric: 'first_token_ms_p95' }), /暂无数据/)
  assert.match(render('ModelPerformanceChart', { series: [{ bucket: '2026-10-06', first_token_ms_p95: null }], metric: 'first_token_ms_p95' }), /暂无首字延迟数据/)
  const single = render('ModelPerformanceChart', { series: [{ bucket: '2026-10-06', first_token_ms_p95: 200 }], metric: 'first_token_ms_p95' })
  assert.match(single, /cx="298"/)
  assert.equal((single.match(/<circle/g) || []).length, 1)
  assert.doesNotMatch(single, /NaN|Infinity/)
  const oneDay = render('UsageDailyChart', { daily: [{ date: '2026-10-06', calls: 1, total_tokens: 2 }] })
  assert.equal((oneDay.match(/class="usage-bar"/g) || []).length, 1)
  const daily = Array.from({ length: 90 }, (_, index) => ({ date: new Date(Date.UTC(2026, 7, index + 1)).toISOString().slice(0, 10), total_tokens: index + 1 }))
  const html = render('UsageDailyChart', { daily })
  const rectangles = [...html.matchAll(/class="usage-bar" x="([^"]+)" y="([^"]+)" width="([^"]+)" height="([^"]+)"/g)]
  assert.equal(rectangles.length, 90)
  for (const [, x, y, width, height] of rectangles) {
    assert.ok(Number(width) > 0)
    assert.ok(Number(x) >= 50 && Number(x) + Number(width) <= 546)
    assert.ok(Number(y) >= 16 && Number(y) + Number(height) <= 186)
  }
  const labels = [...html.matchAll(/<text[^>]*y="210"[^>]*>([^<]+)<\/text>/g)]
  assert.ok(labels.length <= 7)
  assert.equal(labels[0][1], daily[0].date.slice(5))
  assert.equal(labels[labels.length - 1][1], daily[89].date.slice(5))
})

function panelHarness(request, { script = code, name = 'UsagePanel', statePosition = 1, extra = {}, props = {} } = {}) {
  const states = [], effects = [], refs = []
  let stateIndex = 0, effectIndex = 0, refIndex = 0
  const sandbox = vm.createContext({ ...helpers, ...extra,
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
    useEffect: (effect, deps) => {
      const index = effectIndex++
      const previous = effects[index]
      if (previous && deps.every((value, depIndex) => value === previous.deps[depIndex])) return
      previous?.cleanup?.()
      effects[index] = { deps, run: effect }
    },
  })
  vm.runInContext(script, sandbox)
  return {
    render: () => { stateIndex = 0; effectIndex = 0; refIndex = 0; return sandbox[name]({ request, onOpenSession: () => {}, openUser: () => {}, openSession: () => {}, ...props }) },
    runEffects: () => { for (const effect of effects) if (effect.run) { effect.cleanup = effect.run(); effect.run = null } },
    state: () => states[statePosition],
    unmount: () => { for (const effect of effects) effect.cleanup?.() },
  }
}

test('usage range changes discard a stale response and a failed request can be retried', async () => {
  const pending = []
  const harness = panelHarness((path) => new Promise((resolve, reject) => pending.push({ path, resolve, reject })))
  let tree = harness.render()
  harness.runEffects()
  assert.equal(pending[0].path, '/usage?range=7d')
  const thirty = elements(tree, (node) => node.type === 'button' && node.props.children === '30 天')[0]
  thirty.props.onClick()
  tree = harness.render()
  harness.runEffects()
  assert.equal(pending[1].path, '/usage?range=30d')
  pending[0].resolve({ range: '7d' })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(harness.state().data, null)
  pending[1].reject(new Error('network'))
  await new Promise((resolve) => setImmediate(resolve))
  tree = harness.render()
  assert.equal(harness.state().loading, false)
  const retry = elements(tree, (node) => node.type === 'button' && node.props.children === '重试')[0]
  retry.props.onClick()
  harness.render()
  harness.runEffects()
  assert.equal(pending[2].path, '/usage?range=30d')
  pending[2].resolve({ range: '30d', totals: { calls: 1 } })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(harness.state().data.range, '30d')
})

test('Top navigation validates existence before switching and keeps deleted sessions in the usage view', async () => {
  const pending = [], opened = []
  const harness = panelHarness((path) => new Promise((resolve, reject) => pending.push({ path, resolve, reject })), { props: { onOpenSession: (id) => opened.push(id) } })
  harness.render()
  harness.runEffects()
  pending[0].resolve({ totals: { calls: 2 }, top_sessions: [{ session_id: 'deleted/a', title: '已删除的会话' }] })
  await new Promise((resolve) => setImmediate(resolve))
  let tree = harness.render()
  let report = elements(tree, (node) => node.type?.name === 'UserUsageReport')[0]
  const deleted = report.props.onOpenSession('deleted/a')
  assert.equal(pending[1].path, '/sessions/deleted%2Fa')
  pending[1].reject(Object.assign(new Error('missing'), { status: 404 }))
  await deleted
  tree = harness.render()
  assert.deepEqual(opened, [])
  assert.equal(harness.state().data.totals.calls, 2)
  assert.equal(elements(tree, (node) => node.props?.role === 'alert')[0].props.children, '该会话已删除或无法访问')
  report = elements(tree, (node) => node.type?.name === 'UserUsageReport')[0]
  const success = report.props.onOpenSession('existing')
  pending[2].resolve({ id: 'existing' })
  await success
  assert.deepEqual(opened, ['existing'])
  tree = harness.render()
  report = elements(tree, (node) => node.type?.name === 'UserUsageReport')[0]
  const afterClose = report.props.onOpenSession('late')
  harness.unmount()
  pending[3].resolve({ id: 'late' })
  await afterClose
  assert.deepEqual(opened, ['existing'])
})

test('admin model report includes each performance metric and omits response contents', async () => {
  const adminSource = await readFile(new URL('./admin.jsx', import.meta.url), 'utf8')
  const fragments = [
    adminSource.slice(adminSource.indexOf('function fmtNum('), adminSource.indexOf('function useAdmin(')),
    adminSource.slice(adminSource.indexOf('function Stat('), adminSource.indexOf('function hourKeys(')),
    adminSource.slice(adminSource.indexOf('function ModelMetric('), adminSource.indexOf('function Models(')),
  ].join('\n')
  const transformed = await transformWithEsbuild(fragments, 'admin-models.jsx', { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' })
  const sandbox = vm.createContext({ React, PURPOSE_LABELS, ModelPerformanceChart: context.ModelPerformanceChart })
  vm.runInContext(transformed.code, sandbox)
  const metric = { avg: 100, p50: 80, p95: 200, p99: 300 }
  const data = {
    models: [{ model: 'test-model', calls: 10, success_rate: .9, error_types: { timeout: 1 }, first_token_ms: metric, duration_ms: metric, tokens_per_sec: { avg: 12.4, p50: 11.2 }, prompt_tokens: 800, completion_tokens: 200, total_tokens: 1000, tokens_per_call: 100 }],
    users: [{ user_id: 'user-1', user: { display_name: '用户一' }, calls: 10, total_tokens: 1000, first_token_ms_avg: 100 }],
    slow_calls: [{ id: 'call-1', user_id: 'user-1', session_id: 'session-1', purpose: 'summary', model: 'test-model', status: 'error', first_token_ms: 100, duration_ms: 1000, tokens_per_sec: 12, prompt_tokens: 80, completion_tokens: 20, total_tokens: 100, created_at: '2026-10-06T01:00:00Z', prompt: 'PRIVATE_PROMPT', content: 'PRIVATE_RESPONSE' }],
  }
  const html = renderToStaticMarkup(React.createElement(sandbox.ModelAnalysisReport, { data, range: '7d', openUser: () => {}, openSession: () => {} }))
  assert.match(html, /90.0%/)
  assert.match(html, /timeout 1/)
  assert.match(html, /P50 80 ms · P95 200 ms · P99 300 ms/)
  assert.match(html, /12.4/)
  assert.match(html, /P50 11.2/)
  assert.match(html, /用户一/)
  assert.match(html, /session-1/)
  assert.match(html, /会话摘要/)
  assert.doesNotMatch(html, /PRIVATE_PROMPT|PRIVATE_RESPONSE/)
  assert.match(adminSource, /id: 'models', label: '模型'/)
  assert.match(adminSource, /counts\?\.tokens_today/)
  assert.match(adminSource, /counts\?\.model_calls_today/)
})

test('admin range, model and submitted user filters drive the model-analysis endpoint', async () => {
  const adminSource = await readFile(new URL('./admin.jsx', import.meta.url), 'utf8')
  const fragment = adminSource.slice(adminSource.indexOf('function Models('), adminSource.indexOf('function Instances('))
  const transformed = await transformWithEsbuild(fragment, 'admin-models.jsx', { loader: 'jsx', jsx: 'transform', jsxFactory: 'React.createElement', jsxFragment: 'React.Fragment' })
  const pending = []
  const harness = panelHarness((path) => new Promise((resolve, reject) => pending.push({ path, resolve, reject })), {
    script: transformed.code, name: 'Models', statePosition: 5,
    extra: { modelAnalysisPath, Icon: () => null, Loading: () => null, ErrorBox: () => null, ModelAnalysisReport: () => null },
  })
  let tree = harness.render()
  harness.runEffects()
  assert.equal(pending[0].path, '/models?range=7d')
  pending[0].resolve({ available_models: ['model/a'], models: [] })
  await new Promise((resolve) => setImmediate(resolve))
  tree = harness.render()
  elements(tree, (node) => node.type === 'select')[0].props.onChange({ target: { value: 'model/a' } })
  tree = harness.render()
  harness.runEffects()
  assert.equal(pending[1].path, '/models?range=7d&model=model%2Fa')
  elements(tree, (node) => node.type === 'input')[0].props.onChange({ target: { value: ' user+1 ' } })
  tree = harness.render()
  harness.runEffects()
  assert.equal(pending.length, 2)
  elements(tree, (node) => node.type === 'form')[0].props.onSubmit({ preventDefault: () => {} })
  tree = harness.render()
  harness.runEffects()
  assert.equal(pending[2].path, '/models?range=7d&model=model%2Fa&user_id=user%2B1')
  elements(tree, (node) => node.type === 'button' && node.props.children === '24 小时')[0].props.onClick()
  harness.render()
  harness.runEffects()
  assert.equal(pending[3].path, '/models?range=24h&model=model%2Fa&user_id=user%2B1')
  pending[1].resolve({ range: '7d' })
  pending[2].resolve({ range: '7d' })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(harness.state().data, null)
  pending[3].resolve({ range: '24h', models: [] })
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(harness.state().data.range, '24h')
  tree = harness.render()
  const select = elements(tree, (node) => node.type === 'select')[0]
  assert.equal(select.props.value, 'model/a')
  assert.ok(elements(select, (node) => node.type === 'option' && node.props.value === 'model/a').length)
  elements(tree, (node) => node.type === 'button' && node.props.children === '清除筛选')[0].props.onClick()
  harness.render()
  harness.runEffects()
  assert.equal(pending[4].path, '/models?range=24h')
  pending[4].resolve({ models: [] })
  await new Promise((resolve) => setImmediate(resolve))
})
