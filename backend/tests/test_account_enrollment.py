"""Legacy credential enrollment preserves ownership and rejects overwrites."""

import importlib.util
import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from tests import pg

from app import auth
from app.db import get_connection

spec = importlib.util.spec_from_file_location("enroll_account", Path(__file__).resolve().parents[1] / "scripts" / "enroll_account.py")
enroll_account = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enroll_account)


class AccountEnrollmentTests(unittest.TestCase):
    def setUp(self):
        pg.reset_tables()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO users(user_id,username,created_at,first_seen_at,last_seen_at) VALUES (?,?,?,?,?)",
                ("legacy_owner", "legacy_owner", "2026-10-07", "2026-10-07", "2026-10-07"),
            )

    def test_enrollment_preserves_owner_and_never_overwrites_existing_credentials(self):
        password = "Enrollment-password-593!"
        output = io.StringIO()
        with patch.object(enroll_account.getpass, "getpass", return_value=password), patch.object(
            auth, "revoke_user_sessions"
        ), redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(enroll_account.main(["legacy_owner", "--username", "enrolled_user", "--admin"]), 0)
            self.assertEqual(enroll_account.main(["legacy_owner", "--username", "replacement"]), 1)
        self.assertNotIn(password, output.getvalue())
        with get_connection() as conn:
            account = conn.execute("SELECT * FROM users WHERE user_id = ?", ("legacy_owner",)).fetchone()
        self.assertEqual(account["username"], "enrolled_user")
        self.assertEqual(account["role"], "admin")
        self.assertEqual(account["session_version"], 1)
        self.assertTrue(auth.verify_password(password, account["password_hash"]))
        self.assertEqual(account["auth_provider"], "password")

    def _person(self, user_id, username, provider=None, job_number=None, profile="{}"):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO users(user_id,username,auth_provider,job_number,profile,created_at,first_seen_at,last_seen_at) "
                "VALUES (?,?,?,?,?::jsonb,?,?,?)",
                (user_id, username, provider, job_number, profile, "2026-10-07", "2026-10-07", "2026-10-07"),
            )

    def test_enrollment_rejects_sso_rows_and_unbackfilled_rows(self):
        self._person("sso-person", "sso_person", provider="sso")
        self._person("portal-person", "portal_person")
        self._person("legacy_job", "legacy_job", job_number="42")
        self._person("legacy_prof", "legacy_prof", profile='{"job_number":"88"}')
        output = io.StringIO()
        with patch.object(enroll_account.getpass, "getpass", return_value="Enrollment-password-593!"), patch.object(
            auth, "revoke_user_sessions"
        ), redirect_stdout(output), redirect_stderr(output):
            for user_id in ("sso-person", "portal-person", "legacy_job", "legacy_prof", "missing-person"):
                self.assertEqual(enroll_account.main([user_id, "--username", "new_name"]), 1)
        self.assertNotIn("Enrollment-password-593!", output.getvalue())
        with get_connection() as conn:
            rows = {
                row["user_id"]: row
                for row in conn.execute("SELECT user_id, auth_provider, password_hash FROM users WHERE user_id <> ?", ("legacy_owner",))
            }
        self.assertEqual(rows["sso-person"]["auth_provider"], "sso")
        self.assertIsNone(rows["sso-person"]["password_hash"])
        self.assertIsNone(rows["portal-person"]["auth_provider"])
        self.assertIsNone(rows["portal-person"]["password_hash"])
        self.assertIsNone(rows["legacy_job"]["password_hash"])
        self.assertIsNone(rows["legacy_prof"]["password_hash"])
