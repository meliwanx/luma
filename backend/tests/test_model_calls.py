"""Usage parsing, streaming compatibility, and buffered persistence."""

from __future__ import annotations

import asyncio
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError

import httpx

from tests import pg
from app import provider, telemetry
from app.model_calls import ModelCall, UsageTotals, call_context, estimate_tokens, observe, parse_usage
from app.db import get_connection
from app.models import MessageCreate
from starlette.requests import Request
from unittest.mock import AsyncMock
from app.agent import decider
from app.services import chat, memory


CONFIG = provider.ProviderConfig("https://provider.example/v1", "fixture", "fixture-model")
MESSAGES = [{"role": "user", "content": "你好 abcd"}]
USAGE = {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52,
         "prompt_tokens_details": {"cached_tokens": 8},
         "completion_tokens_details": {"reasoning_tokens": 3}}


def sse(*payloads):
    return ''.join('data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n' for payload in payloads) + 'data: [DONE]\n\n'


def text_chunk(text):
    return {"choices": [{"delta": {"content": text}}]}


class ModelCallParsingTests(unittest.TestCase):
    def test_usage_details_and_missing_total(self):
        usage = parse_usage({"usage": USAGE}, 900, 500)
        self.assertEqual(usage, {
            "prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52,
            "cached_tokens": 8, "reasoning_tokens": 3, "estimated": False,
        })
        self.assertEqual(parse_usage({"usage": {"prompt_tokens": 2, "completion_tokens": 3}}, 9, 9)["total_tokens"], 5)

    def test_estimates_cjk_and_english_and_invalid_counts(self):
        self.assertEqual(estimate_tokens("你好abcdefgh"), 4)
        self.assertEqual(parse_usage({}, 4, 3)["total_tokens"], 7)
        self.assertTrue(parse_usage({}, 4, 3)["estimated"])
        usage = parse_usage({"usage": {"prompt_tokens": -1, "completion_tokens": True}}, 4, 3)
        self.assertEqual(usage["total_tokens"], 7)
        self.assertTrue(usage["estimated"])

    def test_zero_usage_is_exact(self):
        usage = parse_usage({"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}, 4, 3)
        self.assertEqual(usage["total_tokens"], 0)
        self.assertFalse(usage["estimated"])

    def test_fragment_estimation_has_no_rounding_per_chunk(self):
        with patch.object(telemetry, "record_model_call") as record, call_context(user_id="u"):
            with observe("m", MESSAGES, stream=True) as call:
                for char in "abcd你好":
                    call.accept(text_chunk(char))
            row = record.call_args[0][0]
        self.assertEqual(row["completion_tokens"], 3)
        self.assertNotIn("messages", row)
        self.assertNotIn("content", row)

    def test_unowned_calls_are_not_attributed(self):
        with patch.object(telemetry, "record_model_call") as record:
            with observe("m", MESSAGES, stream=False):
                pass
            record.assert_not_called()

    def test_background_feature_purposes_are_preserved(self):
        with patch.object(telemetry, "record_model_call") as record:
            for purpose in ("ideas", "proactive", "feed"):
                with call_context(user_id="u", purpose=purpose), observe("m", MESSAGES, stream=False):
                    pass
        self.assertEqual([args[0][0]["purpose"] for args in record.call_args_list], ["ideas", "proactive", "feed"])

    def test_nested_context_and_totals_are_isolated(self):
        totals = UsageTotals()
        with patch.object(telemetry, "record_model_call") as record:
            with call_context(user_id="u1", session_id="s1", message_id="msg1", totals=totals):
                with call_context(purpose="summary"), observe("m", MESSAGES, stream=False) as call:
                    call.accept({"usage": USAGE})
                with observe("m", MESSAGES, stream=False):
                    pass
            with call_context(user_id="u2"), observe("m", MESSAGES, stream=False):
                pass
        rows = [args[0][0] for args in record.call_args_list]
        self.assertEqual((rows[0]["user_id"], rows[0]["session_id"], rows[0]["message_id"], rows[0]["purpose"]), ("u1", "s1", "msg1", "summary"))
        self.assertEqual(rows[1]["purpose"], "other")
        self.assertIsNone(rows[2]["session_id"])
        self.assertEqual(totals.snapshot()["calls"], 2)
        self.assertEqual(totals.snapshot()["estimated_calls"], 1)


class ModelCallAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        provider._usage_unsupported.clear()
        self.config = patch.object(provider, "get_config", return_value=CONFIG)
        self.config.start()
        self.addCleanup(self.config.stop)
        self.record_impl = telemetry.record_model_call
        self.record = patch.object(telemetry, "record_model_call").start()
        self.addCleanup(patch.stopall)

    async def use_transport(self, handler, function):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(provider, "_get_async_client", return_value=client), call_context(user_id="u", session_id="s", purpose="final_answer"):
                return await function()

    async def test_stream_tail_usage_and_stream_never_opens_database(self):
        self.record.side_effect = self.record_impl
        with telemetry._lock:
            original = list(telemetry._model_calls)
        def restore():
            with telemetry._lock:
                telemetry._model_calls[:] = original
        self.addCleanup(restore)
        def handler(request):
            self.assertEqual(json.loads(request.content)["stream_options"], {"include_usage": True})
            return httpx.Response(200, text=sse(text_chunk("你好"), {"choices": [], "usage": USAGE}))
        async def run():
            with patch.object(telemetry, "get_connection", side_effect=AssertionError("stream must not touch SQL")):
                return [chunk async for chunk in provider.astream(MESSAGES)]
        self.assertEqual(await self.use_transport(handler, run), ["你好"])
        row = self.record.call_args[0][0]
        self.assertEqual((row["prompt_tokens"], row["completion_tokens"], row["cached_tokens"], row["reasoning_tokens"]), (40, 12, 8, 3))
        self.assertFalse(row["estimated"])
        self.assertGreaterEqual(row["duration_ms"], row["first_token_ms"])

    async def test_non_stream_usage(self):
        def handler(request):
            self.assertNotIn("stream_options", json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}], "usage": USAGE})
        self.assertEqual(await self.use_transport(handler, lambda: provider.acomplete(MESSAGES)), "answer")
        self.assertFalse(self.record.call_args[0][0]["stream"])
        self.assertEqual(self.record.call_args[0][0]["total_tokens"], 52)

    async def test_non_stream_missing_usage_estimates(self):
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": "你好abcdefgh"}}]})
        self.assertEqual(await self.use_transport(handler, lambda: provider.acomplete(MESSAGES)), "你好abcdefgh")
        row = self.record.call_args[0][0]
        self.assertEqual(row["completion_tokens"], 4)
        self.assertTrue(row["estimated"])

    async def test_json_response_format_does_not_add_tools(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(body["temperature"], 0.7)
            self.assertNotIn("tools", body)
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
        result = await self.use_transport(handler, lambda: provider.acomplete(
            MESSAGES, temperature=0.7, response_format={"type": "json_object"},
        ))
        self.assertEqual(result, "{}")

    async def test_non_stream_can_use_a_background_loop_client(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}], "usage": USAGE},
        ))
        async with httpx.AsyncClient(transport=transport) as client:
            with patch.object(provider, "_get_async_client", side_effect=AssertionError("shared client used")), call_context(user_id="u", purpose="ideas"):
                result = await provider.acomplete(MESSAGES, client=client, response_format={"type": "json_object"})
            self.assertFalse(client.is_closed)
        self.assertEqual(result, "{}")
        self.assertEqual(self.record.call_args[0][0]["purpose"], "ideas")

    async def test_missing_usage_estimates_output(self):
        async def run():
            return [chunk async for chunk in provider.astream(MESSAGES)]
        await self.use_transport(lambda request: httpx.Response(200, text=sse(text_chunk("你好abcdefgh"))), run)
        row = self.record.call_args[0][0]
        self.assertEqual(row["completion_tokens"], 4)
        self.assertTrue(row["estimated"])
        self.assertEqual(row["total_tokens"], row["prompt_tokens"] + 4)

    async def test_rejected_include_usage_retries_once_and_remembers(self):
        bodies = []
        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            if "stream_options" in body:
                return httpx.Response(400, json={"error": "unsupported stream_options"})
            return httpx.Response(200, text=sse(text_chunk("ok")))
        async def run():
            for _ in range(2):
                self.assertEqual([chunk async for chunk in provider.astream(MESSAGES)], ["ok"])
        await self.use_transport(handler, run)
        self.assertEqual(len(bodies), 3)
        self.assertNotIn("stream_options", bodies[1])
        self.assertNotIn("stream_options", bodies[2])
        self.assertEqual(self.record.call_count, 2)
        other = provider.ProviderConfig(CONFIG.base_url, "fixture", "other-model")
        self.assertIn("stream_options", provider._request_body(other, [], stream=True, temperature=0))

    async def test_unknown_parameter_422_retries_and_second_failure_stops(self):
        bodies = []
        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(422, json={"error": "unknown include_usage parameter"})
        async def run():
            with self.assertRaises(provider.ProviderStreamError):
                async for _ in provider.astream_chat(MESSAGES):
                    pass
        await self.use_transport(handler, run)
        self.assertEqual(len(bodies), 2)
        row = self.record.call_args[0][0]
        self.assertEqual((row["status"], row["error_type"]), ("error", "http_422"))

    async def test_tool_call_fragments_and_usage_only_tail(self):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t", "function": {"name": "f", "arguments": '{"x":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]}}]},
            {"usage": USAGE, "choices": []},
        ]
        async def run():
            return [item async for item in provider.astream_chat(MESSAGES, tools=[{"type": "function"}])]
        result = await self.use_transport(lambda request: httpx.Response(200, text=sse(*chunks)), run)
        self.assertEqual(result, [{"type": "tool_calls", "calls": [{"id": "t", "name": "f", "arguments": '{"x":1}'}]}])
        self.assertEqual(self.record.call_args[0][0]["tool_calls_count"], 1)
        self.assertEqual(self.record.call_args[0][0]["total_tokens"], 52)

    async def test_provider_timeout_recorded(self):
        def handler(request):
            raise httpx.ReadTimeout("fixture timeout", request=request)
        async def run():
            with self.assertRaises(provider.ProviderStreamError):
                await provider.acomplete(MESSAGES)
        await self.use_transport(handler, run)
        self.assertEqual(self.record.call_args[0][0]["status"], "timeout")

    async def test_outer_wait_for_deadline_records_timeout(self):
        async def run():
            async def pending():
                with observe("m", MESSAGES, stream=False):
                    await asyncio.sleep(1)
            with call_context(timeout_seconds=0.01):
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(pending(), timeout=0.01)
        await self.use_transport(lambda request: httpx.Response(200), run)
        self.assertEqual(self.record.call_args[0][0]["status"], "timeout")

    async def test_early_close_records_cancelled_and_closes_response(self):
        async def run():
            iterator = provider.astream(MESSAGES)
            self.assertEqual(await iterator.__anext__(), "partial")
            await iterator.aclose()
        await self.use_transport(lambda request: httpx.Response(200, text=sse(text_chunk("partial"))), run)
        self.assertEqual(self.record.call_args[0][0]["status"], "cancelled")

    async def test_decider_http_call_uses_same_recorder(self):
        adapter = decider.HttpDecider("https://provider.example/decision", "fixture", "decision-model", {"needs_tools"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"answers": {"needs_tools": {"noul": 0.8}}, "usage": USAGE}))) as client:
            with patch.object(adapter, "_get_client", return_value=client), call_context(user_id="u", session_id="s"):
                self.assertEqual(await adapter.decide("needs_tools", "opaque", default=0), 0.8)
        row = self.record.call_args[0][0]
        self.assertEqual((row["purpose"], row["model"], row["total_tokens"]), ("decider", "decision-model", 52))


