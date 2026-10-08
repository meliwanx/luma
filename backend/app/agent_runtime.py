"""Per-user Tencent Agent Runtime lifecycle adapter.

The product API remains the control plane.  This module owns only the lease
lifecycle (ensure, inspect and stop) for a user's isolated Agent Runtime
sandbox.  It deliberately does not assume a particular Tencent REST schema:
the paths and API URL are deployment configuration, while the request shape
is the small, stable contract we need at the boundary.  With no endpoint/key
configured the adapter is inert and never creates a cloud resource.

The API key is read only from the service environment.  It is never persisted
in the runtime_leases table or returned by ``status``/``to_public``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .db import get_connection
from .storage import get_storage


logger = logging.getLogger(__name__)


class AgentRuntimeUnavailable(RuntimeError):
    """The cloud adapter is disabled or cannot reach the configured endpoint."""


@dataclass(frozen=True)
class AgentRuntimeConfig:
    enabled: bool
    api_mode: str
    provider: str
    base_url: str
    api_key: str
    secret_id: str
    secret_key: str
    e2b_domain: str
    api_version: str
    region: str
    create_path: str
    get_path: str
    release_path: str
    code_tool: str
    browser_tool: str
    code_tool_id: str
    browser_tool_id: str
    idle_ttl_seconds: int
    request_timeout_seconds: float
    max_instances: int
    max_cpu: int
    max_memory_gib: int
    aio_tool: str = ""
    sandbox_backup_max_mb: int = 512
    sandbox_paused_retention_days: int = 30
    sandbox_max_paused: int = 18


@dataclass(frozen=True)
class RuntimeLease:
    id: str
    user_id_hash: str
    provider: str
    provider_runtime_id: str
    status: str
    capabilities: tuple[str, ...]
    endpoint: Optional[str]
    started_at: Optional[str]
    expires_at: Optional[str]
    last_seen_at: Optional[str]
    stopped_at: Optional[str]
    metadata: dict[str, Any]
    capability: str = "code"

    def to_public(self) -> dict[str, Any]:
        """Return the safe API shape; no user id, key or authorization header."""

        metadata = dict(self.metadata)
        backup = metadata.get("backup")
        if isinstance(backup, dict):
            metadata["backup"] = {
                "size_bytes": backup.get("size_bytes"),
                "created_at": backup.get("created_at"),
            }

        return {
            "id": self.id,
            "provider": self.provider,
            # The provider runtime id is a sandbox identifier.  Keep it only
            # in the server-side lease row; the public response model keeps
            # this legacy field for compatibility but must never receive the
            # provider value.
            "provider_runtime_id": "",
            "status": self.status,
            "capabilities": list(self.capabilities),
            # Provider endpoints can embed the sandbox id in their hostname;
            # preview URLs are issued by the sandbox tool through its own
            # allowlisted response instead.
            "endpoint": None,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "last_seen_at": self.last_seen_at,
            "stopped_at": self.stopped_at,
            "metadata": metadata,
        }


def _bool(value: str, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def config() -> AgentRuntimeConfig:
    base_url = os.getenv("AGENT_RUNTIME_BASE_URL", "").strip().rstrip("/")
    # ``AGENT_RUNTIME_API_MODE`` is the documented name; accept the shorter
    # ``AGENT_RUNTIME_MODE`` used by older deployments while keeping the
    # default fail-closed cloud-api path.
    api_mode = (
        os.getenv("AGENT_RUNTIME_API_MODE", "").strip()
        or os.getenv("AGENT_RUNTIME_MODE", "").strip()
        or "cloud-api"
    ).lower()
    secret_id = os.getenv("TENCENTCLOUD_SECRET_ID", "").strip()
    secret_key = os.getenv("TENCENTCLOUD_SECRET_KEY", "").strip()
    api_key = os.getenv("AGENT_RUNTIME_API_KEY", "").strip()
    e2b_domain = os.getenv("E2B_DOMAIN", "").strip()
    if api_mode == "cloud-api":
        configured = bool(secret_id and secret_key)
    elif api_mode == "e2b":
        api_key = api_key or os.getenv("E2B_API_KEY", "").strip()
        configured = bool(e2b_domain and api_key)
    else:
        configured = bool(base_url and api_key)
    return AgentRuntimeConfig(
        enabled=_bool(os.getenv("AGENT_RUNTIME_ENABLED", "false")) and configured,
        api_mode=api_mode,
        provider=os.getenv("AGENT_RUNTIME_PROVIDER", "tencent-agent-runtime").strip() or "tencent-agent-runtime",
        base_url=base_url,
        api_key=api_key,
        secret_id=secret_id,
        secret_key=secret_key,
        e2b_domain=e2b_domain,
        api_version=os.getenv("AGENT_RUNTIME_API_VERSION", "2025-09-20").strip() or "2025-09-20",
        region=os.getenv("AGENT_RUNTIME_REGION", "").strip(),
        create_path=os.getenv("AGENT_RUNTIME_CREATE_PATH", "/v1/runtimes").strip() or "/v1/runtimes",
        get_path=os.getenv("AGENT_RUNTIME_GET_PATH", "/v1/runtimes/{runtime_id}").strip() or "/v1/runtimes/{runtime_id}",
        release_path=os.getenv("AGENT_RUNTIME_RELEASE_PATH", "/v1/runtimes/{runtime_id}").strip() or "/v1/runtimes/{runtime_id}",
        code_tool=os.getenv("AGENT_RUNTIME_CODE_TOOL", "").strip(),
        browser_tool=os.getenv("AGENT_RUNTIME_BROWSER_TOOL", "").strip(),
        code_tool_id=os.getenv("AGENT_RUNTIME_CODE_TOOL_ID", "").strip(),
        browser_tool_id=os.getenv("AGENT_RUNTIME_BROWSER_TOOL_ID", "").strip(),
        aio_tool=os.getenv("AGENT_RUNTIME_AIO_TOOL", "").strip(),
        idle_ttl_seconds=_int_env("AGENT_RUNTIME_IDLE_TTL_SECONDS", 300, 60, 3600),
        request_timeout_seconds=_float_env("AGENT_RUNTIME_REQUEST_TIMEOUT_SECONDS", 10.0, 1.0, 60.0),
        max_instances=_int_env("AGENT_RUNTIME_MAX_INSTANCES", 5, 1, 50),
        max_cpu=_int_env("AGENT_RUNTIME_MAX_CPU", 10, 1, 100),
        max_memory_gib=_int_env("AGENT_RUNTIME_MAX_MEMORY_GIB", 10, 1, 100),
        sandbox_backup_max_mb=_int_env("SANDBOX_BACKUP_MAX_MB", 512, 1, 10240),
        sandbox_paused_retention_days=_int_env("SANDBOX_PAUSED_RETENTION_DAYS", 30, 1, 3650),
        sandbox_max_paused=_int_env("SANDBOX_MAX_PAUSED", 18, 1, 1000),
    )


def sandbox_tools_configured(settings: Optional[AgentRuntimeConfig] = None) -> bool:
    """Whether the model should see sandbox and browser tools.

    The adapter has to be enabled, and at least one provisioned tool name must
    be set. Cloud API mode may use a tool id instead of a name. Empty defaults
    stay hidden so the model never sees a tool that cannot be called.
    """

    current = config() if settings is None else settings
    if not getattr(current, "enabled", False):
        return False
    if str(getattr(current, "aio_tool", "") or "").strip():
        return True
    if str(getattr(current, "code_tool", "") or "").strip() or str(getattr(current, "browser_tool", "") or "").strip():
        return True
    if str(getattr(current, "api_mode", "") or "") == "cloud-api":
        return bool(
            str(getattr(current, "code_tool_id", "") or "").strip()
            or str(getattr(current, "browser_tool_id", "") or "").strip()
        )
    return False


def _hash_user(user_id: str) -> str:
    return hashlib.sha256((user_id or "local").encode("utf-8")).hexdigest()[:32]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_path(path: str, runtime_id: str = "") -> str:
    value = path if path.startswith("/") else "/" + path
    return value.replace("{runtime_id}", runtime_id)


def _json_load(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row_to_lease(row: Any) -> Optional[RuntimeLease]:
    if row is None:
        return None
    data = dict(row)
    caps = _json_load(data.get("capabilities_json"), [])
    caps = caps if isinstance(caps, list) else []
    metadata = _json_load(data.get("metadata_json"), {})
    return RuntimeLease(
        id=str(data["id"]),
        user_id_hash=str(data.get("user_id_hash") or ""),
        provider=str(data.get("provider") or "tencent-agent-runtime"),
        provider_runtime_id=str(data.get("provider_runtime_id") or ""),
        status=str(data.get("status") or "unknown"),
        capabilities=tuple(str(item) for item in caps if isinstance(item, str)),
        endpoint=data.get("endpoint"),
        started_at=data.get("started_at"),
        expires_at=data.get("expires_at"),
        last_seen_at=data.get("last_seen_at"),
        stopped_at=data.get("stopped_at"),
        metadata=metadata if isinstance(metadata, dict) else {},
        capability=str(data.get("capability") or _capability_key(caps)),
    )


_ACTIVE_LEASE_STATUSES = ("running", "ready", "starting")


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _lease_is_active(lease: RuntimeLease, now: Optional[datetime] = None) -> bool:
    """Return true only for an active lease whose provider TTL has not elapsed."""

    if lease.status not in _ACTIVE_LEASE_STATUSES:
        return False
    expires_at = _parse_timestamp(lease.expires_at)
    return expires_at is not None and expires_at > (now or datetime.now(timezone.utc))


def _pg_advisory_lock(conn: Any, key: str) -> None:
    """Take a transaction-scoped advisory lock on PostgreSQL.

    PostgreSQL is the sole storage backend, so the lock covers the complete
    read/check/create/write critical section.
    """

    conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (key,))


def _require_live_runtime_owner(conn: Any, user_id: str) -> None:
    """Reject new provider activity for disabled or erased accounts."""
    account = conn.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if account is not None and dict(account).get("status") != "active":
        raise AgentRuntimeUnavailable("Account is unavailable")
    deleted = conn.execute(
        "SELECT 1 FROM deleted_account_ids WHERE user_id_hash = ?",
        (hashlib.sha256(user_id.encode("utf-8")).hexdigest(),),
    ).fetchone()
    if deleted is not None:
        raise AgentRuntimeUnavailable("Account is unavailable")


def _capability_key(capabilities: Iterable[str]) -> str:
    values = set(capabilities)
    if {"code", "browser"}.issubset(values):
        return "aio"
    return "browser" if "browser" in values else "code"


def _runtime_capabilities(capability: str, settings: AgentRuntimeConfig) -> tuple[str, ...]:
    return ("code", "browser") if getattr(settings, "aio_tool", "") else (capability,)


def _needs_workspace(lease: RuntimeLease) -> bool:
    return "code" in lease.capabilities


def get_user_runtime(user_id: str, conn: Any = None, capability: str = "code") -> Optional[RuntimeLease]:
    key = "aio" if getattr(config(), "aio_tool", "") else capability
    if conn is not None:
        row = conn.execute(
            "SELECT * FROM runtime_leases WHERE user_id_hash = ? AND capability = ? ORDER BY last_seen_at DESC LIMIT 1",
            (_hash_user(user_id), key),
        ).fetchone()
        return _row_to_lease(row)
    with get_connection() as owned_conn:
        return get_user_runtime(user_id, owned_conn, capability)


def _active_runtime_count(conn: Any = None, now: Optional[datetime] = None) -> int:
    """Count active, unexpired leases (expired rows do not consume budget)."""

    if conn is None:
        with get_connection() as owned_conn:
            return _active_runtime_count(owned_conn, now)
    rows = conn.execute(
        "SELECT status, expires_at FROM runtime_leases WHERE status IN ('running','ready','starting')"
    ).fetchall()
    current = now or datetime.now(timezone.utc)
    count = 0
    for row in rows:
        data = dict(row)
        if _parse_timestamp(data.get("expires_at")) is not None and _parse_timestamp(data.get("expires_at")) > current:
            count += 1
    return count


def _save_lease(user_id: str, lease: RuntimeLease, conn: Any = None) -> RuntimeLease:
    return _save_lease_hash(_hash_user(user_id), lease, conn)


def _save_lease_hash(user_hash: str, lease: RuntimeLease, conn: Any = None) -> RuntimeLease:
    data = {
        "id": lease.id,
        "user_id_hash": user_hash,
        "capability": _capability_key(lease.capabilities),
        "provider": lease.provider,
        "provider_runtime_id": lease.provider_runtime_id,
        "status": lease.status,
        "capabilities_json": json.dumps(list(lease.capabilities), ensure_ascii=False),
        "endpoint": lease.endpoint,
        "started_at": lease.started_at,
        "expires_at": lease.expires_at,
        "last_seen_at": lease.last_seen_at,
        "stopped_at": lease.stopped_at,
        "metadata_json": json.dumps(lease.metadata, ensure_ascii=False),
    }
    if conn is None:
        with get_connection() as owned_conn:
            return _save_lease_hash(user_hash, lease, owned_conn)
    conn.execute(
        "INSERT INTO runtime_leases "
        "(id,user_id_hash,capability,provider,provider_runtime_id,status,capabilities_json,endpoint,started_at,expires_at,last_seen_at,stopped_at,metadata_json) "
        "VALUES (:id,:user_id_hash,:capability,:provider,:provider_runtime_id,:status,:capabilities_json,:endpoint,:started_at,:expires_at,:last_seen_at,:stopped_at,:metadata_json) "
        "ON CONFLICT (user_id_hash, capability) DO UPDATE SET "
        "id=excluded.id, provider=excluded.provider, provider_runtime_id=excluded.provider_runtime_id, status=excluded.status, "
        "capabilities_json=excluded.capabilities_json, endpoint=excluded.endpoint, started_at=excluded.started_at, "
        "expires_at=excluded.expires_at, last_seen_at=excluded.last_seen_at, stopped_at=excluded.stopped_at, metadata_json=excluded.metadata_json",
        data,
    )
    return lease


def _request(method: str, path: str, *, payload: Optional[dict[str, Any]] = None, settings: AgentRuntimeConfig) -> dict[str, Any]:
    if not settings.enabled or not settings.api_key:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime adapter is not configured")
    url = settings.base_url + _normalise_path(path)
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json", "User-Agent": "luma-assistant-runtime/1"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    headers["Authorization"] = "Bearer " + settings.api_key
    request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=settings.request_timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # Do not include response bodies: providers sometimes echo auth data.
        raise AgentRuntimeUnavailable(f"Tencent Agent Runtime HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AgentRuntimeUnavailable(f"Tencent Agent Runtime request failed: {type(exc).__name__}") from exc
    decoded = _json_load(raw, {})
    return decoded if isinstance(decoded, dict) else {}


def sandbox_host_suffix() -> str:
    """Host suffix for sandbox preview and browser data-plane URLs.

    ``SANDBOX_PREVIEW_HOST_SUFFIX`` wins. Otherwise the suffix is derived from
    ``E2B_DOMAIN``. An empty result means the deployment has not chosen a
    data-plane host, and callers must refuse the URL.
    """

    raw = os.getenv("SANDBOX_PREVIEW_HOST_SUFFIX", "").strip().lower().rstrip(".")
    if raw:
        return raw if raw.startswith(".") else "." + raw
    domain = os.getenv("E2B_DOMAIN", "").strip().lower().rstrip(".")
    if domain and "/" not in domain and "\\" not in domain and "@" not in domain:
        return "." + domain
    return ""


def _required_tool_name(settings: AgentRuntimeConfig, capability: str) -> str:
    """Return the provisioned tool name, or explain which variable is empty."""

    aio = str(getattr(settings, "aio_tool", "") or "").strip()
    if aio:
        return aio
    if capability == "code":
        name = str(getattr(settings, "code_tool", "") or "").strip()
        variable = "AGENT_RUNTIME_CODE_TOOL"
    else:
        name = str(getattr(settings, "browser_tool", "") or "").strip()
        variable = "AGENT_RUNTIME_BROWSER_TOOL"
    if not name:
        raise AgentRuntimeUnavailable(variable + " 未配置")
    return name


def _require_region(settings: AgentRuntimeConfig) -> str:
    region = str(getattr(settings, "region", "") or "").strip()
    if not region:
        raise AgentRuntimeUnavailable("AGENT_RUNTIME_REGION 未配置")
    return region


def _cloud_client(settings: AgentRuntimeConfig) -> Any:
    """Create the official Tencent Cloud AGS client lazily.

    The SDK is optional in local/dev environments.  Importing this module does
    not require network access or the Tencent dependency.
    """

    _require_region(settings)
    try:
        from tencentcloud.ags.v20250920 import ags_client, models  # type: ignore
        from tencentcloud.common import credential  # type: ignore
        from tencentcloud.common.profile import client_profile  # type: ignore
    except ImportError as exc:
        raise AgentRuntimeUnavailable("tencentcloud-sdk-python-ags is not installed") from exc
    cred = credential.Credential(settings.secret_id, settings.secret_key)
    profile = client_profile.ClientProfile()
    profile.signMethod = "TC3-HMAC-SHA256"
    client = ags_client.AgsClient(cred, settings.region, profile)
    return client, models


def _cloud_instance_dict(instance: Any) -> dict[str, Any]:
    if instance is None:
        return {}
    result: dict[str, Any] = {}
    for name in ("InstanceId", "ToolId", "ToolName", "Status", "ExpiresAt", "CreateTime", "UpdateTime", "AuthMode", "NetworkMode", "TimeoutSeconds", "StopReason"):
        value = getattr(instance, name, None)
        if value is not None:
            result[name] = value
    return result


def _e2b_pause_lifecycle() -> Any:
    """Return the E2B SDK lifecycle value that pauses on timeout, if present.

    E2B 2.x exposes this as ``SandboxLifecycle`` but older compatible SDKs do
    not.  Keeping this lookup lazy lets local/dev installs use the fallback
    provider timeout without importing an optional dependency at module load.
    """

    try:
        from e2b.sandbox.sandbox_api import SandboxLifecycle  # type: ignore
    except (ImportError, AttributeError):
        return None
    for name in ("PAUSE", "PAUSED", "pause", "paused"):
        value = getattr(SandboxLifecycle, name, None)
        if value is not None:
            return value
    members = getattr(SandboxLifecycle, "__members__", {})
    if isinstance(members, dict):
        for name, value in members.items():
            if str(name).lower() in {"pause", "paused"}:
                return value
    # In e2b 2.x this symbol is a TypedDict/type alias rather than an enum.
    # The documented wire field is ``on_timeout``; construct the value only
    # after the import succeeds so older SDKs still use the timeout cushion.
    return {"on_timeout": "pause"}


def _e2b_start(settings: AgentRuntimeConfig, capability: str) -> dict[str, Any]:
    """Start a Tencent Agent Sandbox through its E2B-compatible endpoint.

    The lifecycle option is required because E2B's plain ``timeout`` kills a
    sandbox.  If the installed SDK has no lifecycle support, use a provider
    timeout 10 minutes longer than our reaper TTL so maintenance can archive
    and pause it first.
    """

    template = _required_tool_name(settings, capability)
    try:
        from e2b import Sandbox  # type: ignore
    except ImportError as exc:
        raise AgentRuntimeUnavailable("e2b is not installed") from exc
    idle_ttl = int(getattr(settings, "idle_ttl_seconds", 300))
    kwargs = {
        "template": template,
        "api_key": getattr(settings, "api_key", ""),
        "domain": getattr(settings, "e2b_domain", ""),
        "metadata": {"luma_capability": capability},
    }
    lifecycle = _e2b_pause_lifecycle()
    try:
        if lifecycle is not None:
            # With lifecycle=PAUSE the provider preserves the sandbox after
            # timeout; our reaper then controls when it is finally killed.
            sandbox = Sandbox.create(timeout=idle_ttl, lifecycle=lifecycle, **kwargs)
        else:
            # Older SDKs reject lifecycle.  Give the reaper a 10-minute head
            # start before the provider's kill timeout as a safe fallback.
            sandbox = Sandbox.create(timeout=idle_ttl + 600, **kwargs)
    except TypeError:
        try:
            # A 2.x-compatible endpoint may expose SandboxLifecycle but still
            # reject the keyword.  Retry using the conservative timeout.
            sandbox = Sandbox.create(timeout=idle_ttl + 600, **kwargs)
        except Exception as exc:
            raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B sandbox start failed") from exc
    except Exception as exc:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B sandbox start failed") from exc
    sandbox_id = str(getattr(sandbox, "sandbox_id", "") or getattr(sandbox, "id", ""))
    if not sandbox_id:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B response did not include a sandbox id")
    domain = getattr(sandbox, "sandbox_domain", None) or getattr(sandbox, "domain", None)
    # Keep the object private to this module so a freshly created sandbox can
    # be used directly; it never enters lease metadata or API responses.
    return {"sandbox_id": sandbox_id, "status": "running", "endpoint": domain, "_sandbox": sandbox}


def _e2b_refresh_timeout(sandbox: Any, settings: AgentRuntimeConfig, user_hash: str = "unknown") -> None:
    """Renew provider-side timeout; failures never expose provider details."""

    setter = getattr(sandbox, "set_timeout", None)
    if not callable(setter):
        return
    try:
        setter(int(getattr(settings, "idle_ttl_seconds", 300)))
    except Exception as exc:
        logger.warning(
            "sandbox timeout renewal failed user_hash=%s error=%s",
            user_hash,
            type(exc).__name__,
        )


def _e2b_connect(settings: AgentRuntimeConfig, provider_runtime_id: str, user_hash: str = "unknown") -> Any:
    """Connect to an existing E2B sandbox, allowing the provider to resume it."""

    try:
        from e2b import Sandbox  # type: ignore
    except ImportError as exc:
        raise AgentRuntimeUnavailable("e2b is not installed") from exc
    try:
        sandbox = Sandbox.connect(
            provider_runtime_id,
            api_key=getattr(settings, "api_key", ""),
            domain=getattr(settings, "e2b_domain", ""),
        )
        # Connecting also resumes a paused instance on E2B.  Renew its
        # provider timeout before returning it to a caller.
        _e2b_refresh_timeout(sandbox, settings, user_hash)
        return sandbox
    except Exception as exc:
        # Provider exception details can contain request data or credentials.
        raise AgentRuntimeUnavailable(
            "Tencent Agent Runtime E2B sandbox connect failed"
        ) from exc


def _sandbox_commands_run(sandbox: Any, command: str, timeout: int = 120) -> Any:
    commands = getattr(sandbox, "commands", None)
    run = getattr(commands, "run", None)
    if not callable(run):
        raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B command channel unavailable")
    try:
        return run(command, timeout=timeout)
    except TypeError:
        # A few test doubles and older SDK releases do not expose timeout.
        try:
            return run(command)
        except Exception as exc:
            raise AgentRuntimeUnavailable(
                "Tencent Agent Runtime E2B command failed"
            ) from exc
    except Exception as exc:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B command failed") from exc


def _sandbox_file_bytes(sandbox: Any, path: str) -> bytes:
    """Read a sandbox file using the SDK's available binary file primitive."""

    files = getattr(sandbox, "files", None)
    for name in ("read_bytes", "download", "read"):
        reader = getattr(files, name, None)
        if not callable(reader):
            continue
        try:
            try:
                value = reader(path, format="bytes")
            except TypeError:
                value = reader(path)
            if isinstance(value, bytes):
                return value
            if isinstance(value, bytearray):
                return bytes(value)
            if isinstance(value, str):
                return value.encode("utf-8")
            # Some SDK methods return a small response wrapper.
            body = getattr(value, "content", None)
            if isinstance(body, bytes):
                return body
        except Exception as exc:
            raise AgentRuntimeUnavailable(
                "Tencent Agent Runtime E2B file read failed"
            ) from exc
    raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B file channel unavailable")


