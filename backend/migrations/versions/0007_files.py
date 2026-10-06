"""Add pluggable storage metadata and soft deletion to uploaded files."""

from typing import Sequence, Union

from alembic import op


revision: str = "0007_files"
down_revision: Union[str, None] = "0003_integrity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Record the storage backend/key and defer physical deletion."""

    # Keep this migration safe to retry on deployments where an operator has
    # already added one of the columns manually.  Existing rows have always
    # used the private local path as ``storage_key``; retaining that value lets
    # them continue to be read without a data migration.
    op.execute(
        """
        ALTER TABLE files
            ADD COLUMN IF NOT EXISTS storage TEXT,
            ADD COLUMN IF NOT EXISTS storage_key TEXT,
            ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ
        """
    )
    op.execute("UPDATE files SET storage = 'local' WHERE storage IS NULL")
    op.execute("ALTER TABLE files ALTER COLUMN storage SET DEFAULT 'local'")
    op.execute("ALTER TABLE files ALTER COLUMN storage SET NOT NULL")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_files_deleted_at ON files(deleted_at)"
    )


def downgrade() -> None:
    # Keep the columns on downgrade: old application workers can ignore them,
    # while dropping them would discard deletion state and backend metadata.
    pass
