"""Application configuration and shared constants."""

from __future__ import annotations

import os
from pathlib import Path

from .widgets import SYSTEM_PROMPT


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def cors_origins() -> list[str]:
    return [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://127.0.0.1:8787,http://localhost:8787,http://127.0.0.1:5173,http://localhost:5173",
        ).split(",")
        if origin.strip()
    ]


# Keep environment-derived values in one module so application assembly and
# routers do not each parse deployment settings differently.
CORS_ORIGINS = cors_origins()
THREADPOOL_SIZE = max(1, env_int("THREADPOOL_SIZE", 64))


APP_VERSION = "0.1.0"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_WEB_ROOT = PROJECT_ROOT / "web"
REACT_WEB_ROOT = PROJECT_ROOT / "apps" / "web" / "dist"
WEB_ROOT = REACT_WEB_ROOT if (REACT_WEB_ROOT / "index.html").exists() else LEGACY_WEB_ROOT
