"""Resource-library ownership, pagination, preview, and metadata coverage."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import uuid
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import pg

from app.db import get_connection
from app.routers import files as files_router
from app.routers import library
from app.services.files import store_file_bytes
from app.storage import clear_storage_cache, get_storage


class LibraryTests(unittest.TestCase):
    def setUp(self):
        pg.reset_tables()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, {"FILE_STORAGE": "local", "ASSISTANT_FILE_ROOT": self.directory.name}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        clear_storage_cache()
        self.addCleanup(clear_storage_cache)
        app = FastAPI()

        @app.middleware("http")
        async def authenticated(request, call_next):
            request.state.luma_user = {"user_id": "library-owner"}
            return await call_next(request)

        app.include_router(library.router)
        app.include_router(files_router.router)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _session(self, session_id="library-session", user_id="library-owner", title="资源来源会话"):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,kind,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (session_id, user_id, title, "side", "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
            )
        return session_id

    def _file(self, filename="note.txt", content=b"hello", media_type="text/plain", file_id=None,
              user_id="library-owner", session_id=None, created_at="2026-10-01T00:00:00+00:00",
              title=None, last_opened_at=None, pinned=False, deleted_at=None):
        file_id = file_id or "library-file-" + uuid.uuid4().hex
        storage = get_storage()
        storage_key = storage.put("library/" + file_id, io.BytesIO(content), len(content), media_type)
        data = {"id": file_id, "user_id": user_id, "session_id": session_id, "filename": filename,
                "title": title, "media_type": media_type, "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(), "storage": storage.name,
                "storage_key": storage_key, "created_at": created_at, "updated_at": created_at,
                "last_opened_at": last_opened_at, "pinned": pinned, "deleted_at": deleted_at}
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO files(id,user_id,session_id,filename,title,media_type,size_bytes,sha256,storage,storage_key,created_at,updated_at,last_opened_at,pinned,deleted_at) "
                "VALUES (:id,:user_id,:session_id,:filename,:title,:media_type,:size_bytes,:sha256,:storage,:storage_key,:created_at,:updated_at,:last_opened_at,:pinned,:deleted_at)",
                data,
            )
        return file_id

    def _preview(self, file_id):
        response = self.client.get("/api/v1/library/{}/preview".format(file_id))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_type_inference_matches_sql_filters_and_counts(self):
        examples = [
            ("NOTE.MD", "text/plain", "document"), ("file.txt", "application/octet-stream", "document"),
            ("report.pdf", "application/pdf", "document"), ("report.doc", "application/msword", "document"),
            ("report.docx", "application/octet-stream", "document"), ("data.csv", "text/plain", "sheet"),
            ("data.xlsx", "application/octet-stream", "sheet"), ("data.xls", "application/octet-stream", "sheet"),
            ("page.html", "text/plain", "web"), ("page.htm", "text/plain", "web"),
            ("photo.PNG", "application/octet-stream", "image"), ("photo.jpg", "application/octet-stream", "image"),
            ("photo.jpeg", "application/octet-stream", "image"), ("photo.gif", "application/octet-stream", "image"),
            ("photo.webp", "application/octet-stream", "image"), ("code.py", "text/plain", "code"),
            ("code.js", "text/plain", "code"), ("code.ts", "text/plain", "code"),
            ("code.json", "text/plain", "code"), ("code.sql", "text/plain", "code"),
            ("code.sh", "text/plain", "code"), ("archive.zip", "application/octet-stream", "archive"),
            ("archive.tar.gz", "application/octet-stream", "archive"), ("archive.tgz", "application/octet-stream", "archive"),
            ("opaque.bin", "application/octet-stream", "other"), ("no-extension", " text/csv; charset=utf-8", "sheet"),
            ("no-json-extension", "application/json", "code"), ("no-image-extension", "image/png", "image"),
        ]
        expected = {kind: 0 for kind in ("all",) + library.LIBRARY_TYPES}
        for filename, media_type, kind in examples:
            with self.subTest(filename=filename):
                self.assertEqual(library.infer_type(filename, media_type), kind)
                self._file(filename=filename, media_type=media_type)
                expected[kind] += 1
                expected["all"] += 1
        all_items = self.client.get("/api/v1/library").json()
        self.assertEqual(all_items["counts"], expected)
        for kind in library.LIBRARY_TYPES:
            with self.subTest(kind=kind):
                filtered = self.client.get("/api/v1/library", params={"type": kind}).json()
                self.assertEqual(len(filtered["items"]), expected[kind])
                self.assertTrue(all(item["type"] == kind for item in filtered["items"]))
                self.assertEqual(filtered["counts"], expected)

    def test_list_join_ownership_soft_delete_and_literal_search(self):
        session_id = self._session()
        own_id = self._file(filename="sales.csv", title="100%_销售", media_type="text/csv", session_id=session_id)
        self._file(filename="other.txt", user_id="other-owner")
        self._file(filename="deleted.txt", deleted_at="2026-10-02T00:00:00+00:00")
        self._file(filename="another.csv", title="100XX销售", media_type="text/csv")
        result = self.client.get("/api/v1/library", params={"q": "%_"}).json()
        self.assertEqual(result["counts"]["all"], 1)
        item = result["items"][0]
        self.assertEqual(item["id"], own_id)
        self.assertEqual(item["session_title"], "资源来源会话")
        self.assertEqual(set(item), {"id", "title", "filename", "media_type", "type", "size_bytes", "origin", "session_id", "session_title", "message_id", "created_at", "last_opened_at", "pinned"})
        self.assertEqual(self.client.get("/api/v1/library", params={"q": "SALES"}).json()["items"][0]["id"], own_id)
        self.assertEqual(self.client.get("/api/v1/library").json()["counts"]["all"], 2)

    def test_session_title_cannot_cross_tenant_boundary(self):
        foreign_session = self._session("foreign-session", "other-owner", "私密标题")
        file_id = self._file(session_id=foreign_session)
        item = self.client.get("/api/v1/library").json()["items"][0]
        self.assertEqual(item["id"], file_id)
        self.assertIsNone(item["session_title"])

    def test_sort_and_cursor_are_stable_for_equal_keys(self):
        self._file(file_id="file-a", title="Alpha", pinned=True)
        self._file(file_id="file-b", title="alpha", last_opened_at="2026-10-03T00:00:00+00:00")
        self._file(file_id="file-c", title="Zulu", created_at="2026-10-02T00:00:00+00:00")
        for sort, expected in [("recent", ["file-b", "file-c", "file-a"]), ("created", ["file-c", "file-b", "file-a"]), ("title", ["file-a", "file-b", "file-c"])]:
            with self.subTest(sort=sort):
                seen = []
                cursor = None
                for _ in range(3):
                    params = {"sort": sort, "limit": 1}
                    if cursor:
                        params["cursor"] = cursor
                    response = self.client.get("/api/v1/library", params=params)
                    self.assertEqual(response.status_code, 200, response.text)
                    page = response.json()
                    self.assertEqual(page["counts"]["all"], 3)
                    seen.extend(item["id"] for item in page["items"])
                    cursor = page["next_cursor"]
                self.assertEqual(seen, expected)
                self.assertIsNone(cursor)

    def test_time_sort_handles_equivalent_timestamps_and_offsets(self):
        self._file(file_id="file-a", created_at="2026-10-01T00:00:00Z")
        self._file(file_id="file-b", created_at="2026-10-01T08:00:00+08:00")
        self._file(file_id="file-c", created_at="2026-09-30T23:00:00-02:00")
        first = self.client.get("/api/v1/library", params={"sort": "created", "limit": 2}).json()
        self.assertEqual([item["id"] for item in first["items"]], ["file-c", "file-b"])
        second = self.client.get("/api/v1/library", params={"sort": "created", "cursor": first["next_cursor"]}).json()
        self.assertEqual([item["id"] for item in second["items"]], ["file-a"])

    def test_invalid_or_mismatched_cursor_is_rejected(self):
        self._file(file_id="file-a")
        self._file(file_id="file-b")
        cursor = self.client.get("/api/v1/library", params={"limit": 1}).json()["next_cursor"]
        for params in [{"cursor": "invalid!"}, {"cursor": cursor, "sort": "title"}, {"cursor": cursor, "type": "code"}, {"cursor": cursor, "q": "different"}, {"type": "bad"}, {"sort": "bad"}, {"limit": 0}]:
            with self.subTest(params=params):
                self.assertEqual(self.client.get("/api/v1/library", params=params).status_code, 422)

    def test_preview_and_patch_cannot_access_foreign_or_deleted_files(self):
        ids = [self._file(user_id="other-owner"), self._file(deleted_at="2026-10-02T00:00:00+00:00"), "missing"]
        with patch.object(library, "storage_for_row") as storage:
            for file_id in ids:
                self.assertEqual(self.client.get("/api/v1/library/{}/preview".format(file_id)).status_code, 404)
                self.assertEqual(self.client.patch("/api/v1/library/{}".format(file_id), json={"title": "变更"}).status_code, 404)
            storage.assert_not_called()

    def test_csv_encodings_quoted_newlines_and_empty_file(self):
        utf8_id = self._file("data.csv", "\ufeff名字,备注\r\n小明,\"第一行\n第二行\"\r\n".encode("utf-8"), "text/csv")
        preview = self._preview(utf8_id)
        self.assertEqual(preview, {"kind": "csv", "columns": ["名字", "备注"], "rows": [["小明", "第一行\n第二行"]], "total_rows": 1, "truncated": False})
        gbk_id = self._file("legacy.csv", "姓名,城市\n小李,杭州\n".encode("gbk"), "text/csv")
        self.assertEqual(self._preview(gbk_id)["rows"], [["小李", "杭州"]])
        empty_id = self._file("empty.csv", b"", "text/csv")
        self.assertEqual(self._preview(empty_id), {"kind": "csv", "columns": [], "rows": [], "total_rows": 0, "truncated": False})

    def test_csv_caps_rows_and_columns(self):
        source = io.StringIO(newline="")
        import csv
        writer = csv.writer(source)
        writer.writerow(["col{}".format(index) for index in range(51)])
        writer.writerows([[str(index)] * 51 for index in range(201)])
        file_id = self._file("wide.csv", source.getvalue().encode("utf-8"), "text/csv")
        preview = self._preview(file_id)
        self.assertEqual(len(preview["columns"]), 50)
        self.assertEqual(len(preview["rows"]), 200)
        self.assertEqual(len(preview["rows"][0]), 50)
        self.assertEqual(preview["total_rows"], 201)
        self.assertTrue(preview["truncated"])

    def test_preview_kinds_and_code_languages(self):
        examples = [("readme.md", "text/markdown", "markdown", None), ("notes.txt", "text/plain", "text", "text"),
                    ("run.py", "text/plain", "text", "python"), ("data.json", "application/json", "text", "json"),
                    ("query.sql", "text/plain", "text", "sql"), ("run.sh", "text/plain", "text", "bash"),
                    ("photo.png", "image/png", "image", None), ("report.pdf", "application/pdf", "none", None),
                    ("data.xlsx", "text/csv", "none", None), ("legacy.xls", "application/vnd.ms-excel", "none", None),
                    ("archive.zip", "application/zip", "none", None), ("opaque.bin", "application/octet-stream", "none", None)]
        for filename, media_type, kind, language in examples:
            with self.subTest(filename=filename):
                preview = self._preview(self._file(filename, b"fixture", media_type))
                self.assertEqual(preview["kind"], kind)
                if language:
                    self.assertEqual(preview["language"], language)
                    self.assertEqual(preview["text"], "fixture")
                if kind in {"none", "image"}:
                    self.assertEqual(preview, {"kind": kind})

    def test_html_preview_only_returns_literal_source(self):
        source = '<script>alert("x")</script><iframe src="https://example.test"></iframe>'
        preview = self._preview(self._file("page.html", source.encode("utf-8"), "text/html"))
        self.assertEqual(preview, {"kind": "html_source", "text": source, "truncated": False})

    def test_storage_reads_have_byte_limits_and_multibyte_boundary(self):
        for filename, media_type, maximum in [("large.md", "text/markdown", library.MARKDOWN_PREVIEW_BYTES), ("large.txt", "text/plain", library.TEXT_PREVIEW_BYTES), ("large.html", "text/html", library.TEXT_PREVIEW_BYTES), ("large.csv", "text/csv", library.TEXT_PREVIEW_BYTES)]:
            with self.subTest(filename=filename):
                file_id = self._file(filename, b"a", media_type)
                with get_connection() as conn:
                    conn.execute("UPDATE files SET size_bytes = ? WHERE id = ?", (maximum + 1, file_id))
                storage = MagicMock()
                storage.get.return_value = b"header\nvalue\n" if filename.endswith(".csv") else b"a" * (maximum - 1) + b"\xe4"
                with patch.object(library, "storage_for_row", return_value=storage):
                    preview = self._preview(file_id)
                self.assertTrue(preview["truncated"])
                self.assertEqual(storage.get.call_args.kwargs, {"max_bytes": maximum})
                if "text" in preview:
                    self.assertEqual(preview["text"], "a" * (maximum - 1))

    def test_preview_updates_last_opened_at_even_for_download_only_files(self):
        file_id = self._file("report.pdf", b"fixture", "application/pdf")
        with patch.object(library, "now", return_value="2026-10-05T10:00:00+00:00"):
            self.assertEqual(self._preview(file_id), {"kind": "none"})
        with get_connection() as conn:
            row = conn.execute("SELECT last_opened_at FROM files WHERE id = ?", (file_id,)).fetchone()
        self.assertEqual(row["last_opened_at"], "2026-10-05T10:00:00+00:00")

    def test_missing_file_content_returns_404(self):
        file_id = self._file()
        with patch.object(library, "storage_for_row") as storage:
            storage.return_value.get.side_effect = FileNotFoundError()
            self.assertEqual(self.client.get("/api/v1/library/{}/preview".format(file_id)).status_code, 404)

    def test_patch_updates_title_and_pin_without_changing_filename(self):
        file_id = self._file("original.txt")
        response = self.client.patch("/api/v1/library/{}".format(file_id), json={"title": "新的标题", "pinned": True})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()
        self.assertEqual(item["title"], "新的标题")
        self.assertEqual(item["filename"], "original.txt")
        self.assertTrue(item["pinned"])
        response = self.client.patch("/api/v1/library/{}".format(file_id), json={"pinned": False})
        self.assertFalse(response.json()["pinned"])
        self.assertEqual(response.json()["title"], "新的标题")
        self.assertEqual(self.client.patch("/api/v1/library/{}".format(file_id), json={}).status_code, 200)

    def test_patch_validation(self):
        file_id = self._file()
        for payload in [{"title": ""}, {"title": " "}, {"title": "字" * 201}, {"title": None}, {"title": 123}, {"pinned": "true"}, {"pinned": 1}, {"pinned": None}, {"origin": "generated"}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.patch("/api/v1/library/{}".format(file_id), json=payload).status_code, 422)

    def test_stored_origins_and_upload_default(self):
        for origin in ("upload", "sandbox_export", "browser_screenshot", "generated"):
            record = store_file_bytes("library-owner", None, origin + ".txt", "text/plain", b"hello", origin=origin)
            with get_connection() as conn:
                row = conn.execute("SELECT origin,title FROM files WHERE id = ?", (record["id"],)).fetchone()
            self.assertEqual(row["origin"], origin)
            self.assertEqual(row["title"], origin + ".txt")
        response = self.client.post("/api/v1/files", files={"upload": ("uploaded.txt", b"hello", "text/plain")})
        self.assertEqual(response.status_code, 201, response.text)
        with get_connection() as conn:
            row = conn.execute("SELECT origin,title FROM files WHERE id = ?", (response.json()["id"],)).fetchone()
        self.assertEqual(dict(row), {"origin": "upload", "title": "uploaded.txt"})

    def test_store_message_id_preserves_source_and_rejects_foreign_message(self):
        session_id = self._session()
        foreign_session = self._session("foreign-session", "other-owner")
        with get_connection() as conn:
            for message_id, user_id, message_session in [("library-message", "library-owner", session_id), ("foreign-message", "other-owner", foreign_session)]:
                conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at,metadata_json,status) VALUES (?,?,?,?,?,?,?,?)", (message_id, user_id, message_session, "assistant", "fixture", "2026-10-01T00:00:00+00:00", "{}", "complete"))
        record = store_file_bytes("library-owner", session_id, "generated.txt", "text/plain", b"hello", origin="generated", message_id="library-message")
        with get_connection() as conn:
            row = conn.execute("SELECT message_id FROM files WHERE id = ?", (record["id"],)).fetchone()
        self.assertEqual(row["message_id"], "library-message")
        with self.assertRaises(ValueError):
            store_file_bytes("library-owner", session_id, "bad.txt", "text/plain", b"hello", message_id="foreign-message")

    def test_library_always_requires_authentication(self):
        anonymous_app = FastAPI()
        anonymous_app.include_router(library.router)
        with patch.dict(os.environ, {"AUTH_REQUIRED": "false"}, clear=False), TestClient(anonymous_app) as client:
            for method, url, body in [("GET", "/api/v1/library", None), ("GET", "/api/v1/library/missing/preview", None), ("PATCH", "/api/v1/library/missing", {"pinned": True})]:
                response = client.request(method, url, json=body)
                self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
