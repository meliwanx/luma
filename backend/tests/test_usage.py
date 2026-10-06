"""Usage ownership, PostgreSQL percentiles, filters, and privacy coverage."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests import pg  # noqa: F401 -- initialize the isolated PostgreSQL schema first

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import admin, auth, usage
from app.db import get_connection
from app.routers import usage as usage_router


class UsageTests(unittest.TestCase):
    def setUp(self):
        pg.reset_tables()
        self.now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        self.env = patch.dict(os.environ, {"AUTH_REQUIRED": "true", "ADMIN_USER_IDS": "admin"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = patch.object(usage, "_now", return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        app = FastAPI()
        app.include_router(usage_router.router)
        app.include_router(admin.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def call(self, call_id, user_id="alice", **overrides):
        fields = {
            "id": call_id, "user_id": user_id, "session_id": None, "message_id": None,
            "purpose": "chat_round", "model": "model-a", "stream": True,
            "prompt_tokens": 60, "completion_tokens": 40, "total_tokens": 100,
            "cached_tokens": None, "reasoning_tokens": None, "estimated": False,
            "first_token_ms": 10, "duration_ms": 100, "tokens_per_sec": 4,
            "status": "ok", "error_type": None, "tool_calls_count": 0,
            "created_at": self.now - timedelta(minutes=1),
        }
        fields.update(overrides)
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO model_calls (" + ", ".join(fields) + ") VALUES ("
                + ", ".join("?" for _ in fields) + ")",
                tuple(fields.values()),
            )

    def session(self, session_id, owner, title):
        timestamp = self.now.isoformat()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (session_id, owner, title, timestamp, timestamp),
            )

    def get(self, path, user_id="alice"):
        user = {"user_id": user_id} if user_id else None
        with patch.object(auth, "current_user", return_value=user), patch.object(
            admin, "current_user", return_value=user
        ):
            return self.client.get(path)

    def test_user_usage_is_isolated_and_contains_aggregates_only(self):
        self.session("alice-session", "alice", "Alice 工作")
        self.session("bob-session", "bob", "Bob 的私密标题")
        self.call("alice-one", session_id="alice-session")
        self.call("alice-two", session_id="alice-session", purpose="summary", model="model-b", estimated=True)
        self.call("bob-one", user_id="bob", session_id="bob-session", total_tokens=999999)

        response = self.get("/api/v1/usage?range=7d&user_id=bob")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["totals"], {
            "calls": 2, "prompt_tokens": 120, "completion_tokens": 80,
            "total_tokens": 200, "estimated_ratio": 0.5,
        })
        self.assertEqual(data["top_sessions"], [{
            "session_id": "alice-session", "title": "Alice 工作", "calls": 2, "total_tokens": 200,
        }])
        self.assertEqual({row["purpose"] for row in data["purposes"]}, {"chat_round", "summary"})
        self.assertEqual({row["model"] for row in data["models"]}, {"model-a", "model-b"})
        self.assertEqual(sum(row["total_tokens"] for row in data["daily"]), 200)
        self.assertNotIn("Bob", json.dumps(data))
        self.assertNotIn("content", json.dumps(data))

    def test_user_range_excludes_old_and_future_calls_and_fills_empty_days(self):
        self.call("recent")
        self.call("old", created_at=self.now - timedelta(days=8))
        self.call("future", created_at=self.now + timedelta(days=1))
        data = self.get("/api/v1/usage?range=7d").json()
        self.assertEqual(data["totals"]["calls"], 1)
        self.assertEqual(len(data["daily"]), 8)
        self.assertEqual(sum(row["calls"] for row in data["daily"]), 1)
        self.assertEqual(data["daily"][0]["calls"], 0)
        self.assertEqual(self.get("/api/v1/usage?range=30d").json()["totals"]["calls"], 2)

    def test_user_90_day_range(self):
        self.call("sixty-five-days", created_at=self.now - timedelta(days=65))
        self.call("too-old", created_at=self.now - timedelta(days=91))
        self.assertEqual(self.get("/api/v1/usage?range=90d").json()["totals"]["calls"], 1)
        self.assertEqual(self.get("/api/v1/usage?range=30d").json()["totals"]["calls"], 0)

    def test_usage_uses_utc_dates_for_offsets(self):
        self.call("offset", created_at="2026-10-06T00:30:00+08:00")
        daily = self.get("/api/v1/usage?range=7d").json()["daily"]
        used = [row for row in daily if row["calls"]]
        self.assertEqual(used[0]["date"], "2026-10-05")

    def test_unknown_session_does_not_expose_another_owners_title(self):
        self.session("other-session", "bob", "私密会话标题")
        self.call("own-call", session_id="other-session")
        self.call("deleted-call", session_id="deleted-session")
        sessions = self.get("/api/v1/usage?range=7d").json()["top_sessions"]
        self.assertEqual({row["title"] for row in sessions}, {"已删除的会话"})

    def test_top_sessions_are_limited_to_ten_by_tokens(self):
        for index in range(11):
            session_id = "session-" + str(index)
            self.session(session_id, "alice", session_id)
            self.call("call-" + str(index), session_id=session_id, total_tokens=index + 1)
        sessions = self.get("/api/v1/usage").json()["top_sessions"]
        self.assertEqual(len(sessions), 10)
        self.assertEqual(sessions[0]["total_tokens"], 11)
        self.assertNotIn("session-0", {row["session_id"] for row in sessions})

    def test_no_data_returns_zero_totals_and_null_performance(self):
        data = self.get("/api/v1/usage").json()
        self.assertEqual(data["totals"]["calls"], 0)
        self.assertEqual(data["totals"]["total_tokens"], 0)
        self.assertEqual(data["totals"]["estimated_ratio"], 0)
        self.assertEqual(data["performance"], {
            "first_token_ms_avg": None, "duration_ms_p50": None, "duration_ms_p95": None,
        })
        self.assertEqual(data["top_sessions"], [])

    def test_usage_requires_authentication_and_rejects_invalid_range(self):
        self.assertEqual(self.get("/api/v1/usage", user_id=None).status_code, 401)
        self.assertEqual(self.get("/api/v1/usage?range=24h").status_code, 422)

    def test_admin_requires_admin_and_signed_in_identity(self):
        self.assertEqual(self.get("/api/admin/models", user_id="alice").status_code, 403)
        self.assertEqual(self.get("/api/admin/models", user_id=None).status_code, 401)
        self.assertEqual(self.get("/api/admin/models?range=90d", user_id="admin").status_code, 422)

    def test_admin_percentiles_rates_and_slow_calls(self):
        for index in range(4):
            self.call(
                "percentile-" + str(index), first_token_ms=(index + 1) * 10,
                duration_ms=(index + 1) * 100, tokens_per_sec=index + 1,
                status=["ok", "ok", "error", "cancelled"][index],
                error_type="RuntimeError" if index == 2 else None,
            )
        data = self.get("/api/admin/models?range=24h", user_id="admin").json()
        model = data["models"][0]
        self.assertEqual(model["calls"], 4)
        self.assertEqual(model["success_rate"], 0.5)
        self.assertEqual(model["error_types"], {"RuntimeError": 1, "cancelled": 1})
        self.assertEqual(model["first_token_ms"], {"avg": 25, "p50": 25, "p95": 38.5, "p99": 39.7})
        self.assertEqual(model["duration_ms"], {"avg": 250, "p50": 250, "p95": 385, "p99": 397})
        self.assertEqual(model["tokens_per_sec"], {"avg": 2.5, "p50": 2.5})
        self.assertEqual(model["prompt_tokens"], 240)
        self.assertEqual(model["completion_tokens"], 160)
        self.assertEqual(model["tokens_per_call"], 100)
        self.assertEqual([row["id"] for row in data["slow_calls"]], ["percentile-3"])
        self.assertEqual(data["slow_calls"][0]["user"]["user_id"], "alice")
        used = [row for row in data["series"] if row["calls"]]
        self.assertEqual(used[0]["first_token_ms_p95"], 38.5)
        self.assertEqual(used[0]["error_rate"], 0.5)
        self.assertEqual(len(data["series"]), 25)
        self.assertEqual(data["users"][0]["first_token_ms_avg"], 25)
        self.assertNotIn("content", json.dumps(data))

    def test_admin_filters_and_parameterized_model_value(self):
        self.call("alice-a")
        self.call("alice-b", model="model-b", total_tokens=250)
        self.call("bob-b", user_id="bob", model="model-b", total_tokens=900)
        self.call("old-b", model="model-b", created_at=self.now - timedelta(days=8))
        data = self.get("/api/admin/models?range=7d&model=model-b&user_id=alice", user_id="admin").json()
        self.assertEqual(data["models"][0]["calls"], 1)
        self.assertEqual(data["models"][0]["total_tokens"], 250)
        self.assertEqual(data["available_models"], ["model-a", "model-b"])
        self.assertEqual([row["user_id"] for row in data["users"]], ["alice"])
        self.assertEqual(sum(row["calls"] for row in data["series"]), 1)
        injection = self.get("/api/admin/models?model=x%27%20OR%201%3D1--", user_id="admin").json()
        self.assertEqual(injection["models"], [])

    def test_admin_user_ranking_and_null_first_token(self):
        self.call("alice", first_token_ms=None)
        self.call("bob", user_id="bob", total_tokens=200)
        data = self.get("/api/admin/models", user_id="admin").json()
        self.assertEqual([row["user_id"] for row in data["users"]], ["bob", "alice"])
        self.assertIsNone(data["users"][1]["first_token_ms_avg"])
        self.assertEqual(data["models"][0]["first_token_ms"]["p50"], 10)

    def test_overview_tokens_are_today_in_utc_rather_than_last_24_hours(self):
        self.call("today")
        self.call("yesterday", created_at=self.now - timedelta(hours=13))
        with patch.object(admin, "_now", return_value=self.now), patch.object(
            admin, "ping_database", return_value=True
        ), patch.object(admin, "ping_redis", return_value=True), patch.object(
            admin, "agent_runtime_status", return_value={}
        ):
            data = self.get("/api/admin/overview", user_id="admin").json()
        self.assertEqual(data["counts"], {"model_calls_today": 1, "tokens_today": 100})


if __name__ == "__main__":
    unittest.main()
