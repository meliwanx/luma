"""Record which login provider owns a user and keep optional profile data."""

from alembic import op

revision = "0019_auth_providers"
down_revision = "0018_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider TEXT")
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS profile JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    # Rows that already have a local password belong to the password provider.
    # Profiles with an empty hash stay unclaimed so another provider can adopt them.
    op.execute(
        "UPDATE users SET auth_provider = 'password' "
        "WHERE auth_provider IS NULL AND password_hash IS NOT NULL AND btrim(password_hash) <> ''"
    )
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'users_auth_provider_check' AND conrelid = 'users'::regclass
            ) THEN
                ALTER TABLE users ADD CONSTRAINT users_auth_provider_check
                    CHECK (auth_provider IS NULL OR auth_provider IN ('password', 'sso'));
            END IF;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_auth_provider_check")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS profile")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS auth_provider")
