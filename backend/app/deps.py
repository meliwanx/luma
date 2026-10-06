"""Request ownership and common database guards."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from .auth import current_user_id
from .db import get_connection
from .services.seed import ensure_default_data


def owner_id(request: Request) -> str:
    # Ownership is always derived from an authenticated account.
    user_id = current_user_id(request, required=True)
    request.state.luma_user_id = user_id
    return user_id


def ensure_user_data(user_id: str) -> None:
    ensure_default_data(user_id)


def require_session(conn: Any, session_id: str, user_id: str = "local") -> None:
    if conn.execute(
        "SELECT 1 FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
    ).fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found")
