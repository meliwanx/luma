"""Browser lease coverage using mocked E2B without starting cloud resources."""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from app import agent_runtime


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self):
        self.rows = {}
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))
        rows = list(self.rows.values())
        if query.startswith("INSERT INTO runtime_leases"):
            key = (params["user_id_hash"], params["capability"])
            self.rows[key] = dict(params)
            return _Cursor([])
        if query.startswith("DELETE FROM runtime_leases"):
            for key in list(self.rows):
                if key[0] == params[0] and ((key[1] != params[1]) if "<>" in query else (key[1] == params[1])):
                    del self.rows[key]
            return _Cursor([])
        if "COUNT(*)" in query:
            return _Cursor([{"c": sum(row["status"] == "paused" for row in rows)}])
        if "WHERE id = ?" in query:
            rows = [row for row in rows if row["id"] == params[0]]
        elif "WHERE user_id_hash = ?" in query:
            rows = [row for row in rows if row["user_id_hash"] == params[0]]
            if "capability <> ?" in query:
                rows = [row for row in rows if row["capability"] != params[1]]
            elif "capability = ?" in query:
                rows = [row for row in rows if row["capability"] == params[1]]
        elif "status IN ('running','ready','starting','paused')" in query:
            rows = [row for row in rows if row["status"] in ("running", "ready", "starting", "paused")]
        elif "status IN ('running','ready','starting')" in query:
            rows = [row for row in rows if row["status"] in ("running", "ready", "starting")]
        elif "status = 'paused'" in query:
            rows = [row for row in rows if row["status"] == "paused"]
        else:
            rows = []
        return _Cursor(rows)


