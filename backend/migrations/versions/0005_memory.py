"""Add durable context summaries, memory pinning and extraction settings."""

from typing import Sequence, Union

from alembic import op

revision: str = "0005_memory"
down_revision: Union[str, None] = "0003_integrity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``IF NOT EXISTS`` keeps this safe for installations that added the
    # columns during an earlier pre-Alembic deployment.
    op.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary_until TEXT")
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary_in_progress BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS summary_started_at TIMESTAMPTZ")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS memory_auto_extract BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute("CREATE INDEX IF NOT EXISTS idx_memories_user_pinned ON memories(user_id, pinned, updated_at DESC)")


def downgrade() -> None:
    # Keep columns on downgrade: older workers ignore them and dropping user
    # data or summaries during a rollback is unsafe.
    pass
