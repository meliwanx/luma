"""Repair widget references and add deferred integrity checks."""
from typing import Sequence, Union

from alembic import op

revision: str = "0003_integrity"
down_revision: Union[str, None] = "0002_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CHECKS = (
    (
        "messages",
        "messages_role_check",
        "CHECK (role IN ('system', 'user', 'assistant', 'tool')) NOT VALID",
    ),
    (
        "memories",
        "memories_importance_check",
        "CHECK (importance BETWEEN 1 AND 5) NOT VALID",
    ),
    (
        "goals",
        "goals_progress_check",
        "CHECK (progress BETWEEN 0 AND 100) NOT VALID",
    ),
    (
        "files",
        "files_size_bytes_check",
        "CHECK (size_bytes >= 0) NOT VALID",
    ),
)


def _add_check(table: str, name: str, expression: str) -> None:
    # The catalog check makes this safe when a deployment has already added a
    # constraint manually under the same name.
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('{table}') IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM pg_constraint
                   WHERE conname = '{name}'
                     AND conrelid = '{table}'::regclass
               ) THEN
                ALTER TABLE {table} ADD CONSTRAINT {name} {expression};
            END IF;
        END $$;
        """.format(table=table, name=name, expression=expression)
    )


def _add_cascade_fk(column: str, target: str, name: str) -> None:
    # Existing pre-Alembic schemas had no widget foreign keys.  Check by name
    # so running this migration against a partially repaired schema is safe.
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('widgets') IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM pg_constraint
                   WHERE conname = '{name}'
                     AND conrelid = 'widgets'::regclass
               ) THEN
                ALTER TABLE widgets
                    ADD CONSTRAINT {name}
                    FOREIGN KEY ({column}) REFERENCES {target}(id) ON DELETE CASCADE;
            END IF;
        END $$;
        """.format(name=name, column=column, target=target)
    )


def upgrade() -> None:
    # Remove rows that would prevent the new cascading session reference from
    # being created.  Also clean widgets whose message was already removed.
    op.execute(
        """
        DELETE FROM widgets AS w
        WHERE NOT EXISTS (SELECT 1 FROM messages AS m WHERE m.id = w.message_id)
           OR NOT EXISTS (SELECT 1 FROM sessions AS s WHERE s.id = w.session_id)
        """
    )
    # Widgets are extracted before the assistant message is committed by the
    # current persistence path, so session_id is the enforceable ownership
    # boundary here.  Deleting a session cascades to its messages and widgets.
    _add_cascade_fk("session_id", "sessions", "fk_widgets_session")
    for table, name, expression in CHECKS:
        _add_check(table, name, expression)


def downgrade() -> None:
    # Integrity constraints are intentionally retained on downgrade.  They are
    # part of the live schema contract and protect existing workers.
    pass
