"""Chat keeps secrets encrypted and lets the model choose connector tools."""

import asyncio
import copy
import io
import json
import logging
import os
import re
import socket
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.pg import reset_tables

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import main, mcp
from app.agent import policy
from app.agent import tools as agent_tools
from app.agent.tools import AgentContext
from app.db import get_connection
from app.services import chat as chat_service
from app.services import generation, mcp_catalog, mcp_connectors, secret_vault


_REFERENCE = re.compile(r"\{\{secret:sec_[a-zA-Z0-9]{16}\}\}")


class MCPChatToolsTests(unittest.TestCase):
    def setUp(self):
        self.identity = patch("app.deps.current_user_id", return_value="local")
        self.identity.start()
        self.addCleanup(self.identity.stop)
        reset_tables()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                ("mcp-chat-session", "local", "MCP 配置", "2026-10-04T00:00:00+00:00", "2026-10-04T00:00:00+00:00"),
            )
        self.ctx = AgentContext("local", session_id="mcp-chat-session")
        self.headers = {"Authorization": "Bearer chat-private-token-987654", "X-Custom-Auth": "tiny-pass"}
        self.endpoint = "https://example.com/private/path?mode=testing"
        self.contexts = []
        self.logs = io.StringIO()
        logger = logging.getLogger()
        handler = logging.StreamHandler(self.logs)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self._patch(patch.dict(os.environ, {
            "MCP_BLOCKED_HOSTS": "203.0.113.10",
            "LUMA_SECRETS_KEY": Fernet.generate_key().decode("ascii"), "MEMORY_AUTO_EXTRACT": "false",
        }))
        self._patch(patch.object(mcp.socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
        ]))
        self.sync = self._patch(patch.object(mcp_connectors, "mcp_sync", side_effect=self._sync))
        self._patch(patch.object(chat_service, "provider_complete", side_effect=self._complete))
        self._patch(patch.object(chat_service, "provider_stream_chat", self._chat_stream))
        self._patch(patch.object(chat_service, "provider_local_mode", return_value=False))
        self._patch(patch("app.services.chat.schedule_memory_extraction"))
        self._patch(patch("app.routers.chat.schedule_memory_extraction"))
        # Every lifespan must stop its runtime/scheduler before the next test
        # truncates their tables. Patches remain active until shutdown finishes.
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def tearDown(self):
        # A terminal SSE event precedes generation's final provider cleanup.
        # Await the actual task, so the next TRUNCATE cannot race its writes.
        async def drain():
            tasks = list(generation.manager.tasks.values())
            if tasks:
                await asyncio.gather(*tasks)

        self.client.portal.call(drain)

    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    @staticmethod
    def _sync(endpoint, headers, *, redaction_headers=None):
        return {
            "tools": [
                {"name": "query_sample", "title": "查询示例记录", "description": "查询", "input_schema": {"type": "object"}, "read_only": True, "enabled": True},
                {"name": "reset_sample", "title": "重置示例记录", "description": "重置", "input_schema": {"type": "object"}, "read_only": False, "enabled": True},
            ],
            "header_hints": mcp.header_hints(headers), "server": {"name": "fixture", "version": "1"},
            "status": "ok", "last_error": None, "synced_at": "2026-10-04T00:00:00+00:00",
        }

    def _complete(self, messages):
        self.contexts.append(copy.deepcopy(messages))
        return "收到配置了，你希望我连接这个服务吗？"

    def _chat_stream(self, messages, tools=None):
        self.contexts.append(copy.deepcopy(messages))
        yield {"type": "text", "content": "收到配置了，你希望我连接这个服务吗？"}

    def _config(self, endpoint=None, headers=None, name="sample-data", indent=None):
        return json.dumps({"mcpServers": {name: {
            "type": "http", "url": endpoint or self.endpoint,
            "headers": self.headers if headers is None else headers,
        }}}, ensure_ascii=False, indent=indent)

    def _post(self, content, stream=False, metadata=None):
        path = "/api/v1/sessions/mcp-chat-session/messages" + ("/stream" if stream else "")
        response = self.client.post(path, json={"content": content, "metadata": metadata or {}})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def _store(self, content, user_id="local", session_id="mcp-chat-session", role="user"):
        safe = secret_vault.protect_message(content, user_id, session_id) if role == "user" else content
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)",
                ("msg_" + uuid.uuid4().hex, user_id, session_id, role, safe, datetime.now(timezone.utc).isoformat(), "{}"),
            )
        return safe

    def _tool(self, suffix):
        return next(item for item in agent_tools._builtin_tools() if item.name == "luma.connectors." + suffix)

    def _call(self, suffix, args, ctx=None):
        return asyncio.run(self._tool(suffix).executor(ctx or self.ctx, args))

    def _add(self, name=None):
        safe = self._store(self._config())
        args = {"config": safe}
        if name is not None:
            args["name"] = name
        result = self._call("add_mcp", args)
        self.assertEqual(result.status, "ok", result.text)
        return result.data["connectors"][0]

    @staticmethod
    def _events(body):
        events = []
        for block in body.split("\n\n"):
            lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
            if "event" in lines:
                events.append((lines["event"], json.loads(lines["data"])))
        return events

    def _assert_safe(self, *values, headers=None):
        secrets = list((self.headers if headers is None else headers).values())
        secrets.extend(value.split(" ", 1)[1] for value in secrets[:] if value.startswith("Bearer "))
        for value in values:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            for secret in secrets:
                self.assertNotIn(secret, text)

    def _assert_chat_safe(self, response):
        with get_connection() as conn:
            messages = [dict(row) for row in conn.execute("SELECT * FROM messages WHERE session_id = ?", ("mcp-chat-session",))]
            connectors = [dict(row) for row in conn.execute("SELECT * FROM connectors WHERE user_id = ?", ("local",))]
        exported = self.client.get("/api/v1/export")
        self.assertEqual(exported.status_code, 200, exported.text)
        self._assert_safe(response.text, messages, connectors, self.contexts, exported.text, self.logs.getvalue())
        return messages

    def test_headers_are_replaced_before_storage_and_format_is_preserved(self):
        original = self._config(indent=2)
        response = self._post(original, metadata={"draft": original})
        message = response.json()
        config = json.loads(message["content"])
        server = config["mcpServers"]["sample-data"]
        self.assertEqual(server["url"], self.endpoint)
        self.assertEqual(server["type"], "http")
        restored = message["content"]
        for key, value in self.headers.items():
            self.assertRegex(server["headers"][key], r"^\{\{secret:sec_[a-zA-Z0-9]{16}\}\}$")
            restored = restored.replace(json.dumps(server["headers"][key]), json.dumps(value))
        self.assertEqual(restored, original)
        self.assertNotIn("mcp_intake", message["metadata"])
        self.assertNotIn("draft", message["metadata"])
        messages = self._assert_chat_safe(response)
        self.assertEqual(next(item["content"] for item in messages if item["role"] == "user"), message["content"])
        self.sync.assert_not_called()
        with get_connection() as conn:
            rows = conn.execute("SELECT id,ciphertext,created_at,expires_at FROM chat_secrets").fetchall()
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM connectors").fetchone()["n"], 0)
        self.assertEqual(len(rows), len(self.headers))
        self._assert_safe([dict(row) for row in rows])
        for row in rows:
            created = datetime.fromisoformat(str(row["created_at"]))
            expires = datetime.fromisoformat(str(row["expires_at"]))
            self.assertEqual(expires - created, timedelta(days=7))

    def test_whole_fenced_and_embedded_json_preserve_surrounding_text(self):
        config = self._config(indent=2)
        for prefix, suffix in (("", ""), ("```json\n", "\n```"), ("帮我把这个配上：\n", "\n连好后告诉我")):
            with self.subTest(prefix=prefix):
                content = secret_vault.protect_message(prefix + config + suffix, "local", "mcp-chat-session")
                self.assertTrue(content.startswith(prefix))
                self.assertTrue(content.endswith(suffix))
                self.assertIn(self.endpoint, content)
                self.assertEqual(len(_REFERENCE.findall(content)), 2)
                self._assert_safe(content)
        self.sync.assert_not_called()

    def test_normal_json_is_unchanged(self):
        for content in ('{"city":"上海","count":2}', '{"url":"https://example.com"}', "```json\n{\"items\":[1,2]}\n```"):
            with self.subTest(content=content):
                self.assertEqual(secret_vault.protect_message(content, "local", "mcp-chat-session"), content)
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM chat_secrets").fetchone()["n"], 0)

    def test_common_secret_keys_and_env_values_are_encrypted(self):
        for key in ("authorization", "token", "apiKey", "api_key", "password", "secret"):
            with self.subTest(key=key):
                original = json.dumps({"value": {key: "private-value", "label": "keep"}})
                safe = secret_vault.protect_message(original, "local", "mcp-chat-session")
                self.assertNotIn("private-value", safe)
                self.assertIn("keep", safe)
                self.assertEqual(secret_vault.resolve_secrets(json.loads(safe), "local"), json.loads(original))
        original = {"env": {"API_ACCESS": "private-env", "PASSWORD": "private-password"}}
        safe = secret_vault.protect_message(json.dumps(original), "local", "mcp-chat-session")
        self.assertNotIn("private-env", safe)
        self.assertNotIn("private-password", safe)
        self.assertEqual(secret_vault.resolve_secrets(json.loads(safe), "local"), original)

    def test_provider_messages_never_receive_tokens(self):
        response = self._post(self._config())
        self.assertTrue(self.contexts)
        self.assertTrue(any("{{secret:" in item.get("content", "") for item in self.contexts[0]))
        self._assert_chat_safe(response)
        self.sync.assert_not_called()

    def test_short_secrets_quotes_backslashes_and_field_names_preserve_json(self):
        for token in ("a", "headers", "Authorization", 'quote"slash\\tail'):
            with self.subTest(kind="escaped" if len(token) > 13 else token):
                config = {"url": self.endpoint, "headers": {"Authorization": token}, "name": "keep"}
                safe = secret_vault.protect_message(json.dumps(config, indent=2), "local", "mcp-chat-session")
                parsed = json.loads(safe)
                self.assertEqual(set(parsed), {"url", "headers", "name"})
                self.assertEqual(set(parsed["headers"]), {"Authorization"})
                self.assertRegex(parsed["headers"]["Authorization"], _REFERENCE)
                self.assertEqual(parsed["url"], self.endpoint)
                self.assertEqual(secret_vault.resolve_secrets(parsed, "local"), config)
                self.assertEqual(json.loads(secret_vault.resolve_secrets(safe, "local")), config)

    def test_arbitrary_headers_values_are_vaulted_as_whole_values(self):
        original = {"headers": {"nested": {"secret": "inside", "normal": 1}, "list": ["one", "two"], "flag": True}}
        safe = secret_vault.protect_message(json.dumps(original), "local", "mcp-chat-session")
        parsed = json.loads(safe)
        self.assertTrue(all(_REFERENCE.fullmatch(value) for value in parsed["headers"].values()))
        self.assertEqual(secret_vault.resolve_secrets(parsed, "local"), original)

    def test_truncated_secret_json_fails_closed(self):
        broken = self._config()[:-2]
        response = self._post("```json\n" + broken)
        self.assertNotIn("{{secret:", response.json()["content"])
        self.assertIn("请重新粘贴完整配置", response.json()["content"])
        self._assert_chat_safe(response)
        self.sync.assert_not_called()

    def test_short_secret_echo_in_prose_never_reaches_storage_or_provider(self):
        content = json.dumps({"headers": {"Authorization": "abc"}, "note": "密码 abc"}) + "\n密码 abc"
        response = self._post(content)
        self.assertNotIn("abc", _REFERENCE.sub("[saved]", response.json()["content"]))
        self.assertNotIn("abc", _REFERENCE.sub("[saved]", json.dumps(self.contexts)))
        with get_connection() as conn:
            rows = [row["content"] for row in conn.execute("SELECT content FROM messages")]
        self.assertNotIn("abc", _REFERENCE.sub("[saved]", json.dumps(rows)))

    def test_generic_chat_redaction_preserves_secret_references(self):
        response = self._post(json.dumps({"token": self.headers["Authorization"]}))
        content = response.json()["content"]
        parsed = json.loads(content)
        self.assertTrue(_REFERENCE.fullmatch(parsed["token"]))
        safe, changed = chat_service._redact_secrets(content)
        self.assertEqual((safe, changed), (content, False))

    def test_add_mcp_restores_headers_and_returns_only_safe_counts(self):
        connector = self._add()
        self.assertEqual(set(connector), {"id", "name", "tools_count", "read_only_count"})
        self.assertEqual((connector["name"], connector["tools_count"], connector["read_only_count"]), ("sample-data", 2, 1))
        self._assert_safe(connector)
        with get_connection() as conn:
            row = conn.execute("SELECT ciphertext FROM connector_secrets WHERE connector_id = ?", (connector["id"],)).fetchone()
        self._assert_safe(row["ciphertext"])
        self.assertEqual(mcp.decrypt_headers(row["ciphertext"]), self.headers)
        self.assertEqual(self.sync.call_args.args, (self.endpoint, self.headers))

    def test_direct_url_headers_and_name_override(self):
        safe = json.loads(self._store(self._config()))["mcpServers"]["sample-data"]
        result = self._call("add_mcp", {"url": self.endpoint, "headers": safe["headers"], "name": "renamed"})
        self.assertEqual(result.status, "ok", result.text)
        self.assertEqual(result.data["connectors"][0]["name"], "renamed")
        self._assert_safe(result.data)

    def test_name_override_cannot_echo_header_values(self):
        safe = self._store(self._config())
        result = self._call("add_mcp", {"config": safe, "name": "service-" + self.headers["X-Custom-Auth"]})
        self.assertEqual(result.status, "ok", result.text)
        self.assertEqual(result.data["connectors"][0]["name"], "service-***")
        self._assert_safe(result.data)

    def test_foreign_expired_and_missing_references_fail_closed(self):
        safe = self._store(self._config())
        for kind in ("foreign", "expired", "missing"):
            with self.subTest(kind=kind):
                with get_connection() as conn:
                    conn.execute("UPDATE chat_secrets SET user_id = ?, expires_at = ?",
                                 ("someone-else" if kind == "foreign" else "local",
                                  "2000-01-01T00:00:00+00:00" if kind == "expired" else "2099-01-01T00:00:00+00:00"))
                config = safe if kind != "missing" else safe.replace(_REFERENCE.findall(safe)[0], "{{secret:sec_0123456789abcdef}}")
                result = self._call("add_mcp", {"config": config})
                self.assertEqual(result.status, "error")
                self.assertEqual(result.data["connectors"], [])
                self.assertIn("令牌引用已失效，请让用户重新粘贴配置", result.text)
        self.sync.assert_not_called()

    def test_url_absent_from_user_messages_is_rejected(self):
        result = self._call("add_mcp", {"url": self.endpoint})
        self.assertEqual(result.status, "error")
        self.assertIn("只能添加用户在对话里提供的地址", result.text)
        self.sync.assert_not_called()

    def test_assistant_and_tool_urls_do_not_authorize_connection(self):
        for role in ("assistant", "tool"):
            self._store(self.endpoint, role=role)
        result = self._call("add_mcp", {"url": self.endpoint})
        self.assertEqual(result.status, "error")
        self.assertIn("只能添加用户在对话里提供的地址", result.text)
        self.sync.assert_not_called()

    def test_other_session_and_foreign_user_urls_do_not_authorize(self):
        with get_connection() as conn:
            conn.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                         ("other-session", "local", "other", "2026-10-04T00:00:00+00:00", "2026-10-04T00:00:00+00:00"))
        self._store(self.endpoint, session_id="other-session")
        self._store(self.endpoint, user_id="someone-else")
        self.assertEqual(self._call("add_mcp", {"url": self.endpoint}).status, "error")
        self.assertEqual(self._call("add_mcp", {"url": self.endpoint}, AgentContext("local")).status, "error")
        self.sync.assert_not_called()

    def test_normalized_url_accepts_case_default_port_query_and_json_escaping(self):
        escaped = self._config(endpoint="https://EXAMPLE.com:443/private/path?old=value", headers={}).replace("https://", "https:\\/\\/")
        self._store(escaped)
        result = self._call("add_mcp", {"url": self.endpoint})
        self.assertEqual(result.status, "ok", result.text)
        self.assertEqual(self.sync.call_args.args[0], self.endpoint)

    def test_other_host_port_and_path_are_rejected(self):
        self._store(self.endpoint)
        for url in ("https://attacker.example.com/private/path", "https://example.com:8443/private/path", "https://example.com/private/path/extra"):
            with self.subTest(url=url):
                result = self._call("add_mcp", {"url": url})
                self.assertEqual(result.status, "error")
                self.assertIn("只能添加用户在对话里提供的地址", result.text)
        self.sync.assert_not_called()

    def test_json_url_punctuation_cannot_authorize_another_path(self):
        self._store(self._config(endpoint="https://example.com/mcp.", headers={}))
        result = self._call("add_mcp", {"url": "https://example.com/mcp"})
        self.assertEqual(result.status, "error")
        self.assertIn("只能添加用户在对话里提供的地址", result.text)
        self.sync.assert_not_called()

    def test_unsafe_addresses_and_stdio_are_rejected(self):
        for endpoint in ("http://example.com/mcp", "https://127.0.0.1/mcp", "https://10.0.0.2/mcp", "https://169.254.169.254/latest", "https://203.0.113.10/mcp"):
            with self.subTest(endpoint=endpoint):
                safe = self._store(self._config(endpoint=endpoint))
                result = self._call("add_mcp", {"config": safe})
                self.assertEqual(result.status, "error")
                self.assertIn("地址不允许", result.text)
                self._assert_safe(result.data)
        result = self._call("add_mcp", {"config": json.dumps({"mcpServers": {"local": {"command": "anything"}}})})
        self.assertEqual(result.status, "error")
        self.assertIn("暂不支持本地命令型 MCP", result.text)
        self.sync.assert_not_called()

    def test_same_endpoint_updates_and_preserves_tool_disabled_state(self):
        created = self.client.post("/api/v1/connectors/mcp", json={"config": self._config()})
        self.assertEqual(created.status_code, 201, created.text)
        connector_id = created.json()["connectors"][0]["id"]
        disabled = self.client.patch("/api/v1/connectors/{}/mcp".format(connector_id), json={"tools": {"reset_sample": False}})
        self.assertEqual(disabled.status_code, 200, disabled.text)
        replacement = {"Authorization": "Bearer chat-new-private-token", "X-Custom-Auth": "updated-pass"}
        safe = self._store(self._config(headers=replacement))
        result = self._call("add_mcp", {"config": safe, "name": "renamed"})
        self.assertEqual(result.status, "ok", result.text)
        connector = result.data["connectors"][0]
        self.assertEqual((connector["id"], connector["name"], connector["tools_count"], connector["read_only_count"]), (connector_id, "renamed", 1, 1))
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM connectors WHERE endpoint = ?", (self.endpoint,)).fetchone()["n"], 1)
            secret = conn.execute("SELECT ciphertext FROM connector_secrets WHERE connector_id = ?", (connector_id,)).fetchone()
        self.assertEqual(mcp.decrypt_headers(secret["ciphertext"]), replacement)
        self._assert_safe(result.data, headers=replacement)

    def test_connection_errors_and_catalog_echo_are_scrubbed(self):
        safe = self._store(self._config())
        self.sync.side_effect = mcp.MCPError("远端拒绝 " + self.headers["Authorization"])
        result = self._call("add_mcp", {"config": safe})
        self.assertEqual(result.status, "error")
        self._assert_safe(result.data)

        def echo(endpoint, headers, *, redaction_headers=None):
            data = self._sync(endpoint, headers)
            data["tools"][0]["description"] = self.headers["Authorization"]
            data["server"]["name"] = self.headers["X-Custom-Auth"]
            return data

        self.sync.side_effect = echo
        result = self._call("add_mcp", {"config": safe})
        self.assertEqual(result.status, "ok", result.text)
        with get_connection() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM connectors")]
        self._assert_safe(result.data, rows)

    def test_batch_unauthorized_url_prevents_all_connections(self):
        self._store(self.endpoint)
        config = {"mcpServers": {
            "provided": {"url": self.endpoint}, "injected": {"url": "https://attacker.example.com/mcp"},
        }}
        result = self._call("add_mcp", {"config": json.dumps(config)})
        self.assertEqual(result.status, "error")
        self.sync.assert_not_called()

    def test_list_returns_only_safe_connector_fields(self):
        connector = self._add()
        result = self._call("list", {})
        self.assertEqual(result.status, "ok", result.text)
        self.assertEqual(result.data["connectors"], [{"id": connector["id"], "name": "sample-data", "host": "example.com", "tools_count": 2, "enabled": True}])
        self._assert_safe(result.data)
        self.assertNotIn("headers", result.text)
        self.assertNotIn("private/path", result.text)
        self.assertEqual(self._call("list", {}, AgentContext("someone-else")).data, {"connectors": []})

    def test_remove_always_requires_confirmation_and_deletes_secret(self):
        connector = self._add()
        tool = self._tool("remove")
        with patch.object(policy, "_audit"), patch.object(policy, "_permission_always", return_value=True):
            for _ in range(2):
                decision = asyncio.run(policy.decide(self.ctx, tool, {"name": "sample-data"}))
                self.assertEqual(decision.decision, "confirm")
                self.assertFalse(decision.allow_always)
            decision = asyncio.run(policy.decide({"user_id": "local", "confirmed": True}, tool))
            self.assertEqual(decision.decision, "allow")
        result = self._call("remove", {"name": "sample-data"})
        self.assertEqual(result.data, {"id": connector["id"], "name": "sample-data", "removed": True})
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM connectors").fetchone()["n"], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM connector_secrets").fetchone()["n"], 0)

    def test_add_and_set_enabled_are_auto_allowed_and_owner_scoped(self):
        with patch.object(policy, "_audit"):
            for suffix in ("add_mcp", "set_enabled"):
                self.assertEqual(asyncio.run(policy.decide(self.ctx, self._tool(suffix), {})).decision, "allow")
        connector = self._add()
        foreign = self._call("set_enabled", {"connector_id": connector["id"], "enabled": False}, AgentContext("someone-else"))
        self.assertEqual(foreign.status, "error")
        self.assertEqual(self._call("remove", {"connector_id": connector["id"]}, AgentContext("someone-else")).status, "error")
        for enabled in (False, True):
            result = self._call("set_enabled", {"name": "sample-data", "enabled": enabled})
            self.assertEqual(result.status, "ok", result.text)
            self.assertEqual(self._call("list", {}).data["connectors"][0]["enabled"], enabled)
        self.assertEqual(self._call("set_enabled", {"name": "sample-data", "enabled": "false"}).status, "error")

    def test_streaming_model_adds_connector_then_answers_naturally(self):
        calls = []

        def provider(messages, tools=None):
            self.contexts.append(copy.deepcopy(messages))
            calls.append(tools)
            if len(calls) == 1:
                user = next(item["content"] for item in reversed(messages) if item.get("role") == "user")
                config = user.split("\n", 1)[0]
                yield {"type": "tool_calls", "calls": [{"id": "add-one", "name": "luma.connectors.add_mcp", "arguments": json.dumps({"config": config})}]}
            else:
                yield {"type": "text", "content": "sample-data 已连接，包含 2 个工具，其中 1 个只读。"}

        with patch.object(chat_service, "provider_stream_chat", provider):
            response = self._post(self._config() + "\n帮我把这个配上", stream=True)
        events = self._events(response.text)
        start = next(data for kind, data in events if kind == "start")
        done = next(data for kind, data in events if kind == "done")
        self.assertIn("{{secret:", start["user_message"]["content"])
        self.assertEqual(done["content"], "sample-data 已连接，包含 2 个工具，其中 1 个只读。")
        self.assertEqual(len(calls), 3)
        self.assertIsNone(calls[-1])
        self.assertEqual("".join(data["content"] for kind, data in events if kind == "delta"), done["content"])
        self.assertTrue(any(item["function"]["name"] == "luma.connectors.add_mcp" for item in calls[0]))
        self.assertFalse(any(kind == "tool" and data.get("kind") == "connector_added" for kind, data in events))
        self.assertFalse(any(kind == "widget" for kind, _data in events))
        self.assertTrue(any(kind == "tool" and data.get("tool") == "luma.connectors.add_mcp" and data.get("status") == "ok" for kind, data in events))
        with get_connection() as conn:
            connector = conn.execute("SELECT id,name FROM connectors WHERE user_id = ?", ("local",)).fetchone()
            secret = conn.execute("SELECT ciphertext FROM connector_secrets WHERE connector_id = ?", (connector["id"],)).fetchone()
        self.assertEqual(connector["name"], "sample-data")
        self.assertEqual(mcp.decrypt_headers(secret["ciphertext"]), self.headers)
        self._assert_chat_safe(response)

    def test_pasting_json_alone_in_stream_never_auto_connects(self):
        response = self._post(self._config(), stream=True)
        events = self._events(response.text)
        self.assertEqual(next(data["content"] for kind, data in events if kind == "done"), "收到配置了，你希望我连接这个服务吗？")
        self.sync.assert_not_called()
        self._assert_chat_safe(response)

    def test_foreign_session_rejects_before_secret_storage(self):
        with get_connection() as conn:
            conn.execute("UPDATE sessions SET user_id = ? WHERE id = ?", ("someone-else", "mcp-chat-session"))
        response = self.client.post("/api/v1/sessions/mcp-chat-session/messages", json={"content": self._config()})
        self.assertEqual(response.status_code, 404, response.text)
        self.sync.assert_not_called()
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM chat_secrets").fetchone()["n"], 0)

    def test_real_catalog_scrubs_credentials_before_truncation(self):
        token = "catalog-test-secret-" + "ABCD" * 350 + "-tail"
        headers = {"Authorization": "Bearer " + token}
        raw_tools = [{"name": "query", "title": "title " + token, "description": "description " + token,
                      "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}]
        with patch.object(mcp, "MCPClient") as client_class:
            client_class.return_value.__enter__.return_value.list_tools.return_value = (raw_tools, {"name": "server " + token})
            metadata = mcp_catalog.mcp_sync(self.endpoint, self.headers, redaction_headers=headers)
        self.assertEqual(metadata["tools"][0]["title"], "title ***")
        self.assertEqual(metadata["tools"][0]["description"], "description ***")
        self._assert_safe(metadata, headers=headers)


if __name__ == "__main__":
    unittest.main()
