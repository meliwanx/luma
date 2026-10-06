"""Administrator CLI validation, secrecy and PostgreSQL bootstrap locking."""

from __future__ import annotations

import io
import logging
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from threading import Barrier
from unittest.mock import Mock, patch

from tests import pg

from fastapi import HTTPException
from psycopg2 import IntegrityError

from app import auth, cli
from app.db import get_connection


class AdminCliTests(unittest.TestCase):
    password = "Admin-cli-test-password-572!"

    def setUp(self):
        pg.reset_tables()
        for item in (
            patch.dict(os.environ, {"AUTH_BOOTSTRAP_TOKEN": "", "AUTH_SESSION_SECRET": "", "AUTH_REGISTRATION": "closed"}),
            patch.object(auth, "_read_dotenv"),
        ):
            item.start()
            self.addCleanup(item.stop)

    def run_cli(self, *options, passwords=None, stdin=""):
        stdout, stderr = io.StringIO(), io.StringIO()
        if passwords is None:
            passwords = [self.password, self.password]
        with patch.object(cli.getpass, "getpass", side_effect=passwords) as prompt, patch.object(
            cli.sys, "stdin", io.StringIO(stdin)
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(["create-admin", "--username", "cli_admin", *options])
        return code, stdout.getvalue(), stderr.getvalue(), prompt

    def users(self):
        with get_connection() as conn:
            return conn.execute("SELECT * FROM users ORDER BY username").fetchall()

    def test_empty_database_creates_admin_without_bootstrap_or_session(self):
        with patch.object(auth, "_issue_session") as issue_session:
            code, stdout, stderr, prompt = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "username=cli_admin role=admin\n")
        self.assertEqual(stderr, "")
        self.assertEqual(prompt.call_count, 2)
        issue_session.assert_not_called()
        rows = self.users()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "admin")
        self.assertEqual(rows[0]["status"], "active")
        self.assertEqual(rows[0]["display_name"], "cli_admin")
        self.assertEqual(rows[0]["login_count"], 0)
        self.assertIsNone(rows[0]["last_login_at"])
        self.assertTrue(auth.verify_password(self.password, rows[0]["password_hash"]))

    def test_existing_user_refuses_creation_with_exit_two(self):
        auth.create_admin_account("existing_admin", self.password)
        code, stdout, stderr, _ = self.run_cli()
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--force-additional-admin", stderr)
        self.assertEqual([row["username"] for row in self.users()], ["existing_admin"])

    def test_force_creates_additional_admin(self):
        auth.create_admin_account("existing_admin", self.password)
        code, stdout, _, _ = self.run_cli("--force-additional-admin")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "username=cli_admin role=admin\n")
        self.assertEqual([row["role"] for row in self.users()], ["admin", "admin"])

    def test_mismatched_passwords_do_not_access_database(self):
        with patch.object(cli, "ensure_db") as migrate:
            code, stdout, stderr, prompt = self.run_cli(passwords=[self.password, "Different-password-581!"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("两次输入的密码不一致", stderr)
        self.assertEqual(prompt.call_count, 2)
        migrate.assert_not_called()
        self.assertEqual(self.users(), [])

    def test_weak_passwords_use_existing_account_rules(self):
        for password in ("short", "password123", "CLI_ADMIN", "x" * 129):
            with self.subTest(password_length=len(password)):
                code, stdout, stderr, _ = self.run_cli(passwords=[password, password])
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("密码必须为", stderr)
                self.assertNotIn(password, stderr)
        self.assertEqual(self.users(), [])

    def test_duplicate_username_exits_three_case_insensitively(self):
        auth.create_admin_account("CLI_ADMIN", self.password)
        code, stdout, stderr, _ = self.run_cli("--force-additional-admin")
        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        self.assertIn("用户名或邮箱已被使用", stderr)
        self.assertEqual(len(self.users()), 1)

    def test_duplicate_email_exits_three_case_insensitively(self):
        auth.create_admin_account("existing_admin", self.password, "ADMIN@EXAMPLE.TEST")
        code, stdout, stderr, _ = self.run_cli("--email", "Admin@Example.Test", "--force-additional-admin")
        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        self.assertIn("用户名或邮箱已被使用", stderr)
        self.assertEqual(len(self.users()), 1)

    def test_stdin_reads_only_one_line_and_preserves_password_spaces(self):
        password = " " + self.password + " "
        stdin = io.StringIO(password + "\r\nsecond-line-is-not-password\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli.sys, "stdin", stdin), patch.object(cli.getpass, "getpass") as prompt, redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            code = cli.main(["create-admin", "--username", "cli_admin", "--password-stdin"])
        self.assertEqual(code, 0)
        prompt.assert_not_called()
        self.assertEqual(stdin.read(), "second-line-is-not-password\n")
        self.assertTrue(auth.verify_password(password, self.users()[0]["password_hash"]))

    def test_stdin_eof_is_rejected_without_migration(self):
        with patch.object(cli, "ensure_db") as migrate:
            code, stdout, stderr, prompt = self.run_cli("--password-stdin")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("未从 stdin 读取到密码", stderr)
        prompt.assert_not_called()
        migrate.assert_not_called()
        self.assertEqual(self.users(), [])

    def test_success_output_contains_neither_password_nor_hash(self):
        code, stdout, stderr, _ = self.run_cli("--password-stdin", stdin=self.password + "\n")
        self.assertEqual(code, 0)
        output = stdout + stderr
        self.assertNotIn(self.password, output)
        self.assertNotIn(self.users()[0]["password_hash"], output)
        self.assertNotIn("password_hash", output)
        self.assertNotIn("$argon2", output)
        self.assertNotIn("scrypt$", output)

    def test_email_and_display_name_use_existing_normalization(self):
        code, _, _, _ = self.run_cli("--email", " ADMIN@EXAMPLE.TEST ", "--display-name", "  CLI 管理员  ")
        self.assertEqual(code, 0)
        row = self.users()[0]
        self.assertEqual(row["email"], "admin@example.test")
        self.assertEqual(row["display_name"], "CLI 管理员")

    def test_invalid_account_fields_are_rejected(self):
        for options in (("--username", "invalid user"), ("--email", "invalid"), ("--display-name", " ")):
            with self.subTest(options=options):
                code, stdout, _, _ = self.run_cli(*options)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
        self.assertEqual(self.users(), [])

    def test_migrations_finish_before_account_transaction(self):
        calls = Mock()
        with patch.object(cli, "ensure_db", wraps=cli.ensure_db) as migrate, patch.object(
            cli, "get_connection", wraps=cli.get_connection
        ) as connect:
            calls.attach_mock(migrate, "migrate")
            calls.attach_mock(connect, "connect")
            code, _, _, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in calls.mock_calls], ["migrate", "connect"])

    def test_database_failure_does_not_echo_exception_values(self):
        sensitive = "synthetic-db-credential-never-print-591"
        previous_level = logging.root.manager.disable
        with patch.object(cli, "ensure_db", side_effect=RuntimeError(sensitive)):
            code, stdout, stderr, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("请检查数据库配置与迁移状态", stderr)
        self.assertNotIn(sensitive, stderr)
        self.assertNotIn(self.password, stderr)
        self.assertEqual(logging.root.manager.disable, previous_level)
        self.assertEqual(self.users(), [])

    def test_database_unique_constraint_maps_to_exit_three(self):
        sensitive = "synthetic-password-hash-never-print-385"
        with patch.object(auth, "_insert_account", side_effect=IntegrityError(sensitive)):
            code, stdout, stderr, _ = self.run_cli()
        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        self.assertIn("用户名或邮箱已被使用", stderr)
        self.assertNotIn(sensitive, stderr)
        self.assertEqual(self.users(), [])

    def test_interactive_cancellation_does_not_migrate(self):
        for interruption in (EOFError, KeyboardInterrupt):
            with self.subTest(interruption=interruption.__name__), patch.object(cli, "ensure_db") as migrate:
                code, stdout, stderr, _ = self.run_cli(passwords=interruption)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("已取消管理员创建", stderr)
                migrate.assert_not_called()
        self.assertEqual(self.users(), [])

    def test_concurrent_creation_produces_only_one_first_admin(self):
        ready = Barrier(2)
        original_hash = auth.hash_password

        def synchronized_hash(password):
            encoded = original_hash(password)
            # Both callers finish migrations and hashing before either takes
            # the real PostgreSQL lock. Only two pooled connections are used.
            ready.wait(timeout=15)
            return encoded

        def create(username):
            try:
                user = cli.create_admin_account(username, self.password)
                return 0, user["role"]
            except HTTPException as exc:
                return {403: 2, 409: 3}.get(exc.status_code, 1), None

        with patch.object(auth, "hash_password", side_effect=synchronized_hash), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create, "concurrent_cli_%d" % index) for index in range(2)]
            results = [future.result(timeout=25) for future in futures]
        self.assertEqual(sorted(code for code, _ in results), [0, 2])
        rows = self.users()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "admin")
