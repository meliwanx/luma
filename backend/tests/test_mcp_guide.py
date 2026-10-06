"""Connector guides are read on demand and remembered within one conversation."""

import asyncio
import json
import os
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet

from tests.pg import reset_tables

from app import mcp
from app.agent import policy, tools
from app.db import get_connection
from app.mcp import MCPClient


_TIMESTAMP = "2026-10-06T00:00:00+00:00"
_INSTRUCTIONS = "先读取语义能力，再读取表结构，最后提供 semantic_plan 查询。"
_HINT = "提示：可先调用 luma.connectors.guide('Sample') 查看该服务的调用说明"
_INFO = {"connector_id": "connector", "connector": "Sample", "tool": "query_sample"}


class MCPGuideTests(unittest.TestCase):
    def setUp(self):
        reset_tables()
        secrets_key = patch.dict(os.environ, {"LUMA_SECRETS_KEY": Fernet.generate_key().decode("ascii")})
        secrets_key.start()
        self.addCleanup(secrets_key.stop)
        self.endpoint = "https://example.com/mcp"
        self.headers = {"Authorization": "Bearer fixture-guide-secret", "X-Key": "fixture-guide-key"}
        self.ciphertext = mcp.encrypt_headers(self.headers)
        self.metadata = {
            "tools": [{"name": "query_sample", "enabled": True}, {"name": "reset_sample", "enabled": False}],
            "server": {"name": "fixture"},
            "synced_at": _TIMESTAMP,
            "instructions": _INSTRUCTIONS,
        }
        self._insert_connector()

    def _insert_connector(self, connector_id="connector", user_id="local", name="Sample", metadata=None):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO connectors(id,user_id,name,kind,endpoint,capabilities_json,enabled,created_at,updated_at,metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (connector_id, user_id, name, "mcp", self.endpoint, '["query_sample"]', 1,
                 _TIMESTAMP, _TIMESTAMP, json.dumps(self.metadata if metadata is None else metadata)),
            )
            conn.execute(
                "INSERT INTO connector_secrets(connector_id,user_id,ciphertext,updated_at) VALUES (?,?,?,?)",
                (connector_id, user_id, self.ciphertext, _TIMESTAMP),
            )

    def _metadata(self, connector_id="connector"):
        with get_connection() as conn:
            row = conn.execute("SELECT metadata_json FROM connectors WHERE id = ?", (connector_id,)).fetchone()
        return json.loads(row["metadata_json"])

    def _clear_instructions(self):
        metadata = dict(self.metadata)
        metadata.pop("instructions")
        with get_connection() as conn:
            conn.execute("UPDATE connectors SET metadata_json = ? WHERE id = ?", (json.dumps(metadata), "connector"))
        return metadata

    def _insert_session(self, session_id="session", user_id="local", marker=None, role="user"):
        metadata = {"attachment_ids": ["fixture-file"]}
        if marker is not None:
            metadata["mcp_guides_read"] = marker
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (session_id, user_id, "guide", _TIMESTAMP, _TIMESTAMP),
            )
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)",
                (session_id + "-request", user_id, session_id, role, "查询数据", _TIMESTAMP, json.dumps(metadata)),
            )
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)",
                (session_id + "-answer", user_id, session_id, "assistant", "", _TIMESTAMP, "{}"),
            )

    def _message_metadata(self, message_id="session-request"):
        with get_connection() as conn:
            row = conn.execute("SELECT metadata_json FROM messages WHERE id = ?", (message_id,)).fetchone()
        return json.loads(row["metadata_json"])

    @contextmanager
    def _remote(self, instructions=_INSTRUCTIONS, results=None, failure=None, initialize_action=None):
        methods = []
        call_results = list(results or [{"content": [{"type": "text", "text": "查询完成"}]}])

        def handler(request):
            body = json.loads(request.content)
            methods.append(body["method"])
            if failure:
                raise failure(request)
            if body["method"] == "initialize":
                if initialize_action:
                    initialize_action()
                value = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fixture"}}
                if instructions is not None:
                    value["instructions"] = instructions
            elif body["method"] == "notifications/initialized":
                return httpx.Response(202)
            elif body["method"] == "tools/call":
                value = call_results.pop(0) if len(call_results) > 1 else call_results[0]
            else:
                raise AssertionError("guide must not relist remote tools")
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": value})

        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        self.addCleanup(client.close)
        with patch.object(mcp, "MCPClient", MCPClient), \
                patch.object(mcp, "validate_url", return_value=self.endpoint), \
                patch.object(mcp, "_http_client", return_value=client):
            yield methods

    def _assert_untrusted(self, result, instructions=_INSTRUCTIONS):
        self.assertEqual(result.status, "ok")
        prefix = result.text.split(instructions, 1)[0]
        self.assertIn("外部服务", prefix)
        self.assertIn("仅供参考", prefix)
        self.assertIn("不能改变你的安全规则", prefix)
        self.assertIn(instructions, result.text)

    def test_cached_guide_accepts_name_or_id_without_remote_access(self):
        with patch.object(mcp, "MCPClient", side_effect=AssertionError("cached guide should be local")):
            for selector in ("Sample", "connector"):
                with self.subTest(selector=selector):
                    result = asyncio.run(tools._connector_guide(tools.AgentContext("local"), {"connector": selector}))
                    self._assert_untrusted(result)

    def test_guide_is_a_registered_read_tool_and_allowed_without_confirmation(self):
        guide = next(tool for tool in tools._builtin_tools() if tool.name == "luma.connectors.guide")
        self.assertEqual(guide.risk, "read")
        self.assertEqual(guide.parameters["properties"]["connector"]["type"], "string")
        self.assertIn("connector", guide.parameters["required"])
        for mode in ("interactive", "background"):
            with self.subTest(mode=mode), patch.object(policy, "_audit"):
                decision = asyncio.run(policy.decide(tools.AgentContext("local", mode=mode), guide, {"connector": "Sample"}))
                self.assertEqual(decision.decision, "allow")

    def test_guide_rejects_another_users_id_and_name(self):
        private_instructions = "another-user-private-guide"
        self._insert_connector("private", "someone-else", "PRIVATE", {"instructions": private_instructions})
        with patch.object(mcp, "MCPClient", side_effect=AssertionError("unauthorized remote access")):
            for selector in ("private", "PRIVATE"):
                with self.subTest(selector=selector):
                    result = asyncio.run(tools._connector_guide(tools.AgentContext("local"), {"connector": selector}))
                    self.assertEqual(result.status, "error")
                    self.assertNotIn(private_instructions, result.text)

    def test_duplicate_names_require_id_while_another_users_name_is_ignored(self):
        self._insert_connector("foreign-sample", "someone-else", "Sample")
        result = asyncio.run(tools._connector_guide(tools.AgentContext("local"), {"connector": "Sample"}))
        self._assert_untrusted(result)
        self._insert_connector("duplicate")
        result = asyncio.run(tools._connector_guide(tools.AgentContext("local"), {"connector": "Sample"}))
        self.assertEqual(result.status, "error")
        self.assertIn("ID", result.text)
        result = asyncio.run(tools._connector_guide(tools.AgentContext("local"), {"connector": "duplicate"}))
        self._assert_untrusted(result)

    def test_missing_guide_initializes_and_backfills_without_relisting(self):
        metadata = self._clear_instructions()
        raw = _INSTRUCTIONS + " " + self.headers["Authorization"] + " " + self.headers["X-Key"]
        with self._remote(instructions=raw) as methods:
            result = asyncio.run(tools._connector_guide(tools.AgentContext("local"), {"connector": "Sample"}))
        self._assert_untrusted(result, _INSTRUCTIONS + " *** ***")
        stored = self._metadata()
        self.assertEqual(stored["instructions"], _INSTRUCTIONS + " *** ***")
        self.assertTrue(stored["instructions_synced_at"])
        self.assertEqual(stored["tools"], metadata["tools"])
        self.assertEqual(stored["synced_at"], metadata["synced_at"])
        self.assertEqual(methods, ["initialize", "notifications/initialized"])
        for secret in self.headers.values():
            self.assertNotIn(secret, result.text)

    def test_guide_and_subsequent_tool_share_initialized_client(self):
        self._clear_instructions()
        ctx = tools.AgentContext("local")

        async def execute():
            guide = await tools._connector_guide(ctx, {"connector": "connector"})
            query = await tools._mcp_executor(ctx, {}, _INFO)
            repeated = await tools._connector_guide(ctx, {"connector": "Sample"})
            return guide, query, repeated

        with self._remote() as methods:
            guide, query, repeated = asyncio.run(execute())
        self._assert_untrusted(guide)
        self._assert_untrusted(repeated)
        self.assertEqual(query.status, "ok")
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/call"])

    def test_tool_initialize_backfills_for_later_guide(self):
        self._clear_instructions()
        ctx = tools.AgentContext("local")

        async def execute():
            query = await tools._mcp_executor(ctx, {}, _INFO)
            guide = await tools._connector_guide(ctx, {"connector": "Sample"})
            return query, guide

        with self._remote() as methods:
            query, guide = asyncio.run(execute())
        self.assertEqual(query.status, "ok")
        self._assert_untrusted(guide)
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/call"])

    def test_concurrent_guide_and_tool_initialize_connector_only_once(self):
        self._clear_instructions()
        ctx = tools.AgentContext("local")

        async def execute():
            return await asyncio.gather(
                tools._connector_guide(ctx, {"connector": "Sample"}),
                tools._mcp_executor(ctx, {}, _INFO),
            )

        with self._remote() as methods:
            guide, query = asyncio.run(execute())
        self._assert_untrusted(guide)
        self.assertEqual(query.status, "ok")
        self.assertEqual(methods.count("initialize"), 1)
        self.assertEqual(methods.count("notifications/initialized"), 1)
        self.assertEqual(methods.count("tools/call"), 1)

    def test_server_without_instructions_returns_explicit_absence(self):
        self._clear_instructions()
        self._insert_session()
        ctx = tools.AgentContext("local", session_id="session", assistant_message_id="session-answer")
        with self._remote(instructions=None) as methods:
            result = asyncio.run(tools._connector_guide(ctx, {"connector": "Sample"}))
        self.assertEqual(result.status, "ok")
        self.assertIn("说明", result.text)
        self.assertIn("未提供", result.text)
        self.assertEqual(methods, ["initialize", "notifications/initialized"])
        self.assertNotIn("instructions", self._metadata())
        self.assertNotIn("mcp_guides_read", self._message_metadata())
        self.assertNotIn("connector", getattr(ctx, "mcp_guides_read", set()))

    def test_background_context_without_client_pool_can_initialize_guide(self):
        self._clear_instructions()
        ctx = SimpleNamespace(user_id="local", session_id=None, assistant_message_id=None, mode="background")
        with self._remote() as methods:
            result = asyncio.run(tools._connector_guide(ctx, {"connector": "Sample"}))
        self._assert_untrusted(result)
        self.assertEqual(methods, ["initialize", "notifications/initialized"])
        self.assertIn("connector", ctx.mcp_clients)

    def test_failed_backfill_evicts_client_and_same_context_retry_initializes_again(self):
        self._clear_instructions()
        self._insert_session()
        ctx = tools.AgentContext("local", session_id="session", assistant_message_id="session-answer")
        clients = []
        initialize_calls = []
        real_backfill = tools.backfill_mcp_instructions
        backfill_calls = []

        class Client:
            def __init__(self, endpoint, headers):
                self.instructions = ""
                self.closed = False
                clients.append(self)

            def initialize(self):
                initialize_calls.append(self)
                self.instructions = _INSTRUCTIONS

            def close(self):
                self.closed = True

        def backfill(*args, **kwargs):
            backfill_calls.append(args)
            if len(backfill_calls) == 1:
                raise RuntimeError("fixture-guide-key private backfill detail")
            return real_backfill(*args, **kwargs)

        async def execute():
            failed = await tools._connector_guide(ctx, {"connector": "Sample"})
            self.assertNotIn("connector", ctx.mcp_clients)
            self.assertNotIn("mcp_guides_read", self._message_metadata())
            self.assertTrue(clients[0].closed)
            retried = await tools._connector_guide(ctx, {"connector": "Sample"})
            return failed, retried

        with patch.object(mcp, "MCPClient", Client), \
                patch.object(tools, "backfill_mcp_instructions", side_effect=backfill), \
                self.assertLogs(tools.logger, level="WARNING") as logs:
            failed, retried = asyncio.run(execute())
        self.assertEqual(failed.status, "error")
        self.assertNotIn(_INSTRUCTIONS, failed.text)
        self._assert_untrusted(retried)
        self.assertEqual(len(clients), 2)
        self.assertEqual(len(initialize_calls), 2)
        self.assertIs(ctx.mcp_clients["connector"], clients[1])
        self.assertEqual(self._metadata()["instructions"], _INSTRUCTIONS)
        self.assertEqual(self._message_metadata()["mcp_guides_read"], ["connector"])
        for forbidden in ("fixture-guide-key", "private backfill detail"):
            self.assertNotIn(forbidden, " ".join(logs.output))
            self.assertNotIn(forbidden, failed.text)

    def test_initialize_snapshot_changes_never_return_old_guide_or_mark_read(self):
        metadata = self._clear_instructions()
        self._insert_session()

        def mutate(action):
            with get_connection() as conn:
                if action == "delete":
                    conn.execute("DELETE FROM connectors WHERE id = ?", ("connector",))
                    conn.execute("DELETE FROM connector_secrets WHERE connector_id = ?", ("connector",))
                elif action == "disable":
                    conn.execute("UPDATE connectors SET enabled = ? WHERE id = ?", (0, "connector"))
                elif action == "endpoint":
                    conn.execute("UPDATE connectors SET endpoint = ? WHERE id = ?", ("https://other.example.com/mcp", "connector"))
                elif action == "kind":
                    conn.execute("UPDATE connectors SET kind = ? WHERE id = ?", ("calendar", "connector"))
                else:
                    conn.execute("UPDATE connector_secrets SET ciphertext = ? WHERE connector_id = ?",
                                 (mcp.encrypt_headers({"X-Key": "fixture-rotated-guide-key"}), "connector"))

        for action in ("delete", "disable", "endpoint", "secret", "kind"):
            with self.subTest(action=action):
                with get_connection() as conn:
                    conn.execute("DELETE FROM connectors WHERE id = ?", ("connector",))
                    conn.execute("DELETE FROM connector_secrets WHERE connector_id = ?", ("connector",))
                self._insert_connector(metadata=metadata)
                ctx = tools.AgentContext("local", session_id="session", assistant_message_id="session-answer")
                with self._remote(instructions="旧快照说明", initialize_action=lambda: mutate(action)) as methods:
                    result = asyncio.run(tools._connector_guide(ctx, {"connector": "Sample"}))
                self.assertEqual(result.status, "error")
                self.assertNotIn("旧快照说明", result.text)
                self.assertNotIn("mcp_guides_read", self._message_metadata())
                self.assertNotIn("connector", getattr(ctx, "mcp_guides_read", set()))
                self.assertEqual(methods, ["initialize", "notifications/initialized"])
                if action != "delete":
                    self.assertNotIn("instructions", self._metadata())

    def test_only_first_error_per_connector_in_a_turn_suggests_guide(self):
        remote_error = {"isError": True, "content": [{"type": "text", "text": "需要 semantic_plan"}]}
        ctx = tools.AgentContext("local")

        async def execute():
            return [await tools._mcp_executor(ctx, {}, _INFO) for _ in range(2)]

        with self._remote(results=[remote_error]) as methods:
            first, second = asyncio.run(execute())
        self.assertEqual([first.status, second.status], ["error", "error"])
        self.assertTrue(first.text.endswith(_HINT))
        self.assertNotIn(_HINT, second.text)
        self.assertEqual(methods.count("initialize"), 1)

    def test_success_does_not_suggest_guide_or_make_later_error_the_first_call(self):
        remote_error = {"isError": True, "content": [{"type": "text", "text": "需要 semantic_plan"}]}
        ctx = tools.AgentContext("local")

        async def execute():
            return [await tools._mcp_executor(ctx, {}, _INFO) for _ in range(2)]

        with self._remote(results=[{"content": [{"type": "text", "text": "完成"}]}, remote_error]):
            first, second = asyncio.run(execute())
        self.assertEqual([first.status, second.status], ["ok", "error"])
        self.assertNotIn(_HINT, first.text)
        self.assertNotIn(_HINT, second.text)

    def test_read_guide_suppresses_error_hint_without_session(self):
        remote_error = {"isError": True, "content": [{"type": "text", "text": "需要 semantic_plan"}]}
        ctx = tools.AgentContext("local")

        async def execute():
            await tools._connector_guide(ctx, {"connector": "Sample"})
            return await tools._mcp_executor(ctx, {}, _INFO)

        with self._remote(results=[remote_error]):
            result = asyncio.run(execute())
        self.assertEqual(result.status, "error")
        self.assertNotIn(_HINT, result.text)

    def test_read_guide_persists_only_marker_and_suppresses_new_context_hint(self):
        self._insert_session()
        ctx = tools.AgentContext("local", session_id="session", assistant_message_id="session-answer")
        guide = asyncio.run(tools._connector_guide(ctx, {"connector": "Sample"}))
        self._assert_untrusted(guide)
        stored = self._message_metadata()
        self.assertEqual(stored["mcp_guides_read"], ["connector"])
        self.assertEqual(stored["attachment_ids"], ["fixture-file"])
        self.assertNotIn(_INSTRUCTIONS, json.dumps(stored))
        self.assertEqual(self._message_metadata("session-answer"), {})
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)",
                ("next-request", "local", "session", "user", "再次查询", "2026-10-06T00:00:01+00:00", "{}"),
            )
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)",
                ("next-answer", "local", "session", "assistant", "", "2026-10-06T00:00:01+00:00", "{}"),
            )
        next_ctx = tools.AgentContext("local", session_id="session", assistant_message_id="next-answer")
        remote_error = {"isError": True, "content": [{"type": "text", "text": "需要 semantic_plan"}]}
        with self._remote(results=[remote_error]):
            result = asyncio.run(tools._mcp_executor(next_ctx, {}, _INFO))
        self.assertEqual(result.status, "error")
        self.assertNotIn(_HINT, result.text)

    def test_foreign_assistant_id_cannot_write_marker_to_latest_user_message(self):
        self._insert_session()
        self._insert_session("other-session")
        self._insert_session("private-session", user_id="someone-else")
        for assistant_id in ("other-session-answer", "private-session-answer", "missing-answer"):
            with self.subTest(assistant_id=assistant_id):
                ctx = tools.AgentContext("local", session_id="session", assistant_message_id=assistant_id)
                result = asyncio.run(tools._connector_guide(ctx, {"connector": "Sample"}))
                self._assert_untrusted(result)
                for message_id in ("session-request", "other-session-request", "private-session-request"):
                    self.assertNotIn("mcp_guides_read", self._message_metadata(message_id))

    def test_concurrent_different_guides_merge_markers_on_same_user_message(self):
        self._insert_session()
        self._insert_connector("second-connector", name="SECOND")
        ctx = tools.AgentContext("local", session_id="session", assistant_message_id="session-answer")
        # Synchronize before opening either connection. The two workers then
        # contend for the same user-message lock using at most two pool slots.
        barrier = threading.Barrier(2)
        mark_guide_read = tools._mark_guide_read

        def mark(context, connector_id):
            barrier.wait(timeout=5)
            return mark_guide_read(context, connector_id)

        async def execute():
            return await asyncio.gather(
                tools._connector_guide(ctx, {"connector": "Sample"}),
                tools._connector_guide(ctx, {"connector": "SECOND"}),
            )

        with patch.object(tools, "_mark_guide_read", side_effect=mark):
            results = asyncio.run(execute())
        for result in results:
            self._assert_untrusted(result)
        stored = self._message_metadata()
        self.assertEqual(set(stored["mcp_guides_read"]), {"connector", "second-connector"})
        self.assertEqual(len(stored["mcp_guides_read"]), 2)
        self.assertEqual(stored["attachment_ids"], ["fixture-file"])
        self.assertNotIn(_INSTRUCTIONS, json.dumps(stored))
        self.assertEqual(self._message_metadata("session-answer"), {})

    def test_unread_guide_hint_reappears_in_new_turn_of_same_session(self):
        self._insert_session()
        remote_error = {"isError": True, "content": [{"type": "text", "text": "需要 semantic_plan"}]}
        with self._remote(results=[remote_error]):
            first = asyncio.run(tools._mcp_executor(tools.AgentContext("local", session_id="session"), {}, _INFO))
            second = asyncio.run(tools._mcp_executor(tools.AgentContext("local", session_id="session"), {}, _INFO))
        self.assertTrue(first.text.endswith(_HINT))
        self.assertTrue(second.text.endswith(_HINT))
        self.assertNotIn("mcp_guides_read", self._message_metadata())

    def test_guide_marker_is_not_shared_between_sessions(self):
        self._insert_session("read-session", marker=["connector"])
        self._insert_session("unread-session")
        remote_error = {"isError": True, "content": [{"type": "text", "text": "需要 semantic_plan"}]}
        with self._remote(results=[remote_error]):
            result = asyncio.run(tools._mcp_executor(tools.AgentContext("local", session_id="unread-session"), {}, _INFO))
        self.assertTrue(result.text.endswith(_HINT))

    def test_guide_marker_in_another_users_session_does_not_suppress_hint(self):
        self._insert_session("private-session", user_id="someone-else", marker=["connector"])
        remote_error = {"isError": True, "content": [{"type": "text", "text": "需要 semantic_plan"}]}
        with self._remote(results=[remote_error]):
            result = asyncio.run(tools._mcp_executor(tools.AgentContext("local", session_id="private-session"), {}, _INFO))
        self.assertTrue(result.text.endswith(_HINT))

    def test_non_user_message_marker_does_not_suppress_hint(self):
        self._insert_session("assistant-session", marker=["connector"], role="assistant")
        remote_error = {"isError": True, "content": [{"type": "text", "text": "需要 semantic_plan"}]}
        with self._remote(results=[remote_error]):
            result = asyncio.run(tools._mcp_executor(tools.AgentContext("local", session_id="assistant-session"), {}, _INFO))
        self.assertTrue(result.text.endswith(_HINT))

    def test_failed_guide_does_not_mark_session_and_transport_error_suggests_guide(self):
        self._clear_instructions()
        self._insert_session()
        ctx = tools.AgentContext("local", session_id="session", assistant_message_id="session-answer")

        async def execute():
            guide = await tools._connector_guide(ctx, {"connector": "Sample"})
            query = await tools._mcp_executor(ctx, {}, _INFO)
            repeated = await tools._mcp_executor(ctx, {}, _INFO)
            return guide, query, repeated

        with self._remote(failure=lambda request: httpx.ReadTimeout("fixture-guide-key private detail", request=request)):
            guide, query, repeated = asyncio.run(execute())
        self.assertEqual([guide.status, query.status, repeated.status], ["error", "error", "error"])
        self.assertTrue(query.text.endswith(_HINT))
        self.assertNotIn(_HINT, repeated.text)
        self.assertNotIn("mcp_guides_read", self._message_metadata())
        for result in (guide, query, repeated):
            self.assertNotIn("fixture-guide-key", result.text)
            self.assertNotIn("private detail", result.text)

    def test_long_remote_error_retains_hint_within_result_limit(self):
        remote_error = {"isError": True, "content": [{"type": "text", "text": "错" * 10000}]}
        with self._remote(results=[remote_error]), patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "4000"}):
            result = asyncio.run(tools._mcp_executor(tools.AgentContext("local"), {}, _INFO))
        self.assertEqual(result.status, "error")
        self.assertLessEqual(len(result.text), 4000)
        self.assertIn("已截断", result.text)
        self.assertTrue(result.text.endswith(_HINT))
