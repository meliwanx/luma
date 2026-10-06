"""Validated preferences and short transactional claims for background work."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from ..db import get_connection


class ProactivePrefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = True
    max_per_day: int = Field(default=2, ge=0, le=5, strict=True)
    window_start: str = "09:00"
    window_end: str = "21:30"
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    topics_like: str = Field(default="", max_length=1000)
    topics_avoid: str = Field(default="", max_length=1000)
    style: str = Field(default="", max_length=500)
    feed_enabled: StrictBool = True
    feed_per_day: int = Field(default=1, ge=0, le=3, strict=True)
    feed_instructions: str = Field(default="", max_length=2000)

    @field_validator("window_start", "window_end")
    @classmethod
    def validate_clock(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("时间必须是合法的 HH:MM")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("无效的时区") from exc
        return value


DEFAULT_PREFS = ProactivePrefs().model_dump()


def get_prefs(user_id: str, conn: Any = None) -> dict[str, Any]:
    if conn is None:
        with get_connection() as connection:
            return get_prefs(user_id, connection)
    row = conn.execute("SELECT * FROM proactive_prefs WHERE user_id = ?", (user_id,)).fetchone()
    return {key: row[key] for key in DEFAULT_PREFS} if row else dict(DEFAULT_PREFS)


def put_prefs(user_id: str, prefs: ProactivePrefs) -> dict[str, Any]:
    data = prefs.model_dump()
    values = {**data, "user_id": user_id, "updated_at": datetime.now(timezone.utc).isoformat()}
    columns = list(values)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO proactive_prefs (" + ",".join(columns) + ") VALUES ("
            + ",".join(":" + key for key in columns) + ") ON CONFLICT (user_id) DO UPDATE SET "
            + ",".join(key + " = EXCLUDED." + key for key in columns if key != "user_id"),
            values,
        )
    return data


def local_now(prefs: dict[str, Any], now: Optional[datetime] = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(ZoneInfo(prefs["timezone"]))


def in_window(prefs: dict[str, Any], now: Optional[datetime] = None) -> bool:
    clock = local_now(prefs, now).strftime("%H:%M")
    start, end = prefs["window_start"], prefs["window_end"]
    if start <= end:
        return start <= clock <= end
    return clock >= start or clock <= end


def get_state(conn: Any, user_id: str, local_day: str) -> dict[str, Any]:
    conn.execute(
        "INSERT INTO proactive_state (user_id,local_day) VALUES (?,?) ON CONFLICT (user_id) DO NOTHING",
        (user_id, local_day),
    )
    row = conn.execute(
        "SELECT * FROM proactive_state WHERE user_id = ? FOR UPDATE", (user_id,),
    ).fetchone()
    state = dict(row)
    if state["local_day"] != local_day:
        conn.execute(
            "UPDATE proactive_state SET local_day = ?, sent_today = 0, feed_today = 0 WHERE user_id = ?",
            (local_day, user_id),
        )
        state.update(local_day=local_day, sent_today=0, feed_today=0)
    return state


def candidate_users(conn: Any, now: Optional[datetime] = None) -> list[str]:
    current = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM messages WHERE role = 'user' AND created_at::timestamptz >= ?::timestamptz ORDER BY user_id",
        ((current - timedelta(days=14)).isoformat(),),
    ).fetchall()
    return [str(row["user_id"]) for row in rows]


def claim_tick(conn: Any, name: str, now: datetime, interval_seconds: int) -> bool:
    # A completed internal queue row is a durable scheduler cursor, never a
    # runnable job. Reusing it keeps throttling shared across both workers
    # without adding a fourth table or depending on Redis availability.
    timestamp = now.astimezone(timezone.utc).isoformat()
    cutoff = (now - timedelta(seconds=interval_seconds)).astimezone(timezone.utc).isoformat()
    row = conn.execute(
        "INSERT INTO runtime_jobs (id,user_id,type,status,payload_json,result_json,attempts,run_at,created_at,updated_at) "
        "VALUES (?,?,'scheduler_tick','succeeded','{}','{}',0,?,?,?) "
        "ON CONFLICT (id) DO UPDATE SET updated_at = EXCLUDED.updated_at "
        "WHERE runtime_jobs.updated_at <= ? RETURNING id",
        ("scheduler_tick:" + name, "", timestamp, timestamp, timestamp, cutoff),
    ).fetchone()
    return row is not None
