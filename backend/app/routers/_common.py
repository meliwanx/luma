"""Shared imports and small helpers used by route modules.

This module intentionally contains no FastAPI application state.  Keeping the
database/model conversion helpers here lets routers be included by ``main``
without importing the application module back again.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from ..db import get_connection
from ..deps import ensure_user_data, owner_id, require_session
from ..mappers import (
    make_artifact,
    make_connector,
    make_goal,
    make_memory,
    make_message,
    make_notification,
    make_stored_file,
    make_task,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def parse_json(value: Optional[str]) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def parse_json_list(value: Optional[str]) -> list[Any]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []


def json_metadata(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_fields(payload: Any, fields: tuple[str, ...], json_fields: tuple[str, ...] = ()) -> tuple[list[str], list[Any]]:
    updates: list[str] = []
    values: list[Any] = []
    for field in fields:
        value = getattr(payload, field)
        if value is None:
            continue
        updates.append("%s = ?" % field)
        values.append(json_metadata(value) if field in json_fields or field == "metadata" else value)
    return updates, values


__all__ = [
    "HTTPException", "get_connection", "ensure_user_data", "owner_id", "require_session",
    "make_artifact", "make_connector", "make_goal", "make_memory", "make_message",
    "make_notification", "make_stored_file", "make_task", "now", "new_id", "parse_json",
    "parse_json_list", "json_metadata", "write_fields",
]
