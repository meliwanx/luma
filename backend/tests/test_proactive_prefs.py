"""Preferences, shared quotas and durable scheduler integration."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from tests import pg

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, model_calls, provider, runtime, scheduler, telemetry
from app.agent import loop
from app.db import get_connection
from app.routers import proactive
from app.services.notifications import create_notification
from app.services.proactive_prefs import DEFAULT_PREFS, ProactivePrefs, claim_tick, get_prefs, get_state, in_window, put_prefs


class ProactivePrefsTests(unittest.TestCase):
    def setUp(self):
        pg.reset_tables()
        app = FastAPI()
        app.include_router(proactive.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.identity = patch("app.deps.current_user_id", return_value="alice")
        self.identity.start()
        self.addCleanup(self.identity.stop)
        self.now = datetime(2026, 10, 6, 4, tzinfo=timezone.utc)

    def test_defaults_and_put_round_trip_without_credentials(self):
        self.assertEqual(self.client.get("/api/v1/proactive/prefs").json(), DEFAULT_PREFS)
        values = {**DEFAULT_PREFS, "enabled": False, "max_per_day": 5, "feed_per_day": 3,
                  "window_start": "22:00", "window_end": "06:00", "timezone": "UTC",
                  "topics_like": "数据分析", "topics_avoid": "娱乐", "style": "简洁",
                  "feed_instructions": "关注数据库更新"}
        response = self.client.put("/api/v1/proactive/prefs", json=values)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), values)
        self.assertEqual(self.client.get("/api/v1/proactive/prefs").json(), values)
        self.assertEqual(get_prefs("bob"), DEFAULT_PREFS)
        with get_connection() as conn:
            self.assertTrue(conn.execute("SELECT updated_at FROM proactive_prefs WHERE user_id = ?", ("alice",)).fetchone()["updated_at"])

    def test_rejects_invalid_clocks_zones_ranges_types_and_lengths(self):
        invalid = [("window_start", value) for value in ("9:00", "24:00", "12:60", " 09:00", "09:00:00")]
        invalid += [("window_end", "23:99"), ("timezone", "Invalid/Zone"), ("timezone", "../UTC"),
                    ("enabled", "true"), ("feed_enabled", 1), ("max_per_day", -1), ("max_per_day", 6),
                    ("max_per_day", True), ("feed_per_day", 4), ("feed_per_day", 1.5),
                    ("topics_like", "字" * 1001), ("topics_avoid", "字" * 1001),
                    ("style", "字" * 501), ("feed_instructions", "字" * 2001), ("api_key", "fixture")]
        for field, value in invalid:
            with self.subTest(field=field, value_type=type(value).__name__):
                response = self.client.put("/api/v1/proactive/prefs", json={field: value})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(get_prefs("alice"), DEFAULT_PREFS)

    def test_prefs_require_authentication(self):
        self.identity.stop()
        with patch.dict(os.environ, {"AUTH_REQUIRED": "true"}), patch.object(auth, "current_user", return_value=None):
            self.assertEqual(self.client.get("/api/v1/proactive/prefs").status_code, 401)
            self.assertEqual(self.client.put("/api/v1/proactive/prefs", json={}).status_code, 401)

    def test_windows_are_inclusive_local_and_support_midnight(self):
        self.assertTrue(in_window(DEFAULT_PREFS, self.now))
        self.assertTrue(in_window(DEFAULT_PREFS, datetime(2026, 10, 6, 1, tzinfo=timezone.utc)))
        self.assertTrue(in_window(DEFAULT_PREFS, datetime(2026, 10, 6, 13, 30, tzinfo=timezone.utc)))
        self.assertFalse(in_window(DEFAULT_PREFS, datetime(2026, 10, 6, 13, 31, tzinfo=timezone.utc)))
        overnight = {**DEFAULT_PREFS, "timezone": "UTC", "window_start": "22:00", "window_end": "06:00"}
        self.assertTrue(in_window(overnight, self.now))
        self.assertFalse(in_window(overnight, self.now.replace(hour=12)))

    def test_state_rolls_local_day_preserving_intervals(self):
        with get_connection() as conn:
            state = get_state(conn, "alice", "2026-10-05")
            self.assertEqual(state["sent_today"], 0)
            conn.execute(
                "UPDATE proactive_state SET sent_today = 2, feed_today = 1, last_sent_at = ?, last_checked_at = ?, last_feed_at = ? WHERE user_id = ?",
                (self.now.isoformat(), self.now.isoformat(), self.now.isoformat(), "alice"),
            )
        with get_connection() as conn:
            state = get_state(conn, "alice", "2026-10-06")
        self.assertEqual((state["sent_today"], state["feed_today"]), (0, 0))
        self.assertEqual(state["last_sent_at"], self.now.isoformat())
        self.assertEqual(state["last_checked_at"], self.now.isoformat())
        self.assertEqual(state["last_feed_at"], self.now.isoformat())

    def test_throttle_survives_transactions_and_has_independent_intervals(self):
        with get_connection() as conn:
            self.assertTrue(claim_tick(conn, "proactive", self.now, 300))
            self.assertTrue(claim_tick(conn, "feed", self.now, 600))
        with get_connection() as conn:
            self.assertFalse(claim_tick(conn, "proactive", self.now + timedelta(seconds=299), 300))
            self.assertTrue(claim_tick(conn, "proactive", self.now + timedelta(seconds=300), 300))
            self.assertFalse(claim_tick(conn, "feed", self.now + timedelta(seconds=599), 600))
            self.assertTrue(claim_tick(conn, "feed", self.now + timedelta(seconds=600), 600))

    def test_scheduler_queues_once_without_provider_or_browser_calls(self):
        with get_connection() as conn:
            conn.execute("INSERT INTO sessions(id,user_id,title,kind,created_at,updated_at) VALUES (?,?,?,'main',?,?)",
                         ("s-alice", "alice", "主聊天", self.now.isoformat(), self.now.isoformat()))
            conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) VALUES (?,?,?,'user',?,?,'{}','complete')",
                         ("m-alice", "alice", "s-alice", "关注数据库", (self.now - timedelta(hours=1)).isoformat()))
        with patch.object(scheduler, "_now", return_value=self.now), patch("app.provider.acomplete", new_callable=AsyncMock) as provider_call, patch("app.services.feed.run_agent", new_callable=AsyncMock) as agent:
            first = scheduler.scheduler_tick()
            second = scheduler.scheduler_tick()
            provider_call.assert_not_called()
            agent.assert_not_called()
        self.assertEqual((first["proactive_jobs"], first["feed_jobs"]), (1, 1))
        self.assertEqual((second["proactive_jobs"], second["feed_jobs"]), (0, 0))
        with patch.object(scheduler, "_now", return_value=self.now + timedelta(minutes=10)):
            third = scheduler.scheduler_tick()
        self.assertEqual((third["proactive_jobs"], third["feed_jobs"]), (0, 0))
        with get_connection() as conn:
            jobs = conn.execute("SELECT type FROM runtime_jobs WHERE user_id = ? AND status = 'queued'", ("alice",)).fetchall()
        self.assertEqual(sorted(row["type"] for row in jobs), ["feed", "proactive"])

    def test_runtime_dispatch_is_owned_and_closes_browser_connections(self):
        with patch("app.provider.local_mode", return_value=False), patch("app.services.proactive.evaluate_user", new_callable=AsyncMock, return_value={"sent": False}) as evaluate:
            self.assertEqual(runtime.execute_job("proactive", {}, "alice"), {"sent": False})
            evaluate.assert_awaited_once_with("alice")
        with patch("app.provider.local_mode", return_value=False), patch("app.services.feed.generate_post", new_callable=AsyncMock, return_value={"generated": False}) as generate, patch("app.services.browser.close_connections", new_callable=AsyncMock) as close:
            self.assertEqual(runtime.execute_job("feed", {"manual": True, "job_id": "job-fixture"}, "alice"), {"generated": False})
            generate.assert_awaited_once_with("alice", manual=True, job_id="job-fixture")
            close.assert_awaited_once_with()
        self.assertFalse(runtime.job_requires_approval("feed", {}))
        self.assertFalse(runtime.job_requires_approval("proactive", {}))

    def test_local_runtime_does_not_call_background_models_or_browsers(self):
        with patch("app.provider.local_mode", return_value=True), patch("app.services.proactive.evaluate_user", new_callable=AsyncMock) as evaluate, patch("app.services.feed.generate_post", new_callable=AsyncMock) as generate:
            self.assertFalse(runtime.execute_job("proactive", {}, "alice")["sent"])
            self.assertFalse(runtime.execute_job("feed", {}, "alice")["generated"])
            evaluate.assert_not_called()
            generate.assert_not_called()

    def test_background_clients_are_owned_closed_and_isolated_from_chat(self):
        clients = []
        shared = object()

        async def evaluate(user_id):
            clients.append(await provider.get_async_client())
            return {"sent": False}

        with patch.object(provider, "local_mode", return_value=False), patch.object(provider, "_async_client", shared), patch(
            "app.services.proactive.evaluate_user", new=evaluate
        ):
            runtime.execute_job("proactive", {}, "alice")
            runtime.execute_job("proactive", {}, "bob")
            self.assertIs(provider._async_client, shared)
            self.assertIs(asyncio.run(provider.get_async_client()), shared)
        self.assertIsNot(clients[0], clients[1])
        self.assertTrue(all(client.is_closed for client in clients))

    def test_scoped_provider_clients_are_isolated_between_concurrent_tasks(self):
        async def run():
            first, second = object(), object()
            ready = asyncio.Event()
            results = []

            async def lookup(client):
                with provider.async_client_context(client):
                    await ready.wait()
                    results.append((client, await provider.get_async_client()))

            tasks = [asyncio.create_task(lookup(first)), asyncio.create_task(lookup(second))]
            ready.set()
            await asyncio.gather(*tasks)
            self.assertTrue(all(expected is actual for expected, actual in results))
            self.assertIsNone(provider._scoped_async_client.get())
            with self.assertRaises(RuntimeError):
                with provider.async_client_context(first):
                    raise RuntimeError("fixture")
            self.assertIsNone(provider._scoped_async_client.get())
        asyncio.run(run())

    def test_feed_schedules_known_users_without_recent_messages(self):
        from app.services.feed import feed_tick

        with get_connection() as conn:
            conn.execute("INSERT INTO users(user_id,username,display_name,created_at,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?)",
                         ("alice", "alice", "Alice", self.now.isoformat(), self.now.isoformat(), self.now.isoformat()))
            conn.execute("INSERT INTO sessions(id,user_id,title,kind,created_at,updated_at) VALUES (?,?,?,'main',?,?)",
                         ("s-bob", "bob", "主聊天", (self.now - timedelta(days=30)).isoformat(), (self.now - timedelta(days=30)).isoformat()))
            self.assertEqual(feed_tick(conn, self.now), 2)
        with get_connection() as conn:
            rows = conn.execute("SELECT user_id FROM runtime_jobs WHERE type = 'feed' AND status = 'queued' ORDER BY user_id").fetchall()
        self.assertEqual([row["user_id"] for row in rows], ["alice", "bob"])

    def test_notification_can_join_and_roll_back_with_outer_transaction(self):
        with patch("app.services.notifications.publish_notification") as publish:
            with self.assertRaises(RuntimeError):
                with get_connection() as conn:
                    create_notification("alice", "proactive", "主动消息", "内容", conn=conn, publish=False)
                    raise RuntimeError("rollback fixture")
            publish.assert_not_called()
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM notifications").fetchone()["count"], 0)

    def test_background_model_purposes_survive_loop_and_database_constraint(self):
        rows = []

        async def round_impl(*args, **kwargs):
            with model_calls.observe("fixture", [], stream=False):
                pass
            return "{}", []

        async def run():
            ctx = loop.AgentContext("alice", mode="background", model_purpose="feed")
            with patch.object(loop, "_model_round_impl", side_effect=round_impl):
                await loop._model_round([], [{"type": "function"}], ctx, None, stream_text=False)
                await loop._model_round([], None, ctx, None, stream_text=True)

        with patch.object(telemetry, "record_model_call", side_effect=rows.append):
            asyncio.run(run())
            for purpose in ("ideas", "proactive"):
                with model_calls.call_context(user_id="alice", purpose=purpose), model_calls.observe("fixture", [], stream=False):
                    pass
        self.assertEqual([row["purpose"] for row in rows], ["feed", "feed", "ideas", "proactive"])
        with get_connection() as conn:
            for row in rows:
                conn.execute("INSERT INTO model_calls(id,user_id,purpose,model,status) VALUES (?,?,?,?,?)",
                             (row["id"], row["user_id"], row["purpose"], row["model"], row["status"]))

    def test_background_generation_skips_optional_decider(self):
        async def run():
            ctx = loop.AgentContext("alice", mode="background", model_purpose="feed", allowed_tools=[])
            with patch.object(loop, "_registry", new_callable=AsyncMock, return_value=([], [])), patch.object(
                loop, "_decider_needs_tools", new_callable=AsyncMock
            ) as decider, patch.object(loop, "_model_round", new_callable=AsyncMock, return_value=("{}", [])):
                outcome = await loop.run_agent(ctx, [{"role": "user", "content": "生成动态"}])
                decider.assert_not_called()
                self.assertEqual(outcome.reply, "{}")
        asyncio.run(run())
