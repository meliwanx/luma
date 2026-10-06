"""PostgreSQL and Redis storage adapters.

The application uses a small DB-API facade instead of an ORM. SQL in the
callers keep ``?`` placeholders; the facade translates those to
psycopg2 placeholders and returns dictionary-like rows.
"""

from __future__ import annotations

import atexit
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


def _read_dotenv(path: Path) -> None:
    """Load simple environment entries without overwriting process values."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or name in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[name] = value


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
_read_dotenv(ENV_PATH)

DEFAULT_DB_NAME = "luma_assistant"
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_pool: Any = None
_pool_semaphore: Optional[threading.BoundedSemaphore] = None
_pool_lock = threading.RLock()

_redis_pool: Any = None
_redis_signature: Any = None
_redis_lock = threading.RLock()


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def redis_settings() -> dict[str, Any]:
    return {
        "host": os.getenv("REDIS_HOST", "127.0.0.1"),
        "port": _env_int("REDIS_PORT", 6379, 1),
        "password": os.getenv("REDIS_PASSWORD", ""),
        "db": _env_int("REDIS_DB", 0),
        "socket_connect_timeout": _env_int("REDIS_CONNECT_TIMEOUT", 2),
        "socket_timeout": _env_int("REDIS_TIMEOUT", 2),
        "decode_responses": True,
    }


def postgres_settings() -> dict[str, Any]:
    """Build psycopg2 settings from process environment.

    ``DB_SCHEMA`` is restricted to a simple identifier before it is put in the
    connection ``search_path`` option. This keeps a schema name from becoming
    an SQL injection vector while allowing isolated test schemas.
    """

    schema = os.getenv("DB_SCHEMA", "").strip()
    if schema and not _SCHEMA_RE.fullmatch(schema):
        raise ValueError("DB_SCHEMA must match ^[a-z_][a-z0-9_]{0,62}$")
    settings: dict[str, Any] = {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": _env_int("DB_PORT", 5432, 1),
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", ""),
        "dbname": os.getenv("DB_NAME", DEFAULT_DB_NAME),
        "connect_timeout": _env_int("DB_CONNECT_TIMEOUT", 8, 1),
        "application_name": os.getenv("DB_APPLICATION_NAME", "luma-assistant"),
        "sslmode": os.getenv("DB_SSLMODE", "prefer"),
    }
    if schema:
        settings["options"] = "-c search_path={}".format(schema)
    return settings


def _postgres_sql(sql: str, params: Any) -> str:
    """Translate repository placeholders for psycopg2."""

    if isinstance(params, dict):
        return re.sub(
            r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)",
            lambda match: "%({})s".format(match.group(1)),
            sql,
        )
    return sql.replace("?", "%s")


class PostgresConnection:
    """Small connection facade matching the subset used by application code."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @property
    def raw(self) -> Any:
        """Expose the DB-API connection to Alembic's env.py."""

        return self._connection

    @property
    def closed(self) -> bool:
        try:
            return bool(self._connection.closed)
        except Exception:
            return True

    def execute(self, sql: str, params: Any = ()) -> Any:
        cursor = self._connection.cursor(cursor_factory=_compat_cursor_factory())
        cursor.execute(_postgres_sql(sql, params), params)
        return cursor

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        # Explicitly closed facades are discarded by the pool return path.
        self._connection.close()


class _CompatRow(dict):
    """Dictionary row that also preserves the old integer-index access."""

    def __init__(self, row: Any) -> None:
        super().__init__(row)
        self._columns = tuple(row.keys())

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            key = self._columns[key]
        return super().__getitem__(key)


_compat_cursor_type: Any = None


