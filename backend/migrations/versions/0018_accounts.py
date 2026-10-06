"""Add independent accounts without changing existing ownership identifiers."""

from alembic import op

revision = "0018_accounts"
down_revision = "0017_merge_features"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy profile columns stay nullable for upgrades; application identity
    # and authorization only read the independent account fields below.
    for column in (
        "username TEXT",
        "password_hash TEXT",
        "display_name TEXT",
        "role TEXT NOT NULL DEFAULT 'user'",
        "status TEXT NOT NULL DEFAULT 'active'",
        "created_at TEXT",
        "password_changed_at TEXT",
        "session_version BIGINT NOT NULL DEFAULT 0",
    ):
        op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS " + column)
    op.execute("UPDATE users SET username = 'legacy_' || substr(md5(user_id), 1, 24) WHERE username IS NULL")
    op.execute("UPDATE users SET display_name = COALESCE(display_name, nickname, username), created_at = COALESCE(created_at, first_seen_at)")
    op.execute("UPDATE users SET email = NULLIF(lower(btrim(email)), '')")
    # Older profiles may share an email; keep one deterministic owner so the
    # unique constraint can be introduced without discarding account rows.
    op.execute("""
        UPDATE users SET email = NULL WHERE user_id IN (
            SELECT user_id FROM (
                SELECT user_id, row_number() OVER (PARTITION BY email ORDER BY user_id) AS duplicate
                FROM users WHERE email IS NOT NULL
            ) ranked WHERE duplicate > 1
        )
    """)
    op.execute("ALTER TABLE users ALTER COLUMN username SET NOT NULL")
    op.execute("ALTER TABLE users ALTER COLUMN created_at SET NOT NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_unique ON users(lower(username))")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(lower(email)) WHERE email IS NOT NULL")
    for name, expression in (
        ("users_username_check", "username ~ '^[a-zA-Z0-9_.-]{3,32}$'"),
        ("users_role_check", "role IN ('user', 'admin')"),
        ("users_status_check", "status IN ('active', 'disabled')"),
        ("users_email_lower_check", "email IS NULL OR email = lower(email)"),
    ):
        op.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}' AND conrelid = 'users'::regclass) THEN
                    ALTER TABLE users ADD CONSTRAINT {name} CHECK ({expression});
                END IF;
            END $$
        """.format(name=name, expression=expression))

    # A delete trigger preserves historical business owners that do not yet
    # have an account row, while ensuring account deletion cascades at the DB
    # boundary too. Identifiers below are fixed migration-owned identifiers.
    tables = (
        "connector_secrets", "chat_secrets", "idea_feedback", "ideas_generation_state",
        "ideas", "feed_posts", "proactive_state", "proactive_prefs", "tool_permissions",
        "push_devices", "routines", "notifications", "connectors", "files", "artifacts",
        "goals", "runtime_activity", "runtime_approvals", "runtime_jobs", "widgets",
        "messages", "memories", "tasks", "sessions", "client_devices", "request_log", "model_calls",
    )
    # Detached jobs can outlive account deletion. Retain only an irreversible
    # identifier digest, then serialize owner writes with deletion so those
    # jobs cannot recreate deleted data. Historical orphan owners remain valid.
    op.execute("CREATE TABLE IF NOT EXISTS deleted_account_ids(user_id_hash TEXT PRIMARY KEY)")
    op.execute("""
        CREATE OR REPLACE FUNCTION guard_account_data() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE owner TEXT;
        BEGIN
            owner := to_jsonb(NEW) ->> COALESCE(TG_ARGV[0], 'user_id');
            PERFORM pg_advisory_xact_lock(hashtext('luma-account-data'), hashtext(substr(encode(sha256(convert_to(owner, 'UTF8')), 'hex'), 1, 32)));
            IF EXISTS (SELECT 1 FROM deleted_account_ids WHERE user_id_hash = encode(sha256(convert_to(owner, 'UTF8')), 'hex')) THEN
                RAISE EXCEPTION 'account no longer exists' USING ERRCODE = '23503';
            END IF;
            RETURN NEW;
        END $$
    """)
    # Content updates cannot recreate erased rows and must not acquire an
    # owner lock after a row lock, which would invert telemetry lock ordering.
    for table in tables:
        op.execute("DROP TRIGGER IF EXISTS guard_account_owner ON " + table)
        op.execute("CREATE TRIGGER guard_account_owner BEFORE INSERT OR UPDATE OF user_id ON " + table + " FOR EACH ROW EXECUTE FUNCTION guard_account_data()")
    op.execute("DROP TRIGGER IF EXISTS guard_account_owner ON admin_audit")
    op.execute("CREATE TRIGGER guard_account_owner BEFORE INSERT OR UPDATE OF admin_user_id ON admin_audit FOR EACH ROW EXECUTE FUNCTION guard_account_data('admin_user_id')")
    op.execute("""
        CREATE OR REPLACE FUNCTION guard_account_runtime() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtext('luma-account-data'), hashtext(NEW.user_id_hash));
            IF EXISTS (SELECT 1 FROM deleted_account_ids WHERE substr(user_id_hash, 1, 32) = NEW.user_id_hash) THEN
                RAISE EXCEPTION 'account no longer exists' USING ERRCODE = '23503';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("DROP TRIGGER IF EXISTS guard_account_runtime ON runtime_leases")
    op.execute("CREATE TRIGGER guard_account_runtime BEFORE INSERT OR UPDATE OF user_id_hash ON runtime_leases FOR EACH ROW EXECUTE FUNCTION guard_account_runtime()")
    deletes = "\n".join("DELETE FROM " + table + " WHERE user_id = OLD.user_id;" for table in tables)
    op.execute("""
        CREATE OR REPLACE FUNCTION delete_account_data() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtext('luma-account-data'), hashtext(substr(encode(sha256(convert_to(OLD.user_id, 'UTF8')), 'hex'), 1, 32)));
            INSERT INTO deleted_account_ids(user_id_hash) VALUES (encode(sha256(convert_to(OLD.user_id, 'UTF8')), 'hex')) ON CONFLICT DO NOTHING;
    """ + deletes + """
            DELETE FROM admin_audit WHERE admin_user_id = OLD.user_id;
            DELETE FROM runtime_leases WHERE user_id_hash = substr(encode(sha256(convert_to(OLD.user_id, 'UTF8')), 'hex'), 1, 32);
            RETURN OLD;
        END $$
    """)
    op.execute("DROP TRIGGER IF EXISTS users_delete_data ON users")
    op.execute("CREATE TRIGGER users_delete_data AFTER DELETE ON users FOR EACH ROW EXECUTE FUNCTION delete_account_data()")


def downgrade() -> None:
    # Account hashes and ownership must survive rollback.
    op.execute("DROP TRIGGER IF EXISTS users_delete_data ON users")
    op.execute("DROP FUNCTION IF EXISTS delete_account_data()")
    # Keep tombstones and write guards to protect already-deleted data.
