"""Cookie/bearer session lifetime, isolation and revocation regressions."""

import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from tests.pg import reset_tables
from tests.test_accounts import BOOTSTRAP_TOKEN, FakeRedis

from fastapi.testclient import TestClient

from app import auth, main, telemetry
from app.db import CacheUnavailable


class AuthSessionTests(unittest.TestCase):
    password = "Session-test-password-592!"
    bootstrap_token = BOOTSTRAP_TOKEN

    def setUp(self):
        reset_tables()
        self.redis = FakeRedis()
        self.redis.install(self)
        for item in (
            patch.dict(os.environ, {"AUTH_REQUIRED": "true", "AUTH_SESSION_SECRET": "synthetic-session-test-secret-" + "z" * 40,
                "AUTH_COOKIE_SECURE": "false", "AUTH_REGISTRATION": "open", "AUTH_BOOTSTRAP_TOKEN": self.bootstrap_token,
                "AUTH_SESSION_TTL_DAYS": "7", "AUTH_SESSION_MAX_DAYS": "30"}),
            patch.object(auth, "_read_dotenv"), patch.object(telemetry, "_requests", []),
            patch.object(telemetry, "_stats", {}), patch.object(telemetry, "_devices", {}),
            patch.object(telemetry, "_users", {}), patch.object(telemetry, "_user_seen", {}),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        response = self.client.post("/api/v1/auth/register", json={"username": "session_user", "password": self.password,
            "bootstrap_token": self.bootstrap_token})
        self.assertEqual(response.status_code, 200, response.text)
        self.user = response.json()["user"]
        self.token = response.json()["access_token"]
        self.digest = auth._token_hash(self.token)
        self.key = auth._session_key(self.digest, is_hash=True)
        self.headers = {"Authorization": "Bearer " + self.token}

    def payload(self):
        return json.loads(self.redis.store[self.key])

    def seed(self, **changes):
        payload = self.payload()
        payload.update(changes)
        self.redis.store[self.key] = json.dumps(payload)
        return payload

    def test_tokens_and_passwords_never_appear_in_session_storage_or_device_list(self):
        self.assertNotIn(self.token, json.dumps(self.redis.store))
        self.assertNotIn(self.password, json.dumps(self.redis.store))
        devices = self.client.get("/api/v1/auth/sessions", headers=self.headers)
        self.assertEqual(devices.status_code, 200)
        self.assertEqual(devices.json()[0]["id"], self.digest)
        self.assertNotIn(self.token, devices.text)
        self.assertEqual(self.payload()["absolute_expires_at"] - self.payload()["created_at"], 30 * 86400)

    def test_sliding_renewal_refreshes_redis_and_cookie_without_exceeding_absolute_limit(self):
        now = int(time.time())
        self.seed(created_at=now - 6 * 86400, expires_at=now + 1000,
                  last_used_at=now - 120, absolute_expires_at=now + 12 * 3600)
        response = self.client.get("/api/v1/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.payload()["expires_at"], now + 12 * 3600)
        self.assertLessEqual(self.redis.ttls[self.key], 12 * 3600)
        cookie = response.headers["set-cookie"]
        max_age = int(cookie.split("Max-Age=", 1)[1].split(";", 1)[0])
        self.assertGreater(max_age, 11 * 3600)
        self.assertLessEqual(max_age, 12 * 3600)
        self.assertIn("HttpOnly", cookie)

    def test_bearer_renewal_does_not_issue_cookie_and_logout_cookie_wins(self):
        now = int(time.time())
        self.seed(expires_at=now + 100, last_used_at=now - 120)
        response = self.client.get("/api/v1/auth/me", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("set-cookie", response.headers)
        self.seed(expires_at=now + 100, last_used_at=now - 120)
        response = self.client.post("/api/v1/auth/logout")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.assertNotIn(self.key, self.redis.store)

    def test_recent_activity_renews_sliding_lifetime_before_half_ttl(self):
        now = int(time.time())
        self.seed(created_at=now - 61, expires_at=now + 7 * 86400 - 61,
                  last_used_at=now - 61, absolute_expires_at=now + 30 * 86400 - 61)
        response = self.client.get("/api/v1/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(self.payload()["expires_at"], now + 7 * 86400)
        self.assertIn("set-cookie", response.headers)

    def test_absolute_and_sliding_expiration_remove_session_and_index(self):
        now = int(time.time())
        self.seed(absolute_expires_at=now - 1)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self.headers).status_code, 401)
        self.assertNotIn(self.key, self.redis.store)
        self.assertNotIn(self.digest, self.redis.sets[auth._session_index_key(self.user["user_id"])])

    def test_renewal_cannot_resurrect_a_concurrently_revoked_session(self):
        now = int(time.time())
        self.seed(expires_at=now + 100, last_used_at=now - 120)
        def revoke_before_write(key, value, ttl):
            self.redis.delete(key)
            self.redis.srem(auth._session_index_key(self.user["user_id"]), self.digest)
            return False
        with patch.object(auth, "cache_set_existing_strict", side_effect=revoke_before_write):
            response = self.client.get("/api/v1/auth/me", headers=self.headers)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(self.key, self.redis.store)
        self.assertNotIn(self.digest, self.redis.sets[auth._session_index_key(self.user["user_id"])])

    def test_bearer_wins_over_another_users_cookie_even_when_malformed(self):
        second = self.client.post("/api/v1/auth/register", json={"username": "session_other", "password": self.password})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self.headers).json()["user_id"], self.user["user_id"])
        self.assertEqual(self.client.get("/api/v1/auth/me", headers={"Authorization": "Bearer bad"}).status_code, 401)
        self.client.post("/api/v1/auth/logout", headers=self.headers)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self.headers).status_code, 401)
        other_headers = {"Authorization": "Bearer " + second.json()["access_token"]}
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=other_headers).status_code, 200)

    def test_logout_all_revokes_unindexed_sessions_through_database_generation(self):
        second = self.client.post("/api/v1/auth/login", json={"login": "session_user", "password": self.password})
        second_headers = {"Authorization": "Bearer " + second.json()["access_token"]}
        self.redis.sets[auth._session_index_key(self.user["user_id"])].discard(self.digest)
        self.assertEqual(self.client.post("/api/v1/auth/logout-all", headers=second_headers).status_code, 200)
        for headers in (self.headers, second_headers):
            self.assertEqual(self.client.get("/api/v1/auth/me", headers=headers).status_code, 401)

    def test_prunes_corrupt_and_expired_device_index_entries(self):
        index = auth._session_index_key(self.user["user_id"])
        self.redis.sadd(index, "malformed", "f" * 64)
        response = self.client.get("/api/v1/auth/sessions", headers=self.headers)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(self.redis.sets[index], {self.digest})

    def test_redis_unavailability_fails_closed(self):
        with patch.object(auth, "cache_get_strict", side_effect=CacheUnavailable()):
            self.assertEqual(self.client.get("/api/v1/auth/me", headers=self.headers).status_code, 503)

    def test_custom_sliding_ttl_and_secure_cookie(self):
        with patch.dict(os.environ, {"AUTH_SESSION_TTL_DAYS": "2", "AUTH_SESSION_MAX_DAYS": "3", "AUTH_COOKIE_SECURE": "true"}):
            response = self.client.post("/api/v1/auth/login", json={"login": "session_user", "password": self.password})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["expires_in"], 2 * 86400)
        self.assertIn("Secure", response.headers["set-cookie"])
        data = json.loads(self.redis.store[auth._session_key(response.json()["access_token"])])
        self.assertEqual(data["absolute_expires_at"] - data["created_at"], 3 * 86400)

    def test_password_rotation_serializes_kept_session_reads_until_database_commit(self):
        published = threading.Event()
        allow_commit = threading.Event()
        reader_started = threading.Event()
        results = {}
        failures = []
        def publish_then_pause(key, value, ttl):
            result = self.redis.set_existing(key, value, ttl)
            if json.loads(value).get("session_version") == 1 and not published.is_set():
                published.set()
                if not allow_commit.wait(10):
                    raise AssertionError("credential change was not released")
            return result
        def track_read(key):
            value = self.redis.get(key)
            if published.is_set() and not allow_commit.is_set():
                reader_started.set()
            return value
        def rotate():
            try:
                client = TestClient(main.app)
                try:
                    results["change"] = client.post("/api/v1/account/password", json={"current_password": self.password,
                        "new_password": "New-session-password-541!"}, headers=self.headers)
                finally:
                    client.close()
            except BaseException as exc:
                failures.append(exc)
        def read():
            try:
                # Lifespan-free: concurrent test clients must not start extra
                # service workers or database migrations.
                client = TestClient(main.app)
                try:
                    results["read"] = client.get("/api/v1/auth/me", headers=self.headers)
                finally:
                    client.close()
            except BaseException as exc:
                failures.append(exc)
        # Both operations share the Redis fake while real PG row locks impose
        # the transaction ordering. Only two borrowed DB connections are used.
        with patch.object(auth, "cache_set_existing_strict", side_effect=publish_then_pause), patch.object(auth, "cache_get_strict", side_effect=track_read):
            writer = threading.Thread(target=rotate)
            reader = threading.Thread(target=read)
            writer.start()
            try:
                self.assertTrue(published.wait(10))
                reader.start()
                self.assertTrue(reader_started.wait(10))
            finally:
                allow_commit.set()
                writer.join(15)
                if reader.ident:
                    reader.join(15)
            self.assertFalse(writer.is_alive())
            self.assertFalse(reader.is_alive())
        self.assertFalse(failures, failures)
        self.assertEqual(results["change"].status_code, 200, results["change"].text)
        self.assertEqual(results["read"].status_code, 200, results["read"].text)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self.headers).status_code, 200)
