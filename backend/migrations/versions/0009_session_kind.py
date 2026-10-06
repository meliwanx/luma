"""Add main/side session kinds and migrate legacy conversations."""

from typing import Sequence, Union

from alembic import op


revision: str = "0009_session_kind"
down_revision: Union[str, None] = "0008_merge_wave2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add session kinds, select one main per owner, and remove safe orphans."""

    # This migration has been deployed to a few installations that carried
    # the column from a pre-Alembic bootstrap.  Keep every schema operation
    # retry-safe and normalize any partial rows before adding the constraint.
    op.execute(
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'side'"
    )
    op.execute(
        "UPDATE sessions SET kind = 'side' "
        "WHERE kind IS NULL OR kind NOT IN ('main', 'side')"
    )
    op.execute("ALTER TABLE sessions ALTER COLUMN kind SET DEFAULT 'side'")
    op.execute("ALTER TABLE sessions ALTER COLUMN kind SET NOT NULL")
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('sessions') IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1
                   FROM pg_constraint
                   WHERE conname = 'sessions_kind_check'
                     AND conrelid = 'sessions'::regclass
               ) THEN
                ALTER TABLE sessions
                    ADD CONSTRAINT sessions_kind_check
                    CHECK (kind IN ('main', 'side'));
            END IF;
        END $$;
        """
    )

    # Start from a known state so a retry also repairs a partially completed
    # migration.  A session with a message always outranks an empty session;
    # within that group the newest message wins, then the session timestamps
    # and id provide deterministic tie-breakers.
    op.execute("UPDATE sessions SET kind = 'side'")
    op.execute(
        """
        WITH latest_messages AS (
            SELECT session_id, MAX(created_at) AS last_message_at
            FROM messages
            GROUP BY session_id
        ), ranked AS (
            SELECT s.id,
                   ROW_NUMBER() OVER (
                       PARTITION BY s.user_id
                       ORDER BY
                           (lm.last_message_at IS NULL),
                           lm.last_message_at DESC NULLS LAST,
                           s.updated_at DESC,
                           s.created_at DESC,
                           s.id
                   ) AS rank
            FROM sessions AS s
            LEFT JOIN latest_messages AS lm ON lm.session_id = s.id
        )
        UPDATE sessions AS s
        SET kind = 'main'
        FROM ranked AS r
        WHERE s.id = r.id AND r.rank = 1
        """
    )

    # At this point the only session references in the application schema are
    # messages, files, and widgets (the latter was added by 0003).  Keep rows
    # referenced by any of them; only historical, empty side sessions are safe
    # to remove.
    op.execute(
        """
        DO $$
        DECLARE
            removed_count BIGINT;
        BEGIN
            DELETE FROM sessions AS s
            WHERE s.kind = 'side'
              AND NOT EXISTS (
                  SELECT 1 FROM messages AS m WHERE m.session_id = s.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM files AS f WHERE f.session_id = s.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM widgets AS w WHERE w.session_id = s.id
              );
            GET DIAGNOSTICS removed_count = ROW_COUNT;
            RAISE NOTICE USING MESSAGE =
                '0009 removed empty non-main sessions: ' || removed_count;
        END $$;
        """
    )

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_main_per_user "
        "ON sessions(user_id) WHERE kind = 'main'"
    )
    # This supports tenant-scoped message search without requiring pg_trgm on
    # the shared PostgreSQL instance.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_user_created "
        "ON messages(user_id, created_at)"
    )


def downgrade() -> None:
    # Keep the additive column and index during rollback so old and new workers
    # can overlap without losing the main-session invariant or data.
    pass
