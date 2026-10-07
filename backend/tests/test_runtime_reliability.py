"""Unit and PostgreSQL integration coverage for runtime reliability."""

import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# Import the shared fixture before importing app modules.  It creates an
# isolated schema in luma_test and configures the DB facade for this process.
from tests.pg import reset_tables

from app import agent_runtime, provider, runtime
from app.db import get_connection


class RuntimePolicyTests(unittest.TestCase):
    def test_approval_policy_is_centralised(self):
        for job_type in ("create_memory", "create_task"):
            self.assertTrue(runtime.job_requires_approval(job_type, {}), job_type)
        self.assertFalse(runtime.job_requires_approval("shell", {}))
        for job_type in ("briefing", "files"):
            self.assertFalse(runtime.job_requires_approval(job_type, {}), job_type)
        self.assertTrue(runtime.job_requires_approval("unknown_future_job", {}))

        self.assertFalse(runtime.job_requires_approval("agent_run", {"engine": "legacy"}))
        for write_tool in ("create_memory", "create_task"):
            self.assertTrue(
                runtime.job_requires_approval("agent_run", {"allowed_tools": [write_tool]}),
                write_tool,
            )
        self.assertFalse(runtime.job_requires_approval("agent_run", {"allowed_tools": ["briefing", "files"]}))

    def test_server_clamps_retry_budget(self):
        self.assertEqual(runtime._max_attempts({"max_attempts": 99}), 3)
        self.assertEqual(runtime._max_attempts({"max_attempts": 0}), 3)
        self.assertEqual(runtime._max_attempts({"max_attempts": 2}), 2)
        with self.assertRaises(ValueError):
            runtime._max_attempts({"max_attempts": "many"})

    def test_idempotency_classification(self):
        for job_type in ("briefing", "files", "list_tasks", "list_memories"):
            self.assertTrue(runtime._job_is_idempotent({"type": job_type, "payload": {}}), job_type)
        for job_type in ("shell", "create_task", "create_memory"):
            self.assertFalse(runtime._job_is_idempotent({"type": job_type, "payload": {}}), job_type)
        self.assertTrue(
            runtime._job_is_idempotent(
                {"type": "agent_run", "payload": {"allowed_tools": ["briefing"]}}
            )
        )
        self.assertFalse(
            runtime._job_is_idempotent(
                {"type": "agent_run", "payload": {"allowed_tools": ["create_memory"]}}
            )
        )
        self.assertTrue(runtime._job_is_idempotent({"type": "agent_run", "payload": {"engine": "legacy"}}))

    def test_agent_result_message_names_actual_tool(self):
        from app.agent.loop import AgentOutcome

        outcome = AgentOutcome(
            reply="已读取文件。",
            tool_calls=[{"tool": "luma.files.list", "status": "ok"}],
        )
        with patch.dict(os.environ, {"LUMA_PROVIDER": "llm"}, clear=False), patch(
            "app.agent.loop.run_agent", new_callable=AsyncMock, return_value=outcome
        ) as run_agent, patch.object(
            provider, "get_config", side_effect=AssertionError("The test must not load model credentials")
        ):
            result = runtime._execute_agent_run(
                {"prompt": "列出文件", "allowed_tools": ["files"]}, user_id="u"
            )
        run_agent.assert_awaited_once()
        context, messages = run_agent.call_args.args
        self.assertEqual(context.allowed_tools, ["luma.files.list"])
        self.assertEqual(messages, [{"role": "user", "content": "列出文件"}])
        self.assertEqual(result["tool"], "luma.files.list")
        self.assertIn("文件", result["message"])
        self.assertNotEqual(result["message"], "已保存记忆。")

    def test_local_agent_run_does_not_load_credentials_or_plan_tools(self):
        from app.services.chat import local_reply

        with patch.dict(
            os.environ, {"LUMA_PROVIDER": "local", "LLM_API_KEY": "", "MIMO_API_KEY": ""}, clear=False
        ), patch("app.agent.loop.run_agent", new_callable=AsyncMock) as run_agent, patch.object(
            provider, "get_config", side_effect=AssertionError("Local mode must not load model credentials")
        ) as get_config, patch.object(runtime, "execute_tool") as execute_tool:
            result = runtime.execute_job(
                "agent_run", {"prompt": "列出文件", "allowed_tools": ["files"]}, user_id="u"
            )
        run_agent.assert_not_called()
        get_config.assert_not_called()
        execute_tool.assert_not_called()
        self.assertEqual(result["provider"], "local")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["message"], local_reply("列出文件"))
        self.assertEqual(result["tool_calls"], [])
        self.assertNotIn("tool", result)


