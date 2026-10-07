"""JSON-safe result limits across MCP executors and the model loop."""

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# Use the suite's shared isolated schema before importing app executors.
import tests.pg

from app import mcp
from app.agent import loop, tools
from app.tool_results import tool_result_max_chars, tool_result_text


class ToolResultTests(unittest.TestCase):
    def test_environment_default_invalid_and_bounds(self):
        for setting, expected in [(None, 48000), ("invalid", 48000), ("8000", 8000),
                                  ("3999", 4000), ("-1", 4000), ("200001", 200000)]:
            with self.subTest(setting=setting), patch.dict(os.environ):
                if setting is None:
                    os.environ.pop("AGENT_TOOL_RESULT_MAX_CHARS", None)
                else:
                    os.environ["AGENT_TOOL_RESULT_MAX_CHARS"] = setting
                self.assertEqual(tool_result_max_chars(), expected)
                text = tool_result_text("字" * (expected + 100))
                self.assertEqual(len(text), expected)
                self.assertTrue(text.endswith("…（已截断，共 %d 字）" % (expected + 100)))

    def test_json_is_compacted_before_limiting(self):
        value = {"rows": [{"day": "周一", "count": index} for index in range(75)]}
        pretty = json.dumps(value, ensure_ascii=False, indent=8)
        self.assertGreater(len(pretty), 4000)
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "4000"}):
            text = tool_result_text(pretty)
        self.assertEqual(text, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(json.loads(text), value)
        self.assertNotIn("_truncated", text)

    def test_large_arrays_keep_complete_rows_and_maximum_prefix(self):
        rows = [{"day": index, "detail": "逐日数据" * 50} for index in range(50)]
        for key in ("rows", "data", "items", "tasks"):
            with self.subTest(key=key), patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "4000"}):
                value = {"unit": "人", key: rows}
                text = tool_result_text(value)
                parsed = json.loads(text)
                kept = parsed["_truncated"]["kept"]
                self.assertLessEqual(len(text), 4000)
                self.assertGreater(kept, 0)
                self.assertLess(kept, len(rows))
                self.assertEqual(parsed[key], rows[:kept])
                self.assertEqual(parsed["_truncated"], {"kept": kept, "total": len(rows)})
                parsed[key] = rows[:kept + 1]
                parsed["_truncated"]["kept"] = kept + 1
                self.assertGreater(len(json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)), 4000)
                self.assertEqual(value[key], rows)

    def test_nested_rows_and_multiple_arrays_remain_valid(self):
        rows = [{"description": "完整行" * 100} for _ in range(40)]
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "4000"}):
            text = tool_result_text({"result": {"data": {"rows": rows}}, "items": rows, "unit": "条"})
        parsed = json.loads(text)
        kept = len(parsed["result"]["data"]["rows"]) + len(parsed["items"])
        self.assertLessEqual(len(text), 4000)
        self.assertEqual(parsed["unit"], "条")
        self.assertEqual(parsed["_truncated"], {"kept": kept, "total": 80})

    def test_top_level_arrays_have_a_valid_truncation_envelope(self):
        rows = [{"description": "完整行" * 100} for _ in range(40)]
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "4000"}):
            parsed = json.loads(tool_result_text(rows))
        self.assertEqual(parsed["items"], rows[:parsed["_truncated"]["kept"]])
        self.assertEqual(parsed["_truncated"]["total"], len(rows))

    def test_truncation_counts_also_include_unchanged_arrays(self):
        rows = [{"detail": "数据" * 100} for _ in range(40)]
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "4000"}):
            parsed = json.loads(tool_result_text({"rows": rows, "items": [{"id": 1}]}))
        self.assertEqual(parsed["items"], [{"id": 1}])
        self.assertEqual(parsed["_truncated"], {"kept": len(parsed["rows"]) + 1, "total": 41})

    def test_oversized_metadata_and_single_rows_use_legal_json(self):
        values = [{"rows": [{"detail": "字" * 8000}], "unit": "条"},
                  {"rows": [{"day": 1}], "description": "字" * 8000, "unit": "条"},
                  {"items": [], "description": "字" * 8000, "unit": "条"}, "字" * 8000]
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "4000"}):
            for value in values:
                with self.subTest(value_type=type(value).__name__):
                    raw = json.dumps(value, ensure_ascii=False)
                    text = tool_result_text(raw)
                    parsed = json.loads(text)
                    self.assertLessEqual(len(text), 4000)
                    self.assertIn("_truncated", parsed)
                    if isinstance(value, dict):
                        self.assertEqual(parsed["unit"], "条")
                        self.assertNotIn("description", parsed)
                        if "description" in value:
                            self.assertEqual(parsed.get("rows"), value.get("rows"))

    def test_repeated_limits_preserve_original_total(self):
        value = {"rows": [{"detail": "数据" * 100} for _ in range(100)]}
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "8000"}):
            text = tool_result_text(value)
            self.assertEqual(tool_result_text(text), text)
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "4000"}):
            smaller = tool_result_text(text)
            self.assertEqual(tool_result_text(smaller), smaller)
        self.assertEqual(json.loads(smaller)["_truncated"]["total"], 100)

    def test_field_omissions_keep_original_totals_when_limit_changes(self):
        value = {"large": "字" * 9000, "medium": "字" * 5000, "unit": "条"}
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "8000"}):
            text = tool_result_text(value)
            self.assertEqual(json.loads(text)["_truncated"]["total"], 3)
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "4000"}):
            smaller = tool_result_text(text)
            self.assertEqual(tool_result_text(smaller), smaller)
        self.assertEqual(json.loads(smaller)["_truncated"], {"kept": 1, "total": 3, "omitted_fields": 2})

    def test_mcp_text_resource_and_structured_results_share_the_limit(self):
        value = {"rows": [{"detail": "数据" * 100} for _ in range(100)]}
        raw = json.dumps(value, ensure_ascii=False, indent=2)
        envelopes = [{"content": [{"type": "text", "text": raw}]},
                     {"content": [{"type": "resource", "resource": {"text": raw}}]},
                     {"structuredContent": value}, list(value["rows"])]
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "8000"}):
            for envelope in envelopes:
                text = mcp.result_to_text(envelope)
                self.assertLessEqual(len(text), 8000)
                self.assertEqual(json.loads(text)["_truncated"]["total"], 100)

    def test_default_keeps_seven_days_through_executor_and_loop(self):
        value = {"rows": [{"day": day, "detail": "数据" * 1400} for day in range(7)]}
        raw = json.dumps(value, ensure_ascii=False, indent=2)
        expected = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        self.assertGreater(len(expected), 16 * 1024)
        client = SimpleNamespace(call_tool=lambda name, args: mcp.result_to_text({"content": [{"type": "text", "text": raw}]}))
        ctx = tools.AgentContext("fixture", mcp_clients={"connector": client})
        with patch.dict(os.environ, {"AGENT_TOOL_RESULT_MAX_CHARS": "48000"}), \
                patch.object(tools, "get_connection") as connection, \
                patch.object(tools, "connector_from_id", return_value={"endpoint": "https://example.com/mcp"}):
            connection.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {"ciphertext": "unused", "endpoint": "https://example.com/mcp", "kind": "mcp", "enabled": 1}
            result = asyncio.run(tools._mcp_executor(ctx, {}, {"connector_id": "connector", "tool": "sample_semantic_query"}))
            text, data, status = loop._as_tool_result(result)
            builtin_text = loop._as_tool_result(tools._result(value))[0]
        self.assertEqual(status, "ok")
        self.assertEqual(text, expected)
        self.assertEqual(builtin_text, expected)
        self.assertEqual(data["result"], expected)
        self.assertEqual(json.loads(text), value)
