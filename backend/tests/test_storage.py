import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import fastapi.routing
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.testclient import TestClient

# Importing the PostgreSQL fixture first keeps this module consistent with the
# rest of the backend suite and exercises migrations (including 0007).
from tests import pg  # noqa: F401

from app.storage import (
    COSStorage,
    LocalStorage,
    StorageConfigurationError,
    clear_storage_cache,
    get_storage,
)
from plugins_examples.fileservice import FileServiceStorage
from app.upload_limit import UploadSizeLimitMiddleware
from app import main


class _CosNotFound(Exception):
    status_code = 404


class _FakeCos:
    def __init__(self):
        self.put_calls = []
        self.objects = {}
        self.deleted = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"].read()

    def get_object(self, **kwargs):
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            raise _CosNotFound()
        return {"Body": io.BytesIO(self.objects[key])}

    def delete_object(self, **kwargs):
        key = (kwargs["Bucket"], kwargs["Key"])
        self.deleted.append(key)
        if key not in self.objects:
            raise _CosNotFound()
        del self.objects[key]


class LocalStorageTests(unittest.TestCase):
    def test_round_trip_delete_and_traversal_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalStorage(directory)
            self.assertEqual(storage.put("u-hash/file-1", io.BytesIO(b"hello"), 5, "text/plain"), "u-hash/file-1")
            self.assertEqual(storage.get("u-hash/file-1"), b"hello")
            self.assertEqual(b"".join(storage.open_stream("u-hash/file-1")), b"hello")
            storage.delete("u-hash/file-1")
            storage.delete("u-hash/file-1")
            with self.assertRaises(ValueError):
                storage.put("../outside", io.BytesIO(b"bad"), 3, "text/plain")
            with self.assertRaises(ValueError):
                storage.get("/etc/passwd")


class COSStorageTests(unittest.TestCase):
    def test_sse_key_stream_and_idempotent_delete(self):
        client = _FakeCos()
        storage = COSStorage(secret_id="id", secret_key="secret", region="ap-test", bucket="bucket", prefix="luma/", client=client)
        key = storage.put("userhash/file-id", io.BytesIO(b"payload"), 7, "application/octet-stream")
        self.assertEqual(key, "userhash/file-id")
        call = client.put_calls[0]
        self.assertEqual(call["Key"], "luma/userhash/file-id")
        self.assertEqual(call["ServerSideEncryption"], "AES256")
        self.assertEqual(b"".join(storage.open_stream(key)), b"payload")
        self.assertEqual(storage.get(key), b"payload")
        storage.delete(key)
        storage.delete(key)


class FileServiceStorageTests(unittest.TestCase):
    def setUp(self):
        self.requests = []

        def handler(request):
            self.requests.append(request)
            if request.url.path.endswith("/upload"):
                return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"id": 42}})
            if request.url.path.endswith("/42/download"):
                return httpx.Response(200, content=b"part-one-part-two")
            if request.method == "DELETE":
                return httpx.Response(404, json={"code": 404, "message": "missing", "data": None})
            return httpx.Response(500, json={"code": 500, "message": "error", "data": None})

        self.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        self.storage = FileServiceStorage(
            base_url="https://files.example.test",
            app_key="app-key",
            app_secret="app-secret",
            folder_id="folder-1",
            client=self.client,
        )

    def tearDown(self):
        self.client.close()

    def test_auth_upload_download_and_delete(self):
        key = self.storage.put("userhash/file-id", io.BytesIO(b"payload"), 7, "text/plain")
        self.assertEqual(key, "fs:42")
        upload = self.requests[0]
        timestamp = upload.headers["X-Timestamp"]
        expected = base64.b64encode(hmac.new(b"app-secret", timestamp.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(upload.headers["X-App-Key"], "app-key")
        self.assertEqual(upload.headers["X-Signature"], expected)
        body = upload.content
        self.assertIn(b'name="file"', body)
        self.assertIn(b'filename="file-id"', body)
        self.assertNotIn(b"original-name", body)
        self.assertIn(b'name="folder_id"', body)
        self.assertIn(b"folder-1", body)
        self.assertEqual(self.storage.get("fs:42"), b"part-one-part-two")
        self.assertEqual(b"".join(self.storage.open_stream("fs:42")), b"part-one-part-two")
        self.storage.delete("fs:42")

    def test_invalid_id_and_secret_redaction(self):
        with self.assertRaises(ValueError):
            self.storage._remote_id("fs:0")
        with self.assertLogs("plugins_examples.fileservice.storage", level=logging.INFO) as captured:
            self.storage.delete("fs:42")
        joined = "\n".join(captured.output)
        self.assertNotIn("app-secret", joined)
        self.assertNotIn("X-Signature", joined)

    def test_delete_server_error_and_invalid_upload_id(self):
        def error_handler(request):
            if request.method == "DELETE":
                return httpx.Response(503, json={"code": 503, "message": "busy", "data": None})
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"id": "bad"}})

        client = httpx.Client(transport=httpx.MockTransport(error_handler), follow_redirects=False)
        try:
            storage = FileServiceStorage(
                base_url="https://files.example.test",
                app_key="app-key",
                app_secret="app-secret",
                client=client,
            )
            with self.assertRaises(ValueError):
                storage.put("userhash/file-id", io.BytesIO(b"payload"), 7, "text/plain")
            with self.assertRaises(RuntimeError):
                storage.delete("fs:42")
        finally:
            client.close()

    def test_http_url_requires_explicit_development_switch(self):
        with patch.dict(os.environ, {"FILE_SERVICE_ALLOW_HTTP": "false"}, clear=False):
            with self.assertRaises(StorageConfigurationError):
                FileServiceStorage(base_url="http://files.example.test", app_key="k", app_secret="s", client=self.client)


