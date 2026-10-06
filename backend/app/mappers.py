"""Database row to API model mappings."""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import Request

from .models import Artifact, Connector, Goal, Memory, Message, Notification, StoredFile, Task


def parse_json(value: Optional[str]) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def parse_json_list(value: Optional[str]) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def make_message(row: dict[str, Any]) -> Message:
    row["metadata"] = parse_json(row.pop("metadata_json", "{}"))
    return Message.model_validate(row)


def make_memory(row: dict[str, Any]) -> Memory:
    row["metadata"] = parse_json(row.pop("metadata_json", "{}"))
    pinned = row.get("pinned", False)
    row["pinned"] = pinned.strip().lower() in {"1", "true", "yes", "on"} if isinstance(pinned, str) else bool(pinned)
    return Memory.model_validate(row)


def make_task(row: dict[str, Any]) -> Task:
    row["metadata"] = parse_json(row.pop("metadata_json", "{}"))
    return Task.model_validate(row)


def make_goal(row: dict[str, Any]) -> Goal:
    row["metadata"] = parse_json(row.pop("metadata_json", "{}"))
    return Goal.model_validate(row)


def make_artifact(row: dict[str, Any]) -> Artifact:
    row["metadata"] = parse_json(row.pop("metadata_json", "{}"))
    return Artifact.model_validate(row)


def make_connector(row: dict[str, Any]) -> Connector:
    row["capabilities"] = parse_json_list(row.pop("capabilities_json", "[]"))
    row["config"] = parse_json(row.pop("config_json", "{}"))
    row["metadata"] = parse_json(row.pop("metadata_json", "{}"))
    row["enabled"] = bool(row.get("enabled"))
    return Connector.model_validate(row)


def make_notification(row: dict[str, Any]) -> Notification:
    row["metadata"] = parse_json(row.pop("metadata_json", "{}"))
    return Notification.model_validate(row)


def make_stored_file(row: dict[str, Any], request: Request) -> StoredFile:
    row = dict(row)
    row["download_url"] = str(request.url_for("download_file", file_id=row["id"]))
    return StoredFile.model_validate(row)
