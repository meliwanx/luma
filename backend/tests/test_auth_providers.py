"""Login provider registry: local passwords and an optional SSO provider."""

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import subprocess
import sys
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.pg import reset_tables

from app import auth
from app.auth import validate_auth_configuration
from app.auth_providers import enabled_provider_names, public_providers
from app.auth_providers.password import router as password_router
from app.auth_providers.sso import router as sso_router
from app.db import _redis_client, get_connection
from app.routers.auth import router as session_router


_SSO_SECRET = "synthetic-sso-api-secret-for-tests"
_SSO_HOST = "https://sso.example.test"
_TICKET = "ticket-token-ok-16"


def _provider_app() -> FastAPI:
    application = FastAPI()
    application.include_router(session_router)
    application.include_router(password_router)
    application.include_router(sso_router)
    return application


def _purge_auth_cache() -> None:
    client = _redis_client()
    patterns = (
        "luma:sso:state:*",
        "luma:auth:sso-password:*",
        "luma:auth:password:*",
        "luma:auth:registration:*",
    )
    for pattern in patterns:
        keys = list(client.scan_iter(match=pattern, count=200))
        if keys:
            client.delete(*keys)


class _Upstream:
    def __init__(self):
        self.requests = []
        self.payload = {"success": True, "data": {"user": {"user_id": "sso-user-1", "nickname": "Ada"}}}
        self.http_status = None
        self.error = None
        self.error_body = None

    def __call__(self, request, timeout=8):
        full = str(getattr(request, "full_url", ""))
        if not full.startswith(_SSO_HOST + "/"):
            raise AssertionError("upstream url was not the configured test host")
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.http_status:
            body = b"{}" if self.error_body is None else self.error_body
            raise HTTPError(full, self.http_status, "no", Message(), io.BytesIO(body))
        body = json.dumps(self.payload).encode("utf-8")

        class _Response:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        return _Response()