def _compat_cursor_factory() -> Any:
    """Build the mapping cursor lazily so importing db.py needs no psycopg2."""

    global _compat_cursor_type
    if _compat_cursor_type is not None:
        return _compat_cursor_type
    from psycopg2.extras import RealDictCursor

    class CompatRealDictCursor(RealDictCursor):
        def fetchone(self) -> Any:
            row = super().fetchone()
            return _CompatRow(row) if row is not None else None

        def fetchmany(self, size: Any = None) -> Any:
            rows = super().fetchmany() if size is None else super().fetchmany(size)
            return [_CompatRow(row) for row in rows]

        def fetchall(self) -> Any:
            return [_CompatRow(row) for row in super().fetchall()]

        def __iter__(self) -> Any:
            while True:
                row = self.fetchone()
                if row is None:
                    return
                yield row

    _compat_cursor_type = CompatRealDictCursor
    return _compat_cursor_type


def _get_pool() -> Any:
    global _pool, _pool_semaphore
    with _pool_lock:
        if _pool is not None:
            return _pool

        settings = postgres_settings()
        if settings["user"] == "luma_dev" and settings["dbname"] == DEFAULT_DB_NAME:
            raise RuntimeError("开发账号不能连接生产库")
        try:
            from psycopg2.pool import ThreadedConnectionPool
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("PostgreSQL storage requires psycopg2-binary") from exc

        minimum = _env_int("DB_POOL_MIN", 1, 1)
        maximum = _env_int("DB_POOL_MAX", 5, 1)
        if maximum < minimum:
            raise ValueError("DB_POOL_MAX must be greater than or equal to DB_POOL_MIN")
        _pool = ThreadedConnectionPool(minimum, maximum, **settings)
        _pool_semaphore = threading.BoundedSemaphore(maximum)
        return _pool


def _is_operational_error(exc: BaseException) -> bool:
    try:
        from psycopg2 import OperationalError

        return isinstance(exc, OperationalError)
    except ImportError:  # pragma: no cover - dependency guard
        return False


def _connection_is_bad(connection: Any) -> bool:
    try:
        return bool(connection.closed)
    except Exception:
        return True


def _pool_timeout() -> float:
    try:
        return max(0.0, float(os.getenv("DB_POOL_TIMEOUT", "10")))
    except (TypeError, ValueError):
        return 10.0


@contextmanager
def get_connection() -> Iterator[PostgresConnection]:
    """Borrow one pooled connection for a short-lived transaction.

    A semaphore sits in front of psycopg2's pool so callers wait for an idle
    connection rather than receiving ``PoolError`` immediately when the pool
    is full. Broken connections are closed on return and never reused.
    """

    pool = _get_pool()
    semaphore = _pool_semaphore
    if semaphore is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("database pool semaphore is not initialized")
    if not semaphore.acquire(timeout=_pool_timeout()):
        raise TimeoutError("timed out waiting for a PostgreSQL connection")

    raw = None
    bad = False
    try:
        raw = pool.getconn()
        facade = PostgresConnection(raw)
        try:
            yield facade
            try:
                facade.commit()
            except Exception as exc:
                bad = _is_operational_error(exc) or _connection_is_bad(raw)
                raise
        except Exception as exc:
            if _is_operational_error(exc):
                bad = True
            try:
                facade.rollback()
            except Exception:
                bad = True
            raise
        finally:
            bad = bad or _connection_is_bad(raw)
            try:
                try:
                    pool.putconn(raw, close=bad)
                except Exception:
                    # close_pool() may run while a background worker is
                    # finishing its final transaction during interpreter
                    # shutdown.  The pool has already discarded the handle.
                    pass
            finally:
                semaphore.release()
    except Exception:
        # getconn() itself can fail (for example after a server restart). In
        # that case no connection was borrowed, but the semaphore still needs
        # to be released.
        if raw is None:
            semaphore.release()
        raise


def lock_account_data(conn: Any, user_id: str) -> None:
    """Serialize account deletion with database ownership writes.

    The account migration's write guards use the same transaction lock.
    """
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('luma-account-data'), "
        "hashtext(substr(encode(sha256(convert_to(?, 'UTF8')), 'hex'), 1, 32)))", (user_id,)
    )


