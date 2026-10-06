"""Conversation/session management and message listing."""

import re
from html import unescape

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from ..db import get_connection
from ..models import Session, SessionCreate, SessionUpdate, Message
from ..widgets import strip_pseudo_markup, widgets_for_messages
from ._common import ensure_user_data, make_message, new_id, now, owner_id, require_session

router = APIRouter()


# Session previews are deliberately derived from the latest message in the
# same query as the session row.  The model is kept free of storage-only
# aliases, so the conversion helper below removes ``last_message_content``.
_SESSION_SELECT = """
    SELECT
        s.id,
        s.title,
        s.kind,
        s.created_at,
        s.updated_at,
        latest.content AS last_message_content,
        latest.created_at AS last_message_at,
        COUNT(messages.id) AS message_count
    FROM sessions AS s
    LEFT JOIN LATERAL (
        SELECT content, created_at
        FROM messages
        WHERE messages.session_id = s.id AND messages.user_id = s.user_id
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    ) AS latest ON TRUE
    LEFT JOIN messages ON messages.session_id = s.id AND messages.user_id = s.user_id
"""

_LUMA_UI_BLOCK = re.compile(r"```luma-ui\b.*?```", re.IGNORECASE | re.DOTALL)
_WIDGET_MARKER = re.compile(r"\[\[widget:[^\]]+\]\]")


def _plain_preview(content: object) -> Optional[str]:
    """Remove widget blocks and collapse whitespace for a short preview."""

    if content is None:
        return None
    text = strip_pseudo_markup(str(content))
    text = _LUMA_UI_BLOCK.sub("", text)
    text = _WIDGET_MARKER.sub("", text)
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80] or None


def _session_from_row(row: object) -> Session:
    data = dict(row)
    data["last_message_preview"] = _plain_preview(data.pop("last_message_content", None))
    # PostgreSQL returns COUNT() as an integer, but keeping this conversion
    # makes the mapper tolerant of lightweight test doubles as well.
    try:
        data["message_count"] = int(data.get("message_count") or 0)
    except (TypeError, ValueError):
        data["message_count"] = 0
    return Session.model_validate(data)


def _fetch_session(conn: object, user_id: str, session_id: str) -> Optional[Session]:
    row = conn.execute(
        _SESSION_SELECT
        + " WHERE s.id = ? AND s.user_id = ? GROUP BY s.id, s.title, s.kind, s.created_at, s.updated_at, latest.content, latest.created_at",
        (session_id, user_id),
    ).fetchone()
    return _session_from_row(row) if row is not None else None


