"""Private feed listing, feedback and side-conversation endpoints."""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from ..db import get_connection
from ..services.feed import decode_cursor, encode_cursor, enqueue_feed, make_post
from ._common import new_id, now, owner_id


router = APIRouter()


class FeedFeedback(BaseModel):
    action: Literal["like", "unlike", "not_interested"]


@router.get("/api/v1/feed")
def list_feed(request: Request, limit: int = Query(default=20, ge=1, le=100), cursor: Optional[str] = Query(default=None, max_length=512)) -> dict[str, Any]:
    user_id = owner_id(request)
    sql = "SELECT * FROM feed_posts WHERE user_id = ? AND deleted_at IS NULL AND dismissed = FALSE"
    args: list[Any] = [user_id]
    if cursor:
        created, post_id = decode_cursor(cursor)
        sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
        args.extend((created, created, post_id))
    with get_connection() as conn:
        rows = conn.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", tuple(args + [limit + 1])).fetchall()
    items = rows[:limit]
    return {"items": [make_post(row) for row in items], "next_cursor": encode_cursor(items[-1]) if len(rows) > limit else None}


@router.post("/api/v1/feed/refresh", status_code=status.HTTP_202_ACCEPTED)
def refresh_feed(request: Request) -> dict[str, Any]:
    return enqueue_feed(owner_id(request))


@router.post("/api/v1/feed/{post_id}/feedback")
def feedback_feed(request: Request, post_id: str, payload: FeedFeedback) -> dict[str, Any]:
    user_id = owner_id(request)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM feed_posts WHERE id = ? AND user_id = ? AND deleted_at IS NULL FOR UPDATE", (post_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="动态不存在")
        if payload.action == "not_interested":
            conn.execute("UPDATE feed_posts SET dismissed = TRUE, liked = FALSE WHERE id = ? AND user_id = ?", (post_id, user_id))
        else:
            conn.execute("UPDATE feed_posts SET liked = ? WHERE id = ? AND user_id = ?", (payload.action == "like", post_id, user_id))
        updated = conn.execute("SELECT * FROM feed_posts WHERE id = ? AND user_id = ?", (post_id, user_id)).fetchone()
    return make_post(updated)


@router.delete("/api/v1/feed/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_feed(request: Request, post_id: str) -> Response:
    user_id = owner_id(request)
    with get_connection() as conn:
        result = conn.execute("UPDATE feed_posts SET deleted_at = ? WHERE id = ? AND user_id = ? AND deleted_at IS NULL", (now(), post_id, user_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="动态不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/v1/feed/{post_id}/discuss")
def discuss_feed(request: Request, post_id: str) -> dict[str, str]:
    user_id, timestamp, session_id = owner_id(request), now(), new_id("ses")
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM feed_posts WHERE id = ? AND user_id = ? AND deleted_at IS NULL AND dismissed = FALSE FOR UPDATE", (post_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="动态不存在")
        post = make_post(row)
        # A new user's first Feed discussion must stay a side conversation
        # when the normal session bootstrap subsequently runs.
        conn.execute(
            "INSERT INTO sessions(id,user_id,title,kind,created_at,updated_at) VALUES (?,?,?,'main',?,?) "
            "ON CONFLICT (user_id) WHERE kind = 'main' DO NOTHING",
            (new_id("ses"), user_id, "主聊天", timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO sessions(id,user_id,title,kind,created_at,updated_at) VALUES (?,?,?,'side',?,?)",
            (session_id, user_id, post["title"], timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) "
            "VALUES (?,?,?,'assistant',?,?,?,'complete')",
            (new_id("msg"), user_id, session_id, post["body_markdown"], timestamp,
             json.dumps({"feed_post_id": post_id, "sources": post["sources"]}, ensure_ascii=False)),
        )
    return {"session_id": session_id}
