"""Add resource-library metadata and per-user idea generation state."""

from typing import Sequence, Union

from alembic import op


revision: str = "0015_library_ideas"
down_revision: Union[str, None] = "0014_model_calls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add origin without a default first so only legacy rows are backfilled.
    # A retry must not relabel a newly uploaded file named like a screenshot.
    op.execute("""
        ALTER TABLE files
            ADD COLUMN IF NOT EXISTS origin TEXT,
            ADD COLUMN IF NOT EXISTS message_id TEXT,
            ADD COLUMN IF NOT EXISTS title TEXT,
            ADD COLUMN IF NOT EXISTS last_opened_at TEXT,
            ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT FALSE
    """)
    op.execute("""
        UPDATE files SET origin = CASE
            WHEN filename = 'browser-screenshot.png' THEN 'browser_screenshot'
            ELSE 'upload' END
        WHERE origin IS NULL OR origin = ''
    """)
    op.execute("UPDATE files SET title = filename WHERE title IS NULL")
    op.execute("UPDATE files SET pinned = FALSE WHERE pinned IS NULL")
    op.execute("ALTER TABLE files ALTER COLUMN origin SET DEFAULT 'upload'")
    op.execute("ALTER TABLE files ALTER COLUMN origin SET NOT NULL")
    op.execute("ALTER TABLE files ALTER COLUMN pinned SET DEFAULT FALSE")
    op.execute("ALTER TABLE files ALTER COLUMN pinned SET NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_files_user_opened ON files(user_id, last_opened_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_files_user_created ON files(user_id, created_at)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS ideas (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            plan_markdown TEXT NOT NULL,
            icon TEXT NOT NULL CHECK (icon IN (
                'chart', 'doc', 'search', 'calendar', 'code', 'mail',
                'spark', 'book', 'money', 'heart', 'globe', 'checklist'
            )),
            source_session_ids_json TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'started', 'dismissed', 'superseded'))
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_ideas_user_created ON ideas(user_id, created_at)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS idea_feedback (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            idea_id TEXT NOT NULL,
            title TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('more_like', 'not_interested')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Template ids refer to code constants, so feedback deliberately has no FK.
    op.execute("CREATE INDEX IF NOT EXISTS idx_idea_feedback_user_created ON idea_feedback(user_id, created_at)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS ideas_generation_state (
            user_id TEXT PRIMARY KEY,
            generated_at TIMESTAMPTZ,
            lock_token TEXT,
            lock_until TIMESTAMPTZ,
            refresh_day DATE,
            refresh_count INTEGER NOT NULL DEFAULT 0 CHECK (refresh_count >= 0),
            template_statuses_json TEXT NOT NULL DEFAULT '{}'
        )
    """)

    # Keep the shared background purposes usable by all four feature tasks.
    op.execute("ALTER TABLE model_calls DROP CONSTRAINT IF EXISTS model_calls_purpose_check")
    op.execute("""
        ALTER TABLE model_calls ADD CONSTRAINT model_calls_purpose_check
        CHECK (purpose IN (
            'chat_round', 'final_answer', 'summary', 'voice_cleanup',
            'decider', 'memory_extract', 'other', 'ideas', 'proactive', 'feed'
        ))
    """)


def downgrade() -> None:
    # Retain additive data while old and new workers overlap during rollback.
    pass
