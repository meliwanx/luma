"""Alembic environment for the assistant's PostgreSQL-only storage.

Connection details are read from :func:`app.db.postgres_settings`; no
credentials or connection strings are stored in alembic.ini.
"""
from __future__ import annotations

import logging.config
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL

# ``alembic`` can be invoked from any working directory; make the backend
# package importable even when ``prepend_sys_path`` is not honored by an older
# Alembic entry point.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import postgres_settings


config = context.config

if config.config_file_name is not None:
    # alembic.ini contains a normal logging section but no database URL.
    logging.config.fileConfig(config.config_file_name, disable_existing_loggers=False)


_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _connection_url() -> URL:
    """Build a SQLAlchemy URL from the application's PostgreSQL settings."""

    settings: Dict[str, Any] = dict(postgres_settings())
    schema = settings.pop("schema", None) or settings.pop("db_schema", None) or os.getenv("DB_SCHEMA", "")
    if schema:
        schema = str(schema).strip()
        if not _SCHEMA_RE.fullmatch(schema):
            raise ValueError("DB_SCHEMA must match ^[a-z_][a-z0-9_]{0,62}$")

    query: Dict[str, str] = {}
    sslmode = settings.pop("sslmode", None) or os.getenv("DB_SSLMODE", "prefer")
    if sslmode:
        query["sslmode"] = str(sslmode)
    for key in ("connect_timeout", "application_name"):
        value = settings.pop(key, None)
        if value is not None:
            query[key] = str(value)
    options = settings.pop("options", None)
    if options:
        query["options"] = str(options)
    elif schema:
        query["options"] = "-c search_path={}".format(schema)

    return URL.create(
        "postgresql+psycopg2",
        username=settings.get("user"),
        password=settings.get("password"),
        host=settings.get("host"),
        port=settings.get("port"),
        database=settings.get("dbname") or settings.get("database"),
        query=query,
    )


def run_migrations_offline() -> None:
    """Run migrations without a live connection (SQL script generation)."""

    context.configure(
        url=str(_connection_url()),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against PostgreSQL.

    ``ensure_db`` injects a SQLAlchemy connection after taking the startup
    advisory lock.  Reusing it is required: opening another session here
    would block on that lock.  The CLI path creates a short-lived engine from
    the application settings instead.
    """

    injected = config.attributes.get("connection")
    if injected is not None:
        context.configure(connection=injected, target_metadata=None, compare_type=False)
        with context.begin_transaction():
            context.run_migrations()
        return

    # Pass the SQLAlchemy URL object directly.  Converting it to a string first
    # can misparse passwords containing URL-reserved characters.
    connectable = create_engine(_connection_url(), poolclass=pool.NullPool, future=True)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None, compare_type=False)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
