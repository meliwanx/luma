"""Nightly and weekly deletion of unpinned memories.

These cases use in-memory fakes so they do not need PostgreSQL.  The delete
predicate and the Asia/Shanghai schedule are what they lock in.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.scheduler import (
    memory_consolidation_due,
    reset_memory_consolidation_schedule,
    scheduler_tick,
)
from app.services.memory import forget_unpinned_memories, memory_consolidation_cutoff

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _at(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=SHANGHAI)


class _Result:
    def __init__(self, rows, rowcount=0):
        self.rows = rows
        self.rowcount = rowcount

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class MemoryStore:
    """Just enough of the DB facade to apply the consolidation statements."""

    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "FROM memories" in sql and sql.strip().upper().startswith("SELECT"):
            return _Result(list(self.rows))
        if sql.strip().upper().startswith("DELETE"):
            memory_id = params[0]
            kept = []
            removed = 0
            for row in self.rows:
                pinned = row.get("pinned", False)
                pinned_true = pinned is True or (
                    isinstance(pinned, str) and pinned.strip().lower() in {"1", "true", "yes", "on"}
                )
                if row.get("id") == memory_id and not pinned_true:
                    removed += 1
                    continue
                kept.append(row)
            self.rows = kept
            return _Result([], removed)
        return _Result([])


class ForgetUnpinnedTests(unittest.TestCase):
    def test_cutoff_is_shanghai_midnight_of_the_run_date(self):
        now = _at(2026, 10, 8, 3, 10)
        cutoff = memory_consolidation_cutoff(now)
        self.assertEqual(cutoff, _at(2026, 10, 8, 0, 0))
        # Monday's sweep uses Monday's midnight, not a trailing week.
        monday = _at(2026, 10, 5, 3, 20)
        self.assertEqual(monday.weekday(), 0)
        self.assertEqual(memory_consolidation_cutoff(monday), _at(2026, 10, 5, 0, 0))

    def test_only_unpinned_memories_from_before_today_are_deleted(self):
        now = _at(2026, 10, 8, 3, 10)
        pinned = {
            "id": "mem_pin",
            "content": "keep me",
            "importance": 1,
            "pinned": True,
            "created_at": "2026-10-01T00:00:00+00:00",
            "updated_at": "2026-10-01T00:00:00+00:00",
        }
        store = MemoryStore([
            pinned,
            {"id": "mem_old", "content": "yesterday", "importance": 5, "pinned": False,
             "created_at": "2026-10-07T14:00:00+00:00"},  # 22:00 Shanghai on the 7th
            {"id": "mem_late", "content": "small hours", "importance": 1, "pinned": False,
             "created_at": "2026-10-07T18:15:00+00:00"},  # 02:15 Shanghai on the 8th
            {"id": "mem_midnight", "content": "exactly midnight", "importance": 3, "pinned": False,
             "created_at": "2026-10-07T16:00:00+00:00"},  # 00:00 Shanghai on the 8th
            {"id": "mem_local", "content": "written with offset", "importance": 3, "pinned": False,
             "created_at": "2026-10-07T23:59:00+08:00"},
            {"id": "mem_bad", "content": "unreadable", "importance": 3, "pinned": False,
             "created_at": "not-a-timestamp"},
            {"id": "mem_text_pin", "content": "string pin", "importance": 2, "pinned": "true",
             "created_at": "2020-01-01T00:00:00+00:00"},
        ])
        deleted = forget_unpinned_memories(store, now=now)
        self.assertEqual(deleted, 2)
        kept_ids = {row["id"] for row in store.rows}
        self.assertEqual(kept_ids, {"mem_pin", "mem_late", "mem_midnight", "mem_bad", "mem_text_pin"})
        surviving_pin = next(row for row in store.rows if row["id"] == "mem_pin")
        self.assertEqual(surviving_pin, pinned)
        selects = [sql for sql, _ in store.statements if "SELECT" in sql]
        deletes = [sql for sql, _ in store.statements if sql.strip().upper().startswith("DELETE")]
        self.assertTrue(selects and all("pinned IS NOT TRUE" in sql for sql in selects))
        self.assertTrue(deletes and all("pinned IS NOT TRUE" in sql for sql in deletes))
        self.assertFalse(any(sql.strip().upper().startswith("UPDATE") for sql, _ in store.statements))

        again = forget_unpinned_memories(store, now=now)
        self.assertEqual(again, 0)
        self.assertEqual({row["id"] for row in store.rows}, kept_ids)

    def test_weekly_sweep_uses_the_same_predicate(self):
        now = _at(2026, 10, 5, 3, 20)  # Monday
        store = MemoryStore([
            {"id": "mem_sunday", "pinned": False, "created_at": "2026-10-04T15:00:00+00:00"},
            {"id": "mem_monday", "pinned": False, "created_at": "2026-10-04T17:30:00+00:00"},
        ])
        # Sunday 23:00 Shanghai is before Monday midnight; Monday 01:30 is not.
        self.assertEqual(forget_unpinned_memories(store, now=now), 1)
        self.assertEqual([row["id"] for row in store.rows], ["mem_monday"])


class _TickConn:
    def __init__(self, fail_forget=False):
        self.fail_forget = fail_forget
        self.statements = []
        self.forget_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.statements.append(sql)
        if "pg_try_advisory" in sql:
            return _Result([{"locked": True}])
        if "FROM tasks" in sql or "FROM routines" in sql or "FROM memories" in sql:
            if self.fail_forget and "FROM memories" in sql:
                raise RuntimeError("consolidation unavailable")
            return _Result([])
        return _Result([])


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        reset_memory_consolidation_schedule()

    def tearDown(self):
        reset_memory_consolidation_schedule()

    def test_slots_follow_shanghai_clock(self):
        self.assertEqual(memory_consolidation_due(_at(2026, 10, 8, 3, 9)), [])
        self.assertEqual(memory_consolidation_due(_at(2026, 10, 8, 2, 30)), [])
        self.assertEqual(memory_consolidation_due(_at(2026, 10, 8, 3, 10)), ["nightly"])
        # Later the same day still counts if this process has not swept yet.
        self.assertEqual(memory_consolidation_due(_at(2026, 10, 8, 15, 16)), ["nightly"])
        # Thursday is not the weekly sweep.
        self.assertEqual(_at(2026, 10, 8, 3, 20).weekday(), 3)
        self.assertEqual(memory_consolidation_due(_at(2026, 10, 8, 3, 20)), ["nightly"])
        monday_early = _at(2026, 10, 5, 3, 19)
        self.assertEqual(monday_early.weekday(), 0)
        self.assertEqual(memory_consolidation_due(monday_early), ["nightly"])
        self.assertEqual(memory_consolidation_due(_at(2026, 10, 5, 3, 20)), ["nightly", "weekly"])

    def test_a_running_process_sweeps_once_per_slot(self):
        from app import scheduler as scheduler_module

        scheduler_module._mark_memory_consolidation(_at(2026, 10, 8, 3, 10), ["nightly"])
        self.assertEqual(memory_consolidation_due(_at(2026, 10, 8, 15, 0)), [])
        self.assertEqual(memory_consolidation_due(_at(2026, 10, 9, 3, 10)), ["nightly"])
        scheduler_module._mark_memory_consolidation(_at(2026, 10, 5, 3, 10), ["nightly"])
        self.assertEqual(memory_consolidation_due(_at(2026, 10, 5, 3, 20)), ["weekly"])

    def test_scheduler_tick_runs_each_due_slot_and_retries_after_failure(self):
        from unittest.mock import patch

        conn = _TickConn()
        calls = []

        def forget(connection, now=None):
            calls.append(now)
            return 1

        def no_users(connection, now):
            return []

        def no_feed(connection, now):
            return 0

        thursday = _at(2026, 10, 8, 3, 10)
        with patch("app.scheduler.get_connection", return_value=conn), patch(
            "app.scheduler._now", return_value=thursday
        ), patch("app.services.proactive.proactive_tick", new=no_users), patch(
            "app.services.feed.feed_tick", new=no_feed
        ), patch("app.services.memory.forget_unpinned_memories", new=forget):
            first = scheduler_tick()
            second = scheduler_tick()
        self.assertEqual(first["memory_forgotten"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], thursday)
        self.assertEqual(second["memory_forgotten"], 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("SAVEPOINT memory_consolidation" in sql for sql in conn.statements))

        reset_memory_consolidation_schedule()
        failing = _TickConn(fail_forget=True)
        recovered = _TickConn()
        monday = _at(2026, 10, 5, 3, 20)
        sequence = {"conn": failing}

        def connection():
            return sequence["conn"]

        with patch("app.scheduler.get_connection", side_effect=connection), patch(
            "app.scheduler._now", return_value=monday
        ), patch("app.services.proactive.proactive_tick", new=no_users), patch(
            "app.services.feed.feed_tick", new=no_feed
        ):
            with self.assertLogs("app.scheduler", level="WARNING") as captured:
                failed = scheduler_tick()
            self.assertTrue(any("memory consolidation" in line for line in captured.output))
            self.assertEqual(failed["memory_forgotten"], 0)
            self.assertEqual(memory_consolidation_due(monday), ["nightly", "weekly"])
            sequence["conn"] = recovered
            calls.clear()
            with patch("app.services.memory.forget_unpinned_memories", new=forget):
                retried = scheduler_tick()
        self.assertEqual(retried["memory_forgotten"], 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(memory_consolidation_due(monday), [])


if __name__ == "__main__":
    unittest.main()
