"""Browser authorization and credential-free model/stream boundaries."""

import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.pg import reset_tables  # Initialize the isolated test configuration.
from app.agent import loop, policy
from app.agent.tools import AgentContext, Tool, ToolResult
from app.services import browser_events, generation, permissions
from app import runtime
from app.db import get_connection
from app.widgets import apply_event, create_confirm_widget


class BrowserPolicyTests(unittest.TestCase):
    def tool(self, name):
        return Tool(name, name, {"type": "object"}, "external_write" if name == "browser.submit" else "read", lambda *_: None)

    def decide(self, name, *, submits=True, always_confirm=False, always=False, confirmed=False, failure=None):
        ctx = AgentContext("owner")
        ctx.confirmed = confirmed
        inspect = AsyncMock(return_value={"submits": submits, "always_confirm": always_confirm, "fingerprint": "page-element"})
        if failure:
            inspect.side_effect = failure
        with patch("app.agent.policy._audit"), patch("app.services.browser.inspect_click", inspect), patch("app.services.permissions.permission_mode", return_value="always" if always else "ask"):
            result = asyncio.run(policy.decide(ctx, self.tool(name), {"selector": "button"}))
        return result, ctx, inspect

    def test_read_tools_execute_without_browser_submit_permission(self):
        for name in ("browser.open", "browser.read", "browser.screenshot", "browser.type", "browser.scroll", "browser.live"):
            result, _, inspected = self.decide(name)
            self.assertEqual(result.decision, "allow", name)
            inspected.assert_not_awaited()

    def test_non_submitting_click_is_read_only(self):
        result, ctx, inspected = self.decide("browser.click", submits=False)
        self.assertEqual(result.decision, "allow")
        self.assertFalse(ctx.browser_approval["allow_submission"])
        inspected.assert_awaited_once_with("owner", {"selector": "button"})

    def test_click_and_submit_default_ask_and_saved_always(self):
        for name in ("browser.click", "browser.submit"):
            result, ctx, _ = self.decide(name)
            self.assertEqual(result.decision, "confirm", name)
            self.assertEqual(result.permission_key, "browser.submit")
            self.assertTrue(result.allow_always)
            self.assertIsNone(getattr(ctx, "browser_approval", None))
            result, ctx, _ = self.decide(name, always=True)
            self.assertEqual(result.decision, "allow", name)
            self.assertTrue(ctx.browser_approval["allow_submission"])
            self.assertEqual(ctx.browser_approval["fingerprint"], "page-element")

    def test_payment_always_requires_one_call_confirmation(self):
        for name in ("browser.click", "browser.submit"):
            result, _, _ = self.decide(name, always=True, always_confirm=True)
            self.assertEqual(result.decision, "confirm", name)
            self.assertFalse(result.allow_always)
            result, ctx, _ = self.decide(name, always=True, always_confirm=True, confirmed=True)
            self.assertEqual(result.decision, "allow", name)
            self.assertFalse(result.allow_always)
            self.assertTrue(ctx.browser_approval["confirmed"])

    def test_inspection_failure_is_a_safe_denial(self):
        result, ctx, _ = self.decide("browser.click", failure=RuntimeError("private-token"))
        self.assertEqual(result.decision, "deny")
        self.assertNotIn("private-token", result.reason)
        self.assertIsNone(getattr(ctx, "browser_approval", None))

    def test_browser_submit_settings_default_ask(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        conn.execute.return_value.fetchall.return_value = []
        conn.__enter__.return_value = conn
        with patch.object(permissions, "get_connection", return_value=conn), patch.object(permissions, "_mcp_specs", return_value=[]):
            self.assertEqual(permissions.permission_mode("owner", "browser.submit"), "ask")
            items = {item["key"]: item for item in permissions.permission_items("owner")}
        self.assertEqual(items["browser.submit"]["mode"], "ask")
        self.assertTrue(items["browser.submit"]["allow_always"])

    def test_changed_dom_cannot_use_waiting_confirmation(self):
        ctx = AgentContext("owner")
        ctx.confirmed = True
        ctx.browser_expected_fingerprint = "original-element"
        inspect = AsyncMock(return_value={"submits": True, "always_confirm": False, "fingerprint": "changed-element"})
        with patch("app.agent.policy._audit"), patch("app.services.browser.inspect_click", inspect):
            result = asyncio.run(policy.decide(ctx, self.tool("browser.click"), {"selector": "button"}))
        self.assertEqual(result.decision, "deny")
        self.assertIsNone(getattr(ctx, "browser_approval", None))


class BrowserPrivacyTests(unittest.TestCase):
    live_url = "https://demo.tencentags.com/novnc/vnc_lite.html?access_token=private-browser-token"

    def test_live_tool_does_not_send_credentials_to_model_or_outcome(self):
        events, model_inputs = [], []

        async def execute(ctx, args):
            return ToolResult(text=self.live_url, data={"kind": "browser_live", "url": self.live_url, "expires_in": 120})

        tool = Tool("browser.live", "live", {"type": "object"}, "read", execute)

        async def provider(messages, tools=None):
            model_inputs.append(copy.deepcopy(messages))
            if len(model_inputs) == 1:
                yield {"type": "tool_calls", "calls": [{"id": "live", "name": "browser.live", "arguments": "{}"}]}
            else:
                yield {"type": "text", "content": "已打开实时画面"}

        ctx = loop.AgentContext("owner")
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        with patch.object(loop, "provider_astream_chat", provider), patch("app.agent.policy._audit"):
            outcome = asyncio.run(loop.run_agent(ctx, [{"role": "user", "content": "观看"}], emit=events.append))
        self.assertNotIn("private-browser-token", json.dumps(model_inputs + outcome.messages + outcome.tool_calls))
        self.assertIn(browser_events.BROWSER_LIVE_TEXT, json.dumps(outcome.messages, ensure_ascii=False))
        self.assertEqual(next(item for item in events if isinstance(item.get("data"), dict))["data"]["url"], self.live_url)

    def test_generation_redis_and_replay_history_never_store_live_url(self):
        writes = []

        async def run():
            manager = generation.GenerationManager()

            async def redis(method, *args, **kwargs):
                writes.append((method, args, kwargs))
                return "1-0"

            manager._redis_quick = redis
            data = {"tool": "browser.live", "data": {"kind": "browser_live", "url": self.live_url, "expires_in": 120}}
            await manager.publish("owned-message", "tool", data)
            self.assertNotIn("private-browser-token", json.dumps(writes + list(manager.histories.values())))
            self.assertEqual(data["data"]["url"], self.live_url)
            return manager.histories["owned-message"][0][2]

        marker = asyncio.run(run())
        self.assertEqual(marker["data"], {"kind": "browser_live", "expires_in": 120})

    def test_owner_event_rehydrates_only_for_authorized_transport_user(self):
        marker = {"tool": "browser.live", "data": {"kind": "browser_live", "expires_in": 120}}
        live = AsyncMock(return_value={"kind": "browser_live", "expires_in": 120, "url": self.live_url})
        with patch("app.services.browser.live", live):
            hydrated = asyncio.run(browser_events.owner_browser_event("tool", marker, "owner"))
        live.assert_awaited_once_with("owner")
        self.assertEqual(hydrated["data"]["url"], self.live_url)
        self.assertNotIn("url", marker["data"])

    def test_live_rehydration_error_does_not_expose_provider_exception(self):
        marker = {"tool": "browser.live", "data": {"kind": "browser_live", "expires_in": 120}}
        with patch("app.services.browser.live", AsyncMock(side_effect=RuntimeError(self.live_url))):
            hydrated = asyncio.run(browser_events.owner_browser_event("tool", marker, "owner"))
        self.assertEqual(hydrated["status"], "error")
        self.assertNotIn("private-browser-token", json.dumps(hydrated))

    def test_runtime_browser_group_uses_registered_namespaced_tools(self):
        tools = [Tool("browser.open", "open", {"type": "object"}, "read", lambda *_: None),
                 Tool("browser.submit", "submit", {"type": "object"}, "external_write", lambda *_: None)]
        with patch("app.agent.tools.registry_for", return_value=(tools, [])):
            self.assertEqual(runtime._allowed_tools({"user_id": "owner", "allowed_tools": ["browser"]}), ["browser.open", "browser.submit"])
        self.assertTrue(runtime.tool_descriptor("browser")["enabled"])

    def test_same_round_browser_calls_keep_individual_dom_approval_markers(self):
        executed = []

        async def inspect(user_id, args):
            return {"submits": False, "always_confirm": False, "fingerprint": args["selector"]}

        async def execute(ctx, args):
            marker = ctx.browser_approval
            executed.append((args["selector"], marker["fingerprint"], marker["arguments"]))
            del ctx.browser_approval
            return ToolResult(text="ok")

        tool = Tool("browser.click", "click", {"type": "object"}, "read", execute)
        calls = []

        async def provider(messages, tools=None):
            calls.append(True)
            if len(calls) == 1:
                yield {"type": "tool_calls", "calls": [{"id": "one", "name": "browser.click", "arguments": {"selector": "first"}},
                                                         {"id": "two", "name": "browser.click", "arguments": {"selector": "second"}}]}
            else:
                yield {"type": "text", "content": "完成"}

        ctx = loop.AgentContext("owner")
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        with patch.object(loop, "provider_astream_chat", provider), patch("app.agent.policy._audit"), patch("app.services.browser.inspect_click", inspect):
            outcome = asyncio.run(loop.run_agent(ctx, [{"role": "user", "content": "操作"}]))
        self.assertEqual(executed, [("first", "first", {"selector": "first"}), ("second", "second", {"selector": "second"})])
        self.assertTrue(all(item.get("status") == "ok" for item in outcome.tool_calls))


class BrowserWidgetPrivacyTests(unittest.TestCase):
    def setUp(self):
        reset_tables()
        self.user_id = "local"
        self.session_id = "browser-widget-session"
        self.message_id = "browser-widget-message"
        self.timestamp = "2026-10-05T00:00:00+00:00"
        with get_connection() as conn:
            conn.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                         (self.session_id, self.user_id, "browser", self.timestamp, self.timestamp))
            conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,metadata_json,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (self.message_id, self.user_id, self.session_id, "assistant", "", "{}", "complete", self.timestamp))

    def pending(self):
        executor = AsyncMock(return_value=ToolResult(text="submitted"))
        tool = Tool("browser.submit", "submit", {"type": "object"}, "external_write", executor)

        async def provider(messages, tools=None):
            yield {"type": "tool_calls", "calls": [{"id": "submit", "name": "browser.submit", "arguments": {"selector": "form"}}]}

        ctx = loop.AgentContext(self.user_id, session_id=self.session_id, assistant_message_id=self.message_id)
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        inspect = AsyncMock(return_value={"submits": True, "always_confirm": False, "fingerprint": "original-form"})
        with patch.object(loop, "provider_astream_chat", provider), patch("app.agent.policy._audit"), patch("app.services.browser.inspect_click", inspect), patch("app.services.permissions.permission_mode", return_value="ask"):
            outcome = asyncio.run(loop.run_agent(ctx, [{"role": "user", "content": "提交"}]))
        self.assertEqual(outcome.status, "waiting_confirmation")
        with get_connection() as conn:
            row = conn.execute("SELECT id,state_json FROM widgets WHERE user_id = ? AND message_id = ?",
                               (self.user_id, self.message_id)).fetchone()
        self.assertEqual(json.loads(row["state_json"])["_pending"]["browser_fingerprint"], "original-form")
        return row["id"], tool, executor

    def test_waiting_confirmation_persists_only_hash_and_refuses_changed_form(self):
        widget_id, tool, executor = self.pending()
        inspect = AsyncMock(return_value={"submits": True, "always_confirm": True, "fingerprint": "different-payment-form"})
        with patch("app.agent.tools.registry_for", return_value=([tool], [])), patch("app.agent.policy._audit"), patch("app.services.browser.inspect_click", inspect):
            widget, _ = apply_event(self.user_id, widget_id, "confirm", None)
        self.assertEqual(widget["state"]["status"], "failed")
        executor.assert_not_awaited()

    def test_unchanged_form_confirmation_executes_once(self):
        from fastapi import HTTPException

        widget_id, tool, executor = self.pending()
        inspect = AsyncMock(return_value={"submits": True, "always_confirm": False, "fingerprint": "original-form"})
        with patch("app.agent.tools.registry_for", return_value=([tool], [])), patch("app.agent.policy._audit"), patch("app.services.browser.inspect_click", inspect):
            widget, _ = apply_event(self.user_id, widget_id, "confirm", None)
            with self.assertRaises(HTTPException):
                apply_event(self.user_id, widget_id, "confirm", None)
        self.assertEqual(widget["state"]["status"], "done")
        executor.assert_awaited_once()
        marker = executor.call_args.args[0].browser_approval
        self.assertTrue(marker["confirmed"])
        self.assertTrue(marker["allow_submission"])

    def test_manufactured_live_confirmation_never_persists_live_credentials(self):
        live_url = BrowserPrivacyTests.live_url
        tool = Tool("browser.live", "live", {"type": "object"}, "read", AsyncMock(return_value=ToolResult(text=live_url, data={"kind": "browser_live", "url": live_url})))
        widget = create_confirm_widget(self.user_id, self.session_id, self.message_id, connector_id="", connector_name="browser", tool="browser.live", title="live", arguments={})
        with patch("app.agent.tools.registry_for", return_value=([tool], [])), patch("app.agent.policy._audit"):
            result, _ = apply_event(self.user_id, widget["id"], "confirm", None)
        self.assertEqual(result["state"]["status"], "done")
        with get_connection() as conn:
            row = conn.execute("SELECT state_json FROM widgets WHERE id = ?", (widget["id"],)).fetchone()
            message = conn.execute("SELECT content,metadata_json FROM messages WHERE id = ?", (self.message_id,)).fetchone()
        self.assertNotIn("private-browser-token", row["state_json"] + json.dumps(dict(message)))
        self.assertEqual(json.loads(row["state_json"])["_result"], browser_events.BROWSER_LIVE_TEXT)

    def test_another_user_cannot_resume_or_hydrate_live_event(self):
        from fastapi.testclient import TestClient
        from app import main

        live = AsyncMock(return_value={"kind": "browser_live", "url": BrowserPrivacyTests.live_url})
        with patch("app.routers.chat.owner_id", return_value="other-user"), patch("app.services.browser.live", live):
            response = TestClient(main.app).get("/api/v1/messages/%s/stream" % self.message_id)
        self.assertEqual(response.status_code, 404)
        live.assert_not_awaited()


