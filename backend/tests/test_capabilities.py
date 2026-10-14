"""Discovery, isolation and schema budgets after implementation."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import mcp
from app.services import capabilities as service


def record(user="alice", count=100):
    raw = [{"name": "query_%d" % i, "title": "Record query %d" % i,
            "description": "Find records", "inputSchema": {"type": "object", "properties": {
                "record_id": {"type": "integer"}}, "required": ["record_id"]},
            "annotations": {"readOnlyHint": True}} for i in range(count)]
    raw[-1]["title"] = "考勤异常诊断"
    return {"id": "connector_" + user, "user_id": user, "name": "Example",
            "endpoint": "https://example.com/mcp", "credential_revision": "revision-1",
            "metadata_json": json.dumps({"tools": mcp.normalize_tools(raw),
                                          "server": {"name": "Example", "version": "1"}})}


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.records = {"alice": record(), "bob": record("bob")}
        self.patch_rows = patch.object(service, "_rows", side_effect=lambda user: [self.records[user]])
        self.patch_rows.start()
        self.addCleanup(self.patch_rows.stop)
        self.env = patch.dict(os.environ, {"LUMA_SKILL_PATHS": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_discovery_reaches_beyond_legacy_catalog_limits_without_schema(self):
        results = service.search("alice", "昨天考勤异常")
        self.assertEqual(results[0]["capability_id"], "mcp:connector_alice:query_99")
        text = json.dumps(results)
        for unwanted in ("input_schema", "parameters", "instructions", "endpoint", "https://"):
            self.assertNotIn(unwanted, text)

    def test_owner_cannot_load_another_users_id(self):
        with self.assertRaises(service.CapabilityError):
            service.load("bob", ["mcp:connector_alice:query_99"])

    def test_load_selects_only_requested_schema_and_stable_names(self):
        loaded = service.load("alice", ["mcp:connector_alice:query_99"])
        self.assertEqual(len(loaded["definitions"]), 1)
        info = next(iter(loaded["mapping"].values()))
        name = info["mcp_name"]
        self.records["alice"]["name"] = "Renamed"
        self.assertEqual(next(iter(service.load("alice", [info["capability_id"]])["mapping"])), name)
        self.assertTrue(info["read_only"])

    def test_selected_tool_budget(self):
        with self.assertRaises(service.CapabilityError):
            service.load("alice", ["mcp:connector_alice:query_%d" % i for i in range(9)])

    def test_disable_and_credential_rotation_invalidate_selection(self):
        info = next(iter(service.load("alice", ["mcp:connector_alice:query_99"])["mapping"].values()))
        self.records["alice"]["credential_revision"] = "revision-2"
        with self.assertRaises(service.CapabilityError):
            service.validate("alice", info["capability_id"], info["version"])
        metadata = json.loads(self.records["alice"]["metadata_json"])
        metadata["tools"][-1]["enabled"] = False
        self.records["alice"]["metadata_json"] = json.dumps(metadata)
        with self.assertRaises(service.CapabilityError):
            service.load("alice", [info["capability_id"]])

    def test_schema_drift_fails_closed_before_call(self):
        info = next(iter(service.load("alice", ["mcp:connector_alice:query_99"])["mapping"].values()))
        raw = {"name": "query_99", "title": "考勤异常诊断", "description": "Find records",
               "inputSchema": info["input_schema"], "annotations": {"readOnlyHint": True}}
        client = Mock()
        client.list_tools.return_value = ([raw], {})
        service.verify_remote_schema(client, info)
        client.list_tools.return_value = ([raw], {"version": "2"})
        with self.assertRaises(service.CapabilityError):
            service.verify_remote_schema(client, info)
        client.list_tools.return_value = ([raw], {})
        raw["inputSchema"] = {"type": "object"}
        with self.assertRaises(service.CapabilityError):
            service.verify_remote_schema(client, info)
        client.call_tool.assert_not_called()

    def test_argument_validation_and_remote_references(self):
        info = next(iter(service.load("alice", ["mcp:connector_alice:query_99"])["mapping"].values()))
        service.validate_arguments(info, {"record_id": 10})
        with self.assertRaises(service.CapabilityError):
            service.validate_arguments(info, {"record_id": "ten"})
        with self.assertRaises(service.CapabilityError):
            service.validate_arguments({"input_schema": {"$ref": "https://example.com/schema"}}, {})

    def _manifest(self, directory, **extra):
        manifest = {"id": "attendance", "title": "考勤异常处理", "summary": "诊断昨天异常",
                    "keywords": ["签到", "考勤"], "instructions": "先读取规则，不猜测未知原因。",
                    "tools": [{"connector_name": "Example", "tool": "query_99"}], **extra}
        path = Path(directory) / (manifest["id"] + ".json")
        path.write_text(json.dumps(manifest, ensure_ascii=False))
        return path

    def test_trusted_skill_loads_dependencies_and_own_revision(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LUMA_SKILL_PATHS": directory}):
            self._manifest(directory)
            found = service.search("alice", "昨天考勤异常")
            self.assertTrue(any(x["kind"] == "skill" for x in found))
            loaded = service.load("alice", ["skill:attendance"])
            self.assertEqual(len(loaded["definitions"]), 1)
            self.assertEqual(loaded["skills"][0]["tool_ids"], ["mcp:connector_alice:query_99"])

    def test_skill_audience_and_missing_dependencies_are_not_discoverable(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LUMA_SKILL_PATHS": directory}):
            self._manifest(directory, allowed_users=["alice"])
            self.assertFalse(any(x["kind"] == "skill" for x in service.search("bob", "考勤")))
            self._manifest(directory, tools=[{"connector_name": "Unavailable", "tool": "query_99"}])
            with self.assertRaises(service.CapabilityError):
                service.load("alice", ["skill:attendance"])

    def test_ambiguous_connector_names_hide_skill(self):
        second = record()
        second["id"] = "second"
        self.patch_rows.stop()
        with patch.object(service, "_rows", return_value=[self.records["alice"], second]), \
             tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LUMA_SKILL_PATHS": directory}):
            self._manifest(directory)
            with self.assertRaises(service.CapabilityError):
                service.load("alice", ["skill:attendance"])

    def test_skill_count_and_context_size_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LUMA_SKILL_PATHS": directory}):
            for i in range(3):
                self._manifest(directory, id="skill%d" % i)
            with self.assertRaises(service.CapabilityError):
                service.load("alice", ["skill:skill%d" % i for i in range(3)])
        metadata = json.loads(self.records["alice"]["metadata_json"])
        for tool in metadata["tools"]:
            tool["input_schema"] = {"type": "object", "description": "x" * 10000}
        self.records["alice"]["metadata_json"] = json.dumps(metadata)
        with self.assertRaises(service.CapabilityError):
            service.load("alice", ["mcp:connector_alice:query_%d" % i for i in range(4)])

    def test_write_is_marked_for_actual_confirmation(self):
        metadata = json.loads(self.records["alice"]["metadata_json"])
        metadata["tools"][-1]["annotations"] = {"readOnlyHint": False}
        self.records["alice"]["metadata_json"] = json.dumps(metadata)
        info = next(iter(service.load("alice", ["mcp:connector_alice:query_99"])["mapping"].values()))
        self.assertTrue(info["requires_confirmation"])

    def test_remote_result_budget_preserves_valid_json_and_reports_omission(self):
        ctx = Mock(capability_result_chars=0)
        payload = {"rows": [{"value": "x" * 100} for _ in range(1000)]}
        first = service.bound_result(ctx, payload)
        self.assertLessEqual(len(first), service.MAX_RESULT_CHARS)
        self.assertIn("_truncated", json.loads(first))
        second = service.bound_result(ctx, payload)
        self.assertLessEqual(len(first) + len(second), service.MAX_TURN_RESULT_CHARS)
        ctx.capability_result_chars = service.MAX_TURN_RESULT_CHARS
        self.assertIn("预算已用完", service.bound_result(ctx, payload))


class CatalogPaginationTests(unittest.TestCase):
    def _client(self, pages):
        client = object.__new__(mcp.MCPClient)
        client.server_info = {}
        client._post = Mock(side_effect=pages)
        return client

    def test_transport_preserves_large_catalog(self):
        client = self._client([{"tools": [{"name": "query_%d" % i} for i in range(100)]}])
        tools, _ = client.list_tools()
        self.assertEqual(len(tools), 100)
        self.assertEqual(len(mcp.normalize_tools(tools)), 100)

    def test_duplicate_cursor_and_oversized_catalog_are_rejected(self):
        for pages in ([{"tools": [], "nextCursor": "again"}] * 2,
                      [{"tools": [{"name": "q"}] * (mcp.MCP_CATALOG_MAX_TOOLS + 1)}]):
            with self.assertRaises(mcp.MCPError):
                self._client(pages).list_tools()

    def test_successful_response_cannot_echo_bound_credentials(self):
        client = object.__new__(mcp.MCPClient)
        client.headers = {"Authorization": "Bearer synthetic-private-token-123456"}
        client._post = Mock(return_value={"content": [{"type": "text", "text":
            "synthetic-private-token-123456 " + "x" * 6000}]})
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "4000"}):
            value = client.call_tool("query", {})
        self.assertNotIn("synthetic-private-token", value)
        self.assertLessEqual(len(value), 4000)
