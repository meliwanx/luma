"""PostgreSQL test database bootstrap.

Import this module before importing :mod:`app`.  A unique schema is used for
each test process so the suite can run against the shared ``luma_test``
database without touching another developer's tables.
"""

from __future__ import annotations

import atexit
import os
import random
import re
import string
from pathlib import Path
from typing import Dict, List

import psycopg2
from psycopg2 import sql


_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_ENV_FILE = _BACKEND_ROOT / ".env"


def _read_env(path: Path) -> Dict[str, str]:
    """Read the small dotenv format used by the backend without logging values."""

    values: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


_dotenv = _read_env(_ENV_FILE)


def _setting(name: str, default: str = "") -> str:
    # Explicit process values are useful in CI; otherwise use backend/.env.
    return os.environ.get(name, _dotenv.get(name, default))


_test_db_name = os.environ.get("TEST_DB_NAME", "luma_test").strip()
if not _test_db_name.startswith("luma_test"):
    raise SystemExit("refusing to run PostgreSQL tests outside a luma_test* database")


def _connection_settings() -> Dict[str, object]:
    settings: Dict[str, object] = {
        "host": _setting("DB_HOST", "127.0.0.1"),
        "port": int(_setting("DB_PORT", "5432")),
        "user": _setting("DB_USER", "postgres"),
        "password": _setting("DB_PASSWORD", ""),
        "dbname": _test_db_name,
        "connect_timeout": int(_setting("DB_CONNECT_TIMEOUT", "8")),
    }
    return settings


_schema_token = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
TEST_SCHEMA = "t_{}_{}".format(os.getpid(), _schema_token)
if not re.fullmatch(r"t_[0-9]+_[a-z0-9]{8}", TEST_SCHEMA):
    raise RuntimeError("generated invalid PostgreSQL test schema name")


def _create_schema() -> None:
    connection = psycopg2.connect(**_connection_settings())
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(TEST_SCHEMA)))
    finally:
        connection.close()


# Set all database values before app.db is imported.
os.environ["DB_NAME"] = _test_db_name
os.environ["DB_SCHEMA"] = TEST_SCHEMA
os.environ["DB_HOST"] = str(_connection_settings()["host"])
os.environ["DB_PORT"] = str(_connection_settings()["port"])
os.environ["DB_USER"] = str(_connection_settings()["user"])
os.environ["DB_PASSWORD"] = str(_connection_settings()["password"])
os.environ["AUTH_REQUIRED"] = "true"
os.environ["AUTH_SESSION_SECRET"] = "unit-test-session-secret-with-32-plus-characters"
os.environ["LUMA_PROVIDER"] = "local"
os.environ["REDIS_DB"] = "15"

try:
    _create_schema()
except Exception:
    # Do not let psycopg2 include host/user details from backend/.env in test
    # output when the local test database is unavailable.
    raise SystemExit("unable to initialize PostgreSQL test database") from None

from app.db import close_pool, ensure_db, get_connection  # noqa: E402


def reset_tables() -> None:
    """Reset rows outside TestClient lifespans, preserving migration state."""

    from app.services.memory import drain_memory_workers

    # A response does not own detached summary/extraction callbacks. Wait for
    # their writes before taking TRUNCATE's AccessExclusive locks, without
    # occupying one of the small pool's connections during the wait.
    drain_memory_workers()
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname = current_schema() AND tablename <> ?",
            ("alembic_version",),
        ).fetchall()
        names: List[str] = []
        for row in rows:
            name = row["tablename"] if isinstance(row, dict) else row[0]
            # pg_tables names cannot contain an unescaped quote, but quoting
            # still keeps this identifier-safe if a future table is unusual.
            names.append('"{}"'.format(str(name).replace('"', '""')))
        if names:
            connection.execute("TRUNCATE TABLE " + ", ".join(names) + " RESTART IDENTITY CASCADE")


def _cleanup() -> None:
    try:
        close_pool()
    except Exception:
        pass
    try:
        connection = psycopg2.connect(**_connection_settings())
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(TEST_SCHEMA)))
        finally:
            connection.close()
    except Exception:
        # Cleanup must not mask a test failure or interpreter shutdown error.
        pass


atexit.register(_cleanup)

# Run migrations only after the cleanup hook is installed.  If Alembic fails
# during test startup, the temporary schema is still removed at interpreter
# exit instead of leaking into luma_test.
try:
    ensure_db()
except Exception:
    # Keep credentials and connection details out of an import traceback.
    raise RuntimeError("unable to migrate PostgreSQL test schema") from None
