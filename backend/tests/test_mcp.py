import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from tests.pg import reset_tables

from cryptography.fernet import Fernet

os.environ.update({"LUMA_SECRETS_KEY": Fernet.generate_key().decode()})

from fastapi.testclient import TestClient
from app import main
from app import mcp
from app import widgets
from app.services import chat as chat_service
from app.services import capabilities
from app.db import get_connection
from mcp_mock import MockMCPServer


class MCPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = patch("app.deps.current_user_id", return_value="local")
        cls.identity.start()
        cls.addClassCleanup(cls.identity.stop)
        reset_tables()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                ("s", "local", "fixture", "2026-10-03T00:00:00+00:00", "2026-10-03T00:00:00+00:00"),
            )
        cls.server = MockMCPServer()
        try:
            cls.endpoint = cls.server.start()
            cls.live_server = True
        except RuntimeError:
            # Restricted CI sandboxes may disallow binding a loopback socket;
            # keep the API tests runnable with the same protocol fixture.
            cls.live_server = False
            cls.endpoint = "http://127.0.0.1:9"
            class FakeClient:
                def __init__(self, endpoint, headers): pass
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def close(self): pass
                def initialize(self): pass
                def list_tools(self): return ([{"name": "query_sample", "title": "查询示例记录", "description": "查询", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}, {"name": "reset_sample", "title": "重置示例记录", "description": "重置", "inputSchema": {"type": "object"}}], {"name": "mock", "version": "1"})
                def call_tool(self, name, arguments): return "已重置" if name == "reset_sample" else "record_id=" + str(arguments.get("record_id", "")) + " 示例用量 1.2GB"
            fake_client = patch.object(mcp, "MCPClient", FakeClient)
            fake_client.start()
            cls.addClassCleanup(fake_client.stop)
        # Protocol tests use one explicit loopback fixture. Production policy
        # has no environment flag that permits HTTP or private destinations.
        real_validate = mcp.validate_url
        real_addresses = mcp.allowed_addresses
        fixture = urlsplit(cls.endpoint)
        def fixture_validate(url):
            return url if url == cls.endpoint else real_validate(url)
        def fixture_addresses(host, port):
            if host == fixture.hostname and port == fixture.port:
                return [fixture.hostname]
            return real_addresses(host, port)
        for helper, implementation in (("validate_url", fixture_validate), ("allowed_addresses", fixture_addresses)):
            mocked = patch.object(mcp, helper, side_effect=implementation)
            mocked.start()
            cls.addClassCleanup(mocked.stop)
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        if getattr(cls, "live_server", False):
            cls.server.stop()

    def test_create_sync_and_secret_redaction(self):
        response = self.client.post("/api/v1/connectors/mcp", json={"config": json.dumps({"mcpServers": {"sample-data": {"url": self.endpoint, "headers": {"Authorization": "Bearer test-token"}}}})})
        self.assertEqual(response.status_code, 201, response.text)
        connector = response.json()["connectors"][0]
        self.assertEqual([tool["name"] for tool in connector["metadata"]["tools"]], ["query_sample", "reset_sample"])
        self.assertNotIn("test-token", response.text)
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM connectors").fetchone()
            secret = conn.execute("SELECT * FROM connector_secrets").fetchone()
        self.assertNotIn("test-token", json.dumps(dict(row)))
        self.assertNotEqual(secret["ciphertext"], "Bearer test-token")
        connector_id = connector["id"]
        patch = self.client.patch(f"/api/v1/connectors/{connector_id}/mcp", json={"tools": {"reset_sample": False}})
        self.assertEqual(patch.status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/connectors/{connector_id}/sync").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/connectors/{connector_id}").status_code, 204)
        with get_connection() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM connector_secrets WHERE connector_id = ?", (connector_id,)).fetchone())

    def test_rejections_and_redacted_user_message(self):
        private_status = self.client.post("/api/v1/connectors/mcp", json={"url": "http://127.0.0.1:1"}).status_code
        self.assertEqual(private_status, 422)
        self.assertEqual(self.client.post("/api/v1/connectors/mcp", json={"config": json.dumps({"x": {"command": "node", "args": []}})}).status_code, 422)
        sid = self.client.get("/api/v1/sessions").json()[0]["id"]
        self.client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "Bearer xxxxxxxxxxxxxxxxxxxx sk-fixture_secret_value"})
        messages = self.client.get(f"/api/v1/sessions/{sid}/messages").json()
        user = [item for item in messages if item["role"] == "user"][-1]
        self.assertNotIn("sk-fixture_secret_value", user["content"])
        self.assertNotIn("Bearer xxxxxxxxxxxxxxxxxxxx", user["content"])

    def _add_sample(self):
        response = self.client.post("/api/v1/connectors/mcp", json={"name": "sample", "url": self.endpoint, "headers": {"Authorization": "Bearer test-token"}})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["connectors"][0]["id"]

    def _stream(self, rounds, content="查一下"):
        """Drive the chat tool loop with scripted model rounds; returns (events, captured contexts)."""
        seen = []
        replies = [list(round_) for round_ in rounds]
        script = iter(replies)

        def fake(messages, tools=None):
            seen.append({"messages": [dict(item) for item in messages], "tools": tools})
            # The final answer uses a tools-disabled request, so it can stream
            # without leaking text from an unresolved tool-capable round.
            yield from replies[-1] if tools is None else next(script)

        original = chat_service.provider_stream_chat
        chat_service.provider_stream_chat = fake
        try:
            sid = self.client.post("/api/v1/sessions", json={"title": "mcp"}).json()["id"]
            text = self.client.post(f"/api/v1/sessions/{sid}/messages/stream", json={"content": content}).text
        finally:
            chat_service.provider_stream_chat = original
        events = []
        for block in text.split("\n\n"):
            lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
            if "event" in lines:
                events.append((lines["event"], json.loads(lines["data"])))
        return events, seen

    def _names(self, seen):
        return [tool["function"]["name"] for tool in seen[0]["tools"]]

    def _loaded_tool(self, connector_id, tool):
        capability_id = "mcp:" + connector_id + ":" + tool
        name = capabilities.load("local", [capability_id])["definitions"][0]["function"]["name"]
        load = [{"type": "tool_calls", "calls": [{"id": "load_capability", "name": "luma.capabilities.load", "arguments": json.dumps({"capability_ids": [capability_id]})}]}]
        return name, load

    def test_private_addresses_and_http_are_rejected(self):
        for url in ("http://example.com/mcp", "https://127.0.0.1/mcp", "https://10.1.2.3/mcp", "https://169.254.169.254/latest", "https://203.0.113.10/mcp", "https://user:pw@example.com/mcp", "https://[::1]/mcp"):
            response = self.client.post("/api/v1/connectors/mcp", json={"url": url, "headers": {"Authorization": "Bearer test-token"}})
            self.assertEqual(response.status_code, 422, url)
            self.assertNotIn("test-token", response.text)

    def test_private_address_rejected_when_blocked_hosts_unset(self):
        # The explicit deployment block-list is optional; built-in private,
        # loopback, link-local and metadata checks must remain active.
        with patch.dict(os.environ, {"MCP_BLOCKED_HOSTS": ""}, clear=False):
            with self.assertRaises(mcp.MCPError):
                mcp.validate_url("https://127.0.0.1/mcp")

    def test_configured_blocked_public_address_is_rejected(self):
        with patch.dict(os.environ, {"MCP_BLOCKED_HOSTS": "203.0.113.10"}, clear=False):
            with self.assertRaises(mcp.MCPError):
                mcp.validate_url("https://203.0.113.10/mcp")

    def test_legacy_unsafe_settings_cannot_allow_private_or_blocked_addresses(self):
        with patch.dict(os.environ, {"MCP_ALLOW_PRIVATE": "true", "MCP_TRUSTED_HOSTS": "example.org", "MCP_BLOCKED_HOSTS": "203.0.113.10"}), patch.object(
            mcp.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("203.0.113.10", 443))]
        ):
            for url in ("http://example.org/mcp", "https://127.0.0.1/mcp", "https://example.org/mcp"):
                with self.subTest(url=url), self.assertRaises(mcp.MCPError):
                    mcp.validate_url(url)

    def test_read_tool_loop_and_metadata(self):
        if not self.live_server:
            self.skipTest("needs loopback MCP server")
        connector_id = self._add_sample()
        try:
            name, load = self._loaded_tool(connector_id, "query_sample")

            def first():
                yield {"type": "text", "content": "我查一下。"}
                yield {"type": "tool_calls", "calls": [{"id": "call_1", "name": name, "arguments": json.dumps({"record_id": "record-001"})}]}

            def second():
                yield {"type": "text", "content": "记录显示用量为 1.2GB。"}

            events, seen = self._stream([load, first(), second()])
            self.assertNotIn(name, self._names(seen))
            self.assertIn(name, self._names(seen[1:]))
            tool_events = [data for kind, data in events if kind == "tool" and data["call_id"] == "call_1"]
            self.assertEqual([item["status"] for item in tool_events], ["running", "ok"])
            tool_message = [item for item in seen[2]["messages"] if item.get("role") == "tool" and item.get("tool_call_id") == "call_1"][0]
            self.assertTrue(tool_message["content"].startswith("以下是外部工具返回的数据"))
            self.assertIn("record_id=record-001 示例用量 1.2GB", tool_message["content"])
            done = [data for kind, data in events if kind == "done"][-1]
            self.assertEqual(done["content"], "记录显示用量为 1.2GB。")
            calls = done["metadata"]["tool_calls"]
            self.assertEqual([(item["tool"], item["status"]) for item in calls if item["tool"] == "query_sample"], [("query_sample", "ok")])
            self.assertNotIn("record-001", json.dumps(calls))
            self.assertNotIn("test-token", json.dumps(seen, ensure_ascii=False))
            # Disabled tools disappear from the model's tool list.
            self.client.patch(f"/api/v1/connectors/{connector_id}/mcp", json={"tools": {"query_sample": False}})
            _, seen = self._stream([iter([{"type": "text", "content": "好"}])])
            self.assertNotIn(name, self._names(seen))
        finally:
            self.client.delete(f"/api/v1/connectors/{connector_id}")

    def test_write_tool_requires_confirmation(self):
        if not self.live_server:
            self.skipTest("needs loopback MCP server")
        connector_id = self._add_sample()
        try:
            response = self.client.put("/api/v1/permissions/mcp:%s:reset_sample" % connector_id, json={"mode": "ask"})
            self.assertEqual(response.status_code, 200, response.text)
            name, load = self._loaded_tool(connector_id, "reset_sample")
            call = {"type": "tool_calls", "calls": [{"id": "call_w", "name": name, "arguments": json.dumps({"record_id": "record-001"})}]}
            before = self.server.counter.get("reset_sample", 0)
            events, _ = self._stream([load, iter([{"type": "text", "content": "需要重置。"}, call])], content="重置一下")
            self.assertEqual([data["status"] for kind, data in events if kind == "tool" and data["call_id"] == "call_w"], ["needs_confirmation"])
            done = [data for kind, data in events if kind == "done"][-1]
            self.assertEqual(self.server.counter.get("reset_sample", 0), before)
            widget = done["metadata"]["widgets"][0]
            self.assertEqual(widget["type"], "confirm")
            self.assertIn(f"[[widget:{widget['id']}]]", done["content"])
            self.assertNotIn("_pending", json.dumps(done))
            with self.assertRaises(Exception) as other:
                widgets.apply_event("someone-else", widget["id"], "confirm", None)
            self.assertEqual(getattr(other.exception, "status_code", None), 404)
            result = self.client.post(f"/api/v1/widgets/{widget['id']}/events", json={"action": "confirm"})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["widget"]["state"], {"status": "done"})
            self.assertTrue(result.json()["message"])
            self.assertEqual(self.server.counter.get("reset_sample", 0), before + 1)
            self.assertEqual(self.client.post(f"/api/v1/widgets/{widget['id']}/events", json={"action": "confirm"}).status_code, 409)
            self.assertEqual(self.server.counter.get("reset_sample", 0), before + 1)
            # History records expose the confirmation status, not raw output.
            described = widgets.describe_for_model(f"[[widget:{widget['id']}]]", {widget["id"]: widget})
            self.assertIn("已执行", described)
            self.assertNotIn("已重置", described)
            events, _ = self._stream([load, iter([call])], content="再重置一次")
            second = [data for kind, data in events if kind == "done"][-1]["metadata"]["widgets"][0]
            cancel = self.client.post(f"/api/v1/widgets/{second['id']}/events", json={"action": "cancel"})
            self.assertEqual(cancel.json()["widget"]["state"], {"status": "cancelled"})
            self.assertEqual(self.server.counter.get("reset_sample", 0), before + 1)
        finally:
            self.client.delete(f"/api/v1/connectors/{connector_id}")

    def test_loaded_write_confirms_even_with_default_always_permission(self):
        if not self.live_server:
            self.skipTest("needs loopback MCP server")
        connector_id = self._add_sample()
        try:
            name, load = self._loaded_tool(connector_id, "reset_sample")
            call = {"type": "tool_calls", "calls": [{"id": "call_direct", "name": name, "arguments": json.dumps({"record_id": "record-001"})}]}
            before = self.server.counter.get("reset_sample", 0)
            events, _ = self._stream([
                load,
                iter([{"type": "text", "content": "请求确认。"}, call]),
            ], content="重置一下")
            self.assertEqual([data["status"] for kind, data in events if kind == "tool" and data["call_id"] == "call_direct"], ["needs_confirmation"])
            done = [data for kind, data in events if kind == "done"][-1]
            self.assertTrue(done["metadata"]["waiting_confirmation"])
            self.assertEqual(done["metadata"]["widgets"][0]["type"], "confirm")
            self.assertEqual(self.server.counter.get("reset_sample", 0), before)
        finally:
            self.client.delete(f"/api/v1/connectors/{connector_id}")

    def test_widget_called_as_function_becomes_widget(self):
        if not self.live_server:
            self.skipTest("needs loopback MCP server")
        connector_id = self._add_sample()
        try:
            spec = {"type": "choice", "title": "选一个", "options": [{"label": "A"}, {"label": "B"}]}
            events, _ = self._stream([
                iter([{"type": "text", "content": "请选："}, {"type": "tool_calls", "calls": [{"id": "c", "name": "luma-ui", "arguments": json.dumps({"spec": spec})}]}]),
                iter([{"type": "text", "content": "请从组件中选择一个方案。"}]),
            ])
            done = [data for kind, data in events if kind == "done"][-1]
            self.assertEqual(done["metadata"]["widgets"][0]["type"], "choice")
            self.assertIn("请从组件中选择一个方案。", done["content"])
        finally:
            self.client.delete(f"/api/v1/connectors/{connector_id}")

    def test_pseudo_tool_call_markup(self):
        spec = '{"type": "choice", "title": "测试", "options": [{"label": "A"}, {"label": "B"}], "multiple": false}'
        raw = "来试试这个示例：<tool_call><function=luma-ui><parameter=spec>" + spec + "</parameter></function></tool_call>\n后面的话"
        content, found, errors = widgets.extract_widgets(raw, user_id="local", session_id="s", message_id="m1")
        self.assertEqual(len(found), 1)
        self.assertEqual(content, f"来试试这个示例：[[widget:{found[0]['id']}]]\n后面的话")
        fence = "先说一句\n```luma-ui\n" + spec + "\n```\n" + raw
        content, found, _ = widgets.extract_widgets(fence, user_id="local", session_id="s", message_id="m2")
        self.assertEqual(len(found), 1)
        self.assertEqual(content.count("[[widget:"), 1)
        for tag in ("<tool_call>", "</tool_call>", "<function", "</parameter>", "<parameter"):
            self.assertNotIn(tag, content)
        self.assertTrue(content.endswith("后面的话"))
        wrapped = '<function_calls><invoke name="luma-ui"><parameter name="spec">{"spec": ' + spec + '}</parameter></invoke></function_calls>'
        content, found, _ = widgets.extract_widgets(wrapped, user_id="local", session_id="s", message_id="m3")
        self.assertEqual((len(found), content), (1, f"[[widget:{found[0]['id']}]]"))
        # Ordinary prose that mentions XML tags is left alone.
        prose = "XML 里写 <parameter name=\"x\">{\"a\": 1}</parameter> 就行"
        self.assertEqual(widgets.extract_widgets(prose, user_id="local", session_id="s", message_id="m4")[0], prose)


