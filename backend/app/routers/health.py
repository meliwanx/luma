"""Liveness and safe diagnostics endpoints."""

import os
from typing import Any

from fastapi import APIRouter, Request

from ..auth import current_user_id
from ..db import ping_database, ping_redis, storage_status
from ..config import APP_VERSION
from ..provider import status as provider_status
from ..agent_runtime import status as agent_runtime_status

router = APIRouter()


@router.get("/health")
def health() -> dict[str, Any]:
    database_ok = ping_database()
    redis_ok = ping_redis()
    return {
        "status": "ok" if database_ok else "degraded",
        "service": "personal-assistant-api",
        "version": APP_VERSION,
        "database": {"reachable": database_ok},
        "redis": {"configured": bool(os.getenv("REDIS_HOST", "").strip()), "reachable": redis_ok},
    }


@router.get("/api/v1/system/status")
def system_status(request: Request) -> dict[str, Any]:
    current_user_id(request)
    database_ok = ping_database()
    redis_ok = ping_redis()
    storage = storage_status()
    provider = provider_status()
    return {
        "storage": {"backend": storage.get("backend"), "reachable": database_ok},
        "redis": {"configured": bool(os.getenv("REDIS_HOST", "").strip()), "reachable": redis_ok},
        "provider": {
            "provider": provider.get("provider"),
            "configured": provider.get("configured"),
            "model": provider.get("model"),
        },
        "agent_runtime": agent_runtime_status(),
    }


@router.get("/api/v1/provider/status")
def provider_configuration(request: Request) -> dict[str, Any]:
    current_user_id(request)
    provider = provider_status()
    return {
        "provider": provider.get("provider"),
        "configured": provider.get("configured"),
        "model": provider.get("model"),
    }
