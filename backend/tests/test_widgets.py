import json
import unittest
from unittest.mock import Mock, patch

from tests.pg import reset_tables

from fastapi import HTTPException

from fastapi.testclient import TestClient

from app import main
from app.services import chat as chat_service
from app.db import get_connection
from app.widgets import HistoryRecordFilter, apply_event, describe_for_model, extract_widgets, strip_history_records


class HistoryRecordTests(unittest.TestCase):
    def test_strips_nested_multiple_and_unclosed_records(self):
        raw = json.dumps({"rows": [{"value": "外层〔嵌套〕"}]}, ensure_ascii=False)
        content = "结果：〔历史组件记录：确认卡 " + raw + "〕9 次。〔历史组件记录：旧卡〕"
        self.assertEqual(strip_history_records(content), "结果：9 次。")
        self.assertEqual(strip_history_records("结论。〔历史组件记录：" + raw), "结论。")
        self.assertEqual(strip_history_records("结论。〔历史组件记录"), "结论。")

    def test_stream_filter_handles_every_chunk_boundary(self):
        content = '结论：〔历史组件记录：确认卡 {"rows":["〔嵌套〕"]}〕签到 9 次。'
        for boundary in range(len(content) + 1):
            with self.subTest(boundary=boundary):
                cleaner = HistoryRecordFilter()
                clean = cleaner.feed(content[:boundary]) + cleaner.feed(content[boundary:]) + cleaner.finish()
                self.assertEqual(clean, "结论：签到 9 次。")
        cleaner = HistoryRecordFilter()
        self.assertEqual("".join(cleaner.feed(char) for char in content) + cleaner.finish(), "结论：签到 9 次。")

    def test_stream_filter_drops_unclosed_nested_record_after_complete_record(self):
        content = 'A〔历史组件记录：旧记录〕B〔历史组件记录：{"rows":["〔嵌套〕"]}'
        cleaner = HistoryRecordFilter()
        self.assertEqual("".join(cleaner.feed(char) for char in content) + cleaner.finish(), "AB")
        self.assertEqual(strip_history_records(content), "AB")
        self.assertEqual(cleaner.finish(), "")

    def test_stream_filter_releases_normal_text_immediately(self):
        cleaner = HistoryRecordFilter()
        self.assertEqual(cleaner.feed("签到共 9 次。"), "签到共 9 次。")
        self.assertEqual(cleaner.feed("〔历"), "")
        self.assertEqual(cleaner.feed("史组件"), "")
        self.assertEqual(cleaner.feed("记录："), "")
        self.assertEqual(cleaner.feed('{"rows":' + "x" * 10000), "")
        self.assertEqual(cleaner.finish(), "")

    def test_keeps_unrelated_brackets_json_and_partial_openers(self):
        content = '〔普通注释〕 {"count":9} 〔历史组件记'
        self.assertEqual(strip_history_records(content), content)
        cleaner = HistoryRecordFilter()
        self.assertEqual(cleaner.feed("〔历史"), "")
        self.assertEqual(cleaner.feed("文本〕"), "〔历史文本〕")
        self.assertEqual(cleaner.finish(), "")
        cleaner = HistoryRecordFilter()
        self.assertEqual(cleaner.feed("〔历史组件记"), "")
        self.assertEqual(cleaner.finish(), "〔历史组件记")

    def test_confirm_description_excludes_private_result(self):
        private_state = {"status": "done", "_pending": {"connector_name": "Sample", "tool_title": "删除记录"}, "_result": '{"secret_raw_rows":[1,2,3]}'}
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = {"state_json": json.dumps(private_state)}
        widget = {"id": "wgt_confirm", "type": "confirm", "spec": {}, "state": {"status": "done"}}
        content = describe_for_model("[[widget:wgt_confirm]]", {widget["id"]: widget}, connection=connection)
        self.assertEqual(content, "〔历史组件记录：确认卡 Sample/删除记录 已执行〕")
        self.assertNotIn("secret_raw_rows", content)
        self.assertEqual(widget["state"], {"status": "done"})
        self.assertEqual(private_state["_result"], '{"secret_raw_rows":[1,2,3]}')


class WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = patch("app.deps.current_user_id", return_value="local")
        cls.identity.start()
        cls.addClassCleanup(cls.identity.stop)
        reset_tables()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                ("s1", "u1", "widget fixtures", "2026-10-03T00:00:00+00:00", "2026-10-03T00:00:00+00:00"),
            )
            for message_id in ("m1", "m2", "m3", "m4"):
                conn.execute(
                    "INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)",
                    (message_id, "u1", "s1", "assistant", "fixture", "2026-10-03T00:00:00+00:00", "{}"),
                )
        cls.client = TestClient(main.app)
        cls.original_complete = chat_service.provider_complete
        cls.original_stream = chat_service.provider_stream
        chat_service.provider_complete = lambda messages: None
        chat_service.provider_stream = lambda messages: (chunk for chunk in ())
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.__exit__(None, None, None)
        finally:
            chat_service.provider_complete = cls.original_complete
            chat_service.provider_stream = cls.original_stream

    def session(self):
        return self.client.get("/api/v1/sessions").json()[0]["id"]

    def test_normalization_and_errors(self):
        content, widgets, errors = extract_widgets(
            "正文\n```luma-ui\n{" + '"type":"choice","extra":1,"options":[{"label":" A "},{"id":"bad id","label":"B"}]}\n```',
            user_id="u1", session_id="s1", message_id="m1",
        )
        self.assertEqual(errors, 0)
        self.assertIn("[[widget:", content)
        self.assertEqual(widgets[0]["spec"]["options"][0]["id"], "opt1")
        self.assertNotIn("extra", widgets[0]["spec"])
        bad, empty, count = extract_widgets("x\n```luma-ui\nnot json\n```", user_id="u1", session_id="s1", message_id="m2")
        self.assertEqual((bad, empty, count), ("x\n", [], 1))

    def test_tolerates_common_model_slips(self):
        body = ('{"type":"form","fields":[{"id":"a","label":"城市","kind":"dropdown","required":"true"},'
                '{"id":"b","label":"预算","kind":"select","options":["只有一个"]},],}')
        content, widgets, errors = extract_widgets(f"```luma-ui\n{body}\n```", user_id="u1", session_id="s1", message_id="m3")
        self.assertEqual(errors, 0)
        fields = widgets[0]["spec"]["fields"]
        self.assertEqual([field["kind"] for field in fields], ["text", "text"])
        self.assertFalse(fields[0]["required"])
        content, widgets, errors = extract_widgets("```luma-ui\n{\"type\":\"choice\"", user_id="u1", session_id="s1", message_id="m4")
        self.assertEqual((content, widgets, errors), ("", [], 1))

    def test_stream_and_messages_include_widgets(self):
        sid = self.session()
        response = self.client.post(f"/api/v1/sessions/{sid}/messages/stream", json={"content": "组件测试"})
        self.assertEqual(response.status_code, 200)
        done = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")][-1]
        payload = json.loads(done)
        self.assertIn("[[widget:", payload["content"])
        self.assertTrue(payload["metadata"]["widgets"])
        listed = self.client.get(f"/api/v1/sessions/{sid}/messages").json()
        self.assertTrue(listed[-1]["metadata"]["widgets"])
        widget_id = listed[-1]["metadata"]["widgets"][0]["id"]
        submitted = self.client.post(f"/api/v1/widgets/{widget_id}/events", json={"action": "submit", "value": ["a"]})
        self.assertEqual(submitted.status_code, 200)
        self.assertIn("我选择了", submitted.json()["message"])
        self.assertEqual(self.client.post(f"/api/v1/widgets/{widget_id}/events", json={"action": "submit", "value": ["a"]}).status_code, 409)
        self.client.post(f"/api/v1/sessions/{sid}/messages/stream", json={"content": "组件测试"})
        fresh = self.client.get(f"/api/v1/sessions/{sid}/messages").json()[-1]["metadata"]["widgets"][0]["id"]
        self.assertEqual(self.client.post(f"/api/v1/widgets/{fresh}/events", json={"action": "submit", "value": ["unknown"]}).status_code, 422)
        with self.assertRaises(HTTPException) as error:
            apply_event("other-user", widget_id, "submit", ["a"])
        self.assertEqual(error.exception.status_code, 404)

    def test_form_checklist_and_tasks(self):
        sid = self.session()
        for command, expected in (("表单测试", "form"), ("清单测试", "checklist")):
            self.client.post(f"/api/v1/sessions/{sid}/messages/stream", json={"content": command})
            msg = self.client.get(f"/api/v1/sessions/{sid}/messages").json()[-1]
            widget = msg["metadata"]["widgets"][0]
            if expected == "form":
                self.assertEqual(self.client.post(f"/api/v1/widgets/{widget['id']}/events", json={"action": "submit", "value": {}}).status_code, 422)
                self.assertEqual(self.client.post(f"/api/v1/widgets/{widget['id']}/events", json={"action": "submit", "value": {"name": "Luma"}}).status_code, 200)
            else:
                self.assertEqual(self.client.post(f"/api/v1/widgets/{widget['id']}/events", json={"action": "toggle", "value": {"item_id": "one", "done": True}}).status_code, 200)
                before = len(self.client.get("/api/v1/tasks").json())
                created = self.client.post(f"/api/v1/widgets/{widget['id']}/events", json={"action": "create_tasks"})
                self.assertEqual(created.status_code, 200)
                self.assertGreater(len(self.client.get("/api/v1/tasks").json()), before)


if __name__ == "__main__":
    unittest.main()