class ProviderTests(unittest.TestCase):
    def setUp(self):
        reset_tables()
        _purge_auth_cache()
        self.upstream = _Upstream()
        self.client = TestClient(_provider_app())
        self.addCleanup(self.client.close)
        self.addCleanup(_purge_auth_cache)
        for item in (
            patch.object(auth, "_read_dotenv"),
            patch("app.auth_providers.sso.urlopen", self.upstream),
        ):
            item.start()
            self.addCleanup(item.stop)

    def _env(self, **extra):
        values = {
            "AUTH_PROVIDERS": "sso",
            "AUTH_REGISTRATION": "closed",
            "AUTH_COOKIE_SECURE": "false",
            "SSO_BASE_URL": _SSO_HOST,
            "SSO_API_SECRET": _SSO_SECRET,
            "SSO_SYSTEM_SECRET": "",
            "SSO_SYSTEM_CODE": "assistant",
            "SSO_VERIFY_URL": "",
            "SSO_FEDERATED_LOGIN_URL": "",
            "SSO_APP_BASE_URL": "",
            "SSO_CALLBACK_URL": "",
            "AUTH_PASSWORD_LOGIN_ENABLED": "true",
        }
        values.update(extra)
        return patch.dict(os.environ, values)

    def _assert_signature(self, request):
        code = request.get_header("X-system-code")
        stamp = request.get_header("X-system-signature-timestamp")
        signature = request.get_header("X-system-signature")
        self.assertEqual(code, "assistant")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        expected = base64.b64encode(
            hmac.new(_SSO_SECRET.encode(), stamp.encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(signature, expected)

    def test_names_default_to_password_and_reject_unknown_values(self):
        with patch.dict(os.environ, {"AUTH_PROVIDERS": "password"}):
            del os.environ["AUTH_PROVIDERS"]
            self.assertEqual(enabled_provider_names(), ["password"])
        with patch.dict(os.environ, {"AUTH_PROVIDERS": " sso, password , sso "}):
            self.assertEqual(enabled_provider_names(), ["sso", "password"])
        with patch.dict(os.environ, {"AUTH_PROVIDERS": "ldap"}):
            with self.assertRaises(RuntimeError) as raised:
                enabled_provider_names()
            with self.assertRaises(RuntimeError):
                validate_auth_configuration()
        self.assertIn("password, sso", str(raised.exception))

    def test_blank_or_invalid_providers_fail_startup_and_not_import(self):
        from app.auth_providers import mount_providers

        backend = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["AUTH_PROVIDERS"] = ","
        completed = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=backend,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        for raw in ("", "   ", ",", ",,", "ldap", "sso,ldap"):
            with patch.dict(os.environ, {"AUTH_PROVIDERS": raw}):
                mount_providers(FastAPI())
                with self.assertRaises(RuntimeError) as raised:
                    validate_auth_configuration()
            self.assertIn("AUTH_PROVIDERS", str(raised.exception))

    def test_sso_only_register_route_is_not_found(self):
        with self._env(AUTH_PROVIDERS="sso"):
            response = self.client.post("/api/v1/auth/register", json={
                "username": "sso_only",
                "password": "Useful-test-password-849!",
            })
        self.assertEqual(response.status_code, 404)

    def test_blank_system_code_fails_startup_without_calling_upstream(self):
        with self._env(SSO_SYSTEM_CODE=""):
            with self.assertRaises(RuntimeError) as raised:
                validate_auth_configuration()
            self.assertIn("SSO_SYSTEM_CODE", str(raised.exception))
            self.upstream.requests.clear()
            started = self.client.get("/api/v1/auth/sso/start")
            self.assertEqual(started.status_code, 503)
            denied = self.client.post(
                "/api/v1/auth/password/login",
                json={"account": "ada", "password": "secret-password"},
            )
            self.assertIn(denied.status_code, (502, 503))
            self.assertEqual(self.upstream.requests, [])
            self.assertNotIn("secret-password", denied.text)
            self.assertNotIn("assistant", started.text)

    def test_startup_requires_sso_settings_only_when_sso_is_enabled(self):
        with self._env(AUTH_PROVIDERS="password", AUTH_REGISTRATION="open", SSO_BASE_URL="", SSO_API_SECRET=""):
            validate_auth_configuration()
        with self._env(AUTH_PROVIDERS="sso", AUTH_REGISTRATION="not-a-mode"):
            validate_auth_configuration()
        with self._env(AUTH_PROVIDERS="password", AUTH_REGISTRATION="sometimes"):
            with self.assertRaises(RuntimeError) as registration:
                validate_auth_configuration()
        self.assertIn("AUTH_REGISTRATION", str(registration.exception))
        with self._env(AUTH_PROVIDERS="sso", SSO_BASE_URL="", SSO_API_SECRET="", SSO_SYSTEM_SECRET=""):
            with self.assertRaises(RuntimeError) as missing:
                validate_auth_configuration()
        message = str(missing.exception)
        self.assertIn("SSO_BASE_URL", message)
        self.assertIn("SSO_API_SECRET", message)
        self.assertNotIn(_SSO_SECRET, message)

    def test_password_only_matches_the_existing_account_contract(self):
        from app import main

        reset_tables()
        with self._env(AUTH_PROVIDERS="password", AUTH_REGISTRATION="closed", AUTH_BOOTSTRAP_TOKEN="synthetic-bootstrap-token-184927"):
            client = TestClient(main.app)
            self.addCleanup(client.close)
            listed = client.get("/api/v1/auth/providers")
            self.assertEqual(listed.status_code, 200)
            payload = listed.json()
            self.assertEqual([item["kind"] for item in payload["providers"]], ["password"])
            self.assertEqual(payload["registration_open"], True)
            self.assertEqual(payload["bootstrap_required"], True)
            self.assertNotIn("api_secret", listed.text)
            self.assertEqual(client.get("/api/v1/auth/sso/status").status_code, 404)
            created = client.post("/api/v1/auth/register", json={
                "username": "provider_pw",
                "password": "Useful-test-password-849!",
                "bootstrap_token": "synthetic-bootstrap-token-184927",
            })
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()["user"]["username"], "provider_pw")

    def test_sso_ticket_exchange_and_ticket_only_callback(self):
        with self._env():
            started = self.client.get("/api/v1/auth/sso/start", params={"next": "/app"})
            self.assertEqual(started.status_code, 200, started.text)
            url = started.json()["url"]
            if not str(url).startswith(_SSO_HOST + "/"):
                self.fail("upstream url was not the configured test host")
            self.assertNotIn("redirect_uri", url)
            self.assertIn("redirect_system=assistant", url)
            state = started.json()["state"]
            self.upstream.payload = {"success": True, "data": {"user": {
                "user_id": "sso-user-1", "nickname": "Ada", "email": "ada@example.test",
                "job_number": "100", "department_name": "Lab", "status": "left",
            }}}
            exchanged = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET, "state": state})
            self.assertEqual(exchanged.status_code, 200, exchanged.text)
            self.assertTrue(exchanged.json()["authenticated"])
            self.assertIn("luma_session", exchanged.headers.get("set-cookie", ""))
            self._assert_signature(self.upstream.requests[-1])
            if not str(self.upstream.requests[-1].full_url).endswith("/api/sso/verify"):
                self.fail("upstream url was not the configured test host")
            with get_connection() as conn:
                row = conn.execute("SELECT * FROM users WHERE user_id = ?", ("sso-user-1",)).fetchone()
            self.assertEqual(row["auth_provider"], "sso")
            self.assertIsNone(row["password_hash"])
            self.assertEqual(row["status"], "active")
            self.assertEqual(row["job_number"], "100")
            self.assertEqual(row["department_name"], "Lab")
            profile = row["profile"]
            if isinstance(profile, str):
                profile = json.loads(profile)
            self.assertEqual(profile["status"], "left")
            self.assertTrue(row["username"].startswith("sso_"))
            self.assertEqual(len(row["username"]), 32)
            again = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET, "state": state})
            self.assertEqual(again.status_code, 400)
            self.upstream.payload["data"]["user"]["user_id"] = "sso-user-2"
            ticket_only = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET})
            self.assertEqual(ticket_only.status_code, 200, ticket_only.text)

    def test_sso_password_channel_signs_requests_and_rate_limits(self):
        with self._env():
            self.upstream.http_status = 401
            for index in range(4):
                failed = self.client.post("/api/v1/auth/password/login", json={"account": "ada", "password": "wrong-password"})
                self.assertEqual(failed.status_code, 401)
                self.assertEqual(failed.json()["detail"], "账号或密码错误")
                self.assertNotIn("wrong-password", failed.text)
            locked = self.client.post("/api/v1/auth/password/login", json={"account": "ada", "password": "wrong-password"})
            self.assertEqual(locked.status_code, 429)
            self.assertEqual(locked.headers.get("retry-after"), "900")
            self._assert_signature(self.upstream.requests[0])
            if not str(self.upstream.requests[0].full_url).endswith("/api/sso/authenticate"):
                self.fail("upstream url was not the configured test host")
            sent = json.loads(self.upstream.requests[0].data.decode())
            self.assertEqual(sent["account"], "ada")
            self.assertEqual(self.upstream.requests[0].get_header("X-system-code"), "assistant")
            _purge_auth_cache()
            self.upstream.requests.clear()
            self.upstream.http_status = 401
            for index in range(20):
                response = self.client.post(
                    "/api/v1/auth/password/login",
                    json={"account": "person-%02d" % index, "password": "wrong-password"},
                )
                self.assertEqual(response.status_code, 401)
            limited = self.client.post(
                "/api/v1/auth/password/login",
                json={"account": "person-20", "password": "wrong-password"},
            )
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.headers.get("retry-after"), "60")
            self.assertEqual(len(self.upstream.requests), 20)

    def test_successful_sso_password_login_clears_failures_only(self):
        with self._env():
            self.upstream.http_status = 401
            for _ in range(4):
                self.assertEqual(self.client.post(
                    "/api/v1/auth/password/login", json={"account": "ada", "password": "wrong-password"},
                ).status_code, 401)
            self.upstream.http_status = None
            self.upstream.payload = {"success": True, "data": {"user": {"user_id": "sso-user-9", "nickname": "Ada"}}}
            signed_in = self.client.post(
                "/api/v1/auth/password/login", json={"account": "ada", "password": "correct-password"},
            )
            self.assertEqual(signed_in.status_code, 200, signed_in.text)
            self.assertNotIn("correct-password", signed_in.text)
            self.upstream.http_status = 401
            follow = self.client.post(
                "/api/v1/auth/password/login", json={"account": "ada", "password": "wrong-password"},
            )
            self.assertEqual(follow.status_code, 401)

    def test_sso_user_cannot_use_local_password_login(self):
        with self._env(AUTH_PROVIDERS="sso,password", AUTH_REGISTRATION="open"):
            self.upstream.payload = {"success": True, "data": {"user": {"user_id": "sso-user-3", "nickname": "Bea"}}}
            exchanged = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET})
            self.assertEqual(exchanged.status_code, 200, exchanged.text)
            with get_connection() as conn:
                row = conn.execute("SELECT username FROM users WHERE user_id = ?", ("sso-user-3",)).fetchone()
                username = row["username"]
            denied = self.client.post("/api/v1/auth/login", json={"login": username, "password": "Useful-test-password-849!"})
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(denied.json()["detail"], "用户名或密码错误")
            encoded = auth.hash_password("Useful-test-password-849!")
            with get_connection() as conn:
                conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ?", (encoded, "sso-user-3"))
            denied_again = self.client.post("/api/v1/auth/login", json={"login": username, "password": "Useful-test-password-849!"})
            self.assertEqual(denied_again.status_code, 401)
            self.assertEqual(denied_again.json()["detail"], "用户名或密码错误")

    def test_password_owned_user_cannot_be_claimed_by_sso(self):
        with self._env(AUTH_PROVIDERS="sso,password"):
            auth.create_admin_account("local_owner", "Useful-test-password-849!")
            with get_connection() as conn:
                conn.execute("UPDATE users SET user_id = ? WHERE username = ?", ("shared-user", "local_owner"))
            self.upstream.payload = {"success": True, "data": {"user": {"user_id": "shared-user", "nickname": "Pat"}}}
            response = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"], "该用户已绑定其他登录方式")
            self.assertNotIn("access_token", response.json())

    def test_providers_payload_hides_secrets_for_each_combination(self):
        with self._env(AUTH_PROVIDERS="sso"):
            body = self.client.get("/api/v1/auth/providers")
            self.assertEqual(body.status_code, 200)
            text = body.text
            self.assertFalse(_SSO_SECRET in text)
            self.assertNotIn("api_secret", text)
            self.assertNotIn("session_secret", text)
            self.assertNotIn("system_code", text)
            kinds = [item["kind"] for item in body.json()["providers"]]
            self.assertEqual(kinds, ["redirect", "credentials"])
            self.assertEqual(body.json()["sso_label"], "单点登录")
            self.assertEqual(body.json()["account_label"], "账号")
            self.assertFalse(body.json()["bootstrap_required"])
            redirect = body.json()["providers"][0]
            self.assertNotIn("origin", redirect)
            self.assertNotIn("sso.example.test", text)
            status = self.client.get("/api/v1/auth/sso/status")
            self.assertEqual(status.status_code, 200, status.text)
            self.assertNotIn("base_origin", status.json())
            self.assertNotIn("redis_reachable", status.json())
            self.assertNotIn("sso.example.test", status.text)
            self.assertNotIn("redis", status.text)
        with self._env(AUTH_PROVIDERS="sso", AUTH_PASSWORD_LOGIN_ENABLED="false", AUTH_SSO_LABEL="统一入口", AUTH_ACCOUNT_LABEL="登录名"):
            hidden = self.client.get("/api/v1/auth/providers").json()
            self.assertEqual([item["kind"] for item in hidden["providers"]], ["redirect"])
            self.assertEqual(hidden["providers"][0]["label"], "统一入口")
            blocked = self.client.post("/api/v1/auth/password/login", json={"account": "ada", "password": "secret-password"})
            self.assertEqual(blocked.status_code, 404)
            self.assertNotIn("secret-password", blocked.text)
        with self._env(AUTH_PROVIDERS="password,sso", AUTH_REGISTRATION="invite"):
            both = self.client.get("/api/v1/auth/providers").json()
            self.assertEqual([item["kind"] for item in both["providers"]], ["password", "redirect", "credentials"])
            self.assertTrue(both["registration_open"])
            self.assertTrue(both["bootstrap_required"])
            self.assertFalse(both["requires_invite"])

    def test_disabled_sso_profile_is_not_given_a_session(self):
        with self._env():
            self.upstream.payload = {"success": True, "data": {"user": {"user_id": "sso-user-4", "nickname": "Cy"}}}
            first = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET})
            self.assertEqual(first.status_code, 200, first.text)
            with get_connection() as conn:
                conn.execute("UPDATE users SET status = 'disabled' WHERE user_id = ?", ("sso-user-4",))
            self.upstream.requests.clear()
            blocked = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET})
            self.assertEqual(blocked.status_code, 403)
            self.assertNotIn("access_token", blocked.text)

    def test_public_providers_helper_omits_secret_material(self):
        with self._env(AUTH_PROVIDERS="sso,password"):
            payload = public_providers()
        encoded = json.dumps(payload)
        self.assertFalse(_SSO_SECRET in encoded)
        self.assertNotIn("api_secret", encoded)
        self.assertNotIn("origin", encoded)
        self.assertNotIn("sso.example.test", encoded)

    def test_malformed_exchange_ticket_is_not_echoed(self):
        leaked = "LEAKED-TICKET"
        with self._env():
            response = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": leaked})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["detail"], "登录信息格式无效")
        self.assertNotIn(leaked, response.text)
        self.assertEqual(self.upstream.requests, [])

    def test_tampered_or_missing_state_is_rejected_before_upstream(self):
        with self._env():
            started = self.client.get("/api/v1/auth/sso/start")
            self.assertEqual(started.status_code, 200, started.text)
            state = started.json()["state"]
            state_id, signature = state.split(".", 1)
            flipped = ("A" if not signature.startswith("A") else "B") + signature[1:]
            self.upstream.requests.clear()
            tampered = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET, "state": state_id + "." + flipped})
            self.assertEqual(tampered.status_code, 400)
            self.assertEqual(tampered.json()["detail"], "SSO state 无效")
            self.assertEqual(self.upstream.requests, [])
            _redis_client().delete("luma:sso:state:" + state_id)
            missing = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET, "state": state})
            self.assertEqual(missing.status_code, 400)
            self.assertEqual(missing.json()["detail"], "SSO state 无效或已使用")
            self.assertEqual(self.upstream.requests, [])

    def test_upstream_timeout_and_5xx_map_to_502_without_locking_or_leaking_password(self):
        password = "upstream-secret-password"
        with self._env():
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            root = logging.getLogger()
            root.addHandler(handler)
            try:
                self.upstream.error = TimeoutError("upstream timed out")
                timed_out = self.client.post("/api/v1/auth/password/login", json={"account": "ada", "password": password})
                self.assertEqual(timed_out.status_code, 502)
                self.assertEqual(timed_out.json()["detail"], "登录服务暂时不可用")
                self.assertNotIn(password, timed_out.text)
                self.upstream.error = None
                self.upstream.http_status = 500
                self.upstream.error_body = password.encode()
                for _ in range(5):
                    failed = self.client.post("/api/v1/auth/password/login", json={"account": "ada", "password": password})
                    self.assertEqual(failed.status_code, 502)
                    self.assertNotIn(password, failed.text)
                self.upstream.http_status = 401
                self.upstream.error_body = None
                still_open = self.client.post("/api/v1/auth/password/login", json={"account": "ada", "password": password})
                self.assertEqual(still_open.status_code, 401)
                self.assertNotIn(password, still_open.text)
            finally:
                root.removeHandler(handler)
        self.assertNotIn(password, stream.getvalue())

    def test_sso_session_ends_after_logout_all_or_version_bump(self):
        with self._env():
            exchanged = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET})
            self.assertEqual(exchanged.status_code, 200, exchanged.text)
            headers = {"Authorization": "Bearer " + exchanged.json()["access_token"]}
            self.assertEqual(self.client.get("/api/v1/auth/me", headers=headers).status_code, 200)
            self.assertEqual(self.client.post("/api/v1/auth/logout-all", headers=headers).status_code, 200)
            self.assertEqual(self.client.get("/api/v1/auth/me", headers=headers).status_code, 401)
            again = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET})
            self.assertEqual(again.status_code, 200, again.text)
            headers = {"Authorization": "Bearer " + again.json()["access_token"]}
            with get_connection() as conn:
                conn.execute("UPDATE users SET session_version = session_version + 1 WHERE user_id = ?", ("sso-user-1",))
            self.assertEqual(self.client.get("/api/v1/auth/me", headers=headers).status_code, 401)

    def test_local_username_matching_sso_user_id_stays_two_accounts(self):
        with self._env(AUTH_PROVIDERS="sso,password", AUTH_REGISTRATION="open"):
            auth.create_admin_account("sharedname", "Useful-test-password-849!")
            with get_connection() as conn:
                local = conn.execute("SELECT user_id, auth_provider FROM users WHERE username = ?", ("sharedname",)).fetchone()
            self.upstream.payload = {"success": True, "data": {"user": {"user_id": "sharedname", "nickname": "Pat"}}}
            exchanged = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET})
            self.assertEqual(exchanged.status_code, 200, exchanged.text)
            with get_connection() as conn:
                rows = list(conn.execute("SELECT user_id, username, auth_provider, role FROM users").fetchall())
            self.assertEqual(len(rows), 2)
            by_id = {row["user_id"]: row for row in rows}
            self.assertEqual(by_id[local["user_id"]]["auth_provider"], "password")
            self.assertEqual(by_id[local["user_id"]]["role"], "admin")
            self.assertEqual(by_id["sharedname"]["auth_provider"], "sso")
            self.assertNotEqual(by_id["sharedname"]["username"], "sharedname")
            signed_in = self.client.post("/api/v1/auth/login", json={"login": "sharedname", "password": "Useful-test-password-849!"})
            self.assertEqual(signed_in.status_code, 200, signed_in.text)
            self.assertEqual(signed_in.json()["user"]["user_id"], local["user_id"])
            sso_name = by_id["sharedname"]["username"]
            denied = self.client.post("/api/v1/auth/login", json={"login": sso_name, "password": "Useful-test-password-849!"})
            self.assertEqual(denied.status_code, 401)

    def test_sso_does_not_claim_local_admin_or_blank_provider(self):
        with self._env(AUTH_PROVIDERS="sso,password"):
            auth.create_admin_account("local_admin", "Useful-test-password-849!")
            with get_connection() as conn:
                admin_row = conn.execute(
                    "SELECT user_id FROM users WHERE username = ?", ("local_admin",),
                ).fetchone()
                conn.execute(
                    "INSERT INTO users(user_id,username,role,status,created_at,first_seen_at,last_seen_at) "
                    "VALUES (?,?, 'user','active',?,?,?)",
                    ("blank-user", "blank_user", "2026-10-08", "2026-10-08", "2026-10-08"),
                )
                conn.execute(
                    "INSERT INTO users(user_id,username,role,status,created_at,first_seen_at,last_seen_at) "
                    "VALUES (?,?, 'admin','active',?,?,?)",
                    ("blank-admin", "blank_admin", "2026-10-08", "2026-10-08", "2026-10-08"),
                )
            self.upstream.payload = {"success": True, "data": {"user": {"user_id": admin_row["user_id"], "nickname": "Boss"}}}
            claimed = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET})
            self.assertEqual(claimed.status_code, 409)
            self.assertEqual(claimed.json()["detail"], "该用户已绑定其他登录方式")
            with get_connection() as conn:
                same = conn.execute("SELECT role, auth_provider FROM users WHERE user_id = ?", (admin_row["user_id"],)).fetchone()
            self.assertEqual(same["role"], "admin")
            self.assertEqual(same["auth_provider"], "password")
            self.upstream.payload = {"success": True, "data": {"user": {"user_id": "blank-admin", "nickname": "Open"}}}
            admin_blank = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET})
            self.assertEqual(admin_blank.status_code, 409)
            self.assertEqual(admin_blank.json()["detail"], "该用户已绑定其他登录方式")
            self.upstream.payload = {"success": True, "data": {"user": {"user_id": "blank-user", "nickname": "Blank"}}}
            blank = self.client.post("/api/v1/auth/sso/exchange", json={"ticket": _TICKET})
            self.assertEqual(blank.status_code, 409)
            self.assertEqual(blank.json()["detail"], "该账号尚未绑定登录方式")
            denied = self.client.post("/api/v1/auth/login", json={"login": "blank_user", "password": "Useful-test-password-849!"})
            self.assertEqual(denied.status_code, 409)
            self.assertEqual(denied.json()["detail"], "该账号尚未绑定登录方式")
            self.assertNotIn("access_token", denied.text)
            with get_connection() as conn:
                rows = {
                    row["user_id"]: row["auth_provider"]
                    for row in conn.execute("SELECT user_id, auth_provider FROM users WHERE user_id IN ('blank-user','blank-admin')")
                }
            self.assertIsNone(rows["blank-user"])
            self.assertIsNone(rows["blank-admin"])

    def test_admin_matches_sso_job_number_local_role_and_rejects_others(self):
        from fastapi.testclient import TestClient

        from app import admin

        now = "2026-10-08T00:00:00+00:00"
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO users(user_id,username,role,status,created_at,first_seen_at,last_seen_at,auth_provider,job_number,profile) "
                "VALUES (?,?, 'user','active',?,?,?,'sso',?,?::jsonb)",
                ("sso-job", "sso_job_user", now, now, now, "10086", "{}"),
            )
            conn.execute(
                "INSERT INTO users(user_id,username,role,status,created_at,first_seen_at,last_seen_at,auth_provider,profile) "
                "VALUES (?,?, 'user','active',?,?,?,'sso',?::jsonb)",
                ("sso-profile", "sso_profile_user", now, now, now, json.dumps({"ext": {"job_number": "10087"}})),
            )
            conn.execute(
                "INSERT INTO users(user_id,username,role,status,created_at,first_seen_at,last_seen_at,auth_provider) "
                "VALUES (?,?, 'admin','active',?,?,?,'password')",
                ("local-admin", "local_admin_user", now, now, now),
            )
            conn.execute(
                "INSERT INTO users(user_id,username,role,status,created_at,first_seen_at,last_seen_at,auth_provider,job_number) "
                "VALUES (?,?, 'user','active',?,?,?,'sso',?)",
                ("sso-other", "sso_other_user", now, now, now, "999"),
            )
        application = FastAPI()
        application.include_router(admin.router)
        client = TestClient(application)
        self.addCleanup(client.close)

        def me(user):
            with patch.dict(os.environ, {"ADMIN_USER_IDS": "10086,10087"}), patch.object(admin, "current_user", return_value=user):
                return client.get("/api/admin/me")

        sso_admin = me({"user_id": "sso-job", "role": "user", "username": "sso_job_user"})
        self.assertEqual(sso_admin.status_code, 200, sso_admin.text)
        self.assertTrue(sso_admin.json()["is_admin"])
        profile_admin = me({"user_id": "sso-profile", "role": "user", "username": "sso_profile_user"})
        self.assertTrue(profile_admin.json()["is_admin"])
        local_admin = me({"user_id": "local-admin", "role": "admin", "username": "local_admin_user"})
        self.assertTrue(local_admin.json()["is_admin"])
        other = me({"user_id": "sso-other", "role": "user", "username": "sso_other_user"})
        self.assertFalse(other.json()["is_admin"])
        with patch.dict(os.environ, {"ADMIN_USER_IDS": "10086,10087"}), patch.object(
            admin, "current_user", return_value={"user_id": "sso-other", "role": "user", "username": "sso_other_user"},
        ):
            self.assertEqual(client.get("/api/admin/audit").status_code, 403)
        with patch.dict(os.environ, {"ADMIN_USER_IDS": "10086"}), patch.object(
            admin, "current_user", return_value={"user_id": "sso-job", "role": "user", "username": "sso_job_user"},
        ):
            self.assertEqual(client.get("/api/admin/audit").status_code, 200)

    def test_auth_provider_backfill_is_idempotent(self):
        import importlib

        migration = importlib.import_module("migrations.versions.0019_auth_providers")
        now = "2026-10-08T00:00:00+00:00"

        def insert(user_id, username, password_hash=None, job_number=None, provider=None, profile="{}"):
            with get_connection() as conn:
                conn.execute(
                    "INSERT INTO users(user_id,username,password_hash,job_number,auth_provider,profile,role,status,created_at,first_seen_at,last_seen_at) "
                    "VALUES (?,?,?,?,?,?::jsonb,'user','active',?,?,?)",
                    (user_id, username, password_hash, job_number, provider, profile, now, now, now),
                )

        insert("legacy_keep", "legacy_keep")
        insert("legacy_job", "legacy_job", job_number="55")
        insert("legacy_prof", "legacy_prof", profile=json.dumps({"job_number": "77"}))
        insert("portal-user", "portal_user")
        insert("pw-user", "pw_user", password_hash="hash")
        insert("keep-sso", "keep_sso", provider="sso")
        insert("keep-pw", "keep_pw", password_hash="hash", provider="password")
        with get_connection() as conn:
            conn.execute(migration.BACKFILL_PASSWORD_SQL)
            conn.execute(migration.BACKFILL_SSO_SQL)
            conn.execute(migration.BACKFILL_PASSWORD_SQL)
            conn.execute(migration.BACKFILL_SSO_SQL)
            rows = {
                row["user_id"]: row["auth_provider"]
                for row in conn.execute("SELECT user_id, auth_provider FROM users")
            }
        self.assertIsNone(rows["legacy_keep"])
        self.assertEqual(rows["legacy_job"], "sso")
        self.assertEqual(rows["legacy_prof"], "sso")
        self.assertEqual(rows["portal-user"], "sso")
        self.assertEqual(rows["pw-user"], "password")
        self.assertEqual(rows["keep-sso"], "sso")
        self.assertEqual(rows["keep-pw"], "password")


if __name__ == "__main__":
    unittest.main()
