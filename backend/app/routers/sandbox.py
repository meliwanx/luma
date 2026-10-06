"""Persistent per-user sandbox status and workspace reset routes."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response, status

from ..agent_runtime import (
    AgentRuntimeUnavailable,
    reset_user_workspace,
    sandbox_status,
)
from ._common import owner_id


router = APIRouter()


def _public_status(value: Any) -> Dict[str, Any]:
    """Keep provider identifiers and backup storage keys out of the API."""

    data = value if isinstance(value, dict) else {}
    backup: Optional[Dict[str, Any]] = None
    raw_backup = data.get("backup")
    if isinstance(raw_backup, dict):
        backup = {
            "size_bytes": raw_backup.get("size_bytes"),
            "created_at": raw_backup.get("created_at"),
        }
    state = str(data.get("state") or "none")
    if state not in {"running", "paused", "none"}:
        state = "none"
    return {
        "state": state,
        "last_seen_at": data.get("last_seen_at"),
        "backup": backup,
    }


@router.get("/api/v1/sandbox")
def get_sandbox_status(request: Request) -> Dict[str, Any]:
    """Return the current user's sandbox state without provider details."""

    try:
        return _public_status(sandbox_status(owner_id(request)))
    except AgentRuntimeUnavailable as exc:
        # Keep provider exception text (which may contain deployment details)
        # out of the public response; telemetry records only the type name.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="沙箱服务暂不可用") from exc


@router.post("/api/v1/sandbox/reset", status_code=status.HTTP_204_NO_CONTENT)
def reset_sandbox(request: Request) -> Response:
    """Destroy the current user's sandbox and remove its workspace backup."""

    try:
        reset_user_workspace(owner_id(request))
    except AgentRuntimeUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="沙箱服务暂不可用") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router", "get_sandbox_status", "reset_sandbox"]
