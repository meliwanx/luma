"""Goal CRUD endpoints."""

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status

from ..db import get_connection
from ..models import Goal, GoalCreate, GoalUpdate
from ._common import make_goal, new_id, now, owner_id, write_fields

router = APIRouter()


@router.get("/api/v1/goals", response_model=list[Goal])
def list_goals(request: Request, status_filter: Optional[str] = Query(default=None, alias="status"), limit: int = Query(default=100, ge=1, le=500)) -> list[Goal]:
    user_id = owner_id(request)
    args: list[Any] = [user_id]
    where = "WHERE user_id = ?"
    if status_filter:
        if status_filter not in {"active", "completed", "paused", "cancelled", "archived"}:
            raise HTTPException(status_code=422, detail="Invalid goal status")
        where += " AND status = ?"
        args.append(status_filter)
    args.append(limit)
    with get_connection() as conn:
        rows = conn.execute(f"SELECT * FROM goals {where} ORDER BY updated_at DESC LIMIT ?", tuple(args)).fetchall()
    return [make_goal(dict(row)) for row in rows]


@router.post("/api/v1/goals", response_model=Goal, status_code=status.HTTP_201_CREATED)
def create_goal(request: Request, payload: GoalCreate) -> Goal:
    user_id = owner_id(request)
    timestamp = now()
    data = {"id": new_id("goal"), "user_id": user_id, "title": payload.title, "description": payload.description, "status": payload.status, "progress": payload.progress, "due_at": payload.due_at.isoformat() if payload.due_at else None, "created_at": timestamp, "updated_at": timestamp, "metadata_json": json.dumps(payload.metadata, ensure_ascii=False)}
    with get_connection() as conn:
        conn.execute("INSERT INTO goals(id,user_id,title,description,status,progress,due_at,created_at,updated_at,metadata_json) VALUES (:id,:user_id,:title,:description,:status,:progress,:due_at,:created_at,:updated_at,:metadata_json)", data)
    return make_goal(data.copy())


@router.patch("/api/v1/goals/{goal_id}", response_model=Goal)
def update_goal(request: Request, goal_id: str, payload: GoalUpdate) -> Goal:
    user_id = owner_id(request)
    updates, args = write_fields(payload, ("title", "description", "status", "progress", "due_at", "metadata"), json_fields=("metadata",))
    if "due_at = ?" in updates:
        idx = updates.index("due_at = ?")
        if isinstance(args[idx], datetime):
            args[idx] = args[idx].isoformat()
    if "metadata = ?" in updates:
        updates[updates.index("metadata = ?")] = "metadata_json = ?"
    with get_connection() as conn:
        if updates:
            updates.append("updated_at = ?")
            args.extend([now(), goal_id, user_id])
            cursor = conn.execute(f'UPDATE goals SET {", ".join(updates)} WHERE id = ? AND user_id = ?', tuple(args))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Goal not found")
        row = conn.execute("SELECT * FROM goals WHERE id = ? AND user_id = ?", (goal_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return make_goal(dict(row))


@router.delete("/api/v1/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_goal(request: Request, goal_id: str) -> None:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM goals WHERE id = ? AND user_id = ?", (goal_id, owner_id(request)))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Goal not found")
