"""Focused unit coverage for model-visible sandbox file and preview tools."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests import pg  # noqa: F401

from app.agent.tools import (
    AgentContext,
    _sandbox_file_exists,
    _sandbox_files_export,
    _sandbox_files_import,
    _sandbox_job_start,
    _sandbox_job_status,
    _sandbox_preview,
    _sandbox_write,
    _safe_sandbox_path,
    registry_for,
)


class _Connection:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _query, _params=()):
        return SimpleNamespace(fetchone=lambda: self.row)


class _Files:
    def __init__(self, data=b"payload"):
        self.data = data
        self.writes = {}

    def read(self, path):
        if path not in self.writes and path != "/home/user/workspace/outputs/result.txt":
            raise FileNotFoundError(path)
        return self.writes.get(path, self.data)

    def write(self, path, data):
        self.writes[path] = data


class _Commands:
    def __init__(self, realpath="/home/user/workspace/outputs/result.txt", size="7", directory=False):
        self.realpath = realpath
        self.size = size
        self.directory = directory
        self.calls = []

    def run(self, command, timeout=None):
        self.calls.append((command, timeout))
        if command.startswith("realpath"):
            return SimpleNamespace(exit_code=0, stdout=self.realpath, stderr="")
        if command.startswith("test -d"):
            return SimpleNamespace(exit_code=0 if self.directory else 1, stdout="", stderr="")
        if command.startswith("stat"):
            return SimpleNamespace(exit_code=0, stdout=self.size, stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


class _Box:
    def __init__(self, **kwargs):
        self.files = _Files()
        self.commands = _Commands(**kwargs)

    def get_host(self, port):
        return "%s-box.ap-shanghai.tencentags.com" % port


class SandboxToolsTests(unittest.TestCase):
    def run_async(self, value):
        return asyncio.run(value)

    def test_path_rejects_parent_and_host_paths(self):
        for path in ("../secret", "/tmp/secret", "/home/user/workspace/../secret"):
            with self.assertRaises(ValueError):
                _safe_sandbox_path(path)

    def test_import_rejects_file_owned_by_another_user(self):
        connection = _Connection(None)
        with patch("app.agent.tools.get_connection", return_value=connection), patch(
            "app.agent.tools._sandbox_box", create=True
        ) as connect:
            result = self.run_async(_sandbox_files_import(AgentContext("owner"), {"file_id": "other"}))
        self.assertEqual(result.status, "error")
        connect.assert_not_called()

    def test_export_rejects_realpath_symlink_escape(self):
        box = _Box(realpath="/etc/passwd")
        with patch("app.agent.tools._sandbox_box", return_value=box):
            result = self.run_async(_sandbox_files_export(AgentContext("owner"), {"path": "/home/user/workspace/link"}))
        self.assertEqual(result.status, "error")
        self.assertEqual(len(box.commands.calls), 1)

    def test_export_rejects_size_limit(self):
        box = _Box(size="999")
        with patch("app.agent.tools._sandbox_box", return_value=box), patch(
            "app.agent.tools.files_service.max_upload_size", return_value=10
        ):
            result = self.run_async(_sandbox_files_export(AgentContext("owner"), {"path": "/home/user/workspace/result.txt"}))
        self.assertEqual(result.status, "error")

    def test_export_stores_file_record(self):
        box = _Box(size="7")
        stored = {"id": "file-export", "filename": "result.txt", "size_bytes": 7, "media_type": "application/octet-stream"}
        with patch("app.agent.tools._sandbox_box", return_value=box), patch(
            "app.agent.tools.files_service.store_file_bytes", return_value=stored
        ) as store:
            result = self.run_async(_sandbox_files_export(AgentContext("owner", session_id="session"), {"path": "/home/user/workspace/result.txt"}))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["file_id"], "file-export")
        store.assert_called_once()

    def test_write_preserves_binary_bytes(self):
        box = _Box()
        payload = b"PK\x03\x04\x00\xff\x80"
        _sandbox_write(box, "/home/user/workspace/archive.zip", payload)
        self.assertEqual(box.files.writes["/home/user/workspace/archive.zip"], payload)

    def test_write_does_not_retry_binary_payload_as_text(self):
        class FailingFiles:
            def write(self, _path, _content):
                raise TypeError("bytes are required")

        box = SimpleNamespace(files=FailingFiles())
        with self.assertRaises(TypeError):
            _sandbox_write(box, "/home/user/workspace/archive.zip", b"PK\x03\x04")

    def test_file_exists_uses_sdk_exists_without_reading_file(self):
        class ExistsFiles:
            def exists(self, path):
                self.path = path
                return True

            def read(self, _path):
                raise AssertionError("existence checks must not read file contents")

        files = ExistsFiles()
        box = SimpleNamespace(files=files)
        self.assertTrue(_sandbox_file_exists(box, "/home/user/workspace/archive.zip"))
        self.assertEqual(files.path, "/home/user/workspace/archive.zip")

    def test_file_exists_falls_back_to_sandbox_test_command(self):
        box = _Box()
        self.assertTrue(_sandbox_file_exists(box, "/home/user/workspace/archive.zip"))
        self.assertTrue(any(call[0].startswith("test -e ") for call in box.commands.calls))

    def test_file_exists_falls_back_when_sdk_exists_errors(self):
        class FailingExistsFiles:
            def exists(self, _path):
                raise RuntimeError("provider method unavailable")

        files = FailingExistsFiles()
        box = SimpleNamespace(files=files, commands=_Commands())
        self.assertTrue(_sandbox_file_exists(box, "/home/user/workspace/archive.zip"))
        self.assertTrue(any(call[0].startswith("test -e ") for call in box.commands.calls))

    def test_preview_requires_trusted_https_host(self):
        box = _Box()
        with patch("app.agent.tools._sandbox_box", return_value=box):
            result = self.run_async(_sandbox_preview(AgentContext("owner"), {"port": 8080}))
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.data["url"].startswith("https://"))
        self.assertTrue(result.data["public"])
        self.assertIn("拿到链接的人在沙箱运行期间都能访问", result.data["note"])

    def test_job_start_persists_bounded_runtime_job(self):
        with patch("app.runtime.create_job", return_value={"id": "job-1", "status": "queued"}) as create:
            result = self.run_async(
                _sandbox_job_start(AgentContext("owner", session_id="session"), {"command": "sleep 3", "timeout_seconds": 1900})
            )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["status"], "running")
        payload = create.call_args.args[1]
        self.assertEqual(payload["timeout_seconds"], 1800)

    def test_job_status_is_owner_scoped(self):
        with patch("app.runtime.get_job", return_value={
            "id": "job-1", "type": "sandbox_job", "status": "succeeded",
            "payload": {"command": "echo ok"},
            "result": {"exit_code": 0, "stdout_tail": "ok"},
        }) as get_job:
            result = self.run_async(_sandbox_job_status(AgentContext("owner"), {"job_id": "job-1"}))
        self.assertEqual(result.data["stdout_tail"], "ok")
        get_job.assert_called_once_with("job-1", user_id="owner")

    def test_registry_contains_sandbox_tools(self):
        with patch("app.agent.tools._sandbox_enabled", return_value=True), patch(
            "app.agent.tools.mcp_catalog", return_value=([], {}, [])
        ):
            tools, _ = registry_for("owner", mode="interactive")
        names = {tool.name for tool in tools}
        self.assertTrue(
            {
                "sandbox.python",
                "sandbox.shell",
                "sandbox.files.import",
                "sandbox.files.export",
                "sandbox.job.start",
                "sandbox.job.status",
                "sandbox.preview",
            }.issubset(names)
        )


if __name__ == "__main__":
    unittest.main()