class RuntimeProviderSafetyTests(unittest.TestCase):
    def test_cloud_api_shell_does_not_use_e2b_connect(self):
        lease = SimpleNamespace(provider_runtime_id="runtime-1")
        with patch.object(runtime, "ensure_user_runtime", return_value=lease), patch.object(
            runtime.agent_runtime, "connect_user_sandbox", side_effect=runtime.AgentRuntimeUnavailable("unavailable"), create=True
        ), patch.dict(
            os.environ,
            {"AGENT_RUNTIME_MODE": "cloud-api", "E2B_API_KEY": "unused", "E2B_DOMAIN": "unused"},
            clear=False,
        ), patch.dict("sys.modules", {"e2b": None}):
            with self.assertRaises((runtime.AgentRuntimeUnavailable, RuntimeError)):
                runtime._execute_shell({"command": "echo ok"}, user_id="u")


class RuntimeLeasePureTests(unittest.TestCase):
    def test_expired_active_lease_does_not_consume_budget(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [
            {"status": "running", "expires_at": (now - timedelta(seconds=1)).isoformat()},
            {"status": "ready", "expires_at": (now + timedelta(seconds=1)).isoformat()},
        ]
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = rows
        self.assertEqual(agent_runtime._active_runtime_count(conn, now), 1)

    @staticmethod
    def _lease_row(expires_at):
        return {
            "id": "lease-1",
            "user_id_hash": "user-hash",
            "provider": "test-provider",
            "provider_runtime_id": "runtime-1",
            "status": "running",
            "capabilities_json": '["code"]',
            "endpoint": "https://runtime.example",
            "started_at": "2026-01-01T00:00:00+00:00",
            "expires_at": expires_at,
            "last_seen_at": "2026-01-01T00:00:00+00:00",
            "stopped_at": None,
            "metadata_json": "{}",
        }

    def _connection(self, row):
        class Conn:
            def __init__(self, value):
                self.value = value
                self.queries = []
                self.rowcount = 1

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query, params=()):
                self.queries.append((query, params))
                return self

            def fetchall(self):
                return [self.value]

            def fetchone(self):
                return self.value

        return Conn(row)

    @staticmethod
    def _job_row(job_type, attempts=1, max_attempts=3):
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        return {
            "id": "job-1",
            "user_id": "user-1",
            "type": job_type,
            "status": "running",
            "payload_json": '{"max_attempts": %d}' % max_attempts,
            "result_json": "{}",
            "error": None,
            "attempts": attempts,
            "run_at": old,
            "created_at": old,
            "updated_at": old,
        }

    def test_stale_idempotent_job_is_requeued(self):
        row = self._job_row("briefing", attempts=1)
        conn = self._connection(row)
        with patch.object(runtime, "get_connection", return_value=conn), patch.object(
            runtime, "append_activity"
        ) as activity:
            self.assertEqual(runtime.reap_stale_jobs(), 1)
        self.assertTrue(any("status = 'queued'" in query for query, _ in conn.queries))
        self.assertEqual(activity.call_args.args[0], "job_requeued")
        self.assertIn("执行中断", activity.call_args.args[2])

    def test_stale_non_idempotent_job_is_failed_without_retry(self):
        row = self._job_row("shell", attempts=1)
        conn = self._connection(row)
        with patch.object(runtime, "get_connection", return_value=conn), patch.object(
            runtime, "append_activity"
        ) as activity:
            self.assertEqual(runtime.reap_stale_jobs(), 1)
        self.assertTrue(any("status = 'failed'" in query for query, _ in conn.queries))
        self.assertEqual(activity.call_args.args[0], "job_failed")
        self.assertIn("请重新提交", activity.call_args.args[2])

    def test_reaper_stops_expired_provider_before_marking_stopped(self):
        row = self._lease_row("2020-01-01T00:00:00+00:00")
        conn = self._connection(row)
        settings = SimpleNamespace(api_mode="gateway", enabled=True)
        with patch.object(agent_runtime, "get_connection", return_value=conn), patch.object(
            agent_runtime, "config", return_value=settings
        ), patch.object(agent_runtime, "_stop_provider") as stop:
            result = agent_runtime.reap_expired_leases()
        self.assertEqual(result, {"stopped": 1, "failed": 0, "expired": 1})
        stop.assert_called_once_with(settings, "runtime-1")
        # The local write follows the provider stop and carries stopped state.
        writes = [query for query, _ in conn.queries if "INSERT INTO runtime_leases" in query]
        self.assertTrue(writes)

    def test_reaper_keeps_expired_lease_for_retry_after_provider_failure(self):
        row = self._lease_row("2020-01-01T00:00:00+00:00")
        conn = self._connection(row)
        settings = SimpleNamespace(api_mode="gateway", enabled=True)
        with patch.object(agent_runtime, "get_connection", return_value=conn), patch.object(
            agent_runtime, "config", return_value=settings
        ), patch.object(
            agent_runtime,
            "_stop_provider",
            side_effect=agent_runtime.AgentRuntimeUnavailable("provider response contains SECRET"),
        ), patch.object(agent_runtime.logger, "warning") as warning:
            result = agent_runtime.reap_expired_leases()
        self.assertEqual(result, {"stopped": 0, "failed": 1, "expired": 1})
        warning_text = " ".join(str(call) for call in warning.call_args_list)
        self.assertNotIn("SECRET", warning_text)


