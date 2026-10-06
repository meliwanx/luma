"""Store short-lived encrypted secrets pasted in chat."""

from typing import Sequence, Union

from alembic import op


revision: str = "0012_chat_secrets"
down_revision: Union[str, None] = "0011_tool_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE chat_secrets (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            ciphertext TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX idx_chat_secrets_expires ON chat_secrets(expires_at)")
    op.execute("CREATE INDEX idx_chat_secrets_user_session ON chat_secrets(user_id, session_id)")


def downgrade() -> None:
    op.execute("DROP TABLE chat_secrets")
