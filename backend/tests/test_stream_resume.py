"""Durable generation and resume coverage for the B3 stream boundary."""

import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request

from tests.pg import reset_tables
from app import main
from app import admin as admin_module
from app.db import get_connection
from app.services import chat as chat_service
from app.services import generation
from app.models import MessageCreate


class StreamResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = patch("app.deps.current_user_id", return_value="local")
        cls.identity.start()
        cls.addClassCleanup(cls.identity.stop)
        reset_tables()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                ("stream-session", "local", "stream", "2026-10-03T00:00:00+00:00", "2026-10-03T00:00:00+00:00"),
            )
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    @staticmethod
    def _events(body):
        events = []
        for block in body.split("\n\n"):
            lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
            if "event" in lines:
                events.append((lines.get("id"), lines["event"], json.loads(lines["data"])))
        return events

    @staticmethod
    def _session(session_id, user_id="local"):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (session_id, user_id, "stream", "2026-10-03T00:00:00+00:00", "2026-10-03T00:00:00+00:00"),
            )

    @staticmethod
    def _request(path):
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
            "root_path": "",
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        return Request(scope, receive)

    def test_generation_is_persisted_with_status(self):
        response = self.client.post(
            "/api/v1/sessions/stream-session/messages/stream", json={"content": "续读测试"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        events = self._events(response.text)
        self.assertIn("start", [event[1] for event in events])
        self.assertEqual(events[-1][1], "done")
        self.assertEqual(events[-1][2]["status"], "complete")
        listed = self.client.get("/api/v1/sessions/stream-session/messages").json()
        self.assertEqual(listed[-1]["status"], "complete")

    def test_admin_metrics_count_status_failures(self):
        rows = [
            {"metadata_json": "{}", "status": "incomplete"},
            {"metadata_json": "{}", "status": "error"},
            {"metadata_json": '{"incomplete": true}', "status": "complete"},
            {"metadata_json": "{}", "status": "complete"},
        ]
        summary = admin_module._metric_summary(rows)
        self.assertEqual(summary["replies"], 4)
        self.assertEqual(summary["failed"], 3)

    def test_delta_events_are_coalesced_without_losing_content(self):
        published = []

        async def fake_publish(message_id, event, data):
            published.append((message_id, event, data))
            return str(len(published))

        async def runner():
            for index in range(100):
                yield "delta", {"content": str(index % 10)}
                await asyncio.sleep(0)
            yield "done", {"id": "msg-aggregate", "status": "complete"}

        async def run():
            manager = generation.GenerationManager()
            manager.publish = fake_publish
            task = await manager.start("msg-aggregate", runner, timeout_seconds=2)
            await task
            return manager

        asyncio.run(run())
        deltas = [data["content"] for _message_id, event, data in published if event == "delta"]
        self.assertLess(len(deltas), 100)
        self.assertEqual("".join(deltas), "".join(str(index % 10) for index in range(100)))

    def test_generation_local_state_expires_after_retention(self):
        async def runner():
            yield "done", {"id": "msg-retention", "status": "complete"}

        async def run():
            manager = generation.GenerationManager()
            with patch.dict(os.environ, {"GENERATION_HISTORY_RETENTION_SECONDS": "0.05"}, clear=False):
                task = await manager.start("msg-retention", runner, timeout_seconds=2)
                await task
                await asyncio.sleep(0.15)
            return manager

        manager = asyncio.run(run())
        self.assertNotIn("msg-retention", manager.histories)
        self.assertNotIn("msg-retention", manager.counters)
        self.assertNotIn("msg-retention", manager.cancel_events)
        self.assertNotIn("msg-retention", manager.queues)

    def test_recover_stale_stream_without_alive_marker(self):
        session_id = "stream-stale-recovery"
        self._session(session_id)
        stale_created_at = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "msg-stale-recovery",
                    "local",
                    session_id,
                    "assistant",
                    "partial",
                    stale_created_at,
                    "{}",
                    "streaming",
                ),
            )

        class FakeRedis:
            def ping(self):
                return True

            def exists(self, _key):
                return 0

        async def run():
            with patch.dict(os.environ, {"REDIS_HOST": "127.0.0.1"}, clear=False), patch(
                "app.db._redis_client", return_value=FakeRedis()
            ):
                await generation.GenerationManager().recover_stale()

        asyncio.run(run())
        with get_connection() as conn:
            row = conn.execute("SELECT status FROM messages WHERE id = ?", ("msg-stale-recovery",)).fetchone()
        self.assertEqual(row["status"], "incomplete")

    def test_resume_after_event_id_does_not_repeat_prefix(self):
        response = self.client.post(
            "/api/v1/sessions/stream-session/messages/stream", json={"content": "断点测试"}
        )
        events = self._events(response.text)
        self.assertGreaterEqual(len(events), 3)
        after = events[0][0]
        resumed = self.client.get(
            "/api/v1/messages/{}/stream".format(events[0][2]["message_id"]), params={"after": after}
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)
        resumed_events = self._events(resumed.text)
        self.assertTrue(resumed_events)
        self.assertNotEqual(resumed_events[0][0], after)
        self.assertEqual(resumed_events[-1][1], "done")

    def test_provider_failure_is_error_without_local_reply(self):
        session_id = "stream-provider-error"
        self._session(session_id)

        async def broken_provider(_messages):
            raise RuntimeError("upstream unavailable")
            yield "unreachable"

        with patch.object(chat_service, "provider_local_mode", return_value=False), patch.object(
            chat_service, "provider_astream", broken_provider
        ):
            response = self.client.post(
                "/api/v1/sessions/{}/messages/stream".format(session_id), json={"content": "失败测试"}
            )
        self.assertEqual(response.status_code, 200, response.text)
        events = self._events(response.text)
        done = [data for _id, event, data in events if event == "done"][-1]
        self.assertEqual(done["status"], "error")
        self.assertEqual(done["content"], "模型暂时不可用，请稍后重试")
        self.assertNotIn(chat_service.local_reply("失败测试"), done["content"])
        self.assertEqual(done["metadata"]["error"], "RuntimeError")

    def test_resume_rejects_message_owned_by_another_user(self):
        session_id = "stream-other-owner"
        self._session(session_id, user_id="other-user")
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "msg-other-owner",
                    "other-user",
                    session_id,
                    "assistant",
                    "private",
                    "2026-10-03T00:00:00+00:00",
                    "{}",
                    "complete",
                ),
            )
        response = self.client.get("/api/v1/messages/msg-other-owner/stream")
        self.assertEqual(response.status_code, 404)

    def test_disconnect_does_not_cancel_background_generation(self):
        session_id = "stream-disconnect"
        self._session(session_id)

        async def provider(_messages):
            for chunk in ("断", "线", "后"):
                yield chunk
                await asyncio.sleep(0.06)

        async def run():
            with patch.object(chat_service, "provider_local_mode", return_value=False), patch.object(
                chat_service, "provider_astream", provider
            ):
                response = await chat_service.stream_message(
                    self._request("/api/v1/sessions/{}/messages/stream".format(session_id)),
                    session_id,
                    MessageCreate(content="断开测试"),
                )
                iterator = response.body_iterator
                first = await iterator.__anext__()
                message_id = json.loads(first.split("data: ", 1)[1].splitlines()[0])["message_id"]
                await iterator.__anext__()
                await iterator.aclose()
                task = generation.manager.tasks.get(message_id)
                if task is not None:
                    await asyncio.wait_for(asyncio.shield(task), timeout=10)

        asyncio.run(run())
        with get_connection() as conn:
            row = conn.execute(
                "SELECT content, status FROM messages WHERE session_id = ? AND role = 'assistant' ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        self.assertEqual(dict(row), {"content": "断线后", "status": "complete"})

    def test_cancel_keeps_partial_content_and_marks_incomplete(self):
        session_id = "stream-cancel"
        self._session(session_id)

        async def provider(_messages):
            for chunk in ("部", "分", "内容"):
                yield chunk
                await asyncio.sleep(0.1)

        async def run():
            with patch.object(chat_service, "provider_local_mode", return_value=False), patch.object(
                chat_service, "provider_astream", provider
            ):
                response = await chat_service.stream_message(
                    self._request("/api/v1/sessions/{}/messages/stream".format(session_id)),
                    session_id,
                    MessageCreate(content="取消测试"),
                )
                iterator = response.body_iterator
                first = await iterator.__anext__()
                message_id = json.loads(first.split("data: ", 1)[1].splitlines()[0])["message_id"]
                await generation.manager.cancel(message_id)
                async for _ in iterator:
                    pass

        asyncio.run(run())
        with get_connection() as conn:
            row = conn.execute(
                "SELECT content, status FROM messages WHERE session_id = ? AND role = 'assistant' ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        self.assertEqual(row["status"], "incomplete")
        self.assertTrue(row["content"] in {"", "部"})

    def test_concurrent_limit_rejects_before_user_message_insert(self):
        session_id = "stream-concurrency-limit"
        self._session(session_id)
        active_at = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            for index in range(3):
                conn.execute(
                    "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        "msg-active-{}".format(index),
                        "local",
                        session_id,
                        "assistant",
                        "",
                        active_at,
                        "{}",
                        "streaming",
                    ),
                )
        with patch.dict(os.environ, {"MAX_CONCURRENT_GENERATIONS_PER_USER": "3"}, clear=False):
            response = self.client.post(
                "/api/v1/sessions/{}/messages/stream".format(session_id),
                json={"content": "第 4 个生成"},
            )
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()["detail"]["code"], "too_many_generations")
        with get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE session_id = ? AND role = 'user'",
                (session_id,),
            ).fetchone()["count"]
        self.assertEqual(count, 0)
        # Keep the shared class schema independent of the following stream
        # tests; these rows intentionally model still-running work.
        with get_connection() as conn:
            conn.execute(
                "UPDATE messages SET status = 'incomplete' WHERE session_id = ? AND status = 'streaming'",
                (session_id,),
            )

    def test_generation_timeout_is_persisted_as_incomplete(self):
        session_id = "stream-timeout"
        self._session(session_id)

        async def slow_provider(_messages):
            yield "首个"
            await asyncio.sleep(2.0)
            yield "不会到达"

        with patch.dict(os.environ, {"GENERATION_MAX_SECONDS": "1"}, clear=False), patch.object(
            chat_service, "provider_local_mode", return_value=False
        ), patch.object(chat_service, "provider_astream", slow_provider):
            response = self.client.post(
                "/api/v1/sessions/{}/messages/stream".format(session_id),
                json={"content": "超时测试"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        events = self._events(response.text)
        done = [data for _id, event, data in events if event == "done"][-1]
        self.assertEqual(done["status"], "incomplete")
        self.assertEqual(done["metadata"]["error"], "timeout")


if __name__ == "__main__":
    unittest.main()
