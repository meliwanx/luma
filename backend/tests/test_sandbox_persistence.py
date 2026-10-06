"""Unit coverage for the persistent E2B workspace lifecycle.

The provider and storage objects are deliberately fakes: these tests exercise
the ordering and lease metadata without starting a real sandbox.
"""

import json
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import agent_runtime


class _Cursor:
    rowcount = 1

    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, row=None, active_rows=None):
        self.row = row
        self.active_rows = list(active_rows or [])
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "status IN ('running','ready','starting','paused')" in query:
            return _Cursor([self.row] if self.row else [])
        if "status IN ('running','ready','starting')" in query:
            return _Cursor(self.active_rows)
        if "ORDER BY last_seen_at" in query or "WHERE id = ?" in query:
            return _Cursor([self.row] if self.row else [])
        return _Cursor()


class _StateConnection(_Connection):
    """Small shared-state connection used by the two-worker race test."""

    def __init__(self, state):
        super().__init__()
        self.state = state
        self.held_locks = []

    def __exit__(self, *_args):
        for lock in reversed(self.held_locks):
            lock.release()
        self.held_locks = []
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))
        if "status IN ('running','ready','starting')" in query:
            return _Cursor([])
        if "ORDER BY last_seen_at" in query or "WHERE id = ?" in query:
            return _Cursor([self.state["row"]] if self.state["row"] else [])
        if "INSERT INTO runtime_leases" in query:
            self.state["row"] = {
                "id": params["id"],
                "user_id_hash": params["user_id_hash"],
                "provider": params["provider"],
                "provider_runtime_id": params["provider_runtime_id"],
                "status": params["status"],
                "capabilities_json": params["capabilities_json"],
                "endpoint": params["endpoint"],
                "started_at": params["started_at"],
                "expires_at": params["expires_at"],
                "last_seen_at": params["last_seen_at"],
                "stopped_at": params["stopped_at"],
                "metadata_json": params["metadata_json"],
            }
        return _Cursor()