def _sandbox_write_bytes(sandbox: Any, path: str, content: bytes) -> None:
    files = getattr(sandbox, "files", None)
    for name in ("write", "upload"):
        writer = getattr(files, name, None)
        if not callable(writer):
            continue
        try:
            writer(path, content)
            return
        except TypeError:
            try:
                writer(content, path)
                return
            except Exception as exc:
                raise AgentRuntimeUnavailable(
                    "Tencent Agent Runtime E2B file write failed"
                ) from exc
        except Exception as exc:
            raise AgentRuntimeUnavailable(
                "Tencent Agent Runtime E2B file write failed"
            ) from exc
    raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B file channel unavailable")


def _sandbox_prepare_workspace(sandbox: Any) -> None:
    result = _sandbox_commands_run(
        sandbox,
        "mkdir -p /home/user/workspace/uploads /home/user/workspace/outputs",
        timeout=30,
    )
    if int(getattr(result, "exit_code", 0) or 0):
        raise AgentRuntimeUnavailable("Tencent Agent Runtime workspace preparation failed")


def _sandbox_pause(sandbox: Any) -> None:
    pause = getattr(sandbox, "pause", None)
    if not callable(pause):
        raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B pause unavailable")
    try:
        pause(keep_memory=False)
    except TypeError:
        # Older E2B SDKs do not expose keep_memory.  The pause operation is
        # still safe because the provider preserves the filesystem by default.
        pause()


