import React, { useCallback, useEffect, useMemo, useState } from 'react'
import Icon from './icons.jsx'
import LumaLogo from './logo.jsx'
import { ModelPerformanceChart } from './usage-panels.jsx'
import { PURPOSE_LABELS, modelAnalysisPath } from './usage-data.js'
import { activityPage, activityPath } from './feed-data.js'
import './feed.css'

// Operator console. Loaded lazily from /admin so the chat bundle stays small;
// User management mutations go through the authenticated /api/admin routes.

const sections = [
  { id: 'overview', label: '概览', icon: 'activity' },
  { id: 'instances', label: '运行实例', icon: 'screen-user' },
  { id: 'users', label: '用户', icon: 'invite' },
  { id: 'sessions', label: '对话', icon: 'chat' },
  { id: 'models', label: '模型', icon: 'activity' },
  { id: 'jobs', label: '作业', icon: 'goal' },
  { id: 'requests', label: '请求日志', icon: 'search' },
  { id: 'activity', label: '运行日志', icon: 'activity' },
  { id: 'audit', label: '审计', icon: 'shield' },
]

const clientLabels = { web: '网页', desktop: '桌面端', flutter: 'Flutter', ios: 'iOS', android: 'Android', macos: 'macOS', windows: 'Windows', linux: 'Linux', unknown: '未知' }

function fmtNum(value) {
  if (value === null || value === undefined || value === '') return '—'
  return Number(value).toLocaleString('zh-CN')
}

function fmtMs(value) {
  if (value === null || value === undefined) return '—'
  return value >= 1000 ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)} s` : `${Math.round(value)} ms`
}

function fmtTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}

function fmtAgo(value) {
  if (!value) return '—'
  const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000)
  if (Number.isNaN(seconds)) return '—'
  if (seconds < 60) return `${Math.round(seconds)} 秒前`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时前`
  return `${Math.round(seconds / 86400)} 天前`
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—'
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} 秒`
  return days ? `${days} 天 ${hours} 小时` : hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分钟`
}

function userName(user) {
  if (!user || !user.user_id) return '—'
  return user.display_name || user.username || user.email || user.user_id
}

function useAdmin(request, endpoint, refreshMs = 0) {
  const [state, setState] = useState({ data: null, error: '', loading: true })
  const load = useCallback(() => {
    if (!endpoint) return Promise.resolve()
    return request(endpoint)
      .then((data) => setState({ data, error: '', loading: false }))
      .catch((error) => setState((current) => ({ ...current, error: error.message || '加载失败', loading: false })))
  }, [request, endpoint])
  useEffect(() => {
    setState((current) => ({ ...current, loading: true }))
    load()
    if (!refreshMs) return undefined
    const timer = window.setInterval(load, refreshMs)
    return () => window.clearInterval(timer)
  }, [load, refreshMs])
  return { ...state, reload: load }
}

function Stat({ label, value, hint, tone }) {
  return <div className={`admin-stat ${tone || ''}`}><span>{label}</span><strong>{value}</strong>{hint && <small>{hint}</small>}</div>
}

function Pill({ tone = '', children }) {
  return <span className={`admin-pill ${tone}`}>{children}</span>
}

function statusTone(status) {
  if (['running', 'ready', 'active', 'succeeded', 'approved'].includes(status)) return 'good'
  if (['failed', 'error', 'rejected', 'expired'].includes(status)) return 'bad'
  if (['queued', 'starting', 'pending', 'waiting_approval'].includes(status)) return 'warn'
  return ''
}

const SANDBOX_STATUS = { running: '运行中', ready: '就绪', starting: '启动中', stopped: '已销毁', expired: '已过期', failed: '失败' }

function sandboxStatus(status) {
  return SANDBOX_STATUS[status] || status
}

function Table({ columns, rows, onRowClick, empty = '暂无数据', rowKey = (row, index) => row.id || index }) {
  if (!rows?.length) return <div className="admin-empty">{empty}</div>
  return <div className="admin-table-wrap"><table className="admin-table"><thead><tr>{columns.map((column) => <th key={column.key} className={column.align || ''}>{column.label}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={rowKey(row, index)} className={onRowClick ? 'clickable' : ''} onClick={onRowClick ? () => onRowClick(row) : undefined}>{columns.map((column) => <td key={column.key} className={column.align || ''}>{column.render ? column.render(row) : (row[column.key] ?? '—')}</td>)}</tr>)}</tbody></table></div>
}

function Section({ title, subtitle, actions, children }) {
  return <section className="admin-section"><header><div><h3>{title}</h3>{subtitle && <p>{subtitle}</p>}</div>{actions && <div className="admin-actions">{actions}</div>}</header>{children}</section>
}

function hourKeys(count = 24) {
  const now = new Date()
  now.setUTCMinutes(0, 0, 0)
  return Array.from({ length: count }, (_, index) => new Date(now.getTime() - (count - 1 - index) * 3600000).toISOString().slice(0, 13))
}

function BarChart({ series, legend }) {
  const keys = hourKeys()
  const totals = keys.map((key) => series.reduce((sum, item) => sum + (item.values[key] || 0), 0))
  const max = Math.max(1, ...totals)
  const width = 720
  const height = 160
  const gap = 4
  const barWidth = (width - gap * (keys.length - 1)) / keys.length
  return <div className="admin-chart">
    <svg viewBox={`0 0 ${width} ${height + 22}`} role="img" aria-label="近 24 小时柱状图">
      {keys.map((key, index) => {
        let offset = 0
        const x = index * (barWidth + gap)
        return <g key={key}>
          <title>{`${new Date(`${key}:00:00Z`).getHours()} 时 · ${series.map((item) => `${item.label} ${item.values[key] || 0}`).join(' · ')}`}</title>
          <rect x={x} y={0} width={barWidth} height={height} className="admin-chart-track" rx="3" />
          {series.map((item) => {
            const value = item.values[key] || 0
            const barHeight = (value / max) * height
            offset += barHeight
            return value ? <rect key={item.label} x={x} y={height - offset} width={barWidth} height={barHeight} className={`admin-chart-bar ${item.tone}`} rx="3" /> : null
          })}
          {index % 4 === 3 && <text x={x + barWidth / 2} y={height + 16} textAnchor="middle">{new Date(`${key}:00:00Z`).getHours()}:00</text>}
        </g>
      })}
    </svg>
    <div className="admin-legend">{legend.map((item) => <span key={item.label}><i className={item.tone} />{item.label}</span>)}<span className="muted">峰值 {fmtNum(max)}</span></div>
  </div>
}

function KeyValues({ items }) {
  return <dl className="admin-kv">{items.filter(Boolean).map(([key, value]) => <React.Fragment key={key}><dt>{key}</dt><dd>{value ?? '—'}</dd></React.Fragment>)}</dl>
}

