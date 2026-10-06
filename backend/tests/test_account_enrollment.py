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
                ("legacy-owner", "legacy_owner", "2026-10-07", "2026-10-07", "2026-10-07"),
            )

    def test_enrollment_preserves_owner_and_never_overwrites_existing_credentials(self):
        password = "Enrollment-password-593!"
        output = io.StringIO()
        with patch.object(enroll_account.getpass, "getpass", return_value=password), patch.object(
            auth, "revoke_user_sessions"
        ), redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(enroll_account.main(["legacy-owner", "--username", "enrolled_user", "--admin"]), 0)
            self.assertEqual(enroll_account.main(["legacy-owner", "--username", "replacement"]), 1)
        self.assertNotIn(password, output.getvalue())
        with get_connection() as conn:
            account = conn.execute("SELECT * FROM users WHERE user_id = ?", ("legacy-owner",)).fetchone()
        self.assertEqual(account["username"], "enrolled_user")
        self.assertEqual(account["role"], "admin")
        self.assertEqual(account["session_version"], 1)
        self.assertTrue(auth.verify_password(password, account["password_hash"]))