def _pause_quota_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in ("limitexceeded", "quota", "too many", "maximum paused"))


def _paused_count(conn: Any) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM runtime_leases WHERE status = 'paused'").fetchone()
    if row is None:
        return 0
    try:
        return int(dict(row).get("c", 0) or 0)
    except (TypeError, ValueError):
        try:
            return int(row[0] or 0)
        except (TypeError, ValueError, IndexError, KeyError):
            return 0


def _paused_candidates(conn: Any) -> list[RuntimeLease]:
    rows = conn.execute(
        "SELECT * FROM runtime_leases WHERE status = 'paused' ORDER BY last_seen_at ASC"
    ).fetchall()
    result: list[RuntimeLease] = []
    for row in rows:
        lease = _row_to_lease(row)
        if lease:
            result.append(lease)
    return result


def _evict_paused_for_capacity(
    settings: AgentRuntimeConfig,
    conn: Any,
    needed: int = 1,
) -> bool:
    """Kill oldest paused sandboxes until one pause slot is available.

    A paused code workspace without a durable backup is never destroyed. We
    first reconnect and archive it; browser-only leases need no backup.
    """

    limit = int(getattr(settings, "sandbox_max_paused", 18))
    evicted = False
    while _paused_count(conn) + needed > limit:
        removed = False
        for candidate in _paused_candidates(conn):
            current_row = conn.execute(
                "SELECT * FROM runtime_leases WHERE id = ? LIMIT 1", (candidate.id,)
            ).fetchone()
            lease = _row_to_lease(current_row)
            if not lease or lease.status != "paused":
                continue
            backup = _backup_metadata(lease)
            metadata = dict(lease.metadata)
            if backup is None and _needs_workspace(lease):
                resumed_sandbox = None
                try:
                    # ``connect`` resumes a paused sandbox.  If archiving is
                    # unsuccessful, put it back into the paused state before
                    # moving on so the failed candidate is never left
                    # running while its lease still says ``paused``.
                    resumed_sandbox = _e2b_connect(
                        settings, lease.provider_runtime_id, lease.user_id_hash
                    )
                    backup = _sandbox_archive(resumed_sandbox, settings, lease.user_id_hash)
                except Exception as exc:
                    logger.warning(
                        "paused sandbox backup failed user_hash=%s error=%s",
                        lease.user_id_hash,
                        type(exc).__name__,
                    )
                    if resumed_sandbox is not None:
                        try:
                            _sandbox_pause(resumed_sandbox)
                        except Exception as pause_exc:
                            logger.warning(
                                "paused sandbox restore failed user_hash=%s error=%s",
                                lease.user_id_hash,
                                type(pause_exc).__name__,
                            )
                    continue
                if not backup or not backup.get("storage_key"):
                    # A size-capped archive is not a valid recovery backup.
                    try:
                        _sandbox_pause(resumed_sandbox)
                    except Exception as pause_exc:
                        logger.warning(
                            "paused sandbox restore failed user_hash=%s error=%s",
                            lease.user_id_hash,
                            type(pause_exc).__name__,
                        )
                    continue
                metadata["backup"] = backup
                metadata.pop("backup_skipped", None)
                _save_lease_hash(
                    lease.user_id_hash,
                    RuntimeLease(**{**asdict(lease), "metadata": metadata}),
                    conn,
                )
                lease = RuntimeLease(**{**asdict(lease), "metadata": metadata})
            try:
                _stop_provider(settings, lease.provider_runtime_id)
            except Exception as exc:
                logger.warning(
                    "paused sandbox eviction failed user_hash=%s error=%s",
                    lease.user_id_hash,
                    type(exc).__name__,
                )
                continue
            stopped_at = _timestamp()
            _save_lease_hash(
                lease.user_id_hash,
                RuntimeLease(
                    **{
                        **asdict(lease),
                        "status": "stopped",
                        "stopped_at": stopped_at,
                        "last_seen_at": stopped_at,
                        "endpoint": None,
                    }
                ),
                conn,
            )
            removed = True
            evicted = True
            break
        if not removed:
            break
    return evicted


