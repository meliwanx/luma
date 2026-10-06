"""Memory CRUD endpoints and user extraction settings."""

import json
import os
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status

from ..db import get_connection
from ..models import Memory, MemoryConfirm, MemoryCreate, MemorySettings, MemoryUpdate
from ._common import make_memory, new_id, now, owner_id

router = APIRouter()
legacy_router = APIRouter()


def _settings_row(conn: Any, user_id: str) -> bool:
    if os.getenv("MEMORY_AUTO_EXTRACT", "true").strip().lower() in {"0", "false", "no", "off"}:
        return False
    row = conn.execute("SELECT memory_auto_extract FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return True
    value = row.get("memory_auto_extract") if isinstance(row, dict) else row[0]
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


@router.get("/api/v1/memories/settings", response_model=MemorySettings)
def get_memory_settings(request: Request) -> MemorySettings:
    with get_connection() as conn:
        enabled = _settings_row(conn, owner_id(request))
    return MemorySettings(auto_extract=enabled)


@router.patch("/api/v1/memories/settings", response_model=MemorySettings)
def update_memory_settings(request: Request, payload: MemorySettings) -> MemorySettings:
    user_id = owner_id(request)
    timestamp = now()
    with get_connection() as conn:
        updated = conn.execute(
            "UPDATE users SET memory_auto_extract = ?, last_seen_at = ? WHERE user_id = ?",
            (payload.auto_extract, timestamp, user_id),
        )
        if updated.rowcount == 0:
            raise HTTPException(status_code=404, detail="Account not found")
    return MemorySettings(auto_extract=payload.auto_extract)


@router.get("/api/v1/memories", response_model=list[Memory])
def list_memories(request: Request, q: Optional[str] = Query(default=None, max_length=200), category: Optional[str] = Query(default=None, max_length=80), limit: int = Query(default=100, ge=1, le=500)) -> list[Memory]:
    user_id = owner_id(request)
    clauses = ["user_id = ?"]
    args: list[Any] = [user_id]
    if q:
        clauses.append("content LIKE ?")
        args.append("%" + q + "%")
    if category:
        clauses.append("category = ?")
        args.append(category)
    where = "WHERE " + " AND ".join(clauses)
    with get_connection() as conn:
        rows = conn.execute(f"SELECT * FROM memories {where} ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT ?", (*args, limit)).fetchall()
    return [make_memory(dict(row)) for row in rows]


@router.post("/api/v1/memories", response_model=Memory, status_code=status.HTTP_201_CREATED)
def create_memory(request: Request, payload: MemoryCreate) -> Memory:
    user_id = owner_id(request)
    timestamp = now()
    data = {"id": new_id("mem"), "user_id": user_id, "content": payload.content, "category": payload.category, "importance": payload.importance, "pinned": payload.pinned, "created_at": timestamp, "updated_at": timestamp, "metadata_json": json.dumps(payload.metadata, ensure_ascii=False)}
    with get_connection() as conn:
        conn.execute("INSERT INTO memories(id,user_id,content,category,importance,created_at,updated_at,metadata_json,pinned) VALUES (:id,:user_id,:content,:category,:importance,:created_at,:updated_at,:metadata_json,:pinned)", data)
    return make_memory(data.copy())


@router.patch("/api/v1/memories/{memory_id}", response_model=Memory)
def update_memory(request: Request, memory_id: str, payload: MemoryUpdate) -> Memory:
    user_id = owner_id(request)
    updates: list[str] = []
    args: list[Any] = []
    for field in ("content", "category", "importance", "pinned"):
        value = getattr(payload, field)
        if value is not None:
            updates.append(f"{field} = ?")
            args.append(value)
    if payload.metadata is not None:
        updates.append("metadata_json = ?")
        args.append(json.dumps(payload.metadata, ensure_ascii=False))
    with get_connection() as conn:
        if updates:
            updates.append("updated_at = ?")
            args.extend([now(), memory_id, user_id])
            cursor = conn.execute(f'UPDATE memories SET {", ".join(updates)} WHERE id = ? AND user_id = ?', args)
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Memory not found")
        row = conn.execute("SELECT * FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return make_memory(dict(row))


@router.post("/api/v1/memories/{memory_id}/confirm", response_model=Memory)
def confirm_memory(request: Request, memory_id: str, payload: Optional[MemoryConfirm] = None) -> Memory:
    user_id = owner_id(request)
    requested = (payload.kind if payload is not None else None) or (payload.category if payload is not None else None)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        record = dict(row)
        if str(record.get("category") or "").strip().lower() not in {"inferred", "推断"}:
            return make_memory(record)
        metadata = {}
        try:
            metadata = json.loads(record.get("metadata_json") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        kind = str(requested or metadata.get("kind") or "fact").strip().lower()
        kind = {"事实": "fact", "偏好": "preference"}.get(kind, kind)
        if kind not in {"fact", "preference"}:
            raise HTTPException(status_code=422, detail="kind must be fact or preference")
        metadata["kind"] = kind
        metadata["confirmed"] = True
        conn.execute(
            "UPDATE memories SET category = ?, metadata_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (kind, json.dumps(metadata, ensure_ascii=False), now(), memory_id, user_id),
        )
        updated = conn.execute("SELECT * FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id)).fetchone()
    return make_memory(dict(updated))


@router.delete("/api/v1/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(request: Request, memory_id: str) -> None:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM memories WHERE id = ? AND user_id = ?", (memory_id, owner_id(request)))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Memory not found")


@legacy_router.delete("/api/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def web_memory_delete(request: Request, memory_id: str) -> None:
    return delete_memory(request, memory_id)
