"""Late account telemetry must not recreate erased data or block other users."""

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests import pg
from starlette.requests import Request

from app import telemetry
from app.db import get_connection


class AccountTelemetryTests(unittest.TestCase):
    def setUp(self):
        pg.reset_tables()
        self.timestamp = "2026-10-07T12:00:00+00:00"
        # Keep this regression's batches independent of measurements queued
        # by another test's detached work, and restore those buffers afterward.
        for name, value in (
            ("_requests", []), ("_stats", {}), ("_devices", {}),
            ("_users", {}), ("_user_seen", {}), ("_model_calls", []),
            ("_counters", {"in_flight": 0, "active_streams": 0, "requests_total": 0, "errors_total": 0}),
        ):
            mocked = patch.object(telemetry, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        with get_connection() as conn:
            for user_id, username in (("erased-account", "erased_account"), ("live-account", "live_account")):
                conn.execute(
                    "INSERT INTO users(user_id,username,display_name,created_at,first_seen_at,last_seen_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (user_id, username, username, self.timestamp, self.timestamp, self.timestamp),
                )

    def record_request(self, user_id):
        request = Request({
            "type": "http", "method": "POST", "path": "/api/v1/memories",
            "raw_path": b"/api/v1/memories", "query_string": b"",
            "headers": [(b"x-luma-client", b"web"), (b"user-agent", b"telemetry-fixture")],
            "client": ("203.0.113.9", 10000), "server": ("testserver", 80),
            "scheme": "https",
        })
        request.state.luma_user = {"user_id": user_id}
        telemetry.request_started()
        telemetry.request_finished(request, 200, 25)

    def record_model_call(self, call_id, user_id):
        telemetry.record_model_call({
            "id": call_id, "user_id": user_id, "session_id": None, "message_id": None,
            "purpose": "summary", "model": "fixture-model", "stream": False,
            "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
            "cached_tokens": None, "reasoning_tokens": None, "estimated": False,
            "first_token_ms": None, "duration_ms": 20, "tokens_per_sec": 150,
            "status": "ok", "error_type": None, "tool_calls_count": 0,
            "created_at": self.timestamp,
        })

    def test_erased_owner_is_dropped_while_surviving_batch_commits(self):
        # Requests and device activity can be queued before erasure; a model
        # callback can finish after the account and its original rows are gone.
        self.record_request("erased-account")
        self.record_request("live-account")
        with get_connection() as conn:
            conn.execute("DELETE FROM users WHERE user_id = ?", ("erased-account",))
        self.record_model_call("erased-measurement", "erased-account")
        self.record_model_call("live-measurement", "live-account")

        with patch.object(telemetry.logger, "exception") as failure:
            telemetry.flush()
            failure.assert_not_called()

        with get_connection() as conn:
            for table in ("users", "request_log", "client_devices", "model_calls"):
                erased = conn.execute(
                    "SELECT COUNT(*) AS count FROM " + table + " WHERE user_id = ?", ("erased-account",)
                ).fetchone()
                survivor = conn.execute(
                    "SELECT COUNT(*) AS count FROM " + table + " WHERE user_id = ?", ("live-account",)
                ).fetchone()
                self.assertEqual(erased["count"], 0, table)
                self.assertEqual(survivor["count"], 1, table)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM deleted_account_ids").fetchone()["count"], 1)
        with telemetry._lock:
            self.assertEqual(telemetry._model_calls, [])

            self.assertEqual(telemetry._requests, [])
            self.assertEqual(telemetry._devices, {})
            self.assertEqual(telemetry._users, {})

        # An erased measurement must not remain on the retry queue and poison
        # subsequent batches belonging to an account that is still present.
        self.record_model_call("live-measurement-next", "live-account")
        with patch.object(telemetry.logger, "exception") as failure:
            telemetry.flush()
            failure.assert_not_called()
        with get_connection() as conn:
            calls = conn.execute("SELECT id FROM model_calls ORDER BY id").fetchall()
        self.assertEqual([row["id"] for row in calls], ["live-measurement", "live-measurement-next"])
        with telemetry._lock:
            self.assertEqual(telemetry._model_calls, [])

    def test_content_updates_can_finish_while_model_flush_waits_for_message(self):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                ("concurrent-session", "live-account", "Fixture", self.timestamp, self.timestamp),
            )
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("concurrent-message", "live-account", "concurrent-session", "assistant", "initial", self.timestamp,
                 '{"model":"fixture-model"}', "streaming"),
            )
        self.record_model_call("concurrent-call", "live-account")
        with telemetry._lock:
            measurement = dict(telemetry._model_calls.pop())
        measurement.update({"session_id": "concurrent-session", "message_id": "concurrent-message"})
        writer_has_row = threading.Event()
        flush_awaiting_message = threading.Event()

        class FlushConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, params=()):
                if sql.startswith("SELECT id FROM messages") and "FOR UPDATE" in sql:
                    # The model INSERT has taken the owner advisory lock.
                    # Let the writer finish while this worker requests its row.
                    flush_awaiting_message.set()
                return self.connection.execute(sql, params)

        def write_content():
            with get_connection() as conn:
                conn.execute("UPDATE messages SET content = ? WHERE id = ? AND user_id = ?",
                             ("partial", "concurrent-message", "live-account"))
                writer_has_row.set()
                self.assertTrue(flush_awaiting_message.wait(timeout=5), "flush never reached the message lock")
                # Ordinary content writes must not wait on the owner advisory
                # lock held by the flush that needs this transaction's row.
                conn.execute("UPDATE messages SET content = ?, status = ? WHERE id = ? AND user_id = ?",
                             ("finished", "complete", "concurrent-message", "live-account"))

        def flush_measurement():
            with get_connection() as conn:
                self.assertTrue(writer_has_row.wait(timeout=5), "writer never acquired the message row")
                telemetry._flush_model_calls(FlushConnection(conn), [measurement])

        # Exactly two database connections reproduce the worker interaction.
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = [workers.submit(write_content), workers.submit(flush_measurement)]
            for future in pending:
                future.result(timeout=10)
        with get_connection() as conn:
            message = conn.execute("SELECT content,status,metadata_json FROM messages WHERE id = ?",
                                   ("concurrent-message",)).fetchone()
            count = conn.execute("SELECT COUNT(*) AS count FROM model_calls WHERE id = ?",
                                 ("concurrent-call",)).fetchone()["count"]
        metadata = json.loads(message["metadata_json"])
        self.assertEqual((message["content"], message["status"]), ("finished", "complete"))
        self.assertEqual(metadata["model"], "fixture-model")
        self.assertEqual(metadata["usage"]["calls"], 1)
        self.assertEqual(metadata["usage"]["total_tokens"], 10)
        self.assertEqual(count, 1)