class RuntimeWorkerUnitTests(unittest.TestCase):
    """Exercise worker boundaries without opening the development database."""

    @staticmethod
    def _job(job_type="shell", attempts=1, payload=None):
        return {
            "id": "job-worker-1",
            "user_id": "user-1",
            "type": job_type,
            "status": "running",
            "attempts": attempts,
            "payload": payload or {},
        }

    def test_worker_runs_shell_without_approval(self):
        job = self._job("shell", payload={"command": "echo ok"})
        with patch.object(runtime, "_approval_granted", return_value=False), patch.object(
            runtime, "_mark_job_failed"
        ) as mark_failed, patch.object(runtime, "execute_job") as execute:
            self.assertTrue(runtime._run_claimed_job(job))
        execute.assert_called_once()
        mark_failed.assert_not_called()

    def test_non_idempotent_worker_error_is_failed_without_retry(self):
        job = self._job("shell", payload={"max_attempts": 3})
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        with patch.object(runtime, "_approval_granted", return_value=True), patch.object(
            runtime, "execute_job", side_effect=RuntimeError("provider down")
        ), patch.object(runtime, "append_activity"), patch.object(
            runtime, "_heartbeat_loop"
        ), patch.object(runtime, "get_connection", return_value=conn):
            self.assertTrue(runtime._run_claimed_job(job))
        statements = [str(call.args[0]) for call in conn.execute.call_args_list]
        self.assertTrue(any("status = 'failed'" in statement for statement in statements))
        self.assertFalse(any("status = 'queued'" in statement for statement in statements))

    def test_postgres_claim_uses_skip_locked_and_same_connection_update(self):
        old = datetime.now(timezone.utc).isoformat()
        row = {
            "id": "job-claim-1",
            "user_id": "user-1",
            "type": "briefing",
            "status": "queued",
            "payload_json": "{}",
            "result_json": "{}",
            "error": None,
            "attempts": 0,
            "run_at": old,
            "created_at": old,
            "updated_at": old,
        }

        class Cursor:
            rowcount = 1

            def __init__(self, rows=None):
                self.rows = rows or []

            def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.queries = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query, params=()):
                self.queries.append((query, params))
                return Cursor([row]) if query.lstrip().upper().startswith("SELECT") else Cursor()

        conn = Connection()
        with patch.object(runtime, "get_connection", return_value=conn):
            claimed = runtime._claim_due_jobs(1)
        self.assertEqual(len(claimed), 1)
        self.assertIn("FOR UPDATE SKIP LOCKED", conn.queries[0][0])
        self.assertIn("status = 'running'", conn.queries[1][0])
        self.assertEqual(len(conn.queries), 2)

    def test_shell_touches_lease_and_does_not_release_after_command(self):
        box = SimpleNamespace(
            commands=SimpleNamespace(
                run=MagicMock(return_value=SimpleNamespace(exit_code=0, stdout="ok", stderr=""))
            )
        )
        sandbox_api = SimpleNamespace(Sandbox=SimpleNamespace(connect=MagicMock(return_value=box)))
        lease = SimpleNamespace(provider_runtime_id="runtime-1")
        settings = SimpleNamespace(api_mode="e2b")
        with patch.object(runtime, "agent_runtime_config", return_value=settings), patch.object(
            runtime, "ensure_user_runtime", return_value=lease
        ), patch.object(runtime.agent_runtime, "connect_user_sandbox", return_value=box, create=True) as connect, patch.object(runtime, "touch_user_runtime") as touch, patch.dict(
            "sys.modules", {"e2b": sandbox_api}
        ):
            result = runtime._execute_shell({"command": "echo ok"}, user_id="u")
        self.assertEqual(result["exit_code"], 0)
        touch.assert_called_once_with("u")
        connect.assert_called_once_with("u")

    def test_maintenance_calls_reapers_in_order(self):
        calls = []
        with patch.object(
            runtime, "reap_stale_jobs", side_effect=lambda: calls.append("jobs") or 2
        ), patch.object(
            agent_runtime, "reap_expired_leases", side_effect=lambda: calls.append("leases") or {"stopped": 1}
        ), patch(
            "app.services.secret_vault.cleanup_expired_secrets", side_effect=lambda: calls.append("secrets") or 3
        ):
            self.assertEqual(runtime.run_maintenance(), {"stale_jobs": 2, "expired_leases": 1})
        self.assertEqual(calls, ["jobs", "leases", "secrets"])

    def test_worker_maintenance_sweeps_files_and_recovers_generation(self):
        async def run_worker():
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            sweep_started = threading.Event()

            def finish_due_jobs():
                sweep_started.wait(timeout=1)
                loop.call_soon_threadsafe(stop_event.set)

            def sweep(_limit):
                sweep_started.set()
                return {"deleted": 1, "failed": 0}

            with patch.object(runtime, "run_maintenance"), patch.object(
                runtime, "run_due_jobs", side_effect=finish_due_jobs
            ), patch(
                "app.services.files.sweep_deleted_files", side_effect=sweep
            ) as sweep, patch(
                "app.services.generation.manager.recover_stale", new_callable=AsyncMock
            ) as recover:
                await runtime.worker_loop(stop_event, interval_seconds=0.01)
            sweep.assert_called_once_with(20)
            recover.assert_awaited_once_with()

        asyncio.run(run_worker())

    def test_worker_keeps_running_when_file_sweep_fails(self):
        async def run_worker():
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            sweep_started = threading.Event()

            def finish_due_jobs():
                sweep_started.wait(timeout=1)
                loop.call_soon_threadsafe(stop_event.set)

            def fail_sweep(_limit):
                sweep_started.set()
                raise RuntimeError("storage down")

            with patch.object(runtime, "run_maintenance"), patch.object(
                runtime, "run_due_jobs", side_effect=finish_due_jobs
            ), patch(
                "app.services.files.sweep_deleted_files", side_effect=fail_sweep
            ), patch(
                "app.services.generation.manager.recover_stale", new_callable=AsyncMock
            ), patch.object(runtime.logger, "warning") as warning:
                await runtime.worker_loop(stop_event, interval_seconds=0.01)
                # The detached task's callback runs on the event loop after
                # the worker's due-job thread has released the stop event.
                await asyncio.sleep(0)
            self.assertTrue(any("deleted-file sweep failed" in str(call) for call in warning.call_args_list))

        asyncio.run(run_worker())

    def test_worker_keeps_running_due_jobs_while_file_sweep_is_slow(self):
        async def run_worker():
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            sweep_started = threading.Event()
            release_sweep = threading.Event()
            due_calls = 0

            def slow_sweep(_limit):
                sweep_started.set()
                release_sweep.wait()
                return {"deleted": 1, "failed": 0}

            def run_due():
                nonlocal due_calls
                due_calls += 1
                if due_calls == 1:
                    # Ensure the first due-job pass overlaps the remote
                    # storage call rather than racing it before startup.
                    sweep_started.wait(timeout=1)
                if due_calls >= 3:
                    release_sweep.set()
                    loop.call_soon_threadsafe(stop_event.set)

            with patch.object(runtime, "run_maintenance"), patch.object(
                runtime, "run_due_jobs", side_effect=run_due
            ), patch(
                "app.services.files.sweep_deleted_files", side_effect=slow_sweep
            ) as sweep, patch(
                "app.services.generation.manager.recover_stale", new_callable=AsyncMock
            ):
                await runtime.worker_loop(stop_event, interval_seconds=0.01)

            self.assertGreaterEqual(due_calls, 3)
            sweep.assert_called_once_with(20)

        asyncio.run(run_worker())

    def test_worker_does_not_overlap_file_sweeps(self):
        async def run_worker():
            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            sweep_started = threading.Event()
            release_sweep = threading.Event()
            due_calls = 0
            monotonic_values = iter((0.0, 61.0, 122.0))

            def monotonic():
                return next(monotonic_values, 122.0)

            def slow_sweep(_limit):
                sweep_started.set()
                release_sweep.wait()
                return {"deleted": 1, "failed": 0}

            def run_due():
                nonlocal due_calls
                due_calls += 1
                if due_calls == 1:
                    sweep_started.wait(timeout=1)
                if due_calls >= 2:
                    release_sweep.set()
                    loop.call_soon_threadsafe(stop_event.set)

            with patch.object(runtime, "_runtime_monotonic", side_effect=monotonic), patch.object(
                runtime, "run_maintenance"
            ), patch.object(runtime, "run_due_jobs", side_effect=run_due), patch(
                "app.services.files.sweep_deleted_files", side_effect=slow_sweep
            ) as sweep, patch(
                "app.services.generation.manager.recover_stale", new_callable=AsyncMock
            ):
                await runtime.worker_loop(stop_event, interval_seconds=0.01)

            self.assertGreaterEqual(due_calls, 2)
            sweep.assert_called_once_with(20)

        asyncio.run(run_worker())

    def test_shutdown_runtime_wakes_heartbeats_and_releases_executor(self):
        executor = MagicMock()
        heartbeat_stop = threading.Event()
        with patch.object(runtime, "_RUNTIME_EXECUTOR", executor), patch.object(
            runtime, "_RUNTIME_EXECUTOR_SIZE", 2
        ), patch.object(runtime, "_RUNTIME_HEARTBEAT_STOPS", {heartbeat_stop}):
            runtime.shutdown_runtime()
        self.assertTrue(heartbeat_stop.is_set())
        executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)


