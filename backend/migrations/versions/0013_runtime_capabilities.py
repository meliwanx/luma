"""Split runtime leases by capability while preserving existing sandboxes."""

from typing import Sequence, Union

from alembic import op


revision: str = "0013_runtime_capabilities"
down_revision: Union[str, None] = "0012_chat_secrets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE runtime_leases ADD COLUMN capability TEXT NOT NULL DEFAULT 'code'")
    # Old releases persisted a JSON string, including legacy empty arrays.
    # Inspect its quoted entries without casting: malformed legacy values
    # still migrate to the safe code default instead of aborting the upgrade.
    op.execute(
        """UPDATE runtime_leases SET capability = CASE
            WHEN position('"browser"' in capabilities_json) > 0
                 AND position('"code"' in capabilities_json) > 0 THEN 'aio'
            WHEN position('"browser"' in capabilities_json) > 0 THEN 'browser'
            ELSE 'code' END"""
    )
    op.execute("ALTER TABLE runtime_leases DROP CONSTRAINT runtime_leases_user_id_hash_key")
    op.execute(
        "ALTER TABLE runtime_leases ADD CONSTRAINT runtime_leases_user_capability_key "
        "UNIQUE (user_id_hash, capability)"
    )
    op.execute(
        "ALTER TABLE runtime_leases ADD CONSTRAINT runtime_leases_capability_check "
        "CHECK (capability IN ('code', 'browser', 'aio'))"
    )


def downgrade() -> None:
    # A downgrade cannot safely discard a user's second live sandbox or its
    # workspace backup. Refuse until operators have explicitly consolidated
    # those leases rather than silently dropping provider resources.
    op.execute(
        """DO $$ BEGIN
            IF EXISTS (SELECT user_id_hash FROM runtime_leases GROUP BY user_id_hash HAVING COUNT(*) > 1) THEN
                RAISE EXCEPTION 'Consolidate runtime leases before downgrading capability support';
            END IF;
        END $$"""
    )
    op.execute("ALTER TABLE runtime_leases DROP CONSTRAINT runtime_leases_user_capability_key")
    op.execute("ALTER TABLE runtime_leases DROP CONSTRAINT runtime_leases_capability_check")
    op.execute("ALTER TABLE runtime_leases ADD CONSTRAINT runtime_leases_user_id_hash_key UNIQUE (user_id_hash)")
    op.execute("ALTER TABLE runtime_leases DROP COLUMN capability")
