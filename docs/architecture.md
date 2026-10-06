# Architecture

Luma is a personal assistant with a FastAPI gateway, a React web client, a
Flutter mobile client and an Electron desktop shell. Clients call JSON APIs and
server-sent event streams. Provider keys and integration credentials belong to
the backend environment.

```text
Web / Mobile / Desktop -> FastAPI -> PostgreSQL
                            |     -> Redis
                            |     -> File storage
                            |     -> Compatible model endpoint
                            |     -> Public HTTPS connectors
                            +---- -> Per-user cloud sandbox
```

PostgreSQL is the durable source of truth for accounts, conversations, messages,
files, memories, tasks, runtime jobs, approvals and notifications. Business
records retain their `user_id` ownership; authenticated routers check ownership
before reading or changing a record. Startup runs Alembic upgrades inside a
database advisory lock, allowing multiple API workers to start safely.

Accounts use a unique username and optional lowercase email. The first
registered account becomes an administrator; later registration follows the
server's open, invite or closed policy. Passwords use Argon2id with a scrypt
fallback. Login accepts a username or email and applies account/IP rate limits.
Redis holds hashed bearer/cookie sessions with sliding and absolute expiry.
Changing a password invalidates other sessions; disabling or deleting an
account invalidates every session. Administrator access uses account roles and
an optional extra user-ID allowlist.

The `0018_accounts` upgrade preserves existing ownership identifiers. Legacy
profiles receive generated usernames and need interactive operator enrollment
before password login becomes available; see the
[upgrade procedure](../deploy/README.md#upgrading-an-existing-database).

Generation tasks persist assistant results and publish resumable events. Redis
coordinates cross-worker session state, counters and event transport. Context
and memory services bound message, attachment and token counts before calling
the configured `LLM_BASE_URL` / `LLM_MODEL`. Background workers recover expired
jobs and perform file cleanup; the scheduler serializes its durable work with
database locks.

The tool registry provides an explicit set of operations, with policy checks and
approval records for sensitive actions. User/model commands execute only in
Tencent Cloud Agent Runtime sandboxes, never in the gateway's host process.
Each sandbox lease belongs to one user and has bounded lifetime and capacity.
Connector tokens are encrypted with Fernet. Connector endpoints require public
HTTPS, and tool output remains untrusted data. Clients render structured widget
data and allow only HTTP/HTTPS external links.

File metadata records both its owner and its storage backend. Content can live
on local disk, Tencent COS or a separate HTTPS file service. Storage credentials
and internal object keys stay server-side. Download and deletion routes check
the same owner boundary as conversation and memory routes.

The Docker image serves the compiled React bundle from the same gateway.
Compose includes isolated PostgreSQL/Redis services and persistent volumes.
See [deployment instructions](../deploy/README.md) for environment configuration,
health checks and backup/restore procedures.