@unittest.skipUnless(os.getenv("DB_SCHEMA"), "需要 PG 测试基建")
class RuntimePostgresIntegrationTests(unittest.TestCase):
    """Exercise queue/lease recovery against the shared PostgreSQL fixture."""

    def setUp(self):
        reset_tables()

    @staticmethod
    def _insert_job(job_id, *, user_id="integration-user", job_type="briefing"):
        timestamp = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO runtime_jobs "
                "(id,user_id,type,status,payload_json,result_json,error,attempts,run_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    user_id,
                    job_type,
                    "queued",
                    json.dumps({"max_attempts": 1}),
                    "{}",
                    None,
                    0,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )

    def test_concurrent_claimers_never_claim_same_job(self):
        """Two PostgreSQL workers race for one row and produce one claim."""

        job_id = "job-concurrent-claim"
        self._insert_job(job_id)

        # The workers use independent pooled connections.  PostgreSQL's
        # FOR UPDATE SKIP LOCKED lets one take the row while the other sees no
        # claimable row; regardless of scheduling the same id can appear once.
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=5)
            return runtime._claim_due_jobs(1)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: claim(), range(2)))
        claimed_ids = [job["id"] for jobs in results for job in jobs]
        self.assertEqual(claimed_ids.count(job_id), 1)
        self.assertEqual(len(claimed_ids), len(set(claimed_ids)))
        with get_connection() as conn:
            row = conn.execute(
                "SELECT status, attempts FROM runtime_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["attempts"], 1)

    def test_expired_lease_is_reaped_after_provider_stop(self):
        """An expired lease is stopped locally after the provider call."""

        lease_id = "lease-expired-integration"
        user_hash = agent_runtime._hash_user("integration-user")
        expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        started = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO runtime_leases "
                "(id,user_id_hash,provider,provider_runtime_id,status,capabilities_json,endpoint,started_at,expires_at,last_seen_at,stopped_at,metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    lease_id,
                    user_hash,
                    "test-provider",
                    "provider-runtime-expired",
                    "running",
                    "[\"code\"]",
                    "https://runtime.example",
                    started,
                    expired,
                    expired,
                    None,
                    "{}",
                ),
            )

        settings = SimpleNamespace(api_mode="gateway", enabled=True)
        with patch.object(agent_runtime, "config", return_value=settings), patch.object(
            agent_runtime, "_stop_provider"
        ) as stop_provider:
            result = agent_runtime.reap_expired_leases()

        self.assertEqual(result, {"stopped": 1, "failed": 0, "expired": 1})
        stop_provider.assert_called_once_with(settings, "provider-runtime-expired")
        with get_connection() as conn:
            row = conn.execute(
                "SELECT status, endpoint, stopped_at FROM runtime_leases WHERE id = ?",
                (lease_id,),
            ).fetchone()
        self.assertEqual(row["status"], "stopped")
        self.assertIsNone(row["endpoint"])
        self.assertIsNotNone(row["stopped_at"])


if __name__ == "__main__":
    unittest.main()
