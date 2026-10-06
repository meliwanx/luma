import React, { useEffect, useRef, useState } from 'react'
import { PURPOSE_LABELS, axisTokens, chartScale, formatLatency, formatTokens, usageNumber } from './usage-data.js'

const CHART = { width: 560, height: 220, left: 50, right: 14, top: 16, bottom: 34 }

export function UsageDailyChart({ daily = [] }) {
  const { width, height, left, right, top, bottom } = CHART
  const plotWidth = width - left - right
  const plotHeight = height - top - bottom
  const { maximum, ticks } = chartScale(daily.map((row) => row.total_tokens), { integer: true })
  const slot = plotWidth / Math.max(1, daily.length)
  const gap = Math.min(5, slot * .3)
  const labelEvery = Math.max(1, Math.ceil(daily.length / 6))
  return <div className="usage-chart">
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="每日模型 token 用量柱状图">
      {ticks.map((tick) => {
        const y = top + plotHeight * (1 - tick / maximum)
        return <g key={tick}><line className="usage-grid" x1={left} x2={width - right} y1={y} y2={y} /><text x={left - 8} y={y + 4} textAnchor="end">{axisTokens(tick)}</text></g>
      })}
      {daily.map((row, index) => {
        const barHeight = usageNumber(row.total_tokens) / maximum * plotHeight
        const x = left + index * slot + gap / 2
        return <g key={row.date}><title>{`${row.date} · ${formatTokens(row.total_tokens)} tokens · ${formatTokens(row.calls)} 次请求`}</title><rect className="usage-bar" x={x} y={top + plotHeight - barHeight} width={Math.max(0, slot - gap)} height={barHeight} rx={Math.min(3, slot / 4)} />{(index % labelEvery === 0 || index === daily.length - 1) && <text x={x + (slot - gap) / 2} y={height - 10} textAnchor="middle">{String(row.date).slice(5)}</text>}</g>
      })}
    </svg>
    {!daily.some((row) => usageNumber(row.total_tokens)) && <p className="usage-empty">这段时间还没有模型用量</p>}
  </div>
}

export function UsageDistribution({ rows = [], dimension }) {
  const total = rows.reduce((sum, row) => sum + usageNumber(row.total_tokens), 0)
  if (!rows.length) return <p className="usage-empty">暂无数据</p>
  return <div className="usage-distribution">{rows.map((row) => {
    const label = dimension === 'purpose' ? PURPOSE_LABELS[row.purpose] || row.purpose : row.model
    const share = total ? usageNumber(row.total_tokens) / total * 100 : 0
    return <div key={row[dimension]} className="usage-distribution-row"><div><strong>{label || '未知'}</strong><span>{formatTokens(row.total_tokens)} <small>tokens · {share.toFixed(1)}%</small></span></div><span className="usage-track"><i style={{ width: `${share}%` }} /></span></div>
  })}</div>
}

export function UserUsageReport({ data, onOpenSession, openingSessionId = '' }) {
  const totals = data.totals || {}
  const performance = data.performance || {}
  return <>
    <div className="usage-summary">
      <div className="ms-usage-card"><small>总 tokens</small><strong>{formatTokens(totals.total_tokens)}</strong><span>{formatTokens(totals.prompt_tokens)} 输入 · {formatTokens(totals.completion_tokens)} 输出</span></div>
      <div className="ms-usage-card"><small>请求数</small><strong>{formatTokens(totals.calls)}</strong><span>本范围内的模型调用</span></div>
      <div className="ms-usage-card"><small>平均首字延迟</small><strong>{formatLatency(performance.first_token_ms_avg)}</strong><span>总耗时 P50 {formatLatency(performance.duration_ms_p50)} · P95 {formatLatency(performance.duration_ms_p95)}</span></div>
    </div>
    {usageNumber(totals.estimated_ratio) > 0 && <p className="ms-note usage-estimated">部分为估算 · {(usageNumber(totals.estimated_ratio) * 100).toFixed(1)}% 的请求未提供 token 用量</p>}
    <h4>每日用量 <span className="usage-timezone">UTC</span></h4><UsageDailyChart daily={data.daily || []} />
    <h4>按用途</h4><UsageDistribution rows={data.purposes || []} dimension="purpose" />
    <h4>按模型</h4><UsageDistribution rows={data.models || []} dimension="model" />
    <h4>Top 会话</h4>
    <div className="usage-sessions">{(data.top_sessions || []).length ? data.top_sessions.map((session, index) => <button type="button" key={session.session_id} disabled={Boolean(openingSessionId)} aria-busy={openingSessionId === session.session_id} onClick={() => onOpenSession(session.session_id)}><span className="usage-session-rank">{index + 1}</span><span className="usage-session-title"><strong>{session.title || '未命名会话'}</strong><small>{formatTokens(session.calls)} 次请求</small></span><span>{formatTokens(session.total_tokens)}<small> tokens</small></span></button>) : <p className="usage-empty">暂无会话用量</p>}</div>
  </>
}