function Overview({ request, openSection }) {
  const { data, error } = useAdmin(request, '/overview', 30000)
  if (error && !data) return <ErrorBox message={error} />
  if (!data) return <Loading />
  const messageSeries = { user: {}, assistant: {} }
  for (const row of data.series.messages) if (messageSeries[row.role]) messageSeries[row.role][row.hour] = Number(row.count)
  const requestOk = {}
  const requestErr = {}
  for (const row of data.series.requests) {
    const key = row.hour.slice(0, 13)
    requestErr[key] = Number(row.errors || 0)
    requestOk[key] = Number(row.count || 0) - requestErr[key]
  }
  const quality = data.quality_24h
  const jobs = data.runtime.jobs || {}
  return <>
    <div className="admin-stats">
      <Stat label="用户总数" value={fmtNum(data.users.total)} hint={`24 小时新增 ${fmtNum(data.users.new_24h)}`} />
      <Stat label="24 小时活跃" value={fmtNum(data.users.active_24h)} hint={`发过消息 ${fmtNum(data.users.chatting_24h)} · 7 天 ${fmtNum(data.users.chatting_7d)}`} />
      <Stat label="对话 / 消息" value={`${fmtNum(data.conversations.sessions)} / ${fmtNum(data.conversations.messages)}`} hint={`24 小时新对话 ${fmtNum(data.conversations.sessions_24h)} · 提问 ${fmtNum(data.conversations.user_messages_24h)}`} />
      <Stat label="服务进程" value={fmtNum(data.runtime.workers_live)} hint="在线 uvicorn worker" tone="accent" />
      <Stat label="Agent 沙箱" value={`${fmtNum(data.runtime.sandboxes_active)} / ${fmtNum(data.runtime.sandboxes_max)}`} hint="运行中 / 上限" tone="accent" />
      <Stat label="24 小时回复" value={fmtNum(quality.replies)} hint={`失败 ${fmtNum(quality.failed)}`} tone={quality.failed ? 'warn' : ''} />
      <Stat label="首字耗时" value={fmtMs(quality.first_token_avg_ms)} hint={`P95 ${fmtMs(quality.first_token_p95_ms)}`} />
      <Stat label="完整回复耗时" value={fmtMs(quality.duration_avg_ms)} hint={`P95 ${fmtMs(quality.duration_p95_ms)}`} />
      <Stat label="今日 tokens" value={fmtNum(data.counts?.tokens_today ?? 0)} hint="所有模型调用的输入与输出" tone="accent" />
      <Stat label="今日模型调用" value={fmtNum(data.counts?.model_calls_today ?? 0)} hint="含摘要、决策与记忆提取" />
    </div>
    <div className="admin-grid two">
      <Section title="近 24 小时消息"><BarChart series={[{ label: '用户提问', tone: 'blue', values: messageSeries.user }, { label: 'AI 回复', tone: 'soft', values: messageSeries.assistant }]} legend={[{ label: '用户提问', tone: 'blue' }, { label: 'AI 回复', tone: 'soft' }]} /></Section>
      <Section title="近 24 小时 API 请求"><BarChart series={[{ label: '成功', tone: 'green', values: requestOk }, { label: '错误', tone: 'red', values: requestErr }]} legend={[{ label: '成功', tone: 'green' }, { label: '错误', tone: 'red' }]} /></Section>
    </div>
    <div className="admin-grid three">
      <Section title="客户端分布" subtitle="近 7 天">
        <Table columns={[{ key: 'client', label: '客户端', render: (row) => clientLabels[row.client] || row.client }, { key: 'users', label: '用户', align: 'num', render: (row) => fmtNum(row.users) }, { key: 'requests', label: '请求', align: 'num', render: (row) => fmtNum(row.requests) }]} rows={data.clients_7d} rowKey={(row) => row.client} />
      </Section>
      <Section title="作业" actions={<button className="secondary-btn" onClick={() => openSection('jobs')}>查看</button>}>
        <KeyValues items={[['待确认审批', fmtNum(data.runtime.pending_approvals)], ...Object.entries(jobs).map(([status, count]) => [status, fmtNum(count)])]} />
      </Section>
      <Section title="模型与健康">
        <KeyValues items={[['数据库', <Pill tone={data.health.database ? 'good' : 'bad'}>{data.health.storage} · {data.health.database ? '正常' : '异常'}</Pill>], ['Redis', <Pill tone={data.health.redis ? 'good' : 'bad'}>{data.health.redis ? '正常' : '异常'}</Pill>], ...Object.entries(quality.models || {}).map(([model, count]) => [model, `${fmtNum(count)} 次回复`]), ['记忆 / 任务 / 文件', `${fmtNum(data.conversations.memories)} / ${fmtNum(data.conversations.tasks)} / ${fmtNum(data.conversations.files)}`]]} />
      </Section>
    </div>
  </>
}

function ModelMetric({ values }) {
  return <span className="admin-model-metric"><strong>{fmtMs(values?.avg)}</strong><small>P50 {fmtMs(values?.p50)} · P95 {fmtMs(values?.p95)} · P99 {fmtMs(values?.p99)}</small></span>
}

function modelUserLabel(row) {
  return row.user?.display_name || row.user?.username || row.user?.email || row.user_id || '—'
}

