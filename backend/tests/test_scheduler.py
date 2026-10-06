"""Scheduler grammar and PostgreSQL queue integration coverage."""

from __future__ import annotations

import json
import os
import asyncio
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.pg import reset_tables

from app.db import get_connection
from app import main
from app.models import PushDeviceCreate, TaskUpdate
from app.scheduler import (
    _try_advisory_lock,
    next_run_at,
    parse_schedule,
    scheduler_tick,
)
from app.push import send_push
from app.routers.notifications import notification_stream
from app.services.notifications import create_notification
from app.routers.push import register_push_device
from app.routers.tasks import update_task


class ScheduleTests(unittest.TestCase):
    def test_lifespan_stops_worker_scheduler_and_telemetry(self):
        async def run_lifespan():
            worker_stops = []
            scheduler_stops = []
            telemetry_stops = []

            async def loop(stop_event, bucket):
                bucket.append(stop_event)
                await stop_event.wait()

            async def worker_loop(stop_event):
                await loop(stop_event, worker_stops)

            async def scheduler_loop_for_test(stop_event):
                await loop(stop_event, scheduler_stops)

            async def telemetry_loop():
                await loop(asyncio.Event(), telemetry_stops)

            with patch.object(main, "ensure_db"), patch.object(main.runtime_module, "start_runtime") as start, patch.object(
                main.runtime_module, "shutdown_runtime"
            ) as shutdown, patch.object(main, "runtime_worker_loop", new=worker_loop), patch.object(
                main, "scheduler_loop", new=scheduler_loop_for_test
            ), patch.object(main.telemetry, "telemetry_loop", new=telemetry_loop
            ):
                async with main.lifespan(main.app):
                    await asyncio.sleep(0.01)
                start.assert_called_once_with()
                shutdown.assert_called_once_with()
            self.assertEqual(len(worker_stops), 1)
            self.assertEqual(len(scheduler_stops), 1)
            self.assertEqual(len(telemetry_stops), 1)
            self.assertTrue(worker_stops[0].is_set())
            self.assertTrue(scheduler_stops[0].is_set())

        asyncio.run(run_lifespan())

    def test_supported_schedule_forms(self):
        self.assertEqual(parse_schedule("daily 09:30").kind, "daily")
        weekly = parse_schedule("weekly 7 23:59")
        self.assertEqual((weekly.kind, weekly.weekday, weekly.hour, weekly.minute), ("weekly", 7, 23, 59))
        self.assertEqual(parse_schedule("every 15m").interval_minutes, 15)
        self.assertEqual(parse_schedule("every 120m").interval_minutes, 120)

    def test_invalid_schedule_forms(self):
        for value in (
            "",
            "daily 9:30",
            "daily 24:00",
            "daily 12:60",
            "weekly 0 09:00",
            "weekly 8 09:00",
            "weekly 1 9:00",
            "every 14m",
            "every 15",
            "hourly 20m",
        ):
            with self.assertRaises(ValueError, msg=value):
                parse_schedule(value)

    def test_next_run_uses_timezone_and_rolls_across_days(self):
        # 2026-10-03 23:30 UTC is 2026-10-04 07:30 in Shanghai.
        base = datetime(2026, 10, 3, 23, 30, tzinfo=timezone.utc)
        daily = next_run_at("daily 07:00", base, "Asia/Shanghai")
        self.assertEqual(daily, datetime(2026, 10, 4, 23, 0, tzinfo=timezone.utc))

        # Monday is 1 in the public grammar; after Monday's occurrence the
        # next one is the following Monday.
        monday = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)
        weekly = next_run_at("weekly 1 09:30", monday, "Asia/Shanghai")
        self.assertEqual(weekly, datetime(2026, 10, 12, 1, 30, tzinfo=timezone.utc))

        interval = next_run_at("every 15m", base, "Asia/Shanghai")
        self.assertEqual(interval, base + timedelta(minutes=15))

    def test_next_run_accepts_iso_values_and_tz_alias(self):
        result = next_run_at("daily 09:00", "2026-10-03T00:00:00Z", tz="Asia/Shanghai")
        self.assertEqual(result, datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc))
        naive = next_run_at("daily 09:00", datetime(2026, 10, 3, 8, 0), "Asia/Shanghai")
        self.assertEqual(naive, datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc))

    def test_push_without_credentials_is_a_silent_noop(self):
        names = ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_BUNDLE_ID", "APNS_KEY_PATH", "FCM_PROJECT_ID", "FCM_SERVICE_ACCOUNT_PATH")
        saved = {name: os.environ.pop(name, None) for name in names}
        try:
            send_push("scheduler-user", "标题", "正文", {"notification_id": "n1"})
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

    def test_notification_stream_receives_created_notification(self):
        class _State:
            _state = {}

        class _Request:
            headers = {}
            cookies = {}
            state = _State()

            async def is_disconnected(self):
                return False

        class _Clock:
            def __init__(self):
                self.value = 0

            def monotonic(self):
                self.value += 10
                return self.value

        async def collect():
            response = await notification_stream(_Request())
            iterator = response.body_iterator
            ready = await iterator.__anext__()
            created = create_notification("local", "info", "流测试", "已收到")
            event = await iterator.__anext__()
            await iterator.aclose()
            return ready, event, created["id"]

        with patch("app.routers.notifications.time", _Clock()), patch(
            "app.routers.notifications._open_pubsub", return_value=None
        ), patch("app.deps.current_user_id", return_value="local"):
            ready, event, notification_id = asyncio.run(collect())
        self.assertIn("event: ready", ready)
        self.assertIn(notification_id, event)
        self.assertIn("已收到", event)


class SchedulerDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_tables()

    def test_advisory_lock_is_single_owner(self):
        # Keep both transactions open while trying the lock.  The xact lock is
        # released when the first context commits below.
        with get_connection() as first:
            with get_connection() as second:
                self.assertTrue(_try_advisory_lock(first))
                self.assertFalse(_try_advisory_lock(second))

    def test_due_task_is_reminded_once(self):
        fixed = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO tasks(id,user_id,title,description,status,due_at,created_at,updated_at,metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("task-scheduler", "scheduler-user", "写日报", "", "todo", fixed.isoformat(), fixed.isoformat(), fixed.isoformat(), "{}"),
            )
        with patch("app.scheduler._now", return_value=fixed):
            first = scheduler_tick()
            second = scheduler_tick()
        self.assertEqual(first["task_reminders"], 1)
        self.assertEqual(second["task_reminders"], 0)
        with get_connection() as conn:
            task = conn.execute("SELECT reminded_at FROM tasks WHERE id = ?", ("task-scheduler",)).fetchone()
            rows = conn.execute("SELECT title,body FROM notifications WHERE user_id = ?", ("scheduler-user",)).fetchall()
        self.assertIsNotNone(task["reminded_at"])
        self.assertEqual(len(rows), 1)
        self.assertIn("写日报", rows[0]["body"])

    def test_due_task_with_offset_is_compared_as_real_time(self):
        fixed = datetime(2026, 10, 4, 1, 1, tzinfo=timezone.utc)
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO tasks(id,user_id,title,description,status,due_at,created_at,updated_at,metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "task-offset",
                    "scheduler-offset",
                    "偏移任务",
                    "",
                    "todo",
                    "2026-10-04T09:00:00+08:00",
                    fixed.isoformat(),
                    fixed.isoformat(),
                    "{}",
                ),
            )
        with patch("app.scheduler._now", return_value=fixed):
            result = scheduler_tick()
        self.assertEqual(result["task_reminders"], 1)

    def test_naive_due_task_uses_shanghai_timezone(self):
        fixed = datetime(2026, 10, 4, 1, 1, tzinfo=timezone.utc)
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO tasks(id,user_id,title,description,status,due_at,created_at,updated_at,metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "task-naive",
                    "scheduler-naive",
                    "无时区任务",
                    "",
                    "todo",
                    "2026-10-04T09:00:00",
                    fixed.isoformat(),
                    fixed.isoformat(),
                    "{}",
                ),
            )
        with patch("app.scheduler._now", return_value=fixed):
            result = scheduler_tick()
        self.assertEqual(result["task_reminders"], 1)

    def test_push_token_moves_between_users_and_response_is_redacted(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM push_devices")
        payload = PushDeviceCreate(platform="fcm", token="token-for-one-device")
        with patch("app.routers.push.owner_id", side_effect=["push-user-a", "push-user-b"]):
            first = register_push_device(SimpleNamespace(), payload)
            second = register_push_device(SimpleNamespace(), payload)
        self.assertEqual(first.token_suffix, "device")
        self.assertEqual(second.token_suffix, "device")
        self.assertNotIn("token", second.model_dump())
        with get_connection() as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM push_devices WHERE user_id = ? AND platform = ? AND token = ?",
                    ("push-user-a", "fcm", payload.token),
                ).fetchone()
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM push_devices WHERE user_id = ? AND platform = ? AND token = ?",
                    ("push-user-b", "fcm", payload.token),
                ).fetchone()
            )

    def test_rescheduling_task_clears_reminded_at(self):
        fixed = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO tasks(id,user_id,title,description,status,due_at,reminded_at,created_at,updated_at,metadata_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "task-reschedule",
                    "scheduler-reschedule",
                    "改期任务",
                    "",
                    "todo",
                    fixed.isoformat(),
                    fixed.isoformat(),
                    fixed.isoformat(),
                    fixed.isoformat(),
                    "{}",
                ),
            )
        request = SimpleNamespace()
        payload = TaskUpdate(due_at=datetime(2026, 10, 5, 9, 0))
        with patch("app.routers.tasks.owner_id", return_value="scheduler-reschedule"):
            update_task(request, "task-reschedule", payload)
        with get_connection() as conn:
            row = conn.execute("SELECT due_at, reminded_at FROM tasks WHERE id = ?", ("task-reschedule",)).fetchone()
        self.assertEqual(row["due_at"], "2026-10-05T01:00:00+00:00")
        self.assertIsNone(row["reminded_at"])

    def test_due_routine_queues_once_and_advances(self):
        fixed = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
        old_next = (fixed - timedelta(minutes=1)).isoformat()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO routines(id,user_id,title,prompt,schedule,timezone,enabled,next_run_at,last_run_at,last_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("routine-scheduler", "scheduler-user", "晨报", "汇总今日待办", "every 15m", "Asia/Shanghai", True, old_next, None, None, fixed.isoformat(), fixed.isoformat()),
            )
        with patch("app.scheduler._now", return_value=fixed):
            first = scheduler_tick()
            second = scheduler_tick()
        self.assertEqual(first["routine_jobs"], 1)
        self.assertEqual(second["routine_jobs"], 0)
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT type,payload_json FROM runtime_jobs WHERE user_id = ? AND type = ?",
                ("scheduler-user", "agent_run"),
            ).fetchall()
            routine = conn.execute("SELECT next_run_at FROM routines WHERE id = ?", ("routine-scheduler",)).fetchone()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["routine_id"], "routine-scheduler")
        self.assertEqual(payload["prompt"], "汇总今日待办")
        self.assertEqual(payload["allowed_tools"], ["briefing", "list_tasks", "list_memories", "files"])
        self.assertGreater(datetime.fromisoformat(routine["next_run_at"]), fixed)


if __name__ == "__main__":
    unittest.main()
