"""Portable, credential-free user data exports."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..db import get_connection
from ..deps import owner_id
from ..mappers import parse_json, parse_json_list
from .seed import now


def export_data(request: Request) -> dict[str, Any]:
    """Return a portable JSON snapshot for the authenticated user.

    Internal storage keys and connector configuration are deliberately omitted
    from the public export.  The shape is kept identical to the legacy
    ``/api/v1/export`` route so clients can migrate without a data transform.
    """

    user_id = owner_id(request)
    with get_connection() as conn:
        sessions = []
        for row in conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY created_at", (user_id,)
        ):
            session = dict(row)
            # ``kind`` is part of the portable session contract.  The
            # fallback keeps an export readable during a rolling migration
            # where an older worker may still expose legacy rows.
            session.setdefault("kind", "side")
            sessions.append(session)
        messages = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM messages WHERE user_id = ? ORDER BY created_at", (user_id,)
            )
        ]
        memories = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY created_at", (user_id,)
            )
        ]
        tasks = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at", (user_id,)
            )
        ]
        runtime_jobs = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM runtime_jobs WHERE user_id = ? ORDER BY created_at", (user_id,)
            )
        ]
        runtime_approvals = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM runtime_approvals WHERE user_id = ? ORDER BY created_at", (user_id,)
            )
        ]
        runtime_activity = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM runtime_activity WHERE user_id = ? ORDER BY created_at", (user_id,)
            )
        ]
        goals = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM goals WHERE user_id = ? ORDER BY created_at", (user_id,)
            )
        ]
        artifacts = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM artifacts WHERE user_id = ? ORDER BY created_at", (user_id,)
            )
        ]
        files = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM files WHERE user_id = ? AND deleted_at IS NULL ORDER BY created_at", (user_id,)
            )
        ]
        connectors = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM connectors WHERE user_id = ? ORDER BY created_at", (user_id,)
            )
        ]
        notifications = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at", (user_id,)
            )
        ]

    for collection in (
        messages,
        memories,
        tasks,
        goals,
        artifacts,
        notifications,
        runtime_jobs,
        runtime_approvals,
    ):
        for row in collection:
            if "metadata_json" in row:
                row["metadata"] = parse_json(row.pop("metadata_json"))
            if "payload_json" in row:
                row["payload"] = parse_json(row.pop("payload_json"))
            if "result_json" in row:
                row["result"] = parse_json(row.pop("result_json"))

    for row in connectors:
        row["capabilities"] = parse_json_list(row.pop("capabilities_json", "[]"))
        # Connector config can contain arbitrary provider details.  Do not
        # include it in an export, even though credentials are normally kept
        # in connector_secrets.
        row.pop("config_json", None)
        if "metadata_json" in row:
            row["metadata"] = parse_json(row.pop("metadata_json"))

    # A portable export contains file metadata only; storage keys disclose
    # tenant paths and are intentionally omitted.
    files = [
        {
            "id": row["id"],
            "session_id": row.get("session_id"),
            "filename": row["filename"],
            "media_type": row["media_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in files
    ]
    return {
        "exported_at": now(),
        "source": str(request.base_url),
        "sessions": sessions,
        "messages": messages,
        "memories": memories,
        "tasks": tasks,
        "runtime_jobs": runtime_jobs,
        "runtime_approvals": runtime_approvals,
        "runtime_activity": runtime_activity,
        "goals": goals,
        "artifacts": artifacts,
        "files": files,
        "connectors": connectors,
        "notifications": notifications,
    }


# Name used by a few clients while the router migration is in progress.
export_snapshot = export_data
