"""Main/side session and conversation search coverage."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.pg import reset_tables

from app import main
from app.db import get_connection
from app.models import MessageCreate
from app.routers.sessions import get_main_session
from app.services.chat import insert_message
from app.services.seed import ensure_default_data


class SessionSearchTests(unittest.TestCase):
    def setUp(self):
        self.identity = patch("app.deps.current_user_id", return_value="local")
        self.identity.start()
        self.addCleanup(self.identity.stop)
        # TRUNCATE must run outside the application lifespan; its worker and
        # scheduler deliberately keep writing while a client is active.
        reset_tables()
        self.client = TestClient(main.app)
        # Keep the empty-workspace premise for the concurrent creation cases.
        # HTTP requests still use the real per-user seeding path.
        with patch.object(main, "ensure_default_data"):
            self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_main_creation_is_unique_under_concurrent_requests(self):
        def fetch_main(_):
            return self.client.get("/api/v1/sessions/main").json()["id"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(fetch_main, (1, 2)))
        self.assertEqual(ids[0], ids[1])

    def test_seed_and_main_creation_are_safe_for_a_new_user(self):
        user_id = "g1-concurrent-{}".format(uuid.uuid4().hex)
        barrier = Barrier(2)

        def fetch_main(_):
            barrier.wait()
            return get_main_session(None).id

        def seed_user(_):
            barrier.wait()
            ensure_default_data(user_id)
            return None

        with patch("app.routers.sessions.owner_id", return_value=user_id):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(fetch_main, 1), executor.submit(seed_user, 2)]
                results = [future.result() for future in futures]

        self.assertTrue(results[0])
        self.assertIsNone(results[1])
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM sessions WHERE user_id = ? AND kind = 'main'",
                (user_id,),
            ).fetchone()
        self.assertEqual(int(row["count"]), 1)

    def test_main_cannot_be_created_by_post_or_deleted(self):
        main_session = self.client.get("/api/v1/sessions/main").json()
        self.assertEqual(
            self.client.post("/api/v1/sessions", json={"kind": "main"}).status_code,
            422,
        )
        response = self.client.delete("/api/v1/sessions/{}".format(main_session["id"]))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "主聊天不能删除")

    def test_list_contains_preview_fields_and_kind_filter(self):
        main_session = self.client.get("/api/v1/sessions/main").json()
        side_session = self.client.post("/api/v1/sessions", json={"title": "旁聊"}).json()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "msg-preview",
                    "local",
                    side_session["id"],
                    "user",
                    "正文\n```luma-ui\n{\"type\":\"choice\"}\n```",
                    "2026-10-04T00:10:00+00:00",
                    "{}",
                    "complete",
                ),
            )
        rows = self.client.get("/api/v1/sessions", params={"kind": "side"}).json()
        self.assertEqual([row["kind"] for row in rows], ["side"])
        self.assertEqual(rows[0]["id"], side_session["id"])
        self.assertEqual(rows[0]["last_message_preview"], "正文")
        self.assertEqual(rows[0]["message_count"], 1)
        self.assertEqual(self.client.get("/api/v1/sessions", params={"kind": "main"}).json()[0]["id"], main_session["id"])

    def test_search_is_literal_and_tenant_scoped(self):
        session = self.client.post("/api/v1/sessions", json={"title": "100%_主题"}).json()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("msg-local-search", "local", session["id"], "user", "needle 100%_literal", "2026-10-04T00:11:00+00:00", "{}", "complete"),
            )
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,kind,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                ("other-session", "other-user", "other needle", "side", "2026-10-04T00:12:00+00:00", "2026-10-04T00:12:00+00:00"),
            )
            conn.execute(
                "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("msg-other-search", "other-user", "other-session", "user", "needle", "2026-10-04T00:12:00+00:00", "{}", "complete"),
            )
        percent = self.client.get("/api/v1/search", params={"q": "%"}).json()
        self.assertEqual([item["id"] for item in percent["messages"]], ["msg-local-search"])
        underscore = self.client.get("/api/v1/search", params={"q": "_"}).json()
        self.assertEqual([item["id"] for item in underscore["messages"]], ["msg-local-search"])
        needle = self.client.get("/api/v1/search", params={"q": "needle"}).json()
        self.assertTrue(all(item["session_id"] != "other-session" for item in needle["messages"]))
        self.assertEqual(self.client.get("/api/v1/search", params={"q": "  "}).status_code, 422)

    def test_side_first_user_message_renames_only_side_chat(self):
        side = self.client.post("/api/v1/sessions", json={}).json()
        insert_message(side["id"], MessageCreate(content="## 规划一个旅行\n第二行"), "local")
        self.assertEqual(self.client.get("/api/v1/sessions/{}".format(side["id"])).json()["title"], "规划一个旅行")

        main_session = self.client.get("/api/v1/sessions/main").json()
        insert_message(main_session["id"], MessageCreate(content="## 不要改名"), "local")
        self.assertEqual(self.client.get("/api/v1/sessions/{}".format(main_session["id"])).json()["title"], "主聊天")


if __name__ == "__main__":
    unittest.main()
