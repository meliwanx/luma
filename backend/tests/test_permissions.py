"""Permission matrix coverage for the long-lived Sentinel policy."""

import asyncio
import json
import unittest
from unittest.mock import patch

from tests.pg import reset_tables

from fastapi.testclient import TestClient
from app import main
from app.agent.policy import decide, permission_key_for
from app.agent.tools import AgentContext, Tool, registry_for
from app.db import get_connection
from app.services import permissions


class PermissionPolicyTests(unittest.TestCase):
    def run_async(self, value):
        return asyncio.run(value)

    def decision(self, ctx, tool, args=None, *, always=True):
        with patch("app.agent.policy._audit"), patch("app.agent.policy._permission_always", return_value=always):
            return self.run_async(decide(ctx, tool, args or {}))

    def test_code_is_allowed_in_both_modes(self):
        tool = Tool("sandbox.python", "python", {"type": "object"}, "code", lambda *_: None)
        self.assertEqual(self.decision(AgentContext("u"), tool).decision, "allow")
        self.assertEqual(self.decision(AgentContext("u", mode="background"), tool).decision, "allow")

    def test_external_write_default_and_saved_ask(self):
        tool = Tool("mcp.c.write", "write", {"type": "object"}, "external_write", lambda *_: None,
                    metadata={"connector_id": "c", "tool": "write"})
        result = self.decision(AgentContext("u"), tool)
        self.assertEqual(result.decision, "allow")
        self.assertEqual(result.reason, "远端写操作，按默认策略直接执行")
        self.assertEqual(self.decision(AgentContext("u", mode="background"), tool).decision, "allow")
        self.assertEqual(self.decision(AgentContext("u"), tool, always=False).decision, "confirm")
        self.assertEqual(self.decision(AgentContext("u", mode="background"), tool, always=False).decision, "deny")

    def test_destructive_tools_never_become_always(self):
        tool = Tool("mcp.c.delete_order", "delete", {"type": "object"}, "external_write", lambda *_: None,
                    metadata={"connector_id": "c", "tool": "delete_order"})
        result = self.decision(AgentContext("u"), tool, always=True)
        self.assertEqual(result.decision, "confirm")
        self.assertFalse(result.allow_always)
        confirmed = self.decision({"user_id": "u", "confirmed": True}, tool, always=True)
        self.assertEqual(confirmed.decision, "allow")
        self.assertFalse(confirmed.allow_always)

    def test_luma_permission_key(self):
        tool = Tool("luma.tasks.update", "update", {"type": "object"}, "write", lambda *_: None)
        self.assertEqual(permission_key_for(tool), ("luma.tasks.update", True))

    def test_destructive_hint_overrides_read_risk_and_saved_always(self):
        tool = Tool("mcp.c.review", "review", {"type": "object"}, "read", lambda *_: None,
                    metadata={"connector_id": "c", "tool": "review", "annotations": {"readOnlyHint": True, "destructiveHint": True}})
        self.assertEqual(permission_key_for(tool), ("mcp:c:review", False))
        for _ in range(2):
            result = self.decision(AgentContext("u"), tool)
            self.assertEqual(result.decision, "confirm")
            self.assertFalse(result.allow_always)

    def test_local_device_calls_always_confirm(self):
        tool = Tool("device.read", "read device", {"type": "object"}, "local", lambda *_: None,
                    metadata={"permission_key": "device.read"})
        self.assertEqual(permission_key_for(tool), (None, False))
        for mode in ("interactive", "background"):
            result = self.decision(AgentContext("u", mode=mode), tool)
            self.assertEqual(result.decision, "confirm")
            self.assertFalse(result.allow_always)
        self.assertEqual(self.decision({"user_id": "u", "confirmed": True}, tool).decision, "allow")

    def test_builtin_permissions_default_always_and_respect_ask(self):
        for spec in permissions.BUILTIN_PERMISSION_SPECS:
            if spec.get("default_mode") == "ask":
                # Browser submissions have their own DOM-aware policy matrix.
                continue
            risk = "external_write" if spec["key"] == "sandbox.preview" else "write"
            tool = Tool(spec["key"], "write", {"type": "object"}, risk, lambda *_: None)
            self.assertEqual(self.decision(AgentContext("u"), tool).decision, "allow", tool.name)
            self.assertEqual(self.decision(AgentContext("u"), tool, always=False).decision, "confirm", tool.name)

    def test_every_decision_is_audited_without_arguments(self):
        tool = Tool("mcp.c.update", "write", {"type": "object"}, "external_write", lambda *_: None,
                    metadata={"connector_id": "c", "tool": "update"})
        with patch("app.agent.policy._audit") as audit, patch("app.agent.policy._permission_always", return_value=True):
            result = self.run_async(decide(AgentContext("u"), tool, {"private": "secret"}))
        audit.assert_called_once()
        self.assertEqual(audit.call_args.args[-1], result)

    def test_background_approval_cannot_be_reused_for_same_destructive_call(self):
        tool = Tool("mcp.c.delete_item", "delete", {"type": "object"}, "external_write", lambda *_: None,
                    metadata={"connector_id": "c", "tool": "delete_item"})
        for context_kind in ("dict", "dataclass"):
            for sequence_kind in ("list", "tuple"):
                with self.subTest(context=context_kind, sequence=sequence_kind):
                    approvals = [{"action": tool.name, "payload": {"item_id": "one"}}]
                    if sequence_kind == "tuple":
                        approvals = tuple(approvals)
                    ctx = {"user_id": "u", "mode": "background", "approved_calls": approvals}
                    if context_kind == "dataclass":
                        ctx = AgentContext("u", mode="background")
                        ctx.approved_calls = approvals
                    self.assertEqual(self.decision(ctx, tool, {"item_id": "other"}).decision, "confirm")
                    self.assertEqual(self.decision(ctx, tool, {"item_id": "one"}).decision, "allow")
                    self.assertEqual(self.decision(ctx, tool, {"item_id": "one"}).decision, "confirm")