def _backup_metadata(lease: RuntimeLease) -> Optional[dict[str, Any]]:
    value = lease.metadata.get("backup") if isinstance(lease.metadata, dict) else None
    return value if isinstance(value, dict) and value.get("storage_key") else None


def _sandbox_archive(
    sandbox: Any, settings: AgentRuntimeConfig, user_hash: str = "unknown"
) -> Optional[dict[str, Any]]:
    """Archive and upload a workspace, retaining the old backup on failure."""

    # Keep both the user workspace and pip --user's ~/.local tree.  The
    # conditional avoids tar failing on fresh sandboxes without .local.  Cache
    # directories are disposable and can be very large, so exclude them at
    # every depth while retaining source files and installed packages.
    command = (
        "cd /home/user && if [ -d .local ]; then "
        "tar czf /tmp/ws.tgz "
        "--exclude='workspace/.cache' --exclude='workspace/**/.cache' "
        "--exclude='workspace/**/__pycache__' "
        "--exclude='workspace/**/node_modules/.cache' "
        "--exclude='.local/.cache' --exclude='.local/**/.cache' "
        "--exclude='.local/**/__pycache__' "
        "--exclude='.local/**/node_modules/.cache' workspace .local; "
        "else tar czf /tmp/ws.tgz "
        "--exclude='workspace/.cache' --exclude='workspace/**/.cache' "
        "--exclude='workspace/**/__pycache__' "
        "--exclude='workspace/**/node_modules/.cache' workspace; fi"
    )
    result = _sandbox_commands_run(sandbox, command, timeout=180)
    exit_code = int(getattr(result, "exit_code", 0) or 0)
    if exit_code:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B workspace backup failed")

    size = 0
    size_result = _sandbox_commands_run(sandbox, "stat -c %s /tmp/ws.tgz", timeout=30)
    try:
        size = int(str(getattr(size_result, "stdout", "") or "").strip().splitlines()[-1])
    except (TypeError, ValueError, IndexError):
        size = 0
    max_bytes = int(getattr(settings, "sandbox_backup_max_mb", 512)) * 1024 * 1024
    if size > max_bytes:
        logger.warning(
            "sandbox backup skipped user_hash=%s size_bytes=%s",
            user_hash,
            size,
        )
        return {"backup_skipped": "too_large", "backup_size_bytes": size}

    content = _sandbox_file_bytes(sandbox, "/tmp/ws.tgz")
    if not size:
        size = len(content)
    if size > max_bytes:
        logger.warning(
            "sandbox backup skipped user_hash=%s size_bytes=%s",
            user_hash,
            size,
        )
        return {"backup_skipped": "too_large", "backup_size_bytes": size}

    # A random suffix avoids clobbering an existing backup if two maintenance
    # workers race after a stale row was selected.
    key_hint = "sandbox_backups/{}/{}-{}.tgz".format(
        secrets.token_hex(16),
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"),
        secrets.token_hex(6),
    )
    storage = get_storage()
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as handle:
        handle.write(content)
        handle.seek(0)
        storage_key = storage.put(key_hint, handle, size, "application/gzip")
    return {
        "storage_key": storage_key,
        "size_bytes": size,
        "created_at": _timestamp(),
    }


def _restore_workspace(sandbox: Any, backup: Optional[dict[str, Any]]) -> bool:
    if not backup or not backup.get("storage_key"):
        _sandbox_prepare_workspace(sandbox)
        return False
    try:
        content = get_storage().get(str(backup["storage_key"]))
        _sandbox_write_bytes(sandbox, "/tmp/ws.tgz", content)
        result = _sandbox_commands_run(
            sandbox,
            "cd /home/user && tar xzf /tmp/ws.tgz && mkdir -p workspace/uploads workspace/outputs",
            timeout=180,
        )
        if int(getattr(result, "exit_code", 0) or 0):
            raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B workspace restore failed")
        return True
    except AgentRuntimeUnavailable:
        raise
    except Exception as exc:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime workspace restore failed") from exc


def _delete_backup(backup: Optional[dict[str, Any]]) -> None:
    if not backup or not backup.get("storage_key"):
        return
    try:
        get_storage().delete(str(backup["storage_key"]))
    except Exception as exc:
        logger.warning("sandbox backup delete failed error=%s", type(exc).__name__)


def _e2b_stop(settings: AgentRuntimeConfig, provider_runtime_id: str) -> None:
    try:
        from e2b import Sandbox  # type: ignore
        Sandbox.kill(
            provider_runtime_id,
            api_key=getattr(settings, "api_key", ""),
            domain=getattr(settings, "e2b_domain", ""),
        )
    except Exception as exc:
        # The E2B-compatible endpoint returns 400 when an auto-expired or
        # already-stopped instance is killed again. Treat that state as an
        # idempotent release so a lease can never remain active in our DB.
        if "STOPPED state" in str(exc) or "not found" in str(exc).lower():
            return
        raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B sandbox stop failed") from exc


def _cloud_start(settings: AgentRuntimeConfig, capabilities: tuple[str, ...], user_id: str) -> dict[str, Any]:
    client, models = _cloud_client(settings)
    request = models.StartSandboxInstanceRequest()
    # Prefer immutable ToolId once provisioned; ToolName keeps the adapter
    # usable with the two existing named tools during initial setup.
    selected = capabilities[0] if capabilities else "code"
    tool_id = "" if getattr(settings, "aio_tool", "") else (settings.code_tool_id if selected == "code" else settings.browser_tool_id)
    if tool_id:
        request.ToolId = tool_id
    else:
        request.ToolName = _required_tool_name(settings, selected)
    request.Timeout = f"{settings.idle_ttl_seconds}s"
    request.ClientToken = "luma-" + _hash_user(user_id) + "-" + _capability_key(capabilities)
    request.AuthMode = "TOKEN"
    try:
        response = client.StartSandboxInstance(request)
    except Exception as exc:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime StartSandboxInstance failed") from exc
    return _cloud_instance_dict(getattr(response, "Instance", None))


def acquire_user_runtime_token(user_id: str) -> dict[str, str]:
    """Acquire a short-lived data-plane token for an existing lease.

    The returned token is intentionally not persisted and must stay inside the
    one tool invocation that needs it. It must never cross the public API.
    """

    settings = config()
    if not settings.enabled or settings.api_mode != "cloud-api":
        raise AgentRuntimeUnavailable("Tencent Agent Runtime Cloud API is not configured")
    lease = get_user_runtime(user_id)
    if not lease or not lease.provider_runtime_id:
        raise AgentRuntimeUnavailable("No runtime lease exists for this user")
    client, models = _cloud_client(settings)
    request = models.AcquireSandboxInstanceTokenRequest()
    request.InstanceId = lease.provider_runtime_id
    try:
        response = client.AcquireSandboxInstanceToken(request)
    except Exception as exc:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime AcquireSandboxInstanceToken failed") from exc
    token = str(getattr(response, "Token", "") or "")
    traffic_token = str(getattr(response, "TrafficToken", "") or "")
    if not token:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime returned no instance token")
    result = {"token": token, "expires_at": str(getattr(response, "ExpiresAt", "") or "")}
    if traffic_token:
        result["traffic_token"] = traffic_token
    return result