function ModelAnalysisReport({ data, range, openUser, openSession }) {
  const models = data.models || []
  return <>
    <Section title="模型对比" subtitle="每次模型调用的性能与用量；包括后台调用">
      <Table rowKey={(row) => row.model} columns={[
        { key: 'model', label: '模型', render: (row) => <span className="admin-strong">{row.model}</span> },
        { key: 'calls', label: '调用数', align: 'num', render: (row) => fmtNum(row.calls) },
        { key: 'success_rate', label: '成功率 / 错误', render: (row) => <span className="admin-model-metric"><strong>{((row.success_rate || 0) * 100).toFixed(1)}%</strong><small>{Object.entries(row.error_types || {}).map(([type, count]) => `${type} ${fmtNum(count)}`).join(' · ') || '无错误'}</small></span> },
        { key: 'first_token_ms', label: '首字延迟 avg / 分位数', render: (row) => <ModelMetric values={row.first_token_ms} /> },
        { key: 'duration_ms', label: '总耗时 avg / 分位数', render: (row) => <ModelMetric values={row.duration_ms} /> },
        { key: 'tokens_per_sec', label: 'tokens/s avg / P50', align: 'num', render: (row) => <span className="admin-model-metric"><strong>{row.tokens_per_sec?.avg === null || row.tokens_per_sec?.avg === undefined ? '—' : Number(row.tokens_per_sec.avg).toFixed(1)}</strong><small>P50 {row.tokens_per_sec?.p50 === null || row.tokens_per_sec?.p50 === undefined ? '—' : Number(row.tokens_per_sec.p50).toFixed(1)}</small></span> },
        { key: 'prompt_tokens', label: '输入 tokens', align: 'num', render: (row) => fmtNum(row.prompt_tokens) },
        { key: 'completion_tokens', label: '输出 tokens', align: 'num', render: (row) => fmtNum(row.completion_tokens) },
        { key: 'total_tokens', label: '总 tokens', align: 'num', render: (row) => fmtNum(row.total_tokens) },
        { key: 'tokens_per_call', label: '平均 tokens/次', align: 'num', render: (row) => row.tokens_per_call === null || row.tokens_per_call === undefined ? '—' : fmtNum(Math.round(row.tokens_per_call)) },
      ]} rows={models} />
    </Section>
    <div className="admin-grid two">
      <Section title="P95 首字延迟" subtitle={range === '24h' ? '按小时 · UTC' : '按天 · UTC'}><ModelPerformanceChart series={data.series || []} metric="first_token_ms_p95" hourly={range === '24h'} /></Section>
      <Section title="错误率" subtitle={range === '24h' ? '按小时 · UTC' : '按天 · UTC'}><ModelPerformanceChart series={data.series || []} metric="error_rate" hourly={range === '24h'} /></Section>
    </div>
    <Section title="用户排行" subtitle="按总 tokens 排序">
      <Table rowKey={(row) => row.user_id} onRowClick={(row) => openUser(row.user_id)} columns={[
        { key: 'user', label: '用户', render: (row) => <span className="admin-user"><span><strong>{modelUserLabel(row)}</strong><small>{row.user_id}</small></span></span> },
        { key: 'total_tokens', label: 'tokens', align: 'num', render: (row) => fmtNum(row.total_tokens) },
        { key: 'calls', label: '调用数', align: 'num', render: (row) => fmtNum(row.calls) },
        { key: 'first_token_ms_avg', label: '平均首字延迟', align: 'num', render: (row) => fmtMs(row.first_token_ms_avg) },
      ]} rows={data.users || []} />
    </Section>
    <Section title="慢调用" subtitle="总耗时超过本范围 P95，显示最近 50 条；仅含调用指标">
      <Table rowKey={(row) => row.id} columns={[
        { key: 'created_at', label: '时间', render: (row) => fmtTime(row.created_at) },
        { key: 'user', label: '用户', render: (row) => modelUserLabel(row) },
        { key: 'session_id', label: '会话', render: (row) => row.session_id ? <button type="button" className="admin-text-btn" onClick={() => openSession(row.session_id)}>{row.session_id}</button> : '—' },
        { key: 'purpose', label: '用途', render: (row) => PURPOSE_LABELS[row.purpose] || row.purpose },
        { key: 'model', label: '模型' },
        { key: 'status', label: '状态', render: (row) => <Pill tone={row.status === 'ok' ? 'good' : 'bad'}>{row.status}</Pill> },
        { key: 'first_token_ms', label: '首字延迟', align: 'num', render: (row) => fmtMs(row.first_token_ms) },
        { key: 'duration_ms', label: '总耗时', align: 'num', render: (row) => fmtMs(row.duration_ms) },
        { key: 'tokens_per_sec', label: 'tokens/s', align: 'num', render: (row) => row.tokens_per_sec === null || row.tokens_per_sec === undefined ? '—' : Number(row.tokens_per_sec).toFixed(1) },
        { key: 'total_tokens', label: 'tokens', align: 'num', render: (row) => <span className="admin-model-metric"><strong>{fmtNum(row.total_tokens)}</strong><small>{fmtNum(row.prompt_tokens)} 输入 · {fmtNum(row.completion_tokens)} 输出</small></span> },
      ]} rows={data.slow_calls || []} empty="没有超过 P95 的慢调用" />
    </Section>
  </>
}

function Models({ request, openUser, openSession }) {
  const [range, setRange] = useState('7d')
  const [model, setModel] = useState('')
  const [userDraft, setUserDraft] = useState('')
  const [userId, setUserId] = useState('')
  const [availableModels, setAvailableModels] = useState([])
  const [state, setState] = useState({ data: null, error: '', loading: true })
  const [revision, setRevision] = useState(0)
  const endpoint = modelAnalysisPath(range, model, userId)
  useEffect(() => {
    let active = true
    setState({ data: null, error: '', loading: true })
    request(endpoint).then((data) => {
      if (!active) return
      setState({ data, error: '', loading: false })
      setAvailableModels((current) => [...new Set([...current, ...(data.available_models || []), ...(data.models || []).map((row) => row.model)])].sort())
    }).catch((error) => {
      if (active) setState({ data: null, error: error.message || '加载失败', loading: false })
    })
    return () => { active = false }
  }, [request, endpoint, revision])
  return <>
    <form className="admin-model-filters" onSubmit={(event) => { event.preventDefault(); setUserId(userDraft.trim()); setRevision((current) => current + 1) }}>
      <div className="admin-tabs inline" role="radiogroup" aria-label="模型分析时间范围">{[['24h', '24 小时'], ['7d', '7 天'], ['30d', '30 天']].map(([value, label]) => <button type="button" role="radio" aria-checked={range === value} className={range === value ? 'active' : ''} key={value} onClick={() => setRange(value)}>{label}</button>)}</div>
      <label className="admin-model-select"><span>模型</span><select value={model} onChange={(event) => setModel(event.target.value)}><option value="">全部模型</option>{availableModels.map((name) => <option key={name} value={name}>{name}</option>)}</select></label>
      <label className="admin-search"><Icon name="search" size={15} /><input value={userDraft} onChange={(event) => setUserDraft(event.target.value)} placeholder="按用户 ID 筛选" aria-label="模型分析用户 ID" /></label>
      <button type="submit" className="secondary-btn">筛选</button>
      {(model || userId) && <button type="button" className="secondary-btn" onClick={() => { setModel(''); setUserDraft(''); setUserId('') }}>清除筛选</button>}
    </form>
    {state.loading && <Loading />}
    {state.error && <><ErrorBox message={state.error} /><button type="button" className="secondary-btn" onClick={() => setRevision((current) => current + 1)}>重试</button></>}
    {state.data && <ModelAnalysisReport data={state.data} range={range} openUser={openUser} openSession={openSession} />}
  </>
}

