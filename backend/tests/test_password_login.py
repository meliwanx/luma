"""Local password hashing and credential-payload privacy regressions."""

import unittest
from unittest.mock import patch

from tests import pg  # noqa: F401: initialize the isolated database first
from app import auth


class PasswordHashTests(unittest.TestCase):
    password = "Independent-password-test-382!"

    def test_argon2id_when_available_and_scrypt_when_optional_library_is_missing(self):
        encoded = auth.hash_password(self.password)
        expected_prefix = "$argon2id$" if auth.PasswordHasher is not None else "scrypt$"
        self.assertTrue(encoded.startswith(expected_prefix))
        self.assertTrue(auth.verify_password(self.password, encoded))
        self.assertFalse(auth.verify_password("Wrong-password-729!", encoded))
        with patch.object(auth, "PasswordHasher", None):
            fallback = auth.hash_password(self.password)
        self.assertTrue(fallback.startswith("scrypt$32768$8$1$"))
        self.assertTrue(auth.verify_password(self.password, fallback))

    def test_malformed_hashes_are_rejected_without_echoing_credentials(self):
        for value in (None, "", "invalid", "scrypt$1$8$1$bad$bad", "scrypt$32768$8$1$bad$bad", "$argon2id$bad"):
            with self.subTest(hash_kind=str(value)[:8]):
                self.assertFalse(auth.verify_password(self.password, value))

    def test_common_password_denylist_has_one_hundred_entries(self):
        self.assertEqual(len(auth._COMMON_PASSWORDS), 100)