def _cloud_get(settings: AgentRuntimeConfig, provider_runtime_id: str) -> dict[str, Any]:
    client, models = _cloud_client(settings)
    request = models.DescribeSandboxInstanceListRequest()
    request.InstanceIds = [provider_runtime_id]
    try:
        response = client.DescribeSandboxInstanceList(request)
    except Exception as exc:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime DescribeSandboxInstanceList failed") from exc
    instances = getattr(response, "InstanceSet", None) or []
    return _cloud_instance_dict(instances[0]) if instances else {}


def _cloud_stop(settings: AgentRuntimeConfig, provider_runtime_id: str) -> None:
    client, models = _cloud_client(settings)
    request = models.StopSandboxInstanceRequest()
    request.InstanceId = provider_runtime_id
    try:
        client.StopSandboxInstance(request)
    except Exception as exc:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime StopSandboxInstance failed") from exc


def _stop_provider(settings: AgentRuntimeConfig, provider_runtime_id: str) -> None:
    """Stop a provider sandbox using only the configured provider protocol."""

    if not provider_runtime_id:
        return
    if settings.api_mode == "cloud-api":
        _cloud_stop(settings, provider_runtime_id)
    elif settings.api_mode == "e2b":
        _e2b_stop(settings, provider_runtime_id)
    elif settings.api_mode == "gateway":
        _request("DELETE", _normalise_path(settings.release_path, provider_runtime_id), settings=settings)
    else:
        raise AgentRuntimeUnavailable("Unsupported Tencent Agent Runtime API mode")


def _provider_id(response: dict[str, Any]) -> str:
    for key in ("runtime_id", "id", "instance_id", "sandbox_id", "InstanceId"):
        value = response.get(key)
        if value:
            return str(value)
    nested = response.get("data")
    if isinstance(nested, dict):
        return _provider_id(nested)
    raise AgentRuntimeUnavailable("Tencent Agent Runtime response did not include a runtime id")


def _from_response(response: dict[str, Any], *, user_id: str, capabilities: tuple[str, ...], settings: AgentRuntimeConfig, existing_id: Optional[str] = None) -> RuntimeLease:
    nested = response.get("data") if isinstance(response.get("data"), dict) else response
    provider_runtime_id = _provider_id(response)
    now = _timestamp()
    expires_at = nested.get("expires_at") or nested.get("ExpiresAt") or (datetime.now(timezone.utc) + timedelta(seconds=settings.idle_ttl_seconds)).isoformat()
    endpoint = nested.get("endpoint") or nested.get("url") or nested.get("Endpoint")
    return RuntimeLease(
        id=existing_id or "rt_" + secrets.token_hex(12),
        user_id_hash=_hash_user(user_id),
        provider=settings.provider,
        provider_runtime_id=provider_runtime_id,
        status=str(nested.get("status") or nested.get("Status") or "running").lower(),
        capabilities=capabilities,
        endpoint=str(endpoint) if endpoint else None,
        started_at=str(nested.get("started_at") or nested.get("CreateTime") or now),
        expires_at=str(expires_at),
        last_seen_at=now,
        stopped_at=None,
        metadata={"region": settings.region, "tool_ids": list(capabilities)},
        capability=_capability_key(capabilities),
    )


def _missing_runtime_settings(settings: AgentRuntimeConfig) -> list[str]:
    """Names of runtime variables that are required once the adapter is enabled."""

    if not settings.enabled:
        return []
    missing = []
    if settings.api_mode == "cloud-api" and not str(settings.region or "").strip():
        missing.append("AGENT_RUNTIME_REGION")
    aio = str(getattr(settings, "aio_tool", "") or "").strip()
    if settings.api_mode == "e2b" and not aio and not str(settings.code_tool or "").strip():
        missing.append("AGENT_RUNTIME_CODE_TOOL")
    if settings.api_mode == "e2b" and not aio and not str(settings.browser_tool or "").strip():
        missing.append("AGENT_RUNTIME_BROWSER_TOOL")
    if settings.api_mode == "e2b" and not sandbox_host_suffix():
        missing.append("SANDBOX_PREVIEW_HOST_SUFFIX")
    return missing


def _status_reason(settings: AgentRuntimeConfig) -> str:
    if not settings.enabled:
        return "未配置"
    missing = _missing_runtime_settings(settings)
    if missing:
        return "未配置：" + ",".join(missing)
    return "ready"


def status() -> dict[str, Any]:
    """Safe diagnostics for health/status pages; never reports the API key."""

    settings = config()
    return {
        "enabled": settings.enabled,
        "configured": bool(
            (settings.secret_id and settings.secret_key)
            if settings.api_mode == "cloud-api"
            else (settings.api_key and (settings.e2b_domain if settings.api_mode == "e2b" else settings.base_url))
        ),
        "provider": settings.provider,
        "api_mode": settings.api_mode,
        "region": settings.region,
        "tools": {
            "code": {"name": settings.code_tool, "id_configured": bool(settings.code_tool_id)},
            "browser": {"name": settings.browser_tool, "id_configured": bool(settings.browser_tool_id)},
            "aio": {"name": getattr(settings, "aio_tool", ""), "configured": bool(getattr(settings, "aio_tool", ""))},
        },
        "idle_ttl_seconds": settings.idle_ttl_seconds,
        "max_instances": settings.max_instances,
        "max_cpu": settings.max_cpu,
        "max_memory_gib": settings.max_memory_gib,
        "sandbox_max_paused": getattr(settings, "sandbox_max_paused", 18),
        "reason": _status_reason(settings),
        "missing": _missing_runtime_settings(settings),
    }


def _e2b_create_and_restore_locked(
    user_id: str,
    capability: str,
    settings: AgentRuntimeConfig,
    conn: Any,
    previous: Optional[RuntimeLease],
) -> Any:
    """Create a replacement sandbox and restore the user's latest backup."""

    if _active_runtime_count(conn) >= int(getattr(settings, "max_instances", 5)):
        raise AgentRuntimeUnavailable("Tencent Agent Runtime instance budget is exhausted")
    response = _e2b_start(settings, capability)
    lease = _from_response(
        response,
        user_id=user_id,
        capabilities=_runtime_capabilities(capability, settings),
        settings=settings,
        existing_id=previous.id if previous and previous.capability == _capability_key(_runtime_capabilities(capability, settings)) else None,
    )
    backup = _backup_metadata(previous) if previous and _needs_workspace(lease) else None
    try:
        sandbox = response.get("_sandbox")
        if sandbox is None:
            sandbox = _e2b_connect(settings, lease.provider_runtime_id, _hash_user(user_id))
        restored = _restore_workspace(sandbox, backup) if _needs_workspace(lease) else False
    except Exception:
        # Do not leave a replacement sandbox running when restoration fails;
        # the previous lease and backup remain intact for a later retry.
        try:
            _e2b_stop(settings, lease.provider_runtime_id)
        except Exception as cleanup_exc:
            logger.warning(
                "sandbox replacement cleanup failed user_hash=%s error=%s",
                _hash_user(user_id),
                type(cleanup_exc).__name__,
            )
        raise
    metadata = dict(lease.metadata)
    if restored:
        metadata["restored_from_backup"] = True
    if backup:
        metadata["backup"] = backup
    lease = RuntimeLease(**{**asdict(lease), "metadata": metadata})
    _save_lease(user_id, lease, conn)
    return sandbox


def _merge_aio_leases_locked(user_id: str, settings: AgentRuntimeConfig, conn: Any) -> Optional[RuntimeLease]:
    """Retire split leases before replacing them with one configured AIO.

    Archive a code workspace before releasing its provider sandbox.  The
    stopped rows remain recoverable if AIO creation fails; remove them only
    after a merged lease has been saved.
    """

    rows = conn.execute(
        "SELECT * FROM runtime_leases WHERE user_id_hash = ? AND capability <> ? ORDER BY last_seen_at DESC",
        (_hash_user(user_id), "aio"),
    ).fetchall()
    previous = None
    for row in rows:
        lease = _row_to_lease(row)
        if lease is None:
            continue
        metadata = dict(lease.metadata)
        if lease.provider_runtime_id and lease.status in set(_ACTIVE_LEASE_STATUSES) | {"paused"}:
            if _needs_workspace(lease):
                sandbox = _e2b_connect(settings, lease.provider_runtime_id, lease.user_id_hash)
                backup = _sandbox_archive(sandbox, settings, lease.user_id_hash)
                if not backup or not backup.get("storage_key"):
                    raise AgentRuntimeUnavailable("Tencent Agent Runtime AIO workspace backup unavailable")
                metadata["backup"] = backup
            _stop_provider(settings, lease.provider_runtime_id)
            lease = RuntimeLease(**{
                **asdict(lease), "status": "stopped", "endpoint": None,
                "stopped_at": _timestamp(), "metadata": metadata,
            })
            _save_lease(user_id, lease, conn)
        if previous is None or _needs_workspace(lease):
            previous = lease
    return previous


