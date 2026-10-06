"""Create the assistant database and idempotently initialize its schema.

This script never drops a database or table. It connects to the PostgreSQL
maintenance database only long enough to create ``DB_NAME`` when missing, then
lets the application storage adapter run the Alembic migrations.

Run from the repository root with ``python backend/scripts/bootstrap_storage.py``
after installing ``backend/requirements.txt``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db import ensure_db, postgres_settings, storage_status  # noqa: E402


def main() -> int:
    try:
        import psycopg2
        from psycopg2 import sql
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise SystemExit("Install backend/requirements.txt first") from exc

    settings = postgres_settings()
    database_name = settings["dbname"]
    admin_settings = dict(settings)
    admin_settings["dbname"] = os.getenv("DB_ADMIN_NAME", "postgres")
    admin = psycopg2.connect(**admin_settings)
    created = False
    try:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,))
            if cursor.fetchone() is None:
                cursor.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(database_name)))
                created = True
    finally:
        admin.close()

    ensure_db()
    print({"database": database_name, "created": created, "storage": storage_status()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