class _Box:
    def __init__(self, archive=b"archive", stat_size=None):
        self.pause = MagicMock()
        self.files = SimpleNamespace(
            read=MagicMock(return_value=archive),
            write=MagicMock(),
        )
        self.commands = SimpleNamespace(run=MagicMock(side_effect=self._run))
        self.sandbox_id = "sandbox-new"
        self.stat_size = stat_size

    def _run(self, command, timeout=120):
        if command.startswith("stat "):
            return SimpleNamespace(exit_code=0, stdout=str(self.stat_size or 0), stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


def _settings(**overrides):
    values = {
        "enabled": True,
        "api_mode": "e2b",
        "provider": "tencent-agent-runtime",
        "api_key": "test-key",
        "e2b_domain": "sandbox.example",
        "code_tool": "code",
        "browser_tool": "browser",
        "idle_ttl_seconds": 300,
        "max_instances": 5,
        "max_cpu": 10,
        "max_memory_gib": 10,
        "region": "ap-shanghai",
        "sandbox_backup_max_mb": 1,
        "sandbox_paused_retention_days": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _row(status="running", expires_at=None, metadata=None):
    return {
        "id": "lease-1",
        "user_id_hash": "user-hash",
        "provider": "tencent-agent-runtime",
        "provider_runtime_id": "sandbox-old",
        "status": status,
        "capabilities_json": '["code"]',
        "endpoint": "https://provider.example",
        "started_at": "2026-01-01T00:00:00+00:00",
        "expires_at": expires_at or (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "last_seen_at": "2026-01-01T00:00:00+00:00",
        "stopped_at": None,
        "metadata_json": json.dumps(metadata or {}),
    }


class SandboxPersistenceTests(unittest.TestCase):
    def test_public_lease_redacts_provider_sandbox_id_and_backup_key(self):
        lease = agent_runtime._row_to_lease(
            _row(
                status="paused",
                metadata={
                    "backup": {
                        "storage_key": "private-storage-key",
                        "size_bytes": 4,
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                },
            )
        )
        public = lease.to_public()
        self.assertEqual(public["provider_runtime_id"], "")
        self.assertIsNone(public["endpoint"])
        self.assertNotIn("sandbox-old", json.dumps(public))
        self.assertNotIn("private-storage-key", json.dumps(public))
        self.assertEqual(public["metadata"]["backup"]["size_bytes"], 4)

    def _provider(self, box, connect=None, create=None):
        sandbox = SimpleNamespace(
            connect=MagicMock(side_effect=connect) if connect is not None else MagicMock(return_value=box),
            create=MagicMock(return_value=create or box),
            kill=MagicMock(),
        )
        return {"e2b": SimpleNamespace(Sandbox=sandbox)}, sandbox

    def test_expired_e2b_lease_uploads_backup_before_pause(self):
        old = {"storage_key": "old-key", "size_bytes": 3, "created_at": "old"}
        conn = _Connection(_row(metadata={"backup": old}), active_rows=[])
        box = _Box(archive=b"data", stat_size=4)
        modules, sandbox = self._provider(box)
        events = []
        storage = SimpleNamespace(
            put=MagicMock(side_effect=lambda *args: events.append("put") or "new-key"),
            delete=MagicMock(side_effect=lambda *args: events.append("delete")),
        )
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.object(agent_runtime, "get_storage", return_value=storage), patch.dict(
            sys.modules, modules
        ):
            result = agent_runtime.reap_expired_leases()
        self.assertEqual(result["stopped"], 1)
        self.assertEqual(result["failed"], 0)
        box.pause.assert_called_once_with(keep_memory=False)
        self.assertEqual(events, ["put", "delete"])
        saved = [params for query, params in conn.queries if "INSERT INTO runtime_leases" in query]
        self.assertTrue(any(params.get("status") == "paused" for params in saved))
        self.assertTrue(any(json.loads(params["metadata_json"])["backup"]["storage_key"] == "new-key" for params in saved))

    def test_archive_includes_local_and_excludes_disposable_caches(self):
        box = _Box(archive=b"data", stat_size=4)
        with patch.object(agent_runtime, "get_storage", return_value=SimpleNamespace(put=MagicMock(return_value="key"))):
            result = agent_runtime._sandbox_archive(box, _settings(), "user-hash")
        self.assertEqual(result["storage_key"], "key")
        command = box.commands.run.call_args_list[0].args[0]
        self.assertIn("workspace .local", command)
        self.assertIn("workspace/**/__pycache__", command)
        self.assertIn("workspace/**/node_modules/.cache", command)
        self.assertIn(".local/.cache", command)

    def test_e2b_start_requests_pause_lifecycle(self):
        box = _Box()
        modules, sandbox = self._provider(box)
        with patch.object(agent_runtime, "_e2b_pause_lifecycle", return_value={"on_timeout": "pause"}), patch.dict(
            sys.modules, modules
        ):
            agent_runtime._e2b_start(_settings(), "code")
        kwargs = sandbox.create.call_args.kwargs
        self.assertEqual(kwargs["lifecycle"], {"on_timeout": "pause"})
        self.assertEqual(kwargs["timeout"], 300)

    def test_e2b_start_uses_timeout_cushion_without_lifecycle_support(self):
        box = _Box()
        modules, sandbox = self._provider(box)
        with patch.object(agent_runtime, "_e2b_pause_lifecycle", return_value=None), patch.dict(
            sys.modules, modules
        ):
            agent_runtime._e2b_start(_settings(), "code")
        self.assertEqual(sandbox.create.call_args.kwargs["timeout"], 900)
        self.assertNotIn("lifecycle", sandbox.create.call_args.kwargs)

    def test_expired_active_connect_resumes_same_provider(self):
        conn = _Connection(_row(status="running"), active_rows=[])
        box = _Box()
        modules, sandbox = self._provider(box)
        storage = SimpleNamespace(put=MagicMock(), delete=MagicMock())
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.object(agent_runtime, "get_storage", return_value=storage), patch.dict(sys.modules, modules):
            result = agent_runtime.connect_user_sandbox("user-1")
        self.assertIs(result, box)
        sandbox.connect.assert_called_once_with("sandbox-old", api_key="test-key", domain="sandbox.example")
        sandbox.create.assert_not_called()
        box.pause.assert_not_called()
        storage.put.assert_not_called()
        saved = [params for query, params in conn.queries if "INSERT INTO runtime_leases" in query]
        self.assertEqual(saved[-1]["status"], "running")

    def test_ensure_expired_active_resumes_same_provider(self):
        conn = _Connection(_row(status="running"), active_rows=[])
        box = _Box()
        modules, sandbox = self._provider(box)
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.dict(sys.modules, modules):
            result = agent_runtime.ensure_user_runtime("user-1", ("code",))
        self.assertEqual(result.status, "running")
        sandbox.connect.assert_called_once_with("sandbox-old", api_key="test-key", domain="sandbox.example")
        sandbox.create.assert_not_called()

    def test_touch_renews_provider_timeout_and_logs_connect_failures(self):
        expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        conn = _Connection(_row(status="running", expires_at=expires), active_rows=[])
        box = _Box()
        box.set_timeout = MagicMock()
        modules, _ = self._provider(box)
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.dict(sys.modules, modules):
            agent_runtime.touch_user_runtime("user-1")
        box.set_timeout.assert_called_once_with(300)

    def test_timeout_renewal_failure_logs_only_exception_type(self):
        box = _Box()
        box.set_timeout = MagicMock(side_effect=RuntimeError("provider secret payload"))
        logger = MagicMock()
        with patch.object(agent_runtime, "logger", logger):
            agent_runtime._e2b_refresh_timeout(box, _settings(), "user-hash")
        box.set_timeout.assert_called_once_with(300)
        logger.warning.assert_called_once()
        message, user_hash, error_type = logger.warning.call_args.args
        self.assertIn("sandbox timeout renewal failed", message)
        self.assertEqual(user_hash, "user-hash")
        self.assertEqual(error_type, "RuntimeError")
        self.assertNotIn("provider secret payload", str(logger.warning.call_args))

    def test_paused_capacity_evicts_only_after_backup(self):
        paused = agent_runtime._row_to_lease(_row(status="paused", metadata={"paused_at": "2026-01-01T00:00:00+00:00"}))
        self.assertIsNotNone(paused)
        box = _Box()
        settings = _settings(sandbox_max_paused=1)
        conn = _Connection(_row(status="paused", metadata={"paused_at": "2026-01-01T00:00:00+00:00"}))
        with patch.object(agent_runtime, "_paused_count", side_effect=[1, 0]), patch.object(
            agent_runtime, "_paused_candidates", return_value=[paused]
        ), patch.object(agent_runtime, "_e2b_connect", return_value=box) as connect, patch.object(
            agent_runtime, "_sandbox_archive", return_value={"storage_key": "new-key", "size_bytes": 4}
        ), patch.object(agent_runtime, "_stop_provider") as stop, patch.object(
            agent_runtime, "_save_lease_hash"
        ):
            self.assertTrue(agent_runtime._evict_paused_for_capacity(settings, conn))
        connect.assert_called_once()
        stop.assert_called_once_with(settings, "sandbox-old")

    def test_paused_capacity_backup_failure_repauses_candidate(self):
        paused = agent_runtime._row_to_lease(
            _row(status="paused", metadata={"paused_at": "2026-01-01T00:00:00+00:00"})
        )
        self.assertIsNotNone(paused)
        box = _Box()
        settings = _settings(sandbox_max_paused=1)
        conn = _Connection(_row(status="paused", metadata={"paused_at": "2026-01-01T00:00:00+00:00"}))
        with patch.object(agent_runtime, "_paused_count", return_value=1), patch.object(
            agent_runtime, "_paused_candidates", return_value=[paused]
        ), patch.object(agent_runtime, "_e2b_connect", return_value=box), patch.object(
            agent_runtime, "_sandbox_archive", side_effect=RuntimeError("archive failed")
        ), patch.object(agent_runtime, "_sandbox_pause") as pause, patch.object(
            agent_runtime, "_stop_provider"
        ) as stop:
            self.assertFalse(agent_runtime._evict_paused_for_capacity(settings, conn))
        pause.assert_called_once_with(box)
        stop.assert_not_called()

    def test_pause_quota_error_evicts_and_retries_once(self):
        conn = _Connection(_row(status="running"), active_rows=[])
        box = _Box(archive=b"data", stat_size=4)
        modules, _ = self._provider(box)
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.object(
            agent_runtime, "_sandbox_archive", return_value={"storage_key": "new-key", "size_bytes": 4}
        ), patch.object(agent_runtime, "_evict_paused_for_capacity"), patch.object(
            agent_runtime, "_sandbox_pause", side_effect=[RuntimeError("LimitExceeded"), None]
        ) as pause, patch.dict(sys.modules, modules):
            result = agent_runtime.reap_expired_leases()
        self.assertEqual(result["stopped"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(pause.call_count, 2)

    def test_backup_too_large_still_pauses_and_keeps_old_backup(self):
        old = {"storage_key": "old-key", "size_bytes": 3, "created_at": "old"}
        conn = _Connection(_row(metadata={"backup": old}), active_rows=[])
        box = _Box(stat_size=2 * 1024 * 1024)
        modules, _ = self._provider(box)
        storage = SimpleNamespace(put=MagicMock(), delete=MagicMock())
        with patch.object(agent_runtime, "config", return_value=_settings(sandbox_backup_max_mb=1)), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.object(agent_runtime, "get_storage", return_value=storage), patch.dict(
            sys.modules, modules
        ):
            agent_runtime.reap_expired_leases()
        storage.put.assert_not_called()
        storage.delete.assert_not_called()
        box.pause.assert_called_once()
        saved = [params for query, params in conn.queries if "INSERT INTO runtime_leases" in query]
        metadata = [json.loads(params["metadata_json"]) for params in saved if params.get("status") == "paused"][-1]
        self.assertEqual(metadata["backup"]["storage_key"], "old-key")
        self.assertEqual(metadata["backup_skipped"], "too_large")

    def test_upload_failure_does_not_delete_old_backup(self):
        old = {"storage_key": "old-key", "size_bytes": 3, "created_at": "old"}
        conn = _Connection(_row(metadata={"backup": old}), active_rows=[])
        box = _Box(archive=b"data", stat_size=4)
        modules, _ = self._provider(box)
        storage = SimpleNamespace(
            put=MagicMock(side_effect=RuntimeError("upload failed")),
            delete=MagicMock(),
        )
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.object(agent_runtime, "get_storage", return_value=storage), patch.dict(
            sys.modules, modules
        ):
            result = agent_runtime.reap_expired_leases()
        self.assertEqual(result["stopped"], 1)
        storage.delete.assert_not_called()
        box.pause.assert_called_once()

    def test_paused_connect_resumes_without_creating(self):
        conn = _Connection(_row(status="paused", metadata={"paused_at": "2026-01-01T00:00:00+00:00"}), active_rows=[])
        box = _Box()
        modules, sandbox = self._provider(box)
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.dict(sys.modules, modules):
            result = agent_runtime.connect_user_sandbox("user-1")
        self.assertIs(result, box)
        sandbox.connect.assert_called_once_with("sandbox-old", api_key="test-key", domain="sandbox.example")
        sandbox.create.assert_not_called()
        saved = [params for query, params in conn.queries if "INSERT INTO runtime_leases" in query]
        self.assertTrue(any(params.get("status") == "running" for params in saved))

    def test_concurrent_connect_creates_one_sandbox(self):
        state = {"row": None}
        connection = _StateConnection(state)
        locks = {}
        locks_guard = threading.Lock()
        box = _Box()
        modules, sandbox = self._provider(box)

        def advisory_lock(conn, key):
            with locks_guard:
                lock = locks.setdefault(key, threading.Lock())
            lock.acquire()
            conn.held_locks.append(lock)

        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", side_effect=lambda: connection
        ), patch.object(agent_runtime, "_pg_advisory_lock", side_effect=advisory_lock), patch.dict(
            sys.modules, modules
        ):
            errors = []

            def connect():
                try:
                    agent_runtime.connect_user_sandbox("same-user")
                except Exception as exc:  # pragma: no cover - assertion below reports it
                    errors.append(exc)

            threads = [threading.Thread(target=connect) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(sandbox.create.call_count, 1)

    def test_resume_failure_creates_and_restores_from_backup(self):
        old = {"storage_key": "old-key", "size_bytes": 3, "created_at": "old"}
        conn = _Connection(_row(status="paused", metadata={"backup": old}), active_rows=[])
        replacement = _Box()
        modules, sandbox = self._provider(replacement, connect=RuntimeError("not found"), create=replacement)
        storage = SimpleNamespace(get=MagicMock(return_value=b"archive"), put=MagicMock(), delete=MagicMock())
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.object(agent_runtime, "get_storage", return_value=storage), patch.dict(
            sys.modules, modules
        ):
            result = agent_runtime.connect_user_sandbox("user-1")
        self.assertIs(result, replacement)
        sandbox.create.assert_called_once()
        replacement.files.write.assert_called_once_with("/tmp/ws.tgz", b"archive")
        saved = [params for query, params in conn.queries if "INSERT INTO runtime_leases" in query]
        metadata = [json.loads(params["metadata_json"]) for params in saved][-1]
        self.assertTrue(metadata["restored_from_backup"])

    def test_long_paused_lease_is_killed_but_backup_is_kept(self):
        old = {"storage_key": "old-key", "size_bytes": 3, "created_at": "old"}
        paused_at = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        conn = _Connection(
            _row(status="paused", metadata={"backup": old, "paused_at": paused_at}),
            active_rows=[],
        )
        box = _Box()
        modules, sandbox = self._provider(box)
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.object(agent_runtime, "get_storage", return_value=SimpleNamespace()), patch.dict(
            sys.modules, modules
        ):
            result = agent_runtime.reap_expired_leases()
        self.assertEqual(result["stopped"], 1)
        sandbox.kill.assert_called_once_with("sandbox-old", api_key="test-key", domain="sandbox.example")
        saved = [params for query, params in conn.queries if "INSERT INTO runtime_leases" in query]
        metadata = json.loads(saved[-1]["metadata_json"])
        self.assertEqual(saved[-1]["status"], "stopped")
        self.assertEqual(metadata["backup"]["storage_key"], "old-key")

    def test_reset_kills_and_deletes_backup(self):
        old = {"storage_key": "old-key", "size_bytes": 3, "created_at": "old"}
        conn = _Connection(_row(metadata={"backup": old}), active_rows=[])
        box = _Box()
        modules, sandbox = self._provider(box)
        storage = SimpleNamespace(delete=MagicMock())
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.object(agent_runtime, "get_storage", return_value=storage), patch.dict(
            sys.modules, modules
        ):
            agent_runtime.reset_user_workspace("user-1")
        sandbox.kill.assert_called_once_with("sandbox-old", api_key="test-key", domain="sandbox.example")
        storage.delete.assert_called_once_with("old-key")
        saved = [params for query, params in conn.queries if "INSERT INTO runtime_leases" in query]
        self.assertEqual(saved[-1]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
