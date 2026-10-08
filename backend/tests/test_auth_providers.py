"""Login provider registry: local passwords and an optional SSO provider."""

import base64
import hashlib
import hmac
import io
import json
import os
import unittest
from email.message import Message
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

    def __call__(self, request, timeout=8):
        full = str(getattr(request, "full_url", ""))
        if not full.startswith(_SSO_HOST + "/"):
            raise AssertionError("upstream url was not the configured test host")
        self.requests.append(request)
        if self.http_status:
            raise HTTPError(full, self.http_status, "no", Message(), io.BytesIO(b"{}"))
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
            "SSO_SYSTEM_CODE": "",
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
        with patch.dict(os.environ, {"AUTH_PROVIDERS": ""}):
            self.assertEqual(enabled_provider_names(), ["password"])
        with patch.dict(os.environ, {"AUTH_PROVIDERS": " sso, password , sso "}):
            self.assertEqual(enabled_provider_names(), ["sso", "password"])
        with patch.dict(os.environ, {"AUTH_PROVIDERS": "ldap"}):
            with self.assertRaises(RuntimeError) as raised:
                enabled_provider_names()
        self.assertIn("password, sso", str(raised.exception))

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
            if redirect.get("origin") != _SSO_HOST:
                self.fail("provider origin was not the configured test host")
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


if __name__ == "__main__":
    unittest.main()
