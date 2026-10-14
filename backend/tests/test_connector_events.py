"""Isolated durable seam tests; no business database, provider or model calls."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from app.services.connector_events import (
    ConnectorBinding, ConnectorEventConflict, ConnectorEventError,
    ConnectorEventForbidden, ConnectorEventService, OwnerAcknowledgement,
)


class SQLiteTransaction:
    """Test implementation of the persistence seam, sharing notifications' tx."""

    def __init__(self, connection, binding, fail_receipt=False):
        self.connection = connection
        self.scope = json.dumps(binding.scope(), sort_keys=True)
        self.fail_receipt = fail_receipt

    def get_receipt(self, event_id):
        row = self.connection.execute("SELECT data FROM event_receipts WHERE event_id=?", (event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_receipt(self, event_id, receipt):
        if self.fail_receipt:
            raise RuntimeError("simulated receipt persistence failure")
        self.connection.execute("INSERT INTO event_receipts(event_id,data) VALUES (?,?)",
                                (event_id, json.dumps(receipt, sort_keys=True)))

    def get_task(self, task_id):
        row = self.connection.execute("SELECT data FROM connector_tasks WHERE scope=? AND task_id=?",
                                      (self.scope, task_id)).fetchone()
        return json.loads(row[0]) if row else None

    def put_task(self, task_id, task):
        self.connection.execute(
            "INSERT INTO connector_tasks(scope,task_id,data) VALUES (?,?,?) "
            "ON CONFLICT(scope,task_id) DO UPDATE SET data=excluded.data",
            (self.scope, task_id, json.dumps(task, sort_keys=True)),
        )


class SQLiteStore:
    """Only a test adapter; public application startup never initializes it."""

    def __init__(self, path):
        self.path, self.fail_receipt = str(path), False
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS event_receipts(event_id TEXT PRIMARY KEY,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS connector_tasks(scope TEXT,task_id TEXT,data TEXT NOT NULL,
                                                          PRIMARY KEY(scope,task_id));
                CREATE TABLE IF NOT EXISTS notifications(
                    id TEXT PRIMARY KEY,user_id TEXT,title TEXT,body TEXT,kind TEXT,level TEXT,
                    action_url TEXT,read_at TEXT,created_at TEXT,metadata_json TEXT);
            """)

    @contextmanager
    def transaction(self, binding, task_id):
        # BEGIN IMMEDIATE serializes independent store instances/processes as
        # well as callers; ledger and inbox rows commit together.
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield SQLiteTransaction(conn, binding, self.fail_receipt)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class ConnectorEventTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "events.sqlite"
        self.store = SQLiteStore(self.path)
        self.published = []
        self.service = ConnectorEventService(self.store, publisher=lambda owner, row: self.published.append((owner, row)))
        self.binding = ConnectorBinding("connector-runtime", "sample-connector", "connection-1", "owner-1", "tenant-1")
        self.task_id = str(uuid.uuid4())

    def event(self, *, kind="task.state", binding=None, **updates):
        binding = binding or self.binding
        data = {**binding.scope(), "connection_version": binding.connection_version, "schema_version": 1,
                "event_id": str(uuid.uuid4()), "task_id": self.task_id, "kind": kind,
                "occurred_at": "2026-10-08T03:00:00+08:00"}
        if kind == "task.state":
            data.update(version=1, status="queued")
        data.update(updates)
        return data

    def begin(self):
        self.service.ingest(self.binding, self.event())
        self.service.ingest(self.binding, self.event(version=2, status="running"))

    def succeed(self):
        self.begin()
        event = self.event(version=3, status="succeeded", result_ref="result-42")
        return event, self.service.ingest(self.binding, event)

    def rows(self, table="notifications"):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM " + table).fetchall()]

    def test_real_notification_insert_and_postcommit_publish_have_safe_content(self):
        event, receipt = self.succeed()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], self.binding.owner_id)
        self.assertIsNone(rows[0]["read_at"])
        self.assertIsNone(rows[0]["action_url"])
        self.assertNotIn("result-42", json.dumps(rows))
        self.assertEqual(json.loads(rows[0]["metadata_json"])["event_id"], event["event_id"])
        self.assertEqual(self.published[0][0], self.binding.owner_id)
        self.assertEqual(receipt["task_status"], "succeeded")
        self.assertNotIn("delivered", receipt)

    def test_same_uuid_is_replayed_without_another_notification_or_publish(self):
        event, receipt = self.succeed()
        replay = self.service.ingest(self.binding, event)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(receipt["notification_id"], replay["notification_id"])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.published), 1)

    def test_timezone_normalization_preserves_replay(self):
        event = self.event()
        self.service.ingest(self.binding, event)
        event["occurred_at"] = "2026-10-07T19:00:00Z"
        self.assertTrue(self.service.ingest(self.binding, event)["duplicate"])

    def test_changed_content_for_existing_uuid_conflicts(self):
        event = self.event()
        self.service.ingest(self.binding, event)
        with self.assertRaises(ConnectorEventConflict):
            self.service.ingest(self.binding, {**event, "version": 2, "status": "running"})
        self.assertEqual(self.service.task(self.binding, self.task_id)["status"], "queued")

    def test_all_binding_boundaries_are_checked_before_writing(self):
        for field in ("source", "connector_id", "connection_id", "owner_id", "tenant_id"):
            with self.subTest(field=field), self.assertRaises(ConnectorEventForbidden):
                self.service.ingest(self.binding, self.event(**{field: "other"}))
        with self.assertRaises(ConnectorEventConflict):
            self.service.ingest(self.binding, self.event(connection_version=2))
        self.assertEqual(self.rows("event_receipts"), [])

    def test_global_uuid_cannot_move_to_another_binding(self):
        event = self.event()
        self.service.ingest(self.binding, event)
        other = ConnectorBinding("connector-runtime", "sample-connector", "connection-1", "other-owner", "tenant-1")
        with self.assertRaises(ConnectorEventForbidden):
            self.service.ingest(other, {**event, **other.scope()})
        self.assertIsNone(self.service.task(other, self.task_id))

    def test_cross_tenant_task_reads_are_isolated(self):
        self.begin()
        other = ConnectorBinding("connector-runtime", "sample-connector", "connection-1", "owner-1", "other-tenant")
        self.assertIsNone(self.service.task(other, self.task_id))

    def test_versions_and_state_transitions_are_strict(self):
        self.begin()
        for updates in ({"version": 2, "status": "succeeded", "result_ref": "result-42"},
                        {"version": 4, "status": "succeeded", "result_ref": "result-42"},
                        {"version": 3, "status": "queued"}):
            with self.subTest(updates=updates), self.assertRaises(ConnectorEventConflict):
                self.service.ingest(self.binding, self.event(**updates))
        self.assertEqual(self.service.task(self.binding, self.task_id)["version"], 2)

    def test_terminal_task_cannot_restart_with_a_new_event(self):
        self.succeed()
        with self.assertRaises(ConnectorEventConflict):
            self.service.ingest(self.binding, self.event(version=4, status="running"))

    def test_unknown_can_only_reconcile_a_result_not_repeat_the_action(self):
        self.begin()
        self.service.ingest(self.binding, self.event(version=3, status="unknown"))
        with self.assertRaises(ConnectorEventConflict):
            self.service.ingest(self.binding, self.event(version=4, status="running"))
        result = self.service.ingest(self.binding, self.event(version=4, status="succeeded", result_ref="result-42"))
        self.assertEqual(result["task_status"], "succeeded")

    def test_sensitive_or_unbounded_payload_fields_are_rejected_without_echo(self):
        for updates in ({"reason": "private medical detail"}, {"tool_args": {"reason": "private medical detail"}},
                        {"summary": "private medical detail"}, {"result_ref": "Bearer=private-value"},
                        {"occurred_at": "2026-10-08T03:00:00"}, {"version": True}, {"schema_version": True}):
            with self.subTest(keys=list(updates)), self.assertRaises(ConnectorEventError) as error:
                self.service.ingest(self.binding, self.event(**updates))
            self.assertNotIn("private", str(error.exception))
        self.assertEqual(self.rows("event_receipts"), [])

    def test_success_requires_source_result_reference(self):
        self.begin()
        with self.assertRaises(ConnectorEventError):
            self.service.ingest(self.binding, self.event(version=3, status="succeeded"))

    def test_business_resolution_does_not_resolve_the_tool_or_mark_notifications_read(self):
        self.begin()
        self.service.ingest(self.binding, self.event(kind="business.state", business_version=1,
                                                   business_status="pending", business_ref="case-42"))
        self.service.ingest(self.binding, self.event(kind="business.state", business_version=2,
                                                   business_status="resolved", business_ref="case-42"))
        task = self.service.task(self.binding, self.task_id)
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["business_status"], "resolved")
        self.assertEqual(task["version"], 2)
        self.assertEqual(self.rows(), [])

    def test_business_entity_and_version_cannot_change_silently(self):
        self.begin()
        self.service.ingest(self.binding, self.event(kind="business.state", business_version=1,
                                                   business_status="pending", business_ref="case-42"))
        for updates in ({"business_version": 2, "business_status": "resolved", "business_ref": "other-case"},
                        {"business_version": 3, "business_status": "resolved", "business_ref": "case-42"}):
            with self.subTest(updates=updates), self.assertRaises(ConnectorEventConflict):
                self.service.ingest(self.binding, self.event(kind="business.state", **updates))

    def test_read_requires_owner_authentication_and_is_separate_from_delivery(self):
        _, receipt = self.succeed()
        event = self.event(kind="delivery.ack", notification_id=receipt["notification_id"], ack="read")
        with self.assertRaises(ConnectorEventForbidden):
            self.service.ingest(self.binding, event)
        with self.assertRaises(ConnectorEventForbidden):
            self.service.ingest(self.binding, event, read_actor=OwnerAcknowledgement("owner-1", "other-tenant"))
        self.service.ingest(self.binding, event, read_actor=OwnerAcknowledgement("owner-1", "tenant-1"))
        task = self.service.task(self.binding, self.task_id)
        ack = task["notifications"][receipt["notification_id"]]
        self.assertIsNone(ack["delivered_at"])
        self.assertIsNotNone(ack["read_at"])
        self.assertIsNotNone(self.rows()[0]["read_at"])
        self.assertEqual(task["status"], "succeeded")
        self.assertNotIn("business_status", task)

    def test_delivery_receipt_does_not_mark_read_and_unknown_notifications_fail(self):
        _, receipt = self.succeed()
        self.service.ingest(self.binding, self.event(kind="delivery.ack", notification_id=receipt["notification_id"], ack="delivered"))
        ack = self.service.task(self.binding, self.task_id)["notifications"][receipt["notification_id"]]
        self.assertIsNotNone(ack["delivered_at"])
        self.assertIsNone(ack["read_at"])
        self.assertIsNone(self.rows()[0]["read_at"])
        with self.assertRaises(ConnectorEventForbidden):
            self.service.ingest(self.binding, self.event(kind="delivery.ack", notification_id="notification_other", ack="delivered"))

    def test_notification_task_and_receipt_rollback_together(self):
        self.begin()
        self.store.fail_receipt = True
        with self.assertRaises(RuntimeError):
            self.service.ingest(self.binding, self.event(version=3, status="succeeded", result_ref="result-42"))
        self.store.fail_receipt = False
        self.assertEqual(len(self.rows("event_receipts")), 2)
        self.assertEqual(self.service.task(self.binding, self.task_id)["status"], "running")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.published, [])

    def test_publish_failure_keeps_inbox_and_never_claims_delivery(self):
        self.begin()
        def failed_publish(owner, notification):
            self.assertEqual(len(self.rows()), 1)
            raise RuntimeError("provider unavailable")
        service = ConnectorEventService(self.store, publisher=failed_publish)
        receipt = service.ingest(self.binding, self.event(version=3, status="succeeded", result_ref="result-42"))
        ack = service.task(self.binding, self.task_id)["notifications"][receipt["notification_id"]]
        self.assertEqual(ack, {"delivered_at": None, "read_at": None})
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_duplicate_writers_persist_one_notification(self):
        self.begin()
        event = self.event(version=3, status="succeeded", result_ref="result-42")
        # Independent stores/services simulate different application workers.
        def ingest(_):
            service = ConnectorEventService(SQLiteStore(self.path), publisher=lambda owner, row: None)
            return service.ingest(self.binding, event)
        with ThreadPoolExecutor(max_workers=8) as executor:
            receipts = list(executor.map(ingest, range(8)))
        self.assertEqual(sum(not item["duplicate"] for item in receipts), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_restart_and_mutable_inbox_metadata_do_not_erase_execution_dedupe(self):
        event, receipt = self.succeed()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE notifications SET metadata_json='{}'")
        restarted = ConnectorEventService(SQLiteStore(self.path), publisher=lambda owner, row: self.fail("must not republish"))
        self.assertTrue(restarted.ingest(self.binding, event)["duplicate"])
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM notifications")
        self.assertEqual(restarted.ingest(self.binding, event)["notification_id"], receipt["notification_id"])
        self.assertEqual(self.rows(), [])
        with self.assertRaises(ConnectorEventForbidden):
            restarted.ingest(self.binding, self.event(kind="delivery.ack", notification_id=receipt["notification_id"], ack="delivered"))


if __name__ == "__main__":
    unittest.main()
