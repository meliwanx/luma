"""Durable routines, scheduler markers, and push device registrations."""
from datetime import datetime, timezone
from typing import Sequence, Union
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa


revision: str = "0006_scheduler"
down_revision: Union[str, None] = "0003_integrity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _normalize_due_at(value: object) -> Union[str, None]:
    """Best-effort conversion for existing task rows during migration."""

    if not isinstance(value, str):
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def upgrade() -> None:
    # The task marker is deliberately nullable.  Existing rows that were
    # already overdue are marked below so the first scheduler tick after a
    # deployment does not fan out a reminder for every historical task.
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reminded_at TEXT")
    # Normalize rows before the scheduler starts comparing due_at as UTC ISO
    # text.  A malformed legacy value is retained so migration never blocks.
    bind = op.get_bind()
    migration_time = bind.execute(sa.text("SELECT CURRENT_TIMESTAMP")).scalar()
    if not isinstance(migration_time, datetime):
        migration_time = datetime.now(timezone.utc)
    elif migration_time.tzinfo is None:
        migration_time = migration_time.replace(tzinfo=timezone.utc)
    else:
        migration_time = migration_time.astimezone(timezone.utc)

    rows = bind.execute(
        sa.text("SELECT id, due_at, status, reminded_at FROM tasks WHERE due_at IS NOT NULL")
    ).fetchall()
    for row in rows:
        normalized = _normalize_due_at(row[1])
        if normalized is not None:
            bind.execute(
                sa.text("UPDATE tasks SET due_at = :due_at WHERE id = :id"),
                {"due_at": normalized, "id": row[0]},
            )
            # The scheduler only reminds actionable task states.  Keep the
            # migration aligned with that allowlist so cancelled or completed
            # historical rows cannot produce a notification on first boot.
            parsed_due_at = datetime.fromisoformat(normalized)
            status = str(row[2] or "").lower()
            if parsed_due_at < migration_time and status in {"todo", "in_progress"} and row[3] is None:
                bind.execute(
                    sa.text(
                        "UPDATE tasks SET reminded_at = :reminded_at "
                        "WHERE id = :id AND reminded_at IS NULL"
                    ),
                    {"reminded_at": migration_time.isoformat(), "id": row[0]},
                )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS routines (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'local',
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            schedule TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            next_run_at TEXT,
            last_run_at TEXT,
            last_status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS push_devices (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            token TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(platform, token)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due_reminder ON tasks(status, due_at, reminded_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_routines_due ON routines(enabled, next_run_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_routines_user ON routines(user_id, enabled, next_run_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_push_devices_user ON push_devices(user_id, updated_at)")


def downgrade() -> None:
    # Keep data on downgrade; deployment rollback must not erase device tokens
    # or user schedules.  The additive schema is safe for older workers.
    pass
