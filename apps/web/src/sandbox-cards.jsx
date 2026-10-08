import React, { useEffect, useMemo, useState } from 'react'
import { browserLiveHostSuffixes, fetchFileBlob, isBrowserLiveUrl, isImageFile, subscribeBrowserLiveHostSuffixes } from './browser-tools.js'

const TERMINAL_JOB_STATUSES = new Set(['succeeded', 'failed', 'cancelled', 'done', 'completed'])
const SANDBOX_CARD_KINDS = new Set(['sandbox', 'file', 'sandbox_import', 'preview', 'sandbox_job', 'browser_live'])
const JOB_STATUS_LABELS = { queued: '排队中', running: '运行中', succeeded: '已完成', done: '已完成', completed: '已完成', failed: '失败', cancelled: '已取消' }

function payloadFor(item) {
  if (!item) return {}
  if (item.payload && typeof item.payload === 'object') return item.payload
  if (item.data && typeof item.data === 'object' && !Array.isArray(item.data)) return item.data
  return {}
}

function resultFor(item) {
  return item && item.result && typeof item.result === 'object' ? item.result : {}
}

function valueFor(item, key, fallback = '') {
  const payload = payloadFor(item)
  const result = resultFor(item)
  const input = item && item.input && typeof item.input === 'object' ? item.input : {}
  const argumentsPayload = item && item.arguments && typeof item.arguments === 'object' ? item.arguments : {}
  return item?.[key] ?? payload[key] ?? result[key] ?? input[key] ?? argumentsPayload[key] ?? fallback
}

function cardKindFor(item) {
  const payload = payloadFor(item)
  // A sandbox tool event can carry the returned data in payload, while the
  // event itself keeps kind="sandbox" for stream compatibility.
  if (payload.kind && SANDBOX_CARD_KINDS.has(payload.kind) && (!SANDBOX_CARD_KINDS.has(item?.kind) || item?.kind === 'sandbox')) return payload.kind
  return item?.kind || payload.kind || 'sandbox'
}

function textValue(value) {
  if (typeof value === 'string') return value
  if (value == null) return ''
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function formatBytes(value) {
  if (value === undefined || value === null || value === '') return '大小未知'
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes < 0) return '大小未知'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

function isTerminal(status) {
  return TERMINAL_JOB_STATUSES.has(String(status || '').toLowerCase())
}

function FileCard({ item, apiUrl, getAuthHeaders }) {
  const fileId = String(valueFor(item, 'file_id') || '')
  const filename = String(valueFor(item, 'filename') || '未命名文件')
  const size = valueFor(item, 'size_bytes')
  const image = isImageFile(valueFor(item, 'media_type', valueFor(item, 'mime_type')))
  const [downloading, setDownloading] = useState(false)
  const [error, setError] = useState('')
  const [thumbnail, setThumbnail] = useState('')

  useEffect(() => {
    setThumbnail('')
    if (!image || !fileId) return undefined
    const controller = new AbortController()
    let href = ''
    let disposed = false
    fetchFileBlob(apiUrl, fileId, getAuthHeaders, controller.signal).then((blob) => {
      if (disposed) return
      href = URL.createObjectURL(blob)
      setThumbnail(href)
    }).catch(() => {})
    return () => {
      disposed = true
      controller.abort()
      if (href) URL.revokeObjectURL(href)
    }
  }, [apiUrl, fileId, getAuthHeaders, image])

  async function download() {
    if (!fileId || downloading) return
    setDownloading(true)
    setError('')
    try {
      const blob = await fetchFileBlob(apiUrl, fileId, getAuthHeaders)
      const href = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = href
      anchor.download = filename
      anchor.rel = 'noopener'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(href), 1000)
    } catch (downloadError) {
      setError(downloadError instanceof Error ? downloadError.message : '下载失败')
    } finally {
      setDownloading(false)
    }
  }

  return <article className="sandbox-tool-card sandbox-file-card">
    <div className="sandbox-tool-head"><strong>文件</strong><span>{formatBytes(size)}</span></div>
    <div className="sandbox-file-name" title={filename}>{filename}</div>
    {thumbnail && <img className="sandbox-file-thumbnail" src={thumbnail} alt={filename} />}
    <button type="button" className="sandbox-file-download" disabled={!fileId || downloading} onClick={download}>{downloading ? '下载中…' : '下载'}</button>
    {error && <small className="sandbox-card-error">{error}</small>}
  </article>
}

