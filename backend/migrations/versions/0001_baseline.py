"""Create the PostgreSQL storage schema used by the assistant.

This migration intentionally uses idempotent DDL.  It can bootstrap an empty
schema and can also be applied to a database created by the pre-Alembic
startup code without replacing any existing rows.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Keep this list in dependency order: child tables refer to the earlier tables.
DDL = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        title TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS widgets (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        type TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        state_json TEXT NOT NULL DEFAULT '{}',
        fallback TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        content TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',
        importance INTEGER NOT NULL DEFAULT 3,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'todo',
        due_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_jobs (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        payload_json TEXT NOT NULL DEFAULT '{}',
        result_json TEXT NOT NULL DEFAULT '{}',
        error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        run_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_approvals (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        job_id TEXT NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
        action TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        payload_json TEXT NOT NULL DEFAULT '{}',
        decision_note TEXT,
        created_at TEXT NOT NULL,
        decided_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_activity (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        job_id TEXT REFERENCES runtime_jobs(id) ON DELETE SET NULL,
        approval_id TEXT REFERENCES runtime_approvals(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_leases (
        id TEXT PRIMARY KEY,
        user_id_hash TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL,
        provider_runtime_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'starting',
        capabilities_json TEXT NOT NULL DEFAULT '[]',
        endpoint TEXT,
        started_at TEXT,
        expires_at TEXT,
        last_seen_at TEXT,
        stopped_at TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS goals (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        progress INTEGER NOT NULL DEFAULT 0,
        due_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        title TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'document',
        content TEXT NOT NULL DEFAULT '',
        uri TEXT,
        status TEXT NOT NULL DEFAULT 'draft',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
        filename TEXT NOT NULL,
        media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        storage_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS connectors (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        endpoint TEXT,
        capabilities_json TEXT NOT NULL DEFAULT '[]',
        config_json TEXT NOT NULL DEFAULT '{}',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS connector_secrets (
        connector_id TEXT PRIMARY KEY REFERENCES connectors(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        ciphertext TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'local',
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'info',
        level TEXT NOT NULL DEFAULT 'info',
        action_url TEXT,
        read_at TEXT,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    # Operator telemetry used by the admin console.
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        job_number TEXT,
        nickname TEXT,
        email TEXT,
        phone_number TEXT,
        department_name TEXT,
        department_path TEXT,
        job_title TEXT,
        avatar_url TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        last_login_at TEXT,
        login_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS client_devices (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        client TEXT NOT NULL,
        client_version TEXT,
        platform TEXT,
        user_agent TEXT,
        last_ip TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        request_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS request_log (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        method TEXT NOT NULL,
        route TEXT NOT NULL,
        status INTEGER NOT NULL,
        duration_ms INTEGER NOT NULL,
        client TEXT,
        ip TEXT,
        error TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_stats_hourly (
        hour TEXT NOT NULL,
        method TEXT NOT NULL,
        route TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        total_ms INTEGER NOT NULL DEFAULT 0,
        max_ms INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (hour, method, route)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_heartbeats (
        id TEXT PRIMARY KEY,
        host TEXT NOT NULL,
        pid INTEGER NOT NULL,
        ppid INTEGER,
        release TEXT,
        started_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        rss_mb REAL NOT NULL DEFAULT 0,
        cpu_seconds REAL NOT NULL DEFAULT 0,
        threads INTEGER NOT NULL DEFAULT 0,
        in_flight INTEGER NOT NULL DEFAULT 0,
        active_streams INTEGER NOT NULL DEFAULT 0,
        requests_total INTEGER NOT NULL DEFAULT 0,
        errors_total INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_audit (
        id TEXT PRIMARY KEY,
        admin_user_id TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT '',
        ip TEXT,
        created_at TEXT NOT NULL
    )
    """,
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_messages_session_created ON messages(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_widgets_message ON widgets(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_widgets_user_session ON widgets(user_id, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_jobs_due ON runtime_jobs(status, run_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_approvals_status ON runtime_approvals(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_activity_created ON runtime_activity(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_leases_status ON runtime_leases(status, last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_goals_user_status ON goals(user_id, status, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_user_updated ON artifacts(user_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_files_user_created ON files(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_files_session_created ON files(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_connectors_user_enabled ON connectors(user_id, enabled, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, read_at, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_client_devices_user ON client_devices(user_id, last_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_request_log_created ON request_log(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit(created_at)",
)

# _ensure_owner_columns() from the pre-Alembic startup path.  These ALTERs are
# needed when upgrading a database whose original tables predate tenant scope.
OWNER_TABLES = (
    "sessions",
    "messages",
    "memories",
    "tasks",
    "runtime_jobs",
    "runtime_approvals",
    "runtime_activity",
    "goals",
    "artifacts",
    "files",
    "connectors",
    "notifications",
)


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)
    for table in OWNER_TABLES:
        op.execute(
            "ALTER TABLE {} ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT 'local'".format(table)
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_{}_user_id ON {}(user_id)".format(table, table)
        )
    for statement in INDEXES:
        op.execute(statement)


def downgrade() -> None:
    # Baseline is intentionally non-destructive.  Production data may predate
    # Alembic, so rolling back the revision must not drop application tables.
    pass
