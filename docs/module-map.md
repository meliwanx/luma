# Backend module map

The ASGI entry point is `app.main:app`. HTTP routers depend on services and
shared modules; services must not import the application entry point.

```text
main -> routers -> services -> db / auth / provider / storage / runtime
```

| Module | Responsibility |
| --- | --- |
| `app/main.py` | Application lifecycle, CORS, middleware, routers and static web files |
| `app/config.py` | Shared environment settings and web bundle paths |
| `app/db.py`, `migrations/` | PostgreSQL pooling, SQL adaptation and serialized migrations |
| `app/auth.py` | Independent account credentials, registration policy and Redis sessions |
| `app/deps.py` | Authenticated owner resolution and ownership checks |
| `app/routers/auth.py` | Registration, login, logout and session endpoints |
| `app/routers/account.py` | Profile/password updates and account deletion |
| `app/admin.py` | Role-protected administration, user status and telemetry views |
| `app/models.py`, `app/mappers.py` | Validated API data and row-to-model conversion |
| `app/provider.py` | Compatible model API client and offline fallback |
| `app/services/chat.py`, `generation.py`, `context.py` | Conversation persistence, streaming and bounded model context |
| `app/services/memory.py` | Visible user memories and bounded background extraction |
| `app/services/files.py`, `app/storage.py` | Upload limits, private storage, download and cleanup |
| `app/services/export.py` | Portable per-user data export without credentials |
| `app/agent/` | Tool registry, policy checks and deterministic agent loop |
| `app/mcp.py`, `app/services/mcp_catalog.py` | Public HTTPS connectors and encrypted tokens |
| `app/runtime.py`, `app/agent_runtime.py` | Durable jobs, per-user sandbox leases and cloud execution |
| `app/scheduler.py` | Durable task reminders and routine scheduling |
| `app/services/notifications.py`, `app/push.py` | Notifications and optional mobile push delivery |
| `app/services/voice.py` | Optional server-side speech recognition |
| `app/telemetry.py` | Request metadata and aggregate metrics without bodies/query strings |
| `app/upload_limit.py` | Early upload body-size enforcement |

All account-scoped queries use `user_id`; SQL values use bound parameters.
Connection scopes remain short, especially around remote requests. Background
workers and shared clients close during application shutdown. Tests run in one
process with an isolated PostgreSQL schema.