def _settings(**changes):
    values = dict(
        enabled=True, api_mode="e2b", api_key="fake-key", e2b_domain="ap-hongkong.tencentags.com",
        provider="test", code_tool="luma-code", browser_tool="luma-browser", aio_tool="",
        idle_ttl_seconds=300, max_instances=5, max_cpu=10, max_memory_gib=10,
        region="ap-hongkong", sandbox_max_paused=18, sandbox_paused_retention_days=30,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def _box(identifier):
    return SimpleNamespace(
        sandbox_id=identifier,
        _envd_access_token="ephemeral-token+/=&",
        get_host=MagicMock(return_value="9000-{}.ap-hongkong.tencentags.com".format(identifier)),
        commands=SimpleNamespace(run=MagicMock(return_value=SimpleNamespace(exit_code=0))),
        set_timeout=MagicMock(), pause=MagicMock(),
    )


class BrowserLeaseTests(unittest.TestCase):
    def setUp(self):
        self._suffix = patch.dict(os.environ, {"SANDBOX_PREVIEW_HOST_SUFFIX": ".tencentags.com"}, clear=False)
        self._suffix.start()
        self.addCleanup(self._suffix.stop)

    def _provider(self):
        boxes = {}

        def create(**_kwargs):
            box = _box("sandbox-{}".format(len(boxes) + 1))
            boxes[box.sandbox_id] = box
            return box

        sdk = SimpleNamespace(
            create=MagicMock(side_effect=create),
            connect=MagicMock(side_effect=lambda identifier, **_kwargs: boxes[identifier]),
            kill=MagicMock(),
        )
        return sdk, boxes

    def test_code_and_browser_leases_are_independent_and_reused(self):
        conn = _Connection()
        sdk, boxes = self._provider()
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.dict(sys.modules, {"e2b": SimpleNamespace(Sandbox=sdk)}):
            code = agent_runtime.connect_user_sandbox("user")
            browser, _, _ = agent_runtime.connect_user_browser("user")
            again, _, _ = agent_runtime.connect_user_browser("user")
            code_lease = agent_runtime.get_user_runtime("user")
            browser_lease = agent_runtime.get_user_runtime("user", capability="browser")
        self.assertEqual(sdk.create.call_count, 2)
        self.assertIs(browser, again)
        self.assertIsNot(code, browser)
        self.assertEqual(code_lease.capabilities, ("code",))
        self.assertEqual(browser_lease.capabilities, ("browser",))
        self.assertEqual(len(conn.rows), 2)
        browser.commands.run.assert_not_called()
        code.commands.run.assert_called_once()
        self.assertNotIn("ephemeral-token", json.dumps(list(conn.rows.values())))

    def test_aio_configuration_creates_one_lease_for_both_capabilities(self):
        conn = _Connection()
        sdk, _ = self._provider()
        with patch.object(agent_runtime, "config", return_value=_settings(aio_tool="luma-aio")), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.dict(sys.modules, {"e2b": SimpleNamespace(Sandbox=sdk)}):
            browser, _, _ = agent_runtime.connect_user_browser("user")
            code = agent_runtime.connect_user_sandbox("user")
            lease = agent_runtime.ensure_user_runtime("user", ("code", "browser"))
        self.assertIs(code, browser)
        self.assertEqual(sdk.create.call_count, 1)
        self.assertEqual(sdk.create.call_args.kwargs["template"], "luma-aio")
        self.assertEqual(len(conn.rows), 1)
        self.assertEqual(lease.capabilities, ("code", "browser"))
        self.assertEqual(lease.capability, "aio")

    def test_browser_live_credentials_are_encoded_and_never_persisted_or_logged(self):
        box = _box("browser")
        with patch.object(agent_runtime, "connect_user_sandbox", return_value=box) as connect, patch.object(
            agent_runtime, "logger"
        ) as logger:
            returned, cdp, live = agent_runtime.connect_user_browser("user")
        self.assertIs(returned, box)
        connect.assert_called_once_with("user", "browser")
        self.assertEqual(parse_qs(urlsplit(cdp).query)["access_token"], [box._envd_access_token])
        live_query = parse_qs(urlsplit(live).query)
        self.assertEqual(live_query["access_token"], [box._envd_access_token])
        self.assertEqual(parse_qs(urlsplit(live_query["path"][0]).query)["access_token"], [box._envd_access_token])
        logger.warning.assert_not_called()

    def test_browser_endpoint_rejects_foreign_hosts_and_userinfo(self):
        for host in ("evil.example", "tencentags.com.evil.example", "token@a.tencentags.com", "a.tencentags.com/path"):
            box = _box("browser")
            box.get_host.return_value = host
            with patch.object(agent_runtime, "connect_user_sandbox", return_value=box):
                with self.assertRaises(agent_runtime.AgentRuntimeUnavailable):
                    agent_runtime.connect_user_browser("user")

    def test_browser_idle_pause_and_resume_skip_workspace_backup(self):
        conn = _Connection()
        sdk, _ = self._provider()
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.dict(sys.modules, {"e2b": SimpleNamespace(Sandbox=sdk)}), patch.object(
            agent_runtime, "_sandbox_archive"
        ) as archive, patch.object(agent_runtime, "_restore_workspace") as restore:
            browser, _, _ = agent_runtime.connect_user_browser("user")
            row = list(conn.rows.values())[0]
            row["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            result = agent_runtime.reap_expired_leases()
            self.assertEqual(list(conn.rows.values())[0]["status"], "paused")
            resumed, _, _ = agent_runtime.connect_user_browser("user")
        self.assertEqual(result, {"stopped": 1, "failed": 0, "expired": 1})
        self.assertIs(browser, resumed)
        browser.pause.assert_called_once()
        browser.commands.run.assert_not_called()
        archive.assert_not_called()
        restore.assert_not_called()
        self.assertEqual(sdk.create.call_count, 1)

    def test_browser_pause_quota_falls_back_to_release_without_archive(self):
        conn = _Connection()
        sdk, _ = self._provider()
        with patch.object(agent_runtime, "config", return_value=_settings()), patch.object(
            agent_runtime, "get_connection", return_value=conn
        ), patch.dict(sys.modules, {"e2b": SimpleNamespace(Sandbox=sdk)}), patch.object(
            agent_runtime, "_sandbox_archive"
        ) as archive:
            browser, _, _ = agent_runtime.connect_user_browser("user")
            browser.pause.side_effect = RuntimeError("maximum paused quota")
            list(conn.rows.values())[0]["expires_at"] = "2020-01-01T00:00:00+00:00"
            result = agent_runtime.reap_expired_leases()
        self.assertEqual(result["failed"], 0)
        self.assertEqual(list(conn.rows.values())[0]["status"], "stopped")
        sdk.kill.assert_called_once()
        archive.assert_not_called()

    def test_configuring_aio_merges_existing_split_leases_and_restores_code(self):
        conn = _Connection()
        sdk, _ = self._provider()
        with patch.object(agent_runtime, "get_connection", return_value=conn), patch.dict(
            sys.modules, {"e2b": SimpleNamespace(Sandbox=sdk)}
        ):
            with patch.object(agent_runtime, "config", return_value=_settings()):
                agent_runtime.connect_user_sandbox("user")
                agent_runtime.connect_user_browser("user")
            with patch.object(agent_runtime, "config", return_value=_settings(aio_tool="luma-aio")), patch.object(
                agent_runtime, "_sandbox_archive", return_value={"storage_key": "fake-backup", "size_bytes": 1}
            ) as archive, patch.object(agent_runtime, "_restore_workspace", return_value=True) as restore:
                agent_runtime.connect_user_browser("user")
        self.assertEqual(len(conn.rows), 1)
        self.assertEqual(list(conn.rows.values())[0]["capability"], "aio")
        self.assertEqual(sdk.kill.call_count, 2)
        archive.assert_called_once()
        restore.assert_called_once()