function BrowserLiveCard({ item }) {
  const url = String(valueFor(item, 'url') || '')
  const expiresIn = Number(valueFor(item, 'expires_in', 0))
  const [expired, setExpired] = useState(false)
  const [suffixes, setSuffixes] = useState(() => browserLiveHostSuffixes())
  useEffect(() => subscribeBrowserLiveHostSuffixes(setSuffixes), [])
  useEffect(() => {
    setExpired(false)
    if (!Number.isFinite(expiresIn) || expiresIn <= 0) return undefined
    const timer = window.setTimeout(() => setExpired(true), expiresIn * 1000)
    return () => window.clearTimeout(timer)
  }, [url, expiresIn])
  const valid = isBrowserLiveUrl(url, suffixes) && !expired
  return <article className="sandbox-tool-card sandbox-browser-card">
    <div className="sandbox-tool-head"><strong>浏览器实时画面</strong></div>
    {valid ? <a className="sandbox-file-download" href={url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">观看实时画面</a> : <span>{expired ? '实时画面已过期，请重新打开' : '实时画面暂不可用'}</span>}
  </article>
}

function ImportCard({ item }) {
  const filename = String(valueFor(item, 'filename') || '文件')
  return <article className="sandbox-tool-card sandbox-import-card"><span>已把 {filename} 放进工作区</span></article>
}

function PreviewCard({ item }) {
  const url = String(valueFor(item, 'url') || '')
  const note = String(valueFor(item, 'note') || '')
  const isPublic = valueFor(item, 'public', false) === true
  const publicHint = '拿到链接的人在沙箱运行期间都能访问'
  let validUrl = false
  try {
    validUrl = new URL(url).protocol === 'https:'
  } catch {
    validUrl = false
  }
  return <article className="sandbox-tool-card sandbox-preview-card">
    <div className="sandbox-tool-head"><strong>预览</strong></div>
    <div className="sandbox-preview-url">{validUrl ? <a href={url} target="_blank" rel="noopener noreferrer">{url}</a> : <span>预览地址无效</span>}</div>
    {note && <small>{note}</small>}
    {isPublic && !note.includes(publicHint) && <small>{publicHint}</small>}
  </article>
}

function SandboxJobCard({ item, request }) {
  const initialJob = useMemo(() => ({ ...item, ...payloadFor(item), ...resultFor(item) }), [item])
  const [job, setJob] = useState(initialJob)
  const [pollError, setPollError] = useState('')
  const jobId = String(valueFor(item, 'job_id') || initialJob.id || '')
  const status = String(job.status || 'running').toLowerCase()
  const command = textValue(job.command || job.payload?.command || '')
  const exitCode = job.exit_code ?? job.result?.exit_code
  const stdoutTail = textValue(job.stdout_tail ?? job.result?.stdout_tail)
  const stderrTail = textValue(job.stderr_tail ?? job.result?.stderr_tail)

  useEffect(() => {
    setJob((current) => ({ ...current, ...initialJob }))
  }, [initialJob])

  useEffect(() => {
    if (!request || !jobId || status !== 'running') return undefined
    let disposed = false
    let timer = null
    const poll = async () => {
      try {
        const latest = await request(`/runtime/jobs/${encodeURIComponent(jobId)}`)
        if (disposed) return
        const latestResult = latest && latest.result && typeof latest.result === 'object' ? latest.result : {}
        const latestStatus = latestResult.status || latest?.status
        setJob((current) => ({ ...current, ...latest, ...latestResult, result: latestResult }))
        setPollError('')
        if (!isTerminal(latestStatus)) timer = window.setTimeout(poll, 5000)
      } catch (error) {
        if (disposed) return
        setPollError(error instanceof Error ? error.message : '任务状态暂时无法获取')
        timer = window.setTimeout(poll, 5000)
      }
    }
    timer = window.setTimeout(poll, 5000)
    return () => {
      disposed = true
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [jobId, request, status])

  return <article className={`sandbox-tool-card sandbox-job-card ${status}`}>
    <div className="sandbox-tool-head"><strong>后台任务</strong><em>{JOB_STATUS_LABELS[status] || status}</em></div>
    {command && <pre className="sandbox-job-command">{command}</pre>}
    {isTerminal(status) && exitCode !== undefined && <div className="sandbox-job-exit">退出码 {String(exitCode)}</div>}
    {isTerminal(status) && (stdoutTail || stderrTail) && <div className="sandbox-tool-output"><small>输出末尾</small><pre>{stdoutTail}{stdoutTail && stderrTail ? '\n' : ''}{stderrTail}</pre></div>}
    {pollError && status === 'running' && <small className="sandbox-card-error">{pollError}</small>}
  </article>
}

export function SandboxToolCard({ item, apiUrl, getAuthHeaders, request }) {
  const kind = cardKindFor(item)
  if (kind === 'browser_live') return <BrowserLiveCard item={item} />
  if (kind === 'file') return <FileCard item={item} apiUrl={apiUrl} getAuthHeaders={getAuthHeaders} />
  if (kind === 'sandbox_import') return <ImportCard item={item} />
  if (kind === 'preview') return <PreviewCard item={item} />
  if (kind === 'sandbox_job') return <SandboxJobCard item={item} request={request} />

  const payload = payloadFor(item)
  const code = textValue(valueFor(item, 'code'))
  const output = textValue(item?.output ?? payload.output ?? resultFor(item).output ?? item?.stdout ?? resultFor(item).stdout ?? item?.stderr ?? resultFor(item).stderr)
  const language = String(valueFor(item, 'language') || 'text')
  const exitCode = valueFor(item, 'exit_code', undefined)
  const lines = code.split('\n')
  return <SandboxCodeCard item={item} code={code} output={output} language={language} exitCode={exitCode} lines={lines} />
}

function SandboxCodeCard({ item, code, output, language, exitCode, lines }) {
  const [expanded, setExpanded] = useState(false)
  const shown = expanded ? code : lines.slice(0, 30).join('\n')
  return <article className={`sandbox-tool-card ${item.status || ''}`}>
    <div className="sandbox-tool-head"><strong>沙箱执行</strong><span>{language}</span>{exitCode !== undefined && <em className={Number(exitCode) === 0 ? 'ok' : 'error'}>退出码 {String(exitCode)}</em>}</div>
    {code && <div className="sandbox-tool-code"><pre>{shown}</pre>{lines.length > 30 && <button type="button" onClick={() => setExpanded((current) => !current)}>{expanded ? '收起代码' : `展开全部（${lines.length} 行）`}</button>}</div>}
    {output && <div className="sandbox-tool-output"><small>输出</small><pre>{output}</pre></div>}
  </article>
}
