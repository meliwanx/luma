"""Creation of the minimal per-user workspace."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from ..brand import get_brand
from ..db import get_connection


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def welcome_text() -> str:
    return "你好，我是 __BRAND_ASSISTANT__。有什么可以帮你？".replace(
        "__BRAND_ASSISTANT__", get_brand().assistant_name
    )


def _main_session(conn: object, user_id: str) -> object:
    return conn.execute(
        "SELECT id FROM sessions WHERE user_id = ? AND kind = 'main' "
        "ORDER BY updated_at DESC, created_at DESC, id LIMIT 1",
        (user_id,),
    ).fetchone()


def _promote_side_session(conn: object, user_id: str, session_id: str) -> object:
    """Promote one legacy side row while tolerating a concurrent main row."""

    # A main row may have appeared after the caller's first lookup.  Recheck
    # immediately before the UPDATE so the partial unique index is only the
    # final guard, rather than the normal concurrency path.
    session = _main_session(conn, user_id)
    if session is not None:
        return session

    savepoint = "ensure_default_data_promote"
    conn.execute("SAVEPOINT " + savepoint)
    try:
        conn.execute(
            "UPDATE sessions SET kind = 'main' "
            "WHERE id = ? AND user_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM sessions WHERE user_id = ? AND kind = 'main'"
            ")",
            (session_id, user_id, user_id),
        )
    except Exception as exc:
        # Two workers can select different legacy side rows and promote them
        # at the same time.  PostgreSQL reports that race as a unique
        # violation from UPDATE; roll back only the savepoint, then use the
        # committed main row selected by the next statement.
        conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
        if getattr(exc, "pgcode", None) != "23505":
            conn.execute("RELEASE SAVEPOINT " + savepoint)
            raise
    conn.execute("RELEASE SAVEPOINT " + savepoint)
    return _main_session(conn, user_id)


def ensure_default_data(user_id: str = "local") -> None:
    """Ensure a user has a main session and its welcome message."""

    timestamp = now()
    with get_connection() as conn:
        session = _main_session(conn, user_id)
        if session is None:
            # A migrated legacy database should already have a main row.  The
            # fallback below also keeps tests and partially upgraded installs
            # from creating a duplicate session when a side row exists.
            existing = conn.execute(
                "SELECT id FROM sessions WHERE user_id = ? "
                "ORDER BY updated_at DESC, created_at DESC, id LIMIT 1",
                (user_id,),
            ).fetchone()
            if existing is not None:
                session = _promote_side_session(conn, user_id, existing["id"])
            if session is None:
                session_id = "ses_default" if user_id == "local" else new_id("ses")
                inserted = conn.execute(
                    "INSERT INTO sessions(id,user_id,title,kind,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT (user_id) WHERE kind = 'main' DO NOTHING "
                    "RETURNING id",
                    (session_id, user_id, "主聊天", "main", timestamp, timestamp),
                ).fetchone()
                if inserted is not None:
                    conn.execute(
                        "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) "
                        "VALUES (?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
                        (
                            f"msg_welcome_{user_id}",
                            user_id,
                            session_id,
                            "assistant",
                            welcome_text(),
                            timestamp,
                            "{}",
                        ),
                    )
