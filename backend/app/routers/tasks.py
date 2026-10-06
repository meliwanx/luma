"""Task CRUD endpoints and legacy web adapter."""

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status

from ..db import get_connection
from ..models import Task, TaskCreate, TaskUpdate
from ..task_dates import normalize_due_at
from ._common import make_task, new_id, now, owner_id

router = APIRouter()
legacy_router = APIRouter()


@router.get("/api/v1/tasks", response_model=list[Task])
def list_tasks(request: Request, status_filter: Optional[str] = Query(default=None, alias="status"), limit: int = Query(default=100, ge=1, le=500)) -> list[Task]:
    user_id = owner_id(request)
    if status_filter and status_filter not in {"todo", "in_progress", "done", "cancelled"}:
        raise HTTPException(status_code=422, detail="Invalid task status")
    with get_connection() as conn:
        if status_filter:
            rows = conn.execute("SELECT * FROM tasks WHERE user_id = ? AND status = ? ORDER BY due_at IS NULL, due_at, updated_at DESC LIMIT ?", (user_id, status_filter, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks WHERE user_id = ? ORDER BY due_at IS NULL, due_at, updated_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [make_task(dict(row)) for row in rows]


@router.post("/api/v1/tasks", response_model=Task, status_code=status.HTTP_201_CREATED)
def create_task(request: Request, payload: TaskCreate) -> Task:
    user_id = owner_id(request)
    timestamp = now()
    data = {"id": new_id("task"), "user_id": user_id, "title": payload.title, "description": payload.description, "status": payload.status, "due_at": normalize_due_at(payload.due_at), "created_at": timestamp, "updated_at": timestamp, "metadata_json": json.dumps(payload.metadata, ensure_ascii=False)}
    with get_connection() as conn:
        conn.execute("INSERT INTO tasks(id,user_id,title,description,status,due_at,created_at,updated_at,metadata_json) VALUES (:id,:user_id,:title,:description,:status,:due_at,:created_at,:updated_at,:metadata_json)", data)
    return make_task(data.copy())


@router.patch("/api/v1/tasks/{task_id}", response_model=Task)
def update_task(request: Request, task_id: str, payload: TaskUpdate) -> Task:
    user_id = owner_id(request)
    updates: list[str] = []
    args: list[Any] = []
    for field in ("title", "description", "status"):
        value = getattr(payload, field)
        if value is not None:
            updates.append(f"{field} = ?")
            args.append(value)
    # Pydantic tracks explicitly supplied nulls separately from an omitted
    # field.  Clearing a due date is still a due-date update and must allow a
    # future date to trigger a fresh reminder.
    fields_set = getattr(payload, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(payload, "__fields_set__", set())
    if payload.due_at is not None or "due_at" in fields_set:
        updates.append("due_at = ?")
        args.append(normalize_due_at(payload.due_at))
        updates.append("reminded_at = NULL")
    if payload.metadata is not None:
        updates.append("metadata_json = ?")
        args.append(json.dumps(payload.metadata, ensure_ascii=False))
    with get_connection() as conn:
        if updates:
            updates.append("updated_at = ?")
            args.extend([now(), task_id, user_id])
            cursor = conn.execute(f'UPDATE tasks SET {", ".join(updates)} WHERE id = ? AND user_id = ?', args)
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Task not found")
        row = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return make_task(dict(row))


@router.delete("/api/v1/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(request: Request, task_id: str) -> None:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, owner_id(request)))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Task not found")


@legacy_router.patch("/api/tasks/{task_id}")
def web_task_update(request: Request, task_id: str, payload: TaskUpdate) -> Task:
    return update_task(request, task_id, payload)
