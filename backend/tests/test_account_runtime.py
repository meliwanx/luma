"""Account erasure releases every sandbox capability and its backup."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests import pg

from app import agent_runtime
from app.db import get_connection


class AccountRuntimeTests(unittest.TestCase):
    def setUp(self):
        pg.reset_tables()
        self.owner = "runtime-account"
        self.settings = SimpleNamespace(enabled=True, api_mode="e2b", aio_tool="changed-aio-template")

    def lease(self, capability, backup=None):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO runtime_leases(id,user_id_hash,capability,provider,provider_runtime_id,status,capabilities_json,metadata_json) "
                "VALUES (?,?,?,?,?,'running',?,?)",
                ("lease-" + capability, agent_runtime._hash_user(self.owner), capability,
                 "test", "sandbox-" + capability, json.dumps([capability]),
                 json.dumps({"backup": {"storage_key": backup}}) if backup else "{}"),
            )

    def test_releases_split_leases_when_configuration_has_changed_to_aio(self):
        self.lease("code", "backup-code")
        self.lease("browser", "backup-browser")
        storage = MagicMock()
        with patch.object(agent_runtime, "config", return_value=self.settings), patch.object(
            agent_runtime, "_stop_provider"
        ) as stop, patch.object(agent_runtime, "get_storage", return_value=storage):
            leases = agent_runtime.release_all_user_runtimes(self.owner)
        self.assertEqual({lease.capability for lease in leases}, {"code", "browser"})
        self.assertEqual({call.args[1] for call in stop.call_args_list}, {"sandbox-code", "sandbox-browser"})
        self.assertEqual({call.args[0] for call in storage.delete.call_args_list}, {"backup-code", "backup-browser"})
        self.assertTrue(all(lease.status == "stopped" and "backup" not in lease.metadata for lease in leases))

    def test_backup_failure_keeps_references_for_retry(self):
        self.lease("code", "backup-code")
        storage = MagicMock()
        storage.delete.side_effect = OSError("storage unavailable")
        with patch.object(agent_runtime, "config", return_value=self.settings), patch.object(
            agent_runtime, "_stop_provider"
        ), patch.object(agent_runtime, "get_storage", return_value=storage):
            with self.assertRaises(OSError):
                agent_runtime.release_all_user_runtimes(self.owner)
        with get_connection() as conn:
            row = conn.execute("SELECT status,metadata_json FROM runtime_leases").fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(json.loads(row["metadata_json"])["backup"]["storage_key"], "backup-code")

    def test_disabled_or_deleted_owner_cannot_create_provider_activity(self):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO users(user_id,username,status,created_at,first_seen_at,last_seen_at) VALUES (?,?,'disabled',?,?,?)",
                (self.owner, "runtime-account", "2026-10-07", "2026-10-07", "2026-10-07"),
            )
        with get_connection() as conn:
            with self.assertRaises(agent_runtime.AgentRuntimeUnavailable):
                agent_runtime._require_live_runtime_owner(conn, self.owner)
            conn.execute("DELETE FROM users WHERE user_id = ?", (self.owner,))
        with get_connection() as conn:
            with self.assertRaises(agent_runtime.AgentRuntimeUnavailable):
                agent_runtime._require_live_runtime_owner(conn, self.owner)
