"""Add durable status to messages for resumable generation."""
from typing import Sequence, Union

from alembic import op

revision: str = "0004_stream"
down_revision: Union[str, None] = "0003_integrity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL 11+ stores a constant default in the catalog, so this does
    # not rewrite the existing messages table.  Keep the migration idempotent
    # for deployments that briefly carried the column before it was recorded.
    op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'complete'")
    op.execute("UPDATE messages SET status = 'complete' WHERE status IS NULL")
    op.execute("ALTER TABLE messages ALTER COLUMN status SET DEFAULT 'complete'")
    op.execute("ALTER TABLE messages ALTER COLUMN status SET NOT NULL")

    # Older workers recorded an unfinished assistant reply in metadata_json
    # instead of the status column.  metadata_json is free-form TEXT and may
    # contain malformed JSON, so inspect each candidate inside a savepoint-like
    # PL/pgSQL block.  One malformed row must not abort the migration.
    op.execute(
        """
        DO $$
        DECLARE
            candidate RECORD;
        BEGIN
            FOR candidate IN
                SELECT id, metadata_json
                FROM messages
                WHERE status = 'complete'
                  AND position('"incomplete"' IN metadata_json) > 0
            LOOP
                BEGIN
                    IF candidate.metadata_json::jsonb ->> 'incomplete' = 'true' THEN
                        UPDATE messages
                        SET status = 'incomplete'
                        WHERE id = candidate.id AND status = 'complete';
                    END IF;
                EXCEPTION WHEN others THEN
                    -- Keep invalid legacy metadata from blocking deploys.
                    NULL;
                END;
            END LOOP;
        END $$;
        """
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname = 'messages_status_check' "
        "AND conrelid = 'messages'::regclass) THEN "
        "ALTER TABLE messages ADD CONSTRAINT messages_status_check "
        "CHECK (status IN ('streaming', 'complete', 'incomplete', 'error')); "
        "END IF; END $$;"
    )
    # Stale-generation recovery scans all owners by status and age.  Keep that
    # hot path independent from the tenant-scoped message listing index.
    op.execute("CREATE INDEX IF NOT EXISTS idx_messages_status_created ON messages(status, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_status_created ON messages(user_id, status, created_at)")


def downgrade() -> None:
    # Keep the column on downgrade so workers from the newer release can
    # finish safely while an operator rolls back application code.
    pass