def ensure_db() -> None:
    """Run Alembic migrations while holding the startup advisory lock."""

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    from sqlalchemy.pool import NullPool

    migrations_root = Path(__file__).resolve().parents[1] / "migrations"
    ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = Config(str(ini_path))
    config.set_main_option("script_location", str(migrations_root))
    settings = postgres_settings()
    if settings["user"] == "luma_dev" and settings["dbname"] == DEFAULT_DB_NAME:
        raise RuntimeError("开发账号不能连接生产库")
    query = {
        "sslmode": str(settings["sslmode"]),
        "connect_timeout": str(settings["connect_timeout"]),
        "application_name": str(settings["application_name"]),
    }
    if "options" in settings:
        query["options"] = str(settings["options"])
    url = URL.create(
        "postgresql+psycopg2",
        username=settings["user"],
        password=settings["password"],
        host=settings["host"],
        port=settings["port"],
        database=settings["dbname"],
        query=query,
    )
    engine = create_engine(url, poolclass=NullPool, future=True)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "SELECT pg_advisory_xact_lock(hashtext('luma-assistant-schema'))"
            )
            # env.py must reuse this connection; a second session would block
            # forever on the transaction-scoped advisory lock above.
            config.attributes["connection"] = connection
            # Upgrade every current branch so independent migration tasks can
            # coexist until a later revision merges them.
            command.upgrade(config, "heads")
            connection.commit()
    finally:
        engine.dispose()


def close_pool() -> None:
    """Close pooled PostgreSQL and Redis connections (tests/process exit)."""

    global _pool, _pool_semaphore, _redis_pool, _redis_signature
    with _pool_lock:
        pool, _pool = _pool, None
        _pool_semaphore = None
        if pool is not None:
            try:
                pool.closeall()
            except Exception:
                pass
    with _redis_lock:
        redis_pool, _redis_pool = _redis_pool, None
        _redis_signature = None
        if redis_pool is not None:
            try:
                redis_pool.disconnect(inuse_connections=True)
            except Exception:
                pass


atexit.register(close_pool)


