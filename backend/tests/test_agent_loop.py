"""Focused coverage for the shared Agent Loop and optional decider."""

import asyncio
import json
import os
import pathlib
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

# Initialise the isolated PostgreSQL schema before importing app modules.
from tests.pg import reset_tables

from app.agent import loop
from app.agent import decider
from app.agent import policy
from app.agent import tools as agent_tools
from app.agent.loop import AgentContext, run_agent
from app.agent.tools import Tool, ToolResult
from app.db import get_connection
from app import runtime
from app.widgets import apply_event, create_confirm_widget


async def _one(item):
    yield item


class AgentLoopTests(unittest.TestCase):
    def _run(self, coroutine):
        return asyncio.run(coroutine)

    def test_allow_calls_run_in_parallel_and_keep_order(self):
        events = []

        async def execute(ctx, args):
            await asyncio.sleep(0.5)
            return ToolResult(text=args["value"], data={"value": args["value"]})

        tools = [
            Tool("test.one", "one", {"type": "object"}, "read", execute),
            Tool("test.two", "two", {"type": "object"}, "read", execute),
        ]
        definitions = [item.openai_definition() for item in tools]
        rounds = iter([
            [{"type": "tool_calls", "calls": [
                {"id": "a", "name": "test.one", "arguments": '{"value":"one"}'},
                {"id": "b", "name": "test.two", "arguments": '{"value":"two"}'},
            ]}],
            [{"type": "text", "content": "done"}],
            [{"type": "text", "content": "done"}],
        ])

        async def provider(messages, tools=None):
            for item in next(rounds):
                yield item

        def registry(user_id, mode="interactive"):
            return tools, definitions

        ctx = AgentContext("user", mode="interactive")
        ctx.registry_for = registry
        started = time.perf_counter()
        with patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "go"}], emit=events.append))
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.9)
        self.assertEqual(outcome.reply, "done")
        tool_messages = [item for item in outcome.messages if item.get("role") == "tool"]
        self.assertEqual([item["tool_call_id"] for item in tool_messages], ["a", "b"])

    def test_tool_round_prose_is_discarded_and_final_answer_streams_live(self):
        events = []
        received = []
        tools = [
            Tool("test.search", "search", {"type": "object"}, "read", lambda *_: ToolResult(text='{"rows":[{"count":7}]}')),
            Tool("test.get", "get", {"type": "object"}, "read", lambda *_: ToolResult(text='{"count":7}')),
        ]
        rounds = iter([
            [{"type": "text", "content": "好的我来查一下，先定位数据模型"}, {"type": "tool_calls", "calls": [{"id": "search", "name": "test.search", "arguments": "{}"}]}],
            [{"type": "text", "content": "再确认签到指标，然后出数"}, {"type": "tool_calls", "calls": [{"id": "get", "name": "test.get", "arguments": "{}"}]}],
            [{"type": "text", "content": "不要展示的最终草稿"}],
        ])

        async def provider(messages, tools=None):
            received.append((messages, tools))
            if tools is not None:
                for item in next(rounds):
                    yield item
                return
            self.assertEqual(messages[-1]["role"], "system")
            self.assertNotIn("最终草稿", json.dumps(messages, ensure_ascii=False))
            yield {"type": "text", "content": "签到结果："}
            # The first final chunk must be published before this stream
            # finishes, even though previous tool rounds were buffered.
            self.assertEqual([item["content"] for item in events if item["type"] == "delta"], ["签到结果："])
            yield {"type": "text", "content": '〔历史组件'}
            yield {"type": "text", "content": '记录：确认卡，结果：{"count":7}〕'}
            yield {"type": "text", "content": "7 人。"}

        ctx = AgentContext("user")
        ctx.registry_for = lambda user_id, mode="interactive": (tools, [tool.openai_definition() for tool in tools])
        with patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "查签到"}], emit=events.append))
        self.assertEqual(outcome.reply, "签到结果：7 人。")
        self.assertEqual("".join(item["content"] for item in events if item["type"] == "delta"), outcome.reply)
        self.assertTrue(any(item["type"] == "tool" and item["status"] == "running" for item in events))
        self.assertEqual(len(received), 4)
        assistant_turns = [item for item in outcome.messages if item["role"] == "assistant"]
        self.assertEqual([item["content"] for item in assistant_turns], ["", ""])
        for forbidden in ("先定位", "再确认", "最终草稿", "〔历史组件记录", '"count"'):
            self.assertNotIn(forbidden, outcome.reply)

    def test_no_tool_registry_streams_without_extra_provider_request(self):
        events = []
        calls = []

        async def provider(messages, tools=None):
            calls.append(tools)
            yield {"type": "text", "content": "第一段"}
            self.assertEqual(events, [{"type": "delta", "content": "第一段"}])
            yield {"type": "text", "content": "第二段"}

        ctx = AgentContext("user")
        ctx.registry_for = lambda user_id, mode="interactive": ([], [])
        with patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "你好"}], emit=events.append))
        self.assertEqual(outcome.reply, "第一段第二段")
        self.assertEqual(len(calls), 1)

    def test_invalid_luma_ui_is_ignored_and_model_can_answer_in_text(self):
        for arguments in ({}, {"spec": {}}, {"spec": None}, {"type": "html"}, {"type": []}):
            with self.subTest(arguments=arguments):
                events = []
                received = []
                tool = Tool("luma-ui", "UI", {"type": "object"}, "read", lambda *_: None)
                rounds = iter([
                    [{"type": "tool_calls", "calls": [{"id": "ui", "name": tool.name, "arguments": json.dumps(arguments)}]}],
                    [{"type": "text", "content": "最终答案草稿"}],
                    [{"type": "text", "content": "首页前五条标题如下。"}],
                ])

                async def provider(messages, tools=None):
                    received.append((messages, tools))
                    for item in next(rounds):
                        yield item

                ctx = AgentContext("user")
                ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
                with patch.object(loop, "provider_astream_chat", provider):
                    outcome = self._run(run_agent(ctx, [{"role": "user", "content": "查首页"}], emit=events.append))
                self.assertEqual(outcome.reply, "首页前五条标题如下。")
                self.assertEqual(outcome.status, "completed")
                self.assertFalse(any(event["type"] == "widget" for event in events))
                self.assertEqual(len(received), 3)
                self.assertIsNotNone(received[1][1])
                self.assertEqual(outcome.tool_calls[0]["status"], "error")
                self.assertEqual(
                    [message["content"] for message in outcome.messages if message["role"] == "tool"],
                    ["组件参数无效，已忽略；请直接用文字回答"],
                )

    def test_only_valid_luma_ui_gets_one_tools_disabled_final_answer(self):
        events = []
        received = []
        tool = Tool("luma-ui", "UI", {"type": "object"}, "read", lambda *_: None)
        spec = {"type": "choice", "options": [{"label": "选项一"}, {"label": "选项二"}], "html": "<script>ignored</script>"}

        async def provider(messages, tools=None):
            received.append((messages, tools))
            if len(received) == 1:
                yield {"type": "tool_calls", "calls": [{"id": "ui", "name": tool.name, "arguments": {"spec": spec}}]}
                return
            self.assertIsNone(tools)
            self.assertIn(loop._FINAL_ANSWER_INSTRUCTION, messages[-1]["content"])
            self.assertIn("组件已展示，用一两句话说明即可。", messages[-1]["content"])
            yield {"type": "text", "content": "请选择一个选项。"}

        ctx = AgentContext("user")
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        with patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "给我选项"}], emit=events.append))
        self.assertEqual(outcome.reply, "请选择一个选项。")
        self.assertEqual(len(received), 2)
        widgets = [event["spec"] for event in events if event["type"] == "widget"]
        self.assertEqual(len(widgets), 1)
        self.assertEqual(widgets[0]["type"], "choice")
        self.assertNotIn("html", widgets[0])

    def test_valid_luma_ui_with_other_tool_continues_tool_rounds(self):
        for other_name in ("test.read", "luma_ui"):
            with self.subTest(other_name=other_name):
                events = []
                received = []
                executed = []
                ui = Tool("luma-ui", "UI", {"type": "object"}, "read", lambda *_: None)
                other = Tool("test.read", "read", {"type": "object"}, "read", lambda *_: executed.append(True) or ToolResult(text="data"))
                tools = [ui, other]
                spec = {"type": "checklist", "items": [{"label": "任务"}]}
                rounds = iter([
                    [{"type": "tool_calls", "calls": [
                        {"id": "ui", "name": ui.name, "arguments": {"spec": spec}},
                        {"id": "other", "name": other_name, "arguments": {}},
                    ]}],
                    [{"type": "text", "content": "草稿"}],
                    [{"type": "text", "content": "这是操作结果。"}],
                ])

                async def provider(messages, tools=None):
                    received.append(tools)
                    for item in next(rounds):
                        yield item

                ctx = AgentContext("user")
                ctx.registry_for = lambda user_id, mode="interactive": (tools, [tool.openai_definition() for tool in tools])
                with patch.object(loop, "provider_astream_chat", provider):
                    outcome = self._run(run_agent(ctx, [{"role": "user", "content": "go"}], emit=events.append))
                self.assertEqual(outcome.reply, "这是操作结果。")
                self.assertEqual(len(received), 3)
                self.assertIsNotNone(received[1])
                self.assertEqual(len([event for event in events if event["type"] == "widget"]), 1)
                self.assertEqual(executed, [True] if other_name == "test.read" else [])

    def test_empty_completed_answer_retries_once_without_tools(self):
        for with_tools in (False, True):
            with self.subTest(with_tools=with_tools):
                events = []
                received = []
                tool = Tool("test.read", "read", {"type": "object"}, "read", lambda *_: ToolResult(text="ok"))

                async def provider(messages, tools=None):
                    received.append(tools)
                    if len(received) < (3 if with_tools else 2):
                        yield {"type": "text", "content": ""}
                        return
                    self.assertIsNone(tools)
                    self.assertIn(loop._FINAL_ANSWER_INSTRUCTION, messages[-1]["content"])
                    yield {"type": "text", "content": "操作已完成。"}

                ctx = AgentContext("user")
                ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()]) if with_tools else ([], [])
                with patch.object(loop, "provider_astream_chat", provider):
                    outcome = self._run(run_agent(ctx, [{"role": "user", "content": "go"}], emit=events.append))
                self.assertEqual(outcome.reply, "操作已完成。")
                self.assertEqual(len(received), 3 if with_tools else 2)
                self.assertEqual("".join(event["content"] for event in events if event["type"] == "delta"), outcome.reply)

    def test_empty_reply_recovery_uses_fixed_fallback_when_still_empty(self):
        for failure in ("empty", "whitespace", "error", "tool_calls"):
            with self.subTest(failure=failure):
                events = []
                received = []

                async def provider(messages, tools=None):
                    received.append(tools)
                    if len(received) == 1:
                        return
                    self.assertIsNone(tools)
                    if failure == "error":
                        raise RuntimeError("generation failed")
                    if failure == "tool_calls":
                        yield {"type": "tool_calls", "calls": [{"id": "extra", "name": "test.read", "arguments": {}}]}
                    else:
                        yield {"type": "text", "content": " \n" if failure == "whitespace" else ""}

                ctx = AgentContext("user")
                ctx.registry_for = lambda user_id, mode="interactive": ([], [])
                with patch.object(loop, "provider_astream_chat", provider):
                    outcome = self._run(run_agent(ctx, [{"role": "user", "content": "go"}], emit=events.append))
                self.assertEqual(outcome.status, "completed")
                self.assertEqual(outcome.reply, "已完成操作，但没有生成说明文字。")
                self.assertEqual(len(received), 2)
                self.assertEqual(events[-1], {"type": "delta", "content": outcome.reply})

    def test_hidden_definitions_retry_without_exposing_speculative_prose(self):
        events = []
        received = []
        executed = []
        tool = Tool("test.read", "read", {"type": "object"}, "read", lambda *_: executed.append(True) or ToolResult(text="ok"))
        call = {"type": "tool_calls", "calls": [{"id": "read", "name": tool.name, "arguments": "{}"}]}
        rounds = iter([
            [{"type": "text", "content": "先查工具"}, call],
            [call],
            [{"type": "text", "content": "draft"}],
            [{"type": "text", "content": "结果"}],
        ])

        async def provider(messages, tools=None):
            received.append(tools)
            for item in next(rounds):
                yield item

        async def skip_tools(messages):
            return False

        ctx = AgentContext("user")
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        with patch.object(loop, "provider_astream_chat", provider), patch.object(loop, "_decider_needs_tools", skip_tools):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "go"}], emit=events.append))
        self.assertEqual(outcome.reply, "结果")
        self.assertEqual(executed, [True])
        self.assertIsNone(received[0])
        self.assertTrue(received[1])
        self.assertIsNone(received[2])
        self.assertIsNone(received[3])
        self.assertEqual([item["content"] for item in events if item["type"] == "delta"], ["结果"])

    def test_final_stream_is_closed_when_generation_is_cancelled(self):
        closed = []

        async def check():
            started = asyncio.Event()
            waiting = asyncio.Event()
            tool = Tool("test.read", "read", {"type": "object"}, "read", lambda *_: ToolResult(text="ok"))

            async def provider(messages, tools=None):
                if tools is not None:
                    yield {"type": "text", "content": "draft"}
                    return
                try:
                    yield {"type": "text", "content": "结果"}
                    await waiting.wait()
                finally:
                    closed.append(True)

            def emit(event):
                if event["type"] == "delta":
                    started.set()

            ctx = AgentContext("user")
            ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
            with patch.object(loop, "provider_astream_chat", provider):
                task = asyncio.create_task(run_agent(ctx, [{"role": "user", "content": "go"}], emit=emit))
                await asyncio.wait_for(started.wait(), timeout=1.0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        self._run(check())
        self.assertEqual(closed, [True])

    def test_ordinary_remote_write_runs_without_confirmation(self):
        tool = Tool("mcp.demo.create", "create", {"type": "object"}, "external_write", lambda *_: ToolResult(text="created"))
        rounds = iter([
            {"type": "tool_calls", "calls": [{"id": "create", "name": tool.name, "arguments": "{}"}]},
            {"type": "text", "content": "draft"},
            {"type": "text", "content": "已创建"},
        ])

        async def provider(messages, tools=None):
            yield next(rounds)

        ctx = AgentContext("user")
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        with patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "create"}]))
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.reply, "已创建")
        self.assertEqual(outcome.tool_calls[0]["decision"], "allow")

    def test_ordinary_remote_write_respects_user_ask_permission(self):
        tool = Tool(
            "mcp.demo.create", "create", {"type": "object"}, "external_write", lambda *_: None,
            metadata={"connector_id": "demo", "mcp_name": "create"},
        )

        async def provider(messages, tools=None):
            yield {"type": "text", "content": "好的我来创建"}
            yield {"type": "tool_calls", "calls": [{"id": "create", "name": tool.name, "arguments": "{}"}]}

        ctx = AgentContext("user", session_id="s", assistant_message_id="a")
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        events = []
        with patch.object(loop, "provider_astream_chat", provider), patch.object(policy, "_permission_always", return_value=False):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "create"}], emit=events.append))
        self.assertEqual(outcome.status, "waiting_confirmation")
        self.assertEqual(outcome.reply, "")
        self.assertFalse(any(event["type"] == "delta" for event in events))

    def test_destructive_write_needs_confirmation(self):
        tools = [Tool("mcp.demo.delete_item", "delete", {"type": "object"}, "external_write", lambda *_: None)]
        call = {"type": "tool_calls", "calls": [{"id": "w", "name": tools[0].name, "arguments": "{}"}]}

        async def provider(messages, tools=None):
            yield call

        ctx = AgentContext("user", session_id="s", assistant_message_id="a")
        ctx.registry_for = lambda user_id, mode="interactive": (tools, [item.openai_definition() for item in tools])
        emitted = []
        with patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "write"}], emit=emitted.append))
        self.assertEqual(outcome.status, "waiting_confirmation")
        self.assertTrue(any(item.get("type") == "tool" and item.get("status") == "needs_confirmation" for item in emitted))

    def test_mcp_confirmation_widget_keeps_connector_and_remote_tool_identity(self):
        connector_id = "connector-" + uuid.uuid4().hex
        session_id = "session-" + uuid.uuid4().hex
        message_id = "message-" + uuid.uuid4().hex
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (session_id, "loop-user", "confirmation", "2026-10-04T00:00:00+00:00", "2026-10-04T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)",
                (message_id, "loop-user", session_id, "assistant", "confirmation", "2026-10-04T00:00:00+00:00", "{}"),
            )
        tool = Tool(
            "mcp.primary.write",
            "write",
            {"type": "object"},
            "external_write",
            lambda *_: None,
            metadata={"connector_id": connector_id, "connector": "Primary", "title": "远端写入", "mcp_name": "remote.write", "annotations": {"destructiveHint": True}},
        )
        call = {"type": "tool_calls", "calls": [{"id": "write", "name": tool.name, "arguments": "{}"}]}

        async def provider(messages, tools=None):
            yield call

        ctx = AgentContext("loop-user", session_id=session_id, assistant_message_id=message_id)
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        with patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "write"}]))
        self.assertEqual(outcome.status, "waiting_confirmation")
        widget = next(item for item in outcome.tool_calls if item["tool"] == tool.name)
        self.assertEqual(widget["decision"], "confirm")
        with get_connection() as conn:
            row = conn.execute("SELECT state_json FROM widgets WHERE user_id = ? AND session_id = ? ORDER BY created_at DESC LIMIT 1", ("loop-user", session_id)).fetchone()
        self.assertIsNotNone(row)
        pending = json.loads(row["state_json"])["_pending"]
        self.assertEqual(pending["connector_id"], connector_id)
        self.assertEqual(pending["tool"], "remote.write")
        private_state = json.loads(row["state_json"])
        self.assertEqual(private_state["_call_id"], "write")
        self.assertEqual(private_state["_resume_messages"][-1]["role"], "assistant")
        self.assertEqual(private_state["_resume_messages"][-1]["tool_calls"][0]["id"], "write")
        self.assertEqual(private_state["_resume_messages"][-1]["content"], "")

    def test_confirmation_with_same_remote_name_runs_matching_connector_only(self):
        connector_a = "connector-a-" + uuid.uuid4().hex
        connector_b = "connector-b-" + uuid.uuid4().hex
        session_id = "session-" + uuid.uuid4().hex
        message_id = "message-" + uuid.uuid4().hex
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (session_id, "loop-user", "confirmation", "2026-10-04T00:00:00+00:00", "2026-10-04T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)",
                (message_id, "loop-user", session_id, "assistant", "confirmation", "2026-10-04T00:00:00+00:00", "{}"),
            )
        widget = create_confirm_widget(
            "loop-user", session_id, message_id, connector_id=connector_b, connector_name="B",
            tool="remote.same", title="同名工具", arguments={},
        )
        called = []

        async def execute_a(ctx, args):
            called.append("a")
            return ToolResult(text="a")

        async def execute_b(ctx, args):
            called.append("b")
            return ToolResult(text="b")

        first = Tool(
            "mcp.a.same", "same", {"type": "object"}, "external_write", execute_a,
            metadata={"connector_id": connector_a, "connector": "A", "mcp_name": "remote.same", "title": "同名工具"},
        )
        second = Tool(
            "mcp.b.same", "same", {"type": "object"}, "external_write", execute_b,
            metadata={"connector_id": connector_b, "connector": "B", "mcp_name": "remote.same", "title": "同名工具"},
        )
        with patch("app.agent.tools.registry_for", return_value=([first, second], [first.openai_definition(), second.openai_definition()])):
            result, _ = apply_event("loop-user", widget["id"], "confirm", None)
        self.assertEqual(result["state"], {"status": "done"})
        self.assertEqual(called, ["b"])

    def test_timeout_and_error_are_isolated_between_parallel_calls(self):
        async def slow(ctx, args):
            await asyncio.sleep(0.2)
            return ToolResult(text="slow", data={"value": "slow"})

        async def broken(ctx, args):
            raise RuntimeError("boom")

        tools = [
            Tool("test.slow", "slow", {"type": "object"}, "read", slow),
            Tool("test.broken", "broken", {"type": "object"}, "read", broken),
        ]
        calls = [{"type": "tool_calls", "calls": [
            {"id": "slow", "name": "test.slow", "arguments": "{}"},
            {"id": "broken", "name": "test.broken", "arguments": "{}"},
        ]}, {"type": "text", "content": "finished"}, {"type": "text", "content": "finished"}]

        async def provider(messages, tools=None):
            yield calls.pop(0)

        ctx = AgentContext("user")
        ctx.registry_for = lambda user_id, mode="interactive": (tools, [item.openai_definition() for item in tools])
        with patch.dict(os.environ, {"AGENT_TOOL_TIMEOUT_SECONDS": "0.05"}), patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "go"}]))
        tool_messages = [item for item in outcome.messages if item.get("role") == "tool"]
        self.assertEqual([item["tool_call_id"] for item in tool_messages], ["slow", "broken"])
        self.assertIn("超时", tool_messages[0]["content"])
        self.assertIn("执行失败", tool_messages[1]["content"])
        self.assertEqual(outcome.reply, "finished")

    def test_code_call_runs_without_confirmation(self):
        tool = Tool("sandbox.python", "python", {"type": "object"}, "code", lambda *_: ToolResult(text="ran"))
        rounds = [
            [{"type": "tool_calls", "calls": [{"id": "code", "name": tool.name, "arguments": '{"code":"print(1)"}'}]}],
            [{"type": "text", "content": "拒绝后继续"}],
            [{"type": "text", "content": "拒绝后继续"}],
        ]

        async def provider(messages, tools=None):
            if tools:
                if len(rounds) == 2:
                    self.assertIn("ran", messages[-1]["content"])
            else:
                self.assertEqual(messages[-1]["role"], "system")
                self.assertIn("ran", messages[-2]["content"])
            for item in rounds.pop(0):
                yield item

        ctx = AgentContext("user", mode="background", allow_code=False, job_approval_granted=False)
        ctx.registry_for = lambda user_id, mode="background": ([tool], [tool.openai_definition()])
        with patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "run"}]))
        self.assertEqual(outcome.reply, "拒绝后继续")
        self.assertEqual(outcome.tool_calls[0]["decision"], "allow")

    def test_background_approved_call_continues(self):
        executed = []

        async def execute(ctx, args):
            executed.append(args)
            return ToolResult(text="approved result", data={"ok": True})

        tool = Tool("mcp.demo.write", "write", {"type": "object"}, "external_write", execute)
        ctx = AgentContext("user", mode="background", job_id="job-1", approved_calls=[{"action": "mcp.demo.write", "payload": {"x": 1}}])
        ctx.registry_for = lambda user_id, mode="background": ([tool], [tool.openai_definition()])

        call = {"type": "tool_calls", "calls": [{"id": "write", "name": tool.name, "arguments": '{"x":1}'}]}
        rounds = iter([call, {"type": "text", "content": "done"}, {"type": "text", "content": "done"}])
        async def resumed(messages, tools=None):
            value = next(rounds)
            yield value
        with patch.object(loop, "provider_astream_chat", resumed):
            second = self._run(run_agent(ctx, [{"role": "user", "content": "write"}]))
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.reply, "done")
        self.assertEqual(executed, [{"x": 1}])

    def test_code_policy_is_always_allowed(self):
        code_tool = Tool("sandbox.python", "python", {"type": "object"}, "code", lambda *_: ToolResult(text="ok"))
        interactive = AgentContext("user-a", session_id="session-a")
        first = self._run(policy.decide(interactive, code_tool, {"code": "1"}))
        self.assertEqual(first.decision, "allow")
        second = self._run(policy.decide(interactive, code_tool, {"code": "1"}))
        self.assertEqual(second.decision, "allow")
        background = AgentContext("user-a", mode="background", allow_code=False, job_approval_granted=False)
        denied = self._run(policy.decide(background, code_tool, {"code": "1"}))
        self.assertEqual(denied.decision, "allow")

    def test_non_agent_approval_does_not_grant_background_code(self):
        user_id = "approval-user-" + uuid.uuid4().hex
        job = runtime.create_job(
            "agent_run",
            {"prompt": "run", "allowed_tools": ["sandbox.python"], "allow_code": True},
            requires_approval=False,
            user_id=user_id,
        )
        approval = runtime.create_approval(job["id"], "sandbox.python", {"code": "1"}, user_id)
        runtime.decide_approval(approval["id"], "approved", user_id=user_id)
        self.assertFalse(runtime._agent_run_code_approval_granted(job))

        code_tool = Tool("sandbox.python", "python", {"type": "object"}, "code", lambda *_: ToolResult(text="ran"))
        background = AgentContext(
            user_id,
            mode="background",
            allow_code=True,
            job_approval_granted=runtime._agent_run_code_approval_granted(job),
        )
        denied = self._run(policy.decide(background, code_tool, {"code": "1"}))
        self.assertEqual(denied.decision, "allow")

    def test_routine_create_is_allowed_by_default(self):
        routine_tool = Tool("luma.routines.create", "create routine", {"type": "object"}, "write", lambda *_: ToolResult(text="ok"))
        ctx = AgentContext("routine-user")
        decision = self._run(policy.decide(ctx, routine_tool, {"title": "daily", "prompt": "brief", "schedule": "daily"}))
        self.assertEqual(decision.decision, "allow")

    def test_sandbox_registration_follows_runtime_availability(self):
        with patch.object(agent_tools.agent_runtime, "config", return_value=SimpleNamespace(enabled=False)), patch.object(agent_tools, "mcp_catalog", return_value=([], {}, [])):
            names = [item.name for item in agent_tools.registry_for("u", mode="interactive")[0]]
        self.assertNotIn("sandbox.python", names)
        with patch.object(agent_tools.agent_runtime, "config", return_value=SimpleNamespace(enabled=True)), patch.object(agent_tools, "mcp_catalog", return_value=([], {}, [])):
            names = [item.name for item in agent_tools.registry_for("u", mode="interactive")[0]]
        self.assertIn("sandbox.python", names)
        self.assertIn("sandbox.shell", names)

    def test_round_limit_streams_one_final_answer_without_tools(self):
        events = []
        received = []
        executed = []
        tool = Tool("test.read", "read", {"type": "object"}, "read", lambda *_: executed.append(True) or ToolResult(text="签到 7 人"))

        async def provider(messages, tools=None):
            received.append((list(messages), tools))
            if tools is not None:
                yield {"type": "text", "content": "继续查询的过程"}
                yield {"type": "tool_calls", "calls": [{"id": "read_%d" % len(received), "name": tool.name, "arguments": "{}"}]}
                return
            self.assertEqual(messages[-1]["role"], "system")
            self.assertIn("工具调用次数已用完", messages[-1]["content"])
            self.assertIn("数据不完整时说明缺什么", messages[-1]["content"])
            self.assertIn(loop._FINAL_ANSWER_INSTRUCTION, messages[-1]["content"])
            self.assertEqual(len([item for item in messages if item["role"] == "tool"]), 2)
            yield {"type": "text", "content": "签到结果："}
            self.assertEqual([item["content"] for item in events if item["type"] == "delta"], ["签到结果："])
            yield {"type": "text", "content": "7 人。"}

        ctx = AgentContext("user")
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        with patch.dict(os.environ, {"AGENT_MAX_ROUNDS": "2", "AGENT_MAX_TOOL_CALLS": "30"}), patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "查签到"}], emit=events.append))
        self.assertEqual(outcome.status, "completed")
        self.assertTrue(outcome.limited)
        self.assertEqual(outcome.error, "round_limit")
        self.assertEqual(outcome.reply, "签到结果：7 人。")
        self.assertEqual("".join(item["content"] for item in events if item["type"] == "delta"), outcome.reply)
        self.assertEqual(executed, [True, True])
        self.assertEqual(len(received), 3)
        self.assertTrue(all(definitions for _, definitions in received[:-1]))
        self.assertIsNone(received[-1][1])
        self.assertTrue(any(item.get("role") == "system" and "同一个问题尽量用最少的工具调用完成" in item.get("content", "")
                            for item in received[0][0]))

    def test_tool_call_limit_streams_one_final_answer_without_tools(self):
        executed = []
        received = []
        tool = Tool("test.read", "read", {"type": "object"}, "read", lambda *_: executed.append(True) or ToolResult(text="ok"))
        calls = [{"id": str(i), "name": tool.name, "arguments": "{}"} for i in range(7)]

        async def provider(messages, tools=None):
            received.append(tools)
            if tools is not None:
                yield {"type": "tool_calls", "calls": calls}
            else:
                self.assertEqual(len([item for item in messages if item["role"] == "tool"]), 30)
                yield {"type": "text", "content": "已有数据的最终结果"}

        ctx = AgentContext("user")
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        with patch.dict(os.environ, {"AGENT_MAX_ROUNDS": "10", "AGENT_MAX_TOOL_CALLS": "30"}), patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "loop"}]))
        self.assertEqual(outcome.status, "completed")
        self.assertTrue(outcome.limited)
        self.assertEqual(outcome.error, "tool_limit")
        self.assertEqual(outcome.reply, "已有数据的最终结果")
        self.assertEqual(len(executed), 30)
        self.assertEqual(len(received), 6)
        self.assertTrue(all(received[:-1]))
        self.assertIsNone(received[-1])

    def test_tool_batch_exceeding_remaining_limit_is_not_executed(self):
        executed = []
        received = []
        events = []
        tool = Tool("test.read", "read", {"type": "object"}, "read", lambda *_: executed.append(True) or ToolResult(text="部分结果"))

        async def provider(messages, tools=None):
            received.append(tools)
            if tools is not None:
                calls = [{"id": "%d_%d" % (len(received), index), "name": tool.name, "arguments": "{}"} for index in range(3)]
                yield {"type": "tool_calls", "calls": calls}
                return
            self.assertEqual(len([item for item in messages if item["role"] == "tool"]), 3)
            self.assertNotIn("2_0", json.dumps(messages))
            yield {"type": "text", "content": "基于部分结果的答案"}

        ctx = AgentContext("user")
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        with patch.dict(os.environ, {"AGENT_MAX_ROUNDS": "10", "AGENT_MAX_TOOL_CALLS": "5"}), patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, [{"role": "user", "content": "loop"}], emit=events.append))
        self.assertEqual(outcome.status, "completed")
        self.assertTrue(outcome.limited)
        self.assertEqual(outcome.error, "tool_limit")
        self.assertEqual(outcome.reply, "基于部分结果的答案")
        self.assertEqual(len(executed), 3)
        self.assertEqual(len(received), 3)
        self.assertIsNone(received[-1])
        self.assertTrue(any(item["type"] == "tool_limit" for item in events))

    def test_limited_final_answer_failures_stream_friendly_fallback(self):
        fallback = "查询步骤较多，已获取部分数据但未能整理完成，请缩小范围再试一次"
        for failure in ("error", "tool_calls", "empty", "whitespace", "partial_error", "partial_tool_calls"):
            with self.subTest(failure=failure):
                events = []
                received = []
                tool = Tool("test.read", "read", {"type": "object"}, "read", lambda *_: ToolResult(text="签到 7 人"))

                async def provider(messages, tools=None):
                    received.append(tools)
                    if tools is not None:
                        yield {"type": "tool_calls", "calls": [{"id": "read", "name": tool.name, "arguments": "{}"}]}
                        return
                    if failure.startswith("partial_"):
                        yield {"type": "text", "content": "已查到 7 人"}
                    if failure in {"error", "partial_error"}:
                        raise RuntimeError("provider failure")
                    if failure in {"tool_calls", "partial_tool_calls"}:
                        yield {"type": "tool_calls", "calls": [{"id": "extra", "name": tool.name, "arguments": "{}"}]}
                    if failure == "whitespace":
                        yield {"type": "text", "content": "   "}

                ctx = AgentContext("user")
                ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
                with patch.dict(os.environ, {"AGENT_MAX_ROUNDS": "1"}), patch.object(loop, "provider_astream_chat", provider), self.assertLogs(loop.logger, level="WARNING"):
                    outcome = self._run(run_agent(ctx, [{"role": "user", "content": "loop"}], emit=events.append))
                partial = "已查到 7 人" if failure.startswith("partial_") else ("   " if failure == "whitespace" else "")
                self.assertEqual(outcome.reply, partial + ("\n\n" if partial else "") + fallback)
                self.assertEqual(outcome.status, "completed")
                self.assertTrue(outcome.limited)
                self.assertEqual("".join(item["content"] for item in events if item["type"] == "delta"), outcome.reply)
                self.assertEqual(len(received), 2)
                self.assertIsNone(received[-1])
                self.assertEqual(len(outcome.tool_calls), 1)

    def test_agent_limits_use_defaults_and_clamp_environment_values(self):
        self.assertEqual(loop.MAX_ROUNDS, 10)
        self.assertEqual(loop.MAX_TOOL_CALLS, 30)
        for name, default, maximum in (("AGENT_MAX_ROUNDS", 10, 20), ("AGENT_MAX_TOOL_CALLS", 30, 60)):
            with self.subTest(name=name), patch.dict(os.environ):
                os.environ.pop(name, None)
                self.assertEqual(loop._env_limit(name, default, maximum), default)
                for raw, expected in (("3", 3), ("0", 1), ("-5", 1), ("999", maximum), ("invalid", default), ("", default)):
                    os.environ[name] = raw
                    self.assertEqual(loop._env_limit(name, default, maximum), expected)

    def test_confirmation_resume_keeps_tool_call_limit(self):
        executed = []
        tool = Tool("test.read", "read", {"type": "object"}, "read", lambda *_: executed.append(True) or ToolResult(text="ok"))
        restored = [{"role": "user", "content": "original question"}]
        for round_index in range(4):
            calls = [{"id": "previous_%d_%d" % (round_index, index), "type": "function", "function": {"name": tool.name, "arguments": "{}"}}
                     for index in range(5)]
            restored.append({"role": "assistant", "content": "", "tool_calls": calls})
            restored.extend({"role": "tool", "tool_call_id": call["id"], "content": "confirmed result"} for call in calls)

        async def provider(messages, tools=None):
            self.assertIsNone(tools)
            self.assertEqual(len([item for item in messages if item["role"] == "tool"]), 20)
            yield {"type": "text", "content": "确认后已有数据的结果"}

        ctx = AgentContext("user")
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        events = []
        with patch.dict(os.environ, {"AGENT_MAX_ROUNDS": "10", "AGENT_MAX_TOOL_CALLS": "20"}), patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, restored, emit=events.append))
        self.assertEqual(outcome.status, "completed")
        self.assertTrue(outcome.limited)
        self.assertEqual(outcome.error, "tool_limit")
        self.assertEqual(outcome.reply, "确认后已有数据的结果")
        self.assertEqual(executed, [])
        self.assertTrue(any(event["type"] == "tool_limit" for event in events))

    def test_confirmation_resume_keeps_round_limit(self):
        received = []
        tool = Tool("test.read", "read", {"type": "object"}, "read", lambda *_: ToolResult(text="ok"))
        restored = [{"role": "user", "content": "original question"}]
        for index in range(loop.MAX_ROUNDS):
            call_id = "previous_%d" % index
            restored.extend([
                {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool.name, "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": call_id, "content": "confirmed result"},
            ])

        async def provider(messages, tools=None):
            received.append(tools)
            self.assertEqual(len([item for item in messages if item["role"] == "tool"]), loop.MAX_ROUNDS)
            yield {"type": "text", "content": "确认后轮次已用完的结果"}

        ctx = AgentContext("user")
        ctx.registry_for = lambda user_id, mode="interactive": ([tool], [tool.openai_definition()])
        with patch.dict(os.environ, {"AGENT_MAX_ROUNDS": str(loop.MAX_ROUNDS), "AGENT_MAX_TOOL_CALLS": "30"}), patch.object(loop, "provider_astream_chat", provider):
            outcome = self._run(run_agent(ctx, restored))
        self.assertEqual(outcome.status, "completed")
        self.assertTrue(outcome.limited)
        self.assertEqual(outcome.error, "round_limit")
        self.assertEqual(outcome.reply, "确认后轮次已用完的结果")
        self.assertEqual(received, [None])

    def test_decider_defaults_and_rejects_unknown_point(self):
        async def check():
            with patch.object(decider, "_current_decider", return_value=decider.NullDecider()):
                self.assertEqual(await decider.decide("needs_tools", "opaque", default=1.0), 1.0)
            with self.assertRaises(ValueError):
                await decider.decide("policy", "opaque", default=1.0)

        self._run(check())

    def test_decider_without_configuration_makes_no_network_request(self):
        async def check():
            with patch.dict(os.environ, {}, clear=True), patch("httpx.AsyncClient") as client:
                self.assertEqual(await decider.decide("model_route", "opaque", default=0.0), 0.0)
                client.assert_not_called()
        self._run(check())

    def test_decider_slow_endpoint_falls_back_to_default(self):
        class SlowClient:
            async def post(self, *args, **kwargs):
                await asyncio.sleep(0.2)

        async def check():
            with patch.dict(os.environ, {"DECIDER_ENDPOINT": "https://decider.invalid", "DECIDER_API_KEY": "test", "DECIDER_POINTS": "needs_tools"}, clear=False), patch.object(decider.HttpDecider, "_get_client", return_value=SlowClient()):
                started = time.perf_counter()
                value = await decider.decide("needs_tools", "opaque", default=1.0)
                self.assertEqual(value, 1.0)
                self.assertLess(time.perf_counter() - started, 0.5)
        self._run(check())

    def test_policy_modules_do_not_import_decider(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "app"
        for name in ("agent/policy.py", "auth.py", "runtime.py", "routers/runtime.py"):
            self.assertNotIn("decider", (root / name).read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