function Instances({ request, openUser }) {
  const { data, error } = useAdmin(request, '/instances', 10000)
  if (error && !data) return <ErrorBox message={error} />
  if (!data) return <Loading />
  const live = data.workers.filter((worker) => worker.live)
  const stopped = data.workers.filter((worker) => !worker.live)
  const host = data.host || {}
  const runtime = data.agent_runtime || {}
  const sandboxes = data.sandboxes || []
  const activeSandboxes = sandboxes.filter((item) => item.active)
  const pastSandboxes = sandboxes.filter((item) => !item.active)
  const idleMinutes = runtime.idle_ttl_seconds ? Math.round(runtime.idle_ttl_seconds / 60) : null
  const owner = (row) => <span className="admin-strong">{row.user?.user_id ? userName(row.user) : '未关联用户'}</span>
  return <>
    <div className="admin-stats">
      <Stat label="Web 服务进程" value={fmtNum(live.length)} hint="常驻 · 所有用户共用" tone="accent" />
      <Stat label="运行中沙箱" value={`${fmtNum(activeSandboxes.length)} / ${fmtNum(runtime.max_instances)}`} hint={runtime.enabled ? `每用户一个 · 空闲 ${idleMinutes ?? '—'} 分钟销毁` : `未启用 · ${runtime.reason || ''}`} tone="accent" />
      <Stat label="主机负载" value={host.loadavg ? host.loadavg.join(' · ') : '—'} hint={`${fmtNum(host.cpu_count)} 核 · 运行 ${fmtDuration(host.uptime_seconds)}`} />
      <Stat label="内存 / 磁盘可用" value={host.memory_mb ? `${fmtNum(host.memory_mb.available)} MB` : '—'} hint={`磁盘剩余 ${host.disk_gb?.free ?? '—'} / ${host.disk_gb?.total ?? '—'} GB`} />
    </div>
    <Section title="Web 服务进程" subtitle={`uvicorn 常驻 worker，负责所有用户的网页和接口请求，不是沙箱 · ${data.service ? `systemd ${data.service.ActiveState}/${data.service.SubState}` : '非 systemd 环境'} · 版本 ${host.release || '—'} · 每 10 秒刷新`}>
      <Table rowKey={(row) => row.id} columns={[
        { key: 'pid', label: '进程', render: (row) => <span className="admin-strong"><i className={`admin-dot ${row.live ? 'good' : ''}`} />{row.host} · {row.pid}</span> },
        { key: 'release', label: '版本' },
        { key: 'rss_mb', label: '内存', align: 'num', render: (row) => `${row.rss_mb ?? '—'} MB` },
        { key: 'cpu_seconds', label: 'CPU 时间', align: 'num', render: (row) => `${row.cpu_seconds ?? '—'} s` },
        { key: 'threads', label: '线程', align: 'num' },
        { key: 'in_flight', label: '处理中', align: 'num' },
        { key: 'active_streams', label: '流式回复', align: 'num' },
        { key: 'requests_total', label: '请求 / 错误', align: 'num', render: (row) => `${fmtNum(row.requests_total)} / ${fmtNum(row.errors_total)}` },
        { key: 'started_at', label: '启动', render: (row) => fmtAgo(row.started_at) },
        { key: 'last_seen_at', label: '心跳', render: (row) => fmtAgo(row.last_seen_at) },
      ]} rows={live} empty="没有在线进程" />
      {data.masters?.length > 0 && <p className="admin-note">主进程 PID {data.masters.map((master) => `${master.pid}（${master.rss_mb} MB，启动于 ${fmtTime(master.started_at)}）`).join('，')}{data.service?.NRestarts !== undefined ? ` · systemd 重启次数 ${data.service.NRestarts}` : ''}</p>}
      {stopped.length > 0 && <p className="admin-note">最近 10 分钟内已退出：{stopped.map((worker) => `${worker.host}·${worker.pid}`).join('，')}</p>}
    </Section>
    <Section title="运行中的 Agent 沙箱" subtitle={`每个用户按需创建一个 · 空闲 ${idleMinutes ?? '—'} 分钟自动销毁 · 规格 ${runtime.max_cpu ?? '—'} CPU / ${runtime.max_memory_gib ?? '—'} GiB`}>
      <Table rowKey={(row) => row.id} onRowClick={(row) => row.user?.user_id && openUser(row.user.user_id)} columns={[
        { key: 'user', label: '用户', render: owner },
        { key: 'status', label: '状态', render: (row) => <Pill tone={statusTone(row.status)}>{sandboxStatus(row.status)}</Pill> },
        { key: 'provider', label: '实例', render: (row) => `${row.provider} · ${String(row.provider_runtime_id || '').slice(0, 14)}` },
        { key: 'capabilities', label: '能力', render: (row) => (row.capabilities || []).join(' / ') || '—' },
        { key: 'started_at', label: '启动', render: (row) => fmtTime(row.started_at) },
        { key: 'last_seen_at', label: '最近使用', render: (row) => fmtAgo(row.last_seen_at) },
        { key: 'expires_at', label: '预计销毁', render: (row) => fmtTime(row.expires_at) },
      ]} rows={activeSandboxes} empty="当前没有运行中的沙箱" />
    </Section>
    {pastSandboxes.length > 0 && <Section title="沙箱历史记录" subtitle="以下实例均已销毁，仅保留记录">
      <Table rowKey={(row) => row.id} onRowClick={(row) => row.user?.user_id && openUser(row.user.user_id)} columns={[
        { key: 'user', label: '用户', render: owner },
        { key: 'status', label: '状态', render: (row) => <Pill>{sandboxStatus(row.status)}</Pill> },
        { key: 'provider', label: '实例', render: (row) => `${row.provider} · ${String(row.provider_runtime_id || '').slice(0, 14)}` },
        { key: 'started_at', label: '启动', render: (row) => fmtTime(row.started_at) },
        { key: 'stopped_at', label: '销毁', render: (row) => fmtTime(row.stopped_at || row.last_seen_at) },
        { key: 'lifetime', label: '存活', align: 'num', render: (row) => row.started_at && (row.stopped_at || row.last_seen_at) ? fmtDuration((new Date(row.stopped_at || row.last_seen_at) - new Date(row.started_at)) / 1000) : '—' },
      ]} rows={pastSandboxes} />
    </Section>}
    <div className="admin-grid two">
      <Section title="Agent Runtime 配置"><KeyValues items={Object.entries(runtime).map(([key, value]) => [key, typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value ?? '—')])} /></Section>
    </div>
  </>
}

