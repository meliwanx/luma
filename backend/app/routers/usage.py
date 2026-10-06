"""Signed-in users can inspect only their own model-call statistics."""

from fastapi import APIRouter, Query, Request

from ..db import get_connection
from ..deps import owner_id
from ..usage import user_usage


router = APIRouter()


@router.get("/api/v1/usage")
def usage(request: Request, range: str = Query(default="7d", pattern="^(7d|30d|90d)$")) -> dict:
    user_id = owner_id(request)
    with get_connection() as conn:
        return user_usage(conn, user_id, range)
