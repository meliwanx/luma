import React, { useState } from 'react'
import { ContentDialog } from './content-ui.jsx'
import './proactive.css'

export function ProactiveLabels({ message }) {
  const [reasonOpen, setReasonOpen] = useState(false)
  if (message.role !== 'assistant') return null
  const proactive = message.metadata?.proactive
  const feedPostId = message.metadata?.feed_post_id
  if (!proactive && !feedPostId) return null
  const reason = typeof proactive?.reason === 'string' ? proactive.reason : '根据我们的对话，主动给你发了这条消息。'
  return <>
    <div className="message-source-labels">
      {feedPostId && <span className="message-feed-source">来自动态</span>}
      {proactive && <button type="button" className="message-proactive-label" title={reason} onClick={() => setReasonOpen(true)}>主动</button>}
    </div>
    {reasonOpen && <ContentDialog title="为什么发给你" onClose={() => setReasonOpen(false)}><p className="proactive-reason">{reason}</p></ContentDialog>}
  </>
}