function Users({ request, openUser, currentUserId }) {
  const [query, setQuery] = useState('')
  const [submitted, setSubmitted] = useState('')
  const endpoint = `/users?limit=500${submitted ? `&q=${encodeURIComponent(submitted)}` : ''}`
  const { data, error, reload } = useAdmin(request, endpoint)
  const [busy, setBusy] = useState('')
  const [updateError, setUpdateError] = useState('')
  async function updateUser(user, patch) {
    if (busy || user.user_id === currentUserId) return
    if (patch.status === 'disabled' && !window.confirm(`禁用 ${userName(user)}？该用户的全部设备将退出登录。`)) return
    setBusy(user.user_id)
    setUpdateError('')
    try {
      await request(`/users/${encodeURIComponent(user.user_id)}`, { method: 'PATCH', body: JSON.stringify(patch) })
      await reload()
    } catch (failure) { setUpdateError(failure.message || '更新用户失败') }
    finally { setBusy('') }
  }
  return <Section title="用户" subtitle={data ? `共 ${fmtNum(data.total)} 人` : ''} actions={<form className="admin-search" onSubmit={(event) => { event.preventDefault(); setSubmitted(query.trim()) }}><Icon name="search" size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="用户名、邮箱、显示名" /></form>}>
    {(error || updateError) && <ErrorBox message={updateError || error} />}
    {!data ? <Loading /> : <Table rowKey={(row) => row.user_id} onRowClick={(row) => openUser(row.user_id)} columns={[
      { key: 'display_name', label: '用户', render: (row) => <span className="admin-user"><span className="admin-avatar">{(userName(row) || '?').slice(0, 1)}</span><span><strong>{userName(row)}</strong><small>{row.username || row.user_id}</small></span></span> },
      { key: 'email', label: '邮箱', render: (row) => row.email || '—' },
      { key: 'role', label: '角色', render: (row) => <Pill tone={row.role === 'admin' ? 'good' : ''}>{row.role === 'admin' ? '管理员' : '用户'}</Pill> },
      { key: 'status', label: '状态', render: (row) => <Pill tone={row.status === 'disabled' ? 'bad' : 'good'}>{row.status === 'disabled' ? '已禁用' : '正常'}</Pill> },
      { key: 'actions', label: '管理', render: (row) => <div className="admin-user-actions" onClick={(event) => event.stopPropagation()}><button type="button" className="secondary-btn" disabled={Boolean(busy) || row.user_id === currentUserId} title={row.user_id === currentUserId ? '不能修改自己的角色或状态' : undefined} onClick={() => updateUser(row, { role: row.role === 'admin' ? 'user' : 'admin' })}>{row.role === 'admin' ? '取消管理员' : '设为管理员'}</button><button type="button" className="secondary-btn" disabled={Boolean(busy) || row.user_id === currentUserId} title={row.user_id === currentUserId ? '不能修改自己的角色或状态' : undefined} onClick={() => updateUser(row, { status: row.status === 'disabled' ? 'active' : 'disabled' })}>{row.status === 'disabled' ? '启用' : '禁用'}</button></div> },
      { key: 'sessions', label: '对话', align: 'num', render: (row) => fmtNum(row.sessions) },
      { key: 'messages', label: '消息', align: 'num', render: (row) => fmtNum(row.messages) },
      { key: 'memories', label: '记忆', align: 'num', render: (row) => fmtNum(row.memories) },
      { key: 'tasks', label: '任务', align: 'num', render: (row) => fmtNum(row.tasks) },
      { key: 'clients', label: '客户端', render: (row) => row.clients?.length ? row.clients.map((client) => <Pill key={client}>{clientLabels[client] || client}</Pill>) : '—' },
      { key: 'login_count', label: '登录', align: 'num', render: (row) => fmtNum(row.login_count) },
      { key: 'last_active_at', label: '最近活跃', render: (row) => fmtAgo(row.last_active_at) },
    ]} rows={data.items} empty="没有匹配的用户" />}
  </Section>
}

function Sessions({ request, filterUser, setFilterUser, openSession }) {
  const [query, setQuery] = useState('')
  const [submitted, setSubmitted] = useState('')
  const [page, setPage] = useState(0)
  const pageSize = 50
  useEffect(() => setPage(0), [submitted, filterUser])
  const params = new URLSearchParams({ limit: String(pageSize), offset: String(page * pageSize) })
  if (submitted) params.set('q', submitted)
  if (filterUser) params.set('user_id', filterUser)
  const { data, error } = useAdmin(request, `/sessions?${params}`)
  const pages = data ? Math.max(1, Math.ceil(data.total / pageSize)) : 1
  return <Section title="对话" subtitle={data ? `共 ${fmtNum(data.total)} 个对话` : ''} actions={<>
    {filterUser && <button className="secondary-btn" onClick={() => setFilterUser('')}>用户 {filterUser} <Icon name="close" size={14} /></button>}
    <form className="admin-search" onSubmit={(event) => { event.preventDefault(); setSubmitted(query.trim()) }}><Icon name="search" size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题或消息内容" /></form>
  </>}>
    {error && <ErrorBox message={error} />}
    {!data ? <Loading /> : <>
      <Table rowKey={(row) => row.id} onRowClick={(row) => openSession(row.id)} columns={[
        { key: 'title', label: '对话', render: (row) => <span className="admin-session"><strong>{row.title}</strong><small>{row.last_user_message || '还没有提问'}</small></span> },
        { key: 'user', label: '用户', render: (row) => userName(row.user) },
        { key: 'message_count', label: '消息', align: 'num', render: (row) => fmtNum(row.message_count) },
        { key: 'created_at', label: '创建', render: (row) => fmtTime(row.created_at) },
        { key: 'updated_at', label: '最近更新', render: (row) => fmtAgo(row.updated_at) },
      ]} rows={data.items} empty="没有匹配的对话" />
      {pages > 1 && <div className="admin-pager"><button className="secondary-btn" disabled={page === 0} onClick={() => setPage((value) => value - 1)}>上一页</button><span>{page + 1} / {pages}</span><button className="secondary-btn" disabled={page + 1 >= pages} onClick={() => setPage((value) => value + 1)}>下一页</button></div>}
    </>}
  </Section>
}