def _connect_user_sandbox_locked(
    user_id: str,
    capability: str,
    settings: AgentRuntimeConfig,
    conn: Any,
) -> Any:
    user_hash = _hash_user(user_id)
    existing = get_user_runtime(user_id, conn, capability)
    if getattr(settings, "aio_tool", ""):
        previous = _merge_aio_leases_locked(user_id, settings, conn)
        if existing is None and previous is not None:
            # The replacement uses a fresh row id; its previous backup still
            # belongs to the stopped split row until creation succeeds.
            sandbox = _e2b_create_and_restore_locked(user_id, capability, settings, conn, previous)
            conn.execute(
                "DELETE FROM runtime_leases WHERE user_id_hash = ? AND capability <> ?",
                (user_hash, "aio"),
            )
            return sandbox
        if previous is not None:
            conn.execute(
                "DELETE FROM runtime_leases WHERE user_id_hash = ? AND capability <> ?",
                (user_hash, "aio"),
            )
    else:
        # When AIO is disabled, retire its provider once and retain a stopped
        # backup row until the code capability has recovered its workspace.
        row = conn.execute(
            "SELECT * FROM runtime_leases WHERE user_id_hash = ? AND capability = ? ORDER BY last_seen_at DESC LIMIT 1",
            (user_hash, "aio"),
        ).fetchone()
        previous_aio = _row_to_lease(row)
        if previous_aio is not None and previous_aio.capability == "aio":
            metadata = dict(previous_aio.metadata)
            if previous_aio.status in set(_ACTIVE_LEASE_STATUSES) | {"paused"}:
                sandbox = _e2b_connect(settings, previous_aio.provider_runtime_id, user_hash)
                backup = _sandbox_archive(sandbox, settings, user_hash)
                if not backup or not backup.get("storage_key"):
                    raise AgentRuntimeUnavailable("Tencent Agent Runtime AIO workspace backup unavailable")
                metadata["backup"] = backup
                _stop_provider(settings, previous_aio.provider_runtime_id)
                previous_aio = RuntimeLease(**{
                    **asdict(previous_aio), "status": "stopped", "endpoint": None,
                    "stopped_at": _timestamp(), "metadata": metadata,
                })
                _save_lease(user_id, previous_aio, conn)
            if capability == "code" and existing is None:
                sandbox = _e2b_create_and_restore_locked(user_id, capability, settings, conn, previous_aio)
                conn.execute(
                    "DELETE FROM runtime_leases WHERE user_id_hash = ? AND capability = ?",
                    (user_hash, "aio"),
                )
                return sandbox
            if existing is not None and capability == "code":
                conn.execute(
                    "DELETE FROM runtime_leases WHERE user_id_hash = ? AND capability = ?",
                    (user_hash, "aio"),
                )
    now = datetime.now(timezone.utc)
    if existing and existing.status in _ACTIVE_LEASE_STATUSES and _lease_is_active(existing, now):
        if capability not in existing.capabilities:
            raise AgentRuntimeUnavailable("A different sandbox capability is already active")
        sandbox = _e2b_connect(settings, existing.provider_runtime_id, user_hash)
        if _needs_workspace(existing):
            _sandbox_prepare_workspace(sandbox)
        touched = RuntimeLease(
            **{
                **asdict(existing),
                "last_seen_at": _timestamp(),
                "expires_at": (now + timedelta(seconds=int(getattr(settings, "idle_ttl_seconds", 300)))).isoformat(),
            }
        )
        _save_lease(user_id, touched, conn)
        return sandbox

    connect_failed = False
    if existing and existing.status in _ACTIVE_LEASE_STATUSES:
        # The provider may have paused this instance just after our local TTL
        # elapsed. Connect first: E2B resumes it in place, preserving files
        # outside the workspace (for example ~/.local packages). Only a
        # failed connect means the provider object is gone and requires a
        # replacement restored from the last durable backup.
        if settings.api_mode == "e2b":
            try:
                sandbox = _e2b_connect(settings, existing.provider_runtime_id, user_hash)
                touched = RuntimeLease(
                    **{
                        **asdict(existing),
                        "status": "running",
                        "endpoint": getattr(sandbox, "sandbox_domain", None) or getattr(sandbox, "domain", None) or existing.endpoint,
                        "last_seen_at": _timestamp(),
                        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=int(getattr(settings, "idle_ttl_seconds", 300)))).isoformat(),
                    }
                )
                _save_lease(user_id, touched, conn)
                return sandbox
            except Exception as exc:
                logger.warning(
                    "sandbox resume failed user_hash=%s error=%s",
                    user_hash,
                    type(exc).__name__,
                )
                connect_failed = True
                # Fall through to replacement + restore.  Existing metadata,
                # including an older backup, remains intact until restore
                # succeeds.
        if existing.provider_runtime_id and not connect_failed:
            _stop_provider(settings, existing.provider_runtime_id)
        existing = RuntimeLease(
            **{
                **asdict(existing),
                "status": "stopped",
                "stopped_at": _timestamp(),
                "last_seen_at": _timestamp(),
                "endpoint": None,
            }
        )
        _save_lease(user_id, existing, conn)

    if existing and existing.status == "paused" and existing.provider_runtime_id:
        if capability not in existing.capabilities:
            raise AgentRuntimeUnavailable("A different sandbox capability is already paused")
        backup = _backup_metadata(existing)
        try:
            sandbox = _e2b_connect(settings, existing.provider_runtime_id, user_hash)
            if _needs_workspace(existing):
                _sandbox_prepare_workspace(sandbox)
            metadata = dict(existing.metadata)
            metadata.pop("paused_at", None)
            touched = RuntimeLease(
                **{
                    **asdict(existing),
                    "status": "running",
                    "endpoint": None,
                    "last_seen_at": _timestamp(),
                    "expires_at": (now + timedelta(seconds=settings.idle_ttl_seconds)).isoformat(),
                    "metadata": metadata,
                }
            )
            _save_lease(user_id, touched, conn)
            return sandbox
        except Exception:
            # A stopped/expired provider object is replaced below. The old
            # backup remains untouched until the replacement is uploaded.
            pass
        return _e2b_create_and_restore_locked(user_id, capability, settings, conn, existing)

    # A stopped lease can still have a durable backup and should be restored.
    if existing and existing.status in {"stopped", "failed", "expired"}:
        return _e2b_create_and_restore_locked(user_id, capability, settings, conn, existing)

    if _active_runtime_count(conn, now) >= int(getattr(settings, "max_instances", 5)):
        raise AgentRuntimeUnavailable("Tencent Agent Runtime instance budget is exhausted")
    response = _e2b_start(settings, capability)
    lease = _from_response(
        response,
        user_id=user_id,
        capabilities=_runtime_capabilities(capability, settings),
        settings=settings,
        existing_id=existing.id if existing else None,
    )
    sandbox = response.get("_sandbox")
    try:
        if sandbox is None:
            sandbox = _e2b_connect(settings, lease.provider_runtime_id, _hash_user(user_id))
        if _needs_workspace(lease):
            _sandbox_prepare_workspace(sandbox)
    except Exception:
        try:
            _e2b_stop(settings, lease.provider_runtime_id)
        except Exception as cleanup_exc:
            logger.warning(
                "sandbox creation cleanup failed user_hash=%s error=%s",
                _hash_user(user_id),
                type(cleanup_exc).__name__,
            )
        raise
    _save_lease(user_id, lease, conn)
    return sandbox


def connect_user_sandbox(user_id: str, capability: str = "code") -> Any:
    """Return a connected E2B sandbox, resuming or restoring it when needed."""

    settings = config()
    if not settings.enabled or settings.api_mode != "e2b":
        raise AgentRuntimeUnavailable("Tencent Agent Runtime E2B is not configured")
    capability = str(capability or "code")
    if capability not in {"code", "browser"}:
        raise AgentRuntimeUnavailable("Unsupported sandbox capability")
    provider_error = None
    sandbox = None
    with get_connection() as conn:
        _pg_advisory_lock(conn, "luma-runtime-user:" + _hash_user(user_id))
        _require_live_runtime_owner(conn, user_id)
        _pg_advisory_lock(conn, "luma-runtime-budget")
        try:
            sandbox = _connect_user_sandbox_locked(user_id, capability, settings, conn)
        except AgentRuntimeUnavailable as exc:
            # A provider failure after retiring a split lease must still
            # commit its backup reference and stopped state.  Raise only
            # after the transaction exits successfully; DB errors retain the
            # normal rollback behavior.
            provider_error = exc
    if provider_error is not None:
        raise provider_error
    return sandbox


def connect_user_browser(user_id: str) -> tuple[Any, str, str]:
    """Return a browser sandbox and ephemeral CDP/noVNC credentials.

    URLs contain the provider data-plane token and are intentionally created
    only in memory.  Callers must keep CDP private and send noVNC only to the
    owning user's transient tool event.
    """

    sandbox = connect_user_sandbox(user_id, "browser")
    try:
        host = str(sandbox.get_host(9000))
        parsed = urlsplit("https://" + host)
        hostname = (parsed.hostname or "").lower()
        suffix = sandbox_host_suffix()
        if not suffix:
            raise AgentRuntimeUnavailable("SANDBOX_PREVIEW_HOST_SUFFIX 或 E2B_DOMAIN 未配置")
        if (
            not hostname.endswith(suffix)
            or re.fullmatch(r"[A-Za-z0-9.-]+(?::443)?", host) is None
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.path or parsed.query or parsed.fragment
        ):
            raise ValueError("invalid browser endpoint")
        token = str(getattr(sandbox, "_envd_access_token", "") or "")
        if not token:
            raise ValueError("missing browser token")
    except AgentRuntimeUnavailable:
        raise
    except Exception as exc:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime browser endpoint unavailable") from exc
    encoded_token = quote(token, safe="")
    base = "https://" + host
    cdp_url = base + "/cdp?access_token=" + encoded_token
    path = quote("websockify?access_token=" + encoded_token, safe="")
    live_url = base + "/novnc/vnc_lite.html?access_token=" + encoded_token + "&path=" + path
    return sandbox, cdp_url, live_url


