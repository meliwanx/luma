from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Request
from ..db import get_connection
from ..deps import owner_id, ensure_user_data
from ..mappers import make_memory, make_task
from ..runtime import list_activity
router = APIRouter()

@router.get("/api/dashboard")
@router.get("/api/v1/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    """Compact view model for the web and desktop shells."""
    user_id = owner_id(request)
    ensure_user_data(user_id)
    with get_connection() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE user_id = ? AND kind = 'main' LIMIT 1", (user_id,)).fetchone()
        messages = conn.execute(
            "SELECT * FROM ("
            "SELECT * FROM messages WHERE user_id = ? AND session_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 100"
            ") recent ORDER BY created_at ASC, id ASC",
            (user_id, session["id"]),
        ).fetchall()
        memories = conn.execute("SELECT * FROM memories WHERE user_id = ? ORDER BY importance DESC, updated_at DESC LIMIT 100", (user_id,)).fetchall()
        tasks = conn.execute("SELECT * FROM tasks WHERE user_id = ? ORDER BY updated_at DESC LIMIT 100", (user_id,)).fetchall()
    activity = [
        {
            "time": item.get("created_at", "")[11:16],
            "text": item.get("title", "后台活动"),
            "tag": item.get("kind", "RUNTIME").upper(),
        }
        for item in list_activity(limit=50, user_id=user_id)
    ]
    return {
        "conversation_id": session["id"],
        "messages": [{"id": row["id"], "role": row["role"], "content": row["content"], "status": row.get("status", "complete"), "time": row["created_at"][11:16]} for row in messages],
        "memories": [
            {
                **(memory := make_memory(dict(row))).model_dump(mode="json"),
                "kind": memory.category,
            }
            for row in memories
        ],
        "tasks": [
            {
                **(task := make_task(dict(row))).model_dump(mode="json"),
                "status": {"todo": "queued", "cancelled": "done"}.get(task.status, task.status),
                "progress": 100 if task.status == "done" else 0,
                "due_at": task.due_at.isoformat() if task.due_at else "待安排",
            }
            for row in tasks
        ],
        "activity": activity,
    }
