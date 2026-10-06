"""Operator activity permissions, filtering, and stable pagination."""

from __future__ import annotations

import base64
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests import pg  # noqa: F401 -- initialize the isolated PostgreSQL schema first

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import admin, auth
from app.db import get_connection


class AdminActivityTests(unittest.TestCase):
    def setUp(self):
        pg.reset_tables()
        self.now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        self.env = patch.dict(os.environ, {"AUTH_REQUIRED": "true", "ADMIN_USER_IDS": "admin"})
        self.env.start()
        self.addCleanup(self.env.stop)
        app = FastAPI()
        app.include_router(admin.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def activity(self, activity_id, user_id="alice", kind="job_succeeded", created_at=None):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO runtime_activity(id,user_id,kind,title,detail,created_at) VALUES (?,?,?,?,?,?)",
                (activity_id, user_id, kind, "后台任务完成", "已完成资料整理", (created_at or self.now).isoformat()),
            )

    def person(self, user_id, display_name):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO users(user_id,username,display_name,created_at,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?)",
                (user_id, user_id, display_name, self.now.isoformat(), self.now.isoformat(), self.now.isoformat()),
            )

    def get(self, actor="admin", **params):
        user = {"user_id": actor} if actor else None
        with patch.object(auth, "current_user", return_value=user), patch.object(
            admin, "current_user", return_value=user
        ):
            return self.client.get("/api/admin/activity", params=params)

    def test_requires_signed_in_admin(self):
        self.assertEqual(self.get(actor=None).status_code, 401)
        self.assertEqual(self.get(actor="alice").status_code, 403)
        self.assertEqual(self.get().status_code, 200)

    def test_returns_all_users_with_display_name_and_unknown_owner(self):
        self.person("alice", "阿梨")
        self.activity("alice-activity")
        self.activity("bob-activity", user_id="bob")
        response = self.get()
        self.assertEqual(response.status_code, 200, response.text)
        rows = {item["id"]: item for item in response.json()["items"]}
        self.assertEqual(set(rows), {"alice-activity", "bob-activity"})
        self.assertEqual(rows["alice-activity"]["display_name"], "阿梨")
        self.assertEqual(rows["alice-activity"]["user"]["display_name"], "阿梨")
        self.assertEqual(rows["alice-activity"]["detail"], "已完成资料整理")
        self.assertIsNone(rows["bob-activity"]["display_name"])
        self.assertEqual(rows["bob-activity"]["user"]["user_id"], "bob")
        self.assertIsNone(response.json()["next_cursor"])

    def test_filters_user_and_kind_with_parameterized_values(self):
        self.activity("alice-success")
        self.activity("alice-failure", kind="job_failed")
        self.activity("bob-success", user_id="bob")
        self.assertEqual(
            {item["id"] for item in self.get(user_id="alice").json()["items"]},
            {"alice-success", "alice-failure"},
        )
        self.assertEqual(
            {item["id"] for item in self.get(kind="job_succeeded").json()["items"]},
            {"alice-success", "bob-success"},
        )
        with patch.object(auth, "current_user", return_value={"user_id": "admin"}), patch.object(
            admin, "current_user", return_value={"user_id": "admin"}
        ):
            response = self.client.get("/api/admin/activity", params={"user_id": "alice", "kind": "job_failed"})
            injection = self.client.get("/api/admin/activity", params={"user_id": "alice' OR 1=1--"})
        self.assertEqual([item["id"] for item in response.json()["items"]], ["alice-failure"])
        self.assertEqual(injection.json()["items"], [])
        self.assertEqual(self.get(kind="job_failed' OR 1=1--").json()["items"], [])

    def test_cursor_pages_equal_timestamps_without_gaps_or_duplicates(self):
        for activity_id in ("a", "b", "c", "d"):
            self.activity(activity_id)
        self.activity("old", created_at=self.now - timedelta(seconds=1))
        first = self.get(limit=2).json()
        self.assertEqual([item["id"] for item in first["items"]], ["d", "c"])
        self.assertTrue(first["next_cursor"])
        self.activity("new", created_at=self.now + timedelta(seconds=1))
        second = self.get(limit=2, cursor=first["next_cursor"]).json()
        third = self.get(limit=2, cursor=second["next_cursor"]).json()
        self.assertEqual([item["id"] for item in second["items"]], ["b", "a"])
        self.assertEqual([item["id"] for item in third["items"]], ["old"])
        self.assertIsNone(third["next_cursor"])

    def test_cursor_preserves_filtered_pagination(self):
        for index in range(5):
            self.activity(str(index), kind="job_failed" if index % 2 else "job_succeeded")
        first = self.get(kind="job_succeeded", limit=1).json()
        second = self.get(kind="job_succeeded", limit=1, cursor=first["next_cursor"]).json()
        self.assertEqual([item["id"] for item in first["items"] + second["items"]], ["4", "2"])

    def test_rejects_invalid_cursor_and_limit(self):
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode("utf-8")).decode("ascii")

        for cursor in ("bad!", encode({}), encode(["bad-date", "id"]), encode([self.now.isoformat(), 1])):
            with self.subTest(cursor=cursor):
                response = self.get(cursor=cursor)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"], "无效的 cursor")
        self.assertEqual(self.get(limit=0).status_code, 422)
        self.assertEqual(self.get(limit=501).status_code, 422)


if __name__ == "__main__":
    unittest.main()
