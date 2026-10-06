"""Store user-scoped long-lived tool permission choices."""

from typing import Sequence, Union

from alembic import op


revision: str = "0011_tool_permissions"
down_revision: Union[str, None] = "0009_session_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tool_permissions (
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'ask',
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (user_id, key),
            CONSTRAINT tool_permissions_mode_check CHECK (mode IN ('ask', 'always'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_permissions_user_updated "
        "ON tool_permissions(user_id, updated_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tool_permissions")
