import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timezone

from tests.pg import reset_tables

from app import main  # noqa: E402
from app.db import ensure_db, get_connection  # noqa: E402
from app.widgets import _pseudo_analysis, extract_widgets, strip_pseudo_markup  # noqa: E402


class PseudoRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_tables()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                ("s", "local", "fixture", "2026-10-03T00:00:00+00:00", "2026-10-03T00:00:00+00:00"),
            )
        ensure_db()

    def setUp(self):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                ("s", "local", "fixture", "2026-10-03T00:00:00+00:00", "2026-10-03T00:00:00+00:00"),
            )

    def test_strip_checklist_and_missing_parameter(self):
        checklist = (
            "给你看一个清单组件：<tool_call><function=luma-ui>"
            '{"type":"checklist","title":"我的一天","items":[{"label":"起床"}]}'
            "</parameter></invoke></function_calls></function></tool_call>后面"
        )
        self.assertEqual(strip_pseudo_markup(checklist), "给你看一个清单组件：后面")
        no_parameter = '<function_calls><invoke name="luma-ui">{"type":"checklist","items":[{"label":"一步"}]}</invoke></function_calls>'
        self.assertEqual(strip_pseudo_markup(no_parameter), "")

    def test_code_fence_and_existing_marker(self):
        source = "```text\n<tool_call>{\"type\":\"checklist\"}</tool_call>\n```"
        self.assertEqual(strip_pseudo_markup(source), source)
        spec = '{"type":"checklist","title":"旧","items":[{"label":"一步"}]}'
        message_id = "msg_" + uuid.uuid4().hex
        content, widgets, _ = extract_widgets(
            f"```luma-ui\n{spec}\n```", user_id="local", session_id="s", message_id=message_id
        )
        self.assertEqual(len(widgets), 1)
        raw = content + '<tool_call><function=luma-ui>' + spec + '</function>'
        self.assertEqual(strip_pseudo_markup(raw), content)

    def test_strong_markers_only_remove_confirmed_pseudo_calls(self):
        # A/B: tags discussed in prose, including inline code, are preserved.
        a = "模型有时会输出 `<tool_call>` 这种标签，这是格式错误。下面是正确写法。"
        b = "说明：<function_calls> 不是工具。后面还有很多正文。"
        self.assertEqual(strip_pseudo_markup(a), a)
        self.assertEqual(strip_pseudo_markup(b), b)

        # C: a real pseudo call is removed, while surrounding text survives.
        c = (
            '看这个：<tool_call>\n<parameter name="spec">'
            '{"type":"checklist","title":"t","items":[{"id":"a","label":"x"}]}'
            '</parameter>\n</tool_call>\n后面的话'
        )
        self.assertEqual(strip_pseudo_markup(c), "看这个：\n后面的话")
        extracted, widgets, errors = extract_widgets(
            c, user_id="local", session_id="s", message_id="m-c"
        )
        self.assertEqual(errors, 0)
        self.assertEqual(len(widgets), 1)
        self.assertEqual(extracted, f"看这个：[[widget:{widgets[0]['id']}]]\n后面的话")

        # D/E: streaming callers can distinguish a tail from a truncated object
        # and show loading from the strong marker onward.
        d = '好的：<tool_call><function=luma-ui>\n'
        e = '好的：<tool_call><function=luma-ui>\n{"type":"choice","ti'
        for value, expected_kind in ((d, "tail"), (e, "truncated")):
            marker_end = value.index(">", value.index("<tool_call>")) + 1
            self.assertEqual(_pseudo_analysis(value, marker_end)[0], expected_kind)
            self.assertEqual(strip_pseudo_markup(value), "好的：")

        # F: an explanatory inline marker and a later real call are independent.
        f = (
            '解释：`<tool_call>` 是格式示例，然后：<tool_call><function=luma-ui>'
            '{"type":"choice","title":"t"}</function>后面的正文'
        )
        self.assertEqual(strip_pseudo_markup(f), '解释：`<tool_call>` 是格式示例，然后：后面的正文')

    def test_strong_marker_followed_by_prose_is_not_a_widget(self):
        source = "说明：<function_calls> 不是工具。后面还有很多正文。"
        content, found, errors = extract_widgets(
            source, user_id="local", session_id="s", message_id="m-prose"
        )
        self.assertEqual((content, found, errors), (source, [], 0))

    def test_repair_script_dry_run_apply_and_idempotence(self):
        reset_tables()
        env = dict(os.environ)
        seed_code = r'''
from app.db import ensure_db, get_connection
ensure_db()
with get_connection() as conn:
    conn.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)", ("s1","local","x","2026-10-03T00:00:00+00:00","2026-10-03T00:00:00+00:00"))
    conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)", ("m1","local","s1","assistant",'<tool_call><function=luma-ui>{"type":"checklist","items":[{"label":"一步"}]}</function>',"2026-10-03T00:00:01+00:00","{}"))
    conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)", ("m2","local","s1","assistant",'```text\n<tool_call>{"x":1}</tool_call>\n```',"2026-10-03T00:00:02+00:00","{}"))
    conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)", ("m3","local","s1","assistant",'[[widget:wgt_existing]]<tool_call>{"type":"checklist","items":[{"label":"重复"}]}</function>',"2026-10-03T00:00:03+00:00","{}"))
    spec = '{"type":"checklist","items":[{"id":"item1","label":"旧"}]}'
    conn.execute("INSERT INTO widgets(id,user_id,session_id,message_id,type,spec_json,state_json,fallback,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", ("wgt_existing","local","s1","m3","checklist",spec,'{"done":{},"tasks_created":false,"task_ids":[]}',"待办清单：旧","2026-10-03T00:00:03+00:00","2026-10-03T00:00:03+00:00"))
'''
        subprocess.run([sys.executable, "-c", seed_code], cwd=os.path.dirname(__file__) + "/..", env=env, check=True, capture_output=True, text=True)
        script = os.path.join(os.path.dirname(__file__), "..", "scripts", "repair_pseudo_widgets.py")
        dry = subprocess.run([sys.executable, script], cwd=os.path.dirname(__file__) + "/..", env=env, check=True, capture_output=True, text=True)
        self.assertIn("dry-run: 2 message(s)", dry.stdout)
        self.assertIn("m1", dry.stdout)
        self.assertIn("m3", dry.stdout)
        self.assertNotIn("一步", dry.stdout)
        applied = subprocess.run([sys.executable, script, "--apply"], cwd=os.path.dirname(__file__) + "/..", env=env, check=True, capture_output=True, text=True)
        self.assertIn("applied: 2 message(s)", applied.stdout)
        second = subprocess.run([sys.executable, script, "--apply"], cwd=os.path.dirname(__file__) + "/..", env=env, check=True, capture_output=True, text=True)
        self.assertIn("applied: 0 message(s)", second.stdout)
        verify_code = r'''from app.db import get_connection
with get_connection() as conn:
    print(conn.execute("SELECT content FROM messages WHERE id = ?", ("m3",)).fetchone()["content"])
    print(conn.execute("SELECT COUNT(*) AS count FROM widgets WHERE message_id = ?", ("m3",)).fetchone()["count"])
'''
        verified = subprocess.run([sys.executable, "-c", verify_code], cwd=os.path.dirname(__file__) + "/..", env=env, check=True, capture_output=True, text=True)
        self.assertIn("[[widget:wgt_existing]]", verified.stdout)
        self.assertIn("\n1\n", "\n" + verified.stdout)

    def test_conversation_history_is_sanitized(self):
        session_id = "s_" + uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            conn.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)", (session_id, "local", "history", now, now))
            conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json) VALUES (?,?,?,?,?,?,?)", ("m_" + uuid.uuid4().hex, "local", session_id, "assistant", '<tool_call>{"type":"checklist","items":[{"label":"一步"}]}</tool_call>历史正文', now, "{}"))
        messages = main.conversation_messages(session_id, "现在继续", "local")
        history = "\n".join(item["content"] for item in messages)
        self.assertNotIn("<tool_call>", history)


if __name__ == "__main__":
    unittest.main()
