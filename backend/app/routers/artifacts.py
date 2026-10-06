"""Artifact CRUD endpoints."""

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status

from ..db import get_connection
from ..models import Artifact, ArtifactCreate, ArtifactUpdate
from ._common import make_artifact, new_id, now, owner_id, write_fields

router = APIRouter()


@router.get("/api/v1/artifacts", response_model=list[Artifact])
def list_artifacts(request: Request, kind: Optional[str] = None, limit: int = Query(default=100, ge=1, le=500)) -> list[Artifact]:
    user_id = owner_id(request)
    with get_connection() as conn:
        if kind:
            rows = conn.execute("SELECT * FROM artifacts WHERE user_id = ? AND kind = ? ORDER BY updated_at DESC LIMIT ?", (user_id, kind, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM artifacts WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [make_artifact(dict(row)) for row in rows]


@router.post("/api/v1/artifacts", response_model=Artifact, status_code=status.HTTP_201_CREATED)
def create_artifact(request: Request, payload: ArtifactCreate) -> Artifact:
    user_id = owner_id(request)
    timestamp = now()
    data = {"id": new_id("artifact"), "user_id": user_id, "title": payload.title, "kind": payload.kind, "content": payload.content, "uri": payload.uri, "status": payload.status, "created_at": timestamp, "updated_at": timestamp, "metadata_json": json.dumps(payload.metadata, ensure_ascii=False)}
    with get_connection() as conn:
        conn.execute("INSERT INTO artifacts(id,user_id,title,kind,content,uri,status,created_at,updated_at,metadata_json) VALUES (:id,:user_id,:title,:kind,:content,:uri,:status,:created_at,:updated_at,:metadata_json)", data)
    return make_artifact(data.copy())


@router.get("/api/v1/artifacts/{artifact_id}", response_model=Artifact)
def get_artifact(request: Request, artifact_id: str) -> Artifact:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, owner_id(request))).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return make_artifact(dict(row))


@router.patch("/api/v1/artifacts/{artifact_id}", response_model=Artifact)
def update_artifact(request: Request, artifact_id: str, payload: ArtifactUpdate) -> Artifact:
    user_id = owner_id(request)
    updates, args = write_fields(payload, ("title", "kind", "content", "uri", "status", "metadata"), json_fields=("metadata",))
    if "metadata = ?" in updates:
        updates[updates.index("metadata = ?")] = "metadata_json = ?"
    with get_connection() as conn:
        if updates:
            updates.append("updated_at = ?")
            args.extend([now(), artifact_id, user_id])
            cursor = conn.execute(f'UPDATE artifacts SET {", ".join(updates)} WHERE id = ? AND user_id = ?', tuple(args))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Artifact not found")
        row = conn.execute("SELECT * FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return make_artifact(dict(row))


@router.delete("/api/v1/artifacts/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_artifact(request: Request, artifact_id: str) -> None:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, owner_id(request)))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Artifact not found")
