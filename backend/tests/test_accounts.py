"""Independent-account contract tests with an isolated PG schema and fake Redis."""

import json
import logging
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from tests.pg import reset_tables

from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg2 import IntegrityError

from app import auth, main, telemetry
from app.db import CacheUnavailable, get_connection


BOOTSTRAP_TOKEN = "synthetic-bootstrap-token-184927"


class FakeRedis:
    """Model atomic auth scripts and session operations without shared Redis."""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.sets = {}
        self.windows = {}
        self.locks = {}
        self.now = 10000
        self.writes = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=60):
        self.store[key] = value
        self.ttls[key] = ttl
        return True

    def set_existing(self, key, value, ttl=60):
        self.writes.append((key, value, ttl))
        return self.set(key, value, ttl) if key in self.store else False

    def delete(self, key):
        existed = key in self.store or key in self.windows or key in self.sets
        self.store.pop(key, None)
        self.ttls.pop(key, None)
        self.windows.pop(key, None)
        self.sets.pop(key, None)
        return existed

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)
        return len(members)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def srem(self, key, *members):
        values = self.sets.setdefault(key, set())
        before = len(values)
        values.difference_update(members)
        return before - len(values)

    def eval(self, script, keys, args):
        if script == auth._PASSWORD_ATTEMPT_SCRIPT:
            ip_key, lock_key = keys
            values = [stamp for stamp in self.windows.get(ip_key, []) if stamp > self.now - 60]
            if len(values) >= 20:
                return 2
            values.append(self.now)
            self.windows[ip_key] = values
            return int(self.locks.get(lock_key, 0) > self.now)
        if script == auth._PASSWORD_FAILURE_SCRIPT:
            failures_key, lock_key = keys
            if self.locks.get(lock_key, 0) > self.now:
                return 1
            values = [stamp for stamp in self.windows.get(failures_key, []) if stamp > self.now - 900]
            values.append(self.now)
            if len(values) >= 5:
                self.locks[lock_key] = self.now + 900
                self.windows.pop(failures_key, None)
                return 1
            self.windows[failures_key] = values
            return 0
        if script == auth._PASSWORD_SUCCESS_SCRIPT:
            if self.locks.get(keys[1], 0) > self.now:
                return 1
            self.windows.pop(keys[0], None)
            return 0
        if script == auth._REGISTRATION_ATTEMPT_SCRIPT:
            values = [stamp for stamp in self.windows.get(keys[0], []) if stamp > self.now - 3600]
            if len(values) >= 10:
                return 1
            values.append(self.now)
            self.windows[keys[0]] = values
            return 0
        raise AssertionError("unexpected authentication Redis script")

    def install(self, case):
        for name, callback in (
            ("cache_get_strict", self.get), ("cache_set_strict", self.set),
            ("cache_set_existing_strict", self.set_existing), ("cache_delete_strict", self.delete),
            ("cache_sadd_strict", self.sadd), ("cache_smembers_strict", self.smembers),
            ("cache_srem_strict", self.srem), ("cache_eval_strict", self.eval),
        ):
            item = patch.object(auth, name, side_effect=callback)
            item.start()
            case.addCleanup(item.stop)


class CapturedLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(self.format(record))