class ModelCallSyncTests(unittest.TestCase):
    def setUp(self):
        provider._usage_unsupported.clear()

    def test_sync_non_stream_and_stream_usage(self):
        with patch.object(provider, "get_config", return_value=CONFIG), patch.object(telemetry, "record_model_call") as record, call_context(user_id="u"):
            with patch.object(provider, "urlopen", return_value=io.BytesIO(json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": USAGE}).encode())):
                self.assertEqual(provider.complete(MESSAGES), "ok")
            with patch.object(provider, "urlopen", return_value=io.BytesIO(sse(text_chunk("ok"), {"usage": USAGE, "choices": []}).encode())):
                self.assertEqual(list(provider.stream(MESSAGES)), ["ok"])
        self.assertEqual([args[0][0]["total_tokens"] for args in record.call_args_list], [52, 52])

    def test_sync_retries_include_usage(self):
        responses = [HTTPError("https://provider.example", 400, "unsupported", {}, io.BytesIO()), io.BytesIO(sse(text_chunk("ok")).encode())]
        with patch.object(provider, "get_config", return_value=CONFIG), patch.object(provider, "urlopen", side_effect=responses) as send, patch.object(telemetry, "record_model_call"), call_context(user_id="u"):
            self.assertEqual(list(provider.stream_chat(MESSAGES)), [{"type": "text", "content": "ok"}])
        self.assertEqual(send.call_count, 2)
        self.assertNotIn("stream_options", json.loads(send.call_args_list[1][0][0].data))

    def test_sync_json_response_format_preserves_ideas_measurement(self):
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode())
        with patch.object(provider, "get_config", return_value=CONFIG), patch.object(provider, "urlopen", return_value=response) as send, patch.object(telemetry, "record_model_call") as record, call_context(user_id="u", purpose="ideas"):
            self.assertEqual(provider.complete(MESSAGES, temperature=0.7, response_format={"type": "json_object"}), "{}")
        body = json.loads(send.call_args[0][0].data)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", body)
        self.assertEqual(record.call_args[0][0]["purpose"], "ideas")

    def test_background_executor_preserves_call_context(self):
        with patch.object(telemetry, "record_model_call") as record:
            with call_context(user_id="u", session_id="s", message_id="msg", purpose="summary"):
                future = memory.submit_memory_background(lambda: _background_observation())
            future.result(timeout=2)
        row = record.call_args[0][0]
        self.assertEqual((row["user_id"], row["session_id"], row["message_id"], row["purpose"]), ("u", "s", "msg", "summary"))


def _background_observation():
    with observe("m", MESSAGES, stream=False):
        pass


class ModelCallPersistenceTests(unittest.TestCase):
    def setUp(self):
        pg.reset_tables()
        with telemetry._lock:
            telemetry._model_calls.clear()

    def tearDown(self):
        with telemetry._lock:
            telemetry._model_calls.clear()

    def record(self, *, created_at=None, message_id=None):
        with call_context(user_id="u", session_id="s", message_id=message_id, purpose="final_answer"):
            call = ModelCall("m", MESSAGES, stream=False)
            call.accept({"usage": USAGE})
            if created_at:
                call.created_at = created_at
            call.finish()

    def test_buffer_flush_and_assistant_metadata_late_call(self):
        now = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            conn.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)", ("s", "u", "title", now, now))
            conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) VALUES (?,?,?,?,?,?,?,?)", ("msg", "u", "s", "assistant", "answer", now, '{"model":"m"}', "complete"))
        self.record(message_id="msg")
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0], 0)
        telemetry.flush()
        self.record(message_id="msg")
        telemetry.flush()
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM model_calls").fetchall()
            meta = json.loads(conn.execute("SELECT metadata_json FROM messages WHERE id = ?", ("msg",)).fetchone()[0])
        self.assertEqual(len(rows), 2)
        self.assertEqual(meta["usage"]["calls"], 2)
        self.assertEqual(meta["usage"]["total_tokens"], 104)
        self.assertEqual(meta["model"], "m")
        self.assertNotIn("content", rows[0])

    def test_generation_runner_can_close_in_another_task_context(self):
        now = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            conn.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)", ("s", "u", "title", now, now))

        async def run():
            start = AsyncMock()
            with patch.object(chat, "owner_id", return_value="u"), patch.object(chat, "provider_configuration_model", return_value="m"), patch.object(chat.generation.manager, "start", start):
                await chat.stream_message(Request({"type": "http", "headers": [], "path": "/"}), "s", MessageCreate(role="user", content="hello"))
                runner = start.call_args[0][1]()
                event, _ = await runner.__anext__()
                self.assertEqual(event, "start")
                await asyncio.create_task(runner.aclose())
        asyncio.run(run())

    def test_two_workers_flush_same_message_without_losing_totals(self):
        now = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            conn.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)", ("s", "u", "title", now, now))
            conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) VALUES (?,?,?,?,?,?,?,?)", ("msg", "u", "s", "assistant", "answer", now, '{}', "complete"))
        self.record(message_id="msg")
        self.record(message_id="msg")
        with telemetry._lock:
            rows = list(telemetry._model_calls)
            telemetry._model_calls.clear()
        ready = threading.Barrier(2)

        def flush(row):
            with get_connection() as conn:
                # Both workers own their PG connections before competing for
                # the account write lock. INSERT is serialized by that lock;
                # placing a barrier after INSERT would deadlock the holder
                # against the second worker waiting to acquire it.
                ready.wait(timeout=5)
                telemetry._flush_model_calls(conn, [row])
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(flush, row) for row in rows]
            for future in futures:
                future.result(timeout=10)
        with get_connection() as conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM messages WHERE id = ?", ("msg",)).fetchone()[0])
        self.assertEqual(metadata["usage"]["calls"], 2)
        self.assertEqual(metadata["usage"]["total_tokens"], 104)

    def test_late_flush_then_stale_stream_checkpoint_cannot_reduce_usage(self):
        now = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            conn.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)", ("s", "u", "title", now, now))
            conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) VALUES (?,?,?,?,?,?,?,?)", ("msg", "u", "s", "assistant", "answer", now, '{}', "streaming"))
        self.record(message_id="msg")
        self.record(message_id="msg")
        telemetry.flush()
        stale = {"provider": "llm", "usage": {"calls": 1, "prompt_tokens": 40, "completion_tokens": 12,
                 "total_tokens": 52, "estimated_calls": 0, "estimated": False}}
        chat.persist_assistant_progress(session_id="s", assistant_id="msg", content="partial", created_at=now,
                                        metadata=stale, user_id="u")
        with patch.object(chat, "schedule_memory_extraction"):
            assistant = chat.persist_assistant_message(session_id="s", assistant_id="msg", content="answer", created_at=now,
                                                      metadata=stale, user_id="u")
        self.assertEqual(assistant["metadata"]["usage"]["total_tokens"], 104)
        self.assertEqual(assistant["metadata"]["usage"]["calls"], 2)
        with get_connection() as conn:
            metadata = json.loads(conn.execute("SELECT metadata_json FROM messages WHERE id = ?", ("msg",)).fetchone()[0])
        self.assertEqual(metadata["usage"]["calls"], 2)
        self.assertEqual(metadata["provider"], "llm")

    def test_prunes_only_calls_older_than_180_days(self):
        now = datetime.now(timezone.utc)
        self.record(created_at=(now - timedelta(days=181)).isoformat())
        self.record(created_at=(now - timedelta(days=179)).isoformat())
        telemetry.flush()
        telemetry.prune()
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0], 1)

    def test_transient_flush_failure_retains_rows_and_retry_is_idempotent(self):
        self.record()
        with patch.object(telemetry, "get_connection", side_effect=RuntimeError("fixture database unavailable")), self.assertLogs(telemetry.logger, level="ERROR"):
            telemetry.flush()
        self.assertEqual(len(telemetry._model_calls), 1)
        row = dict(telemetry._model_calls[0])
        telemetry.flush()
        telemetry.record_model_call(row)
        telemetry.flush()
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0], 1)
