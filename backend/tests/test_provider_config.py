"""Generic provider settings and legacy environment compatibility."""

import os
import unittest
from unittest.mock import patch

from app import provider


class ProviderConfigurationTests(unittest.TestCase):
    def config(self, values):
        with patch.dict(os.environ, values, clear=True), patch.object(provider, "_read_dotenv"):
            return provider.get_config()

    def test_generic_names_override_legacy_aliases(self):
        config = self.config({
            "LLM_BASE_URL": " https://generic.example/v1/ ",
            "LLM_API_KEY": " generic-fixture ",
            "LLM_MODEL": "generic-model",
            "LLM_TIMEOUT_SECONDS": "19",
            "MIMO_API_BASE_URL": "https://legacy.example/v1",
            "MIMO_API_KEY": "legacy-fixture",
            "MIMO_MODEL": "legacy-model",
            "MIMO_TIMEOUT_SECONDS": "11",
        })
        self.assertEqual(config.base_url, "https://generic.example/v1")
        self.assertEqual(config.api_key, "generic-fixture")
        self.assertEqual(config.model, "generic-model")
        self.assertEqual(config.timeout_seconds, 19)

    def test_legacy_aliases_remain_readable(self):
        config = self.config({
            "MIMO_API_BASE_URL": "https://legacy.example/v1/",
            "MIMO_API_KEY": "legacy-fixture",
            "MIMO_MODEL": "legacy-model",
            "MIMO_TIMEOUT_SECONDS": "11",
        })
        self.assertEqual(config.base_url, "https://legacy.example/v1")
        self.assertEqual(config.api_key, "legacy-fixture")
        self.assertEqual(config.model, "legacy-model")
        self.assertEqual(config.timeout_seconds, 11)

    def test_explicit_empty_key_disables_legacy_key(self):
        config = self.config({"LLM_API_KEY": "", "MIMO_API_KEY": "legacy-fixture"})
        self.assertFalse(config.configured)

    def test_defaults_are_generic_and_timeout_is_bounded(self):
        config = self.config({})
        self.assertEqual(config.model, "your-model-name")
        self.assertEqual(config.base_url, "https://api.example.com/v1")
        self.assertFalse(config.configured)
        self.assertEqual(self.config({"LLM_TIMEOUT_SECONDS": "invalid"}).timeout_seconds, 45)
        self.assertEqual(self.config({"LLM_TIMEOUT_SECONDS": "0"}).timeout_seconds, 1)
        self.assertEqual(self.config({"LLM_TIMEOUT_SECONDS": "999"}).timeout_seconds, 120)
