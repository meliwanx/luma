"""Authenticated full-text search across the current user's conversations."""

from __future__ import annotations

import re
from html import unescape
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ..db import get_connection
from ..models import Session
from ..widgets import strip_pseudo_markup
from ._common import owner_id


router = APIRouter()

_LUMA_UI_FENCE = re.compile(
    r"(?is)```luma-ui\b.*?(?:```|\Z)"
)
_WIDGET_MARKER = re.compile(r"\[\[widget:[^\]]+\]\]")


def _escape_like(value: str) -> str:
    """Escape the three pattern characters before passing a LIKE argument."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _plain_text(value: Any) -> str:
    text = strip_pseudo_markup(str(value or ""))
    text = _LUMA_UI_FENCE.sub("", text)
    text = _WIDGET_MARKER.sub("", text)
    # Search snippets are plain text; never pass model/user HTML through to a
    # client that may render the snippet for highlighting.
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _snippet(value: Any, query: str) -> str:
    text = _plain_text(value)
    if not text:
        return ""
    index = text.casefold().find(query.casefold())
    if index < 0:
        # A match can be inside a luma-ui fence that is intentionally omitted
        # from the public snippet.  Keep the result useful without exposing
        # the structured component body.
        return text[:160] + ("…" if len(text) > 160 else "")
    start = max(0, index - 80)
    end = min(len(text), index + len(query) + 80)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _session_from_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["last_message_preview"] = _plain_text(data.get("last_message_preview"))[:80] or None
    session = Session.model_validate(data)
    return session.model_dump(mode="json")


@router.get("/api/v1/search")
def search(
    request: Request,
    q: str = Query(...),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    query = q.strip()
    if not 1 <= len(query) <= 100:
        raise HTTPException(status_code=422, detail="q must contain 1-100 characters")
    pattern = "%{}%".format(_escape_like(query))
    user_id = owner_id(request)
    with get_connection() as conn:
        session_rows = conn.execute(
            "SELECT s.id, s.title, s.kind, s.created_at, s.updated_at, "
            "(SELECT m.content FROM messages m WHERE m.session_id = s.id AND m.user_id = s.user_id "
            " ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_message_preview, "
            "(SELECT m.created_at FROM messages m WHERE m.session_id = s.id AND m.user_id = s.user_id "
            " ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_message_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id AND m.user_id = s.user_id) AS message_count "
            "FROM sessions s WHERE s.user_id = ? AND s.title ILIKE ? ESCAPE '\\' "
            "ORDER BY COALESCE((SELECT m.created_at FROM messages m WHERE m.session_id = s.id "
            " AND m.user_id = s.user_id ORDER BY m.created_at DESC, m.id DESC LIMIT 1), s.updated_at) DESC, s.id DESC "
            "LIMIT 10",
            (user_id, pattern),
        ).fetchall()
        message_rows = conn.execute(
            "SELECT m.id, m.session_id, s.title AS session_title, s.kind AS session_kind, "
            "m.role, m.content, m.created_at FROM messages m "
            "JOIN sessions s ON s.id = m.session_id AND s.user_id = m.user_id "
            "WHERE m.user_id = ? AND m.role IN ('user', 'assistant') "
            "AND m.content ILIKE ? ESCAPE '\\' "
            "ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
            (user_id, pattern, limit),
        ).fetchall()

    sessions = [_session_from_row(row) for row in session_rows]
    messages = [
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "session_title": row["session_title"],
            "session_kind": row["session_kind"],
            "role": row["role"],
            "snippet": _snippet(row.get("content"), query),
            "created_at": row["created_at"],
        }
        for row in message_rows
    ]
    return {"query": query, "sessions": sessions, "messages": messages}