class MCPToolClassificationTests(unittest.TestCase):
    def normalized(self, name, annotations=None):
        raw = {"name": name, "inputSchema": {"type": "object"}}
        if annotations is not None:
            raw["annotations"] = annotations
        return mcp.normalize_tools([raw])[0]

    def test_missing_annotations_use_read_name_constants(self):
        names = ["demo_%s_values" % prefix for prefix in mcp.MCP_READ_ONLY_NAME_PREFIXES]
        names.extend("demo_%s" % part for part in mcp.MCP_READ_ONLY_NAME_PARTS)
        names.extend(["get", "search", "demo_get_table_schema", "demo_search_metrics", "demo_search_table_values", "getMetrics"])
        for name in names:
            with self.subTest(name=name):
                info = self.normalized(name)
                self.assertTrue(info["read_only"])
                self.assertEqual(mcp.tool_risk(info), "read")
                self.assertEqual(mcp.tool_risk({"name": name, "read_only": False}), "read")

    def test_declared_annotations_disable_name_fallback(self):
        for annotations in ({"readOnlyHint": False}, {"destructiveHint": False}, {"title": "Search"}):
            with self.subTest(annotations=annotations):
                self.assertFalse(self.normalized("demo_get_values", annotations)["read_only"])
        self.assertTrue(self.normalized("demo_get_values", {})["read_only"])
        self.assertTrue(self.normalized("demo_propose_change", {"readOnlyHint": True})["read_only"])

    def test_high_risk_names_and_hints_override_read_only(self):
        for part in mcp.MCP_CONFIRM_NAME_PARTS:
            with self.subTest(part=part):
                info = self.normalized("demo_get_%s_items" % part, {"readOnlyHint": True})
                self.assertFalse(info["read_only"])
                self.assertTrue(mcp.tool_requires_confirmation(info))
        info = self.normalized("demo_review", {"readOnlyHint": True, "destructiveHint": True})
        self.assertFalse(info["read_only"])
        self.assertTrue(mcp.tool_requires_confirmation(info))
        self.assertTrue(info["annotations"]["destructiveHint"])

    def test_ordinary_writes_keep_external_write_risk(self):
        for name in ("demo_create_note", "demo_update_note", "demo_propose_action", "demo_reset"):
            info = self.normalized(name)
            self.assertEqual(mcp.tool_risk(info), "external_write")
            self.assertFalse(mcp.tool_requires_confirmation(info))

    def test_write_verbs_exclude_read_name_fallback(self):
        for name in ("demo_create_schema", "demo_update_query", "demo_set_my_profile"):
            with self.subTest(name=name):
                info = self.normalized(name)
                self.assertFalse(info["read_only"])
                self.assertEqual(mcp.tool_risk(info), "external_write")
                self.assertTrue(self.normalized(name, {"readOnlyHint": True})["read_only"])


if __name__ == "__main__":
    unittest.main()
