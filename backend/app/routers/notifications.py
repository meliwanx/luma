"""Notification persistence, delivery, and server-sent events."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional, Tuple

import anyio
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ..db import get_connection
from ..models import Notification, NotificationCreate, NotificationUpdate
from ._common import make_notification, now, owner_id, write_fields
from ..services.notifications import _public_notification, create_notification, publish_notification

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/v1/notifications", response_model=list[Notification])
def list_notifications(request: Request, unread_only: bool = Query(default=False), limit: int = Query(default=100, ge=1, le=500)) -> list[Notification]:
    user_id = owner_id(request)
    with get_connection() as conn:
        query = "SELECT * FROM notifications WHERE user_id = ?"
        args: list[Any] = [user_id]
        if unread_only:
            query += " AND read_at IS NULL"
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(query, tuple(args)).fetchall()
    return [make_notification(dict(row)) for row in rows]


@router.post("/api/v1/notifications", response_model=Notification, status_code=status.HTTP_201_CREATED)
def create_notification_endpoint(request: Request, payload: NotificationCreate) -> Notification:
    data = create_notification(owner_id(request), payload.kind, payload.title, payload.body, payload.action_url)
    if payload.level != "info" or payload.metadata:
        with get_connection() as conn:
            conn.execute("UPDATE notifications SET level = ?, metadata_json = ? WHERE id = ? AND user_id = ?", (payload.level, json.dumps(payload.metadata, ensure_ascii=False), data["id"], data["user_id"]))
        data["level"], data["metadata"] = payload.level, payload.metadata
    return Notification.model_validate(data)


@router.patch("/api/v1/notifications/{notification_id}", response_model=Notification)
def update_notification(request: Request, notification_id: str, payload: NotificationUpdate) -> Notification:
    user_id = owner_id(request)
    updates, args = write_fields(payload, ("title", "body", "metadata"), json_fields=("metadata",))
    if "metadata = ?" in updates:
        updates[updates.index("metadata = ?")] = "metadata_json = ?"
    if payload.read is not None:
        updates.append("read_at = ?")
        args.append(now() if payload.read else None)
    with get_connection() as conn:
        if updates:
            cursor = conn.execute(f"UPDATE notifications SET {', '.join(updates)} WHERE id = ? AND user_id = ?", (*args, notification_id, user_id))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Notification not found")
        row = conn.execute("SELECT * FROM notifications WHERE id = ? AND user_id = ?", (notification_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    return make_notification(dict(row))


@router.post("/api/v1/notifications/{notification_id}/read", response_model=Notification)
def mark_notification_read(request: Request, notification_id: str) -> Notification:
    return update_notification(request, notification_id, NotificationUpdate(read=True))


@router.delete("/api/v1/notifications/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification(request: Request, notification_id: str) -> None:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM notifications WHERE id = ? AND user_id = ?", (notification_id, owner_id(request)))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Notification not found")


def _notification_cursor(user_id: str, event_id: str) -> Optional[Tuple[str, str]]:
    """Resolve a Last-Event-ID to the durable ordering tuple.

    Event IDs are notification IDs, while the database orders by ``created_at``.
    Looking up the ID first avoids comparing an opaque ID with a timestamp and
    also handles multiple notifications written in the same microsecond.
    """
    if not event_id:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT created_at, id FROM notifications WHERE user_id = ? AND id = ?",
            (user_id, event_id),
        ).fetchone()
    if row is None:
        return None
    return str(row.get("created_at", "")), str(row.get("id", event_id))


def _notifications_after(
    user_id: str,
    cursor: Optional[str] = None,
    limit: int = 100,
    cursor_created_at: Optional[str] = None,
    cursor_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Read notifications after an event ID or timestamp.

    ``cursor`` is normally the SSE event ID.  ``cursor_created_at`` is used by
    the stream after it has already emitted an event and keeps the query safe
    when two rows share the same timestamp.  All database work remains in the
    caller's worker thread.
    """
    resolved = _notification_cursor(user_id, cursor) if cursor and not cursor_created_at else None
    # Accept a timestamp cursor for compatibility with early clients that
    # used ``created_at`` as the event ID.
    timestamp_cursor = cursor if cursor and "T" in cursor and not resolved else None
    created = cursor_created_at or (resolved[0] if resolved else timestamp_cursor)
    event_id = resolved[1] if resolved else (cursor_id or ("" if timestamp_cursor else (cursor if created else "")))
    with get_connection() as conn:
        if created:
            if cursor_id is not None:
                # The stream has already seen ``cursor_id``.  Include the
                # whole timestamp bucket because UUID IDs do not encode write
                # order; Python-side ``seen`` suppression handles the cursor
                # row and makes same-microsecond notifications resumable.
                rows = conn.execute(
                    "SELECT * FROM notifications WHERE user_id = ? AND created_at >= ? "
                    "ORDER BY created_at ASC, id ASC LIMIT ?",
                    (user_id, created, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM notifications WHERE user_id = ? "
                    "AND (created_at > ? OR (created_at = ? AND id > ?)) "
                    "ORDER BY created_at ASC, id ASC LIMIT ?",
                    (user_id, created, created, event_id, limit),
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE user_id = ? "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (user_id, limit),
            ).fetchall()
    return [_public_notification(dict(row)) for row in rows]


def _open_pubsub(user_id: str) -> Any:
    if not os.getenv("REDIS_HOST", "").strip():
        return None
    try:
        from ..db import _redis_client
        pubsub = _redis_client().pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe("luma:notify:%s" % user_id)
        return pubsub
    except Exception:
        logger.info("notification Redis unavailable; using database polling")
        return None


def _poll_pubsub(pubsub: Any) -> Optional[dict[str, Any]]:
    if pubsub is None:
        return None
    try:
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if not message or message.get("type") != "message":
            return None
        value = message.get("data")
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


@router.get("/api/v1/notifications/stream")
async def notification_stream(request: Request) -> StreamingResponse:
    # ``owner_id`` is currently a request-state helper, but resolve it in the
    # worker pool so this route remains safe if identity lookup gains storage
    # access in a future deployment.
    user_id = await anyio.to_thread.run_sync(owner_id, request)
    initial_event_id = request.headers.get("last-event-id", "").strip()
    # Do not replay the whole inbox for a new stream.  Resolve a supplied
    # Last-Event-ID inside the worker thread and otherwise use the stream's
    # start timestamp as the initial cursor.
    initial_created, initial_cursor_id = await anyio.to_thread.run_sync(
        lambda: (_notification_cursor(user_id, initial_event_id) or (now(), ""))
    )

    async def events():
        cursor_created = initial_created
        cursor_id = initial_cursor_id
        seen: set[str] = set()
        pubsub = await anyio.to_thread.run_sync(_open_pubsub, user_id)
        last_poll = 0.0
        last_heartbeat = time.monotonic()
        try:
            yield "event: ready\ndata: %s\n\n" % json.dumps(
                {"after": initial_event_id or cursor_created}, ensure_ascii=False
            )
            while True:
                if await request.is_disconnected():
                    break
                now_mono = time.monotonic()
                if now_mono - last_poll >= (5.0 if pubsub is None else 15.0):
                    rows = await anyio.to_thread.run_sync(
                        _notifications_after,
                        user_id,
                        None,
                        100,
                        cursor_created,
                        cursor_id,
                    )
                    last_poll = now_mono
                    for item in rows:
                        item_id, created = str(item.get("id", "")), str(item.get("created_at", ""))
                        if item_id in seen or (
                            created == cursor_created and cursor_id and item_id == cursor_id
                        ):
                            continue
                        cursor_created, cursor_id = created, item_id
                        seen.add(item_id)
                        yield "id: %s\nevent: notification\ndata: %s\n\n" % (item_id, json.dumps(item, ensure_ascii=False))
                message = await anyio.to_thread.run_sync(_poll_pubsub, pubsub)
                if message:
                    item_id, created = str(message.get("id", "")), str(message.get("created_at", ""))
                    if item_id and item_id not in seen and created >= cursor_created:
                        if created > cursor_created or (created == cursor_created and item_id > cursor_id):
                            cursor_created, cursor_id = created, item_id
                        seen.add(item_id)
                        yield "id: %s\nevent: notification\ndata: %s\n\n" % (item_id, json.dumps(message, ensure_ascii=False))
                # Keep duplicate suppression bounded for long-lived browser
                # tabs.  Ordering cursors still provide the durable guarantee.
                if len(seen) > 1024:
                    seen = set(list(seen)[-512:])
                if time.monotonic() - last_heartbeat >= 15.0:
                    yield ": heartbeat\n\n"
                    last_heartbeat = time.monotonic()
                await asyncio.sleep(0.2)
        finally:
            if pubsub is not None:
                await anyio.to_thread.run_sync(pubsub.close)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