function Jobs({ request, openUser }) {
  const [status, setStatus] = useState('')
  const { data, error } = useAdmin(request, `/jobs${status ? `?status=${status}` : ''}`, 15000)
  if (error && !data) return <ErrorBox message={error} />
  if (!data) return <Loading />
  return <>
    <div className="admin-tabs"><button className={!status ? 'active' : ''} onClick={() => setStatus('')}>全部</button>{Object.entries(data.counts).map(([key, count]) => <button key={key} className={status === key ? 'active' : ''} onClick={() => setStatus(key)}>{key} <em>{count}</em></button>)}</div>
    <Section title="作业">
      <Table rowKey={(row) => row.id} onRowClick={(row) => openUser(row.user_id)} columns={[
        { key: 'type', label: '类型', render: (row) => <span className="admin-session"><strong>{row.type}</strong><small>{row.payload?.title || row.payload?.prompt?.slice?.(0, 80) || row.id}</small></span> },
        { key: 'status', label: '状态', render: (row) => <Pill tone={statusTone(row.status)}>{row.status}</Pill> },
        { key: 'user', label: '用户', render: (row) => userName(row.user) },
        { key: 'attempts', label: '尝试', align: 'num' },
        { key: 'error', label: '错误', render: (row) => row.error ? <span className="admin-error-text">{row.error}</span> : '—' },
        { key: 'updated_at', label: '更新', render: (row) => fmtAgo(row.updated_at) },
      ]} rows={data.jobs} empty="没有作业" />
    </Section>
    <Section title="审批记录">
      <Table rowKey={(row) => row.id} columns={[
        { key: 'action', label: '动作', render: (row) => <span className="admin-strong">{row.action}</span> },
        { key: 'status', label: '状态', render: (row) => <Pill tone={statusTone(row.status)}>{row.status}</Pill> },
        { key: 'user', label: '用户', render: (row) => userName(row.user) },
        { key: 'decision_note', label: '备注', render: (row) => row.decision_note || '—' },
        { key: 'created_at', label: '创建', render: (row) => fmtTime(row.created_at) },
        { key: 'decided_at', label: '处理', render: (row) => fmtTime(row.decided_at) },
      ]} rows={data.approvals} empty="没有审批" />
    </Section>
  </>
}

function Requests({ request, openUser }) {
  const [kind, setKind] = useState('errors')
  const [hours, setHours] = useState(24)
  const log = useAdmin(request, `/requests?kind=${kind}&limit=300`, 15000)
  const routes = useAdmin(request, `/routes?hours=${hours}`)
  return <>
    <Section title="接口统计" subtitle="按路由汇总，不记录请求内容" actions={<div className="admin-tabs inline">{[[24, '24 小时'], [168, '7 天'], [720, '30 天']].map(([value, label]) => <button key={value} className={hours === value ? 'active' : ''} onClick={() => setHours(value)}>{label}</button>)}</div>}>
      {!routes.data ? <Loading /> : <Table rowKey={(row) => `${row.method} ${row.route}`} columns={[
        { key: 'route', label: '路由', render: (row) => <code className="admin-code">{row.method} {row.route}</code> },
        { key: 'count', label: '请求', align: 'num', render: (row) => fmtNum(row.count) },
        { key: 'errors', label: '错误', align: 'num', render: (row) => row.errors ? <span className="admin-error-text">{fmtNum(row.errors)}（{(row.error_rate * 100).toFixed(1)}%）</span> : '0' },
        { key: 'avg_ms', label: '平均', align: 'num', render: (row) => fmtMs(row.avg_ms) },
        { key: 'max_ms', label: '最慢', align: 'num', render: (row) => fmtMs(row.max_ms) },
      ]} rows={routes.data.items} />}
    </Section>
    <Section title="请求明细" subtitle="仅保存写操作、错误和慢请求（≥1 秒），保留 30 天" actions={<div className="admin-tabs inline">{[['errors', '错误'], ['slow', '慢请求'], ['all', '全部']].map(([value, label]) => <button key={value} className={kind === value ? 'active' : ''} onClick={() => setKind(value)}>{label}</button>)}</div>}>
      {!log.data ? <Loading /> : <Table onRowClick={(row) => row.user_id && openUser(row.user_id)} columns={[
        { key: 'created_at', label: '时间', render: (row) => fmtTime(row.created_at) },
        { key: 'route', label: '请求', render: (row) => <code className="admin-code">{row.method} {row.route}</code> },
        { key: 'status', label: '状态', render: (row) => <Pill tone={row.status >= 500 ? 'bad' : row.status >= 400 ? 'warn' : 'good'}>{row.status}</Pill> },
        { key: 'duration_ms', label: '耗时', align: 'num', render: (row) => fmtMs(row.duration_ms) },
        { key: 'user', label: '用户', render: (row) => userName(row.user) },
        { key: 'client', label: '客户端', render: (row) => `${clientLabels[row.client] || row.client || '—'} · ${row.ip || ''}` },
        { key: 'error', label: '错误', render: (row) => row.error ? <span className="admin-error-text">{row.error}</span> : '—' },
      ]} rows={log.data.items} empty="没有记录" />}
    </Section>
  </>
}

function ActivityLog({ request, openUser }) {
  const [userDraft, setUserDraft] = useState('')
  const [kindDraft, setKindDraft] = useState('')
  const [query, setQuery] = useState({ userId: '', kind: '', cursors: [''], page: 0 })
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState({ data: null, loading: true, error: '' })
  const endpoint = activityPath(query.userId, query.kind, query.cursors[query.page])
  useEffect(() => {
    let active = true
    setState({ data: null, loading: true, error: '' })
    request(endpoint)
      .then((data) => { if (active) setState({ data: activityPage(data), loading: false, error: '' }) })
      .catch((error) => { if (active) setState({ data: null, loading: false, error: error.message || '运行日志加载失败' }) })
    return () => { active = false }
  }, [request, endpoint, revision])
  const nextCursor = state.data?.next_cursor
  return <Section title="运行日志" subtitle="所有用户的工具调用和后台任务记录" actions={<form className="admin-activity-filters" onSubmit={(event) => { event.preventDefault(); setQuery({ userId: userDraft.trim(), kind: kindDraft.trim(), cursors: [''], page: 0 }) }}>
    <label className="admin-search"><Icon name="invite" size={15} /><input value={userDraft} onChange={(event) => setUserDraft(event.target.value)} aria-label="运行日志用户 ID" placeholder="按用户 ID 筛选" /></label>
    <label className="admin-search"><Icon name="activity" size={15} /><input value={kindDraft} onChange={(event) => setKindDraft(event.target.value)} aria-label="运行日志类型" placeholder="按类型筛选" /></label>
    <button type="submit" className="secondary-btn">筛选</button>
    {(query.userId || query.kind) && <button type="button" className="secondary-btn" onClick={() => { setUserDraft(''); setKindDraft(''); setQuery({ userId: '', kind: '', cursors: [''], page: 0 }) }}>清除筛选</button>}
    <button type="button" className="secondary-btn" disabled={state.loading} onClick={() => setRevision((value) => value + 1)}>刷新</button>
  </form>}>
    {state.error && <><ErrorBox message={state.error} /><button type="button" className="secondary-btn" onClick={() => setRevision((value) => value + 1)}>重试</button></>}
    {state.loading ? <Loading /> : state.data && <>
      <Table columns={[
        { key: 'created_at', label: '时间', render: (row) => fmtTime(row.created_at) },
        { key: 'user_id', label: '用户', render: (row) => row.user_id ? <button type="button" className="small-link" onClick={() => openUser(row.user_id)}>{row.user?.display_name || row.user?.username || row.user_id}</button> : '—' },
        { key: 'kind', label: '类型', render: (row) => <Pill>{row.kind || '—'}</Pill> },
        { key: 'title', label: '标题', render: (row) => <span className="admin-strong">{row.title || '—'}</span> },
        { key: 'detail', label: '详情', render: (row) => typeof row.detail === 'string' && row.detail ? row.detail : '—' },
        { key: 'job_id', label: '作业', render: (row) => row.job_id ? <code className="admin-code">{row.job_id}</code> : '—' },
        { key: 'approval_id', label: '审批', render: (row) => row.approval_id ? <code className="admin-code">{row.approval_id}</code> : '—' },
      ]} rows={state.data.items} empty="没有匹配的运行记录" />
      <div className="admin-pager"><button type="button" className="secondary-btn" disabled={query.page === 0} onClick={() => setQuery((current) => ({ ...current, page: current.page - 1 }))}>上一页</button><span>第 {query.page + 1} 页</span><button type="button" className="secondary-btn" disabled={!nextCursor || nextCursor === query.cursors[query.page]} onClick={() => setQuery((current) => ({ ...current, cursors: [...current.cursors.slice(0, current.page + 1), nextCursor], page: current.page + 1 }))}>下一页</button></div>
    </>}
  </Section>
}