def row_to_dict(row: Optional[Any]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return dict(row)


def storage_status() -> dict[str, Any]:
    """Return safe backend metadata for health and diagnostics."""

    settings = postgres_settings()
    return {
        "backend": "postgres",
        "database": settings["dbname"],
        "host": settings["host"],
        "port": settings["port"],
    }


def ping_database() -> bool:
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


class CacheUnavailable(RuntimeError):
    """Raised by strict cache operations when Redis cannot be reached."""


def _redis_client() -> Any:
    global _redis_pool, _redis_signature
    import redis

    settings = redis_settings()
    # Do not retain a pool across test configuration changes. The password is
    # part of the signature but is never logged or returned to callers.
    signature = tuple(sorted(settings.items()))
    with _redis_lock:
        if _redis_pool is None or _redis_signature != signature:
            if _redis_pool is not None:
                try:
                    _redis_pool.disconnect(inuse_connections=True)
                except Exception:
                    pass
            _redis_pool = redis.ConnectionPool(**settings)
            _redis_signature = signature
        return redis.Redis(connection_pool=_redis_pool)


def ping_redis() -> bool:
    """Return whether the configured Redis endpoint accepts a PING."""

    if not os.getenv("REDIS_HOST", "").strip():
        return False
    try:
        return bool(_redis_client().ping())
    except Exception:
        return False


def cache_get_strict(key: str) -> Optional[str]:
    """Read a value, raising :class:`CacheUnavailable` on Redis failures."""

    try:
        return _redis_client().get(key)
    except Exception as exc:
        raise CacheUnavailable("Redis is unavailable") from exc


def cache_eval_strict(script: str, keys: list[str], args: list[Any]) -> Any:
    """Run a server-owned Lua script atomically, raising on Redis failures."""

    try:
        return _redis_client().eval(script, len(keys), *keys, *args)
    except Exception as exc:
        raise CacheUnavailable("Redis is unavailable") from exc


def cache_set_strict(key: str, value: str, ttl_seconds: int = 60) -> bool:
    """Write a cache value, raising :class:`CacheUnavailable` on failures."""

    try:
        return bool(_redis_client().setex(key, max(1, int(ttl_seconds)), value))
    except Exception as exc:
        raise CacheUnavailable("Redis is unavailable") from exc


def cache_set_existing_strict(key: str, value: str, ttl_seconds: int = 60) -> bool:
    """Refresh an existing cache value without recreating a deleted key.

    Redis' ``XX`` condition makes the value and expiry update conditional on
    the key still existing at the time of the write.  This is used for
    session sliding renewal so a request that raced with logout cannot bring
    a revoked session back to life.
    """

    try:
        return bool(_redis_client().set(key, value, ex=max(1, int(ttl_seconds)), xx=True))
    except Exception as exc:
        raise CacheUnavailable("Redis is unavailable") from exc


def cache_set_if_absent_strict(key: str, value: str, ttl_seconds: int = 60) -> bool:
    """Create a cache value only when the key does not already exist."""

    try:
        return bool(_redis_client().set(key, value, ex=max(1, int(ttl_seconds)), nx=True))
    except Exception as exc:
        raise CacheUnavailable("Redis is unavailable") from exc


def cache_delete_strict(key: str) -> bool:
    """Delete a cache value, raising :class:`CacheUnavailable` on failures."""

    try:
        return bool(_redis_client().delete(key))
    except Exception as exc:
        raise CacheUnavailable("Redis is unavailable") from exc


def cache_sadd_strict(key: str, *members: str) -> int:
    """Add members to a Redis Set, raising on connection failures."""

    try:
        if not members:
            return 0
        return int(_redis_client().sadd(key, *members))
    except Exception as exc:
        raise CacheUnavailable("Redis is unavailable") from exc


def cache_smembers_strict(key: str) -> set[str]:
    """Read a Redis Set, raising on connection failures."""

    try:
        return {str(member) for member in _redis_client().smembers(key)}
    except Exception as exc:
        raise CacheUnavailable("Redis is unavailable") from exc


def cache_srem_strict(key: str, *members: str) -> int:
    """Remove members from a Redis Set, raising on connection failures."""

    try:
        if not members:
            return 0
        return int(_redis_client().srem(key, *members))
    except Exception as exc:
        raise CacheUnavailable("Redis is unavailable") from exc


def cache_pop_strict(key: str) -> Optional[str]:
    """Atomically consume a cache value, preserving Redis failures."""

    try:
        client = _redis_client()
        with client.pipeline(transaction=True) as pipe:
            pipe.get(key)
            pipe.delete(key)
            values = pipe.execute()
        return values[0]
    except Exception as exc:
        raise CacheUnavailable("Redis is unavailable") from exc


def cache_get(key: str) -> Optional[str]:
    """Read an optional short-lived Redis value; failures are non-fatal."""

    try:
        return cache_get_strict(key)
    except CacheUnavailable:
        return None


def cache_set(key: str, value: str, ttl_seconds: int = 60) -> bool:
    """Write an optional short-lived Redis value; failures are non-fatal."""

    try:
        return bool(_redis_client().setex(key, ttl_seconds, value))
    except Exception:
        return False


def cache_delete(key: str) -> bool:
    """Delete a short-lived Redis value; failures are non-fatal."""

    try:
        return bool(_redis_client().delete(key))
    except Exception:
        return False


def cache_pop(key: str) -> Optional[str]:
    """Atomically read and delete one Redis value for one-time cache value."""

    try:
        client = _redis_client()
        with client.pipeline(transaction=True) as pipe:
            pipe.get(key)
            pipe.delete(key)
            values = pipe.execute()
        return values[0]
    except Exception:
        return None
