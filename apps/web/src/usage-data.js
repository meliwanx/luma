export const PURPOSE_LABELS = {
  chat_round: '对话轮次', final_answer: '最终回复', summary: '会话摘要',
  voice_cleanup: '语音整理', decider: '决策', memory_extract: '记忆提取', other: '其他',
}

export function usageNumber(value) {
  const number = Number(value)
  return Number.isFinite(number) ? Math.max(0, number) : 0
}

export function formatTokens(value) {
  return usageNumber(value).toLocaleString('zh-CN')
}

export function formatLatency(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—'
  return Number(value) >= 1000 ? `${(Number(value) / 1000).toFixed(1)} s` : `${Math.round(Number(value))} ms`
}

export function chartScale(values, { integer = false, divisions = 4 } = {}) {
  const max = Math.max(0, ...values.map(usageNumber))
  const target = (max || 1) / divisions
  const magnitude = 10 ** Math.floor(Math.log10(target))
  const step = Math.max(integer ? 1 : 0, [1, 2, 5, 10].find((value) => value * magnitude >= target) * magnitude)
  const maximum = Math.max(step, Math.ceil(max / step) * step)
  const ticks = Array.from({ length: Math.round(maximum / step) + 1 }, (_, index) => Number((index * step).toPrecision(12)))
  return { maximum, ticks }
}

export function axisTokens(value) {
  if (value >= 1000000) return `${Number((value / 1000000).toPrecision(3))}M`
  if (value >= 1000) return `${Number((value / 1000).toPrecision(3))}k`
  return formatTokens(value)
}

export function modelAnalysisPath(range, model = '', userId = '') {
  const params = new URLSearchParams({ range })
  if (model.trim()) params.set('model', model.trim())
  if (userId.trim()) params.set('user_id', userId.trim())
  return `/models?${params}`
}