function Audit({ request }) {
  const { data, error } = useAdmin(request, '/audit?limit=300')
  return <Section title="管理员审计" subtitle="每次打开后台、查看用户或对话都会记录">
    {error && <ErrorBox message={error} />}
    {!data ? <Loading /> : <Table columns={[
      { key: 'created_at', label: '时间', render: (row) => fmtTime(row.created_at) },
      { key: 'user', label: '管理员', render: (row) => userName(row.user) },
      { key: 'action', label: '操作', render: (row) => <code className="admin-code">{row.action}</code> },
      { key: 'target', label: '参数', render: (row) => row.target && row.target !== '{}' ? <code className="admin-code">{row.target}</code> : '—' },
      { key: 'ip', label: 'IP' },
    ]} rows={data.items} />}
  </Section>
}

function Transcript({ request, sessionId, onClose, openUser }) {
  const { data, error } = useAdmin(request, `/sessions/${encodeURIComponent(sessionId)}/messages`)
  return <Drawer onClose={onClose} title={data?.session?.title || '对话'} subtitle={data ? `${userName(data.user)} · ${data.messages.length} 条消息 · 创建于 ${fmtTime(data.session.created_at)}` : ''} actions={data && <button className="secondary-btn" onClick={() => openUser(data.session.user_id)}>查看用户</button>}>
    {error && <ErrorBox message={error} />}
    {!data ? <Loading /> : <div className="admin-transcript">
      {data.files?.length > 0 && <div className="admin-files">{data.files.map((file) => <Pill key={file.id}>{file.filename} · {Math.round(file.size_bytes / 1024)} KB</Pill>)}</div>}
      {data.messages.map((message) => {
        const meta = message.metadata || {}
        const facts = [fmtTime(message.created_at), meta.client && (clientLabels[meta.client] || meta.client), meta.model || meta.provider, meta.first_token_ms !== undefined && `首字 ${fmtMs(meta.first_token_ms)}`, meta.duration_ms !== undefined && `总耗时 ${fmtMs(meta.duration_ms)}`, meta.incomplete && '未完成', meta.error && `错误 ${meta.error}`].filter(Boolean)
        return <div key={message.id} className={`message ${message.role === 'user' ? 'user' : 'assistant'}`}><div className="admin-message"><div className="bubble"><p>{message.content}</p></div><small className={meta.incomplete || meta.error ? 'bad' : ''}>{facts.join(' · ')}</small></div></div>
      })}
      {!data.messages.length && <div className="admin-empty">这个对话还没有消息</div>}
    </div>}
  </Drawer>
}

function UserDetail({ request, userId, onClose, openSession, showSessions }) {
  const { data, error } = useAdmin(request, `/users/${encodeURIComponent(userId)}`)
  const profile = data?.profile || {}
  return <Drawer onClose={onClose} title={profile.display_name || profile.username || userId} subtitle={[profile.username, profile.email].filter(Boolean).join(' · ')} actions={<button className="secondary-btn" onClick={() => showSessions(userId)}>全部对话</button>}>
    {error && <ErrorBox message={error} />}
    {!data ? <Loading /> : <>
      <div className="admin-stats compact">
        <Stat label="对话" value={fmtNum(data.sessions.length)} />
        <Stat label="7 天回复" value={fmtNum(data.replies_7d.replies)} hint={`失败 ${fmtNum(data.replies_7d.failed)}`} />
        <Stat label="首字平均" value={fmtMs(data.replies_7d.first_token_avg_ms)} />
        <Stat label="登录次数" value={fmtNum(profile.login_count)} hint={profile.last_login_at ? `最近 ${fmtAgo(profile.last_login_at)}` : ''} />
      </div>
      <KeyValues items={[['用户 ID', profile.user_id], ['用户名', profile.username], ['邮箱', profile.email], ['显示名', profile.display_name], ['角色', profile.role === 'admin' ? '管理员' : '用户'], ['状态', profile.status === 'disabled' ? '已禁用' : '正常'], ['注册时间', fmtTime(profile.created_at)], ['最近在线', fmtAgo(profile.last_seen_at)], ['沙箱', data.sandbox ? <Pill tone={statusTone(data.sandbox.status)}>{data.sandbox.status} · {fmtAgo(data.sandbox.last_seen_at)}</Pill> : '无'], ['作业', Object.entries(data.jobs).map(([key, count]) => `${key} ${count}`).join(' · ') || '无']]} />
      <h4 className="admin-subhead">设备</h4>
      <Table rowKey={(row) => row.id} columns={[
        { key: 'client', label: '客户端', render: (row) => `${clientLabels[row.client] || row.client}${row.client_version ? ` ${row.client_version}` : ''}` },
        { key: 'platform', label: '平台', render: (row) => row.platform || '—' },
        { key: 'last_ip', label: 'IP' },
        { key: 'request_count', label: '请求', align: 'num', render: (row) => fmtNum(row.request_count) },
        { key: 'last_seen_at', label: '最近', render: (row) => fmtAgo(row.last_seen_at) },
      ]} rows={data.devices} empty="还没有设备记录" />
      <h4 className="admin-subhead">最近对话</h4>
      <Table rowKey={(row) => row.id} onRowClick={(row) => openSession(row.id)} columns={[
        { key: 'title', label: '标题', render: (row) => <span className="admin-strong">{row.title}</span> },
        { key: 'message_count', label: '消息', align: 'num' },
        { key: 'updated_at', label: '更新', render: (row) => fmtAgo(row.updated_at) },
      ]} rows={data.sessions.slice(0, 20)} empty="没有对话" />
      <h4 className="admin-subhead">记忆</h4>
      {data.memories.length ? <ul className="admin-list">{data.memories.slice(0, 30).map((memory) => <li key={memory.id}><Pill>{memory.category}</Pill>{memory.content}</li>)}</ul> : <div className="admin-empty">没有记忆</div>}
      <h4 className="admin-subhead">任务与目标</h4>
      {data.tasks.length + data.goals.length ? <ul className="admin-list">{data.goals.map((goal) => <li key={goal.id}><Pill tone="good">目标 {goal.progress}%</Pill>{goal.title}</li>)}{data.tasks.slice(0, 30).map((task) => <li key={task.id}><Pill tone={statusTone(task.status)}>{task.status}</Pill>{task.title}</li>)}</ul> : <div className="admin-empty">没有任务</div>}
      {data.errors.length > 0 && <><h4 className="admin-subhead">最近错误</h4><Table columns={[
        { key: 'created_at', label: '时间', render: (row) => fmtTime(row.created_at) },
        { key: 'route', label: '请求', render: (row) => <code className="admin-code">{row.method} {row.route}</code> },
        { key: 'status', label: '状态', render: (row) => <Pill tone="bad">{row.status}</Pill> },
        { key: 'error', label: '错误', render: (row) => row.error || '—' },
      ]} rows={data.errors} /></>}
    </>}
  </Drawer>
}