def sandbox_status(user_id: str) -> dict[str, Any]:
    lease = get_user_runtime(user_id)
    if lease is None:
        return {"state": "none", "last_seen_at": None, "backup": None}
    if lease.status == "paused":
        state = "paused"
    elif lease.status in _ACTIVE_LEASE_STATUSES:
        state = "running"
    else:
        state = "none"
    backup = _backup_metadata(lease)
    public_backup = None
    if backup:
        try:
            size_bytes = int(backup.get("size_bytes") or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        public_backup = {
            "size_bytes": size_bytes,
            "created_at": backup.get("created_at"),
        }
    return {"state": state, "last_seen_at": lease.last_seen_at, "backup": public_backup}


def reset_user_workspace(user_id: str) -> None:
    """Kill the user's sandbox, remove its durable backup, and stop its lease."""

    settings = config()
    with get_connection() as conn:
        _pg_advisory_lock(conn, "luma-runtime-user:" + _hash_user(user_id))
        existing = get_user_runtime(user_id, conn)
        if not existing:
            return
        if existing.provider_runtime_id and existing.status not in {"stopped", "failed", "expired"}:
            if not bool(getattr(settings, "enabled", False)):
                raise AgentRuntimeUnavailable(
                    "Tencent Agent Runtime is not configured; refusing to orphan an active lease"
                )
            _stop_provider(settings, existing.provider_runtime_id)
        _delete_backup(_backup_metadata(existing))
        stopped_at = _timestamp()
        stopped = RuntimeLease(
            **{
                **asdict(existing),
                "status": "stopped",
                "endpoint": None,
                "stopped_at": stopped_at,
                "last_seen_at": stopped_at,
                "metadata": {},
            }
        )
        _save_lease(user_id, stopped, conn)


def ensure_user_runtime(user_id: str, capabilities: Iterable[str] = ("code",)) -> RuntimeLease:
    """Reuse a live per-user lease, or create a short-lived one on demand.

    This function never silently falls back to the shared FastAPI process.  A
    caller must explicitly handle ``AgentRuntimeUnavailable`` and keep the
    high-risk tools disabled until a real lease exists.
    """

    settings = config()
    configured = (settings.secret_id and settings.secret_key) if settings.api_mode == "cloud-api" else (settings.api_key and (settings.e2b_domain if settings.api_mode == "e2b" else settings.base_url))
    if not settings.enabled or not configured:
        raise AgentRuntimeUnavailable("Tencent Agent Runtime is not configured")
    requested = tuple(dict.fromkeys(str(item) for item in capabilities if str(item) in {"code", "browser"}))
    if not requested:
        requested = ("code",)
    if len(requested) > 1 and not getattr(settings, "aio_tool", ""):
        raise AgentRuntimeUnavailable("A sandbox lease uses one tool template; request code or browser separately")
    requested_capability = requested[0]
    if getattr(settings, "aio_tool", ""):
        requested = ("code", "browser")
    if settings.api_mode == "e2b":
        connect_user_sandbox(user_id, requested_capability)
        lease = get_user_runtime(user_id, capability=requested_capability)
        if lease is None:
            raise AgentRuntimeUnavailable("Tencent Agent Runtime lease disappeared during connect")
        return lease
    # The lock is transaction-scoped.  Keep this connection open through the
    # provider call and local write so two API workers cannot create a second
    # sandbox for one user or race the global instance budget.
    with get_connection() as conn:
        user_hash = _hash_user(user_id)
        _pg_advisory_lock(conn, "luma-runtime-user:" + user_hash)
        _require_live_runtime_owner(conn, user_id)
        _pg_advisory_lock(conn, "luma-runtime-budget")
        existing = get_user_runtime(user_id, conn, requested_capability)
        now = datetime.now(timezone.utc)
        if existing and _lease_is_active(existing, now):
            if not set(requested).issubset(set(existing.capabilities)):
                raise AgentRuntimeUnavailable(
                    "A different sandbox tool lease is active; release it before requesting another capability"
                )
            # A tool invocation is activity: extend the local lease and
            # retain the same sandbox instead of destroying it per command.
            touched = RuntimeLease(
                **{
                    **asdict(existing),
                    "last_seen_at": _timestamp(),
                    "expires_at": (now + timedelta(seconds=settings.idle_ttl_seconds)).isoformat(),
                }
            )
            return _save_lease(user_id, touched, conn)

        # Expired active leases must be stopped before their row is reused.
        # Keeping the old provider id until a successful stop prevents an
        # orphaned sandbox when the provider refuses the release request.
        if existing and existing.status in _ACTIVE_LEASE_STATUSES:
            if existing.provider_runtime_id:
                try:
                    _stop_provider(settings, existing.provider_runtime_id)
                except AgentRuntimeUnavailable as exc:
                    logger.warning(
                        "runtime lease stop before replacement failed user_hash=%s error=%s",
                        existing.user_id_hash,
                        type(exc).__name__,
                    )
                    raise
            existing = RuntimeLease(
                **{
                    **asdict(existing),
                    "status": "stopped",
                    "stopped_at": _timestamp(),
                    "last_seen_at": _timestamp(),
                    "endpoint": None,
                }
            )
            _save_lease(user_id, existing, conn)

        if _active_runtime_count(conn, now) >= settings.max_instances:
            raise AgentRuntimeUnavailable("Tencent Agent Runtime instance budget is exhausted")
        tool_ids = [getattr(settings, "aio_tool", "")] if getattr(settings, "aio_tool", "") else [settings.code_tool if item == "code" else settings.browser_tool for item in requested]
        if settings.api_mode == "cloud-api":
            response = _cloud_start(settings, requested, user_id)
        elif settings.api_mode == "gateway":
            payload = {
                "region": settings.region,
                "tool_ids": tool_ids,
                "owner_hash": user_hash,
                "lifecycle": {"idle_ttl_seconds": settings.idle_ttl_seconds, "auto_release": True},
                "limits": {"max_instances": settings.max_instances, "max_cpu": settings.max_cpu, "max_memory_gib": settings.max_memory_gib},
                "idempotency_key": "luma-" + user_hash + "-" + _capability_key(requested),
            }
            response = _request("POST", settings.create_path, payload=payload, settings=settings)
        else:
            raise AgentRuntimeUnavailable("Unsupported Tencent Agent Runtime API mode")
        lease = _from_response(
            response,
            user_id=user_id,
            capabilities=requested,
            settings=settings,
            existing_id=existing.id if existing else None,
        )
        return _save_lease(user_id, lease, conn)


def touch_user_runtime(user_id: str, capability: str = "code") -> Optional[RuntimeLease]:
    """Renew the local idle lease after a command/job uses its sandbox.

    No provider release occurs here.  The runtime worker calls this after a
    command completes so a user-opened sandbox and concurrent jobs continue to
    share the same provider instance until it is idle or explicitly deleted.
    """

    settings = config()
    with get_connection() as conn:
        _pg_advisory_lock(conn, "luma-runtime-user:" + _hash_user(user_id))
        existing = get_user_runtime(user_id, conn, capability)
        if not existing or not _lease_is_active(existing):
            return existing
        if settings.api_mode == "e2b" and existing.provider_runtime_id:
            # Renew the provider TTL as well as our local lease.  A failure is
            # logged by _e2b_connect/_e2b_refresh_timeout without exposing
            # provider response data.
            try:
                _e2b_connect(settings, existing.provider_runtime_id, existing.user_id_hash)
            except Exception as exc:
                logger.warning(
                    "sandbox timeout renewal connect failed user_hash=%s error=%s",
                    existing.user_id_hash,
                    type(exc).__name__,
                )
        now = _timestamp()
        touched = RuntimeLease(
            **{
                **asdict(existing),
                "last_seen_at": now,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(seconds=settings.idle_ttl_seconds)
                ).isoformat(),
            }
        )
        return _save_lease(user_id, touched, conn)


