"""Indexes for tenant-scoped and queue hot paths."""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_indexes"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEXES = (
    # API list/detail paths.
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_updated ON sessions(user_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_created ON sessions(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_user_session_created ON messages(user_id, session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_user_created ON messages(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_role_created ON messages(role, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_user_status_due ON tasks(user_id, status, due_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_user_updated ON tasks(user_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memories_user_category ON memories(user_id, category)",
    "CREATE INDEX IF NOT EXISTS idx_memories_user_importance_updated ON memories(user_id, importance DESC, updated_at DESC)",
    # Agent Runtime queue and activity paths.
    "CREATE INDEX IF NOT EXISTS idx_runtime_activity_user_created ON runtime_activity(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_jobs_status_run_at ON runtime_jobs(status, run_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_jobs_user_created ON runtime_jobs(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_leases_user_hash_status ON runtime_leases(user_id_hash, status)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_approvals_user_status_created ON runtime_approvals(user_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_approvals_user_created ON runtime_approvals(user_id, created_at DESC)",
    # Other frequently filtered or ordered collections.
    "CREATE INDEX IF NOT EXISTS idx_widgets_user_session_created ON widgets(user_id, session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_widgets_user_message_created ON widgets(user_id, message_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_user_status_updated ON artifacts(user_id, status, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_files_user_updated ON files(user_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_files_user_created ON files(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_files_user_session_created ON files(user_id, session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_connectors_user_kind_enabled ON connectors(user_id, kind, enabled)",
    "CREATE INDEX IF NOT EXISTS idx_connectors_user_kind_created ON connectors(user_id, kind, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_user_created ON notifications(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_client_devices_user_client ON client_devices(user_id, client, last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_request_log_user_created ON request_log(user_id, created_at)",
)


def upgrade() -> None:
    for statement in INDEXES:
        op.execute(statement)


def downgrade() -> None:
    # Keep indexes in place when rolling back; dropping them can surprise a
    # live worker and is not needed for compatibility with older releases.
    pass
