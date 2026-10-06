import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.pg import reset_tables

from app import main
from app.db import get_connection


class MessagePaginationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = patch("app.deps.current_user_id", return_value="local")
        cls.identity.start()
        cls.addClassCleanup(cls.identity.stop)
        reset_tables()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                ("pagination-session", "local", "分页", "2026-10-03T00:00:00+00:00", "2026-10-03T00:00:00+00:00"),
            )
            for index in range(250):
                conn.execute(
                    "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        "message-{:03d}".format(index),
                        "local",
                        "pagination-session",
                        "user",
                        "消息 {:03d}".format(index),
                        "2026-10-03T00:00:{:02d}.{:06d}+00:00".format(index // 1_000_000, index % 1_000_000),
                        "{}",
                    ),
                )
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_dashboard_uses_latest_messages(self):
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 200)
        messages = response.json()["messages"]
        self.assertEqual(len(messages), 100)
        self.assertEqual(messages[0]["id"], "message-150")
        self.assertEqual(messages[-1]["id"], "message-249")

    def test_default_returns_latest_messages_in_ascending_order(self):
        response = self.client.get("/api/v1/sessions/pagination-session/messages")
        self.assertEqual(response.status_code, 200)
        messages = response.json()
        self.assertEqual(len(messages), 100)
        self.assertEqual(messages[0]["id"], "message-150")
        self.assertEqual(messages[-1]["id"], "message-249")
        self.assertEqual([item["id"] for item in messages], ["message-{:03d}".format(i) for i in range(150, 250)])
        self.assertEqual(response.headers["X-Has-More"], "true")
        self.assertEqual(response.headers["X-Oldest-Id"], "message-150")

    def test_before_cursor_pages_without_duplicates_or_gaps(self):
        all_ids = []
        before = None
        for page_number in range(3):
            params = {"limit": "100"}
            if before:
                params["before"] = before
            response = self.client.get("/api/v1/sessions/pagination-session/messages", params=params)
            self.assertEqual(response.status_code, 200)
            page = response.json()
            self.assertEqual(len(page), 100 if page_number < 2 else 50)
            page_ids = [item["id"] for item in page]
            all_ids.extend(page_ids)
            self.assertEqual(response.headers["X-Oldest-Id"], page_ids[0])
            if page_number < 2:
                self.assertEqual(response.headers["X-Has-More"], "true")
                before = page_ids[0]
            else:
                self.assertEqual(response.headers["X-Has-More"], "false")
        self.assertEqual(set(all_ids), {"message-{:03d}".format(i) for i in range(250)})
        self.assertEqual(len(set(all_ids)), 250)

    def test_same_timestamp_uses_message_id_as_stable_tiebreaker(self):
        with get_connection() as conn:
            conn.execute(
                "UPDATE messages SET created_at = ? WHERE session_id = ? AND id IN (?, ?)",
                ("2026-10-03T00:01:00+00:00", "pagination-session", "message-200", "message-201"),
            )
        response = self.client.get(
            "/api/v1/sessions/pagination-session/messages",
            params={"limit": 1, "before": "message-201"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["id"], "message-200")


if __name__ == "__main__":
    unittest.main()
