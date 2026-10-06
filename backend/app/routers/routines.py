"""CRUD endpoints for user-owned scheduled assistant routines."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request, status

from ..models import Routine, RoutineCreate, RoutineUpdate, RuntimeJob
from ..runtime import create_job
from ..scheduler import next_run_at, parse_schedule
from ._common import get_connection, new_id, now, owner_id


router = APIRouter()

_READ_ONLY_TOOLS = ["briefing", "list_tasks", "list_memories", "files"]


def _validate_schedule(value: str) -> str:
    """Validate and normalize one of the supported routine schedules."""

    schedule = value.strip()
    if not schedule:
        raise ValueError("schedule 不能为空")
    try:
        parse_schedule(schedule)
    except (TypeError, ValueError) as exc:
        raise ValueError("schedule 格式无效，支持 daily HH:MM、weekly 1-7 HH:MM 或 every N m（N ≥ 15）") from exc
    return schedule


def _validate_timezone(value: str) -> str:
    timezone_name = value.strip()
    if not timezone_name:
        raise ValueError("timezone 不能为空")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone 无效") from exc
    return timezone_name


def _invalid_schedule(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _routine(row: Any) -> Routine:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    return Routine.model_validate(data)


def _next_run(schedule: str, timezone_name: str) -> str:
    """Return the next occurrence as an ISO UTC value for the DB text column."""

    value = next_run_at(
        schedule,
        after=datetime.now(timezone.utc),
        timezone_name=timezone_name,
    )
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _get_owned_routine(conn: Any, routine_id: str, user_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM routines WHERE id = ? AND user_id = ?",
        (routine_id, user_id),
    ).fetchone()
    return dict(row) if row is not None else None


@router.get("/api/v1/routines", response_model=list[Routine])
def list_routines(
    request: Request,
    enabled_only: bool = Query(default=False),
) -> list[Routine]:
    user_id = owner_id(request)
    query = "SELECT * FROM routines WHERE user_id = ?"
    args: list[Any] = [user_id]
    if enabled_only:
        query += " AND enabled = ?"
        args.append(True)
    query += " ORDER BY created_at DESC"
    with get_connection() as conn:
        rows = conn.execute(query, tuple(args)).fetchall()
    return [_routine(row) for row in rows]


@router.post("/api/v1/routines", response_model=Routine, status_code=status.HTTP_201_CREATED)
def create_routine(request: Request, payload: RoutineCreate) -> Routine:
    user_id = owner_id(request)
    try:
        schedule = _validate_schedule(payload.schedule)
        timezone_name = _validate_timezone(payload.timezone)
        next_run = _next_run(schedule, timezone_name) if payload.enabled else None
    except ValueError as exc:
        raise _invalid_schedule(exc) from exc

    timestamp = now()
    data = {
        "id": new_id("routine"),
        "user_id": user_id,
        "title": payload.title,
        "prompt": payload.prompt,
        "schedule": schedule,
        "timezone": timezone_name,
        "enabled": payload.enabled,
        "next_run_at": next_run,
        "last_run_at": None,
        "last_status": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    with get_connection() as conn:
        if payload.enabled:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM routines WHERE user_id = ? AND enabled = ?",
                (user_id, True),
            ).fetchone()
            if int((row or {}).get("count", 0)) >= 20:
                raise HTTPException(status_code=422, detail="每个用户最多启用 20 个例程")
        conn.execute(
            "INSERT INTO routines(id,user_id,title,prompt,schedule,timezone,enabled,next_run_at,last_run_at,last_status,created_at,updated_at) "
            "VALUES (:id,:user_id,:title,:prompt,:schedule,:timezone,:enabled,:next_run_at,:last_run_at,:last_status,:created_at,:updated_at)",
            data,
        )
    return _routine(data)


@router.patch("/api/v1/routines/{routine_id}", response_model=Routine)
def update_routine(request: Request, routine_id: str, payload: RoutineUpdate) -> Routine:
    user_id = owner_id(request)
    with get_connection() as conn:
        current = _get_owned_routine(conn, routine_id, user_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Routine not found")

        schedule = str(payload.schedule if payload.schedule is not None else current["schedule"])
        timezone_name = str(payload.timezone if payload.timezone is not None else current["timezone"])
        try:
            schedule = _validate_schedule(schedule)
            timezone_name = _validate_timezone(timezone_name)
        except ValueError as exc:
            raise _invalid_schedule(exc) from exc

        enabled = payload.enabled if payload.enabled is not None else bool(current.get("enabled"))
        if enabled and not bool(current.get("enabled")):
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM routines WHERE user_id = ? AND enabled = ? AND id <> ?",
                (user_id, True, routine_id),
            ).fetchone()
            if int((row or {}).get("count", 0)) >= 20:
                raise HTTPException(status_code=422, detail="每个用户最多启用 20 个例程")

        updates: list[str] = []
        args: list[Any] = []
        for field, value in (
            ("title", payload.title),
            ("prompt", payload.prompt),
            ("schedule", schedule if payload.schedule is not None else None),
            ("timezone", timezone_name if payload.timezone is not None else None),
            ("enabled", enabled if payload.enabled is not None else None),
        ):
            if value is not None:
                updates.append(field + " = ?")
                args.append(value)

        schedule_changed = payload.schedule is not None or payload.timezone is not None
        if enabled:
            if schedule_changed or not bool(current.get("enabled")):
                updates.append("next_run_at = ?")
                args.append(_next_run(schedule, timezone_name))
        elif payload.enabled is not None and not enabled:
            updates.append("next_run_at = ?")
            args.append(None)

        if updates:
            updates.append("updated_at = ?")
            args.extend([now(), routine_id, user_id])
            conn.execute(
                "UPDATE routines SET " + ", ".join(updates) + " WHERE id = ? AND user_id = ?",
                tuple(args),
            )
        row = _get_owned_routine(conn, routine_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Routine not found")
    return _routine(row)


@router.delete("/api/v1/routines/{routine_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_routine(request: Request, routine_id: str) -> None:
    user_id = owner_id(request)
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM routines WHERE id = ? AND user_id = ?",
            (routine_id, user_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Routine not found")


@router.post("/api/v1/routines/{routine_id}/run", response_model=RuntimeJob, status_code=status.HTTP_202_ACCEPTED)
def run_routine(request: Request, routine_id: str) -> RuntimeJob:
    user_id = owner_id(request)
    with get_connection() as conn:
        row = _get_owned_routine(conn, routine_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Routine not found")
    try:
        job = create_job(
            "agent_run",
            {
                "prompt": row["prompt"],
                "routine_id": routine_id,
                "allowed_tools": list(_READ_ONLY_TOOLS),
            },
            user_id=user_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RuntimeJob.model_validate(job)


__all__ = ["router", "_validate_schedule"]
