export async function startIdeaSession(result, openSession, sendText) {
  if (!result?.session_id || typeof result.prompt !== 'string' || !result.prompt.trim()) throw new Error('点子启动结果无效')
  const opened = await openSession(result.session_id, { focus: true })
  if (opened === false) return false
  return sendText(result.prompt, {}, result.session_id)
}
