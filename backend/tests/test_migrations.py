"""Migration graph and second-wave data migration coverage."""

from __future__ import annotations

import os
import importlib
import uuid
import unittest
from pathlib import Path

import psycopg2
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from tests import pg


class MigrationTests(unittest.TestCase):
    """Exercise the complete graph in a disposable PostgreSQL schema."""

    def _make_schema(self) -> str:
        schema = "mig_{}".format(uuid.uuid4().hex[:12])
        connection = psycopg2.connect(**pg._connection_settings())
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute('CREATE SCHEMA "{}"'.format(schema))
        finally:
            connection.close()
        return schema

    def _drop_schema(self, schema: str) -> None:
        connection = psycopg2.connect(**pg._connection_settings())
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute('DROP SCHEMA IF EXISTS "{}" CASCADE'.format(schema))
        finally:
            connection.close()

    def test_second_wave_merges_to_one_head_and_marks_overdue_tasks(self):
        schema = self._make_schema()
        engine = None
        try:
            settings = pg._connection_settings()
            query = {
                "options": "-c search_path={}".format(schema),
                "sslmode": os.getenv("DB_SSLMODE", "prefer"),
            }
            url = URL.create(
                "postgresql+psycopg2",
                username=settings["user"],
                password=settings["password"],
                host=settings["host"],
                port=settings["port"],
                database=settings["dbname"],
                query=query,
            )
            engine = create_engine(url, poolclass=NullPool, future=True)
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.set_main_option(
                "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
            )

            with engine.connect() as connection:
                config.attributes["connection"] = connection
                # Start from an empty schema, then seed rows before the
                # scheduler branch is applied.
                command.upgrade(config, "0003_integrity")
                connection.commit()
                connection.execute(text(
                    "INSERT INTO users(user_id,nickname,email,first_seen_at,last_seen_at) VALUES "
                    "('legacy-alpha','Legacy Alpha','SHARED@example.com','2020-01-01','2020-01-01'),"
                    "('legacy-beta','Legacy Beta','shared@example.com','2020-01-02','2020-01-02')"
                ))
                connection.execute(text("ALTER TABLE tasks ADD COLUMN reminded_at TEXT"))
                connection.execute(
                    text(
                        "INSERT INTO tasks "
                        "(id,user_id,title,description,status,due_at,created_at,updated_at,metadata_json,reminded_at) "
                        "VALUES (:id,:user_id,:title,'',:status,:due_at,:created_at,:updated_at,'{}',:reminded_at)"
                    ),
                    [
                        {
                            "id": "migration-overdue-todo",
                            "user_id": "migration-user",
                            "title": "overdue todo",
                            "status": "todo",
                            "due_at": "2020-01-01T00:00:00Z",
                            "created_at": "2020-01-01T00:00:00Z",
                            "updated_at": "2020-01-01T00:00:00Z",
                            "reminded_at": None,
                        },
                        {
                            "id": "migration-overdue-progress",
                            "user_id": "migration-user",
                            "title": "overdue progress",
                            "status": "in_progress",
                            "due_at": "2020-01-01T00:00:00Z",
                            "created_at": "2020-01-01T00:00:00Z",
                            "updated_at": "2020-01-01T00:00:00Z",
                            "reminded_at": None,
                        },
                        {
                            "id": "migration-done",
                            "user_id": "migration-user",
                            "title": "done",
                            "status": "done",
                            "due_at": "2020-01-01T00:00:00Z",
                            "created_at": "2020-01-01T00:00:00Z",
                            "updated_at": "2020-01-01T00:00:00Z",
                            "reminded_at": None,
                        },
                        {
                            "id": "migration-completed",
                            "user_id": "migration-user",
                            "title": "completed",
                            "status": "completed",
                            "due_at": "2020-01-01T00:00:00Z",
                            "created_at": "2020-01-01T00:00:00Z",
                            "updated_at": "2020-01-01T00:00:00Z",
                            "reminded_at": None,
                        },
                        {
                            "id": "migration-cancelled",
                            "user_id": "migration-user",
                            "title": "cancelled",
                            "status": "cancelled",
                            "due_at": "2020-01-01T00:00:00Z",
                            "created_at": "2020-01-01T00:00:00Z",
                            "updated_at": "2020-01-01T00:00:00Z",
                            "reminded_at": None,
                        },
                        {
                            "id": "migration-future",
                            "user_id": "migration-user",
                            "title": "future",
                            "status": "todo",
                            "due_at": "2999-01-01T00:00:00Z",
                            "created_at": "2020-01-01T00:00:00Z",
                            "updated_at": "2020-01-01T00:00:00Z",
                            "reminded_at": None,
                        },
                        {
                            "id": "migration-malformed",
                            "user_id": "migration-user",
                            "title": "malformed",
                            "status": "todo",
                            "due_at": "not-a-date",
                            "created_at": "2020-01-01T00:00:00Z",
                            "updated_at": "2020-01-01T00:00:00Z",
                            "reminded_at": None,
                        },
                        {
                            "id": "migration-premarked",
                            "user_id": "migration-user",
                            "title": "premarked",
                            "status": "todo",
                            "due_at": "2020-01-01T00:00:00Z",
                            "created_at": "2020-01-01T00:00:00Z",
                            "updated_at": "2020-01-01T00:00:00Z",
                            "reminded_at": "already-reminded",
                        },
                    ],
                )
                connection.execute(
                    text(
                        "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES "
                        "('migration-main-old','session-migration-user','old','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z'),"
                        "('migration-main-new','session-migration-user','new','2020-01-02T00:00:00Z','2020-01-02T00:00:00Z'),"
                        "('migration-empty','session-migration-user','empty','2020-01-03T00:00:00Z','2020-01-03T00:00:00Z'),"
                        "('migration-file','session-migration-user','file','2020-01-04T00:00:00Z','2020-01-04T00:00:00Z')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES "
                        "('migration-message-old','session-migration-user','migration-main-old','user','old','2020-01-01T00:00:01Z','{}'),"
                        "('migration-message-new','session-migration-user','migration-main-new','user','new','2020-02-01T00:00:01Z','{}')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO files(id,user_id,session_id,filename,media_type,size_bytes,sha256,storage_key,created_at,updated_at) "
                        "VALUES ('migration-file-ref','session-migration-user','migration-file','keep.txt','text/plain',1,'hash','migration-file-ref','2020-01-04T00:00:00Z','2020-01-04T00:00:00Z')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO runtime_leases "
                        "(id,user_id_hash,provider,provider_runtime_id,capabilities_json) "
                        "VALUES (:id,:user_hash,'test','test-runtime',:caps)"
                    ),
                    [
                        {"id": "legacy-code", "user_hash": "legacy-code-user", "caps": '["code"]'},
                        {"id": "legacy-browser", "user_hash": "legacy-browser-user", "caps": '["browser"]'},
                        {"id": "legacy-aio", "user_hash": "legacy-aio-user", "caps": '["code","browser"]'},
                        {"id": "legacy-empty", "user_hash": "legacy-empty-user", "caps": "[]"},
                    ],
                )
                connection.execute(text(
                    "INSERT INTO files(id,user_id,session_id,filename,media_type,size_bytes,sha256,storage_key,created_at,updated_at) "
                    "VALUES (:id,:user_id,:session_id,:filename,:media_type,:size,:sha256,:key,:timestamp,:timestamp)"
                ), [
                    {"id": "migration-screenshot", "user_id": "session-migration-user", "session_id": "migration-file",
                     "filename": "browser-screenshot.png", "media_type": "image/png", "size": 1,
                     "sha256": "hash", "key": "migration-screenshot", "timestamp": "2020-01-04T00:00:00Z"},
                    {"id": "migration-upload", "user_id": "session-migration-user", "session_id": "migration-file",
                     "filename": "notes.md", "media_type": "text/markdown", "size": 1,
                     "sha256": "hash", "key": "migration-upload", "timestamp": "2020-01-04T00:00:00Z"},
                ])
                connection.commit()

                # The singular target is required to remain valid once the
                # independent branches have been merged.
                command.upgrade(config, "head")
                connection.commit()

                accounts = connection.execute(text(
                    "SELECT user_id,username,email,display_name,role,status,created_at,password_hash,session_version "
                    "FROM users ORDER BY user_id"
                )).fetchall()
                self.assertEqual(len(accounts), 2)
                self.assertEqual(accounts[0][2], "shared@example.com")
                self.assertIsNone(accounts[1][2])
                for account in accounts:
                    self.assertTrue(account[1].startswith("legacy_"))
                    self.assertEqual(len(account[1]), 31)
                    self.assertEqual(account[4:6], ("user", "active"))
                    self.assertIsNotNone(account[6])
                    self.assertIsNone(account[7])
                    self.assertEqual(account[8], 0)
                rows = connection.execute(
                    text(
                        "SELECT id,due_at,status,reminded_at FROM tasks "
                        "WHERE id LIKE :prefix ORDER BY id"
                    ),
                    {"prefix": "migration-%"},
                ).fetchall()
                by_id = {row[0]: row for row in rows}
                self.assertIsNotNone(by_id["migration-overdue-todo"][3])
                self.assertIsNotNone(by_id["migration-overdue-progress"][3])
                for task_id in (
                    "migration-done",
                    "migration-completed",
                    "migration-cancelled",
                    "migration-future",
                    "migration-malformed",
                ):
                    self.assertIsNone(by_id[task_id][3], task_id)
                self.assertEqual(by_id["migration-premarked"][3], "already-reminded")
                self.assertEqual(by_id["migration-overdue-todo"][1], "2020-01-01T00:00:00+00:00")

                session_rows = connection.execute(
                    text(
                        "SELECT id,kind FROM sessions WHERE user_id = 'session-migration-user' ORDER BY id"
                    )
                ).fetchall()
                self.assertEqual(
                    {row[0]: row[1] for row in session_rows},
                    {
                        "migration-main-new": "main",
                        "migration-main-old": "side",
                        "migration-file": "side",
                    },
                )
                lease_rows = connection.execute(
                    text("SELECT id,capability,provider_runtime_id FROM runtime_leases ORDER BY id")
                ).fetchall()
                self.assertEqual(
                    {row[0]: row[1] for row in lease_rows},
                    {"legacy-code": "code", "legacy-browser": "browser", "legacy-aio": "aio", "legacy-empty": "code"},
                )
                self.assertTrue(all(row[2] == "test-runtime" for row in lease_rows))
                # The old single-user unique constraint is replaced: the
                # browser row can coexist with that same user's code row.
                connection.execute(text(
                    "INSERT INTO runtime_leases "
                    "(id,user_id_hash,capability,provider,provider_runtime_id,capabilities_json) "
                    "VALUES ('split-browser','legacy-code-user','browser','test','browser-runtime','[\"browser\"]')"
                ))
                connection.commit()
                self.assertEqual(connection.execute(text(
                    "SELECT COUNT(*) FROM runtime_leases WHERE user_id_hash = 'legacy-code-user'"
                )).scalar(), 2)
                constraints = connection.execute(text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'runtime_leases'::regclass AND contype = 'u'"
                )).fetchall()
                self.assertEqual([row[0] for row in constraints], ["UNIQUE (user_id_hash, capability)"])

                columns = connection.execute(text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :table"
                ), {"table": "model_calls"}).fetchall()
                self.assertEqual({row[0] for row in columns}, {
                    "id", "user_id", "session_id", "message_id", "purpose", "model", "stream",
                    "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
                    "estimated", "first_token_ms", "duration_ms", "tokens_per_sec", "status", "error_type",
                    "tool_calls_count", "created_at",
                })
                self.assertEqual(dict(columns)["created_at"], "timestamp with time zone")
                indexes = connection.execute(text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = :table"
                ), {"table": "model_calls"}).fetchall()
                self.assertTrue({
                    "idx_model_calls_user_created", "idx_model_calls_created", "idx_model_calls_model_created", "idx_model_calls_message",
                }.issubset({row[0] for row in indexes}))

                library_rows = connection.execute(text(
                    "SELECT id,origin,title,pinned,last_opened_at,message_id FROM files WHERE id IN (:image,:upload)"
                ), {"image": "migration-screenshot", "upload": "migration-upload"}).fetchall()
                self.assertEqual({row[0]: tuple(row[1:]) for row in library_rows}, {
                    "migration-screenshot": ("browser_screenshot", "browser-screenshot.png", False, None, None),
                    "migration-upload": ("upload", "notes.md", False, None, None),
                })
                connection.execute(text(
                    "UPDATE files SET origin=:origin,title=:title,pinned=:pinned WHERE id=:id"
                ), {"origin": "generated", "title": "保留自定义标题", "pinned": True, "id": "migration-screenshot"})
                migration = importlib.import_module("migrations.versions.0015_library_ideas")
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                    migration.upgrade()
                connection.commit()
                self.assertEqual(tuple(connection.execute(text(
                    "SELECT origin,title,pinned FROM files WHERE id=:id"
                ), {"id": "migration-screenshot"}).fetchone()), ("generated", "保留自定义标题", True))
                for purpose in ("ideas", "proactive", "feed"):
                    connection.execute(text(
                        "INSERT INTO model_calls(id,user_id,purpose,model,status) VALUES (:id,:user_id,:purpose,:model,:status)"
                    ), {"id": "migration-" + purpose, "user_id": "session-migration-user", "purpose": purpose, "model": "fixture", "status": "ok"})
                connection.commit()

                # Re-running heads is a no-op and leaves exactly one version
                # row at the current migration head.
                command.upgrade(config, "heads")
                connection.commit()
                versions = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
                self.assertEqual([row[0] for row in versions], ["0018_accounts"])

            script = ScriptDirectory.from_config(config)
            self.assertEqual(script.get_heads(), ["0018_accounts"])
            self.assertEqual(script.get_revision("0016_proactive_feed").down_revision, "0014_model_calls")
            self.assertEqual(set(script.get_revision("0017_merge_features").down_revision), {"0015_library_ideas", "0016_proactive_feed"})
            for revision in ("0004_stream", "0005_memory", "0006_scheduler", "0007_files"):
                self.assertEqual(script.get_revision(revision).down_revision, "0003_integrity")
        finally:
            if engine is not None:
                engine.dispose()
            self._drop_schema(schema)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
