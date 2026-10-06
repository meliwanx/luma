"""Background writes must finish before stream and database teardown."""

import asyncio
import os
import threading
import unittest
from unittest.mock import AsyncMock, patch

from tests import pg
from app import main, telemetry
from app.agent import tools
from app.db import get_connection
from app.services import generation, memory


class BackgroundLifecycleTests(unittest.TestCase):
    def tearDown(self):
        memory.shutdown_memory_workers()

    def test_memory_shutdown_joins_running_callback(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        shutdown_started = threading.Event()
        shutdown_finished = threading.Event()

        def callback():
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("callback release missing")
            finished.set()

        memory.submit_memory_background(callback)
        self.assertTrue(started.wait(timeout=5))
        executor = memory._MEMORY_EXECUTOR
        real_shutdown = executor.shutdown

        def observed_shutdown(**kwargs):
            shutdown_started.set()
            return real_shutdown(**kwargs)

        def shutdown():
            memory.shutdown_memory_workers()
            shutdown_finished.set()

        with patch.object(executor, "shutdown", side_effect=observed_shutdown):
            thread = threading.Thread(target=shutdown)
            thread.start()
            try:
                self.assertTrue(shutdown_started.wait(timeout=5))
                self.assertFalse(shutdown_finished.is_set())
                self.assertIsNone(memory.submit_memory_background(lambda: None))
            finally:
                release.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())
        self.assertTrue(shutdown_finished.is_set())

    def test_reset_drains_writer_before_taking_truncate_locks(self):
        pg.reset_tables()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                ("lifecycle-session", "local", "before", "2026-10-04T00:00:00+00:00", "2026-10-04T00:00:00+00:00"),
            )
        writer_started = threading.Event()
        release_writer = threading.Event()
        drain_started = threading.Event()
        reset_finished = threading.Event()
        errors = []
        real_drain = memory.drain_memory_workers

        def write():
            with get_connection() as conn:
                conn.execute("UPDATE sessions SET title = ? WHERE id = ?", ("during", "lifecycle-session"))
                writer_started.set()
                if not release_writer.wait(timeout=5):
                    raise RuntimeError("writer release missing")
                # This second table write would conflict with an in-progress
                # TRUNCATE that already acquired some of its table locks.
                conn.execute(
                    "INSERT INTO messages(id,user_id,session_id,role,content,created_at) VALUES (?,?,?,?,?,?)",
                    ("lifecycle-message", "local", "lifecycle-session", "assistant", "finished", "2026-10-04T00:00:01+00:00"),
                )

        def observed_drain():
            drain_started.set()
            real_drain()

        def reset():
            try:
                pg.reset_tables()
            except BaseException as exc:
                errors.append(exc)
            finally:
                reset_finished.set()

        writer = memory.submit_memory_background(write)
        self.assertTrue(writer_started.wait(timeout=5))
        with patch.object(memory, "drain_memory_workers", side_effect=observed_drain), patch.object(
            pg, "get_connection", wraps=get_connection
        ) as reset_connection:
            thread = threading.Thread(target=reset)
            thread.start()
            try:
                self.assertTrue(drain_started.wait(timeout=5))
                self.assertFalse(reset_finished.is_set())
                reset_connection.assert_not_called()
            finally:
                release_writer.set()
                thread.join(timeout=5)
        writer.result(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS count FROM messages").fetchone()["count"], 0)

    def test_terminal_stream_waits_for_runner_cleanup(self):
        async def run():
            cleanup_started = asyncio.Event()
            release = asyncio.Event()
            cleanup_finished = asyncio.Event()
            manager = generation.GenerationManager()

            async def no_redis(*args, **kwargs):
                raise RuntimeError("disabled for test")

            async def runner():
                try:
                    yield "done", {"id": "lifecycle-generation", "status": "complete"}
                finally:
                    cleanup_started.set()
                    await release.wait()
                    cleanup_finished.set()

            async def consume():
                return [item async for item in manager.subscribe("lifecycle-generation")]

            manager._redis_quick = no_redis
            task = await manager.start("lifecycle-generation", runner, timeout_seconds=0)
            subscriber = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(cleanup_started.wait(), timeout=5)
                self.assertFalse(subscriber.done())
            finally:
                release.set()
            events = await asyncio.wait_for(subscriber, timeout=5)
            self.assertEqual(events[-1][1], "done")
            self.assertTrue(task.done())
            self.assertTrue(cleanup_finished.is_set())
            self.assertNotIn("lifecycle-generation", manager.tasks)
            for handle in manager._cleanup_handles.values():
                handle.cancel()

        asyncio.run(run())

    def test_cancelled_add_mcp_waits_for_save_thread(self):
        async def run():
            loop = asyncio.get_running_loop()
            save_started = asyncio.Event()
            cancellation_dispatched = asyncio.Event()
            release = threading.Event()
            save_finished = threading.Event()
            endpoint = "https://example.com/mcp"

            def save(*args, **kwargs):
                loop.call_soon_threadsafe(save_started.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("connector save release missing")
                save_finished.set()
                return [], []

            with patch.object(tools.mcp, "validate_url", side_effect=lambda value: value), patch.object(
                tools, "_user_provided_urls", return_value={tools._normalized_connector_url(endpoint)}
            ), patch.object(tools, "save_mcp_connectors", side_effect=save):
                context = tools.AgentContext(user_id="local", session_id="lifecycle-session")
                task = asyncio.create_task(tools._connector_add_mcp(context, {"url": endpoint}))
                await asyncio.wait_for(save_started.wait(), timeout=5)
                task.cancel()
                # The cancelled coroutine is scheduled before this callback,
                # so its cleanup handler has run when this event arrives.
                loop.call_soon(cancellation_dispatched.set)
                try:
                    await asyncio.wait_for(cancellation_dispatched.wait(), timeout=5)
                    self.assertFalse(task.done())
                    self.assertFalse(save_finished.is_set())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(save_finished.is_set())

        asyncio.run(run())

    def test_cancelled_telemetry_drains_sql_before_final_flush(self):
        async def run():
            loop = asyncio.get_running_loop()
            flush_started = asyncio.Event()
            cancellation_dispatched = asyncio.Event()
            release = threading.Event()
            first_finished = threading.Event()
            calls = []

            def flush():
                calls.append("flush")
                if len(calls) == 1:
                    loop.call_soon_threadsafe(flush_started.set)
                    if not release.wait(timeout=5):
                        raise RuntimeError("telemetry release missing")
                    first_finished.set()
                else:
                    self.assertTrue(first_finished.is_set())

            with patch.object(telemetry, "flush", side_effect=flush), patch.object(telemetry, "prune"):
                task = asyncio.create_task(telemetry.telemetry_loop())
                await asyncio.wait_for(flush_started.wait(), timeout=5)
                task.cancel()
                loop.call_soon(cancellation_dispatched.set)
                try:
                    await asyncio.wait_for(cancellation_dispatched.wait(), timeout=5)
                    self.assertFalse(task.done())
                    self.assertFalse(first_finished.is_set())
                    self.assertEqual(calls, ["flush"])
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(first_finished.is_set())
                self.assertEqual(calls, ["flush", "flush"])

        asyncio.run(run())

    def test_lifespan_waits_for_worker_thread_to_finish(self):
        async def run():
            loop = asyncio.get_running_loop()
            worker_started = asyncio.Event()
            scheduler_stopped = asyncio.Event()
            release = threading.Event()
            worker_finished = threading.Event()

            def blocking_write():
                loop.call_soon_threadsafe(worker_started.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("worker release missing")
                worker_finished.set()

            async def worker(stop):
                await asyncio.to_thread(blocking_write)

            async def scheduler(stop):
                await stop.wait()
                scheduler_stopped.set()

            async def telemetry():
                await asyncio.Event().wait()

            with patch.object(main, "get_storage"), patch.object(main, "ensure_db"), patch.object(
                main, "ensure_default_data"
            ), patch.object(main.generation, "startup_recover", new_callable=AsyncMock), patch.object(
                main.generation, "shutdown", new_callable=AsyncMock
            ), patch.object(main.runtime_module, "start_runtime"), patch.object(
                main.runtime_module, "shutdown_runtime"
            ), patch.object(main, "shutdown_memory_workers"), patch.object(
                main.provider, "close_async_client", new_callable=AsyncMock
            ), patch.object(main, "runtime_worker_loop", new=worker), patch.object(
                main, "scheduler_loop", new=scheduler
            ), patch.object(main.telemetry, "telemetry_loop", new=telemetry), patch.object(
                main.telemetry, "flush"
            ), patch.dict(
                os.environ, {"AUTH_REQUIRED": "false"}, clear=False
            ):
                context = main.lifespan(main.app)
                await context.__aenter__()
                await asyncio.wait_for(worker_started.wait(), timeout=5)
                closing = asyncio.create_task(context.__aexit__(None, None, None))
                try:
                    await asyncio.wait_for(scheduler_stopped.wait(), timeout=5)
                    self.assertFalse(closing.done())
                    self.assertFalse(worker_finished.is_set())
                finally:
                    release.set()
                await asyncio.wait_for(closing, timeout=5)
                self.assertTrue(worker_finished.is_set())

        asyncio.run(run())

    def test_lifespan_flushes_late_memory_usage_before_provider_teardown(self):
        async def run():
            loop = asyncio.get_running_loop()
            callback_started = asyncio.Event()
            release = threading.Event()
            pending = []
            persisted = []
            order = []
            real_shutdown = memory.shutdown_memory_workers

            def callback():
                loop.call_soon_threadsafe(callback_started.set)
                if not release.wait(timeout=5):
                    raise RuntimeError("memory callback release missing")
                pending.append("late-model-call")
                order.append("call_completed")

            def memory_shutdown():
                order.append("memory_join_started")
                release.set()
                real_shutdown()
                order.append("memory_join_finished")

            def flush():
                persisted.extend(pending)
                pending.clear()
                order.append("flush")

            async def telemetry_loop():
                try:
                    await asyncio.Event().wait()
                finally:
                    # The periodic loop's cancellation flush happens before
                    # the running memory callback is allowed to complete.
                    flush()

            async def stopped_worker(stop):
                await stop.wait()

            async def close_provider():
                order.append("provider_closed")
                self.assertEqual(persisted, ["late-model-call"])
                self.assertEqual(pending, [])

            async def close_browser():
                order.append("browser_closed")

            with patch.object(main, "get_storage"), patch.object(main, "ensure_db"), patch.object(
                main, "ensure_default_data"
            ), patch.object(main.generation, "startup_recover", new_callable=AsyncMock), patch.object(
                main.generation, "shutdown", new_callable=AsyncMock
            ), patch.object(main.runtime_module, "start_runtime"), patch.object(
                main.runtime_module, "shutdown_runtime", side_effect=lambda: order.append("runtime_join_finished")
            ), patch.object(main, "shutdown_memory_workers", side_effect=memory_shutdown), patch.object(
                main.provider, "close_async_client", side_effect=close_provider
            ), patch.object(main, "runtime_worker_loop", new=stopped_worker), patch.object(
                main, "scheduler_loop", new=stopped_worker
            ), patch.object(main.telemetry, "telemetry_loop", new=telemetry_loop), patch.object(
                main.telemetry, "flush", side_effect=flush
            ), patch("app.services.browser.close_connections", side_effect=close_browser), patch.dict(
                os.environ, {"AUTH_REQUIRED": "false"}, clear=False
            ):
                context = main.lifespan(main.app)
                await context.__aenter__()
                memory.submit_memory_background(callback)
                try:
                    await asyncio.wait_for(callback_started.wait(), timeout=5)
                    await asyncio.wait_for(context.__aexit__(None, None, None), timeout=5)
                finally:
                    release.set()
                    await asyncio.to_thread(real_shutdown)
                final_flush = len(order) - 1 - order[::-1].index("flush")
                self.assertLess(order.index("runtime_join_finished"), order.index("memory_join_finished"))
                self.assertLess(order.index("call_completed"), order.index("memory_join_finished"))
                self.assertLess(order.index("memory_join_finished"), final_flush)
                self.assertLess(final_flush, order.index("provider_closed"))

        asyncio.run(run())