export function UsagePanel({ request, onOpenSession }) {
  const [range, setRange] = useState('7d')
  const [state, setState] = useState({ data: null, error: '', loading: true })
  const [revision, setRevision] = useState(0)
  const [navigation, setNavigation] = useState({ sessionId: '', error: '' })
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  useEffect(() => {
    let active = true
    setState({ data: null, error: '', loading: true })
    request(`/usage?range=${range}`).then((data) => {
      if (active) setState({ data, error: '', loading: false })
    }).catch(() => {
      if (active) setState({ data: null, error: '用量加载失败，请稍后重试', loading: false })
    })
    return () => { active = false }
  }, [range, request, revision])
  async function openUsageSession(id) {
    if (navigation.sessionId) return
    setNavigation({ sessionId: id, error: '' })
    try {
      const session = await request(`/sessions/${encodeURIComponent(id)}`)
      if (!mounted.current) return
      if (session?.id !== id) {
        setNavigation({ sessionId: '', error: '该会话已删除或无法访问' })
        return
      }
      await onOpenSession(id)
    } catch (error) {
      if (mounted.current) setNavigation({ sessionId: '', error: [403, 404].includes(error?.status) ? '该会话已删除或无法访问' : '会话加载失败，请稍后重试' })
    } finally {
      if (mounted.current) setNavigation((current) => ({ ...current, sessionId: '' }))
    }
  }
  return <div className="usage-panel">
    <div className="usage-toolbar"><div className="ms-segment text" role="radiogroup" aria-label="用量时间范围">{[['7d', '7 天'], ['30d', '30 天'], ['90d', '90 天']].map(([value, label]) => <button type="button" role="radio" aria-checked={range === value} className={range === value ? 'active' : ''} key={value} onClick={() => setRange(value)}>{label}</button>)}</div></div>
    {state.loading && <p className="usage-empty" role="status">正在加载用量…</p>}
    {state.error && <div className="usage-error" role="alert">{state.error}<button type="button" className="ms-btn" onClick={() => setRevision((current) => current + 1)}>重试</button></div>}
    {navigation.error && <p className="usage-error" role="alert">{navigation.error}</p>}
    {navigation.sessionId && <p className="usage-empty" role="status">正在打开会话…</p>}
    {state.data && <UserUsageReport data={state.data} onOpenSession={openUsageSession} openingSessionId={navigation.sessionId} />}
  </div>
}

export function ModelPerformanceChart({ series = [], metric, hourly = false }) {
  const latency = metric === 'first_token_ms_p95'
  const title = latency ? 'P95 首字延迟' : '错误率'
  const format = latency ? formatLatency : (value) => `${Number(value).toFixed(1)}%`
  const values = series.map((row) => row[metric] === null || row[metric] === undefined ? null : usageNumber(row[metric]) * (latency ? 1 : 100))
  const { maximum, ticks } = chartScale(values.filter((value) => value !== null))
  const { width, height, left, right, top, bottom } = CHART
  const plotWidth = width - left - right
  const plotHeight = height - top - bottom
  const x = (index) => left + (series.length > 1 ? index / (series.length - 1) : .5) * plotWidth
  const y = (value) => top + plotHeight * (1 - value / maximum)
  let newSegment = true
  const path = values.map((value, index) => {
    if (value === null) { newSegment = true; return '' }
    const command = `${newSegment ? 'M' : 'L'} ${x(index)} ${y(value)}`
    newSegment = false
    return command
  }).join(' ')
  const labelEvery = Math.max(1, Math.ceil(series.length / 6))
  return <div className="admin-chart usage-chart">
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title}时间序列`}>
      {ticks.map((tick) => <g key={tick}><line className="usage-grid" x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} /><text x={left - 8} y={y(tick) + 4} textAnchor="end">{format(tick)}</text></g>)}
      <path className={`usage-line ${latency ? '' : 'errors'}`} d={path} />
      {series.map((row, index) => <g key={row.bucket}><title>{`${row.bucket} · ${title} ${values[index] === null ? '—' : format(values[index])} · ${formatTokens(row.calls)} 次调用 · ${formatTokens(row.total_tokens)} tokens`}</title>{values[index] !== null && <circle className={`usage-point ${latency ? '' : 'errors'}`} cx={x(index)} cy={y(values[index])} r="3" />}{(index % labelEvery === 0 || index === series.length - 1) && <text x={x(index)} y={height - 10} textAnchor="middle">{hourly ? `${String(row.bucket).slice(11, 13)}:00` : String(row.bucket).slice(5, 10)}</text>}</g>)}
    </svg>
    {!series.length ? <p className="usage-empty">暂无数据</p> : !values.some((value) => value !== null) && <p className="usage-empty">暂无首字延迟数据</p>}
  </div>
}
