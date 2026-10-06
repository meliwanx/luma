"""Authenticated proactive preferences and operator evaluation endpoint."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..admin import require_admin
from ..db import get_connection
from ..deps import owner_id
from ..services.proactive_prefs import ProactivePrefs, get_prefs, put_prefs


router = APIRouter()


@router.get("/api/v1/proactive/prefs", response_model=ProactivePrefs)
def read_prefs(request: Request) -> dict[str, Any]:
    return get_prefs(owner_id(request))


@router.put("/api/v1/proactive/prefs", response_model=ProactivePrefs)
def update_prefs(request: Request, payload: ProactivePrefs) -> dict[str, Any]:
    return put_prefs(owner_id(request), payload)


def _known_user(user_id: str) -> bool:
    with get_connection() as conn:
        return conn.execute(
            "SELECT user_id FROM users WHERE user_id = ? UNION SELECT user_id FROM sessions WHERE user_id = ? LIMIT 1",
            (user_id, user_id),
        ).fetchone() is not None


@router.post("/api/admin/proactive/run")
async def run_proactive(
    user_id: str = Query(..., min_length=1, max_length=200),
    force: bool = Query(default=False),
    _: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    if not await asyncio.to_thread(_known_user, user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    from ..services.proactive import evaluate_user

    return await evaluate_user(user_id, force=force)
