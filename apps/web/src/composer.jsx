import React, { useLayoutEffect, useRef, useState } from 'react'
import Icon from './icons.jsx'
import { isRecordingSupported } from './voice.js'
import { createVoiceInput, insertVoiceText } from './voice-input.js'

export function Composer({ input, setInput, resetKey = 0, voiceStartKey = 0, onVoiceStartConsumed, sendMessage, enterToSend, canSend, generating, onCancelMessage, onAttach, sessionId, voiceRaw, request, notify, errorDetail }) {
  const textareaRef = useRef(null)
  const voiceRef = useRef(null)
  const caretRef = useRef(null)
  const [voice, setVoice] = useState({ status: 'idle', elapsedSeconds: 0 })
  const [level, setLevel] = useState(0)
  const supported = isRecordingSupported()
  const recording = voice.status === 'recording'
  const busy = voice.status === 'processing' || voice.status === 'requesting'

  useLayoutEffect(() => {
    if (!resetKey) return
    caretRef.current = null
    textareaRef.current?.focus()
  }, [resetKey])

  useLayoutEffect(() => {
    caretRef.current = null
    setVoice({ status: 'idle', elapsedSeconds: 0 })
    setLevel(0)
    const controller = createVoiceInput({
      request,
      onState: setVoice,
      onLevel: setLevel,
      onText: (text) => {
        const textarea = textareaRef.current
        if (!textarea) return
        const inserted = insertVoiceText(textarea.value, text, textarea.selectionStart)
        caretRef.current = inserted.caret
        setInput(inserted.value)
      },
      onError: (error) => notify(errorDetail(error, '语音识别失败，请稍后重试')),
    })
    voiceRef.current = controller
    return () => { controller.dispose(); voiceRef.current = null }
  }, [sessionId])

  useLayoutEffect(() => {
    if (caretRef.current === null || !textareaRef.current) return
    const position = caretRef.current
    caretRef.current = null
    textareaRef.current.focus()
    textareaRef.current.setSelectionRange(position, position)
  }, [input])

  function beginRecording() {
    if (!supported) { notify('当前环境不支持录音'); return }
    if (recording || busy) return
    textareaRef.current?.focus()
    const context = { sessionId, raw: voiceRaw }
    setLevel(0)
    void voiceRef.current?.start(context)
  }

  useLayoutEffect(() => {
    if (!voiceStartKey) return
    beginRecording()
    onVoiceStartConsumed?.()
  }, [voiceStartKey])

  function toggleRecording() {
    if (recording) void voiceRef.current?.finish()
    else beginRecording()
  }

  function handleKeyDown(event) {
    if (event.isComposing || event.nativeEvent?.isComposing) return
    if ((event.metaKey || event.ctrlKey) && event.shiftKey && (event.code === 'Space' || event.key === ' ')) {
      event.preventDefault()
      if (!event.repeat) toggleRecording()
      return
    }
    if (event.key === 'Enter' && !event.shiftKey && enterToSend) { event.preventDefault(); sendMessage(event) }
  }

  const elapsed = `${String(Math.floor(voice.elapsedSeconds / 60)).padStart(2, '0')}:${String(voice.elapsedSeconds % 60).padStart(2, '0')}`
  const tip = !supported ? '当前环境不支持录音' : recording ? '结束录音' : '语音输入（⌘⇧Space / Ctrl⇧Space）'
  return <>
    {voice.status !== 'idle' && <div className={`composer-voice-status ${recording ? 'recording' : ''}`}>
      <div className="composer-voice-info" role="status" aria-live="polite">
        {recording ? <><span className="composer-record-dot" aria-hidden="true" /><span className="composer-voice-time">{elapsed}</span><span className="composer-voice-bars" aria-hidden="true">{[0.5, 0.8, 1, 0.7, 0.4].map((weight, index) => <i key={index} style={{ height: `${4 + Math.min(1, Math.max(0, level)) * weight * 20}px` }} />)}</span></> : <><Icon name="loader" size={16} className="composer-voice-spinner" /><span>{voice.status === 'processing' ? '识别中…' : '正在请求麦克风…'}</span></>}
      </div>
      {voice.status !== 'processing' && <div className="composer-voice-actions"><button type="button" onClick={() => voiceRef.current?.cancel()}>取消</button>{recording && <button type="button" className="composer-voice-done" onClick={toggleRecording}>完成</button>}</div>}
    </div>}
    <form className="composer" onSubmit={sendMessage}>
      <button className="composer-attach" type="button" aria-label="附加文件" onClick={onAttach}><Icon name="plus" /></button>
      <textarea key={resetKey} ref={textareaRef} rows="1" value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={handleKeyDown} placeholder="消息" />
      <span className="composer-voice-control" title={tip}><button className={`composer-voice ${recording ? 'recording' : ''}`} type="button" aria-label={recording ? '结束录音' : busy ? '语音识别处理中' : '语音输入'} aria-pressed={recording} disabled={!supported || busy} onClick={toggleRecording}>{recording ? <span className="composer-record-dot" aria-hidden="true" /> : busy ? <Icon name="loader" className="composer-voice-spinner" /> : <Icon name="microphone" />}</button></span>
      {canSend && <button className="send-btn" type={generating ? 'button' : 'submit'} aria-label={generating ? '停止生成' : '发送'} onClick={generating ? onCancelMessage : undefined}>{generating ? <span className="stop-icon" aria-hidden="true" /> : <Icon name="arrow-up" />}</button>}
    </form>
  </>
}
