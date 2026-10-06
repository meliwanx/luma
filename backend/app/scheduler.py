"""Durable scheduler for task reminders and user routines.

The scheduler is intentionally independent from the HTTP layer.  It keeps the
small schedule grammar here, claims work while holding a PostgreSQL advisory
lock, and writes durable runtime jobs before releasing that lock.  A transient
storage or parsing error is isolated to one tick so the lifespan task can keep
running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import get_connection
from .task_dates import normalize_due_at
from .services.notifications import create_notification

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Asia/Shanghai"
ADVISORY_LOCK_NAME = "luma-scheduler"
READ_ONLY_TOOLS = ["briefing", "list_tasks", "list_memories", "files"]
_SCHEDULE_RE = re.compile(r"^(daily|weekly|every)\s+(.+)$", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^(\d{2}):(\d{2})$")
_WEEKLY_RE = re.compile(r"^([1-7])\s+(\d{2}:\d{2})$")
_EVERY_RE = re.compile(r"^(\d+)m$")


@dataclass(frozen=True)
class ScheduleSpec:
    """Parsed representation of one supported routine schedule."""

    kind: str
    hour: Optional[int] = None
    minute: Optional[int] = None
    weekday: Optional[int] = None
    interval_minutes: Optional[int] = None
    raw: str = ""


def parse_schedule(schedule: Union[str, ScheduleSpec]) -> ScheduleSpec:
    """Parse ``daily``, ``weekly`` and ``every`` routine schedules.

    The grammar deliberately remains smaller than cron.  A ``ValueError`` is
    raised for malformed input so HTTP callers can return a 422 response.
    """

    if isinstance(schedule, ScheduleSpec):
        return schedule
    if not isinstance(schedule, str):
        raise ValueError("schedule 必须是 daily HH:MM、weekly <1-7> HH:MM 或 every <N>m")
    raw = schedule.strip()
    match = _SCHEDULE_RE.fullmatch(raw)
    if not match:
        raise ValueError("schedule 格式应为 daily HH:MM、weekly <1-7> HH:MM 或 every <N>m")
    kind, value = match.group(1).lower(), match.group(2).strip()
    if kind == "daily":
        clock = _CLOCK_RE.fullmatch(value)
        if not clock:
            raise ValueError("daily 时间应为 HH:MM")
        hour, minute = int(clock.group(1)), int(clock.group(2))
        if hour > 23 or minute > 59:
            raise ValueError("daily 时间超出范围")
        return ScheduleSpec(kind="daily", hour=hour, minute=minute, raw=raw)
    if kind == "weekly":
        weekly = _WEEKLY_RE.fullmatch(value)
        if not weekly:
            raise ValueError("weekly 格式应为 weekly <1-7> HH:MM")
        clock = _CLOCK_RE.fullmatch(weekly.group(2))
        # The clock part is covered by the weekly regex; retain the defensive
        # branch so this function is safe if that regex changes later.
        if clock is None:
            raise ValueError("weekly 时间应为 HH:MM")
        hour, minute = int(clock.group(1)), int(clock.group(2))
        if hour > 23 or minute > 59:
            raise ValueError("weekly 时间超出范围")
        return ScheduleSpec(
            kind="weekly",
            hour=hour,
            minute=minute,
            weekday=int(weekly.group(1)),
            raw=raw,
        )
    every = _EVERY_RE.fullmatch(value)
    if not every:
        raise ValueError("every 格式应为 every <N>m")
    interval = int(every.group(1))
    if interval < 15:
        raise ValueError("every 的 N 必须不小于 15")
    return ScheduleSpec(kind="every", interval_minutes=interval, raw=raw)


# A name that reads naturally in route validation code.
validate_schedule = parse_schedule


def _coerce_datetime(value: Optional[Union[str, datetime]], default_zone: Any = timezone.utc) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("时间必须是 ISO 8601 格式") from exc
    else:
        raise ValueError("时间必须是 datetime 或 ISO 8601 字符串")
    if parsed.tzinfo is None:
        # A caller-supplied naive value is a wall-clock value in the routine's
        # configured timezone. Persisted database timestamps are aware UTC.
        parsed = parsed.replace(tzinfo=default_zone)
    return parsed


def _zone(name: Optional[str]) -> ZoneInfo:
    timezone_name = (name or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("无效的时区") from exc


def next_run_at(
    schedule: Union[str, ScheduleSpec],
    after: Optional[Union[str, datetime]] = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    **kwargs: Any,
) -> datetime:
    """Return the next occurrence as an aware UTC ``datetime``.

    ``after`` is exclusive: asking for the next run at exactly 09:00 daily
    returns the following day.  This makes advancing a due routine atomic and
    prevents the same occurrence from being queued twice.
    """

    # ``tz`` is accepted as a small compatibility convenience for callers that
    # use the shorter name, without making it part of the public grammar.
    if kwargs.get("tz") is not None:
        timezone_name = str(kwargs["tz"])
    spec = parse_schedule(schedule)
    zone = _zone(timezone_name)
    base = _coerce_datetime(after, zone).astimezone(zone)

    if spec.kind == "every":
        result = base + timedelta(minutes=int(spec.interval_minutes or 0))
    else:
        assert spec.hour is not None and spec.minute is not None
        candidate = base.replace(
            hour=spec.hour,
            minute=spec.minute,
            second=0,
            microsecond=0,
        )
        if spec.kind == "daily":
            if candidate <= base:
                candidate += timedelta(days=1)
        else:
            # Python weekday is Monday=0, while the API grammar is 1..7.
            target = int(spec.weekday or 1) - 1
            days = (target - candidate.weekday()) % 7
            candidate += timedelta(days=days)
            if candidate <= base:
                candidate += timedelta(days=7)
        result = candidate
    return result.astimezone(timezone.utc)


# Older callers used this descriptive name in early prototypes.
calculate_next_run = next_run_at


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row.get(key, default)
    except AttributeError:
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return default


def _new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)


def _try_advisory_lock(conn: Any) -> bool:
    """Try the scheduler lock without blocking another worker."""

    row = conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(?)) AS locked",
        (ADVISORY_LOCK_NAME,),
    ).fetchone()
    value = _row_value(row, "locked", False)
    return bool(value)


def _queue_routine_job(conn: Any, routine: Any, run_at: str) -> dict[str, Any]:
    user_id = str(_row_value(routine, "user_id", "local"))
    routine_id = str(_row_value(routine, "id", ""))
    prompt = str(_row_value(routine, "prompt", ""))
    timestamp = _now_iso()
    payload = {
        "prompt": prompt,
        "routine_id": routine_id,
        "allowed_tools": list(READ_ONLY_TOOLS),
        "max_attempts": 3,
    }
    job = {
        "id": _new_id("job"),
        "user_id": user_id,
        "type": "agent_run",
        "status": "queued",
        "payload_json": _json(payload),
        "result_json": "{}",
        "error": None,
        "attempts": 0,
        "run_at": run_at,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    conn.execute(
        "INSERT INTO runtime_jobs(id,user_id,type,status,payload_json,result_json,error,attempts,run_at,created_at,updated_at) "
        "VALUES (:id,:user_id,:type,:status,:payload_json,:result_json,:error,:attempts,:run_at,:created_at,:updated_at)",
        job,
    )
    return job


def _due_task_reminders(conn: Any, now_iso: str) -> tuple[int, list[dict[str, Any]]]:
    # Read a bounded batch and compare parsed instants.  New writes and the
    # 0006 migration store UTC, while parsing here also keeps legacy +08:00 and
    # naive Shanghai values correct during a rolling deployment.
    rows = conn.execute(
        "SELECT id,user_id,title,due_at FROM tasks "
        "WHERE due_at IS NOT NULL "
        "AND status IN ('todo','in_progress') AND reminded_at IS NULL "
        "ORDER BY due_at LIMIT 500",
    ).fetchall()
    try:
        current = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        current = _now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    count = 0
    emitted: list[dict[str, Any]] = []
    for row in rows:
        task_id = str(_row_value(row, "id", ""))
        user_id = str(_row_value(row, "user_id", "local"))
        title = str(_row_value(row, "title", ""))
        try:
            due_text = normalize_due_at(_row_value(row, "due_at"))
            due_at = datetime.fromisoformat(str(due_text)) if due_text else None
        except (TypeError, ValueError, OverflowError):
            logger.warning("ignoring invalid task due_at id=%s", task_id)
            continue
        if due_at is None or due_at > current:
            continue
        reminder_at = now_iso
        updated = conn.execute(
            "UPDATE tasks SET reminded_at = ? WHERE id = ? AND reminded_at IS NULL "
            "AND status IN ('todo','in_progress')",
            (reminder_at, task_id),
        )
        if not getattr(updated, "rowcount", 0):
            continue
        emitted.append({
            "user_id": user_id,
            "kind": "task_due",
            "title": "任务到期：%s" % title,
            "body": "任务到期：%s" % title,
            "link": "/app/tasks/%s" % task_id,
        })
        count += 1
    return count, emitted


def _due_routines(conn: Any, now_dt: datetime, now_iso: str) -> tuple[int, list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT id,user_id,title,prompt,schedule,timezone,next_run_at "
        "FROM routines WHERE enabled = TRUE AND next_run_at IS NOT NULL AND next_run_at <= ? "
        "ORDER BY next_run_at LIMIT 500",
        (now_iso,),
    ).fetchall()
    count = 0
    emitted: list[dict[str, Any]] = []
    for row in rows:
        schedule = str(_row_value(row, "schedule", ""))
        timezone_name = str(_row_value(row, "timezone", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE)
        due_value = _row_value(row, "next_run_at")
        try:
            # Advance from the current wall clock so an offline service does
            # not enqueue one job for every missed 15-minute interval.
            next_at = next_run_at(schedule, now_dt, timezone_name)
        except ValueError:
            logger.warning("ignoring invalid routine schedule id=%s", _row_value(row, "id", ""))
            continue
        job = _queue_routine_job(conn, row, now_iso)
        conn.execute(
            "UPDATE routines SET next_run_at = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND enabled = TRUE AND next_run_at <= ?",
            (
                next_at.isoformat(),
                now_iso,
                _row_value(row, "id", ""),
                _row_value(row, "user_id", "local"),
                now_iso,
            ),
        )
        count += 1
        # Publication of routine queue events is not a user notification; the
        # completion callback below sends the result summary.
        _ = due_value
        _ = job
    return count, emitted


def scheduler_tick() -> dict[str, int]:
    """Run one scheduler pass under the process-wide advisory lock."""

    lock_conn = None
    try:
        with get_connection() as conn:
            lock_conn = conn
            if not _try_advisory_lock(conn):
                return {"locked": 0, "task_reminders": 0, "routine_jobs": 0}
            current = _now()
            now_iso = current.isoformat()
            tasks, task_notifications = _due_task_reminders(conn, now_iso)
            routines, _ = _due_routines(conn, current, now_iso)
            from .services.proactive import proactive_tick
            from .services.feed import feed_tick

            proactive_jobs = 0
            feed_jobs = 0
            for name, tick in (("proactive", proactive_tick), ("feed", feed_tick)):
                conn.execute("SAVEPOINT background_tick")
                try:
                    work = tick(conn, current)
                    if name == "feed":
                        feed_jobs = int(work)
                    else:
                        for user_id in work:
                            active = conn.execute(
                                "SELECT id FROM runtime_jobs WHERE user_id = ? AND type = ? "
                                "AND status IN ('queued','running') LIMIT 1", (user_id, "proactive"),
                            ).fetchone()
                            if active:
                                continue
                            conn.execute(
                                "INSERT INTO runtime_jobs(id,user_id,type,status,payload_json,result_json,attempts,run_at,created_at,updated_at) "
                                "VALUES (?,?,'proactive','queued',?,'{}',0,?,?,?)",
                                (_new_id("job"), user_id, _json({"max_attempts": 1}), now_iso, now_iso, now_iso),
                            )
                            proactive_jobs += 1
                    conn.execute("RELEASE SAVEPOINT background_tick")
                except Exception as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT background_tick")
                    conn.execute("RELEASE SAVEPOINT background_tick")
                    logger.warning("background tick kind=%s error=%s", name, type(exc).__name__)
        # Route every notification through the shared persistence/fan-out
        # adapter after the scheduler transaction has committed.
        for item in task_notifications:
            try:
                create_notification(
                    item["user_id"], item["kind"], item["title"], item["body"], item.get("link")
                )
            except Exception:
                logger.exception("task reminder notification failed")
        return {"locked": 1, "task_reminders": tasks, "routine_jobs": routines,
                "proactive_jobs": proactive_jobs, "feed_jobs": feed_jobs}
    except Exception:
        logger.exception("scheduler tick failed")
        return {"locked": 1 if lock_conn is not None else 0, "task_reminders": 0, "routine_jobs": 0}


async def scheduler_loop(stop_event: Any, interval_seconds: float = 30.0) -> None:
    """Keep scheduling while the FastAPI lifespan is alive.

    Each DB call runs in AnyIO's worker thread through ``asyncio.to_thread``;
    no synchronous PostgreSQL operation is allowed to block the event loop.
    """

    interval = max(float(interval_seconds), 0.1)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(scheduler_tick)
        except Exception:
            logger.exception("scheduler loop tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break


def _result_summary(result: Any, detail: Optional[str] = None) -> str:
    if detail:
        return str(detail)[:500]
    if isinstance(result, dict):
        for key in ("message", "summary", "detail", "text", "result"):
            value = result.get(key)
            if value:
                return str(value)[:500]
    if result:
        return str(result)[:500]
    return "例程已完成"


def routine_job_finished(
    job: Any,
    status: str,
    result: Any = None,
    detail: Optional[str] = None,
) -> None:
    """Persist an ``agent_run`` result and notify the routine owner.

    Runtime calls this hook after transitioning a routine job to succeeded or
    failed.  Missing/old jobs are ignored so this hook remains safe during a
    rolling deployment.
    """

    if isinstance(job, dict):
        payload = job.get("payload") or job.get("payload_json") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        routine_id = payload.get("routine_id") if isinstance(payload, dict) else None
        user_id = str(job.get("user_id", "local"))
    else:
        return
    if not routine_id:
        return
    final_status = "succeeded" if str(status).lower() in {"succeeded", "success", "completed"} else "failed"
    timestamp = _now_iso()
    title = "例程执行完成"
    body = _result_summary(result, detail)
    if final_status == "failed":
        title = "例程执行失败"
        body = body or "例程执行失败"
    notification: Optional[dict[str, Any]] = None
    try:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT title FROM routines WHERE id = ? AND user_id = ?",
                (routine_id, user_id),
            ).fetchone()
            if row is None:
                return
            routine_title = str(_row_value(row, "title", "例程"))
            conn.execute(
                "UPDATE routines SET last_run_at = ?, last_status = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (timestamp, final_status, timestamp, routine_id, user_id),
            )
            notification = {
                "user_id": user_id,
                "kind": "routine",
                "title": "%s：%s" % (title, routine_title),
                "body": body,
                "link": "/app/routines/%s" % routine_id,
            }
    except Exception:
        logger.exception("routine completion update failed")
        return
    if notification:
        try:
            create_notification(
                notification["user_id"],
                notification["kind"],
                notification["title"],
                notification["body"],
                notification.get("link"),
            )
        except Exception:
            logger.exception("routine notification failed")


__all__ = [
    "ADVISORY_LOCK_NAME",
    "DEFAULT_TIMEZONE",
    "READ_ONLY_TOOLS",
    "ScheduleSpec",
    "parse_schedule",
    "validate_schedule",
    "next_run_at",
    "calculate_next_run",
    "scheduler_tick",
    "scheduler_loop",
    "routine_job_finished",
]