class BrowserBackgroundApprovalTests(unittest.TestCase):
    def setUp(self):
        reset_tables()
        self.user_id = "local"
        self.args = {"selector": "button"}
        self.tool = Tool("browser.submit", "submit", {"type": "object"}, "external_write", AsyncMock(return_value=ToolResult(text="submitted")))
        self.job = runtime.create_job("agent_run", {"prompt": "submit", "allowed_tools": [self.tool.name]},
                                      requires_approval=False, user_id=self.user_id)
        self.payload = {"prompt": "submit", "allowed_tools": [self.tool.name], "job_id": self.job["id"]}

    def approved(self, fingerprint="original-element"):
        approval = runtime.create_approval(self.job["id"], self.tool.name, self.args, self.user_id,
                                           browser_fingerprint=fingerprint)
        runtime.decide_approval(approval["id"], "approved", user_id=self.user_id)
        return approval["id"]

    def invoke(self, fake_agent, *, fingerprint="original-element", payment=True):
        inspect = AsyncMock(return_value={"submits": True, "always_confirm": payment, "fingerprint": fingerprint})
        with patch("app.agent.loop.run_agent", fake_agent), patch("app.agent.tools.registry_for", return_value=([self.tool], [])), patch("app.services.browser.inspect_click", inspect), patch("app.services.permissions.permission_mode", return_value="ask"), patch("app.agent.policy._audit"):
            return runtime._execute_agent_run(self.payload, self.user_id)

    def status(self, approval_id):
        with get_connection() as conn:
            return conn.execute("SELECT status FROM runtime_approvals WHERE id = ?", (approval_id,)).fetchone()["status"]

    def test_payment_approval_is_spent_across_job_resumes_and_context_confirmed_cannot_bypass(self):
        from app.models import RuntimeApproval

        approval_id = self.approved()
        decisions = []

        async def agent(ctx, messages, emit=None):
            ctx.confirmed = True
            decision = await policy.decide(ctx, self.tool, self.args)
            decisions.append(decision.decision)
            return loop.AgentOutcome()

        self.invoke(agent)
        self.invoke(agent)
        self.assertEqual(decisions, ["allow", "confirm"])
        self.assertEqual(self.status(approval_id), "consumed")
        public = next(item for item in runtime.list_approvals(user_id=self.user_id) if item["id"] == approval_id)
        self.assertEqual(RuntimeApproval.model_validate(public).status, "consumed")
        self.assertNotIn("_browser_guard", public["payload"])

    def test_two_workers_with_same_loaded_approval_can_authorize_only_one_call(self):
        approval_id = self.approved()
        contexts = []

        async def capture(ctx, messages, emit=None):
            contexts.append(ctx)
            return loop.AgentOutcome()

        self.invoke(capture)
        self.invoke(capture)
        inspector = AsyncMock(return_value={"submits": True, "always_confirm": True, "fingerprint": "original-element"})
        with patch("app.services.browser.inspect_click", inspector), patch("app.agent.policy._audit"):
            first = asyncio.run(policy.decide(contexts[0], self.tool, self.args))
            second = asyncio.run(policy.decide(contexts[1], self.tool, self.args))
        self.assertEqual((first.decision, second.decision), ("allow", "confirm"))
        self.assertEqual(self.status(approval_id), "consumed")

    def test_changed_element_expires_waiting_approval_and_needs_new_confirmation(self):
        approval_id = self.approved()
        decisions = []

        async def agent(ctx, messages, emit=None):
            decisions.append((await policy.decide(ctx, self.tool, self.args)).decision)
            return loop.AgentOutcome()

        self.invoke(agent, fingerprint="different-element")
        self.assertEqual(decisions, ["confirm"])
        self.assertEqual(self.status(approval_id), "expired")

    def test_background_submission_asks_and_persists_server_observed_private_hash(self):
        approval_ids = []

        async def agent(ctx, messages, emit=None):
            self.assertEqual((await policy.decide(ctx, self.tool, self.args)).decision, "confirm")
            approval_ids.append((await ctx.request_approval(self.tool, self.args))["approval_id"])
            return loop.AgentOutcome(status="waiting_approval")

        self.invoke(agent, payment=False)
        with get_connection() as conn:
            row = conn.execute("SELECT payload_json FROM runtime_approvals WHERE id = ?", (approval_ids[0],)).fetchone()
        saved = json.loads(row["payload_json"])
        self.assertEqual(saved["_browser_guard"]["fingerprint"], "original-element")
        self.assertEqual(saved["selector"], "button")
        public = next(item for item in runtime.list_approvals(user_id=self.user_id) if item["id"] == approval_ids[0])
        self.assertEqual(public["payload"], self.args)

    def test_model_cannot_use_approval_for_different_arguments(self):
        approval_id = self.approved()
        decisions = []

        async def agent(ctx, messages, emit=None):
            decisions.append((await policy.decide(ctx, self.tool, {"selector": "another-button"})).decision)
            return loop.AgentOutcome()

        self.invoke(agent)
        self.assertEqual(decisions, ["confirm"])
        self.assertEqual(self.status(approval_id), "approved")