function Drawer({ title, subtitle, actions, onClose, children }) {
  useEffect(() => {
    function onKeyDown(event) { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])
  return <div className="admin-drawer-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <aside className="admin-drawer" role="dialog" aria-modal="true" aria-label={title}>
      <header><div><h3>{title}</h3>{subtitle && <p>{subtitle}</p>}</div><div className="admin-actions">{actions}<button className="admin-icon-btn" onClick={onClose} aria-label="关闭"><Icon name="close" size={18} /></button></div></header>
      <div className="admin-drawer-body">{children}</div>
    </aside>
  </div>
}

function Loading() {
  return <div className="admin-empty"><Icon name="loader" size={18} /> 正在加载…</div>
}

function ErrorBox({ message }) {
  let text = message
  try { text = JSON.parse(message).detail || message } catch { /* plain text */ }
  return <div className="admin-errorbox">加载失败：{text}</div>
}

function initialSection() {
  const hash = window.location.hash.replace('#', '')
  return sections.some((section) => section.id === hash) ? hash : 'overview'
}

export default function AdminApp({ request, onExit }) {
  const [me, setMe] = useState(null)
  const [meError, setMeError] = useState('')
  const [section, setSection] = useState(initialSection)
  const [userId, setUserId] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [sessionUser, setSessionUser] = useState('')
  const theme = useMemo(() => {
    const forced = new URLSearchParams(window.location.search).get('t')
    return forced === 'dark' || forced === 'light' ? forced : (localStorage.getItem('luma-theme') || 'light')
  }, [])
  useEffect(() => { document.documentElement.dataset.theme = theme; document.title = 'Luma 后台管理' }, [theme])
  useEffect(() => { request('/me').then(setMe).catch((error) => setMeError(error.message || '无法确认权限')) }, [request])
  useEffect(() => { window.history.replaceState({}, '', `${window.location.pathname}${window.location.search}#${section}`) }, [section])
  const openSection = useCallback((id) => { setSection(id); setUserId(''); setSessionId('') }, [])
  const openUser = useCallback((id) => { setSessionId(''); setUserId(id) }, [])
  const openSession = useCallback((id) => setSessionId(id), [])
  const showSessions = useCallback((id) => { setSessionUser(id); setUserId(''); setSection('sessions') }, [])
  const shellClass = `app-shell muse-shell admin-shell ${theme === 'light' ? 'theme-light' : ''}`

  if (!me) return <div className={shellClass}><main className="admin-gate">{meError ? <ErrorBox message={meError} /> : <Loading />}</main></div>
  if (me.role !== 'admin') return <div className={shellClass}><main className="admin-gate"><div className="admin-gate-card"><Icon name="shield" size={28} /><h2>无权限访问后台</h2><p>当前账号不是管理员。如需开通，请把下面的账号 ID 发给管理员。</p><code className="admin-code">{me.user_id}{me.username ? ` · ${me.username}` : ''}</code><button className="primary-btn" onClick={onExit}>返回 Luma</button></div></main></div>

  const current = sections.find((item) => item.id === section)
  return <div className={shellClass}>
    <aside className="admin-nav">
      <div className="admin-brand"><LumaLogo label="Luma" /><em>后台</em></div>
      <nav>{sections.map((item) => <button key={item.id} className={section === item.id ? 'active' : ''} onClick={() => openSection(item.id)}><Icon name={item.icon} size={18} /><span>{item.label}</span></button>)}</nav>
      <div className="admin-nav-foot"><span className="admin-avatar">{(me.display_name || me.username || me.user_id || '?').slice(0, 1)}</span><span><strong>{me.display_name || me.username || me.user_id}</strong><small>管理员</small></span></div>
      <button className="admin-back" onClick={onExit}><Icon name="chat" size={16} /> 返回 Luma</button>
    </aside>
    <main className="admin-main">
      <header className="admin-header"><h2>{current?.label}</h2><span>{new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date())}</span></header>
      {section === 'overview' && <Overview request={request} openSection={openSection} />}
      {section === 'instances' && <Instances request={request} openUser={openUser} />}
      {section === 'users' && <Users request={request} openUser={openUser} currentUserId={me.user_id} />}
      {section === 'sessions' && <Sessions request={request} filterUser={sessionUser} setFilterUser={setSessionUser} openSession={openSession} />}
      {section === 'models' && <Models request={request} openUser={openUser} openSession={openSession} />}
      {section === 'jobs' && <Jobs request={request} openUser={openUser} />}
      {section === 'requests' && <Requests request={request} openUser={openUser} />}
      {section === 'activity' && <ActivityLog request={request} openUser={openUser} />}
      {section === 'audit' && <Audit request={request} />}
    </main>
    {userId && !sessionId && <UserDetail request={request} userId={userId} onClose={() => setUserId('')} openSession={openSession} showSessions={showSessions} />}
    {sessionId && <Transcript request={request} sessionId={sessionId} onClose={() => setSessionId('')} openUser={openUser} />}
  </div>
}
