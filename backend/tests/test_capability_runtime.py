"""On-demand schemas, owner-bound handles and durable write confirmations."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from tests.pg import reset_tables
from cryptography.fernet import Fernet
from app import mcp, widgets, runtime
from app.agent import tools, loop, policy
from app.services import capabilities, chat, context
from app.db import get_connection


class CapabilityRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.user = "cap-runtime-" + uuid.uuid4().hex
        self.connector = "connector-" + uuid.uuid4().hex
        self.session = "session-" + uuid.uuid4().hex
        self.message = "message-" + uuid.uuid4().hex
        self.raw = [
            {"name": "query_attendance", "description": "Read attendance diagnostics", "inputSchema": {"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]}, "annotations": {"readOnlyHint": True}},
            {"name": "submit_leave", "description": "Submit a leave draft", "inputSchema": {"type": "object", "properties": {"draft_id": {"type": "string"}}, "required": ["draft_id"]}, "annotations": {"readOnlyHint": False}},
        ]
        self.env = patch.dict(os.environ, {"LUMA_SECRETS_KEY": Fernet.generate_key().decode(), "LUMA_SKILL_PATHS": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.metadata = {"tools": mcp.normalize_tools(self.raw), "server": {"name": "fixture", "version": "1"}, "status": "ok"}
        with get_connection() as conn:
            conn.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)", (self.session, self.user, "runtime", "2026-10-08T00:00:00+00:00", "2026-10-08T00:00:00+00:00"))
            conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)", (self.message, self.user, self.session, "assistant", "", "2026-10-08T00:00:00+00:00", "{}"))
            conn.execute("INSERT INTO connectors(id,user_id,name,kind,endpoint,capabilities_json,config_json,enabled,created_at,updated_at,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (self.connector, self.user, "Fixture", "mcp", "https://example.org/mcp", "[]", "{}", 1, "2026-10-08T00:00:00+00:00", "2026-10-08T00:00:00+00:00", json.dumps(self.metadata)))
            conn.execute("INSERT INTO connector_secrets(connector_id,user_id,ciphertext,updated_at) VALUES (?,?,?,?)", (self.connector, self.user, mcp.encrypt_headers({}), "2026-10-08T00:00:00+00:00"))
        self.ctx = loop.AgentContext(self.user, session_id=self.session, assistant_message_id=self.message)
        self.read_id = "mcp:" + self.connector + ":query_attendance"
        self.write_id = "mcp:" + self.connector + ":submit_leave"
        self.sandbox = patch.object(tools, "_sandbox_enabled", return_value=False)
        self.sandbox.start()
        self.addCleanup(self.sandbox.stop)

    def load(self, *ids, ctx=None):
        return asyncio.run(tools._capability_load(ctx or self.ctx, {"capability_ids": list(ids)}))

    def test_initial_registry_does_not_read_full_catalog(self):
        with patch.object(tools, "mcp_catalog", side_effect=AssertionError("full schema catalog read")):
            names = [item.name for item in tools.registry_for(self.user, mode="interactive", ctx=self.ctx)[0]]
        self.assertIn("luma.capabilities.search", names)
        self.assertFalse(any(name.startswith("mcp_") or name.startswith("mcp.") for name in names))

    def test_load_next_round_and_release_on_completion(self):
        seen, called = [], []
        read_name = capabilities.load(self.user, [self.read_id])["definitions"][0]["function"]["name"]
        rounds = iter([
            {"type": "tool_calls", "calls": [{"id": "load", "name": "luma.capabilities.load", "arguments": json.dumps({"capability_ids": [self.read_id]})}]},
            {"type": "tool_calls", "calls": [{"id": "read", "name": read_name, "arguments": '{"day":"2026-10-07"}'}]},
            {"type": "text", "content": "draft"},
            {"type": "text", "content": "真实模拟证据：无异常"},
        ])
        async def provider(messages, tools=None):
            seen.append([item["function"]["name"] for item in tools or []])
            yield next(rounds)
        client = SimpleNamespace(list_tools=lambda: (self.raw, {}), call_tool=lambda name, args: called.append((name, args)) or "无异常", close=lambda: None)
        async def get_client(*args): return client
        with patch.object(loop, "provider_astream_chat", provider), patch.object(loop, "_decider_needs_tools", return_value=True), patch.object(tools, "_mcp_client", get_client):
            result = asyncio.run(loop.run_agent(self.ctx, [{"role": "user", "content": "查昨天考勤"}]))
        self.assertNotIn(read_name, seen[0])
        self.assertIn(read_name, seen[1])
        self.assertEqual(called, [("query_attendance", {"day": "2026-10-07"})])
        self.assertEqual(result.reply, "真实模拟证据：无异常")
        self.assertEqual(tools.capability_state(self.ctx), {"tools": [], "skills": []})

    def test_loaded_handles_persist_without_schema_or_arguments(self):
        self.assertEqual(self.load(self.read_id).status, "ok")
        with get_connection() as conn:
            state = json.loads(conn.execute("SELECT metadata_json FROM messages WHERE id=? AND user_id=?", (self.message, self.user)).fetchone()["metadata_json"])["capability_state"]
        self.assertEqual(set(state["tools"][0]), {"id", "version"})
        self.assertNotIn("example.org", json.dumps(state))
        restored = loop.AgentContext(self.user, session_id=self.session, assistant_message_id=self.message)
        self.assertTrue(any(item.metadata.get("capability_id") == self.read_id for item in tools.registry_for(self.user, mode="interactive", ctx=restored)[0]))
        outsider = loop.AgentContext("other", session_id=self.session, assistant_message_id=self.message)
        self.assertFalse(any(item.metadata.get("capability_id") for item in tools.registry_for("other", mode="interactive", ctx=outsider)[0]))

    def test_write_confirmation_bound_to_version_and_no_replay(self):
        self.assertEqual(self.load(self.write_id).status, "ok")
        target = next(item for item in tools.registry_for(self.user, mode="interactive", ctx=self.ctx)[0] if item.metadata.get("capability_id") == self.write_id)
        decision = asyncio.run(policy.decide(self.ctx, target, {"draft_id": "fixture-draft"}))
        self.assertEqual(decision.decision, "confirm")
        self.assertFalse(decision.allow_always)
        widget = widgets.create_confirm_widget(self.user, self.session, self.message, connector_id=self.connector, connector_name="Fixture", tool=target.metadata["mcp_name"], title="Submit draft", arguments={"draft_id": "fixture-draft"}, capability_id=self.write_id, capability_version=target.metadata["version"])
        self.metadata["tools"][1]["input_schema"]["required"] = ["new_required"]
        with get_connection() as conn:
            conn.execute("UPDATE connectors SET metadata_json=? WHERE id=? AND user_id=?", (json.dumps(self.metadata), self.connector, self.user))
        with patch.object(tools, "_mcp_client", side_effect=AssertionError("changed schema submitted")):
            result, _ = widgets.apply_event(self.user, widget["id"], "confirm", None)
        self.assertEqual(result["state"], {"status": "failed"})
        with self.assertRaises(Exception) as second:
            widgets.apply_event(self.user, widget["id"], "confirm", None)
        self.assertEqual(second.exception.status_code, 409)

    def test_remote_schema_changed_or_bad_args_never_calls(self):
        self.load(self.read_id)
        info = next(iter(self.ctx.capability_bundle["mapping"].values()))
        called, closed = [], []
        drifted = [dict(self.raw[0], inputSchema={"type": "object", "required": ["other"]})]
        client = SimpleNamespace(list_tools=lambda: (drifted, {}), call_tool=lambda *args: called.append(args), close=lambda: closed.append(True))
        async def get_client(ctx, connector_id):
            ctx.mcp_clients[connector_id] = client
            return client
        with patch.object(tools, "_mcp_client", get_client):
            bad_args = asyncio.run(tools._mcp_executor(self.ctx, {}, info))
            drift = asyncio.run(tools._mcp_executor(self.ctx, {"day": "2026-10-07"}, info))
        self.assertEqual([bad_args.status, drift.status], ["error", "error"])
        self.assertEqual(called, [])
        self.assertEqual(closed, [True])

    def test_release_removes_selection_and_budget_rejects_whole_schema(self):
        self.load(self.read_id)
        asyncio.run(tools._capability_release(self.ctx, {}))
        self.assertFalse(any(item.metadata.get("capability_id") for item in tools.registry_for(self.user, mode="interactive", ctx=self.ctx)[0]))
        self.metadata["tools"][0]["input_schema"]["description"] = "huge schema " * 8000
        with get_connection() as conn:
            conn.execute("UPDATE connectors SET metadata_json=? WHERE id=? AND user_id=?", (json.dumps(self.metadata), self.connector, self.user))
        self.assertEqual(self.load(self.read_id).status, "error")
        self.assertEqual(tools.capability_state(self.ctx)["tools"], [])

    def test_sensitive_skill_excludes_memory_even_after_release(self):
        bundle = capabilities.load(self.user, [self.read_id])
        bundle["skills"] = [{"id": "skill:private", "title": "Private workflow", "instructions": "Use owner workflow", "tool_ids": [self.read_id], "version": "fixture", "exclude_from_memory": True}]
        with patch.object(capabilities, "load", return_value=bundle):
            self.assertEqual(self.load("skill:private").status, "ok")
        asyncio.run(tools.release_capabilities(self.ctx))
        self.assertTrue(self.ctx.capability_sensitive)
        with patch.object(chat, "schedule_memory_extraction") as extraction:
            chat.persist_assistant_message(session_id=self.session, assistant_id=self.message, content="请确认草稿", created_at="2026-10-08T00:00:00+00:00", metadata={"memory_extraction_excluded": self.ctx.capability_sensitive}, user_id=self.user)
        extraction.assert_not_called()
        for name in ("luma.memory.create", "luma.notifications.create"):
            target = next(item for item in tools._builtin_tools() if item.name == name)
            self.assertEqual(asyncio.run(policy.decide(self.ctx, target, {})).decision, "deny")

    def test_changed_skill_rules_are_removed_before_next_round(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "skill.json"
            manifest = {"id": "fixture", "title": "Fixture skill", "summary": "Attendance rules", "instructions": "OLD RULE TEXT", "tools": [{"capability_id": self.read_id}], "allowed_users": [self.user]}
            manifest_path.write_text(json.dumps(manifest))
            with patch.dict(os.environ, {"LUMA_SKILL_PATHS": directory}):
                self.assertEqual(self.load("skill:fixture").status, "ok")
                manifest["allowed_users"] = ["other"]
                manifest_path.write_text(json.dumps(manifest))
                registered, _ = tools.registry_for(self.user, mode="interactive", ctx=self.ctx)
        self.assertFalse(any(item.metadata.get("capability_id") for item in registered))
        self.assertEqual(tools.capability_state(self.ctx), {"tools": [], "skills": []})
        self.assertIn("重新检索", self.ctx.capability_notice)

    def test_sensitive_summary_window_never_reaches_summary_model(self):
        evicted = [
            ({"id": "u", "role": "user", "content": "PRIVATE REASON"}, {"memory_extraction_excluded": True}, ""),
            ({"id": "a", "role": "assistant", "content": "PRIVATE WORKFLOW"}, {"memory_extraction_excluded": True}, ""),
        ]
        with patch.dict(os.environ, {"SUMMARY_TRIGGER_MESSAGES": "1"}), patch.object(context, "submit_memory_background") as submit:
            context._schedule_summary(self.user, self.session, evicted, "")
        submit.assert_not_called()

    def test_source_user_marker_protects_split_summary_window(self):
        original_id = "original-" + uuid.uuid4().hex
        with get_connection() as conn:
            conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)", (original_id, self.user, self.session, "user", "PRIVATE REASON", "2026-10-07T23:59:59+00:00", "{}"))
            conn.execute("UPDATE messages SET metadata_json=? WHERE id=? AND user_id=?", (json.dumps({"user_message_id": original_id}), self.message, self.user))
        self.ctx.capability_sensitive = True
        self.load(self.read_id)
        with get_connection() as conn:
            user = dict(conn.execute("SELECT * FROM messages WHERE id=? AND user_id=?", (original_id, self.user)).fetchone())
        metadata = json.loads(user["metadata_json"])
        self.assertTrue(metadata["memory_extraction_excluded"])
        # Its assistant is still in the live window; only this older user
        # row reaches the summary boundary.
        with patch.dict(os.environ, {"SUMMARY_TRIGGER_MESSAGES": "1"}), patch.object(context, "submit_memory_background") as submit:
            context._schedule_summary(self.user, self.session, [(user, metadata, ""), ({"id": "older", "role": "user"}, {}, "")], "")
        submit.assert_not_called()

    def test_client_binding_changes_on_endpoint_and_credential_rotation(self):
        created, closed = [], []
        class Client:
            def __init__(client, endpoint, headers):
                client.endpoint, client.headers, client.instructions = endpoint, headers, ""
                created.append(client)
            def initialize(client): pass
            def close(client): closed.append(client)
        async def run():
            first = await tools._mcp_client(self.ctx, self.connector)
            self.assertIs(await tools._mcp_client(self.ctx, self.connector), first)
            with get_connection() as conn:
                conn.execute("UPDATE connector_secrets SET ciphertext=?,updated_at=? WHERE connector_id=? AND user_id=?", (mcp.encrypt_headers({"X-Key": "fixture-rotated"}), "2026-10-08T00:00:01+00:00", self.connector, self.user))
            second = await tools._mcp_client(self.ctx, self.connector)
            self.assertIsNot(second, first)
            self.assertEqual(second.headers, {"X-Key": "fixture-rotated"})
            with get_connection() as conn:
                conn.execute("UPDATE connectors SET endpoint=? WHERE id=? AND user_id=?", ("https://example.org/new-mcp", self.connector, self.user))
            third = await tools._mcp_client(self.ctx, self.connector)
            self.assertIsNot(third, second)
            self.assertEqual(third.endpoint, "https://example.org/new-mcp")
        with patch.object(mcp, "MCPClient", Client):
            asyncio.run(run())
        self.assertEqual(len(created), 3)
        self.assertEqual(closed, created[:2])

    def test_same_client_revalidates_remote_schema_before_every_call(self):
        self.load(self.read_id)
        info = next(iter(self.ctx.capability_bundle["mapping"].values()))
        raw, called, listed = list(self.raw), [], []
        def list_tools():
            listed.append(True)
            return raw, {}
        client = SimpleNamespace(list_tools=list_tools, call_tool=lambda *args: called.append(args) or "ok", close=lambda: None)
        async def get_client(*args): return client
        async def run():
            first = await tools._mcp_executor(self.ctx, {"day": "2026-10-07"}, info)
            raw[0] = dict(raw[0], annotations={"readOnlyHint": False})
            second = await tools._mcp_executor(self.ctx, {"day": "2026-10-07"}, info)
            return first, second
        with patch.object(tools, "_mcp_client", get_client):
            first, second = asyncio.run(run())
        self.assertEqual([first.status, second.status], ["ok", "error"])
        self.assertEqual(len(called), 1)
        self.assertEqual(len(listed), 2)

    def test_background_allowlist_uses_exact_owner_handles(self):
        read_name = capabilities.load(self.user, [self.read_id])["definitions"][0]["function"]["name"]
        write_name = capabilities.load(self.user, [self.write_id])["definitions"][0]["function"]["name"]
        default = runtime._allowed_tools({"user_id": self.user})
        self.assertIn("luma.capabilities.search", default)
        self.assertIn("luma.capabilities.load", default)
        self.assertIn(read_name, default)
        self.assertNotIn(write_name, default)
        self.assertEqual(runtime._allowed_tools({"user_id": self.user, "allowed_tools": [self.read_id]}), [read_name])
        self.assertEqual(runtime._allowed_tools({"user_id": self.user, "allowed_tools": ["mcp.Fixture.query_attendance"]}), [read_name])
        self.assertEqual(runtime._allowed_tools({"user_id": self.user, "allowed_tools": [write_name]}), [write_name])
        for requested, owner in [(read_name, "other"), ("mcp_arbitrary", self.user)]:
            with self.assertRaises(ValueError):
                runtime._allowed_tools({"user_id": owner, "allowed_tools": [requested]})
        self.assertFalse(runtime._job_is_idempotent({"type": "agent_run", "payload": {"allowed_tools": [write_name]}}))

    def test_background_approval_cannot_authorize_changed_capability(self):
        self.load(self.write_id)
        target = next(item for item in tools.registry_for(self.user, mode="interactive", ctx=self.ctx)[0] if item.metadata.get("capability_id") == self.write_id)
        job = runtime.create_job("agent_run", {"prompt": "submit fixture", "allowed_tools": [target.name]}, user_id=self.user)
        approved_payload = {"draft_id": "fixture", "_capability_guard": {"id": self.write_id, "version": target.metadata["version"]}}
        approval = runtime.create_approval(job["id"], target.name, approved_payload, self.user)
        runtime.decide_approval(approval["id"], "approved", user_id=self.user)
        with get_connection() as conn:
            raw = conn.execute("SELECT payload_json FROM runtime_approvals WHERE id=? AND user_id=?", (approval["id"], self.user)).fetchone()["payload_json"]
        approved = {"id": approval["id"], "action": target.name, "payload": {"draft_id": "fixture"}, "capability_guard": {"id": self.write_id, "version": "expired"}, "approval_payload_json": raw}
        ctx = loop.AgentContext(self.user, mode="background", job_id=job["id"])
        ctx.approved_calls = [approved]
        self.assertEqual(asyncio.run(policy.decide(ctx, target, {"draft_id": "fixture"})).decision, "confirm")
        self.assertEqual(len(ctx.approved_calls), 1)
        ctx.approved_calls[0]["capability_guard"]["version"] = target.metadata["version"]
        self.assertEqual(asyncio.run(policy.decide(ctx, target, {"draft_id": "fixture"})).decision, "allow")
        self.assertEqual(ctx.approved_calls, [])
        second = loop.AgentContext(self.user, mode="background", job_id=job["id"])
        second.approved_calls = [approved]
        self.assertEqual(asyncio.run(policy.decide(second, target, {"draft_id": "fixture"})).decision, "confirm")
        with get_connection() as conn:
            state = conn.execute("SELECT status FROM runtime_approvals WHERE id=? AND user_id=?", (approval["id"], self.user)).fetchone()["status"]
        self.assertEqual(state, "consumed")

    def test_background_load_cannot_expand_job_allowlist(self):
        self.ctx.mode = "background"
        read_name = capabilities.load(self.user, [self.read_id])["definitions"][0]["function"]["name"]
        write_name = capabilities.load(self.user, [self.write_id])["definitions"][0]["function"]["name"]
        self.ctx.allowed_tools = ["luma.capabilities.load", read_name]
        seen = []
        rounds = iter([
            {"type": "tool_calls", "calls": [{"id": "load", "name": "luma.capabilities.load", "arguments": json.dumps({"capability_ids": [self.write_id]})}]},
            {"type": "tool_calls", "calls": [{"id": "write", "name": write_name, "arguments": '{"draft_id":"fixture"}'}]},
            {"type": "text", "content": "blocked"},
            {"type": "text", "content": "白名单未授权"},
        ])
        async def provider(messages, tools=None):
            seen.append([item["function"]["name"] for item in tools or []])
            yield next(rounds)
        with patch.object(loop, "provider_astream_chat", provider), patch.object(loop, "_decider_needs_tools", return_value=True), patch.object(tools, "_mcp_client", side_effect=AssertionError("unauthorized background write")):
            outcome = asyncio.run(loop.run_agent(self.ctx, [{"role": "user", "content": "try write"}]))
        self.assertNotIn(write_name, seen[1])
        self.assertEqual(next(item for item in outcome.tool_calls if item["call_id"] == "write")["decision"], "deny")

    def test_partial_skill_release_removes_only_released_instructions(self):
        seen = []
        rounds = iter([
            {"type": "tool_calls", "calls": [{"id": "load", "name": "luma.capabilities.load", "arguments": '{"capability_ids":["skill:a","skill:b"]}'}]},
            {"type": "tool_calls", "calls": [{"id": "release", "name": "luma.capabilities.release", "arguments": '{"capability_ids":["skill:a"]}'}]},
            {"type": "text", "content": "draft"},
            {"type": "text", "content": "done"},
        ])
        async def provider(messages, tools=None):
            seen.append(json.dumps(messages))
            yield next(rounds)
        with tempfile.TemporaryDirectory() as directory:
            for name in ("a", "b"):
                Path(directory, name + ".json").write_text(json.dumps({"id": name, "instructions": "RULE_" + name.upper(), "tools": [{"capability_id": self.read_id}]}))
            with patch.dict(os.environ, {"LUMA_SKILL_PATHS": directory}), patch.object(loop, "provider_astream_chat", provider), patch.object(loop, "_decider_needs_tools", return_value=True):
                asyncio.run(loop.run_agent(self.ctx, [{"role": "user", "content": "use rules"}]))
        self.assertIn("RULE_A", seen[1])
        self.assertNotIn("RULE_A", seen[2])
        self.assertIn("RULE_B", seen[2])

    def test_background_revision_snapshot_cannot_upgrade_read_to_write(self):
        self.ctx.mode = "background"
        versions = {}
        self.ctx.allowed_tools = runtime._allowed_tools({"user_id": self.user}, capability_versions=versions)
        self.ctx.allowed_capability_versions = versions
        read_name = capabilities.load(self.user, [self.read_id])["definitions"][0]["function"]["name"]
        self.metadata["tools"][0]["annotations"] = {"readOnlyHint": False}
        with get_connection() as conn:
            conn.execute("UPDATE connectors SET metadata_json=? WHERE id=? AND user_id=?", (json.dumps(self.metadata), self.connector, self.user))
        seen = []
        rounds = iter([
            {"type": "tool_calls", "calls": [{"id": "load", "name": "luma.capabilities.load", "arguments": json.dumps({"capability_ids": [self.read_id]})}]},
            {"type": "tool_calls", "calls": [{"id": "changed", "name": read_name, "arguments": '{"day":"2026-10-07"}'}]},
            {"type": "text", "content": "draft"},
            {"type": "text", "content": "能力版本已变化"},
        ])
        async def provider(messages, tools=None):
            seen.append([item["function"]["name"] for item in tools or []])
            yield next(rounds)
        with patch.object(loop, "provider_astream_chat", provider), patch.object(loop, "_decider_needs_tools", return_value=True), patch.object(tools, "_mcp_client", side_effect=AssertionError("read approval upgraded to write")):
            outcome = asyncio.run(loop.run_agent(self.ctx, [{"role": "user", "content": "read only"}]))
        self.assertNotIn(read_name, seen[1])
        self.assertEqual(next(item for item in outcome.tool_calls if item["call_id"] == "changed")["decision"], "deny")

    def test_confirmation_resume_applies_rolling_result_budget(self):
        name, messages, seen = "mcp_fixture", [], []
        for index in range(3):
            call_id = "receipt_" + str(index)
            messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]})
            messages.append({"role": "tool", "tool_call_id": call_id, "content": loop._UNTRUSTED_PREFIX + json.dumps({"value": str(index) + "x" * 11980})})
        target = tools.Tool(name, "fixture", {"type": "object"}, "read", lambda *_: tools.ToolResult("unused"))
        self.ctx.registry_for = lambda user_id, mode="interactive": ([target], [target.openai_definition()])
        async def provider(messages, tools=None):
            seen.append(json.loads(json.dumps(messages)))
            yield {"type": "text", "content": "done"}
        with patch.object(loop, "provider_astream_chat", provider), patch.object(loop, "_decider_needs_tools", return_value=True):
            asyncio.run(loop.run_agent(self.ctx, messages))
        receipts = [item["content"].removeprefix(loop._UNTRUSTED_PREFIX) for item in seen[0] if item.get("role") == "tool"]
        self.assertLessEqual(sum(len(item) for item in receipts), 24_000 + 100)
        self.assertIn("已释放", receipts[0])
        self.assertEqual(json.loads(receipts[-1])["value"][0], "2")


if __name__ == "__main__":
    unittest.main()
