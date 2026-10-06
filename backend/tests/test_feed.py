"""Sourced background generation, quotas and tenant-scoped feed endpoints."""

from __future__ import annotations

import asyncio
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests.pg import reset_tables

from app.agent import loop
from app.agent.loop import AgentOutcome
from app.agent.tools import Tool, ToolResult
from app.db import get_connection
from app.model_calls import _context as model_context
from app.routers import feed as feed_router
from app.services import feed
from app.services.proactive_prefs import ProactivePrefs, get_state, put_prefs


NOW = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)
URL = "https://example.com/news"


def post_data(**updates):
    data = {
        "title": "与你关注的学习研究有关的更新", "body_markdown": "这是一篇经过来源核对的研究资讯。" * 10,
        "sources": [{"title": "原始研究", "url": URL}], "reason": "你最近关注学习研究", "topic": "学习研究", "icon": "book",
    }
    data.update(updates)
    return data


class FeedValidationTests(unittest.TestCase):
    def test_fabricated_sources_are_discarded(self):
        data = post_data(sources=[{"title": "真实来源", "url": URL + "?lang=zh#part"}, {"title": "编造来源", "url": "https://fake.example/news"}])
        result = feed.validate_post(json.dumps(data), {("example.com", "/news")})
        self.assertEqual(result["sources"], [data["sources"][0]])

    def test_all_invalid_sources_abandon_post(self):
        for url in ("https://fake.example/news", "javascript:alert(1)", "data:text/html,hello", "https://secret@example.com/news"):
            with self.subTest(url=url):
                self.assertIsNone(feed.validate_post(json.dumps(post_data(sources=[{"title": "来源", "url": url}])), {("example.com", "/news")}))

    def test_schema_limits_and_types_are_strict(self):
        for updates in ({"title": "长" * 41}, {"body_markdown": "短" * 99}, {"body_markdown": "长" * 601},
                        {"reason": "长" * 81}, {"topic": ""}, {"icon": []}, {"sources": []}, {"extra": 1}):
            with self.subTest(updates=updates):
                self.assertIsNone(feed.validate_post(json.dumps(post_data(**updates)), {("example.com", "/news")}))
        self.assertIsNone(feed.validate_post("```json\n{}\n```", set()))

    def test_html_and_embedded_destinations_are_inert(self):
        body = '<script>alert(1)</script> [bad](javascript:alert(1)) ![image](https://example.com/a.png) \\[bad][ref]\n[ref]: data:text/html,x\n' + "正文" * 70
        result = feed.validate_post(json.dumps(post_data(body_markdown=body)), {("example.com", "/news")})
        self.assertIsNotNone(result)
        self.assertNotIn("<script", result["body_markdown"])
        self.assertNotIn("[", result["body_markdown"])
        self.assertIn("&lt;script&gt;", result["body_markdown"])

    def test_sensitive_output_is_rejected(self):
        self.assertIsNone(feed.validate_post(json.dumps(post_data(reason="api_key=example-sensitive-value")), {("example.com", "/news")}))

    def test_escaped_body_still_obeys_length_and_encoded_secrets_are_rejected(self):
        self.assertIsNone(feed.validate_post(json.dumps(post_data(body_markdown="&" * 600)), {("example.com", "/news")}))
        self.assertIsNone(feed.validate_post(json.dumps(post_data(reason="api&#95;key=fixture")), {("example.com", "/news")}))
        self.assertIsNone(feed.validate_post(json.dumps(post_data(sources=[{"title": "来源", "url": URL + "?to%6ben=fixture"}])), {("example.com", "/news")}))

    def test_url_normalization_uses_host_and_path(self):
        self.assertEqual(feed._url_key("HTTPS://EXAMPLE.COM:443/a/../news/?q=1#part"), ("example.com", "/news/"))
        self.assertNotEqual(feed._url_key("https://example.com:8443/news"), feed._url_key(URL))
        self.assertIsNone(feed._url_key("https://example.com:bad/news"))
        self.assertNotEqual(feed._url_key("https://example.com/a%2fb"), feed._url_key("https://example.com/a/b"))
        self.assertNotEqual(feed._url_key("https://example.com/a//b"), feed._url_key("https://example.com/a/b"))
        self.assertNotEqual(feed._url_key(URL + "/"), feed._url_key(URL))
        self.assertEqual(feed._url_key("https://example.com/%6Eews"), feed._url_key(URL))


