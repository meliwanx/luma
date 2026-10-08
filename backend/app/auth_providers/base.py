"""Login provider interface shared by local accounts and optional SSO."""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter


class AuthProvider(Protocol):
    """A way to prove identity. Session issuance stays in the shared auth module."""

    name: str

    def public_config(self) -> dict[str, Any]:
        """Return login-page settings. Never include secrets."""

    def routes(self) -> APIRouter:
        """Return the routes this provider mounts under /api/v1."""
