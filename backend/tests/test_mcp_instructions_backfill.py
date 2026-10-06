"""Existing connector instructions are filled by their normal initialize."""

import asyncio
import json
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet

from tests.pg import reset_tables

from app import mcp
from app.agent import tools
from app.db import get_connection
from app.mcp import MCPClient
from app.services import mcp_connectors
from app.services.mcp_connectors import backfill_mcp_instructions


class MCPInstructionsBackfillTests(unittest.TestCase):
    def setUp(self):
        reset_tables()
        secrets_key = patch.dict(os.environ, {"LUMA_SECRETS_KEY": Fernet.generate_key().decode("ascii")})
        secrets_key.start()
        self.addCleanup(secrets_key.stop)
        self.endpoint = "https://example.com/mcp"
        self.headers = {"Authorization": "Bearer fixture-instructions-secret", "X-Key": "fixture-private-header"}
        self.ciphertext = mcp.encrypt_headers(self.headers)
        self.metadata = {
            "tools": [{"name": "query_sample", "enabled": True}, {"name": "reset_sample", "enabled": False}],
            "synced_at": "2026-10-01T00:00:00+00:00",
            "server": {"name": "fixture"},
        }
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO connectors(id,user_id,name,kind,endpoint,capabilities_json,enabled,created_at,updated_at,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("connector", "local", "Sample", "mcp", self.endpoint, '["query_sample"]', 1,
                 "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00", json.dumps(self.metadata)),
            )
            conn.execute(
                "INSERT INTO connector_secrets(connector_id,user_id,ciphertext,updated_at) VALUES (?,?,?,?)",
                ("connector", "local", self.ciphertext, "2026-10-01T00:00:00+00:00"),
            )
        self.info = {"connector_id": "connector", "connector": "Sample", "tool": "query_sample"}

    def _row(self):
        with get_connection() as conn:
            return dict(conn.execute("SELECT * FROM connectors WHERE id = ?", ("connector",)).fetchone())

    def _metadata(self):
        return json.loads(self._row()["metadata_json"])

    def _backfill(self, instructions="先读取能力，再查询字段。", user_id="local", endpoint=None, ciphertext=None):
        return backfill_mcp_instructions(user_id, "connector", endpoint or self.endpoint,
                                         ciphertext or self.ciphertext, instructions, self.headers)

    def _execute(self, instructions, count=1, initialize_action=None):
        methods = []

        def handler(request):
            body = json.loads(request.content)
            methods.append(body["method"])
            if body["method"] == "initialize":
                if initialize_action:
                    initialize_action()
                result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fixture"},
                          "instructions": instructions}
            elif body["method"] == "notifications/initialized":
                return httpx.Response(202)
            elif body["method"] == "tools/call":
                result = {"content": [{"type": "text", "text": "查询完成"}]}
            else:
                raise AssertionError("unexpected remote synchronization")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(client.close)
        ctx = tools.AgentContext("local")

        async def execute():
            return [await tools._mcp_executor(ctx, {}, self.info) for _ in range(count)]

        with patch.object(mcp, "MCPClient", MCPClient), \
                patch.object(mcp, "validate_url", return_value=self.endpoint), \
                patch.object(mcp, "_http_client", return_value=client):
            results = asyncio.run(execute())
        return results, methods

    def test_executor_initialize_backfills_once_without_relisting_tools(self):
        raw = "先读能力。" + self.headers["Authorization"] + " " + self.headers["X-Key"]
        results, methods = self._execute(raw, count=2)
        self.assertEqual([result.status for result in results], ["ok", "ok"])
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/call", "tools/call"])
        metadata = self._metadata()
        self.assertEqual(metadata["instructions"], "先读能力。*** ***")
        self.assertTrue(metadata["instructions_synced_at"])
        self.assertEqual(metadata["synced_at"], self.metadata["synced_at"])
        self.assertEqual(metadata["tools"], self.metadata["tools"])
        self.assertEqual(self._row()["capabilities_json"], '["query_sample"]')

    def test_existing_instructions_are_not_overwritten(self):
        self.assertTrue(self._backfill("第一次说明"))
        metadata = self._metadata()
        self.assertFalse(self._backfill("替换说明"))
        self.assertEqual(self._metadata(), metadata)

    def test_concurrent_workers_only_fill_once(self):
        barrier = threading.Barrier(2)

        def fill(value):
            barrier.wait(timeout=5)
            return self._backfill(value)

        # Only two pool connections, matching the shared-instance limit.
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(fill, value) for value in ("说明甲", "说明乙")]
            written = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(written), 1)
        self.assertIn(self._metadata()["instructions"], ("说明甲", "说明乙"))
        self.assertEqual(self._metadata()["tools"], self.metadata["tools"])

    def test_delete_and_backfill_use_the_same_lock_order(self):
        connector_locked = threading.Event()
        delete_started = threading.Event()
        first_delete = []
        deadline = time.monotonic() + 10

        def remaining():
            return max(0.01, deadline - time.monotonic())

        class CoordinatedConnection:
            def __init__(self, connection, deleting):
                self.connection = connection
                self.deleting = deleting

            def execute(self, sql, params=()):
                if self.deleting and sql.startswith("DELETE FROM ") and not first_delete:
                    table = sql.split()[2]
                    first_delete.append(table)
                    if table == "connector_secrets":
                        # Reproduce the former inverse lock order: hold the
                        # secret before allowing backfill to request its lock.
                        cursor = self.connection.execute(sql, params)
                        delete_started.set()
                        return cursor
                    delete_started.set()
                cursor = self.connection.execute(sql, params)
                if not self.deleting and sql.startswith("SELECT * FROM connectors") and "FOR UPDATE" in sql:
                    connector_locked.set()
                    if not delete_started.wait(remaining()):
                        raise AssertionError("connector deletion did not start")
                return cursor

        @contextmanager
        def controlled_connection(deleting):
            with get_connection() as conn:
                conn.execute("SELECT set_config(?, ?, true)", ("statement_timeout", "5s"))
                yield CoordinatedConnection(conn, deleting)

        with patch.object(mcp_connectors, "get_connection", side_effect=lambda: controlled_connection(False)), \
                patch.object(tools, "get_connection", side_effect=lambda: controlled_connection(True)), \
                ThreadPoolExecutor(max_workers=2) as workers:
            fill = workers.submit(self._backfill)
            self.assertTrue(connector_locked.wait(remaining()))
            remove = workers.submit(lambda: asyncio.run(tools._connector_remove(
                tools.AgentContext("local"), {"connector_id": "connector"},
            )))
            self.assertTrue(fill.result(timeout=remaining()))
            removed = remove.result(timeout=remaining())
        self.assertEqual(first_delete[0], "connectors")
        self.assertEqual(removed.status, "ok")
        self.assertTrue(removed.data["removed"])
        with get_connection() as conn:
            self.assertIsNone(conn.execute("SELECT id FROM connectors WHERE id = ?", ("connector",)).fetchone())
            self.assertIsNone(conn.execute("SELECT connector_id FROM connector_secrets WHERE connector_id = ?", ("connector",)).fetchone())

    def test_wrong_owner_or_connection_snapshot_is_not_written(self):
        self.assertFalse(self._backfill(user_id="someone-else"))
        self.assertFalse(self._backfill(endpoint="https://other.example.com/mcp"))
        self.assertFalse(self._backfill(ciphertext="replaced-secret"))
        self.assertEqual(self._metadata(), self.metadata)

    def test_changed_or_deleted_connector_during_initialize_is_not_written(self):
        def rotate_key():
            with get_connection() as conn:
                conn.execute("UPDATE connector_secrets SET ciphertext = ? WHERE connector_id = ?",
                             (mcp.encrypt_headers({"X-Key": "replacement-header"}), "connector"))

        results, methods = self._execute("旧服务说明", initialize_action=rotate_key)
        self.assertEqual(results[0].status, "ok")
        self.assertEqual(methods.count("initialize"), 1)
        self.assertEqual(self._metadata(), self.metadata)
        for column, value in (("endpoint", "https://other.example.com/mcp"), ("kind", "calendar"), ("enabled", 0)):
            with self.subTest(column=column):
                with get_connection() as conn:
                    conn.execute("UPDATE connectors SET endpoint = ?, kind = ?, enabled = ? WHERE id = ?",
                                 (self.endpoint, "mcp", 1, "connector"))
                    conn.execute("UPDATE connector_secrets SET ciphertext = ? WHERE connector_id = ?",
                                 (self.ciphertext, "connector"))
                    if column == "endpoint":
                        conn.execute("UPDATE connectors SET endpoint = ? WHERE id = ?", (value, "connector"))
                    elif column == "kind":
                        conn.execute("UPDATE connectors SET kind = ? WHERE id = ?", (value, "connector"))
                    else:
                        conn.execute("UPDATE connectors SET enabled = ? WHERE id = ?", (value, "connector"))
                self.assertFalse(self._backfill())
                self.assertEqual(self._metadata(), self.metadata)
        with get_connection() as conn:
            conn.execute("DELETE FROM connectors WHERE id = ?", ("connector",))
        self.assertFalse(self._backfill())

    def test_backfill_redacts_before_truncation(self):
        secret = "fixture-long-private-header-" + "abcdef" * 100
        headers = {"X-Key": secret}
        raw = "字" * 7900 + secret + "尾" * 2000
        self.assertTrue(backfill_mcp_instructions("local", "connector", self.endpoint,
                                                 self.ciphertext, raw, headers))
        instructions = self._metadata()["instructions"]
        self.assertLessEqual(len(instructions), mcp.MCP_INSTRUCTIONS_MAX_CHARS)
        self.assertIn("***", instructions)
        self.assertNotIn("fixture-long-private-header", instructions)
        self.assertIn("截断", instructions)

    def test_absent_or_invalid_instructions_do_not_write_metadata(self):
        for instructions in (None, "", " \n", {"instructions": "unexpected object"}):
            with self.subTest(instructions=instructions):
                self.assertFalse(self._backfill(instructions))
                self.assertEqual(self._metadata(), self.metadata)

    def test_backfill_failure_does_not_fail_tool_or_log_remote_contents(self):
        with patch.object(tools, "backfill_mcp_instructions", side_effect=RuntimeError(self.headers["X-Key"])), \
                self.assertLogs(tools.logger, level="WARNING") as logs:
            results, methods = self._execute("远端说明", count=2)
        self.assertEqual([result.status for result in results], ["ok", "ok"])
        self.assertEqual(methods.count("initialize"), 1)
        self.assertEqual(self._metadata(), self.metadata)
        self.assertIn("RuntimeError", " ".join(logs.output))
        self.assertNotIn(self.headers["X-Key"], " ".join(logs.output))
