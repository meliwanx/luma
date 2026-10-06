"""Message creation, streaming and the legacy web conversation adapter."""

import asyncio
import json
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..models import Message, MessageCreate
from ..model_calls import UsageTotals, call_context
from ..services import generation
from ..widgets import apply_event
from ..services import chat as chat_service
from ..services.memory import schedule_memory_extraction
from ._common import ensure_user_data, get_connection, owner_id

router = APIRouter()


def _legacy_assistant(session_id: str, message: Message, user_id: str) -> Message:
    assistant_id = chat_service.new_id("msg")
    totals = UsageTotals()
    with call_context(user_id=user_id, session_id=session_id, message_id=assistant_id,
                      purpose="final_answer", totals=totals):
        if message.role == "user":
            reply, provider = chat_service.assistant_reply(session_id, message.content, user_id, message.metadata)
        else:
            reply, provider = "已记录。", "local"
        assistant = chat_service.insert_message(
            session_id, MessageCreate(role="assistant", content=reply, metadata={
                "provider": provider, "usage": totals.snapshot(),
                **({"error": "provider_unavailable"} if provider == "error" else {}),
            }), user_id, message_id=assistant_id,
        )
        if provider == "error":
            chat_service.set_message_status(assistant.id, "error", user_id)
        else:
            schedule_memory_extraction(user_id, session_id, assistant.id)
        return assistant


@router.post("/api/v1/sessions/{session_id}/messages", response_model=Message)
def create_message(request: Request, session_id: str, payload: MessageCreate) -> Message:
    user_id = owner_id(request)
    message = chat_service.insert_message(session_id, payload, user_id)
    if payload.role == "user":
        _legacy_assistant(session_id, message, user_id)
    return message


class WidgetEventPayload(BaseModel):
    action: str = Field(min_length=1, max_length=40)
    value: Any = None
    remember: bool = False


@router.post("/api/v1/widgets/{widget_id}/events")
def widget_event(request: Request, widget_id: str, payload: WidgetEventPayload) -> dict[str, Any]:
    user_id = owner_id(request)
    widget, message = apply_event(user_id, widget_id, payload.action, payload.value)
    if payload.action == "confirm" and payload.remember and isinstance(widget, dict) and (widget.get("state") or {}).get("status") == "done":
        spec = widget.get("spec") if isinstance(widget, dict) else {}
        source = spec if isinstance(spec, dict) else widget if isinstance(widget, dict) else {}
        permission_key = source.get("permission_key")
        allow_always = bool(source.get("allow_always", False))
        if permission_key and allow_always:
            try:
                from ..services.permissions import set_permission_mode

                set_permission_mode(user_id, str(permission_key), "always")
            except (KeyError, PermissionError, ValueError):
                # The confirmed operation is already complete.  A stale
                # connector or a rolling migration must not replay it just to
                # save a preference that is no longer valid.
                pass
    return {"widget": widget, "message": message}


@router.post("/api/v1/sessions/{session_id}/messages/stream")
async def stream_message(request: Request, session_id: str, payload: MessageCreate) -> StreamingResponse:
    """Delegate the streaming orchestration to the chat service.

    Keeping the route as a thin adapter makes provider/tool call names resolve
    in ``services.chat`` and avoids a dependency cycle through ``main``.
    """

    return await chat_service.stream_message(request, session_id, payload)


@router.get("/api/v1/messages/{message_id}/stream")
async def resume_message_stream(
    request: Request,
    message_id: str,
    after: Optional[str] = Query(default=None),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Resume a generation stream after an SSE event id."""
    user_id = await run_in_threadpool(owner_id, request)

    def owned() -> Optional[dict[str, Any]]:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ? AND user_id = ? AND role = 'assistant'",
                (message_id, user_id),
            ).fetchone()
        return dict(row) if row is not None else None

    row = await run_in_threadpool(owned)
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found")
    cursor = after or last_event_id

    async def events() -> AsyncIterator[str]:
        emitted = False
        iterator = generation.manager.subscribe(message_id, cursor)
        pending: Optional[asyncio.Task[Any]] = asyncio.create_task(iterator.__anext__())

        def final_payload(current_row: dict[str, Any]) -> Optional[dict[str, Any]]:
            from ..mappers import make_message
            from ..widgets import widgets_for_messages

            if current_row.get("status") not in {"complete", "incomplete", "error"}:
                return None
            message = make_message(dict(current_row))
            widget_map = widgets_for_messages(user_id, [message.id])
            message.metadata["widgets"] = widget_map.get(message.id, [])
            return json.loads(message.model_dump_json())

        try:
            while pending is not None:
                done, _ = await asyncio.wait({pending}, timeout=15.0)
                if not done:
                    yield ": ping\n\n"
                    # If the owning task disappeared (for example after a
                    # worker crash), stop waiting on the queue and emit the
                    # durable terminal row below as soon as it is visible.
                    if message_id not in generation.manager.tasks:
                        current = await run_in_threadpool(owned)
                        final = final_payload(current) if current is not None else None
                        if final is not None:
                            yield chat_service.sse_event("done", final)
                            return
                    continue
                try:
                    event_id, event_name, data = pending.result()
                except StopAsyncIteration:
                    break
                emitted = True
                data = await chat_service.owner_browser_event(event_name, data, user_id)
                yield "id: {}\n{}".format(event_id, chat_service.sse_event(event_name, data))
                if event_name == "done":
                    return
                pending = asyncio.create_task(iterator.__anext__())
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                try:
                    await pending
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
            try:
                await iterator.aclose()
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
        if not emitted:
            current_row = await run_in_threadpool(owned)
            if current_row is None:
                return
            final_row = current_row if current_row.get("status") in {"complete", "incomplete", "error"} else None
            if final_row is not None:
                final = await run_in_threadpool(final_payload, final_row)
                if final is not None:
                    yield chat_service.sse_event("done", final)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/api/v1/messages/{message_id}/cancel")
async def cancel_message(request: Request, message_id: str) -> dict[str, Any]:
    user_id = await run_in_threadpool(owner_id, request)

    def owned() -> bool:
        with get_connection() as conn:
            return conn.execute(
                "SELECT 1 FROM messages WHERE id = ? AND user_id = ? AND role = 'assistant'",
                (message_id, user_id),
            ).fetchone() is not None

    if not await run_in_threadpool(owned):
        raise HTTPException(status_code=404, detail="Message not found")
    await generation.manager.cancel(message_id)
    return {"message_id": message_id, "status": "cancelling"}


def latest_session_id(user_id: str = "local") -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE user_id = ? AND kind = 'main' LIMIT 1", (user_id,)).fetchone()
    if row is None:
        ensure_user_data(user_id)
        with get_connection() as conn:
            row = conn.execute("SELECT id FROM sessions WHERE user_id = ? AND kind = 'main' LIMIT 1", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Main session not found")
    return str(row["id"])


@router.post("/api/conversations/{session_id}/messages")
def web_message(request: Request, session_id: str, payload: MessageCreate) -> dict[str, Any]:
    user_id = owner_id(request)
    actual_id = latest_session_id(user_id) if session_id in {"default", ""} else session_id
    user_message = chat_service.insert_message(actual_id, payload, user_id)
    assistant = _legacy_assistant(actual_id, user_message, user_id)
    return {"user_message": user_message.model_dump(mode="json"), "assistant": {"role": "assistant", "content": assistant.content, "metadata": assistant.metadata, "time": assistant.created_at.strftime("%H:%M")}, "activity": "Luma 完成了一次本地对话"}