class AccountTests(unittest.TestCase):
    password = "Useful-test-password-849!"
    bootstrap_token = BOOTSTRAP_TOKEN

    def setUp(self):
        reset_tables()
        self.redis = FakeRedis()
        self.redis.install(self)
        self.file_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.file_dir.cleanup)
        for item in (
            patch.dict(os.environ, {"AUTH_REQUIRED": "true", "AUTH_SESSION_SECRET": "synthetic-account-session-secret-" + "x" * 40,
                "AUTH_COOKIE_SECURE": "false", "AUTH_REGISTRATION": "open", "AUTH_INVITE_CODE": "synthetic-invite-value",
                "AUTH_BOOTSTRAP_TOKEN": self.bootstrap_token,
                "ADMIN_USER_IDS": "", "AUTH_SESSION_TTL_DAYS": "7", "AUTH_SESSION_MAX_DAYS": "30",
                "AGENT_RUNTIME_ENABLED": "false", "FILE_STORAGE": "local", "ASSISTANT_FILE_ROOT": self.file_dir.name}),
            patch.object(auth, "_read_dotenv"), patch.object(telemetry, "_requests", []),
            patch.object(telemetry, "_stats", {}), patch.object(telemetry, "_devices", {}),
            patch.object(telemetry, "_users", {}), patch.object(telemetry, "_user_seen", {}),
        ):
            item.start()
            self.addCleanup(item.stop)
        # Lifespan-free: migrations already ran in tests.pg; no worker threads
        # or telemetry flushes may race our deterministic account assertions.
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def register(self, username="account_one", **extra):
        bootstrap_token = extra.pop("bootstrap_token", self.bootstrap_token)
        payload = {"username": username, "password": self.password, **extra}
        if bootstrap_token is not None:
            payload["bootstrap_token"] = bootstrap_token
        return self.client.post("/api/v1/auth/register", json=payload)

    def bearer(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    def login(self, login="account_one", password=None, **kwargs):
        return self.client.post("/api/v1/auth/login", json={"login": login, "password": password or self.password}, **kwargs)

    def test_first_registration_with_bootstrap_is_admin_when_registration_closed(self):
        with patch.dict(os.environ, {"AUTH_REGISTRATION": "closed"}):
            self.assertEqual(self.client.get("/api/v1/auth/config").json(), {"registration_open": True, "requires_invite": False, "bootstrap_required": True})
            first = self.register()
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["user"]["role"], "admin")
            self.assertEqual(self.client.get("/api/v1/auth/config").json(), {"registration_open": False, "requires_invite": False, "bootstrap_required": False})
            self.assertEqual(self.register("account_two").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/auth/me").json(), first.json()["user"])
        cookie = first.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM users").fetchone()
        self.assertNotEqual(row["password_hash"], self.password)
        self.assertTrue(row["password_hash"].startswith(("$argon2id$", "scrypt$")))
        self.assertEqual(row["user_id"], first.json()["user"]["user_id"])

    def test_first_registration_requires_configured_bootstrap_token_of_at_least_sixteen_characters(self):
        for configured in ("", "short-token", "界" * 15):
            with self.subTest(configured_length=len(configured)), patch.dict(os.environ, {"AUTH_BOOTSTRAP_TOKEN": configured}):
                response = self.register(bootstrap_token=configured)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {"detail": "尚未完成初始化：请在服务器上设置 AUTH_BOOTSTRAP_TOKEN 后创建首个管理员"})
                self.assertTrue(self.client.get("/api/v1/auth/config").json()["bootstrap_required"])
                with get_connection() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"], 0)

    def test_first_registration_rejects_missing_or_invalid_bootstrap_without_disclosing_length(self):
        for supplied in (None, "", "bad", "invalid-bootstrap-token-739145"):
            with self.subTest(supplied_length=len(supplied) if supplied is not None else None):
                response = self.register(bootstrap_token=supplied)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {"detail": "初始化令牌无效"})
                with get_connection() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"], 0)

    def test_unicode_bootstrap_token_uses_constant_time_comparison(self):
        token = "甲" * 16
        with patch.dict(os.environ, {"AUTH_BOOTSTRAP_TOKEN": token}), patch.object(auth.hmac, "compare_digest", wraps=auth.hmac.compare_digest) as comparison:
            response = self.register(bootstrap_token=token)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["role"], "admin")
        comparison.assert_any_call(token.encode("utf-8"), token.encode("utf-8"))

    def test_open_invite_and_closed_registration_modes(self):
        self.bearer(self.register())
        second = self.register("account_two")
        self.assertEqual(second.json()["user"]["role"], "user")
        with patch.dict(os.environ, {"AUTH_REGISTRATION": "invite"}):
            self.assertEqual(self.client.get("/api/v1/auth/config").json(), {"registration_open": True, "requires_invite": True, "bootstrap_required": False})
            self.assertEqual(self.register("invite_none").status_code, 403)
            self.assertEqual(self.register("invite_bad", invite_code="bad").status_code, 403)
            invited = self.register("invite_good", invite_code="synthetic-invite-value")
            self.assertEqual(invited.status_code, 200)
            self.assertEqual(invited.json()["user"]["role"], "user")
        with patch.dict(os.environ, {"AUTH_REGISTRATION": "closed"}):
            self.assertEqual(self.register("closed_no_invite").status_code, 403)
            self.assertEqual(self.register("closed_one", invite_code="synthetic-invite-value").status_code, 403)
        with patch.dict(os.environ, {"AUTH_BOOTSTRAP_TOKEN": ""}):
            third = self.register("after_bootstrap_removed", bootstrap_token=None)
            self.assertEqual(third.status_code, 200)
            self.assertEqual(third.json()["user"]["role"], "user")
            self.assertEqual(self.client.get("/api/v1/auth/config").json(), {"registration_open": True, "requires_invite": False, "bootstrap_required": False})

    def test_concurrent_bootstrap_registrations_create_only_one_admin_in_invite_mode(self):
        ready = Barrier(2)
        original_hash_password = auth.hash_password

        def synchronized_hash_password(password):
            encoded = original_hash_password(password)
            # Both requests finish password validation before either can take
            # the real PostgreSQL bootstrap lock and observe the user table.
            ready.wait(timeout=10)
            return encoded

        clients = [TestClient(main.app), TestClient(main.app)]
        for client in clients:
            self.addCleanup(client.close)

        def register(client, username):
            return client.post("/api/v1/auth/register", json={"username": username, "password": self.password, "bootstrap_token": self.bootstrap_token})

        with patch.dict(os.environ, {"AUTH_REGISTRATION": "invite"}), patch.object(auth, "hash_password", side_effect=synchronized_hash_password), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(register, client, "concurrent_%d" % index) for index, client in enumerate(clients)]
            responses = [future.result(timeout=20) for future in futures]
        self.assertEqual(sorted(response.status_code for response in responses), [200, 403])
        for response in responses:
            if response.status_code == 200:
                self.assertEqual(response.json()["user"]["role"], "admin")
            else:
                self.assertEqual(response.json(), {"detail": "邀请码无效"})
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"], 1)
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM users WHERE role = ?", ("admin",)).fetchone()["n"], 1)

    def test_failed_bootstrap_attempts_count_towards_registration_limit(self):
        for index in range(10):
            response = self.register("bootstrap_attempt_%d" % index, bootstrap_token="wrong-bootstrap-token-7291")
            self.assertEqual(response.status_code, 403)
        limited = self.register("bootstrap_limited")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "3600")
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"], 0)
        self.redis.now += 3601
        self.assertEqual(self.register("bootstrap_after_limit").status_code, 200)

    def test_server_admin_creation_needs_no_bootstrap_or_session_secret(self):
        with patch.dict(os.environ, {"AUTH_BOOTSTRAP_TOKEN": "", "AUTH_SESSION_SECRET": "", "AUTH_REGISTRATION": "closed"}):
            user = auth.create_admin_account("server_admin", self.password, "ADMIN@EXAMPLE.TEST")
        self.assertEqual(user["role"], "admin")
        self.assertEqual(user["email"], "admin@example.test")
        self.assertNotIn("password_hash", user)
        self.assertNotIn(self.password, json.dumps(user))
        self.assertEqual(self.redis.store, {})
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user["user_id"],)).fetchone()
        self.assertEqual(row["login_count"], 0)
        self.assertIsNone(row["last_login_at"])
        self.assertTrue(auth.verify_password(self.password, row["password_hash"]))

    def test_server_admin_creation_refuses_existing_users_unless_forced(self):
        first = auth.create_admin_account("server_admin", self.password)
        with self.assertRaises(HTTPException) as denied:
            auth.create_admin_account("another_admin", self.password)
        self.assertEqual(denied.exception.status_code, 403)
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"], 1)
        second = auth.create_admin_account("another_admin", self.password, force_additional_admin=True)
        self.assertEqual(second["role"], "admin")
        self.assertNotEqual(first["user_id"], second["user_id"])
        with get_connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM users WHERE role = ?", ("admin",)).fetchone()["n"], 2)

    def test_duplicate_usernames_and_emails_are_rejected_case_insensitively(self):
        first = self.register(email="PERSON@EXAMPLE.TEST")
        self.assertEqual(first.json()["user"]["email"], "person@example.test")
        for username, email in (("ACCOUNT_ONE", None), ("account_other", "Person@Example.Test")):
            with self.subTest(username=username):
                response = self.register(username, email=email)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"], "用户名或邮箱已被使用")

    def test_weak_password_and_invalid_username_rules(self):
        for value in ("12345678", "password", "password123", "ACCOUNT_ONE", "short", "x" * 129):
            with self.subTest(length=len(value)):
                response = self.register(password=value)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(value, response.text)
        self.assertEqual(self.register(" account_one").status_code, 422)
        self.assertEqual(self.register(email="invalid").status_code, 422)

    def test_username_and_email_login_issue_hashed_sessions(self):
        first = self.register(email="person@example.test", display_name="Example Person")
        user = first.json()["user"]
        for login in ("ACCOUNT_ONE", "PERSON@EXAMPLE.TEST"):
            response = self.login(login, headers={"X-Luma-Client": "flutter", "X-Luma-Platform": "ios"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["user"], user)
            token = response.json()["access_token"]
            digest = auth._token_hash(token)
            data = json.loads(self.redis.store[auth._session_key(digest, is_hash=True)])
            self.assertEqual(data["client_type"], "flutter")
            self.assertEqual(data["platform"], "ios")
            self.assertNotIn(token, json.dumps(self.redis.store))
            self.assertNotIn(self.password, json.dumps(self.redis.store))
            self.assertEqual(self.client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token}).json(), user)

    def test_login_failure_is_uniform_and_unknown_user_verifies_dummy_hash(self):
        self.bearer(self.register())
        missing = self.login("missing_account", "Wrong-test-password-738!")
        wrong = self.login(password="Wrong-test-password-738!")
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(missing.json(), wrong.json())
        self.assertEqual(wrong.json()["detail"], "用户名或密码错误")
        with patch.object(auth, "verify_password", wraps=auth.verify_password) as verifier:
            self.login("another_missing", "Wrong-test-password-738!")
            self.assertEqual(verifier.call_args.args[1], auth._DUMMY_HASH)

    def test_five_failures_lock_account_across_username_and_email(self):
        self.bearer(self.register(email="person@example.test"))
        for index in range(4):
            login = "account_one" if index % 2 else "person@example.test"
            self.assertEqual(self.login(login, "Wrong-test-password-738!").status_code, 401)
        response = self.login(password="Wrong-test-password-738!")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "900")
        self.assertEqual(self.login("person@example.test").status_code, 429)
        self.redis.now += 901
        self.assertEqual(self.login("person@example.test").status_code, 200)

    def test_ip_limit_cannot_be_bypassed_with_forwarded_header(self):
        for index in range(20):
            response = self.login("unknown_%d" % index, headers={"X-Forwarded-For": "203.0.113.%d" % (index + 1)})
            self.assertEqual(response.status_code, 401)
        limited = self.login("unknown_last", headers={"X-Forwarded-For": "203.0.113.99"})
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "60")
        self.redis.now += 61
        self.assertEqual(self.login("unknown_reset").status_code, 401)

    def test_registration_ip_limit_is_ten_per_hour(self):
        for index in range(10):
            self.assertEqual(self.register("registered_%d" % index).status_code, 200)
        self.assertEqual(self.register("registered_11").status_code, 429)
        self.redis.now += 3601
        self.assertEqual(self.register("registered_after").status_code, 200)

    def test_password_change_preserves_current_and_revokes_other_sessions(self):
        first = self.register()
        other_headers = self.bearer(first)
        current = self.login()
        current_headers = self.bearer(current)
        user_id = current.json()["user"]["user_id"]
        # A missing Redis index entry must not let another worker's session
        # survive credential rotation: the DB generation is authoritative.
        self.redis.sets[auth._session_index_key(user_id)].discard(auth._token_hash(first.json()["access_token"]))
        changed = self.client.post("/api/v1/account/password", json={"current_password": self.password, "new_password": "Replacement-password-272!"}, headers=current_headers)
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=current_headers).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=other_headers).status_code, 401)
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login(password="Replacement-password-272!").status_code, 200)

    def test_disabling_account_revokes_all_sessions_and_live_role_changes_apply(self):
        administrator = self.register("admin_account")
        admin_headers = self.bearer(administrator)
        member = self.register()
        member_headers = self.bearer(member)
        other_headers = self.bearer(self.login())
        user_id = member.json()["user"]["user_id"]
        self.assertEqual(self.client.get("/api/admin/users", headers=member_headers).status_code, 403)
        promoted = self.client.patch("/api/admin/users/" + user_id, json={"role": "admin"}, headers=admin_headers)
        self.assertEqual(promoted.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=member_headers).json()["role"], "admin")
        self.assertEqual(self.client.get("/api/admin/users", headers=member_headers).status_code, 200)
        disabled = self.client.patch("/api/admin/users/" + user_id, json={"status": "disabled"}, headers=admin_headers)
        self.assertEqual(disabled.status_code, 200)
        for headers in (member_headers, other_headers):
            self.assertEqual(self.client.get("/api/v1/auth/me", headers=headers).status_code, 401)
        self.assertEqual(self.login().status_code, 401)
        for payload in ({"role": "user"}, {"status": "disabled"}):
            protected = self.client.patch("/api/admin/users/" + administrator.json()["user"]["user_id"], json=payload, headers=admin_headers)
            self.assertEqual(protected.status_code, 400)

    def test_profile_update_normalizes_email_and_rejects_duplicates(self):
        first = self.register(email="first@example.test")
        first_headers = self.bearer(first)
        second = self.register("account_two", email="second@example.test")
        second_headers = self.bearer(second)
        response = self.client.patch("/api/v1/account/profile", json={"display_name": "New Name", "email": "NEW@EXAMPLE.TEST"}, headers=first_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["email"], "new@example.test")
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=first_headers).json()["display_name"], "New Name")
        self.assertEqual(self.client.patch("/api/v1/account/profile", json={"email": "NEW@EXAMPLE.TEST"}, headers=second_headers).status_code, 409)
        self.assertEqual(self.client.patch("/api/v1/account/profile", json={"email": None}, headers=first_headers).json()["email"], None)

    def test_account_delete_cascades_owned_data_and_removes_file_bytes(self):
        first = self.register()
        headers = self.bearer(first)
        user_id = first.json()["user"]["user_id"]
        session = self.client.post("/api/v1/sessions", json={"title": "Example session"}, headers=headers).json()
        uploaded = self.client.post("/api/v1/files", files={"upload": ("example.txt", b"private example", "text/plain")}, data={"session_id": session["id"]}, headers=headers)
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.assertEqual(self.client.post("/api/v1/memories", json={"content": "Example memory"}, headers=headers).status_code, 201)
        connector = self.client.post("/api/v1/connectors", json={"name": "Example service", "kind": "api"}, headers=headers)
        self.assertEqual(connector.status_code, 201, connector.text)
        with get_connection() as conn:
            conn.execute("INSERT INTO messages(id,user_id,session_id,role,content,created_at) VALUES (?,?,?,?,?,?)", ("delete_example_message", user_id, session["id"], "user", "Example", auth._timestamp()))
            record = conn.execute("SELECT * FROM files WHERE id = ?", (uploaded.json()["id"],)).fetchone()
            conn.execute("INSERT INTO connector_secrets(connector_id,user_id,ciphertext,updated_at) VALUES (?,?,?,?)", (connector.json()["id"], user_id, "synthetic-ciphertext", auth._timestamp()))
        path = Path(self.file_dir.name) / record["storage_key"]
        self.assertTrue(path.exists())
        self.assertEqual(self.client.request("DELETE", "/api/v1/account", json={"password": "Wrong-confirmation-927!"}, headers=headers).status_code, 401)
        other = self.register("account_two")
        other_headers = self.bearer(other)
        deleted = self.client.request("DELETE", "/api/v1/account", json={"password": self.password}, headers=headers)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertFalse(path.exists())
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=other_headers).status_code, 200)
        with get_connection() as conn:
            for table in ("users", "sessions", "messages", "files", "memories", "connectors", "connector_secrets", "client_devices"):
                self.assertEqual(conn.execute("SELECT count(*) AS n FROM " + table + " WHERE user_id = ?", (user_id,)).fetchone()["n"], 0)
        with self.assertRaises(IntegrityError), get_connection() as conn:
            conn.execute("INSERT INTO memories(id,user_id,content,created_at,updated_at) VALUES (?,?,?,?,?)", ("late_deleted_memory", user_id, "Late write", auth._timestamp(), auth._timestamp()))

    def test_user_cannot_read_another_users_sessions_files_or_memories(self):
        first = self.register()
        first_headers = self.bearer(first)
        session = self.client.post("/api/v1/sessions", json={"title": "Private session"}, headers=first_headers).json()
        uploaded = self.client.post("/api/v1/files", files={"upload": ("private.txt", b"private data", "text/plain")}, headers=first_headers).json()
        memory = self.client.post("/api/v1/memories", json={"content": "Private memory"}, headers=first_headers).json()
        second_headers = self.bearer(self.register("account_two"))
        self.assertEqual(self.client.get("/api/v1/sessions/" + session["id"], headers=second_headers).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/files/" + uploaded["id"], headers=second_headers).status_code, 404)
        self.assertNotIn(memory["id"], {item["id"] for item in self.client.get("/api/v1/memories", headers=second_headers).json()})
        self.assertEqual(self.client.patch("/api/v1/memories/" + memory["id"], json={"content": "Unauthorized"}, headers=second_headers).status_code, 404)
        first_session = auth._token_hash(first.json()["access_token"])
        self.assertFalse(self.client.delete("/api/v1/auth/sessions/" + first_session, headers=second_headers).json()["revoked"])
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=first_headers).status_code, 200)

    def test_credentials_never_appear_in_errors_logs_or_telemetry(self):
        captured = CapturedLogs()
        logger = logging.getLogger()
        logger.addHandler(captured)
        self.addCleanup(logger.removeHandler, captured)
        wrong_bootstrap = "Do-not-log-this-bootstrap-token-821!"
        rejected_bootstrap = self.register(bootstrap_token=wrong_bootstrap)
        self.assertEqual(rejected_bootstrap.status_code, 403)
        first = self.register()
        self.assertEqual(first.status_code, 200)
        for response in (rejected_bootstrap, first):
            for credential in (self.password, self.bootstrap_token, wrong_bootstrap):
                self.assertNotIn(credential, response.text)
        malformed_bootstrap = self.register("malformed_bootstrap", bootstrap_token=[self.bootstrap_token])
        self.assertEqual(malformed_bootstrap.status_code, 422)
        self.assertNotIn(self.bootstrap_token, malformed_bootstrap.text)
        from app.routers.auth import RegisterPayload
        payload = RegisterPayload(username="test_account", password=self.password, bootstrap_token=self.bootstrap_token)
        self.assertNotIn(self.password, repr(payload))
        self.assertNotIn(self.bootstrap_token, repr(payload))
        secret = "Do-not-log-this-password-294!"
        response = self.login(password=secret)
        self.assertEqual(response.status_code, 401)
        for payload in ({"login": "account_one", "password": [secret]}, {"login": "account_one", "password": secret * 9}, [secret]):
            response = self.client.post("/api/v1/auth/login", json=payload)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(secret, response.text)
        response = self.client.post("/api/v1/auth/register", json={"username": "new_account", "password": [secret]})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertEqual(self.client.post("/api/v1/auth/login", data={"login": "account_one", "password": secret}).status_code, 415)
        for credential in (secret, self.password, self.bootstrap_token, wrong_bootstrap):
            self.assertNotIn(credential, "\n".join(captured.messages))
            self.assertNotIn(credential, json.dumps(telemetry._requests))
            self.assertNotIn(credential, json.dumps(self.redis.store))

    def test_cache_failure_is_closed_and_auth_flag_cannot_bypass_login(self):
        self.bearer(self.register())
        with patch.object(auth, "cache_get_strict", side_effect=CacheUnavailable()):
            self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 503)
        self.client.cookies.clear()
        with patch.dict(os.environ, {"AUTH_REQUIRED": "false"}):
            self.assertEqual(self.client.get("/api/v1/sessions").status_code, 401)

    def test_scrypt_fallback_is_salted_self_describing_and_verifiable(self):
        with patch.object(auth, "PasswordHasher", None):
            first = auth.hash_password(self.password)
            second = auth.hash_password(self.password)
        self.assertTrue(first.startswith("scrypt$32768$8$1$"))
        self.assertNotEqual(first, second)
        self.assertTrue(auth.verify_password(self.password, first))
        self.assertFalse(auth.verify_password("Wrong-password-748!", first))

    def test_scrypt_fallback_supports_python_without_openssl_scrypt(self):
        with patch.object(auth, "PasswordHasher", None), patch.object(auth.hashlib, "scrypt", None, create=True):
            encoded = auth.hash_password(self.password)
            self.assertTrue(auth.verify_password(self.password, encoded))
            self.assertFalse(auth.verify_password("Wrong-password-748!", encoded))
