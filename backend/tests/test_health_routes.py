import json
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.routers import health


class HealthRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(health.router)
        cls.client = TestClient(app)

    def test_health_does_not_include_storage_connection_details(self):
        with patch.dict(os.environ, {"REDIS_HOST": "redis.internal"}, clear=False), patch.object(
            health, "ping_database", return_value=True
        ), patch.object(health, "ping_redis", return_value=True):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["database"], {"reachable": True})
        self.assertEqual(payload["redis"], {"configured": True, "reachable": True})
        encoded = json.dumps(payload)
        for key in ("host", "port", "dbname", "backend"):
            self.assertNotIn(key, payload["database"])
        for value in ('"host"', '"port"', '"dbname"', '"backend"'):
            self.assertNotIn(value, encoded)

    def test_status_routes_require_authentication(self):
        with patch.dict(os.environ, {"AUTH_REQUIRED": "true"}, clear=False):
            system = self.client.get("/api/v1/system/status")
            provider = self.client.get("/api/v1/provider/status")

        self.assertEqual(system.status_code, 401)
        self.assertEqual(provider.status_code, 401)

    def test_authenticated_system_status_redacts_storage_and_provider_urls(self):
        storage = {
            "backend": "postgres",
            "host": "db.internal",
            "port": 5432,
            "database": "luma_prod",
        }
        provider = {
            "provider": "llm",
            "configured": True,
            "base_url": "https://provider.internal/v1",
            "model": "your-model-name",
        }
        with patch.dict(os.environ, {"AUTH_REQUIRED": "true"}, clear=False), patch.object(
            auth, "current_user", return_value={"user_id": "u-health"}
        ), patch.object(health, "ping_database", return_value=True), patch.object(
            health, "ping_redis", return_value=False
        ), patch.object(health, "storage_status", return_value=storage), patch.object(
            health, "provider_status", return_value=provider
        ), patch.object(health, "agent_runtime_status", return_value={"region": "ap-shanghai"}):
            system = self.client.get("/api/v1/system/status")
            provider_response = self.client.get("/api/v1/provider/status")

        self.assertEqual(system.status_code, 200)
        system_payload = system.json()
        self.assertEqual(system_payload["storage"], {"backend": "postgres", "reachable": True})
        self.assertEqual(
            system_payload["provider"],
            {"provider": "llm", "configured": True, "model": "your-model-name"},
        )
        self.assertNotIn("db.internal", json.dumps(system_payload))
        self.assertNotIn("luma_prod", json.dumps(system_payload))
        self.assertNotIn("https://provider.internal/v1", json.dumps(system_payload))

        self.assertEqual(provider_response.status_code, 200)
        self.assertEqual(
            provider_response.json(),
            {"provider": "llm", "configured": True, "model": "your-model-name"},
        )


if __name__ == "__main__":
    unittest.main()