class PermissionSettingsTests(unittest.TestCase):
    def setUp(self):
        self.identity = patch("app.deps.current_user_id", return_value="local")
        self.identity.start()
        self.addCleanup(self.identity.stop)
        reset_tables()
        self.user_id = "local"
        self.connector_id = "order-connector"
        self.metadata = {"tools": [
            {"name": "demo_get_table_schema", "read_only": False},
            {"name": "demo_search_metrics", "read_only": False},
            {"name": "demo_update_note", "read_only": False},
            {"name": "demo_review", "read_only": True, "annotations": {"readOnlyHint": True, "destructiveHint": True}},
            {"name": "demo_drop_table", "read_only": True},
            {"name": "demo_transfer_funds", "read_only": False},
        ]}
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO connectors(id,user_id,name,kind,endpoint,capabilities_json,config_json,enabled,created_at,updated_at,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self.connector_id, self.user_id, "order server", "mcp", "https://example.com/mcp", "[]", "{}", 1,
                 "2026-10-05T00:00:00+00:00", "2026-10-05T00:00:00+00:00", json.dumps(self.metadata)),
            )

    def key(self, name):
        return "mcp:%s:%s" % (self.connector_id, name)

    def test_get_permissions_defaults_and_legacy_catalog_classification(self):
        response = TestClient(main.app).get("/api/v1/permissions")
        self.assertEqual(response.status_code, 200, response.text)
        items = {item["key"]: item for item in response.json()["items"]}
        for name in ("demo_get_table_schema", "demo_search_metrics"):
            self.assertNotIn(self.key(name), items)
        self.assertEqual(items[self.key("demo_update_note")]["mode"], "always")
        self.assertTrue(items[self.key("demo_update_note")]["allow_always"])
        for name in ("demo_review", "demo_drop_table", "demo_transfer_funds"):
            self.assertEqual(items[self.key(name)]["mode"], "ask")
            self.assertFalse(items[self.key(name)]["allow_always"])
        for spec in permissions.BUILTIN_PERMISSION_SPECS:
            self.assertEqual(items[spec["key"]]["mode"], spec.get("default_mode", "always"))

    def test_saved_modes_are_preserved_and_destructive_always_is_effectively_ask(self):
        choices = [(self.key("demo_update_note"), "ask"), ("luma.tasks.update", "ask"),
                   ("luma.routines.create", "always"), (self.key("demo_review"), "always")]
        with get_connection() as conn:
            for key, mode in choices:
                conn.execute("INSERT INTO tool_permissions(user_id,key,mode,updated_at) VALUES (?,?,?,?)",
                             (self.user_id, key, mode, "2026-10-05T00:00:00+00:00"))
        items = {item["key"]: item for item in permissions.permission_items(self.user_id)}
        self.assertEqual(items[self.key("demo_update_note")]["mode"], "ask")
        self.assertEqual(items["luma.tasks.update"]["mode"], "ask")
        self.assertEqual(items["luma.routines.create"]["mode"], "always")
        self.assertEqual(items[self.key("demo_review")]["mode"], "ask")
        self.assertEqual(permissions.permission_mode(self.user_id, self.key("demo_update_note")), "ask")
        with get_connection() as conn:
            row = conn.execute("SELECT mode FROM tool_permissions WHERE user_id = ? AND key = ?",
                               (self.user_id, self.key("demo_review"))).fetchone()
        self.assertEqual(row["mode"], "always")

    def test_existing_connector_risks_are_recomputed_without_sync(self):
        tools, _ = registry_for(self.user_id, mode="interactive")
        remote = {tool.metadata["tool"]: tool for tool in tools if tool.metadata.get("connector_id") == self.connector_id}
        with patch("app.agent.policy._audit"):
            for name in ("demo_get_table_schema", "demo_search_metrics"):
                self.assertEqual(remote[name].risk, "read")
                self.assertEqual(asyncio.run(decide(AgentContext(self.user_id), remote[name])).decision, "allow")
            self.assertEqual(asyncio.run(decide(AgentContext(self.user_id), remote["demo_update_note"])).decision, "allow")
            for name in ("demo_review", "demo_drop_table", "demo_transfer_funds"):
                self.assertEqual(remote[name].risk, "external_write")
                self.assertEqual(asyncio.run(decide(AgentContext(self.user_id), remote[name])).decision, "confirm")
                self.assertFalse(permission_key_for(remote[name])[1])

    def test_setting_ask_is_respected_and_high_risk_always_rejected(self):
        key = self.key("demo_update_note")
        self.assertEqual(permissions.permission_mode(self.user_id, key), "always")
        permissions.set_permission_mode(self.user_id, key, "ask")
        tool = Tool("mcp.order_server.demo_update_note", "update", {"type": "object"}, "external_write", lambda *_: None,
                    metadata={"connector_id": self.connector_id, "tool": "demo_update_note"})
        with patch("app.agent.policy._audit"):
            self.assertEqual(asyncio.run(decide(AgentContext(self.user_id), tool)).decision, "confirm")
        for name in ("demo_review", "demo_drop_table", "demo_transfer_funds"):
            with self.assertRaises(PermissionError):
                permissions.set_permission_mode(self.user_id, self.key(name), "always")


if __name__ == "__main__":
    unittest.main()
