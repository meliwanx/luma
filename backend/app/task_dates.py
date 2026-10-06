"""Helpers for storing task due dates as comparable UTC values."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def normalize_due_at(value: Optional[Union[str, datetime]]) -> Optional[str]:
    """Parse a task due date and persist it as an aware UTC ISO value.

    Naive values are wall-clock timestamps in Shanghai, matching the product's
    default user timezone.  Invalid strings use the runtime's established
    ``ValueError`` message so callers can surface the same validation error.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("due_at must be an ISO string or null") from exc
    else:
        raise ValueError("due_at must be an ISO string or null")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    try:
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("due_at must be an ISO string or null") from exc


__all__ = ["normalize_due_at"]