class StorageConfigurationTests(unittest.TestCase):
    def tearDown(self):
        clear_storage_cache()

    def test_missing_cos_configuration_names_only(self):
        values = {"FILE_STORAGE": "cos", "COS_SECRET_ID": "", "COS_SECRET_KEY": "", "COS_BUCKET": "", "COS_REGION": ""}
        with patch.dict(os.environ, values, clear=False):
            with self.assertRaises(StorageConfigurationError) as error:
                get_storage()
        message = str(error.exception)
        self.assertIn("COS_SECRET_ID", message)
        self.assertIn("COS_SECRET_KEY", message)
        self.assertIn("COS_BUCKET", message)
        self.assertIn("COS_REGION", message)
        self.assertNotIn("s3cr3t", message)


class UploadSizeLimitMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.calls = []

        @self.app.middleware("http")
        async def passthrough(request: Request, call_next):
            return await call_next(request)

        @self.app.post("/api/v1/files")
        async def upload(upload: UploadFile = File(...)):
            payload = await upload.read()
            self.calls.append(payload)
            return {"size": len(payload)}

        self.app.add_middleware(UploadSizeLimitMiddleware)

    @staticmethod
    def _multipart(payload):
        boundary = b"b8-upload-test"
        prefix = (
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="upload"; filename="a.bin"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\n"
        )
        suffix = b"\r\n--" + boundary + b"--\r\n"
        return boundary, prefix, payload, suffix

    def test_content_length_rejected_before_route(self):
        with patch.dict(os.environ, {"ASSISTANT_MAX_UPLOAD_BYTES": "10"}, clear=False):
            with TestClient(self.app) as client:
                response = client.post("/api/v1/files", content=b"x" * (1024 * 1024 + 11))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": {"code": "file_too_large", "max_bytes": 10}})
        self.assertEqual(self.calls, [])

    def test_chunked_body_rejected_before_route(self):
        boundary, prefix, payload, suffix = self._multipart(b"x" * (1024 * 1024 + 11))

        def chunks():
            yield prefix
            yield payload
            yield suffix

        with patch.dict(os.environ, {"ASSISTANT_MAX_UPLOAD_BYTES": "10"}, clear=False):
            with TestClient(self.app) as client:
                response = client.post(
                    "/api/v1/files",
                    headers={"content-type": "multipart/form-data; boundary=" + boundary.decode("ascii")},
                    content=chunks(),
                )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": {"code": "file_too_large", "max_bytes": 10}})
        self.assertEqual(self.calls, [])

    def test_normal_upload_is_unchanged(self):
        boundary, prefix, payload, suffix = self._multipart(b"hello")

        def chunks():
            yield prefix
            yield payload
            yield suffix

        with patch.dict(os.environ, {"ASSISTANT_MAX_UPLOAD_BYTES": "10"}, clear=False):
            with TestClient(self.app) as client:
                response = client.post(
                    "/api/v1/files",
                    headers={"content-type": "multipart/form-data; boundary=" + boundary.decode("ascii")},
                    content=chunks(),
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"size": 5})
        self.assertEqual(self.calls, [b"hello"])

    def test_upload_larger_than_staging_memory_is_unchanged(self):
        payload = b"x" * (1024 * 1024 + 1)
        with patch.dict(os.environ, {"ASSISTANT_MAX_UPLOAD_BYTES": str(len(payload))}, clear=False):
            with TestClient(self.app) as client:
                response = client.post(
                    "/api/v1/files",
                    files={"upload": ("large.bin", payload, "application/octet-stream")},
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"size": len(payload)})
        self.assertEqual(self.calls, [payload])


class UploadLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = patch("app.deps.current_user_id", return_value="local")
        cls.identity.start()
        cls.addClassCleanup(cls.identity.stop)
        cls.file_root = tempfile.TemporaryDirectory()
        cls.environment = patch.dict(
            os.environ,
            {
                "ASSISTANT_FILE_ROOT": cls.file_root.name,
                "ASSISTANT_MAX_UPLOAD_BYTES": "64",
                "FILE_STORAGE": "local",
            },
            clear=False,
        )
        cls.environment.start()
        clear_storage_cache()
        pg.reset_tables()
        cls.client = TestClient(main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        clear_storage_cache()
        cls.environment.stop()
        cls.file_root.cleanup()

    @staticmethod
    def _upload_route():
        # FastAPI 0.142 keeps included routers as wrappers. Their effective
        # contexts contain the dependency tree actually used for requests.
        iter_contexts = getattr(fastapi.routing, "iter_route_contexts", None)
        routes = iter_contexts(main.app.routes) if iter_contexts is not None else main.app.routes
        for route in routes:
            if getattr(route, "path", None) == "/api/v1/files" and "POST" in getattr(route, "methods", set()):
                return route
        raise AssertionError("upload route not found")

    @staticmethod
    def _asgi_post(messages, headers):
        sent = []
        received = []
        pending = iter(messages)

        async def receive():
            message = next(pending)
            received.append(message)
            return message

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/api/v1/files", "raw_path": b"/api/v1/files",
            "root_path": "", "query_string": b"", "headers": headers,
            "server": ("testserver", 80), "client": ("testclient", 123),
        }
        asyncio.run(main.app(scope, receive, send))
        return sent, received

    def test_content_length_rejected_before_upload_handler(self):
        body = b"x" * (64 + 1024 * 1024 + 1)
        route = self._upload_route()
        with patch.object(route.dependant, "call", side_effect=AssertionError("upload handler was called")) as handler, \
                patch("starlette.requests.MultiPartParser", side_effect=AssertionError("multipart parser was called")) as parser:
            response = self.client.post(
                "/api/v1/files",
                content=body,
                headers={"content-type": "multipart/form-data; boundary=test"},
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": {"code": "file_too_large", "max_bytes": 64}})
        handler.assert_not_called()
        parser.assert_not_called()

    def test_chunked_body_rejected_before_multipart_parser(self):
        def body():
            yield b"x" * (1024 * 1024)
            yield b"x" * 65

        route = self._upload_route()
        with patch.object(route.dependant, "call", side_effect=AssertionError("upload handler was called")) as handler, \
                patch("starlette.requests.MultiPartParser", side_effect=AssertionError("multipart parser was called")) as parser:
            response = self.client.post(
                "/api/v1/files",
                content=body(),
                headers={"content-type": "multipart/form-data; boundary=test"},
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": {"code": "file_too_large", "max_bytes": 64}})
        handler.assert_not_called()
        parser.assert_not_called()

    def test_content_length_rejected_without_reading_body(self):
        route = self._upload_route()
        with patch.object(route.dependant, "call", side_effect=AssertionError("upload handler was called")) as handler, \
                patch("starlette.requests.MultiPartParser", side_effect=AssertionError("multipart parser was called")) as parser:
            sent, received = self._asgi_post([], [
                (b"content-type", b"multipart/form-data; boundary=test"),
                (b"content-length", str(64 + 1024 * 1024 + 1).encode("ascii")),
            ])
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(received, [])
        handler.assert_not_called()
        parser.assert_not_called()

    def test_chunked_asgi_messages_rejected_before_multipart_parser(self):
        boundary, prefix, payload, suffix = UploadSizeLimitMiddlewareTests._multipart(b"x" * 65)
        messages = [
            {"type": "http.request", "body": prefix, "more_body": True},
            {"type": "http.request", "body": b"x" * (1024 * 1024 + 64 - len(prefix)), "more_body": True},
            {"type": "http.request", "body": payload, "more_body": True},
            {"type": "http.request", "body": suffix, "more_body": False},
        ]
        route = self._upload_route()
        with patch.object(route.dependant, "call", side_effect=AssertionError("upload handler was called")) as handler, \
                patch("starlette.requests.MultiPartParser", side_effect=AssertionError("multipart parser was called")) as parser:
            sent, received = self._asgi_post(messages, [
                (b"content-type", b"multipart/form-data; boundary=" + boundary),
                (b"transfer-encoding", b"chunked"),
            ])
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(json.loads(sent[1]["body"]), {"detail": {"code": "file_too_large", "max_bytes": 64}})
        self.assertEqual(len(received), 3)
        handler.assert_not_called()
        parser.assert_not_called()

    def test_real_oversized_multipart_upload_returns_413(self):
        response = self.client.post(
            "/api/v1/files",
            files={"upload": ("large.bin", b"x" * (64 + 1024 * 1024 + 1), "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": {"code": "file_too_large", "max_bytes": 64}})

    def test_incomplete_upload_disconnect_never_enters_multipart_parser(self):
        boundary, prefix, _, _ = UploadSizeLimitMiddlewareTests._multipart(b"hello")
        route = self._upload_route()
        with patch.object(route.dependant, "call", side_effect=AssertionError("upload handler was called")) as handler, \
                patch("starlette.requests.MultiPartParser", side_effect=AssertionError("multipart parser was called")) as parser:
            sent, received = self._asgi_post([
                {"type": "http.request", "body": prefix, "more_body": True},
                {"type": "http.disconnect"},
            ], [(b"content-type", b"multipart/form-data; boundary=" + boundary)])
        self.assertEqual(sent, [])
        self.assertEqual(len(received), 2)
        handler.assert_not_called()
        parser.assert_not_called()

    def test_normal_upload_is_unchanged(self):
        response = self.client.post(
            "/api/v1/files",
            files={"upload": ("small.txt", b"hello", "text/plain")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["size_bytes"], 5)


if __name__ == "__main__":
    unittest.main()