@router.get("/api/v1/sessions", response_model=list[Session])
def list_sessions(
    request: Request,
    kind: Optional[str] = Query(default=None, pattern="^(main|side)$"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Session]:
    user_id = owner_id(request)
    ensure_user_data(user_id)
    with get_connection() as conn:
        condition = " WHERE s.user_id = ?"
        params: list[object] = [user_id]
        if kind is not None:
            condition += " AND s.kind = ?"
            params.append(kind)
        rows = conn.execute(
            _SESSION_SELECT
            + condition
            + " GROUP BY s.id, s.title, s.kind, s.created_at, s.updated_at, latest.content, latest.created_at"
            + " ORDER BY COALESCE(latest.created_at, s.updated_at) DESC, s.id DESC LIMIT ?",
            tuple(params + [limit]),
        ).fetchall()
    return [_session_from_row(row) for row in rows]


@router.get("/api/v1/sessions/main", response_model=Session)
def get_main_session(request: Request) -> Session:
    """Return this user's sole main chat, creating it safely when absent."""

    user_id = owner_id(request)
    timestamp = now()
    session_id = new_id("ses")
    with get_connection() as conn:
        # The partial unique index from 0009 is the concurrency authority.  A
        # second worker waits for the first insert and then takes the existing
        # row after ON CONFLICT DO NOTHING.
        conn.execute(
            "INSERT INTO sessions(id,user_id,title,kind,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (user_id) WHERE kind = 'main' DO NOTHING",
            (session_id, user_id, "主聊天", "main", timestamp, timestamp),
        )
        session = _fetch_session(conn, user_id, session_id)
        if session is None:
            row = conn.execute(
                "SELECT id FROM sessions WHERE user_id = ? AND kind = 'main'",
                (user_id,),
            ).fetchone()
            if row is not None:
                session = _fetch_session(conn, user_id, row["id"])
    if session is None:  # pragma: no cover - protected by the unique index
        raise HTTPException(status_code=500, detail="Unable to create main session")
    return session


@router.post("/api/v1/sessions", response_model=Session, status_code=status.HTTP_201_CREATED)
def create_session(request: Request, payload: SessionCreate) -> Session:
    user_id = owner_id(request)
    timestamp = now()
    kind = getattr(payload, "kind", "side")
    if kind != "side":
        raise HTTPException(status_code=422, detail="只能创建旁聊")
    data = {
        "id": new_id("ses"),
        "user_id": user_id,
        "title": payload.title,
        "kind": kind,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sessions(id,user_id,title,kind,created_at,updated_at) "
            "VALUES (:id,:user_id,:title,:kind,:created_at,:updated_at)",
            data,
        )
        session = _fetch_session(conn, user_id, data["id"])
    if session is None:  # pragma: no cover - insert either succeeds or raises
        raise HTTPException(status_code=500, detail="Unable to create session")
    return session


@router.get("/api/v1/sessions/{session_id}", response_model=Session)
def get_session(request: Request, session_id: str) -> Session:
    user_id = owner_id(request)
    with get_connection() as conn:
        session = _fetch_session(conn, user_id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.patch("/api/v1/sessions/{session_id}", response_model=Session)
def update_session(request: Request, session_id: str, payload: SessionUpdate) -> Session:
    user_id = owner_id(request)
    with get_connection() as conn:
        cursor = conn.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?", (payload.title, now(), session_id, user_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Session not found")
        session = _fetch_session(conn, user_id, session_id)
    if session is None:  # pragma: no cover - rowcount guarantees this
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.delete("/api/v1/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(request: Request, session_id: str) -> None:
    user_id = owner_id(request)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT kind FROM sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if row["kind"] == "main":
            raise HTTPException(status_code=409, detail="主聊天不能删除")
        cursor = conn.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Session not found")


@router.get("/api/v1/sessions/{session_id}/messages", response_model=list[Message])
def list_messages(
    request: Request,
    session_id: str,
    response: Response,
    limit: int = Query(default=100, ge=1, le=500),
    before: Optional[str] = Query(default=None),
) -> list[Message]:
    user_id = owner_id(request)
    with get_connection() as conn:
        require_session(conn, session_id, user_id)
        params = [user_id, session_id]
        cursor_created_at = None
        if before:
            cursor = conn.execute(
                "SELECT id, created_at FROM messages "
                "WHERE id = ? AND user_id = ? AND session_id = ?",
                (before, user_id, session_id),
            ).fetchone()
            if cursor is None:
                raise HTTPException(status_code=404, detail="Message not found")
            cursor_created_at = cursor["created_at"]

        # Fetch newest-first so the bounded query always returns the most
        # recent messages.  Reverse the bounded result below to preserve the
        # historical oldest-to-newest response order.  The id tie-breaker
        # keeps pagination stable when messages share a timestamp.
        condition = "user_id = ? AND session_id = ?"
        if cursor_created_at is not None:
            condition += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            params.extend([cursor_created_at, cursor_created_at, before])
        rows = conn.execute(
            "SELECT * FROM messages WHERE " + condition +
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params + [limit + 1]),
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    response.headers["X-Has-More"] = "true" if has_more else "false"
    response.headers["X-Oldest-Id"] = rows[0]["id"] if rows else ""
    widgets_by_message = widgets_for_messages(user_id, [row["id"] for row in rows if row["role"] == "assistant"])
    messages = [make_message(dict(row)) for row in rows]
    for message in messages:
        if message.role == "assistant":
            message.metadata["widgets"] = widgets_by_message.get(message.id, [])
    return messages
