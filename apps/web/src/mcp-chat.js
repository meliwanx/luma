// The original secret-bearing message is sent only in the request body. Keep
// it out of optimistic messages and retry state until the server vaults it.
export function containsSecretJson(value) {
  if (typeof value !== 'string' || !value.includes('{')) return false
  const text = value.replace(/\\u([0-9a-f]{4})/gi, (_, code) => String.fromCharCode(parseInt(code, 16)))
  return /"(?:headers|env|authorization|token|api[-_]?key|password|secret|access[-_]?token|access[-_]?key|client[-_]?secret)"\s*:/i.test(text)
}

export function optimisticChatContent(value) {
  return containsSecretJson(value) ? '[含敏感信息的消息：等待服务端加密保存]' : value
}

export function displaySecretReferences(value) {
  return String(value || '').replace(/\{\{secret:sec_[A-Za-z0-9_-]{16}\}\}/g, '••••••（已加密保存）')
}

export function toolStatusKey(item, index = 0) {
  return item.stream_key || `${item.event_type || 'tool'}:${item.call_id || item.approval_id || item.id || item.tool || item.reason || index}`
}
