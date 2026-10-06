"""Durable notification persistence and best-effort delivery."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..db import get_connection
from ..push import send_push

logger = logging.getLogger(__name__)


def now() -> str:
    """Return an aware UTC timestamp for durable notification rows."""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def _public_notification(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    raw = data.pop("metadata_json", data.pop("metadata", "{}"))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except (TypeError, ValueError):
            raw = {}
    data["metadata"] = raw if isinstance(raw, dict) else {}
    return data


def publish_notification(user_id: str, data: dict[str, Any]) -> None:
    """Best-effort Redis fan-out and mobile push for an already persisted row."""
    if os.getenv("REDIS_HOST", "").strip():
        try:
            from ..db import _redis_client

            _redis_client().publish("luma:notify:%s" % user_id, json.dumps(data, ensure_ascii=False))
        except Exception:
            logger.debug("notification Redis publish unavailable", exc_info=True)
    try:
        send_push(
            user_id,
            str(data.get("title", "")),
            str(data.get("body", ""))[:500],
            {"notification_id": data.get("id"), "kind": data.get("kind")},
        )
    except Exception:
        logger.debug("push delivery failed", exc_info=True)


def create_notification(
    user_id: str,
    kind: str,
    title: str,
    body: str,
    link: Optional[str] = None,
    *,
    conn: Any = None,
    publish: bool = True,
) -> dict[str, Any]:
    """Persist and deliver, or join a caller's transaction with delivery deferred."""
    if conn is not None and publish:
        raise ValueError("Publish an externally persisted notification after commit")
    timestamp = now()
    data: dict[str, Any] = {
        "id": new_id("notification"),
        "user_id": user_id,
        "title": str(title)[:300],
        "body": str(body)[:20_000],
        "kind": str(kind)[:80],
        "level": "info",
        "action_url": link,
        "read_at": None,
        "created_at": timestamp,
        "metadata_json": "{}",
    }
    def persist(connection: Any) -> None:
        connection.execute(
            "INSERT INTO notifications(id,user_id,title,body,kind,level,action_url,read_at,created_at,metadata_json) "
            "VALUES (:id,:user_id,:title,:body,:kind,:level,:action_url,:read_at,:created_at,:metadata_json)",
            data,
        )
    if conn is None:
        with get_connection() as connection:
            persist(connection)
    else:
        persist(conn)
    public = _public_notification(data)
    if publish:
        publish_notification(user_id, public)
    return public


__all__ = ["create_notification", "publish_notification", "_public_notification"]