class FeedTests(unittest.TestCase):
    def setUp(self):
        reset_tables()
        application = FastAPI()
        application.include_router(feed_router.router)
        self.client = TestClient(application)
        self.owner = patch.object(feed_router, "owner_id", return_value="feed-user")
        self.owner.start()
        self.addCleanup(self.owner.stop)

    def prefs(self, **values):
        put_prefs("feed-user", ProactivePrefs(**values))

    def seed_activity(self, user_id="feed-user"):
        timestamp = (NOW - timedelta(minutes=30)).isoformat()
        with get_connection() as conn:
            conn.execute("INSERT INTO sessions(id,user_id,title,kind,created_at,updated_at) VALUES (?,?,?,'main',?,?)",
                         ("ses-" + user_id, user_id, "主聊天", timestamp, timestamp))
            conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) VALUES (?,?,?,'user',?,?,'{}','complete')",
                         ("msg-" + user_id, user_id, "ses-" + user_id, "最近想学习研究", timestamp))

    def insert_post(self, post_id="feed-first", user_id="feed-user", created_at=NOW):
        data = post_data()
        with get_connection() as conn:
            conn.execute("INSERT INTO feed_posts(id,user_id,title,body_markdown,sources_json,reason,topic,icon,liked,dismissed,created_at) VALUES (?,?,?,?,?,?,?,?,FALSE,FALSE,?)",
                         (post_id, user_id, data["title"], data["body_markdown"], json.dumps(data["sources"]), data["reason"], data["topic"], data["icon"], created_at.isoformat()))
        return post_id

    def generate(self, data=None, events=None, manual=False):
        async def mocked_agent(ctx, messages, emit):
            self.assertEqual(ctx.mode, "background")
            self.assertEqual(tuple(ctx.allowed_tools), feed.ALLOWED_TOOLS)
            self.assertEqual(ctx.model_purpose, "feed")
            self.assertIn("严格 JSON", ctx.output_instruction)
            for event in events if events is not None else [{"type": "tool", "tool": "browser.open", "status": "ok", "data": {"url": URL}}]:
                await emit(event)
            return AgentOutcome(reply=json.dumps(data or post_data()))

        with patch.object(feed, "run_agent", new=mocked_agent):
            return asyncio.run(feed.generate_post("feed-user", NOW, manual=manual))

    def test_generation_persists_only_opened_sources_and_updates_count(self):
        result = self.generate(post_data(sources=[{"title": "来源", "url": URL}, {"title": "编造", "url": "https://fake.example/news"}]))
        self.assertTrue(result["generated"])
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM feed_posts WHERE id = ?", (result["post_id"],)).fetchone()
            state = conn.execute("SELECT * FROM proactive_state WHERE user_id = ?", ("feed-user",)).fetchone()
        self.assertEqual(json.loads(row["sources_json"]), [{"title": "来源", "url": URL}])
        self.assertEqual(state["feed_today"], 1)
        self.assertEqual(state["last_feed_at"], NOW.isoformat())

    def test_missing_failed_or_read_only_browser_events_do_not_prove_source(self):
        for events in ([], [{"type": "tool", "tool": "browser.open", "status": "error", "data": {"url": URL}}],
                       [{"type": "tool", "tool": "browser.read", "status": "ok", "data": {"url": URL}}]):
            with self.subTest(events=events):
                self.assertFalse(self.generate(events=events)["generated"])
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM feed_posts").fetchone()["count"], 0)
            self.assertEqual(conn.execute("SELECT feed_today FROM proactive_state WHERE user_id = ?", ("feed-user",)).fetchone()["feed_today"], 0)

    def test_daily_cap_prevents_another_provider_call(self):
        self.assertTrue(self.generate()["generated"])
        with patch.object(feed, "run_agent", new_callable=AsyncMock) as agent:
            self.assertFalse(asyncio.run(feed.generate_post("feed-user", NOW + timedelta(hours=7)))["generated"])
            agent.assert_not_called()

    def test_disabled_window_and_six_hour_interval_prevent_generation(self):
        for prefs, timestamp, last_sent in (({"feed_enabled": False}, NOW, None), ({}, NOW.replace(hour=0), None), ({}, NOW, NOW - timedelta(hours=6))):
            with self.subTest(prefs=prefs, timestamp=timestamp):
                self.prefs(**prefs)
                with get_connection() as conn:
                    get_state(conn, "feed-user", "2026-10-06")
                    conn.execute("UPDATE proactive_state SET last_feed_at = ? WHERE user_id = ?", (last_sent.isoformat() if last_sent else None, "feed-user"))
                with patch.object(feed, "run_agent", new_callable=AsyncMock) as agent:
                    self.assertFalse(asyncio.run(feed.generate_post("feed-user", timestamp))["generated"])
                    agent.assert_not_called()

    def test_manual_ignores_window_and_interval_but_keeps_cap(self):
        with get_connection() as conn:
            get_state(conn, "feed-user", "2026-10-06")
            conn.execute("UPDATE proactive_state SET last_feed_at = ? WHERE user_id = ?", (NOW.isoformat(), "feed-user"))
        self.assertTrue(self.generate(manual=True)["generated"])
        self.assertFalse(self.generate(manual=True)["generated"])

    def test_tick_is_throttled_durable_and_independent_of_proactive_enabled(self):
        self.seed_activity()
        self.prefs(enabled=False)
        with get_connection() as conn:
            self.assertEqual(feed.feed_tick(conn, NOW), 1)
        with get_connection() as conn:
            self.assertEqual(feed.feed_tick(conn, NOW + timedelta(minutes=5)), 0)
            self.assertEqual(feed.feed_tick(conn, NOW + timedelta(minutes=10)), 0)
            jobs = conn.execute("SELECT * FROM runtime_jobs WHERE type = ?", ("feed",)).fetchall()
        self.assertEqual(len(jobs), 1)
        self.assertFalse(json.loads(jobs[0]["payload_json"])["manual"])

    def test_manual_refresh_quota_counts_failed_jobs_and_returns_202(self):
        for _ in range(3):
            with patch.object(feed_router, "enqueue_feed", side_effect=lambda user: feed.enqueue_feed(user, NOW)):
                response = self.client.post("/api/v1/feed/refresh")
            self.assertEqual(response.status_code, 202)
            with get_connection() as conn:
                conn.execute("UPDATE runtime_jobs SET status = 'failed' WHERE id = ?", (response.json()["job_id"],))
        with self.assertRaises(HTTPException) as error:
            feed.enqueue_feed("feed-user", NOW)
        self.assertEqual(error.exception.status_code, 429)

    def test_concurrent_refreshes_queue_only_one_job(self):
        barrier = Barrier(2)

        def enqueue():
            barrier.wait()
            try:
                return feed.enqueue_feed("feed-user", NOW)
            except HTTPException as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: enqueue(), range(2)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn(409, results)

    def test_store_rechecks_daily_cap_atomically(self):
        barrier = Barrier(2)

        def store():
            barrier.wait()
            return feed._store("feed-user", post_data(), NOW, True)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: store(), range(2)))
        self.assertEqual(sum(result["generated"] for result in results), 1)

    def test_feedback_discuss_delete_and_hidden_posts(self):
        post_id = self.insert_post()
        url = "/api/v1/feed/" + post_id
        self.assertTrue(self.client.post(url + "/feedback", json={"action": "like"}).json()["liked"])
        self.assertFalse(self.client.post(url + "/feedback", json={"action": "unlike"}).json()["liked"])
        response = self.client.post(url + "/discuss")
        self.assertEqual(response.status_code, 200)
        with get_connection() as conn:
            session = conn.execute("SELECT * FROM sessions WHERE id = ?", (response.json()["session_id"],)).fetchone()
            message = conn.execute("SELECT * FROM messages WHERE session_id = ?", (session["id"],)).fetchone()
        self.assertEqual((session["user_id"], session["kind"]), ("feed-user", "side"))
        self.assertEqual(json.loads(message["metadata_json"])["feed_post_id"], post_id)
        self.assertEqual((message["role"], message["status"]), ("assistant", "complete"))
        self.assertEqual(message["content"], post_data()["body_markdown"])
        from app.services.seed import ensure_default_data

        ensure_default_data("feed-user")
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT kind FROM sessions WHERE id = ?", (session["id"],)).fetchone()["kind"], "side")
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM sessions WHERE user_id = ? AND kind = 'main'", ("feed-user",)).fetchone()["count"], 1)
        self.client.post(url + "/feedback", json={"action": "not_interested"})
        self.assertEqual(self.client.get("/api/v1/feed").json()["items"], [])
        self.assertEqual(self.client.post(url + "/discuss").status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(self.client.post(url + "/feedback", json={"action": "like"}).status_code, 404)

    def test_ownership_checks_for_all_mutations_and_listing(self):
        post_id = self.insert_post(user_id="other-user")
        url = "/api/v1/feed/" + post_id
        self.assertEqual(self.client.get("/api/v1/feed").json()["items"], [])
        self.assertEqual(self.client.post(url + "/feedback", json={"action": "like"}).status_code, 404)
        self.assertEqual(self.client.post(url + "/discuss").status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)

    def test_pagination_has_stable_tiebreaker_and_rejects_invalid_cursor(self):
        self.insert_post("feed-a")
        self.insert_post("feed-b")
        page = self.client.get("/api/v1/feed", params={"limit": 1}).json()
        self.assertEqual(page["items"][0]["id"], "feed-b")
        next_page = self.client.get("/api/v1/feed", params={"cursor": page["next_cursor"], "limit": 1}).json()
        self.assertEqual(next_page["items"][0]["id"], "feed-a")
        self.assertIsNone(next_page["next_cursor"])
        self.assertEqual(self.client.get("/api/v1/feed", params={"cursor": "!!!"}).status_code, 400)
        self.assertEqual(self.client.post("/api/v1/feed/feed-a/feedback", json={"action": "unknown"}).status_code, 422)

    def test_generation_redacts_input_and_logs_no_exception_details(self):
        self.seed_activity()
        self.prefs(feed_instructions="api_key=example-sensitive-value")
        with get_connection() as conn:
            conn.execute("UPDATE messages SET content = ? WHERE user_id = ?", ("Bearer example-sensitive-value", "feed-user"))

        async def failing_agent(ctx, messages, emit):
            self.assertNotIn("example-sensitive-value", json.dumps(messages))
            raise RuntimeError("Bearer example-sensitive-value")

        with patch.object(feed, "run_agent", new=failing_agent), self.assertLogs("app.services.feed", level="WARNING") as logs:
            self.assertFalse(asyncio.run(feed.generate_post("feed-user", NOW))["generated"])
        self.assertNotIn("example-sensitive-value", " ".join(logs.output))
        self.assertNotIn("feed-user", " ".join(logs.output))
        self.assertIn("RuntimeError", " ".join(logs.output))

    def test_real_agent_rejects_forbidden_tools_including_ui_and_preserves_purpose(self):
        forbidden_calls, widget_events, purposes = [], [], []

        async def browser(ctx, arguments):
            return ToolResult(text="真实研究内容", data={"url": URL})

        async def forbidden(ctx, arguments):
            forbidden_calls.append(arguments)
            return ToolResult(text="should not run")

        registered = [Tool("browser.open", "browser", {"type": "object"}, "read", browser),
                      Tool("luma.tasks.create", "write", {"type": "object"}, "write", forbidden),
                      Tool("luma-ui", "ui", {"type": "object"}, "read", forbidden)]
        rounds = iter([
            [{"type": "tool_calls", "calls": [
                {"id": "open", "name": "browser.open", "arguments": json.dumps({"url": URL})},
                {"id": "write", "name": "luma.tasks.create", "arguments": "{}"},
                {"id": "ui", "name": "luma-ui", "arguments": '{"type":"choice","title":"injected"}'},
            ]}],
            [{"type": "text", "content": json.dumps(post_data())}],
            [{"type": "text", "content": json.dumps(post_data())}],
        ])

        async def provider(messages, tools=None):
            purposes.append(model_context.get().purpose)
            if tools:
                self.assertTrue(all(tool["function"]["name"] in feed.ALLOWED_TOOLS for tool in tools))
            for event in next(rounds):
                yield event

        real_run_agent = loop.run_agent

        async def observed_agent(ctx, messages, emit):
            async def observed_emit(event):
                if event["type"] == "widget":
                    widget_events.append(event)
                await emit(event)
            return await real_run_agent(ctx, messages, emit=observed_emit)

        with patch("app.agent.tools.registry_for", return_value=(registered, [tool.openai_definition() for tool in registered])), patch.object(
            loop, "provider_astream_chat", new=provider
        ), patch.object(loop, "_decider_needs_tools", new=AsyncMock(return_value=True)), patch.object(feed, "run_agent", new=observed_agent):
            result = asyncio.run(feed.generate_post("feed-user", NOW))
        self.assertTrue(result["generated"])
        self.assertEqual(forbidden_calls, [])
        self.assertEqual(widget_events, [])
        self.assertTrue(purposes)
        self.assertEqual(set(purposes), {"feed"})


if __name__ == "__main__":
    unittest.main()
