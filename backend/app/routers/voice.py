"""Authenticated, in-memory voice input for all clients."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from ..auth import current_user_id
from ..model_calls import call_context
from ..db import _redis_client, get_connection
from ..services import voice

logger = logging.getLogger(__name__)
router = APIRouter()

_RATE_LIMIT = 10
_RATE_WINDOW_SECONDS = 60
_rate_limits: dict[str, tuple[float, int]] = {}
_rate_lock = threading.Lock()
_RATE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""
_MULTIPART_OVERHEAD = 64 * 1024


class VoiceTranscription(BaseModel):
    text: str
    transcript: str
    cleaned: bool
    duration_ms: int


def check_rate_limit(user_id: str) -> None:
    count = None
    if os.getenv("REDIS_HOST", "").strip():
        try:
            count = int(_redis_client().eval(
                _RATE_SCRIPT, 1, "luma:voice:rate:" + user_id, _RATE_WINDOW_SECONDS
            ))
        except Exception:
            # Redis may be absent in local development or temporarily offline.
            # Never log its connection settings or exception text.
            pass
    if count is None:
        timestamp = time.monotonic()
        with _rate_lock:
            expired = [key for key, (deadline, _) in _rate_limits.items() if deadline <= timestamp]
            for key in expired:
                del _rate_limits[key]
            deadline, previous = _rate_limits.get(user_id, (timestamp + _RATE_WINDOW_SECONDS, 0))
            count = previous + 1
            _rate_limits[user_id] = (deadline, count)
    if count > _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="语音输入太频繁，请稍后再试")


def _session_context(session_id: str, user_id: str) -> list[dict[str, str]]:
    with get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
        ).fetchone() is None:
            return []
        rows = conn.execute(
            "SELECT role, LEFT(content, ?) AS content FROM messages "
            "WHERE session_id = ? AND user_id = ? AND role IN (?, ?) "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (300, session_id, user_id, "user", "assistant", 6),
        ).fetchall()
    return [{"role": row["role"], "content": str(row["content"] or "")[:300]} for row in reversed(rows)]


class _MemoryVoiceParser(MultiPartParser):
    # Both attribute names are used by supported Starlette releases. The
    # per-file check runs before a write, so the spool can never reach disk.
    spool_max_size = voice.MAX_AUDIO_BYTES + 1
    max_file_size = voice.MAX_AUDIO_BYTES + 1

    def __init__(self, headers, stream):
        super().__init__(headers, stream, max_files=1, max_fields=2)
        self.max_part_size = _MULTIPART_OVERHEAD
        self._audio_bytes = 0
        self.form_complete = False

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._audio_bytes += end - start
            if self._audio_bytes > voice.MAX_AUDIO_BYTES:
                raise voice.VoiceInputError(voice.TOO_LONG_DETAIL, 413)
        super().on_part_data(data, start, end)

    def on_end(self) -> None:
        self.form_complete = True
        super().on_end()


async def _voice_form(request: Request) -> FormData:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise HTTPException(status_code=400, detail="请使用表单上传录音")
    request_limit = voice.MAX_AUDIO_BYTES + _MULTIPART_OVERHEAD
    try:
        advertised_size = int(request.headers.get("content-length", "0"))
    except ValueError:
        advertised_size = 0
    if advertised_size > request_limit:
        raise HTTPException(status_code=413, detail=voice.TOO_LONG_DETAIL)

    async def bounded_stream() -> AsyncIterator[bytes]:
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > request_limit:
                raise voice.VoiceInputError(voice.TOO_LONG_DETAIL, 413)
            yield chunk

    parser = _MemoryVoiceParser(request.headers, bounded_stream())
    parsed = False
    try:
        form = await parser.parse()
        if not parser.form_complete:
            raise MultiPartException("Incomplete multipart form")
        parsed = True
        return form
    except voice.VoiceInputError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    except (MultiPartException, ValueError, LookupError):
        raise HTTPException(status_code=400, detail="录音表单无效，请重新录制") from None
    finally:
        # parse() only cleans up MultiPartException; also close memory buffers
        # for size errors, malformed boundaries and disconnected clients.
        # On success the caller owns these files until form.close().
        if not parsed:
            for file in parser._files_to_close_on_error:
                file.close()


_VOICE_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["audio"],
                    "properties": {
                        "audio": {"type": "string", "format": "binary"},
                        "mode": {"type": "string", "enum": ["smart", "raw"], "default": "smart"},
                        "session_id": {"type": "string"},
                    },
                }
            }
        },
    }
}


@router.post("/api/v1/voice/transcribe", response_model=VoiceTranscription, openapi_extra=_VOICE_REQUEST_BODY)
async def transcribe_voice(request: Request) -> VoiceTranscription:
    # Voice input always requires an actual login, including in local mode.
    user_id = await run_in_threadpool(current_user_id, request, required=True)
    request.state.luma_user_id = user_id
    await run_in_threadpool(check_rate_limit, user_id)
    form = await _voice_form(request)
    started = time.perf_counter()
    size_bytes, duration_ms, cleaned = 0, 0, False
    try:
        audio = form.get("audio")
        if not isinstance(audio, UploadFile):
            raise HTTPException(status_code=400, detail="请上传录音文件")
        mode = form.get("mode", "smart")
        session_id = form.get("session_id", "")
        if mode not in ("smart", "raw"):
            raise HTTPException(status_code=400, detail="语音模式仅支持 smart 或 raw")
        if not isinstance(session_id, str):
            raise HTTPException(status_code=400, detail="会话参数无效")
        audio_bytes = await audio.read(voice.MAX_AUDIO_BYTES + 1)
        size_bytes = len(audio_bytes)
        duration_ms = voice.validate_audio(audio_bytes, audio.filename or "", audio.content_type or "")
        transcript = await voice.transcribe(audio_bytes, audio.filename or "", audio.content_type or "")
        if not transcript or not transcript.strip():
            raise voice.VoiceInputError("没听清，请靠近麦克风再说一次", 422)
        text = transcript
        if mode == "smart":
            context = await run_in_threadpool(_session_context, session_id, user_id) if session_id else []
            with call_context(user_id=user_id, session_id=session_id or None, purpose="voice_cleanup", timeout_seconds=20):
                text = await voice.smart_cleanup(transcript, context)
            cleaned = text != transcript
        return VoiceTranscription(text=text, transcript=transcript, cleaned=cleaned, duration_ms=duration_ms)
    except voice.VoiceInputError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    finally:
        await form.close()
        logger.info(
            "voice duration_ms=%s bytes=%s elapsed_ms=%s cleaned=%s",
            duration_ms, size_bytes, round((time.perf_counter() - started) * 1000), cleaned,
        )