def reap_expired_leases() -> dict[str, int]:
    """Pause expired E2B sandboxes after backup; stop other providers."""

    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM runtime_leases WHERE status IN ('running','ready','starting','paused')"
        ).fetchall()
    expired = []
    paused_retention = []
    for row in rows:
        lease = _row_to_lease(row)
        if not lease:
            continue
        if lease.status in _ACTIVE_LEASE_STATUSES and (
            not lease.expires_at or (_parse_timestamp(lease.expires_at) or now) <= now
        ):
            expired.append(lease)
        elif lease.status == "paused":
            paused_at = _parse_timestamp(lease.metadata.get("paused_at")) or _parse_timestamp(lease.last_seen_at)
            settings = config()
            retention_days = int(getattr(settings, "sandbox_paused_retention_days", 30))
            if paused_at and paused_at + timedelta(days=retention_days) <= now:
                paused_retention.append(lease)

    stopped = 0
    failed = 0
    for candidate in expired + paused_retention:
        try:
            with get_connection() as conn:
                _pg_advisory_lock(conn, "luma-runtime-user:" + candidate.user_id_hash)
                # Serialize the paused count, eviction and pause operation so
                # two maintenance workers cannot both consume the last slot.
                _pg_advisory_lock(conn, "luma-runtime-budget")
                row = conn.execute(
                    "SELECT * FROM runtime_leases WHERE id = ? LIMIT 1", (candidate.id,)
                ).fetchone()
                lease = _row_to_lease(row)
                if not lease:
                    continue
                settings = config()
                if lease.status == "paused":
                    paused_at = _parse_timestamp(lease.metadata.get("paused_at")) or _parse_timestamp(lease.last_seen_at)
                    retention_days = int(getattr(settings, "sandbox_paused_retention_days", 30))
                    if not paused_at or paused_at + timedelta(days=retention_days) > datetime.now(timezone.utc):
                        continue
                    _stop_provider(settings, lease.provider_runtime_id)
                    stopped_at = _timestamp()
                    stopped_lease = RuntimeLease(
                        **{
                            **asdict(lease),
                            "status": "stopped",
                            "stopped_at": stopped_at,
                            "last_seen_at": stopped_at,
                            "endpoint": None,
                        }
                    )
                    _save_lease_hash(lease.user_id_hash, stopped_lease, conn)
                    stopped += 1
                    continue
                if lease.status not in _ACTIVE_LEASE_STATUSES:
                    continue
                expiry = _parse_timestamp(lease.expires_at)
                if expiry is not None and expiry > datetime.now(timezone.utc):
                    continue

                if settings.api_mode == "e2b":
                    sandbox = _e2b_connect(settings, lease.provider_runtime_id, lease.user_id_hash)
                    old_backup = _backup_metadata(lease)
                    backup = None
                    try:
                        if _needs_workspace(lease):
                            backup = _sandbox_archive(sandbox, settings, lease.user_id_hash)
                    except Exception as exc:
                        # Backup failure must not prevent pausing; retain the
                        # old backup and write only an exception type marker.
                        logger.warning(
                            "sandbox backup failed user_hash=%s error=%s",
                            lease.user_id_hash,
                            type(exc).__name__,
                        )
                    # Tencent limits the number of paused instances. Evict a
                    # recoverable oldest lease before consuming another slot.
                    _evict_paused_for_capacity(settings, conn)
                    try:
                        _sandbox_pause(sandbox)
                    except Exception as pause_exc:
                        if not _pause_quota_error(pause_exc):
                            raise
                        # A concurrent worker may have consumed the slot after
                        # the count above; evict once and retry the pause.
                        _evict_paused_for_capacity(settings, conn)
                        try:
                            _sandbox_pause(sandbox)
                        except Exception as retry_exc:
                            # A browser has no workspace to lose.  If the
                            # paused quota remains full, release it instead.
                            if _needs_workspace(lease) or not _pause_quota_error(retry_exc):
                                raise
                            _stop_provider(settings, lease.provider_runtime_id)
                            _save_lease_hash(
                                lease.user_id_hash,
                                RuntimeLease(**{
                                    **asdict(lease), "status": "stopped", "endpoint": None,
                                    "stopped_at": _timestamp(), "last_seen_at": _timestamp(),
                                }), conn,
                            )
                            stopped += 1
                            continue
                    metadata = dict(lease.metadata)
                    metadata["paused_at"] = _timestamp()
                    old_backup_to_delete = None
                    if backup and backup.get("storage_key"):
                        metadata["backup"] = backup
                        metadata.pop("backup_skipped", None)
                        if old_backup and old_backup.get("storage_key") != backup.get("storage_key"):
                            old_backup_to_delete = old_backup
                    elif backup and backup.get("backup_skipped"):
                        metadata["backup_skipped"] = backup["backup_skipped"]
                    stopped_lease = RuntimeLease(
                        **{
                            **asdict(lease),
                            "status": "paused",
                            "endpoint": None,
                            "last_seen_at": _timestamp(),
                            "metadata": metadata,
                        }
                    )
                else:
                    _stop_provider(settings, lease.provider_runtime_id)
                    stopped_lease = RuntimeLease(
                        **{
                            **asdict(lease),
                            "status": "stopped",
                            "stopped_at": _timestamp(),
                            "last_seen_at": _timestamp(),
                            "endpoint": None,
                        }
                    )
                _save_lease_hash(lease.user_id_hash, stopped_lease, conn)
                if settings.api_mode == "e2b" and old_backup_to_delete:
                    # Persist the new backup reference before removing the
                    # previous object.  A failed upload never reaches here,
                    # so the previous backup remains recoverable.
                    _delete_backup(old_backup_to_delete)
                stopped += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "runtime expired lease operation failed user_hash=%s error=%s",
                candidate.user_id_hash,
                type(exc).__name__,
            )
            try:
                with get_connection() as conn:
                    current_row = conn.execute(
                        "SELECT * FROM runtime_leases WHERE id = ? LIMIT 1", (candidate.id,)
                    ).fetchone()
                    current = _row_to_lease(current_row)
                    if current:
                        metadata = dict(current.metadata)
                        metadata["reap_error"] = type(exc).__name__
                        _save_lease_hash(
                            candidate.user_id_hash,
                            RuntimeLease(**{**asdict(current), "metadata": metadata}),
                            conn,
                        )
            except Exception:
                logger.warning(
                    "runtime expired lease error marker update failed user_hash=%s",
                    candidate.user_id_hash,
                )
    return {"stopped": stopped, "failed": failed, "expired": len(expired)}


def refresh_user_runtime(user_id: str) -> RuntimeLease:
    """Refresh provider state for a persisted lease without creating one."""

    settings = config()
    existing = get_user_runtime(user_id)
    if not existing or not existing.provider_runtime_id:
        raise AgentRuntimeUnavailable("No runtime lease exists for this user")
    response = _cloud_get(settings, existing.provider_runtime_id) if settings.api_mode == "cloud-api" else _request("GET", _normalise_path(settings.get_path, existing.provider_runtime_id), settings=settings)
    nested = response.get("data") if isinstance(response.get("data"), dict) else response
    refreshed = RuntimeLease(
        **{
            **asdict(existing),
            "status": str(nested.get("status") or nested.get("Status") or existing.status).lower(),
            "endpoint": nested.get("endpoint") or nested.get("url") or existing.endpoint,
            "expires_at": nested.get("expires_at") or nested.get("ExpiresAt") or existing.expires_at,
            "last_seen_at": _timestamp(),
        }
    )
    return _save_lease(user_id, refreshed)


def release_all_user_runtimes(user_id: str) -> list[RuntimeLease]:
    """Stop every capability and erase its backup before account deletion."""
    settings = config()
    with get_connection() as conn:
        _pg_advisory_lock(conn, "luma-runtime-user:" + _hash_user(user_id))
        leases = [_row_to_lease(row) for row in conn.execute(
            "SELECT * FROM runtime_leases WHERE user_id_hash = ? ORDER BY capability", (_hash_user(user_id),)
        ).fetchall()]
        released = []
        for lease in leases:
            if lease is None:
                continue
            if lease.provider_runtime_id and lease.status not in {"stopped", "failed", "expired"}:
                if not settings.enabled:
                    raise AgentRuntimeUnavailable("Runtime is not configured; refusing to orphan an active lease")
                _stop_provider(settings, lease.provider_runtime_id)
            backup = _backup_metadata(lease)
            if backup and backup.get("storage_key"):
                # Erasure must report unavailable storage instead of silently
                # dropping the only remaining object reference.
                get_storage().delete(str(backup["storage_key"]))
            stopped_at = _timestamp()
            metadata = dict(lease.metadata)
            metadata.pop("backup", None)
            stopped = RuntimeLease(**{
                **asdict(lease), "status": "stopped", "stopped_at": stopped_at,
                "last_seen_at": stopped_at, "endpoint": None, "metadata": metadata,
            })
            released.append(_save_lease(user_id, stopped, conn))
    return released


def release_user_runtime(user_id: str) -> Optional[RuntimeLease]:
    """Release the user's provider sandbox and mark the local lease stopped."""

    settings = config()
    # Keep the user lock through provider stop and the local write.  Otherwise
    # an overlapping ensure request can create a new sandbox while DELETE is
    # still stopping the previous one, and the later upsert can overwrite the
    # newly-created provider id.
    with get_connection() as conn:
        _pg_advisory_lock(conn, "luma-runtime-user:" + _hash_user(user_id))
        existing = get_user_runtime(user_id, conn)
        if not existing:
            return None
        if not settings.enabled and existing.status not in {"stopped", "failed", "expired"}:
            raise AgentRuntimeUnavailable("Tencent Agent Runtime is not configured; refusing to orphan an active lease")
        if settings.enabled and existing.provider_runtime_id:
            _stop_provider(settings, existing.provider_runtime_id)
        stopped_at = _timestamp()
        stopped = RuntimeLease(
            **{
                **asdict(existing),
                "status": "stopped",
                "stopped_at": stopped_at,
                "last_seen_at": stopped_at,
                "endpoint": None,
            }
        )
        return _save_lease(user_id, stopped, conn)
